"""
Stock Runner: Telegram bot updates for
  1) Screener.in scan (a screen you saved on screener.in)
  2) Your current holdings (via Dhan API), with automatic PIN+TOTP token refresh

Both are stored in Oracle Cloud (OCI) Object Storage with automatic
retention cleanup, and run on a schedule (Mon-Fri):
  - Holdings:  11:00 AM and 4:00 PM  -> kept for 3 days, then auto-deleted
  - Screener:  9:30 AM only          -> kept for 2 days, then auto-deleted

RUNNING WITH UV (recommended - handles dependencies automatically)
--------------------------------------------------------------
This file declares its own dependencies below (the "# /// script" block).
Just run:
    uv run DhanBotnotokennew.py
uv will create an isolated environment with the right packages installed
the first time you run it - no need for `pip install` or `uv add` at all.

Run modes:
    uv run DhanBotnotokennew.py          -> starts the scheduler, runs forever
    uv run DhanBotnotokennew.py once     -> runs both jobs once immediately, then exits (for testing)

SETUP
-----
"""

# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests",
#     "beautifulsoup4",
#     "dhanhq",
#     "pyotp",
#     "python-dotenv",
#     "apscheduler",
#     "oci",
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

2. Create your screen on screener.in (one-time):
    - Log in to screener.in in your browser
    - Click "Create New Screen" and paste your query:
        Promoter holding > 70 AND FII holding > 3.5 AND Market Capitalization < 10000
    - Click "Save Query", give it a name
    - Copy the resulting URL, e.g. https://www.screener.in/screens/123456/my-screen/
      (free-text queries need a login and 404 for anonymous requests -
      a SAVED screen's URL is public and needs no login to scrape)

3. Set up Oracle Cloud (OCI) Object Storage (one-time):
    - In OCI Console: create a bucket (Storage -> Buckets -> Create Bucket)
      e.g. name it "stock-runner-data". Note your bucket name.
    - Generate an API signing key: Profile icon -> User Settings ->
      API Keys -> Add API Key -> follow the download/paste steps.
      This produces a config block AND a private key .pem file.
    - Save the private key (e.g. as ~/.oci/oci_api_key.pem) and put the
      config block it gives you into ~/.oci/config, e.g.:
        [DEFAULT]
        user=ocid1.user.oc1..xxxx
        fingerprint=xx:xx:xx:...
        tenancy=ocid1.tenancy.oc1..xxxx
        region=ap-mumbai-1
        key_file=~/.oci/oci_api_key.pem
      Docs: https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm
    - If this script runs ON an OCI compute VM, you may instead use
      "instance principal" auth (no config file needed) - ask me to wire
      that in separately if that's your setup.

4. Create a .env file in the SAME folder you run the script from:
    TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
    TELEGRAM_CHAT_ID=123456789
    DHAN_CLIENT_ID=1000000401
    DHAN_PIN=your_6_digit_dhan_pin
    DHAN_TOTP_SECRET=your_base32_totp_secret
    SCREENER_URL=https://www.screener.in/screens/123456/my-screen/
    OCI_BUCKET_NAME=stock-runner-data

   No quotes around values, no spaces inside DHAN_TOTP_SECRET.
   (OCI credentials themselves live in ~/.oci/config, not in .env)

   SECURITY WARNING: DHAN_PIN + DHAN_TOTP_SECRET together can generate a
   full trading-access token, not just a read-only one. Treat this .env
   file like your banking password - restrict its file permissions, never
   commit it to git, never store it on a shared/public machine.

5. Test once immediately:
    uv run DhanBotnotokennew.py once

6. Run continuously on schedule (leave this running - use tmux/screen/a
   systemd service on your Oracle Cloud VM so it survives SSH disconnects):
    uv run DhanBotnotokennew.py
"""

import os
import json
import html
import requests
import pyotp
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_PIN = os.getenv("DHAN_PIN")

# IMPORTANT: this stores the RAW SECRET only. Do NOT convert this to a
# 6-digit code here - a TOTP code expires in 30 seconds, so it must be
# generated fresh, at the moment it's used, inside refresh_dhan_access_token().
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")

# Public URL of a screen YOU created and saved on screener.in.
SCREENER_URL = os.getenv("SCREENER_URL", "")

# Oracle Cloud Object Storage bucket used for retention-limited storage
OCI_BUCKET_NAME = os.getenv("OCI_BUCKET_NAME", "")

HOLDINGS_RETENTION_DAYS = 3
SCREENER_RETENTION_DAYS = 2


def _debug_env_status():
    checks = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "DHAN_CLIENT_ID": DHAN_CLIENT_ID,
        "DHAN_PIN": DHAN_PIN,
        "DHAN_TOTP_SECRET": DHAN_TOTP_SECRET,
        "SCREENER_URL": SCREENER_URL,
        "OCI_BUCKET_NAME": OCI_BUCKET_NAME,
    }
    print("--- .env load check ---")
    for k, v in checks.items():
        print(f"  {k}: {'loaded' if v else 'MISSING'}")
    print("-----------------------")


# ---------------------------------------------------------------------------
# ORACLE CLOUD (OCI) OBJECT STORAGE - store + retention cleanup
# ---------------------------------------------------------------------------
_oci_client = None
_oci_namespace = None


def _get_oci_client():
    """
    Lazily creates and caches the OCI Object Storage client using the
    standard ~/.oci/config file. Returns (client, namespace) or (None, None)
    if OCI isn't configured/reachable - callers should skip storage in that
    case rather than crash the whole run.
    """
    global _oci_client, _oci_namespace

    if not OCI_BUCKET_NAME:
        print("OCI storage skipped: OCI_BUCKET_NAME not set in .env.")
        return None, None

    if _oci_client is not None:
        return _oci_client, _oci_namespace

    try:
        import oci

        config = oci.config.from_file()  # reads ~/.oci/config, [DEFAULT] profile
        client = oci.object_storage.ObjectStorageClient(config)
        namespace = client.get_namespace().data
        _oci_client = client
        _oci_namespace = namespace
        return client, namespace
    except Exception as e:
        print(f"OCI storage skipped: could not connect ({e}). "
              f"Check ~/.oci/config is set up correctly.")
        return None, None


def store_to_oracle(prefix: str, data) -> None:
    """
    Saves `data` (any JSON-serializable object) as a timestamped object in
    the OCI bucket under the given prefix, e.g. prefix="holdings" ->
    object name "holdings/20260914T110000Z.json".
    """
    client, namespace = _get_oci_client()
    if not client:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    object_name = f"{prefix}/{timestamp}.json"
    body = json.dumps(data, default=str, ensure_ascii=False)

    try:
        client.put_object(namespace, OCI_BUCKET_NAME, object_name, body)
        print(f"Stored to Oracle Cloud: {object_name}")
    except Exception as e:
        print(f"Failed to store {object_name} to Oracle Cloud: {e}")


def cleanup_old_objects(prefix: str, max_age_days: int) -> None:
    """
    Deletes objects under `prefix` in the OCI bucket that are older than
    max_age_days. Called right after storing new data for that prefix, so
    the bucket never accumulates more than the retention window.
    """
    client, namespace = _get_oci_client()
    if not client:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    try:
        response = client.list_objects(
            namespace, OCI_BUCKET_NAME, prefix=f"{prefix}/", fields="timeCreated"
        )
        for obj in response.data.objects:
            if obj.time_created and obj.time_created < cutoff:
                client.delete_object(namespace, OCI_BUCKET_NAME, obj.name)
                print(f"Deleted expired object (> {max_age_days}d old): {obj.name}")
    except Exception as e:
        print(f"Cleanup failed for prefix '{prefix}/': {e}")


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
# 2. SCREENER.IN SCAN
# ---------------------------------------------------------------------------
def scan_screener(screen_url: str, max_results: int = 20):
    """
    Scrapes a screen YOU already created and saved on screener.in.

    Robust approach: rather than relying on an exact table class name
    (which may not match, and screener.in also repeats header rows
    mid-table on long screens), this finds the results table structurally:
    - grabs column labels from the first row of <th> cells
    - treats any <tr> containing a company link (<a>) as a real data row,
      which naturally skips repeated header rows that lack a link
    """
    if not screen_url:
        raise ValueError(
            "SCREENER_URL is not set. Create and save your screen on "
            "screener.in first, then put its URL in .env."
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }

    resp = requests.get(screen_url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")

    if table is None:
        raise ValueError(
            "No results table found on the page. Double-check SCREENER_URL "
            "points to a real saved screen (paste it in a browser to confirm "
            "it shows a stock list, not a login page or an empty screen)."
        )

    header_cells = table.find("tr")
    column_labels = (
        [th.get_text(strip=True) for th in header_cells.find_all("th")]
        if header_cells else []
    )

    results = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue  # header-only row, skip
        if not row.find("a"):
            continue  # repeated header row lacking a company link, skip
        values = [c.get_text(strip=True) for c in cells]
        if column_labels and len(column_labels) == len(values):
            record = dict(zip(column_labels, values))
        else:
            record = {f"Col{i+1}": v for i, v in enumerate(values)}
        results.append(record)
        if len(results) >= max_results:
            break

    return results


def format_screener_results(results: list) -> str:
    if not results:
        return "No stocks matched the screener query today."

    lines = ["<b>📊 Screener.in Scan Results</b>", ""]
    for i, r in enumerate(results, 1):
        # screener.in labels the company column "Company" (or "Name" on
        # some screen layouts) - check both
        name = html.escape(str(r.get("Company", r.get("Name", "Unknown"))))
        row_text = " | ".join(
            f"{html.escape(str(k))}: {html.escape(str(v))}"
            for k, v in r.items() if k not in ("Company", "Name", "S.No.")
        )
        lines.append(f"{i}. <b>{name}</b> - {row_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. AUTO TOKEN REFRESH (PIN + TOTP)
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
# 4. HOLDINGS (Dhan API)
# ---------------------------------------------------------------------------
def get_holdings():
    from dhanhq import DhanContext, dhanhq

    access_token = refresh_dhan_access_token()
    if not access_token or not DHAN_CLIENT_ID:
        return None

    dhan_context = DhanContext(DHAN_CLIENT_ID, access_token)
    dhan = dhanhq(dhan_context)

    response = dhan.get_holdings()
    if isinstance(response, dict):
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
# SCHEDULED JOBS
# ---------------------------------------------------------------------------
def run_holdings():
    """
    Job 1: fetch holdings, send to Telegram, store in Oracle Cloud, and
    delete any stored holdings snapshots older than 3 days.
    Scheduled for 11:00 AM and 4:00 PM, Mon-Fri.
    """
    _debug_env_status()
    try:
        holdings = get_holdings()
        send_telegram_message(format_holdings(holdings))
        if holdings:
            store_to_oracle("holdings", holdings)
            cleanup_old_objects("holdings", HOLDINGS_RETENTION_DAYS)
    except Exception as e:
        send_telegram_message(f"⚠️ Holdings check failed: {e}")


def run_screener():
    """
    Job 2: run the screener.in scan, send to Telegram, store in Oracle
    Cloud, and delete any stored screener snapshots older than 2 days.
    Scheduled for 9:30 AM only, Mon-Fri.
    """
    _debug_env_status()
    try:
        results = scan_screener(SCREENER_URL)
        send_telegram_message(format_screener_results(results))
        store_to_oracle("screener", results)
        cleanup_old_objects("screener", SCREENER_RETENTION_DAYS)
    except Exception as e:
        send_telegram_message(f"⚠️ Screener scan failed: {e}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "once":
        # Manual test mode: run both jobs immediately, then exit
        run_holdings()
        run_screener()
    else:
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = BlockingScheduler(timezone="Asia/Kolkata")

        # Holdings: Mon-Fri at 11:00 AM and 4:00 PM
        scheduler.add_job(run_holdings, "cron", day_of_week="mon-fri", hour=11, minute=0, id="holdings_11am")
        scheduler.add_job(run_holdings, "cron", day_of_week="mon-fri", hour=16, minute=0, id="holdings_4pm")

        # Screener: Mon-Fri at 9:30 AM only
        scheduler.add_job(run_screener, "cron", day_of_week="mon-fri", hour=9, minute=30, id="screener_930am")

        print("Scheduler started (Asia/Kolkata). Jobs:")
        print("  Holdings -> Mon-Fri 11:00 AM and 4:00 PM (3-day retention)")
        print("  Screener -> Mon-Fri 9:30 AM (2-day retention)")
        print("Press Ctrl+C to stop.")

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            print("Scheduler stopped.")
