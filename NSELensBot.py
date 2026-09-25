"""
NSE Market Lens scanner -> Telegram

IMPORTANT - read this before running:
NSE Market Lens (marketlens.nseindia.com) is a JavaScript single-page app.
The raw HTML contains no stock data at all - everything renders after
JavaScript runs in a real browser and calls NSE's backend. A simple
requests + BeautifulSoup scrape (like the screener.in bot uses) CANNOT
work here; there's nothing to parse in the initial page source. This
script instead drives a real headless browser via Playwright and uses
the page's own "Export" button to get the data as a CSV, which is far
more reliable than guessing at React's auto-generated CSS class names.

This is inherently more fragile than the screener.in bot:
  - It's a full browser automation, so it's slower and heavier
  - NSE Market Lens is explicitly in BETA - the UI (including the
    Export button/flow) can change without notice
  - NSE's main site is known for aggressive bot detection; Market Lens
    may add similar protections as it matures out of beta

RUNNING WITH UV
---------------
This file declares its own Python dependencies below, but Playwright
ALSO needs a one-time browser binary install that uv can't do for you
automatically. Run these two commands once:

    uv run --with playwright playwright install chromium
    uv run NSELensBot.py

(If that install command errors, use: pip install playwright && playwright install chromium)

SETUP
-----
1. Get your NSE Market Lens screen link:
    - Go to marketlens.nseindia.com, build your filters
    - Use the share/link feature to get a URL with a "?t=..." token
      (this is the link you already have)

2. Create a .env.nse file in the same folder:
    TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
    TELEGRAM_CHAT_ID=123456789
    NSE_MARKETLENS_URL=https://marketlens.nseindia.com/screener?t=your_token_here

3. First run: use headless=False (see HEADLESS constant below) so you can
   WATCH the browser and confirm the Export click actually triggers a
   download the way this script expects. NSE Market Lens is new/beta, so
   the exact click sequence may need small adjustments - watching it once
   makes that easy to fix.

4. Once confirmed working, set HEADLESS back to True for scheduled runs.

5. Run:
    uv run NSELensBot.py
"""

# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests",
#     "python-dotenv",
#     "playwright",
# ]
# ///

import os
import csv
import html
import tempfile
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path="./.env.nse")
load_dotenv(dotenv_path="./.env.dhan")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NSE_MARKETLENS_URL = os.getenv("NSE_MARKETLENS_URL", "")

# Set to False for your first run so you can watch the browser and confirm
# the Export click works as expected. Set back to True once confirmed -
# headless is required for unattended/scheduled runs on a server.
HEADLESS = True

MAX_ROWS_IN_MESSAGE = 20


def _debug_env_status():
    checks = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "NSE_MARKETLENS_URL": NSE_MARKETLENS_URL,
    }
    print("--- .env.nse load check ---")
    for k, v in checks.items():
        print(f"  {k}: {'loaded' if v else 'MISSING'}")
    print("----------------------------")


# ---------------------------------------------------------------------------
# 1. TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram_message(text: str):
    """Send a message to your Telegram chat via the bot, splitting long messages if needed."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - printing instead:\n", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram limit is 4096 chars. Split safely by lines.
    chunks = []
    current_chunk = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if current_len + len(line) > 3800:
            chunks.append("".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line)

    if current_chunk:
        chunks.append("".join(current_chunk))

    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, data=payload, timeout=15)
            if resp.status_code != 200:
                print("Telegram send failed:", resp.text)
        except Exception as e:
            print("Telegram send exception:", e)


# ---------------------------------------------------------------------------
# 2. NSE MARKET LENS SCAN (Playwright - real browser required)
# ---------------------------------------------------------------------------
def scan_nse_marketlens(url: str, timeout_ms: int = 30000):
    """
    Loads your saved NSE Market Lens screen in a real (headless) browser,
    waits for the filtered results to load, then clicks Export and reads
    the downloaded CSV. Returns a list of dicts (one per stock row).
    """
    if not url:
        raise ValueError("NSE_MARKETLENS_URL is not set in .env.nse.")

    import sys
    from playwright.sync_api import sync_playwright

    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    launch_kwargs = {"headless": HEADLESS, "args": launch_args}
    if sys.platform == "win32":
        launch_kwargs["channel"] = "msedge"

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Give the SPA a moment to apply filters from the URL token and
            # fetch matching stocks from NSE's backend.
            page.wait_for_timeout(6000)

            with tempfile.TemporaryDirectory() as tmp_dir:
                # Click "Export" and capture the resulting file download.
                # If Market Lens shows a format sub-menu (e.g. CSV/Excel)
                # after this click, you'll need to add one more click here
                # for the specific format - run with HEADLESS=False once to
                # see exactly what happens and adjust accordingly.
                with page.expect_download(timeout=timeout_ms) as download_info:
                    page.click("text=Export")
                download = download_info.value

                csv_path = os.path.join(tmp_dir, download.suggested_filename)
                download.save_as(csv_path)

                return _parse_csv(csv_path)

        finally:
            browser.close()


def _parse_csv(path: str) -> list:
    results = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            results.append(dict(row))
    return results


def format_nse_results(results: list) -> str:
    if not results:
        return "No stocks matched your NSE Market Lens filters today."

    lines = ["<b>📊 NSE Market Lens Scan Results</b>", ""]
    shown = results[:MAX_ROWS_IN_MESSAGE]

    for i, r in enumerate(shown, 1):
        # Try common column name variants for the stock's name/symbol
        name = None
        for key in ("Company", "Symbol", "Name", "Stock"):
            if key in r and r[key]:
                name = r[key]
                break
        name = html.escape(str(name or "Unknown"))

        row_text = " | ".join(
            f"{html.escape(str(k))}: {html.escape(str(v))}"
            for k, v in r.items()
            if k not in ("Company", "Symbol", "Name", "Stock")
        )
        lines.append(f"{i}. <b>{name}</b> - {row_text}")

    if len(results) > MAX_ROWS_IN_MESSAGE:
        lines.append(f"\n...and {len(results) - MAX_ROWS_IN_MESSAGE} more (see the full CSV export).")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def run():
    _debug_env_status()

    try:
        results = scan_nse_marketlens(NSE_MARKETLENS_URL)
        send_telegram_message(format_nse_results(results))
    except Exception as e:
        print(f"[ERROR] NSE Market Lens scan failed: {e}")
        send_telegram_message(f"⚠️ NSE Market Lens scan failed: {e}")


if __name__ == "__main__":
    run()

    # --- Optional: run on a schedule instead of a cron job ---
    # from apscheduler.schedulers.blocking import BlockingScheduler
    # scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    # scheduler.add_job(run, "cron", day_of_week="mon-fri", hour=9, minute=30)
    # scheduler.start()
