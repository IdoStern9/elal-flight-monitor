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


class Scraper:
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("Browser launched")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def _restart_browser(self):
        logger.warning("Restarting browser...")
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def scrape(self) -> ScrapeResult:
        if not self._browser or not self._browser.is_connected():
            await self._restart_browser()

        page = await self._browser.new_page()
        try:
            return await self._do_scrape(page)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _do_scrape(self, page: Page) -> ScrapeResult:
        logger.info("Navigating to El Al seat availability page...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        try:
            btn = page.locator('button:has-text("I understand")')
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass

        await page.wait_for_selector(".seat-availability-page", state="attached", timeout=25000)
        await page.wait_for_selector(".header-date", state="attached", timeout=20000)
        await page.wait_for_timeout(2000)

        # Scroll to load all destinations (page uses lazy rendering)
        # Do two full passes to ensure everything loads
        for pass_num in range(2):
            prev_count = 0
            stable_rounds = 0
            for _ in range(40):
                await page.evaluate("window.scrollBy(0, 2000)")
                await page.wait_for_timeout(500)
                count = await page.evaluate("document.querySelectorAll('app-seat-availability-item').length")
                if count == prev_count:
                    stable_rounds += 1
                    if stable_rounds >= 4:
                        break
                else:
                    stable_rounds = 0
                    prev_count = count
            if pass_num == 0:
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1000)

        logger.info("Scrolled page, loaded %d flight items", prev_count)

        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)

        result = await page.evaluate(PARSE_JS)

        if result.get("error"):
            logger.error("Scrape parse error: %s", result["error"])
            return []

        logger.info(
            "Parsed %d flight items, %d raw records, dates: %s",
            result.get("flightCount", 0),
            len(result.get("flights", [])),
            result.get("dates", []),
        )

        available_dates = result.get("dates", [])

        records = []
        for f in result.get("flights", []):
            if not f["fn"]:
                continue
            if f.get("dir") == "inbound":
                continue

            book_url = f.get("url")
            if not book_url and f["seats"] is not None and f["seats"] > 0:
                iata = _extract_iata(f["dest"])
                if iata:
                    dd, mm = f["date"].split(".")
                    book_url = f"https://www.elal.com/heb/book-a-flight?from=TLV&dest={iata}&dtfrom=2026-{mm}-{dd}&journey=one_way"

            records.append(FlightRecord(
                flight_number=f["fn"],
                destination=f["dest"],
                time=f["time"],
                date=f["date"],
                seats=f["seats"],
                book_url=book_url,
            ))

        dests = set(r.destination for r in records)
        logger.info("Scraped %d outbound records across %d destinations, dates: %s", len(records), len(dests), available_dates)
        return ScrapeResult(records=records, available_dates=available_dates)
