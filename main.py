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
import html as html_lib
import re
import shutil
import uuid
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
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


IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm'}


def _requests_session_with_cookies() -> requests.Session:
    """Build a requests session, reusing cookies.txt if it's valid, so the
    fallback scraper sees the same logged-in session yt-dlp would use."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
        ),
    })
    cookies_path = Path(__file__).parent / 'cookies.txt'
    if cookies_path.exists():
        try:
            jar = MozillaCookieJar(str(cookies_path))
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except Exception as exc:
            logger.warning("Failed to load cookies.txt for photo fallback: %s", exc)
    return session


def download_instagram_photo_fallback(url: str) -> tuple[list[Path], str | None]:
    """Fallback for single Instagram photo posts.

    yt-dlp's Instagram extractor is primarily built for video content and can
    raise 'No video formats found!' for photo-only posts. This scrapes the
    Open Graph image/description tags directly from the post page instead.
    """
    session = _requests_session_with_cookies()
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    page = resp.text

    img_match = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    if not img_match:
        raise RuntimeError(
            "og:image not found on the post page (it may be private or require login)"
        )
    image_url = html_lib.unescape(img_match.group(1))

    desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', page)
    caption = html_lib.unescape(desc_match.group(1)) if desc_match else None

    uid = uuid.uuid4().hex
    target_dir = DOWNLOAD_DIR / uid
    target_dir.mkdir(parents=True, exist_ok=True)
    img_resp = session.get(image_url, timeout=30)
    img_resp.raise_for_status()
    file_path = target_dir / f"{uid}.jpg"
    file_path.write_bytes(img_resp.content)
    logger.info("Photo fallback OK: caption_len=%s", len(caption) if caption else 0)
    return [file_path], caption


def download_with_ytdlp(url: str, platform: str) -> tuple[list[Path], str | None]:
    """Download a post using yt-dlp and return all resulting files along with
    the post/reel caption (if available).

    Handles both single video/photo posts and carousels (multiple photos/videos
    in one post) — yt-dlp downloads every item in a carousel by default, so we
    collect all files that end up in a dedicated per-request directory rather
    than assuming there is exactly one output file.
    """
    uid = uuid.uuid4().hex
    target_dir = DOWNLOAD_DIR / uid
    target_dir.mkdir(parents=True, exist_ok=True)
    # autonumber ensures unique, ordered filenames for every item, whether the
    # post is a single video or a multi-item carousel.
    outtmpl = str(target_dir / '%(autonumber)03d_%(id)s.%(ext)s')

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

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info("Starting download: %s", url)
            info_dict = ydl.extract_info(url, download=True)

        entries = info_dict.get('entries')
        if entries:
            # Carousel: the caption usually lives on the playlist-level
            # 'description', but fall back to the first entry's description.
            caption = info_dict.get('description') or next(
                (e.get('description') for e in entries if e and e.get('description')),
                None,
            ) or info_dict.get('title')
        else:
            caption = info_dict.get('description') or info_dict.get('title')

        # Collect every file yt-dlp produced for this request (one file for a
        # single video/photo, several for a carousel).
        files = sorted(p for p in target_dir.iterdir() if p.is_file())
        if not files:
            raise RuntimeError("yt-dlp finished without producing any files")
        logger.info(
            "Download OK: %d file(s), cookies_used=%s, caption_len=%s, caption_preview=%r",
            len(files),
            bool(ydl_opts.get('cookiefile')),
            len(caption) if caption else 0,
            (caption or '')[:200],
        )
        return files, caption
    except Exception as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        if platform == 'instagram' and 'No video formats found' in str(exc):
            logger.info("yt-dlp found no video formats, falling back to photo scrape: %s", url)
            return download_instagram_photo_fallback(url)
        raise


async def download_video(url: str, platform: str) -> tuple[list[Path], str | None]:
    """Download post asynchronously using a thread executor."""
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
        # Download post asynchronously (may return several files for a carousel)
        file_paths, caption = await download_video(url, platform)
    except Exception as exc:
        logger.exception("Download failed: %s", exc)
        await update.message.reply_text("Не удалось скачать видео. Возможно, ссылка неправильная или доступ ограничен.")
        await send_error_to_admin(context, f"Ошибка загрузки для пользователя {user_id}: {exc}")
        return

    max_size = 50 * 1024 * 1024  # 50 MB
    TELEGRAM_CAPTION_LIMIT = 1024
    video_caption = None
    extra_text = None
    if caption:
        if len(caption) <= TELEGRAM_CAPTION_LIMIT:
            video_caption = caption
        else:
            video_caption = caption[:TELEGRAM_CAPTION_LIMIT]
            extra_text = caption[TELEGRAM_CAPTION_LIMIT:]

    try:
        # Split into files small enough to send directly vs. ones that need
        # to be uploaded externally (Telegram's ~50 MB bot upload limit).
        sendable = [p for p in file_paths if p.stat().st_size <= max_size]
        oversized = [p for p in file_paths if p.stat().st_size > max_size]

        if len(sendable) == 1 and not oversized:
            # Single file: send as video or photo depending on extension
            p = sendable[0]
            with open(p, 'rb') as f:
                if p.suffix.lower() in IMAGE_EXTS:
                    await update.message.reply_photo(photo=f, caption=video_caption)
                else:
                    await update.message.reply_video(video=f, caption=video_caption)
            if extra_text:
                await update.message.reply_text(extra_text)
        elif sendable:
            # Carousel: send as a media group, Telegram allows max 10 items per group
            open_files = []
            try:
                for chunk_start in range(0, len(sendable), 10):
                    chunk = sendable[chunk_start:chunk_start + 10]
                    media = []
                    for i, p in enumerate(chunk):
                        f = open(p, 'rb')
                        open_files.append(f)
                        item_caption = video_caption if (chunk_start == 0 and i == 0) else None
                        if p.suffix.lower() in IMAGE_EXTS:
                            media.append(InputMediaPhoto(media=f, caption=item_caption))
                        else:
                            media.append(InputMediaVideo(media=f, caption=item_caption))
                    await update.message.reply_media_group(media=media)
            finally:
                for f in open_files:
                    f.close()
            if extra_text:
                await update.message.reply_text(extra_text)
        else:
            await update.message.reply_text("Не удалось скачать видео. Возможно, ссылка неправильная или доступ ограничен.")

        # Anything too large to send directly gets uploaded externally
        for p in oversized:
            try:
                link = upload_to_transfersh(p)
                await update.message.reply_text(
                    f"Файл {p.name} слишком большой для отправки в Telegram. Вот ссылка для скачивания: {link}"
                )
            except Exception as exc:
                logger.exception("Upload failed: %s", exc)
                await update.message.reply_text(f"Не удалось загрузить файл {p.name} на внешний сервис.")
                await send_error_to_admin(context, f"Ошибка загрузки файла {p.name}: {exc}")
    finally:
        # Clean up the whole per-request download directory
        if file_paths:
            shutil.rmtree(file_paths[0].parent, ignore_errors=True)


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
