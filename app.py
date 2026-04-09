import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import init_db, process_scrape, get_flights_json, get_changes_json, Change
from scraper import Scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scraper = Scraper()
scheduler = AsyncIOScheduler()
connected_clients: List[WebSocket] = []

scraper_status = {
    "last_scrape": None,
    "last_success": None,
    "last_error": None,
    "is_running": False,
    "available_dates": [],
}


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
    os.chdir(Path(__file__).parent)
    init_db()
    await scraper.start()
    scheduler.add_job(run_scrape, "interval", seconds=10, id="scraper", next_run_time=datetime.now())
    scheduler.start()
    logger.info("Flight alert system started -- dashboard at http://localhost:8080")
    yield
    scheduler.shutdown(wait=False)
    await scraper.stop()


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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    logger.info("WebSocket client connected (%d total)", len(connected_clients))
    try:
        # Send current state immediately
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
