import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import (
    init_db, process_scrape, get_flights_json, get_changes_json, Change,
    get_all_ntfy_configs, get_ntfy_config, save_ntfy_config, delete_ntfy_config,
    get_all_destinations,
)
from scraper import Scraper


class NtfyConfigBody(BaseModel):
    id: Optional[int] = None
    name: str = "Notification"
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    topic: str = ""
    mode: str = "all"
    min_seats: int = 1
    destinations: List[str] = []
    triggers: List[str] = ["new_flight", "seats_available"]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scraper = Scraper()
scheduler = AsyncIOScheduler()
connected_clients: List[WebSocket] = []
_http_client: httpx.AsyncClient | None = None

scraper_status = {
    "last_scrape": None,
    "last_success": None,
    "last_error": None,
    "is_running": False,
    "available_dates": [],
}


def _extract_iata(dest: str) -> str:
    m = re.search(r"\(([A-Z]{3})\)", dest)
    return m.group(1) if m else ""


def _dest_name_ascii(dest: str) -> str:
    """Extract the destination name, stripping the IATA code parenthetical."""
    return dest.replace(f"({_extract_iata(dest)})", "").replace(",", ",").strip()


def _match_trigger(cfg: dict, c: Change) -> Optional[str]:
    """Check if a change matches the config's trigger rules. Returns a label or None."""
    triggers = set(cfg.get("triggers", ["new_flight", "seats_available"]))
    min_s = cfg["min_seats"]

    if c.change_type == "new_flight":
        if "new_flight" in triggers and c.new_seats is not None and c.new_seats >= min_s:
            return "new_flight"

    elif c.change_type == "seats_changed":
        went_above = (c.new_seats is not None and c.new_seats >= min_s
                      and (c.old_seats is None or c.old_seats < min_s))
        went_down = (c.old_seats is not None and c.new_seats is not None
                     and c.new_seats < c.old_seats)

        if "seats_available" in triggers and went_above:
            return "seats_available"
        if "seats_decreased" in triggers and went_down:
            return "seats_decreased"
        if "seats_changed" in triggers and not went_above and not went_down:
            return "seats_changed"

    elif c.change_type == "flight_removed":
        if "flight_removed" in triggers:
            return "flight_removed"

    return None


_TRIGGER_TITLES = {
    "new_flight":      lambda iata, s, _o: f"New flight: TLV -> {iata} ({s} seats)",
    "seats_available": lambda iata, s, o:  f"Seats available: TLV -> {iata} ({o} -> {s})",
    "seats_changed":   lambda iata, s, o:  f"Seats update: TLV -> {iata} ({o} -> {s})",
    "seats_decreased": lambda iata, s, o:  f"Seats dropping: TLV -> {iata} ({o} -> {s})",
    "flight_removed":  lambda iata, _s, _o: f"Flight removed: TLV -> {iata}",
}

_TRIGGER_PRIORITY = {
    "new_flight": "high",
    "seats_available": "high",
    "seats_decreased": "urgent",
    "seats_changed": "default",
    "flight_removed": "low",
}

_TRIGGER_TAGS = {
    "new_flight": "airplane,new",
    "seats_available": "airplane,white_check_mark",
    "seats_decreased": "airplane,warning",
    "seats_changed": "airplane",
    "flight_removed": "airplane,x",
}


async def _send_for_config(cfg: dict, changes: List[Change]):
    """Send one ntfy push per matching change."""
    global _http_client
    url = f"{cfg['server_url'].rstrip('/')}/{cfg['topic']}"

    for c in changes:
        iata = _extract_iata(c.destination)
        if cfg["mode"] == "selected" and iata not in cfg["destinations"]:
            continue

        trigger = _match_trigger(cfg, c)
        if not trigger:
            continue

        new_s = "9+" if (c.new_seats or 0) >= 9 else str(c.new_seats or 0)
        old_s = str(c.old_seats) if c.old_seats is not None else "0"
        dest_name = _dest_name_ascii(c.destination)

        title = _TRIGGER_TITLES[trigger](iata, new_s, old_s)

        body_lines = [
            f"Flight: {c.flight_number}",
            f"Route: TLV -> {iata} {dest_name}",
            f"Date: {c.date}",
            f"Time: {c.time}",
        ]
        if trigger == "flight_removed":
            if c.old_seats is not None:
                body_lines.append(f"Had: {old_s} seats")
        elif trigger in ("seats_available", "seats_changed", "seats_decreased"):
            body_lines.append(f"Seats: {old_s} -> {new_s}")
        else:
            body_lines.append(f"Seats: {new_s}")

        headers = {
            "Title": title,
            "Priority": _TRIGGER_PRIORITY.get(trigger, "default"),
            "Tags": _TRIGGER_TAGS.get(trigger, "airplane"),
        }

        if trigger != "flight_removed" and iata:
            dd, mm = c.date.split(".")
            book_url = f"https://www.elal.com/heb/book-a-flight?from=TLV&dest={iata}&dtfrom=2026-{mm}-{dd}&journey=one_way"
            headers["Click"] = book_url

        try:
            if _http_client is None:
                _http_client = httpx.AsyncClient(timeout=10)
            resp = await _http_client.post(url, content="\n".join(body_lines), headers=headers)
            logger.info("ntfy [%s] %s %s %s %s -> [%d]", cfg["name"], trigger, c.flight_number, iata, c.date, resp.status_code)
        except Exception:
            logger.exception("ntfy [%s] send failed for %s", cfg["name"], c.flight_number)


async def send_ntfy_alerts(changes: List[Change]):
    """Iterate all enabled ntfy configs and send matching alerts."""
    configs = get_all_ntfy_configs()
    for cfg in configs:
        if cfg["enabled"] and cfg["topic"]:
            await _send_for_config(cfg, changes)


async def broadcast(message: dict):
    dead = []
    data = json.dumps(message, ensure_ascii=False)
    for ws in connected_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.remove(ws)


async def run_scrape():
    if scraper_status["is_running"]:
        logger.warning("Scrape already in progress, skipping")
        return

    scraper_status["is_running"] = True
    try:
        logger.info("Starting scrape cycle...")
        result = await scraper.scrape()
        changes = process_scrape(result.records)
        now = datetime.now().isoformat(timespec="seconds")
        scraper_status["last_scrape"] = now
        scraper_status["last_success"] = now
        scraper_status["last_error"] = None
        scraper_status["available_dates"] = result.available_dates

        logger.info("Scrape complete: %d records, %d changes, dates: %s", len(result.records), len(changes), result.available_dates)

        if changes:
            await send_ntfy_alerts(changes)

        await broadcast({
            "type": "scrape_complete",
            "timestamp": now,
            "record_count": len(result.records),
            "available_dates": result.available_dates,
            "changes": [
                {
                    "timestamp": c.timestamp,
                    "flight_number": c.flight_number,
                    "destination": c.destination,
                    "time": c.time,
                    "date": c.date,
                    "old_seats": c.old_seats,
                    "new_seats": c.new_seats,
                    "change_type": c.change_type,
                }
                for c in changes
            ],
            "flights": get_flights_json(),
        })
    except Exception as e:
        logger.exception("Scrape failed")
        scraper_status["last_error"] = str(e)
        scraper_status["last_scrape"] = datetime.now().isoformat(timespec="seconds")
        await broadcast({"type": "scrape_error", "error": str(e), "timestamp": scraper_status["last_scrape"]})
    finally:
        scraper_status["is_running"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    os.chdir(Path(__file__).parent)
    init_db()
    await scraper.start()
    _http_client = httpx.AsyncClient(timeout=10)
    scheduler.add_job(run_scrape, "interval", seconds=5, id="scraper", next_run_time=datetime.now())
    scheduler.start()
    logger.info("Flight alert system started -- dashboard at http://localhost:8080")
    yield
    scheduler.shutdown(wait=False)
    await scraper.stop()
    if _http_client:
        await _http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


PLANE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<text y="56" font-size="56">✈</text></svg>'
)

@app.get("/favicon.ico")
@app.get("/%E2%9C%88%EF%B8%8F")
async def favicon():
    return Response(content=PLANE_SVG, media_type="image/svg+xml")


@app.get("/api/flights")
async def api_flights():
    return get_flights_json()


@app.get("/api/changes")
async def api_changes(limit: int = 100):
    return get_changes_json(limit)


@app.get("/api/status")
async def api_status():
    return scraper_status


@app.get("/api/ntfy/configs")
async def api_ntfy_list():
    return get_all_ntfy_configs()


@app.post("/api/ntfy/configs")
async def api_ntfy_save(body: NtfyConfigBody):
    saved = save_ntfy_config(body.model_dump())
    return saved


@app.delete("/api/ntfy/configs/{config_id}")
async def api_ntfy_delete(config_id: int):
    delete_ntfy_config(config_id)
    return {"ok": True}


@app.post("/api/ntfy/test/{config_id}")
async def api_ntfy_test(config_id: int):
    """Send a test notification for a specific config."""
    cfg = get_ntfy_config(config_id)
    if not cfg:
        return {"ok": False, "error": "Config not found"}
    if not cfg["topic"]:
        return {"ok": False, "error": "No topic configured"}
    url = f"{cfg['server_url'].rstrip('/')}/{cfg['topic']}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                content=f"El Al Flight Monitor test - {cfg['name']}",
                headers={"Title": "El Al Monitor - Test", "Tags": "white_check_mark,airplane"},
            )
        return {"ok": resp.status_code < 300, "status": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/destinations")
async def api_destinations():
    return get_all_destinations()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    logger.info("WebSocket client connected (%d total)", len(connected_clients))
    try:
        await ws.send_text(json.dumps({
            "type": "initial",
            "flights": get_flights_json(),
            "changes": get_changes_json(50),
            "status": scraper_status,
        }, ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in connected_clients:
            connected_clients.remove(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(connected_clients))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
