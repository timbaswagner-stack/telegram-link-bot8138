import os
import re
import time
import socket
import ipaddress
import traceback
import sys
import subprocess
import importlib.util
from html import unescape
from urllib.parse import urlparse, urljoin

# Автоматически устанавливаем нужные пакеты, если облако их ещё не установило.
# Хостинг должен разрешать установку Python-пакетов во время запуска.
def ensure_package(module_name, package_name):
    if importlib.util.find_spec(module_name) is None:
        print(f"Устанавливаю библиотеку {package_name}...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", package_name
        ])

ensure_package("requests", "requests")
ensure_package("telebot", "pyTelegramBotAPI")

import requests
import telebot
from telebot import types


# ======================== CONFIG ========================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        'Не задан BOT_TOKEN. Добавь токен в переменные окружения '
        'облачного сервиса под именем BOT_TOKEN.'
    )

bot = telebot.TeleBot(TOKEN)

MAX_REDIRECTS = 15
TIMEOUT = (5, 12)
MAX_HTML_BYTES = 500_000
REDIRECT_CODES = (301, 302, 303, 307, 308)

MODE_LINK = "link"
MODE_KEY = "key"

# Stored in memory; modes reset if the bot process restarts.
user_modes = {}


# ======================== URL SAFETY ========================

def validate_url(url):
    """Allow only HTTP/HTTPS URLs resolving exclusively to public IPs."""
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False

        port = parsed.port
        if port is not None and port not in (80, 443):
            return False

        addresses = socket.getaddrinfo(
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses:
            return False

        # Reject a hostname if any resolved address is non-public.
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                return False

        return True
    except (ValueError, OSError):
        return False


def read_limited_html(response):
    """Read only a limited amount of response content."""
    chunks = []
    total = 0

    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue

        remaining = MAX_HTML_BYTES - total
        if remaining <= 0:
            break

        chunk = chunk[:remaining]
        chunks.append(chunk)
        total += len(chunk)

        if total >= MAX_HTML_BYTES:
            break

    raw = b"".join(chunks)
    encoding = response.encoding or "utf-8"

    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ======================== PAGE ANALYSIS ========================

def find_page_redirect(html):
    """Find simple meta-refresh and literal JavaScript redirects."""
    # meta refresh
    for tag in re.findall(r"<meta\b[^>]*>", html, flags=re.I):
        if not re.search(
            r"""\bhttp-equiv\s*=\s*['"]?\s*refresh\b""",
            tag,
            flags=re.I,
        ):
            continue

        content = re.search(
            r"""\bcontent\s*=\s*(["'])(.*?)\1""",
            tag,
            flags=re.I | re.S,
        )
        if content:
            match = re.search(
                r"\burl\s*=\s*(.*?)\s*$",
                content.group(2),
                flags=re.I,
            )
            if match:
                target = unescape(match.group(1).strip().strip("'\""))
                if target:
                    return target, "meta refresh"

    # Simple redirects with a URL written literally in the HTML.
    patterns = [
        r"""(?:window\s*\.\s*)?location\s*
            (?:\.\s*href)?\s*=\s*["']([^"'<>]+)["']""",
        r"""(?:window\s*\.\s*)?location\s*
            \.\s*(?:replace|assign)\s*
            \(\s*["']([^"'<>]+)["']\s*\)""",
    ]

    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I | re.X)
        if match:
            target = unescape(match.group(1).strip())
            if target:
                return target, "simple JavaScript"

    return None, None


def find_access_key(html):
    """
    Look for a key explicitly printed in accessible page content.
    This does not execute scripts, solve challenges, or bypass ad gates.
    """
    text = unescape(html)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text,
                  flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text,
                  flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    patterns = [
        r"""(?:access[\s_-]*key|your[\s_-]*key|license[\s_-]*key|
             key\s*(?:is|:|=)|ключ(?:\s+доступа)?\s*(?:это|:|=))
             \s*["']?([A-Za-z0-9][A-Za-z0-9_-]{7,127})""",
        r"""["'](?:key|access_key|accessKey|license_key)["']\s*:
             \s*["']([A-Za-z0-9][A-Za-z0-9_-]{7,127})["']""",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.X)
        if match:
            candidate = match.group(1).strip()
            if candidate.lower() not in {
                "available", "required", "undefined", "null", "example"
            }:
                return candidate

    return None


def expand_url(original_url, search_key=False):
    """
    Walk supported HTTP redirects and simple HTML redirects.
    Returns a dict describing the result and redirect history.
    Does not automate mandatory ad tasks, CAPTCHA, or authentication.
    """
    url = original_url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    history = []
    seen = set()
    found_key = None

    with requests.Session() as session:
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        })

        for step in range(MAX_REDIRECTS + 1):
            if not validate_url(url):
                raise ValueError(
                    "Адрес некорректен или ведёт на непубличный IP."
                )

            if url in seen:
                return {
                    "url": url, "history": history, "status": None,
                    "kind": "loop", "key": found_key
                }

            seen.add(url)
            history.append(url)
            print(f"[{step + 1}] Проверка: {url}", flush=True)

            with session.get(
                url,
                allow_redirects=False,
                timeout=TIMEOUT,
                stream=True,
            ) as response:
                status = response.status_code
                location = response.headers.get("Location")
                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

                print(f"    HTTP {status}", flush=True)

                if status in REDIRECT_CODES and location:
                    next_url = urljoin(url, location.strip())
                    if next_url == url:
                        return {
                            "url": url, "history": history,
                            "status": status, "kind": "loop",
                            "key": found_key
                        }
                    url = next_url
                    continue

                if status in REDIRECT_CODES and not location:
                    return {
                        "url": url, "history": history,
                        "status": status, "kind": "missing_location",
                        "key": found_key
                    }

                if status in (401, 403, 407, 429):
                    return {
                        "url": url, "history": history,
                        "status": status, "kind": "restricted",
                        "key": found_key
                    }

                if status == 200 and "text/html" in content_type:
                    html = read_limited_html(response)
                    lower_html = html.lower()

                    gate_markers = (
                        "captcha", "recaptcha", "hcaptcha", "turnstile",
                        "verify you are human", "complete the offer",
                        "unlock your key", "key system",
                    )
                    if any(marker in lower_html for marker in gate_markers):
                        # Do not try to get past an access gate.
                        if search_key:
                            found_key = find_access_key(html) or found_key
                        return {
                            "url": url, "history": history,
                            "status": status, "kind": "gate",
                            "key": found_key
                        }

                    if search_key and not found_key:
                        found_key = find_access_key(html)

                    target, redirect_kind = find_page_redirect(html)
                    if target:
                        next_url = urljoin(url, target)
                        parsed = urlparse(next_url)

                        if parsed.scheme not in ("http", "https"):
                            return {
                                "url": url, "history": history,
                                "status": status, "kind": "unsupported",
                                "key": found_key
                            }

                        if next_url == url:
                            return {
                                "url": url, "history": history,
                                "status": status, "kind": "loop",
                                "key": found_key
                            }

                        print(f"    Найден переход: {redirect_kind}",
                              flush=True)
                        url = next_url
                        continue

                return {
                    "url": url, "history": history,
                    "status": status, "kind": "done",
                    "key": found_key
                }

    return {
        "url": url, "history": history, "status": None,
        "kind": "limit", "key": found_key
    }


# ======================== TELEGRAM UI ========================

def copy_button(text, label="📋 Скопировать"):
    """Use Telegram's native copy-text button when supported."""
    try:
        copy_text_type = getattr(types, "CopyTextButton", None)
        if copy_text_type is not None:
            return types.InlineKeyboardButton(
                text=label,
                copy_text=copy_text_type(text=text[:256]),
            )
    except (TypeError, AttributeError):
        pass
    return None


def menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(
            "🔗 Режим 1: конечная ссылка", callback_data="mode_link"
        ),
        types.InlineKeyboardButton(
            "🔑 Режим 2: анализ ключа", callback_data="mode_key"
        ),
    )
    return markup


def result_keyboard(copy_value=None, copy_label="📋 Скопировать"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    if copy_value:
        btn = copy_button(copy_value, copy_label)
        if btn is not None:
            markup.add(btn)
    markup.add(
        types.InlineKeyboardButton("⚙️ Сменить режим", callback_data="show_modes")
    )
    return markup


def current_mode(chat_id):
    return user_modes.get(chat_id, MODE_LINK)


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    mode = current_mode(message.chat.id)
    mode_name = "Конечная ссылка" if mode == MODE_LINK else "Анализ ключа"
    bot.send_message(
        message.chat.id,
        "Привет! Я проверяю цепочки перенаправлений и анализирую "
        "доступное содержимое веб-страниц.\n\n"
        f"Текущий режим: {mode_name}\n\n"
        "Команды:\n"
        "/mode — выбрать режим\n"
        "/link — раскрытие обычных ссылок\n"
        "/key — поиск явно опубликованного ключа\n"
        "/cancel — отменить текущую задачу (если ещё выполняется)\n\n"
        "Ограничение: бот не запускает полноценный браузер и не "
        "обходит CAPTCHA, авторизацию или обязательные рекламные задания.",
        reply_markup=menu_keyboard(),
    )


@bot.message_handler(commands=["mode"])
def cmd_mode(message):
    bot.send_message(
        message.chat.id,
        "Выбери режим:",
        reply_markup=menu_keyboard(),
    )


@bot.message_handler(commands=["link"])
def cmd_link(message):
    user_modes[message.chat.id] = MODE_LINK
    bot.reply_to(message, "✅ Включён режим конечной ссылки. Отправь URL.")


@bot.message_handler(commands=["key"])
def cmd_key(message):
    user_modes[message.chat.id] = MODE_KEY
    bot.reply_to(
        message,
        "🔑 Включён режим анализа ключа. Отправь URL страницы, "
        "содержимое которой разрешено просматривать."
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    bot.reply_to(
        message,
        "Бот не хранит отдельную очередь задач. Если запрос уже выполняется, "
        "дождись ответа или останови процесс бота в Termux сочетанием Ctrl+C."
    )


@bot.callback_query_handler(func=lambda call: call.data in (
    "mode_link", "mode_key", "show_modes"
))
def mode_callback(call):
    chat_id = call.message.chat.id

    if call.data == "mode_link":
        user_modes[chat_id] = MODE_LINK
        bot.answer_callback_query(call.id, "Включён режим конечной ссылки")
        bot.send_message(chat_id, "🔗 Режим конечной ссылки включён. Отправь URL.")
    elif call.data == "mode_key":
        user_modes[chat_id] = MODE_KEY
        bot.answer_callback_query(call.id, "Включён режим анализа ключа")
        bot.send_message(
            chat_id,
            "🔑 Режим анализа ключа включён. Отправь URL страницы."
        )
    else:
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "Выбери режим:", reply_markup=menu_keyboard())


@bot.message_handler(content_types=["text"])
def handle_text(message):
    original = message.text.strip()

    if original.startswith("/"):
        return

    if not original.startswith(("http://", "https://")):
        bot.reply_to(
            message,
            "Отправь полную ссылку, начинающуюся с http:// или https://\n"
            "Для выбора режима используй /mode."
        )
        return

    mode = current_mode(message.chat.id)
    wait_msg = bot.reply_to(message, "⏳ Проверяю ссылку...")

    try:
        result = expand_url(original, search_key=(mode == MODE_KEY))
        final_url = result["url"]
        history = result["history"]
        status = result["status"]
        kind = result["kind"]
        key = result["key"]

        lines = []

        if mode == MODE_LINK:
            if kind == "done":
                lines.append("✅ Проверка поддерживаемых переходов завершена.")
                lines.append("Последний подтверждённый адрес:")
            elif kind == "gate":
                lines.append("⚠️ Обнаружена страница проверки или рекламного задания.")
                lines.append("Бот не пытался обходить её. Текущий адрес:")
            elif kind == "restricted":
                lines.append(f"⚠️ Сайт ограничил запрос (HTTP {status}).")
                lines.append("Последний проверенный адрес:")
            elif kind == "limit":
                lines.append("⚠️ Достигнут лимит перенаправлений.")
                lines.append("Последний проверенный адрес:")
            elif kind == "missing_location":
                lines.append("⚠️ Сервер не указал адрес следующего перехода.")
                lines.append("Последний проверенный адрес:")
            elif kind == "unsupported":
                lines.append("⚠️ Обнаружен неподдерживаемый тип перехода.")
                lines.append("Последний проверенный адрес:")
            else:
                lines.append("⚠️ Цепочка остановилась: дальнейший переход не найден.")
                lines.append("Последний проверенный адрес:")

            lines.append(final_url)
            copy_value = final_url
            copy_label = "📋 Скопировать ссылку"

        else:
            if key:
                lines.append("🔑 Найден текст, похожий на ключ:")
                lines.append(key)
                lines.append("")
                lines.append("Проверь, что это нужный ключ для данного сайта.")
                copy_value = key
                copy_label = "📋 Скопировать ключ"
            else:
                lines.append("🔎 Явно опубликованный ключ не найден.")
                lines.append(
                    "Возможно, страница формирует его JavaScript-кодом, "
                    "требует входа или ручного действия."
                )
                copy_value = final_url
                copy_label = "📋 Скопировать адрес страницы"

            lines.append("")
            lines.append(f"Последний проверенный адрес:\n{final_url}")

            if kind == "gate":
                lines.append(
                    "\nОбнаружена страница проверки/рекламного задания. "
                    "Автоматически обходить её бот не будет."
                )
            elif kind == "restricted":
                lines.append(f"\nСайт ограничил запрос: HTTP {status}.")

        lines.append("")
        lines.append(f"Проверено адресов: {len(history)}")
        if status is not None:
            lines.append(f"HTTP-статус последней страницы: {status}")

        if len(history) > 1:
            lines.append("\nИстория переходов:")
            for index, item in enumerate(history, start=1):
                lines.append(f"{index}. {item}")

        output = "\n".join(lines)

        # Avoid Telegram message length errors.
        if len(output) > 3900:
            output = output[:3800] + "\n\n…История сокращена."

        bot.send_message(
            message.chat.id,
            output,
            reply_markup=result_keyboard(copy_value, copy_label),
            disable_web_page_preview=True,
        )

    except requests.exceptions.Timeout:
        bot.send_message(
            message.chat.id,
            "⌛ Сайт отвечает слишком долго. Попробуй позже."
        )
    except requests.exceptions.RequestException as error:
        print("Ошибка сети:", repr(error), flush=True)
        bot.send_message(
            message.chat.id,
            "⚠️ Не удалось получить ответ сайта. Подробности смотри в Termux."
        )
    except ValueError as error:
        bot.send_message(message.chat.id, f"⚠️ Ошибка ссылки:\n{error}")
    except Exception:
        traceback.print_exc()
        bot.send_message(
            message.chat.id,
            "⚠️ Ошибка при обработке. Подробности смотри в Termux."
        )
    finally:
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except Exception:
            pass


print("Бот запущен!", flush=True)

while True:
    try:
        bot.infinity_polling(
            timeout=20,
            long_polling_timeout=20,
            skip_pending=True,
        )
        print("Соединение завершилось. Перезапуск...", flush=True)
    except KeyboardInterrupt:
        print("Бот остановлен вручную.", flush=True)
        break
    except Exception:
        traceback.print_exc()
        print("Ошибка соединения. Повтор через 3 секунды.", flush=True)
        time.sleep(3)
