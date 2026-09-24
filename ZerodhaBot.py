"""
Stock Runner: Telegram bot updates for your current holdings (via Zerodha
Kite Connect) with 100% AUTOMATIC daily login using User ID, Password, and TOTP.

RUNNING WITH UV (recommended - handles dependencies automatically)
--------------------------------------------------------------
Just run:
    uv run ZerodhaBot.py
"""

# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests",
#     "kiteconnect",
#     "python-dotenv",
#     "pyotp",
# ]
# ///

import os
import html
import requests
from dotenv import load_dotenv

# Load this bot's own env file explicitly
load_dotenv(dotenv_path="./.env.zerodha")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET") or os.getenv("KIT_API_SECRET")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET") or os.getenv("KITE_ACCESS_TOKEN")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN")


def _debug_env_status():
    checks = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "KITE_USER_ID": KITE_USER_ID,
        "KITE_PASSWORD": bool(KITE_PASSWORD),
        "KITE_API_KEY": KITE_API_KEY,
        "KITE_API_SECRET": bool(KITE_API_SECRET),
        "KITE_TOTP_SECRET": bool(KITE_TOTP_SECRET),
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
# 2. AUTOMATIC LOGIN & TOKEN GENERATION
# ---------------------------------------------------------------------------
def generate_kite_access_token() -> str:
    """
    Logs into Zerodha Kite automatically using User ID, Password, and TOTP,
    authorizes Kite Connect, and exchanges request_token for a fresh access_token.
    """
    if not all([KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET, KITE_API_KEY, KITE_API_SECRET]):
        raise ValueError(
            "Missing credentials for auto-login. Please make sure KITE_USER_ID, "
            "KITE_PASSWORD, KITE_TOTP_SECRET, KITE_API_KEY, and KITE_API_SECRET are set."
        )

    import pyotp
    from urllib.parse import parse_qs, urlparse
    from kiteconnect import KiteConnect

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    })

    print("Logging in to Zerodha Kite...")
    login_resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": KITE_USER_ID, "password": KITE_PASSWORD},
        timeout=15,
    )
    try:
        login_data = login_resp.json()
    except Exception:
        raise Exception(f"Zerodha Login returned HTTP {login_resp.status_code}: {login_resp.text[:200]}")

    if login_data.get("status") != "success":
        raise Exception(f"Zerodha Login Failed: {login_data.get('message', login_resp.text[:200])}")

    request_id = login_data["data"]["request_id"]

    # Generate 6-digit TOTP
    clean_secret = KITE_TOTP_SECRET.replace(" ", "").strip()
    totp = pyotp.TOTP(clean_secret)
    twofa_code = totp.now()

    print("Authenticating 2FA TOTP...")
    twofa_resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": KITE_USER_ID,
            "request_id": request_id,
            "twofa_value": twofa_code,
            "twofa_type": "totp",
            "skip_session": ""
        },
        timeout=15,
    )
    try:
        twofa_data = twofa_resp.json()
    except Exception:
        raise Exception(f"Zerodha 2FA returned HTTP {twofa_resp.status_code}: {twofa_resp.text[:200]}")

    if twofa_data.get("status") != "success":
        raise Exception(f"Zerodha 2FA Failed: {twofa_data.get('message', twofa_resp.text[:200])}")

    print("Obtaining Kite Connect request_token...")
    auth_url = f"https://kite.zerodha.com/connect/login?api_key={KITE_API_KEY}&v=3"
    auth_resp = session.get(auth_url, allow_redirects=True, timeout=20)

    request_token = None
    parsed = urlparse(auth_resp.url)
    qs = parse_qs(parsed.query)
    if "request_token" in qs:
        request_token = qs["request_token"][0]
    else:
        for r in auth_resp.history:
            loc = r.headers.get("Location", "")
            loc_qs = parse_qs(urlparse(loc).query)
            if "request_token" in loc_qs:
                request_token = loc_qs["request_token"][0]
                break

    if not request_token:
        raise Exception(f"Could not extract request_token from redirect: {auth_resp.url}")

    print("Generating today's fresh Kite access token...")
    kite = KiteConnect(api_key=KITE_API_KEY)
    session_data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    return session_data["access_token"]


# ---------------------------------------------------------------------------
# 3. HOLDINGS (Zerodha Kite Connect)
# ---------------------------------------------------------------------------
def get_holdings():
    """
    Fetches your current holdings from Zerodha. Automatically refreshes
    the access token if missing or expired.
    """
    from kiteconnect import KiteConnect

    token = None
    # If a valid session token exists and is not a 32-char TOTP secret, try it
    if KITE_ACCESS_TOKEN and len(KITE_ACCESS_TOKEN) == 32 and not KITE_ACCESS_TOKEN.isupper():
        try:
            kite = KiteConnect(api_key=KITE_API_KEY)
            kite.set_access_token(KITE_ACCESS_TOKEN)
            return kite.holdings()
        except Exception:
            print("Existing access token expired. Performing automated login...")

    # Generate fresh access token automatically
    token = generate_kite_access_token()
    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(token)
    return kite.holdings()


def format_holdings(holdings: list) -> str:
    if not holdings:
        return "No holdings data available."

    lines = ["<b>💼 Your Current Holdings (Zerodha)</b>", ""]
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

    # Explicit check for missing credentials
    missing = [
        var for var, val in {
            "KITE_USER_ID": KITE_USER_ID,
            "KITE_PASSWORD": KITE_PASSWORD,
            "KITE_API_KEY": KITE_API_KEY,
            "KITE_API_SECRET": KITE_API_SECRET,
            "KITE_TOTP_SECRET": KITE_TOTP_SECRET,
        }.items() if not val
    ]
    if missing:
        msg = f"⚠️ Zerodha error: Missing secret(s) in GitHub Actions: {', '.join(missing)}"
        print(f"[ERROR] {msg}")
        send_telegram_message(msg)
        return

    try:
        holdings = get_holdings()
        send_telegram_message(format_holdings(holdings))
    except Exception as e:
        print(f"[SECURITY] Zerodha Holdings check failed: {e}")
        send_telegram_message(f"⚠️ Zerodha Holdings check failed: {e}")


if __name__ == "__main__":
    run()