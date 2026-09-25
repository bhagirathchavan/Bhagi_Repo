"""
Stock Runner: Telegram bot updates for your current holdings (via Dhan
API), with automatic PIN+TOTP token refresh and a static-token fallback.

RUNNING WITH UV (recommended - handles dependencies automatically)
--------------------------------------------------------------
This file declares its own dependencies below (the "# /// script" block).
Just run:
    uv run DhanBot.py
uv will create an isolated environment with the right packages installed
the first time you run it - no need for `pip install` or `uv add` at all.

SETUP
-----
"""

# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests",
#     "dhanhq",
#     "pyotp",
#     "python-dotenv",
#     "apscheduler",
# ]
# ///

"""
1. Enable TOTP for API access on Dhan:
    - Log in to web.dhan.co -> Profile icon -> "Access DhanHQ APIs"
    - Switch to API Key Mode, then find "Set-up TOTP" under Optional Settings
    - Scan the QR code with an authenticator app AND copy the base32 TEXT
      secret shown next to/under the QR code (NOT the 6-digit code, which
      changes every 30 seconds - your script needs the permanent secret)
    Docs: https://dhanhq.co/docs/v2/authentication/

2. Create a .env.dhan file in the SAME folder you run the script from:
    TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
    TELEGRAM_CHAT_ID=123456789
    DHAN_CLIENT_ID=1000000401
    DHAN_PIN=your_6_digit_dhan_pin
    DHAN_TOTP_SECRET=your_base32_totp_secret
    DHAN_ACCESS_TOKEN=optional_static_token   # tried first, auto-refreshed if expired/missing

   No quotes around values, no spaces inside DHAN_TOTP_SECRET.

   SECURITY WARNING: DHAN_PIN + DHAN_TOTP_SECRET together can generate a
   full trading-access token, not just a read-only one. Treat this .env
   file like your banking password - restrict its file permissions, never
   commit it to git, never store it on a shared/public machine.

3. Run:
    uv run DhanBot.py
"""

import os
import html
import requests
import pyotp
from dotenv import load_dotenv

load_dotenv(dotenv_path="./.env.dhan")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_PIN = os.getenv("DHAN_PIN")

# IMPORTANT: this stores the RAW SECRET only. Do NOT convert this to a
# 6-digit code here - a TOTP code expires in 30 seconds, so it must be
# generated fresh, at the moment it's used, inside refresh_dhan_access_token().
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")


def _debug_env_status():
    checks = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "DHAN_CLIENT_ID": DHAN_CLIENT_ID,
        "DHAN_PIN": DHAN_PIN,
        "DHAN_TOTP_SECRET": DHAN_TOTP_SECRET,
    }
    print("--- .env.dhan load check ---")
    for k, v in checks.items():
        print(f"  {k}: {'loaded' if v else 'MISSING'}")
    print("-----------------------------")


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
# 2. AUTO TOKEN REFRESH (PIN + TOTP)
# ---------------------------------------------------------------------------
def refresh_dhan_access_token():
    """
    Generates a FRESH Dhan access token every call, using PIN + a live
    TOTP code computed at this exact moment from your TOTP secret.
    """
    if not all([DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET]):
        print("Dhan auto-refresh not configured (missing client ID / PIN / TOTP secret).")
        return None

    try:
        from dhanhq import DhanLogin

        # Clean the secret defensively (spaces/dashes/lowercase break base32)
        clean_secret = DHAN_TOTP_SECRET.replace(" ", "").replace("-", "").upper()

        # Generate the 6-digit code fresh, right now - never store/reuse this
        totp_code = pyotp.TOTP(clean_secret).now()

        dhan_login = DhanLogin(DHAN_CLIENT_ID)
        result = dhan_login.generate_token(DHAN_PIN, totp_code)
        token = result.get("accessToken")
        if not token:
            print("Token refresh returned no accessToken:", result)
        return token
    except Exception as e:
        if "base32" in str(e).lower():
            print(
                "Dhan token refresh failed: DHAN_TOTP_SECRET contains invalid "
                "characters. Re-copy the base32 TEXT secret from Dhan's TOTP "
                "setup screen (not a 6-digit code), with no spaces."
            )
        else:
            print("Dhan token refresh failed:", e)
        return None


# ---------------------------------------------------------------------------
# 3. HOLDINGS (Dhan API)
# ---------------------------------------------------------------------------
def get_holdings():
    from dhanhq import DhanContext, dhanhq

    access_token = os.getenv("DHAN_ACCESS_TOKEN")

    # If static access token is provided, test it first
    if access_token and DHAN_CLIENT_ID:
        try:
            dhan_context = DhanContext(DHAN_CLIENT_ID, access_token)
            dhan = dhanhq(dhan_context)
            resp = dhan.get_holdings()
            if isinstance(resp, dict) and resp.get("status") != "failure":
                return resp.get("data", [])
            print("[INFO] Static DHAN_ACCESS_TOKEN is expired or invalid. Falling back to PIN + TOTP refresh...")
        except Exception:
            pass

    # Automatically refresh access token using PIN + TOTP
    access_token = refresh_dhan_access_token()
    if not access_token or not DHAN_CLIENT_ID:
        return None

    dhan_context = DhanContext(DHAN_CLIENT_ID, access_token)
    dhan = dhanhq(dhan_context)

    response = dhan.get_holdings()
    if isinstance(response, dict):
        if response.get("status") == "failure":
            error_msg = response.get("remarks", {}).get("error_message", response)
            print(f"[SECURITY] Dhan API error: {error_msg}")
            return None
        return response.get("data", [])
    return response


def format_holdings(holdings: list) -> str:
    if not holdings:
        return "No holdings data available (check Dhan API credentials)."

    lines = ["<b>💼 Your Current Holdings</b>", ""]
    total_invested = 0
    total_current = 0

    for h in holdings:
        qty = h.get("totalQty", h.get("quantity", 0))
        avg_price = h.get("avgCostPrice", h.get("average_price", 0))
        ltp = h.get("lastTradedPrice", h.get("last_price", 0))
        symbol = html.escape(str(h.get("tradingSymbol", h.get("tradingsymbol", "Unknown"))))

        try:
            qty_f = float(qty)
            avg_f = float(avg_price)
            ltp_f = float(ltp)
            invested = avg_f * qty_f
            current = ltp_f * qty_f
            pnl = current - invested
            pnl_pct = (pnl / invested * 100) if invested else 0
            total_invested += invested
            total_current += current
            arrow = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{arrow} <b>{symbol}</b>\n"
                f"    Qty: {qty_f:g}  |  Avg: {avg_f:.2f}  |  LTP: {ltp_f:.2f}\n"
                f"    PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)"
            )
        except (TypeError, ValueError):
            # Fall back to raw values if Dhan returns something unexpected
            lines.append(f"⚪ <b>{symbol}</b>\n    Qty: {qty} | Avg: {avg_price} | LTP: {ltp}")

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
        print(f"[SECURITY] Dhan Holdings error: {e}")
        send_telegram_message("⚠️ Holdings check failed. Please check local logs.")


if __name__ == "__main__":
    run()

    # --- Optional: run on a schedule instead of a cron job ---
    # from apscheduler.schedulers.blocking import BlockingScheduler
    # scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    # scheduler.add_job(run, "cron", day_of_week="mon-fri", hour=9, minute=20)
    # scheduler.add_job(run, "cron", day_of_week="mon-fri", hour=15, minute=15)
    # scheduler.start()
