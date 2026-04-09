import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional
from playwright.async_api import async_playwright, Browser, Page

from db import FlightRecord


@dataclass
class ScrapeResult:
    records: List[FlightRecord] = field(default_factory=list)
    available_dates: List[str] = field(default_factory=list)

logger = logging.getLogger(__name__)

URL = "https://www.elal.com/heb/seat-availability"

IATA_RE = re.compile(r"\(([A-Z]{3})\)")

PARSE_JS = """
() => {
    const headerRow = document.querySelector('.header-row .header-dates');
    const dates = headerRow
        ? Array.from(headerRow.querySelectorAll('.header-date')).map(d => d.textContent.trim())
        : [];
    if (dates.length === 0) return {error: 'no_dates', flights: []};

    const items = document.querySelectorAll('app-seat-availability-item');
    const flights = [];

    items.forEach(item => {
        const desktop = item.querySelector('.desktop-layout');
        if (!desktop) return;

        const flightNum = (desktop.querySelector('.flight-number-value') || {}).textContent?.trim() || '';
        const flightTime = (desktop.querySelector('.flight-time-value') || {}).textContent?.trim() || '';

        const originEl = desktop.querySelector('.origin-value');
        let destination = '';
        if (originEl) {
            destination = originEl.textContent.trim().replace(/^\\s+|\\s+$/g, '').replace(/\\s+/g, ' ');
        }

        const allLinks = desktop.querySelectorAll('a.availability-cell');
        let direction = null;
        for (const a of allLinks) {
            const href = a.href || '';
            if (href.includes('from=TLV')) { direction = 'outbound'; break; }
            if (href.includes('dest=TLV')) { direction = 'inbound'; break; }
        }

        const cellWrappers = desktop.querySelectorAll('.availability-dates > .availability-cell-wrapper');
        cellWrappers.forEach((wrapper, idx) => {
            if (idx >= dates.length) return;
            const date = dates[idx];

            const noFlight = wrapper.querySelector('.no-flight-icon');
            if (noFlight) {
                flights.push({fn: flightNum, dest: destination, time: flightTime, date, seats: null, url: null, dir: direction});
                return;
            }

            const link = wrapper.querySelector('a.availability-cell');
            const url = link ? link.href : null;

            const valueEl = wrapper.querySelector('.availability-value');
            if (valueEl) {
                const text = valueEl.textContent.trim();
                let seats = text.includes('+') ? (parseInt(text) || 10) : (parseInt(text) || 0);
                flights.push({fn: flightNum, dest: destination, time: flightTime, date, seats, url, dir: direction});
                return;
            }

            const cell = wrapper.querySelector('.availability-cell');
            const cellText = cell ? cell.textContent.trim() : '';
            if (cellText) {
                let seats = cellText.includes('+') ? (parseInt(cellText) || 10) : (parseInt(cellText) || 0);
                flights.push({fn: flightNum, dest: destination, time: flightTime, date, seats, url, dir: direction});
            } else {
                flights.push({fn: flightNum, dest: destination, time: flightTime, date, seats: null, url: null, dir: direction});
            }
        });
    });

    return {error: null, dates, flightCount: items.length, flights};
}
"""


def _extract_iata(destination: str) -> Optional[str]:
    m = IATA_RE.search(destination)
    return m.group(1) if m else None


DIRECTION_URLS = {
    "outbound": URL,
    "inbound": URL + "?d=1",
}


class Scraper:
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._page_ready = False

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("Browser launched")

    async def stop(self):
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def _restart_browser(self):
        logger.warning("Restarting browser...")
        self._page = None
        self._page_ready = False
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def _ensure_page(self) -> Page:
        if not self._browser or not self._browser.is_connected():
            await self._restart_browser()
        if self._page is None or self._page.is_closed():
            self._page = await self._browser.new_page()
            self._page_ready = False
        return self._page

    async def scrape(self) -> ScrapeResult:
        page = await self._ensure_page()
        try:
            return await self._do_scrape(page)
        except Exception:
            self._page = None
            self._page_ready = False
            raise

    async def _scroll_all(self, page: Page) -> int:
        prev_count = 0
        stable_rounds = 0
        for _ in range(40):
            await page.evaluate("window.scrollBy(0, 3000)")
            await page.wait_for_timeout(300)
            count = await page.evaluate("document.querySelectorAll('app-seat-availability-item').length")
            if count == prev_count:
                stable_rounds += 1
                if stable_rounds >= 3:
                    break
            else:
                stable_rounds = 0
                prev_count = count
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
        return prev_count

    def _build_records(self, raw_flights: list, force_direction: str) -> List[FlightRecord]:
        records = []
        for f in raw_flights:
            if not f["fn"]:
                continue
            direction = force_direction or f.get("dir") or "outbound"
            book_url = f.get("url")
            if not book_url and f["seats"] is not None and f["seats"] > 0:
                iata = _extract_iata(f["dest"])
                if iata:
                    dd, mm = f["date"].split(".")
                    if direction == "inbound":
                        book_url = f"https://www.elal.com/heb/book-a-flight?from={iata}&dest=TLV&dtfrom=2026-{mm}-{dd}&journey=one_way"
                    else:
                        book_url = f"https://www.elal.com/heb/book-a-flight?from=TLV&dest={iata}&dtfrom=2026-{mm}-{dd}&journey=one_way"
            records.append(FlightRecord(
                flight_number=f["fn"],
                destination=f["dest"],
                time=f["time"],
                date=f["date"],
                seats=f["seats"],
                book_url=book_url,
                direction=direction,
            ))
        return records

    async def _scrape_tab(self, page: Page, direction: str) -> tuple:
        """Scroll the current tab and parse flights. Returns (records, dates)."""
        item_count = await self._scroll_all(page)
        logger.info("Scrolled %s tab, loaded %d flight items", direction, item_count)

        result = await page.evaluate(PARSE_JS)
        if result.get("error"):
            logger.error("Scrape parse error (%s): %s", direction, result["error"])
            return [], []

        available_dates = result.get("dates", [])
        logger.info(
            "Parsed %s: %d items, %d raw records, dates: %s",
            direction, result.get("flightCount", 0),
            len(result.get("flights", [])), available_dates,
        )
        records = self._build_records(result.get("flights", []), direction)
        return records, available_dates

    async def _load_page(self, page: Page, url: str):
        """Navigate to a URL and wait for content to appear."""
        logger.info("Navigating to %s ...", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        if not self._page_ready:
            try:
                btn = page.locator('button:has-text("I understand")')
                if await btn.count() > 0:
                    await btn.first.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

        await page.wait_for_selector(".seat-availability-page", state="attached", timeout=25000)
        await page.wait_for_selector(".header-date", state="attached", timeout=20000)
        await page.wait_for_timeout(1000)

    async def _do_scrape(self, page: Page) -> ScrapeResult:
        all_records: List[FlightRecord] = []
        available_dates: List[str] = []

        for direction in ("outbound", "inbound"):
            await self._load_page(page, DIRECTION_URLS[direction])
            records, dates = await self._scrape_tab(page, direction)
            all_records.extend(records)
            if dates:
                available_dates = dates

        self._page_ready = True

        out_count = sum(1 for r in all_records if r.direction == "outbound")
        in_count = sum(1 for r in all_records if r.direction == "inbound")
        dests = set(r.destination for r in all_records)
        logger.info(
            "Scraped %d records (%d outbound, %d inbound) across %d destinations, dates: %s",
            len(all_records), out_count, in_count, len(dests), available_dates,
        )
        return ScrapeResult(records=all_records, available_dates=available_dates)
