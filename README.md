# El Al Flight Monitor

Live dashboard that scrapes the [El Al seat availability page](https://www.elal.com/heb/seat-availability) and tracks flight availability from Tel Aviv (TLV) to all destinations. Alerts you in real time when seats open up.

![Dashboard screenshot](https://img.shields.io/badge/status-running-brightgreen)

## Features

- Real-time scraping every 10 seconds using Playwright (headless browser)
- WebSocket-powered live dashboard — no manual refresh needed
- Seat availability grid grouped by destination with 14-day date range
- Alerts (visual + sound) when new flights appear or seat counts change
- Configurable minimum seat filter
- Destination filter sidebar with search
- Change log drawer tracking all detected changes
- Dark / Light theme toggle
- Adjustable font size
- Direct booking links to El Al website
- SQLite storage with staleness-aware change detection

## Prerequisites

- **Python 3.10+**
- **pip**

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/IdoStern9/elal-flight-monitor.git
cd elal-flight-monitor

# 2. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers (one-time setup)
playwright install chromium

# 5. Run the monitor
python3 app.py
```

Open **http://localhost:8080** in your browser.

## How It Works

1. **`scraper.py`** — Uses Playwright to load the El Al seat availability page in a headless Chromium browser, scrolls to load all lazy-rendered content, and extracts flight data (flight number, time, destination, seats, booking URL).

2. **`db.py`** — Stores flight records in a local SQLite database (`flight_data.db`). Compares each scrape against stored data to detect new flights, seat changes, and removed flights. Uses a 5-minute staleness threshold to avoid false removals from incomplete scrapes.

3. **`app.py`** — FastAPI server that orchestrates scraping on a 10-second interval, serves the dashboard HTML, and broadcasts updates to all connected clients via WebSocket.

4. **`dashboard.html`** — Single-page dashboard with real-time updates, filtering, alerts, and theming. All state (theme, font size, selected dates, destinations) is persisted in localStorage.

## Configuration

| Setting | Where | Default |
|---------|-------|---------|
| Scrape interval | `app.py` line with `seconds=10` | 10s |
| Server port | `app.py` bottom `uvicorn.run(...)` | 8080 |
| Min seats filter | Dashboard top bar | 1 |
| Date range | Dashboard date bar | Next 14 days |

## Project Structure

```
elal_flight_monitor/
├── app.py              # FastAPI server + scheduler
├── scraper.py          # Playwright scraper
├── db.py               # SQLite storage + change detection
├── dashboard.html      # Frontend (HTML/CSS/JS)
├── requirements.txt    # Python dependencies
└── README.md
```

## License

MIT
