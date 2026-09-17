"""
Stock Runner: Telegram bot updates for your current holdings (via Zerodha
Kite Connect). No auto-refresh, no screener - holdings only, using a
manually-generated daily access token.

RUNNING WITH UV (recommended - handles dependencies automatically)
--------------------------------------------------------------
This file declares its own dependencies below (the "# /// script" block).
Just run:
    uv run ZerodhaHoldingsBot.py
uv will create an isolated environment with the right packages installed
the first time you run it - no need for `pip install` or `uv add` at all.

SETUP
-----
1. Get your Kite Connect API key from developers.kite.trade (create an app
   if you haven't already).

2. Generate today's access token manually:
    - Visit: https://kite.trade/connect/login?v=3&api_key=YOUR_API_KEY
    - Log in, approve the app - you'll be redirected to a URL containing
      request_token=XXXX
    - Exchange it for an access token (one-time, e.g. in a python shell):
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key="YOUR_API_KEY")
        data = kite.generate_session("REQUEST_TOKEN_FROM_URL", api_secret="YOUR_API_SECRET")
        print(data["access_token"])
    - Kite access tokens expire daily (around 6 AM) - you'll need to repeat
      this each morning and update .env.zerodha. (If you want this fully
      automated via TOTP instead, say so and I'll wire that version back in.)

3. Create a .env.zerodha file in the SAME folder you run this script from:
    TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
    TELEGRAM_CHAT_ID=123456789
    KITE_API_KEY=your_kite_api_key
    KITE_ACCESS_TOKEN=todays_generated_access_token

   No quotes around values.

4. Run manually to test:
    uv run ZerodhaHoldingsBot.py

5. Once it works, schedule it with cron / Windows Task Scheduler, or ask
   me to wire in APScheduler cron jobs the same way as the Dhan bot.
"""

# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests",
#     "kiteconnect",
#     "python-dotenv",
# ]
# ///

import os
import html
import requests
from dotenv import load_dotenv

# Load this bot's own env file explicitly, so it never collides with
# another bot's .env file sitting in the same folder (e.g. Dhan's .env.dhan).
load_dotenv(dotenv_path="./.env.zerodha")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN")


def _debug_env_status():
    checks = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "KITE_API_KEY": KITE_API_KEY,
        "KITE_ACCESS_TOKEN": KITE_ACCESS_TOKEN,
    }
    print("--- .env.zerodha load check ---")
    for k, v in checks.items():
        print(f"  {k}: {'loaded' if v else 'MISSING'}")
    print("--------------------------------")


# ---------------------------------------------------------------------------
# 1. TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram_message(text: str):
    """Send a message to your Telegram chat via the bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - printing instead:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print("Telegram send failed:", resp.text)


# ---------------------------------------------------------------------------
# 2. HOLDINGS (Zerodha Kite Connect)
# ---------------------------------------------------------------------------
def get_holdings():
    """
    Fetches your current holdings from Zerodha via Kite Connect.
    Requires KITE_API_KEY and a valid, same-day KITE_ACCESS_TOKEN.
    """
    from kiteconnect import KiteConnect

    if not KITE_API_KEY or not KITE_ACCESS_TOKEN:
        print("Kite not configured (missing API key or access token).")
        return None

    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(KITE_ACCESS_TOKEN)
    return kite.holdings()  # returns a list of dicts


def format_holdings(holdings: list) -> str:
    if not holdings:
        return "No holdings data available (check Kite API credentials)."

    lines = ["<b>💼 Your Current Holdings</b>", ""]
    total_invested = 0
    total_current = 0

    for h in holdings:
        symbol = html.escape(str(h.get("tradingsymbol", "Unknown")))
        try:
            qty = float(h.get("quantity", 0))
            avg = float(h.get("average_price", 0))
            ltp = float(h.get("last_price", 0))
            invested = avg * qty
            current = ltp * qty
            pnl = current - invested
            pnl_pct = (pnl / invested * 100) if invested else 0
            total_invested += invested
            total_current += current
            arrow = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{arrow} <b>{symbol}</b>\n"
                f"    Qty: {qty:g}  |  Avg: {avg:.2f}  |  LTP: {ltp:.2f}\n"
                f"    PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)"
            )
        except (TypeError, ValueError):
            lines.append(f"⚪ <b>{symbol}</b>\n    (unexpected data format)")

    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0
    lines.append("")
    lines.append(f"<b>Invested:</b> {total_invested:,.2f}")
    lines.append(f"<b>Current:</b> {total_current:,.2f}")
    lines.append(f"<b>Total PnL:</b> {total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def run():
    _debug_env_status()

    try:
        holdings = get_holdings()
        send_telegram_message(format_holdings(holdings))
    except Exception as e:
        send_telegram_message(f"⚠️ Holdings check failed: {e}")


if __name__ == "__main__":
    run()

    # --- Optional: run on a schedule instead of a cron job ---
    # from apscheduler.schedulers.blocking import BlockingScheduler
    # scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    # scheduler.add_job(run, "cron", day_of_week="mon-fri", hour=11, minute=0)
    # scheduler.add_job(run, "cron", day_of_week="mon-fri", hour=16, minute=0)
    # scheduler.start()