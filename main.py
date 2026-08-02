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
import json
import re
import shutil
import subprocess
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
from telegram.error import TelegramError, TimedOut
from telegram.request import HTTPXRequest

import yt_dlp
import requests
from aiohttp import web


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


async def _retry_on_timeout(send_coro_factory, attempts: int = 3, delay: float = 3.0):
    """Call an async factory (which performs one send attempt, e.g. opening
    files fresh and calling reply_video) up to `attempts` times, retrying on
    Telegram TimedOut errors — these have been showing up as transient
    network blips on Render's free tier rather than permanent failures."""
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
    cookie_count = 0
    if cookies_path.exists():
        try:
            jar = MozillaCookieJar(str(cookies_path))
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
            cookie_count = len(jar)
        except Exception as exc:
            logger.warning("Failed to load cookies.txt for photo fallback: %s", exc)
    else:
        logger.warning("cookies.txt not found at %s", cookies_path)
    logger.info("Photo fallback session: cookies_loaded=%d", cookie_count)
    return session


def download_instagram_photo_fallback(url: str) -> tuple[list[Path], str | None]:
    """Fallback for single Instagram photo posts.

    yt-dlp's Instagram extractor is primarily built for video content and can
    raise 'No video formats found!' for photo-only posts. This scrapes the
    Open Graph image/description tags directly from the post page instead.
    """
    session = _requests_session_with_cookies()
    # Instagram serves a JS app shell (no Open Graph meta tags in the initial
    # HTML) to regular browser User-Agents, but still server-renders full
    # og: tags for social-media crawlers (so link previews work in
    # Messenger/Twitter/Discord etc.) — spoof that instead of a browser UA.
    crawler_headers = {
        'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    }
    resp = session.get(url, timeout=20, headers=crawler_headers)
    resp.raise_for_status()
    page = resp.text
    logger.info(
        "Photo fallback page fetched: status=%s final_url=%s len=%d",
        resp.status_code,
        resp.url,
        len(page),
    )

    img_match = (
        re.search(r'<meta property="og:image" content="([^"]+)"', page)
        or re.search(r'<meta content="([^"]+)" property="og:image"', page)
        or re.search(r'"display_url"\s*:\s*"([^"]+)"', page)
    )
    if not img_match:
        # Retry once with a regular browser UA (+ cookies) in case the
        # crawler UA got a stripped-down or blocked response instead.
        logger.warning("Crawler-UA fetch had no og:image, snippet: %r", page[:500])
        resp = session.get(
            url,
            timeout=20,
            headers={
                'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
                'Referer': 'https://www.instagram.com/',
            },
        )
        resp.raise_for_status()
        page = resp.text
        img_match = (
            re.search(r'<meta property="og:image" content="([^"]+)"', page)
            or re.search(r'<meta content="([^"]+)" property="og:image"', page)
            or re.search(r'"display_url"\s*:\s*"([^"]+)"', page)
        )
    if not img_match:
        logger.warning("Photo fallback page snippet: %r", page[:500])
        raise RuntimeError(
            "og:image not found on the post page (it may be private, require login, "
            "or Instagram served a login-wall page to this server's IP)"
        )
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


def get_video_metadata(file_path: Path) -> dict:
    """Probe a video file and return {'width', 'height', 'duration'} (best
    effort — empty dict if ffprobe fails). Also logs a full stream summary,
    to diagnose cases where playback shows a static image with only audio
    playing (usually a video-stream/frame-count problem)."""
    metadata: dict = {}
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'stream=index,codec_type,codec_name,width,height,'
                                  'avg_frame_rate,nb_frames,duration',
                '-of', 'json',
                str(file_path),
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
            if video_stream.get('width') and video_stream.get('height'):
                metadata['width'] = int(video_stream['width'])
                metadata['height'] = int(video_stream['height'])
            if video_stream.get('duration'):
                metadata['duration'] = int(float(video_stream['duration']))
        metadata['has_audio'] = any(s.get('codec_type') == 'audio' for s in streams)
    except Exception as exc:
        logger.warning("ffprobe diagnostic failed for %s: %s", file_path.name, exc)
    return metadata


# Max concurrent transcodes is implicitly capped by MAX_CONCURRENT_DOWNLOADS
# (=1), since this only ever runs inside a download job. Keep resolution and
# encoder settings conservative — Render's free tier has only ~512MB RAM
# total for the whole process, and a full-resolution/quality transcode
# previously OOM-killed the entire bot.
MAX_TRANSCODE_HEIGHT = 720


def ensure_playable_h264(file_path: Path, metadata: dict) -> Path:
    """If the video isn't H.264 (e.g. some Instagram reels are VP9-only,
    which Telegram's client often fails to play), transcode it to H.264/AAC
    at a capped, memory-conservative resolution. Falls back to leaving the
    original file untouched if ffmpeg fails or times out."""
    video_codec = metadata.get('video_codec')
    if video_codec in ('h264', 'avc1'):
        return file_path

    has_audio = metadata.get('has_audio', False)
    width = metadata.get('width')
    height = metadata.get('height')
    logger.info(
        "Transcoding %s (video=%s, has_audio=%s, size=%sx%s) to H.264, capped at %dp",
        file_path.name, video_codec, has_audio, width, height, MAX_TRANSCODE_HEIGHT,
    )

    tmp_path = file_path.with_name(file_path.stem + '_h264.mp4')
    scale_filter = (
        f"scale=-2:'min({MAX_TRANSCODE_HEIGHT},ih)'"
    )
    cmd = [
        'ffmpeg', '-y', '-i', str(file_path),
        '-vf', scale_filter,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
        '-threads', '1',
    ]
    cmd += ['-c:a', 'aac', '-b:a', '96k'] if has_audio else ['-an']
    cmd += ['-movflags', '+faststart', str(tmp_path)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=True)
        file_path.unlink(missing_ok=True)
        tmp_path.rename(file_path)
        logger.info("Transcode OK: %s", file_path.name)
    except Exception as exc:
        logger.warning("Transcode to H.264 failed/skipped for %s: %s", file_path.name, exc)
        tmp_path.unlink(missing_ok=True)
    return file_path


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
        # Format priority:
        #  1) an already-muxed H.264 file with audio (ideal — no merge needed,
        #     and H.264/AAC is what Telegram's player reliably supports)
        #  2) any already-muxed format that at least HAS audio (avoids
        #     picking a video-only 'best', which happened for one post and
        #     produced a silent, sometimes-unplayable VP9 file)
        #  3) merge H.264 video specifically with the best audio
        #  4) merge whatever video+audio streams are available
        #  5) absolute last resort: bare 'best', even if silent/non-H.264
        'format': (
            'best[vcodec^=avc1][acodec!=none]'
            '/best[acodec!=none]'
            '/bestvideo[vcodec^=avc1]+bestaudio'
            '/bestvideo+bestaudio'
            '/best'
        ),
        # Ensure the video's metadata (moov atom) is at the start of the
        # file so Telegram (and other players) can start playback and show
        # a proper video preview instead of treating it as a static file.
        'postprocessor_args': {
            'ffmpeg': ['-movflags', '+faststart'],
        },
        # Some sites require this to embed video and audio
        'merge_output_format': 'mp4',
        # If one item in a carousel can't be extracted (e.g. a photo slide
        # yt-dlp's Instagram extractor fails on), skip just that item instead
        # of aborting the whole post's download.
        'ignoreerrors': True,
        # Only ever download the first item of a carousel/playlist.
        'playlist_items': '1',
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

        if info_dict is None:
            # With ignoreerrors=True, a totally unextractable single post
            # (e.g. a lone photo yt-dlp's Instagram extractor can't handle)
            # comes back as None instead of raising.
            raise RuntimeError("yt-dlp returned no info for this URL")

        entries = info_dict.get('entries')
        if entries:
            entries = [e for e in entries if e]  # drop skipped/failed entries
            # Carousel: the caption usually lives on the playlist-level
            # 'description', but fall back to the first entry's description.
            caption = info_dict.get('description') or next(
                (e.get('description') for e in entries if e.get('description')),
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
        if platform == 'instagram':
            logger.info("yt-dlp failed (%s), trying photo-page fallback for: %s", exc, url)
            try:
                return download_instagram_photo_fallback(url)
            except Exception as fallback_exc:
                logger.warning("Photo fallback also failed: %s", fallback_exc)
        raise


# Limit how many downloads run at the same time. Render's free tier has
# very limited CPU/RAM, and running several yt-dlp/ffmpeg jobs at once was
# likely causing the timeouts under bursts of requests — extra requests now
# simply queue instead of competing for the same scarce resources.
MAX_CONCURRENT_DOWNLOADS = 1
_download_semaphore: asyncio.Semaphore | None = None


def _get_download_semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _download_semaphore


async def download_video(url: str, platform: str) -> tuple[list[Path], str | None]:
    """Download post asynchronously using a thread executor, capped to
    MAX_CONCURRENT_DOWNLOADS concurrent jobs."""
    async with _get_download_semaphore():
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
# Default httpx timeouts (a few seconds) are too short for uploading video
# files to Telegram over Render's connection, causing WriteTimeout/TimedOut
# errors on sendVideo. Give the bot's HTTP client much more room, especially
# for writing (uploading) request bodies.
def _make_bot_request() -> HTTPXRequest:
    return HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=180.0,
        pool_timeout=30.0,
    )


telegram_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .request(_make_bot_request())
    .get_updates_request(_make_bot_request())
    .build()
)


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
    """Handle incoming messages, but only react to ones containing a link."""
    if not update.message or not update.message.text:
        return
    text = update.message.text
    url = extract_url(text)
    if not url:
        # Not a link — let people chat in the group without the bot reacting.
        return
    user_id = update.message.from_user.id
    # Enforce subscription if required
    if not await is_subscribed(user_id, REQUIRED_CHANNEL, telegram_app):
        channel_link = REQUIRED_CHANNEL_LINK or REQUIRED_CHANNEL
        await update.message.reply_text(
            f"Чтобы пользоваться ботом, подпишитесь на канал {channel_link} и попробуйте снова."
        )
        return
    platform = get_platform(url)
    if not platform:
        await update.message.reply_text("Извините, я пока не умею скачивать с этого ресурса. Поддерживаются Instagram, TikTok, YouTube Shorts и VK.")
        return
    try:
        # Download post asynchronously (may return several files for a carousel)
        file_paths, caption = await download_video(url, platform)
    except Exception as exc:
        logger.exception("Download failed: %s", exc)
        if platform == 'instagram' and 'No video formats found' in str(exc):
            await update.message.reply_text(
                "Похоже, это фото-пост Instagram — сейчас такие посты (без видео) "
                "скачать не получается из-за ограничений на стороне Instagram/yt-dlp. "
                "Видео и рилсы работают нормально."
            )
        else:
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
            if p.suffix.lower() in IMAGE_EXTS:
                async def _send():
                    with open(p, 'rb') as f:
                        return await update.message.reply_photo(photo=f, caption=video_caption)
                await _retry_on_timeout(_send)
            else:
                meta = get_video_metadata(p)
                p = ensure_playable_h264(p, meta)
                meta = get_video_metadata(p)

                async def _send():
                    with open(p, 'rb') as f:
                        return await update.message.reply_video(
                            video=f,
                            caption=video_caption,
                            supports_streaming=True,
                            width=meta.get('width'),
                            height=meta.get('height'),
                            duration=meta.get('duration'),
                        )
                await _retry_on_timeout(_send)
            if extra_text:
                await update.message.reply_text(extra_text)
        elif sendable:
            # Carousel: send as a media group, Telegram allows max 10 items per group
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
                                p = ensure_playable_h264(p, meta)
                                meta = get_video_metadata(p)
                                f.close()
                                f = open(p, 'rb')
                                open_files[-1] = f
                                media.append(InputMediaVideo(
                                    media=f,
                                    caption=item_caption,
                                    supports_streaming=True,
                                    width=meta.get('width'),
                                    height=meta.get('height'),
                                    duration=meta.get('duration'),
                                ))
                        return await update.message.reply_media_group(media=media)
                    finally:
                        for f in open_files:
                            f.close()

                await _retry_on_timeout(_send)
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
    except TimedOut as exc:
        logger.exception("Sending to Telegram timed out after retries: %s", exc)
        await update.message.reply_text(
            "Файл скачался, но не получилось отправить его из-за таймаута сети. Попробуйте ещё раз."
        )
        await send_error_to_admin(context, f"sendVideo/sendPhoto TimedOut для пользователя {user_id}: {exc}")
    finally:
        # Clean up the whole per-request download directory
        if file_paths:
            shutil.rmtree(file_paths[0].parent, ignore_errors=True)


# Регистрация хендлеров (обязательно после определения функций выше)
telegram_app.add_error_handler(on_error)
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(on_callback_query))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


async def telegram_webhook(request: 'web.Request') -> 'web.Response':
    """Receive Telegram updates and hand them to the Application."""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return web.Response(text="OK")


async def health(request: 'web.Request') -> 'web.Response':
    """Trivial 200 OK endpoint for uptime pings (cron-job.org, UptimeRobot,
    etc.) so the free Render instance doesn't spin down from inactivity."""
    return web.Response(text="OK")


async def on_startup(app: 'web.Application') -> None:
    await telegram_app.initialize()
    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info("Webhook set to %s", webhook_url)
    await telegram_app.start()


async def on_cleanup(app: 'web.Application') -> None:
    await telegram_app.stop()
    await telegram_app.shutdown()


def run() -> None:
    """Start our own aiohttp server: it serves /webhook for Telegram updates
    and / + /health for uptime pings, so the free Render instance can be
    kept awake externally without hitting 404s."""
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
