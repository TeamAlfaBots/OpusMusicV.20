import asyncio
import glob
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Union

import aiohttp
import yt_dlp
from py_yt import Playlist, VideosSearch
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message

from opus import config, logger
from opus.core.cache import TursoCache
from opus.helpers import Track, utils

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ── Multi-API config ──────────────────────────────────────────
# Keys code me nahi hain: OpusKey1..3 env me set karo.
# URL ab env se override ho sakta hai: OpusURL1..3 (nahi diya to default).
# Same URL par alag keys isliye hain ki har key ki daily limit alag hoti hai.
DEFAULT_API_URL = "https://opusytapi-5xd4.onrender.com"
API_SLOTS = 3


def _load_apis():
    apis = []
    for i in range(1, API_SLOTS + 1):
        key = os.getenv(f"OpusKey{i}")
        if not key:
            continue
        apis.append(
            {
                "url": os.getenv(f"OpusURL{i}", DEFAULT_API_URL).rstrip("/"),
                "key": key,
                "param": "api_key",
                "endpoint": "/download",
            }
        )
    return apis


APIS = _load_apis()
if not APIS:
    logger.warning("Koi OpusKey set nahi hai, sirf yt-dlp fallback chalega.")

FAIL_THRESHOLD = 3
BLOCK_DURATION = 10 * 60  # 10 minutes
QUOTA_BLOCK_DURATION = 60 * 60  # 429/403 (quota khatam) par 1 ghanta
# Render free par service so jaati hai: wake-up me 30-50s lag sakte hain.
API_CONNECT_TIMEOUT = 20
API_TOTAL_TIMEOUT = 90

DOWNLOAD_DIR = "downloads"
MIN_MEDIA_BYTES = 10_000  # isse chhota = error page/JSON, gaana nahi
AUDIO_EXTS = ("mp3", "m4a", "webm", "opus", "ogg", "aac")
VIDEO_EXTS = ("mp4", "mkv", "webm")


def _looks_like_media(path: str) -> bool:
    """False for tiny files or JSON/HTML error bodies saved by mistake."""
    try:
        if os.path.getsize(path) < MIN_MEDIA_BYTES:
            return False
        with open(path, "rb") as f:
            head = f.read(16).lstrip()
        return not head.startswith((b"{", b"<", b"["))
    except OSError:
        return False


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="

        # Playable: watch / shorts / youtu.be / playlist (koi bhi list id)
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|[A-Za-z0-9_-]{12,})"
        )
        # YouTube ke woh links jo gaana nahi hain (channel, search, feed...)
        self.iregex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?youtube\.com/"
            r"(channel/|c/|user/|@|results\?|feed/|clip/|live/|embed/|hashtag/|post/|community)"
        )

        self.cookies = []
        self.checked = False
        self.cookie_dir = "opus/cookies"
        self._api_index = 0
        self.cache = TursoCache()
        self._locks: dict[str, asyncio.Lock] = {}

        # Circuit breaker
        self._fail_count = {self._api_id(api): 0 for api in APIS}
        self._blocked_until = {self._api_id(api): 0 for api in APIS}

    # ── helpers ───────────────────────────────────────────────

    def _api_id(self, api: dict) -> str:
        return f"{api['url']}::{api['key']}"

    def _api_label(self, api: dict) -> str:
        """Log ke liye safe label — key kabhi log me nahi aati."""
        return f"{api['url']} #{APIS.index(api) + 1}"

    def valid(self, url: str) -> bool:
        """Playable YouTube video/playlist link?"""
        return bool(url and self.regex.search(url))

    def invalid(self, url: str) -> bool:
        """YouTube ka link hai par gaana nahi (channel, search, feed...)."""
        return bool(url and self.iregex.search(url))

    def log_version(self) -> None:
        """Boot par yt-dlp version log karo aur purana ho to warn karo."""
        try:
            from yt_dlp.version import __version__ as v

            parts = [int(p) for p in v.split(".")[:3]]
            age = (datetime.now(timezone.utc) - datetime(*parts, tzinfo=timezone.utc)).days
            if age > 45:
                logger.warning(
                    f"yt-dlp {v} {age} din purana hai! "
                    f"`uv lock --upgrade-package yt-dlp` chalao, warna YouTube download tootega."
                )
            else:
                logger.info(f"yt-dlp version: {v}")
        except Exception as e:
            logger.warning(f"yt-dlp version check fail: {e}")

    # ── cookies ───────────────────────────────────────────────

    @staticmethod
    def _is_netscape(path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline()
            return first.startswith("# Netscape") or first.startswith("# HTTP Cookie")
        except Exception:
            return False

    def get_cookies(self):
        """Sirf valid Netscape cookie files load karta hai."""
        if not self.checked:
            self.cookies = []
            if os.path.exists(self.cookie_dir):
                for file in os.listdir(self.cookie_dir):
                    if not file.endswith(".txt"):
                        continue
                    path = f"{self.cookie_dir}/{file}"
                    if self._is_netscape(path):
                        self.cookies.append(path)
                    else:
                        logger.warning(f"Skipping invalid cookie file: {path}")
            self.checked = True
        return random.choice(self.cookies) if self.cookies else None

    async def save_cookies(self, urls: list) -> None:
        """COOKIES_URL (batbin.me) se cookie files download karke save karo."""
        os.makedirs(self.cookie_dir, exist_ok=True)
        logger.info("Saving cookies from urls...")
        saved = 0
        try:
            async with aiohttp.ClientSession() as session:
                for i, url in enumerate(urls):
                    path = f"{self.cookie_dir}/cookie_{i}.txt"
                    if "batbin.me" in url:
                        link = "https://batbin.me/raw/" + url.rstrip("/").split("/")[-1]
                    else:
                        link = url  # catbox / koi bhi direct raw .txt link
                    try:
                        body = None
                        headers = {
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36"
                            ),
                            "Accept": "text/plain,*/*",
                        }
                        for attempt in range(3):
                            try:
                                async with session.get(
                                    link,
                                    headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=20),
                                ) as resp:
                                    if resp.status != 200:
                                        logger.warning(
                                            f"Cookie url {i} status {resp.status}"
                                        )
                                        break
                                    body = await resp.read()
                                    break
                            except Exception as e:
                                logger.warning(
                                    f"Cookie url {i} attempt {attempt + 1}/3: {e}"
                                )
                                await asyncio.sleep(2)
                        if body is None:
                            continue
                        with open(path, "wb") as fw:
                            fw.write(body)
                        if self._is_netscape(path):
                            saved += 1
                        else:
                            logger.warning(f"Cookie url {i}: Netscape format nahi hai")
                            _safe_remove(path)
                    except Exception as e:
                        logger.warning(f"Cookie url {i} error: {e}")
        finally:
            self.checked = False  # dobara scan hone do
        logger.info(f"Cookies saved: {saved}/{len(urls)}")

    # ── circuit breaker ───────────────────────────────────────

    def _is_blocked(self, api: dict) -> bool:
        aid = self._api_id(api)
        now = time.time()

        if now < self._blocked_until[aid]:
            remaining = int((self._blocked_until[aid] - now) / 60) + 1
            logger.info(f"[{self._api_label(api)}] Blocked! ~{remaining}m remaining.")
            return True

        if self._fail_count[aid] >= FAIL_THRESHOLD:
            logger.info(f"[{self._api_label(api)}] Block khatam, phir se try karega.")
            self._fail_count[aid] = 0
        return False

    def _mark_fail(self, api: dict, duration: int = BLOCK_DURATION, instant: bool = False):
        aid = self._api_id(api)
        self._fail_count[aid] = FAIL_THRESHOLD if instant else self._fail_count[aid] + 1
        logger.warning(
            f"[{self._api_label(api)}] Fail count: {self._fail_count[aid]}/{FAIL_THRESHOLD}"
        )
        if self._fail_count[aid] >= FAIL_THRESHOLD:
            self._blocked_until[aid] = time.time() + duration
            logger.warning(f"[{self._api_label(api)}] Blocked for {duration // 60} min!")

    def _mark_success(self, api: dict):
        aid = self._api_id(api)
        self._fail_count[aid] = 0
        self._blocked_until[aid] = 0

    # ── search / playlist ─────────────────────────────────────

    async def exists(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    def make_track(self, data: dict, m_id: int = 0, video: bool = False, user=None):
        """py_yt result dict -> Track (None agar id nahi). Search/playlist/autoplay teeno use karte hain."""
        vid = data.get("id")
        if not vid:
            return None
        dur = data.get("duration")
        try:
            sec = utils.to_seconds(dur) if dur else 0
        except Exception:
            sec = 0
        thumbs = data.get("thumbnails") or [{}]
        link = (data.get("link") or self.base + vid).split("&list=")[0]
        return Track(
            id=vid,
            channel_name=(data.get("channel") or {}).get("name"),
            duration=dur or "00:00",
            duration_sec=sec,
            message_id=m_id,
            title=(data.get("title") or "Unknown")[:25],
            thumbnail=(thumbs[-1].get("url") or "").split("?")[0],
            url=link,
            user=user,
            view_count=(data.get("viewCount") or {}).get("short"),
            video=video,
        )

    async def search(self, query: str, m_id: int, video=False):
        try:
            results = await VideosSearch(query, limit=1).next()
        except Exception as e:
            logger.error(f"Search Error: {e}")
            return None

        if results and results.get("result"):
            return self.make_track(results["result"][0], m_id, video)
        return None

    async def playlist(self, limit, user, url, video):
        tracks = []
        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                track = self.make_track(data, 0, video, user)
                if track:
                    track.view_count = ""
                    tracks.append(track)
        except Exception as e:
            logger.error(f"Playlist Error: {e}")
        return tracks

    async def url(self, message_1: Message):
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)

        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        return text[entity.offset: entity.offset + entity.length]
        return None

    # ── downloaded file lookup ────────────────────────────────

    def find_downloaded(self, video_id: str, video: bool = False):
        """downloads/ me pehle se maujood valid file (kisi bhi ext me)."""
        exts = VIDEO_EXTS if video else AUDIO_EXTS
        for ext in exts:
            path = f"{DOWNLOAD_DIR}/{video_id}.{ext}"
            if os.path.exists(path) and _looks_like_media(path):
                return path
        return None

    # ── API download ──────────────────────────────────────────

    async def _try_single_api(self, api: dict, video_id: str, video: bool):
        tmp = None
        try:
            ext = "mp4" if video else "mp3"
            filename = f"{DOWNLOAD_DIR}/{video_id}.{ext}"
            tmp = filename + ".part"
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)

            params = {
                "url": self.base + video_id,
                "type": "video" if video else "audio",
            }
            if api["key"] and api["param"]:
                params[api["param"]] = api["key"]

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{api['url']}{api['endpoint']}",
                    params=params,
                    timeout=aiohttp.ClientTimeout(
                        total=API_TOTAL_TIMEOUT, connect=API_CONNECT_TIMEOUT
                    ),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[{self._api_label(api)}] Status: {resp.status}")
                        if resp.status in (401, 403, 429):
                            # key/quota ka masla — retry bekaar, seedha lamba block
                            self._mark_fail(api, QUOTA_BLOCK_DURATION, instant=True)
                        else:
                            self._mark_fail(api)
                        return None

                    with open(tmp, "wb") as f:
                        async for chunk in resp.content.iter_chunked(131072):
                            f.write(chunk)

            if _looks_like_media(tmp):
                os.replace(tmp, filename)  # atomic: adhoori file kabhi play nahi hogi
                self._mark_success(api)
                logger.info(f"[{self._api_label(api)}] Download success ✓")
                return filename

            logger.warning(f"[{self._api_label(api)}] Response media nahi hai (empty/JSON/HTML)!")
            _safe_remove(tmp)
            self._mark_fail(api)

        except Exception as e:
            err = str(e).replace(api["key"], "***")
            logger.warning(f"[{self._api_label(api)}] Error: {err}")
            if tmp:
                _safe_remove(tmp)
            self._mark_fail(api)
        return None

    async def api_download(self, video_id: str, video: bool = False):
        available = [api for api in APIS if not self._is_blocked(api)]
        if not available:
            logger.error("Saari APIs blocked/missing! yt-dlp fallback.")
            return None

        start = self._api_index % len(available)
        ordered = available[start:] + available[:start]
        self._api_index += 1

        for api in ordered:
            logger.info(
                f"Trying: {self._api_label(api)} "
                f"(fails: {self._fail_count[self._api_id(api)]}/{FAIL_THRESHOLD})"
            )
            result = await self._try_single_api(api, video_id, video)
            if result:
                return result
        return None

    # ── yt-dlp fallback ───────────────────────────────────────

    def _ytdlp_opts(self, video: bool, cookie):
        opts = {
            "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 20,
            "geo_bypass": True,
            "overwrites": False,
            "match_filter": yt_dlp.utils.match_filter_func(
                f"duration < {config.DURATION_LIMIT}"
            ),
        }
        if video:
            # 720p tak — chhota file, Telegram VC me HD_720p hi chalta hai
            opts["format"] = (
                "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            )
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        if cookie:
            opts["cookiefile"] = cookie
        return opts

    def _ytdlp_run(self, video_id: str, video: bool, cookie):
        try:
            with yt_dlp.YoutubeDL(self._ytdlp_opts(video, cookie)) as ydl:
                info = ydl.extract_info(self.base + video_id, download=True)
                if not info:
                    return None
                # Asli final path (merge ke baad ext badal sakta hai)
                path = None
                if info.get("requested_downloads"):
                    path = info["requested_downloads"][0].get("filepath")
                path = path or ydl.prepare_filename(info)
        except Exception as e:
            logger.error(f"yt-dlp Error ({'cookie' if cookie else 'no-cookie'}): {e}")
            return None

        if path and os.path.exists(path) and _looks_like_media(path):
            return path
        # Last resort: id se glob
        for p in glob.glob(f"{DOWNLOAD_DIR}/{video_id}.*"):
            if not p.endswith((".part", ".ytdl")) and _looks_like_media(p):
                return p
        return None

    async def ytdlp_download(self, video_id: str, video: bool = False):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        cookie = self.get_cookies()
        # Attempt 1: cookie ke saath. Fail ho to Attempt 2: bina cookie
        # (kharab/expire cookie kabhi kabhi bina cookie se better chalti hai).
        attempts = [cookie, None] if cookie else [None]
        for ck in attempts:
            path = await asyncio.to_thread(self._ytdlp_run, video_id, video, ck)
            if path:
                return path
        return None

    # ── main entry ────────────────────────────────────────────

    async def download(self, video_id: str, video: bool = False, title: str = None):
        # Same id ke do parallel download (play + preload prefetch) ek hi baar chalein
        if len(self._locks) > 300:  # memory ke liye: free locks saaf
            self._locks = {k: l for k, l in self._locks.items() if l.locked()}
        lock = self._locks.setdefault(f"{video_id}:{int(video)}", asyncio.Lock())
        async with lock:
            return await self._download(video_id, video)

    async def _download(self, video_id: str, video: bool):
        # Step 0: disk par pehle se hai?
        existing = self.find_downloaded(video_id, video)
        if existing:
            return existing

        # Step 1: Turso cache
        cached = await self.cache.get(video_id, video)
        if cached and len(cached[1]) < MIN_MEDIA_BYTES:
            logger.warning(f"[TursoCache] Broken cache entry ignore: {video_id}")
            cached = None
        if cached:
            ext, data = cached
            try:
                return await self.cache.write_to_disk(video_id, ext, data, DOWNLOAD_DIR)
            except Exception as e:
                logger.warning(f"[TursoCache] Disk write error: {e}")

        # Step 2: API chain -> yt-dlp
        file = await self.api_download(video_id, video)
        if not file:
            logger.warning("All APIs failed/blocked, falling back to yt-dlp")
            file = await self.ytdlp_download(video_id, video)

        # Step 3: cache me save (background)
        if file:
            ext = file.rsplit(".", 1)[-1] if "." in file else ("mp4" if video else "mp3")
            asyncio.create_task(self._save_to_cache(video_id, file, ext, video))
        else:
            logger.error(f"Download fail (API + yt-dlp dono): {video_id}")
        return file

    async def _save_to_cache(self, video_id: str, filepath: str, ext: str, video: bool = False) -> None:
        try:
            await self.cache.save(video_id, filepath, ext, video)
        except Exception as e:
            logger.warning(f"[TursoCache] Background save error: {e}")
