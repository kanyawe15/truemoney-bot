#!/usr/bin/env python3
"""
TrueMoney Telegram Bot - Render Web Service Version
Ultra-stable with 10-second monitoring interval + HTTP health check
Features:
- Fast monitoring every 10 seconds
- Environment variable configuration (secure)
- HTTP health check endpoint for Render Web Service (Free Tier)
- Improved error handling and recovery
- Reliable notification delivery
- Both incoming and outgoing transfer detection
- Thai language notifications
"""

import logging
import requests
import json
import os
import time
import asyncio
import threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

# Configuration from environment variables
TRUEMONEY_API_URL = os.environ.get("TRUEMONEY_API_URL", "https://apis.truemoneyservices.com/account/v1/balance")
TRUEMONEY_TOKEN = os.environ.get("TRUEMONEY_TOKEN", "4a5a8b0ff44d2bb689b11c33ac336c99")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8616602042:AAE-IfylFHobXmje1063wWForTPvrx6m7Mo")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("CHAT_ID", "-1003781331341")))
PORT = int(os.environ.get("PORT", "10000"))

# Monitoring configuration
MONITORING_INTERVAL = int(os.environ.get("MONITORING_INTERVAL", "10"))
BALANCE_HISTORY_FILE = os.environ.get("BALANCE_HISTORY_FILE", "/tmp/balance_history.json")

# Bot start time for uptime tracking
BOT_START_TIME = datetime.now()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ============================================================
# HTTP Health Check Server (for Render Web Service Free Tier)
# ============================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for Render health checks"""

    def do_GET(self):
        if self.path == '/' or self.path == '/health' or self.path == '/healthz':
            uptime = datetime.now() - BOT_START_TIME
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)

            health_data = {
                "status": "ok",
                "service": "TrueMoney Telegram Bot",
                "uptime": f"{hours}h {minutes}m {seconds}s",
                "monitoring_interval": f"{MONITORING_INTERVAL}s",
                "timestamp": datetime.now().isoformat()
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(health_data).encode())
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found')

    def log_message(self, format, *args):
        """Suppress default HTTP request logging to reduce noise"""
        pass


def start_health_server():
    """Start HTTP health check server in a separate thread"""
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"Health check server started on port {PORT}")
    server.serve_forever()


# ============================================================
# Balance Tracker
# ============================================================
class BalanceTracker:
    """Track balance history and detect changes"""

    def __init__(self, history_file=BALANCE_HISTORY_FILE):
        self.history_file = history_file
        self.current_balance = None
        self.previous_balance = None
        self.load_history()

    def load_history(self):
        try:
            if Path(self.history_file).exists():
                with open(self.history_file, 'r') as f:
                    data = json.load(f)
                    self.current_balance = data.get('current_balance')
                    self.previous_balance = data.get('previous_balance')
                    logger.info(f"Loaded balance history: Current={self.current_balance}, Previous={self.previous_balance}")
            else:
                logger.info("No balance history found, starting fresh")
        except Exception as e:
            logger.error(f"Error loading balance history: {str(e)}")

    def save_history(self):
        try:
            data = {
                'current_balance': self.current_balance,
                'previous_balance': self.previous_balance,
                'last_updated': datetime.now().isoformat()
            }
            with open(self.history_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving balance history: {str(e)}")

    def update_balance(self, new_balance):
        self.previous_balance = self.current_balance
        self.current_balance = new_balance
        self.save_history()

    def get_balance_change(self):
        if self.previous_balance is None or self.current_balance is None:
            return None
        return self.current_balance - self.previous_balance

    def has_balance_changed(self):
        change = self.get_balance_change()
        return change is not None and change != 0

    def is_money_received(self):
        change = self.get_balance_change()
        return change is not None and change > 0

    def is_money_sent(self):
        change = self.get_balance_change()
        return change is not None and change < 0


# ============================================================
# TrueMoney API
# ============================================================
def get_truemoney_balance():
    max_retries = 3
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            headers = {
                "Authorization": f"Bearer {TRUEMONEY_TOKEN}",
                "Content-Type": "application/json"
            }
            response = requests.get(TRUEMONEY_API_URL, headers=headers, timeout=5)

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok":
                    return {"success": True, "data": data.get("data", {})}
                else:
                    return {"success": False, "error": data.get("err", "Unknown error from API")}
            elif response.status_code == 401:
                return {"success": False, "error": "Unauthorized (401) - Token ไม่ถูกต้อง"}
            elif response.status_code == 403:
                return {"success": False, "error": "Forbidden (403) - ไม่มีสิทธิ์เข้าถึง"}
            elif response.status_code == 429:
                return {"success": False, "error": "Too Many Requests (429) - เกินจำนวนที่กำหนด"}
            elif response.status_code == 500:
                return {"success": False, "error": "Server Error (500) - เซิร์ฟเวอร์ TrueMoney มีปัญหา"}
            else:
                return {"success": False, "error": f"HTTP Error {response.status_code}"}

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return {"success": False, "error": "Request timeout"}
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return {"success": False, "error": "Connection error"}
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request error attempt {attempt + 1}/{max_retries}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return {"success": False, "error": f"Request error - {str(e)}"}
        except ValueError:
            return {"success": False, "error": "Invalid response format"}

    return {"success": False, "error": "Failed after retries"}


# ============================================================
# Message Formatters
# ============================================================
def format_balance_message(balance_data):
    balance_satang = balance_data.get("balance", "0")
    mobile_no = balance_data.get("mobile_no", "N/A")
    updated_at = balance_data.get("updated_at", "N/A")
    try:
        balance_baht = float(balance_satang) / 100
        balance_str = f"฿{balance_baht:,.2f}"
    except (ValueError, TypeError):
        balance_str = "N/A"

    return (
        "💰 <b>ยอดเงิน TrueMoney</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>ยอดเงิน:</b> {balance_str}\n"
        f"📱 <b>เบอร์โทร:</b> {mobile_no}\n"
        f"🕐 <b>อัพเดท:</b> {updated_at}\n"
    )


def format_money_received_notification(balance_data, transfer_amount_baht):
    balance_satang = balance_data.get("balance", "0")
    mobile_no = balance_data.get("mobile_no", "N/A")
    updated_at = balance_data.get("updated_at", "N/A")
    try:
        balance_baht = float(balance_satang) / 100
        balance_str = f"฿{balance_baht:,.2f}"
    except (ValueError, TypeError):
        balance_str = "N/A"
    transfer_str = f"฿{transfer_amount_baht:,.2f}"

    return (
        "🎉 <b>มีเงินเข้า!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💸 <b>จำนวนเงินที่เข้า:</b> {transfer_str}\n"
        f"💰 <b>ยอดคงเหลือใหม่:</b> {balance_str}\n"
        f"📱 <b>เบอร์โทร:</b> {mobile_no}\n"
        f"🕐 <b>อัพเดท:</b> {updated_at}\n"
        f"⏰ <b>เวลาแจ้งเตือน:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )


def format_money_sent_notification(balance_data, transfer_amount_baht):
    balance_satang = balance_data.get("balance", "0")
    mobile_no = balance_data.get("mobile_no", "N/A")
    updated_at = balance_data.get("updated_at", "N/A")
    try:
        balance_baht = float(balance_satang) / 100
        balance_str = f"฿{balance_baht:,.2f}"
    except (ValueError, TypeError):
        balance_str = "N/A"
    transfer_str = f"฿{transfer_amount_baht:,.2f}"

    return (
        "💸 <b>มีเงินออก!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 <b>จำนวนเงินที่ออก:</b> {transfer_str}\n"
        f"💰 <b>ยอดคงเหลือ:</b> {balance_str}\n"
        f"📱 <b>เบอร์โทร:</b> {mobile_no}\n"
        f"🕐 <b>อัพเดท:</b> {updated_at}\n"
        f"⏰ <b>เวลาแจ้งเตือน:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )


# ============================================================
# Telegram Command Handlers
# ============================================================
async def check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.chat.send_action("typing")
        result = get_truemoney_balance()
        if result["success"]:
            message = format_balance_message(result["data"])
        else:
            message = f"❌ {result['error']}"
        await update.message.reply_text(message, parse_mode="HTML")
        logger.info(f"Balance check by user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error in check_balance: {str(e)}")
        await update.message.reply_text(f"❌ Error: {str(e)}", parse_mode="HTML")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_message = (
        "👋 <b>ยินดีต้อนรับสู่ TrueMoney Balance Bot!</b>\n\n"
        "📋 <b>คำสั่งที่ใช้ได้:</b>\n"
        "/balance - เช็คยอดเงิน\n"
        "/check - เช็คยอดเงิน (ชื่ออื่น)\n"
        "/status - แสดงสถานะการ Monitoring\n"
        "/start - แสดงข้อความนี้\n\n"
        "🤖 <b>ฟีเจอร์:</b>\n"
        f"✅ ตรวจสอบยอดเงินอัตโนมัติทุก {MONITORING_INTERVAL} วินาที\n"
        "✅ แจ้งเตือนอัตโนมัติเมื่อมีเงินเข้า 🎉\n"
        "✅ แจ้งเตือนอัตโนมัติเมื่อมีเงินออก 💸\n"
    )
    await update.message.reply_text(welcome_message, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tracker = context.bot_data.get('tracker')
    uptime = datetime.now() - BOT_START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    if tracker is None:
        status_msg = "❌ ระบบ Monitoring ยังไม่เริ่มต้น"
    else:
        current = tracker.current_balance
        previous = tracker.previous_balance
        current_str = f"฿{current/100:,.2f}" if current is not None else "N/A"
        previous_str = f"฿{previous/100:,.2f}" if previous is not None else "N/A"

        status_msg = (
            "📊 <b>สถานะการ Monitoring</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>สถานะ:</b> ทำงาน\n"
            f"⏱ <b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
            f"💰 <b>ยอดเงินปัจจุบัน:</b> {current_str}\n"
            f"📈 <b>ยอดเงินครั้งก่อน:</b> {previous_str}\n"
            f"🔄 <b>ช่วงเวลาตรวจสอบ:</b> {MONITORING_INTERVAL} วินาที\n"
            f"⏰ <b>ตรวจสอบล่าสุด:</b> {datetime.now().strftime('%H:%M:%S')}\n"
            f"🌐 <b>Health Check:</b> Port {PORT}\n"
        )
    await update.message.reply_text(status_msg, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_message = (
        "🆘 <b>ความช่วยเหลือ - TrueMoney Balance Bot</b>\n\n"
        "📋 <b>คำสั่ง:</b>\n"
        "• /balance - เช็คยอดเงิน TrueMoney\n"
        "• /check - ชื่ออื่นของ /balance\n"
        "• /status - แสดงสถานะการ Monitoring\n"
        "• /start - แสดงข้อความต้อนรับ\n"
        "• /help - แสดงข้อความนี้\n\n"
        "🤖 <b>ฟีเจอร์อัตโนมัติ:</b>\n"
        f"• ตรวจสอบยอดเงินทุก {MONITORING_INTERVAL} วินาที\n"
        "• แจ้งเตือนอัตโนมัติเมื่อมีเงินเข้า 🎉\n"
        "• แจ้งเตือนอัตโนมัติเมื่อมีเงินออก 💸\n"
    )
    await update.message.reply_text(help_message, parse_mode="HTML")


# ============================================================
# Balance Monitor (Job Queue)
# ============================================================
async def monitor_balance(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if 'tracker' not in context.bot_data:
            context.bot_data['tracker'] = BalanceTracker()
        tracker = context.bot_data['tracker']

        result = get_truemoney_balance()
        if not result["success"]:
            logger.warning(f"Failed to fetch balance: {result['error']}")
            return

        balance_data = result["data"]
        try:
            current_balance_satang = int(balance_data.get("balance", "0"))
        except (ValueError, TypeError):
            logger.error("Invalid balance format")
            return

        tracker.update_balance(current_balance_satang)

        if tracker.has_balance_changed():
            change_satang = tracker.get_balance_change()
            change_baht = abs(change_satang) / 100

            if tracker.is_money_received():
                notification = format_money_received_notification(balance_data, change_baht)
                logger.info(f"💸 Money received: +฿{change_baht:,.2f}")
            else:
                notification = format_money_sent_notification(balance_data, change_baht)
                logger.info(f"💸 Money sent: -฿{change_baht:,.2f}")

            try:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=notification,
                    parse_mode="HTML"
                )
                logger.info("📢 Notification sent successfully")
            except TelegramError as e:
                logger.error(f"Failed to send notification: {str(e)}")
        else:
            logger.debug(f"Balance: ฿{current_balance_satang/100:,.2f} (no change)")

    except Exception as e:
        logger.error(f"Error in monitor_balance: {str(e)}", exc_info=True)


# ============================================================
# Main Entry Point
# ============================================================
def main() -> None:
    logger.info("=" * 50)
    logger.info("TrueMoney Balance Bot - Starting...")
    logger.info(f"Monitoring interval: {MONITORING_INTERVAL}s")
    logger.info(f"Chat ID: {CHAT_ID}")
    logger.info(f"Health check port: {PORT}")
    logger.info("=" * 50)

    # Start HTTP health check server in background thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    logger.info(f"Health check server started on port {PORT}")

    # Build Telegram bot application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data['tracker'] = BalanceTracker()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", check_balance))
    application.add_handler(CommandHandler("check", check_balance))
    application.add_handler(CommandHandler("status", status_command))

    # Start balance monitoring job
    application.job_queue.run_repeating(
        monitor_balance,
        interval=MONITORING_INTERVAL,
        first=5
    )

    logger.info("Bot is running! Monitoring started.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
