"""
Telegram Bot Listener for Dhan & Zerodha Holdings
Supports commands:
  /holdings - Returns holdings from BOTH Zerodha and Dhan
  /zerodha  - Returns ONLY Zerodha holdings
  /dhan     - Returns ONLY Dhan holdings
  /help     - Lists available commands

Usage:
  uv run TelegramBot.py
"""

import os
import time
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv("./.env.zerodha")
load_dotenv("./.env.dhan")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env files.")

if not ALLOWED_CHAT_ID:
    raise ValueError("SECURITY ALERT: TELEGRAM_CHAT_ID is not configured. Refusing to start to protect user data.")


def send_message(chat_id: str | int, text: str):
    """Sends an HTML-formatted message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        return resp.json()
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")
        return None


def register_bot_commands():
    """Registers /holdings, /zerodha, /dhan in Telegram's autocomplete menu."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "holdings", "description": "Get holdings from both Zerodha & Dhan"},
        {"command": "zerodha", "description": "Get Zerodha holdings only"},
        {"command": "dhan", "description": "Get Dhan holdings only"},
        {"command": "help", "description": "Show available bot commands"},
    ]
    try:
        resp = requests.post(url, json={"commands": commands}, timeout=10)
        if resp.status_code == 200 and resp.json().get("ok"):
            print("Telegram bot menu commands registered successfully.")
        else:
            print("Failed to register bot commands:", resp.text)
    except Exception as e:
        print(f"Error registering bot commands: {e}")


def handle_zerodha() -> str:
    """Fetches and formats Zerodha holdings without leaking credentials in errors."""
    try:
        import ZerodhaBot

        holdings = ZerodhaBot.get_holdings()
        if not holdings:
            return "<b>💼 Zerodha Holdings</b>\nNo holdings data found."
        return ZerodhaBot.format_holdings(holdings)
    except Exception as e:
        print(f"[SECURITY] Zerodha Holdings Error: {e}")
        return "⚠️ <b>Zerodha Holdings:</b> Unable to fetch holdings. Please check your credentials."


def handle_dhan() -> str:
    """Fetches and formats Dhan holdings without leaking credentials in errors."""
    try:
        import DhanBotnotokennew

        holdings = DhanBotnotokennew.get_holdings()
        if not holdings:
            return "<b>💼 Dhan Holdings</b>\nNo holdings data available. Please verify your Dhan credentials or TOTP."
        return DhanBotnotokennew.format_holdings(holdings)
    except Exception as e:
        print(f"[SECURITY] Dhan Holdings Error: {e}")
        return "⚠️ <b>Dhan Holdings:</b> Unable to fetch holdings. Please verify your credentials."


def handle_both_holdings(chat_id: str | int):
    """Fetches and returns holdings from both Zerodha and Dhan."""
    send_message(chat_id, "⏳ <i>Fetching holdings from Zerodha and Dhan...</i>")

    zerodha_text = handle_zerodha()
    send_message(chat_id, zerodha_text)

    dhan_text = handle_dhan()
    send_message(chat_id, dhan_text)


def process_command(chat_id: int | str, command: str):
    """Routes commands to their respective handlers strictly for the authorized user."""
    # Strict security check: Silently ignore ANY arbitrary user who is not ALLOWED_CHAT_ID
    if not ALLOWED_CHAT_ID or str(chat_id) != str(ALLOWED_CHAT_ID):
        print(f"[SECURITY] Silently blocked unauthorized access attempt from Chat ID: {chat_id}")
        return

    cmd = command.strip().lower().split("@")[0]  # strip bot username if present, e.g. /holdings@mybot

    if cmd in ("/holdings", "holdings"):
        handle_both_holdings(chat_id)

    elif cmd in ("/zerodha", "zerodha"):
        send_message(chat_id, "⏳ <i>Fetching Zerodha holdings...</i>")
        send_message(chat_id, handle_zerodha())

    elif cmd in ("/dhan", "dhan"):
        send_message(chat_id, "⏳ <i>Fetching Dhan holdings...</i>")
        send_message(chat_id, handle_dhan())

    elif cmd in ("/start", "/help", "help"):
        help_msg = (
            "<b>🤖 Trading Bot Command Menu</b>\n\n"
            "Use the commands below to check your portfolio:\n\n"
            "🔹 <b>/holdings</b> - Returns holdings from <b>BOTH</b> Zerodha & Dhan\n"
            "🔹 <b>/zerodha</b>  - Returns <b>Zerodha</b> holdings only\n"
            "🔹 <b>/dhan</b>     - Returns <b>Dhan</b> holdings only\n"
            "🔹 <b>/help</b>     - Show this help menu"
        )
        send_message(chat_id, help_msg)

    else:
        send_message(chat_id, f"❓ Unknown command: <code>{command}</code>\nSend /help to see available commands.")


def run_listener():
    """Continuously polls Telegram for incoming commands."""
    print("=" * 50)
    print("🤖 Telegram Bot Listener Started")
    print(f"Allowed Chat ID: {ALLOWED_CHAT_ID}")
    print("Listening for: /holdings, /zerodha, /dhan")
    print("Press Ctrl+C to stop.")
    print("=" * 50)

    register_bot_commands()

    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=35)
            if resp.status_code != 200:
                print(f"getUpdates returned status {resp.status_code}: {resp.text}")
                time.sleep(3)
                continue

            data = resp.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message:
                    continue

                text = message.get("text", "").strip()
                chat_id = message.get("chat", {}).get("id")

                if text.startswith("/"):
                    print(f"Received command '{text}' from Chat ID {chat_id}")
                    process_command(chat_id, text)

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError:
            print("Network connection error. Retrying in 5 seconds...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopping Telegram Bot Listener...")
            break
        except Exception as e:
            print(f"Unexpected error in polling loop: {e}")
            time.sleep(3)


if __name__ == "__main__":
    run_listener()

