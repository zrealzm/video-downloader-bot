#!/usr/bin/env python3
"""
Telegram video downloader bot.

Принимает ссылки на посты/рилсы/видео из Instagram, TikTok, YouTube Shorts и VK
и присылает скачанный файл (видео, фото, или все элементы карусели) в чат,
вместе с исходной подписью. Работает как веб-сервис на вебхуках (свой сервер на
aiohttp), что подходит для хостинга на Render и подобных площадках.
"""

import os
import logging
import asyncio
import html as html_lib
import json
import re
import shutil
import subprocess
import uuid
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from aiohttp import web

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
from telegram.error import TelegramError, TimedOut
from telegram.request import HTTPXRequest

import yt_dlp
import requests


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # Telegram ID администратора для уведомлений об ошибках
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")  # numeric chat_id канала, напр. -1001234567890
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK")  # инвайт-ссылка для пользователя
BASE_URL = os.getenv("BASE_URL")  # публичный URL сервиса для вебхука

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable not set")

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', 'vp9'}

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 2000 MB
TELEGRAM_CAPTION_LIMIT = 1024

# Render's free tier has very limited CPU/RAM. Only one download (and
# possible transcode) runs at a time, so bursts of requests queue instead of
# competing for the same scarce resources and crashing the process.
MAX_CONCURRENT_DOWNLOADS = 1
_download_semaphore: asyncio.Semaphore | None = None



# ---------------------------------------------------------------------------
# Helpers: subscription check, URL/platform parsing
# ---------------------------------------------------------------------------

async def is_subscribed(user_id: int, channel: str, application: Application) -> bool:
    """Check whether a user is subscribed to a (usually private) channel.

    REQUIRED_CHANNEL must be the channel's numeric chat_id (e.g.
    -1001234567890) — Bot API can't check membership via an invite link.
    Returns True if no channel is configured.
    """
    if not channel:
        return True
    try:
        member = await application.bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ("creator", "administrator", "member")
    except TelegramError as e:
        logger.warning("Failed to check subscription: %s", e)
        return False


async def send_error_to_admin(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_ID), text=message)
        except Exception as exc:
            logger.error("Failed to send error to admin: %s", exc)


def extract_url(text: str) -> str | None:
    for part in text.split():
        if part.startswith("http://") or part.startswith("https://"):
            return part
    return None


def get_platform(url: str) -> str | None:
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


# ---------------------------------------------------------------------------
# Instagram photo-post fallback (for posts yt-dlp's Instagram extractor
# can't handle at all — it's primarily built for video content)
# ---------------------------------------------------------------------------

def _requests_session_with_cookies() -> requests.Session:
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
            logger.info("Photo fallback session: cookies_loaded=%d", len(jar))
        except Exception as exc:
            logger.warning("Failed to load cookies.txt for photo fallback: %s", exc)
    return session


def download_instagram_photo_fallback(url: str) -> tuple[list[Path], str | None]:
    """Fallback for Instagram posts yt-dlp can't extract at all (typically
    photo-only posts). Scrapes Open Graph tags from the post page. Note:
    Instagram increasingly serves a JS app shell without these tags to plain
    HTTP clients, so this is best-effort and may simply fail."""
    session = _requests_session_with_cookies()
    crawler_headers = {
        'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    }
    resp = session.get(url, timeout=20, headers=crawler_headers)
    resp.raise_for_status()
    page = resp.text
    logger.info(
        "Photo fallback page fetched: status=%s final_url=%s len=%d",
        resp.status_code, resp.url, len(page),
    )

    def _find_image(page: str):
        return (
            re.search(r'<meta property="og:image" content="([^"]+)"', page)
            or re.search(r'<meta content="([^"]+)" property="og:image"', page)
            or re.search(r'"display_url"\s*:\s*"([^"]+)"', page)
        )

    img_match = _find_image(page)
    if not img_match:
        logger.warning("Crawler-UA fetch had no og:image, retrying with browser UA")
        resp = session.get(
            url, timeout=20,
            headers={'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8', 'Referer': 'https://www.instagram.com/'},
        )
        resp.raise_for_status()
        page = resp.text
        img_match = _find_image(page)

    if not img_match:
        logger.warning("Photo fallback page snippet: %r", page[:500])
        raise RuntimeError("og:image not found on the post page (private, login-walled, or blocked)")

    image_url = html_lib.unescape(img_match.group(1)).replace('\\u0026', '&').replace('\\/', '/')
    desc_match = (
        re.search(r'<meta property="og:description" content="([^"]+)"', page)
        or re.search(r'<meta content="([^"]+)" property="og:description"', page)
    )
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


# ---------------------------------------------------------------------------
# Video inspection / lightweight H.264 transcode
# ---------------------------------------------------------------------------

def get_video_metadata(file_path: Path) -> dict:
    """Probe a video file with ffprobe. Returns a dict with whatever of
    {'video_codec', 'has_audio', 'width', 'height', 'duration'} could be
    determined (empty dict on failure). Also logs a full stream summary."""
    metadata: dict = {}
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'stream=codec_type,codec_name,pix_fmt,profile,width,height,'
                                  'avg_frame_rate,nb_frames,duration',
                '-of', 'json', str(file_path),
            ],
            capture_output=True, text=True, timeout=20,
        )
        info = json.loads(result.stdout or '{}')
        streams = info.get('streams', [])
        summary = [
            {
                'type': s.get('codec_type'),
                'codec': s.get('codec_name'),
                'size': f"{s.get('width')}x{s.get('height')}" if s.get('width') else None,
                'fps': s.get('avg_frame_rate'),
                'frames': s.get('nb_frames'),
                'duration': s.get('duration'),
            }
            for s in streams
        ]
        logger.info("ffprobe %s: %s", file_path.name, summary)
        if result.stderr:
            logger.warning("ffprobe stderr for %s: %s", file_path.name, result.stderr.strip())

        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video_stream:
            metadata['video_codec'] = video_stream.get('codec_name')
            metadata['pix_fmt'] = video_stream.get('pix_fmt')
            metadata['profile'] = video_stream.get('profile')
            if video_stream.get('width') and video_stream.get('height'):
                metadata['width'] = int(video_stream['width'])
                metadata['height'] = int(video_stream['height'])
            if video_stream.get('duration'):
                metadata['duration'] = int(float(video_stream['duration']))
        metadata['has_audio'] = any(s.get('codec_type') == 'audio' for s in streams)
    except Exception as exc:
        logger.warning("ffprobe diagnostic failed for %s: %s", file_path.name, exc)
    return metadata


def convert_for_telegram(src: Path, duration: int | None = None) -> Path:
    """
    Перекодирование в максимально совместимый с Telegram формат.

    Render free tier: очень мало CPU/RAM, encode speed часто заметно ниже
    реального времени. Поэтому: ultrafast, 1 поток, разрешение до 480p, и
    таймаут, растущий вместе с длительностью исходника (иначе более длинные
    ролики просто не успевали до фиксированного лимита).
    """
    dst = src.with_name(src.stem + "_telegram.mp4")

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),

        "-map", "0:v:0",
        "-map", "0:a?",

        "-vf", "scale=-2:'min(480,ih)'",

        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "26",
        "-threads", "1",

        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.1",

        "-movflags", "+faststart",

        "-c:a", "aac",
        "-b:a", "96k",

        str(dst),
    ]

    # Assume worst case ~1x realtime on Render's shared CPU, with generous
    # headroom, capped so a single conversion can't hang forever.
    timeout = min(max(60, (duration or 30) * 6), 240)

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        logger.warning("convert_for_telegram failed/timed out for %s (timeout=%ds): %s", src.name, timeout, exc)
        dst.unlink(missing_ok=True)
        return src  # graceful fallback: send the original file as-is

    return dst


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_with_ytdlp(url: str, platform: str) -> tuple[list[Path], str | None]:
    """Download a post (video, photo, or carousel) and return every file
    produced plus the caption. Carousels: downloads every item it can;
    if some items fail to extract (e.g. a broken photo slide), those are
    skipped rather than aborting the whole post."""
    uid = uuid.uuid4().hex
    target_dir = DOWNLOAD_DIR / uid
    target_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(target_dir / '%(autonumber)03d_%(id)s.%(ext)s')

    ydl_opts: dict[str, object] = {
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
        # Format priority — force H.264 wherever possible. We now also
        # transcode as a safety net (see convert_for_telegram), so this is
        # just about avoiding unnecessary transcodes when H.264 is available.
        'format': (
            'bv*[vcodec*=avc1]+ba'
            '/b*[vcodec*=avc1]'
            '/bv+ba'
            '/b'
        ),
        'postprocessor_args': {'ffmpeg': ['-movflags', '+faststart']},
        'merge_output_format': 'mp4',
        # Skip individual broken carousel items instead of aborting the
        # whole post's download.
        'ignoreerrors': True,
    }

    cookies_path = Path(__file__).parent / 'cookies.txt'
    if platform == 'instagram' and cookies_path.exists():
        try:
            first_line = cookies_path.read_text(encoding='utf-8', errors='ignore').splitlines()[0]
        except (IndexError, OSError):
            first_line = ''
        if first_line.startswith('# Netscape HTTP Cookie File') or first_line.startswith('# HTTP Cookie File'):
            ydl_opts['cookiefile'] = str(cookies_path)
        else:
            logger.warning("cookies.txt exists but is not in Netscape format, ignoring it: %s", cookies_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info("Starting download: %s", url)
            info_dict = ydl.extract_info(url, download=True)

        if info_dict is None:
            # ignoreerrors=True means a totally unextractable single post
            # (e.g. a lone photo yt-dlp's Instagram extractor can't handle)
            # comes back as None instead of raising.
            raise RuntimeError("yt-dlp returned no info for this URL")

        entries = info_dict.get('entries')
        if entries:
            entries = [e for e in entries if e]  # drop skipped/failed items
            caption = info_dict.get('description') or next(
                (e.get('description') for e in entries if e.get('description')), None,
            ) or info_dict.get('title')
        else:
            caption = info_dict.get('description') or info_dict.get('title')

        files = sorted(p for p in target_dir.iterdir() if p.is_file())
        if not files:
            raise RuntimeError("yt-dlp finished without producing any files")
        logger.info(
            "Download OK: %d file(s), cookies_used=%s, caption_len=%s, caption_preview=%r",
            len(files), bool(ydl_opts.get('cookiefile')),
            len(caption) if caption else 0, (caption or '')[:200],
        )
        return files, caption
    except Exception as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        if platform == 'instagram':
            logger.info("yt-dlp failed (%s), trying photo-page fallback for: %s", exc, url)
            try:
                return download_instagram_photo_fallback(url)
            except Exception as fallback_exc:
                logger.warning("Photo fallback also failed: %s", fallback_exc)
        raise


def _get_download_semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _download_semaphore


async def download_video(url: str, platform: str) -> tuple[list[Path], str | None]:
    """Download asynchronously via a thread executor, capped to
    MAX_CONCURRENT_DOWNLOADS concurrent jobs."""
    async with _get_download_semaphore():
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, download_with_ytdlp, url, platform)


def upload_to_transfersh(file_path: Path) -> str:
    """Upload a file to transfer.sh and return the download URL."""
    file_name = file_path.name
    with open(file_path, 'rb') as f:
        response = requests.put(f'https://transfer.sh/{file_name}', data=f, timeout=120)
    if response.status_code == 200:
        return response.text.strip()
    raise RuntimeError(f"Failed to upload file: HTTP {response.status_code}")


async def _retry_on_timeout(send_coro_factory, attempts: int = 3, delay: float = 3.0):
    """Retry a send operation on Telegram TimedOut errors — these have shown
    up as transient network blips on Render's free tier."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await send_coro_factory()
        except TimedOut as exc:
            last_exc = exc
            logger.warning("Send timed out (attempt %d/%d): %s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------------
# Telegram application & handlers
# ---------------------------------------------------------------------------

def _make_bot_request() -> HTTPXRequest:
    # Default httpx timeouts (a few seconds) are too short for uploading
    # video files to Telegram over Render's connection.
    return HTTPXRequest(connect_timeout=30.0, read_timeout=60.0, write_timeout=180.0, pool_timeout=30.0)


telegram_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .request(_make_bot_request())
    .get_updates_request(_make_bot_request())
    .build()
)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("An error occurred: %s", context.error)
    await send_error_to_admin(context, f"⚠️ Произошла ошибка: {context.error}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[
        InlineKeyboardButton("🎥 Скачать видео", callback_data="download"),
        InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
    ]]
    await update.message.reply_text(
        "Привет! Я помогу скачать видео и фото с Instagram, TikTok, YouTube Shorts и VK. "
        "Просто отправь ссылку.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data == "help":
        await query.message.reply_text(
            "Отправьте ссылку на пост/рилс/видео — я скачаю и пришлю файл(ы). "
            "Поддерживаются Instagram, TikTok, YouTube Shorts и VK. Если файл больше 50 МБ, "
            "пришлю ссылку для скачивания."
        )
    elif query.data == "download":
        await query.message.reply_text("Отправьте ссылку на видео или пост – и я всё сделаю!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """React only to messages containing a supported link — everything else
    (regular chat) is ignored so the bot doesn't interrupt group chatter."""
    if not update.message or not update.message.text:
        return
    url = extract_url(update.message.text)
    if not url:
        return

    user_id = update.message.from_user.id
    if not await is_subscribed(user_id, REQUIRED_CHANNEL, telegram_app):
        channel_link = REQUIRED_CHANNEL_LINK or REQUIRED_CHANNEL
        await update.message.reply_text(
            f"Чтобы пользоваться ботом, подпишитесь на канал {channel_link} и попробуйте снова."
        )
        return

    platform = get_platform(url)
    if not platform:
        await update.message.reply_text(
            "Извините, я пока не умею скачивать с этого ресурса. "
            "Поддерживаются Instagram, TikTok, YouTube Shorts и VK."
        )
        return

    try:
        file_paths, caption = await download_video(url, platform)
    except Exception as exc:
        logger.exception("Download failed: %s", exc)
        if platform == 'instagram' and 'No video formats found' in str(exc):
            await update.message.reply_text(
                "Похоже, это фото-пост Instagram, который сейчас не получается скачать "
                "из-за ограничений на стороне Instagram/yt-dlp. Видео и рилсы работают нормально."
            )
        else:
            await update.message.reply_text("Не удалось скачать. Возможно, ссылка неправильная или доступ ограничен.")
        await send_error_to_admin(context, f"Ошибка загрузки для пользователя {user_id}: {exc}")
        return

    video_caption = None
    extra_text = None
    if caption:
        if len(caption) <= TELEGRAM_CAPTION_LIMIT:
            video_caption = caption
        else:
            video_caption = caption[:TELEGRAM_CAPTION_LIMIT]
            extra_text = caption[TELEGRAM_CAPTION_LIMIT:]

    try:
        sendable = [p for p in file_paths if p.stat().st_size <= MAX_TELEGRAM_FILE_SIZE]
        oversized = [p for p in file_paths if p.stat().st_size > MAX_TELEGRAM_FILE_SIZE]

        if len(sendable) == 1 and not oversized:
            p = sendable[0]
            if p.suffix.lower() in IMAGE_EXTS:
                async def _send():
                    with open(p, 'rb') as f:
                        return await update.message.reply_photo(photo=f, caption=video_caption)
                await _retry_on_timeout(_send)
            else:
                meta = get_video_metadata(p)

                need_convert = (
                    meta.get("video_codec") != "h264"
                    or meta.get("pix_fmt") != "yuv420p"
                )

                if need_convert:
                    logger.info(
                        "Converting for Telegram: codec=%s pix_fmt=%s",
                        meta.get("video_codec"),
                        meta.get("pix_fmt"),
                    )
                    new_file = await asyncio.get_running_loop().run_in_executor(
                        None,
                        convert_for_telegram,
                        p,
                        meta.get("duration"),
                    )
                    p = new_file
                    meta = get_video_metadata(p)

                async def _send():
                    with open(p, 'rb') as f:
                        return await update.message.reply_video(
                            video=f, caption=video_caption, supports_streaming=True,
                            width=meta.get('width'), height=meta.get('height'), duration=meta.get('duration'),
                        )
                await _retry_on_timeout(_send)
            if extra_text:
                await update.message.reply_text(extra_text)

        elif sendable:
            # Carousel: send as media group(s), max 10 items per Telegram group
            for chunk_start in range(0, len(sendable), 10):
                chunk = sendable[chunk_start:chunk_start + 10]

                async def _send(chunk=chunk, chunk_start=chunk_start):
                    open_files = []
                    try:
                        media = []
                        for i, p in enumerate(chunk):
                            f = open(p, 'rb')
                            open_files.append(f)
                            item_caption = video_caption if (chunk_start == 0 and i == 0) else None
                            if p.suffix.lower() in IMAGE_EXTS:
                                media.append(InputMediaPhoto(media=f, caption=item_caption))
                            else:
                                meta = get_video_metadata(p)

                                need_convert = (
                                    meta.get("video_codec") != "h264"
                                    or meta.get("pix_fmt") != "yuv420p"
                                )

                                if need_convert:
                                    logger.info(
                                        "Converting for Telegram: codec=%s pix_fmt=%s",
                                        meta.get("video_codec"),
                                        meta.get("pix_fmt"),
                                    )
                                    new_file = await asyncio.get_running_loop().run_in_executor(
                                        None,
                                        convert_for_telegram,
                                        p,
                                        meta.get("duration"),
                                    )
                                    p = new_file
                                    meta = get_video_metadata(p)
                                    f.close()
                                    f = open(p, 'rb')
                                    open_files[-1] = f
                                media.append(InputMediaVideo(
                                    media=f, caption=item_caption, supports_streaming=True,
                                    width=meta.get('width'), height=meta.get('height'), duration=meta.get('duration'),
                                ))
                        return await update.message.reply_media_group(media=media)
                    finally:
                        for f in open_files:
                            f.close()

                await _retry_on_timeout(_send)
            if extra_text:
                await update.message.reply_text(extra_text)
        else:
            await update.message.reply_text("Не удалось скачать. Возможно, ссылка неправильная или доступ ограничен.")

        for p in oversized:
            try:
                link = upload_to_transfersh(p)
                await update.message.reply_text(
                    f"Файл {p.name} слишком большой для отправки в Telegram. Вот ссылка для скачивания: {link}"
                )
            except Exception as exc:
                logger.exception("Upload failed: %s", exc)
                await update.message.reply_text(f"Не удалось загрузить файл {p.name} на внешний сервис (он слишком большой).")
                await send_error_to_admin(context, f"Ошибка загрузки файла {p.name}: {exc}")
    except TimedOut as exc:
        logger.exception("Sending to Telegram timed out after retries: %s", exc)
        await update.message.reply_text(
            "Файл скачался, но не получилось отправить его из-за таймаута сети. Попробуйте ещё раз."
        )
        await send_error_to_admin(context, f"sendVideo/sendPhoto TimedOut для пользователя {user_id}: {exc}")
    finally:
        if file_paths:
            shutil.rmtree(file_paths[0].parent, ignore_errors=True)


telegram_app.add_error_handler(on_error)
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(on_callback_query))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


# ---------------------------------------------------------------------------
# Web server: webhook + health check (for uptime pings, so Render's free
# tier doesn't spin down from inactivity)
# ---------------------------------------------------------------------------

async def telegram_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    update = Update.de_json(data, telegram_app.bot)
    # Ack Telegram immediately — downloading/transcoding can take way longer
    # than Telegram's webhook timeout, and processing inline here risked
    # Telegram re-delivering the same update (looking like duplicate/mixed-up
    # sends when several links come in quickly).
    asyncio.create_task(telegram_app.process_update(update))
    return web.Response(text="OK")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def on_startup(app: web.Application) -> None:
    await telegram_app.initialize()
    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info("Webhook set to %s", webhook_url)
    await telegram_app.start()


async def on_cleanup(app: web.Application) -> None:
    await telegram_app.stop()
    await telegram_app.shutdown()


def run() -> None:
    port = int(os.environ.get("PORT", 8080))
    web_app = web.Application()
    web_app.router.add_get('/', health)
    web_app.router.add_get('/health', health)
    web_app.router.add_post('/webhook', telegram_webhook)
    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)
    web.run_app(web_app, host='0.0.0.0', port=port)


if __name__ == '__main__':
    run()
