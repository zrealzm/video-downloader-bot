#!/usr/bin/env python3
"""
Telegram video downloader bot.

Этот скрипт реализует Telegram‑бота, который принимает ссылки на видео из популярных сервисов
и возвращает скачанный файл пользователю или ссылку на скачивание, если файл слишком большой.

Бот запускается через встроенный веб-сервер python-telegram-bot (webhook), что подходит
для хостинга на Render.
"""

import os
import logging
import asyncio
import uuid
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

import yt_dlp
import requests


# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # Telegram ID of admin for error notifications
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")  # Numeric chat_id of the private channel, e.g. -1001234567890
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK")  # Invite link shown to users, e.g. https://t.me/+xxxxxxxxxxxx
BASE_URL = os.getenv("BASE_URL")  # Public URL of this service for webhook

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable not set")


# Create downloads directory
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)


async def is_subscribed(user_id: int, channel: str, application: Application) -> bool:
    """Check whether a user is subscribed to a channel.

    Returns True if subscribed or no channel configured.
    """
    if not channel:
        # No channel restriction
        return True
    try:
        member = await application.bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ("creator", "administrator", "member")
    except TelegramError as e:
        logger.warning("Failed to check subscription: %s", e)
        return False


async def send_error_to_admin(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    """Send error message to admin if ADMIN_ID is configured."""
    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_ID), text=message)
        except Exception as exc:
            logger.error("Failed to send error to admin: %s", exc)


def extract_url(text: str) -> str | None:
    """Extract the first URL from text using a simple split/parse.

    This function isn't perfect but suffices for typical Telegram messages.
    """
    for part in text.split():
        if part.startswith("http://") or part.startswith("https://"):
            return part
    return None


def get_platform(url: str) -> str | None:
    """Determine the platform based on the URL domain."""
    url_lower = url.lower()
    if "instagram.com" in url_lower:
        return "instagram"
    if "tiktok.com" in url_lower:
        return "tiktok"
    if "youtube.com/shorts" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    if "vk.com" in url_lower:
        return "vk"
    return None


def download_with_ytdlp(url: str, platform: str) -> Path:
    """Download a video using yt-dlp and return the path to the downloaded file.

    This function runs synchronously and may block. It returns the path of the downloaded file
    in DOWNLOAD_DIR.
    """
    # Create a unique filename prefix to avoid collisions
    uid = uuid.uuid4().hex
    # Output template: store in downloads directory with uid
    outtmpl = str(DOWNLOAD_DIR / f"{uid}.%(ext)s")

    ydl_opts: dict[str, object] = {
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
        # Format selection: best video+audio
        'format': 'bestvideo+bestaudio/best',
        # Some sites require this to embed video and audio
        'merge_output_format': 'mp4',
    }

    # Add cookies for Instagram if available and the file looks like a valid
    # Netscape-format cookies file (yt-dlp/http.cookiejar will crash otherwise)
    cookies_path = Path(__file__).parent / 'cookies.txt'
    if platform == 'instagram' and cookies_path.exists():
        try:
            first_line = cookies_path.read_text(encoding='utf-8', errors='ignore').splitlines()[0]
        except (IndexError, OSError):
            first_line = ''
        if first_line.startswith('# Netscape HTTP Cookie File') or first_line.startswith('# HTTP Cookie File'):
            ydl_opts['cookiefile'] = str(cookies_path)
        else:
            logger.warning(
                "cookies.txt exists but is not in Netscape format, ignoring it: %s",
                cookies_path,
            )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        logger.info("Starting download: %s", url)
        info_dict = ydl.extract_info(url, download=True)
        # Determine file extension and path
        if 'requested_downloads' in info_dict:
            # When using merge_output_format the final file name is in requested_downloads
            download_info = info_dict['requested_downloads'][0]
        else:
            download_info = info_dict
        filepath = download_info.get('filepath') or download_info.get('filename') or outtmpl
        return Path(filepath)


async def download_video(url: str, platform: str) -> Path:
    """Download video asynchronously using a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, download_with_ytdlp, url, platform)


def upload_to_transfersh(file_path: Path) -> str:
    """Upload a file to transfer.sh and return the download URL."""
    file_name = file_path.name
    with open(file_path, 'rb') as f:
        response = requests.put(f'https://transfer.sh/{file_name}', data=f)
    if response.status_code == 200:
        return response.text.strip()
    raise RuntimeError(f"Failed to upload file: HTTP {response.status_code}")


# Telegram application
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for Telegram application."""
    logger.exception("An error occurred: %s", context.error)
    await send_error_to_admin(context, f"⚠️ Произошла ошибка: {context.error}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    keyboard = [
        [
            InlineKeyboardButton("🎥 Скачать видео", callback_data="download"),
            InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! Я помогу скачать видео с Instagram, TikTok, YouTube Shorts и VK. "
        "Нажми на кнопку или просто отправь ссылку.",
        reply_markup=reply_markup,
    )


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button presses from inline keyboard."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data
    if data == "help":
        await query.message.reply_text(
            "Отправьте мне ссылку на видео, и я постараюсь его скачать. "
            "Поддерживаются Instagram, TikTok, YouTube Shorts и VK. Если файл больше 50 МБ, "
            "я пришлю ссылку для скачивания."
        )
    elif data == "download":
        await query.message.reply_text(
            "Отправьте ссылку на видео – и я всё сделаю!"
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages with potential URLs."""
    if not update.message:
        return
    user_id = update.message.from_user.id
    # Enforce subscription if required
    if not await is_subscribed(user_id, REQUIRED_CHANNEL, telegram_app):
        channel_link = REQUIRED_CHANNEL_LINK or REQUIRED_CHANNEL
        await update.message.reply_text(
            f"Чтобы пользоваться ботом, подпишитесь на канал {channel_link} и попробуйте снова."
        )
        return
    text = update.message.text
    url = extract_url(text)
    if not url:
        await update.message.reply_text("Я не вижу ссылки в вашем сообщении. Пожалуйста, отправьте корректную ссылку.")
        return
    platform = get_platform(url)
    if not platform:
        await update.message.reply_text("Извините, я пока не умею скачивать с этого ресурса. Поддерживаются Instagram, TikTok, YouTube Shorts и VK.")
        return
    # Acknowledge reception
    await update.message.reply_text("🔄 Загружаю ваше видео, подождите немного...")
    try:
        # Download video asynchronously
        file_path = await download_video(url, platform)
    except Exception as exc:
        logger.exception("Download failed: %s", exc)
        await update.message.reply_text("Не удалось скачать видео. Возможно, ссылка неправильная или доступ ограничен.")
        await send_error_to_admin(context, f"Ошибка загрузки для пользователя {user_id}: {exc}")
        return
    # Send or upload depending on size
    try:
        file_size = file_path.stat().st_size
        max_size = 50 * 1024 * 1024  # 50 MB
        if file_size <= max_size:
            # Send directly
            with open(file_path, 'rb') as f:
                await update.message.reply_video(video=f)
        else:
            # Upload to transfer.sh
            try:
                link = upload_to_transfersh(file_path)
                await update.message.reply_text(
                    f"Файл слишком большой для отправки в Telegram. Вот ссылка для скачивания: {link}"
                )
            except Exception as exc:
                logger.exception("Upload failed: %s", exc)
                await update.message.reply_text("Не удалось загрузить видео на внешний сервис.")
                await send_error_to_admin(context, f"Ошибка загрузки файла {file_path.name}: {exc}")
                return
    finally:
        # Clean up downloaded file
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass


# Регистрация хендлеров (обязательно после определения функций выше)
telegram_app.add_error_handler(on_error)
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(on_callback_query))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


def run() -> None:
    """Start the bot using PTB's built-in webhook server."""
    port = int(os.environ.get("PORT", 8080))
    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook"
        telegram_app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=webhook_url,
        )
    else:
        # Fallback: polling (для локальной отладки; на Render как web service не подходит)
        telegram_app.run_polling()


if __name__ == '__main__':
    run()
