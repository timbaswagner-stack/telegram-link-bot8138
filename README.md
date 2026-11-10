import os
import time
import json
import threading
import subprocess
import importlib.util
import sys
from pathlib import Path

def ensure_package(module_name, package_name):
    if importlib.util.find_spec(module_name) is None:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", package_name
        ])

ensure_package("telebot", "pyTelegramBotAPI")

import telebot
from telebot import types
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError('Не задан BOT_TOKEN.')

bot = telebot.TeleBot(TOKEN)

COMMAND_FILE = Path("commands.json")
COMMAND_PORT = int(os.getenv("PORT", os.getenv("COMMAND_PORT", "8080")))
command_lock = threading.Lock()

VALID_COMMANDS = {
    "w": "W", "a": "A", "s": "S", "d": "D",
    "пробел": "Space", "space": "Space", "jump": "Space",
    "прыжок": "Space", "sprint": "W", "бег": "W"
}

def save_command(cmd: str):
    key = cmd.strip().lower()
    if key not in VALID_COMMANDS:
        return False
    entry = {"cmd": VALID_COMMANDS[key], "ts": time.time()}
    with command_lock:
        COMMAND_FILE.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    return True

class CommandHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path != "/get_command":
            self.send_response(404)
            self.end_headers()
            return
        with command_lock:
            try:
                data = json.loads(COMMAND_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if data:
                COMMAND_FILE.write_text("", encoding="utf-8")
        body = json.dumps(data or {}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def run_command_server():
    try:
        server = HTTPServer(("0.0.0.0", COMMAND_PORT), CommandHandler)
        print(f"HTTP server на порту: {COMMAND_PORT}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"Ошибка сервера: {e}", flush=True)

def menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📖 Список команд", callback_data="show_help"))
    return markup

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    text = (
        "🤖 **Функционал изменён.**\n\n"
        "Старый функционал со ссылками больше не работает.\n\n"
        "Управление:\n"
        "• `w` — вперёд\n"
        "• `s` — назад\n"
        "• `a` — влево\n"
        "• `d` — вправо\n"
        "• `пробел` — прыжок"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=menu_keyboard())

@bot.callback_query_handler(func=lambda call: call.data == "show_help")
def callback_help(call):
    bot.answer_callback_query(call.id)
    cmd_start(call.message)

@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = message.text.strip()
    
    if text.startswith("/"):
        return

    if save_command(text):
        bot.reply_to(message, f"✅ Команда «{text}» отправлена в Roblox.")
        return

    bot.reply_to(
        message,
        "⚠️ Старый функционал больше не работает.\nОтправь `w`, `a`, `s`, `d` или `пробел`.",
        parse_mode="Markdown"
    )

if __name__ == "__main__":
    print("Бот запущен!", flush=True)
    threading.Thread(target=run_command_server, daemon=True).start()

    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
            print("Перезапуск...", flush=True)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Ошибка: {e}. Повтор через 3 сек.", flush=True)
            time.sleep(3)
