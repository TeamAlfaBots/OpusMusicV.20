# Copyright (c) 2025 OpusMusic
# Licensed under the MIT License.
# This file is part of OpusMusic


import asyncio
import os

import libsql_client

from opus import config, logger

# ── Turso Cache ────────────────────────────────────────────────
# Downloaded songs (audio/video bytes) yahan cache hote hain taaki same song
# dobara aane par API limit hit na ho. Multiple Turso DBs round-robin me use
# hote hain; jo DB full ho jaye use is session ke liye skip kiya jata hai.
#
# Key format:  audio -> "<video_id>"   (purane entries ke saath compatible)
#              video -> "<video_id>:v"  (pehle audio/video mix ho jaate the)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS songs (
    video_id TEXT PRIMARY KEY,
    ext TEXT NOT NULL,
    data BLOB NOT NULL,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
)
"""

FULL_MARKERS = (
    "quota",
    "storage",
    "database size",
    "disk full",
    "no space",
    "limit exceeded",
    "row limit",
    "database full",
)

# Bade video files cache me daalna bekaar hai (row/quota fail hota hai)
MAX_CACHE_BYTES = 15 * 1024 * 1024
READ_TIMEOUT = 8  # cache kabhi playback ko 8s se zyada na roke


def _key(video_id: str, video: bool) -> str:
    return f"{video_id}:v" if video else video_id


class TursoCache:
    def __init__(self):
        self.databases = getattr(config, "TURSO_DATABASES", [])
        self._full = {i: False for i in range(len(self.databases))}
        self._index = 0
        self._ready = {i: False for i in range(len(self.databases))}
        self._clients: dict[int, object] = {}  # har DB ka ek hi client reuse

    @property
    def enabled(self) -> bool:
        return bool(self.databases)

    def _client(self, i: int):
        client = self._clients.get(i)
        if client is not None:
            return client
        db = self.databases[i]
        # libsql:// = WebSocket (Render jaise hosts par fail hota hai) -> https:// (HTTP) force
        url = db["url"]
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        client = libsql_client.create_client(url=url, auth_token=db["token"])
        self._clients[i] = client
        return client

    async def _drop_client(self, i: int) -> None:
        """Error ke baad client discard karo, agli baar naya banega."""
        client = self._clients.pop(i, None)
        self._ready[i] = False
        if client:
            try:
                await client.close()
            except Exception:
                pass

    async def close(self) -> None:
        for i in list(self._clients):
            await self._drop_client(i)

    async def _ensure_table(self, i: int, client):
        if self._ready.get(i):
            return
        await client.execute(CREATE_TABLE)
        self._ready[i] = True

    def _is_full_error(self, e: Exception) -> bool:
        text = str(e).lower()
        return any(marker in text for marker in FULL_MARKERS)

    # ── Read ───────────────────────────────────────────────────

    async def _read_one(self, i: int, key: str):
        try:
            client = self._client(i)
            await self._ensure_table(i, client)
            result = await client.execute(
                "SELECT ext, data FROM songs WHERE video_id = ?", [key]
            )
            if result.rows:
                ext, data = result.rows[0]
                logger.info(f"[TursoCache DB{i}] Cache hit: {key}")
                return ext, bytes(data)
        except Exception as e:
            logger.warning(f"[TursoCache DB{i}] Read error: {e}")
            await self._drop_client(i)
        return None

    async def get(self, video_id: str, video: bool = False):
        """Saari DBs parallel me check karta hai; pehla hit (ext, data) return."""
        if not self.enabled:
            return None
        key = _key(video_id, video)
        tasks = [
            asyncio.create_task(self._read_one(i, key))
            for i in range(len(self.databases))
        ]
        try:
            for fut in asyncio.as_completed(tasks, timeout=READ_TIMEOUT):
                res = await fut
                if res:
                    return res
        except asyncio.TimeoutError:
            logger.warning("[TursoCache] Read timeout, cache skip.")
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        return None

    # ── Write ──────────────────────────────────────────────────

    async def save(self, video_id: str, filepath: str, ext: str, video: bool = False) -> bool:
        """Round-robin DBs me save. Koi exception bahar nahi jaati."""
        if not self.enabled:
            return False

        try:
            if os.path.getsize(filepath) > MAX_CACHE_BYTES:
                logger.info(f"[TursoCache] Bahut bada file, cache skip: {video_id}")
                return False
            with open(filepath, "rb") as f:
                data = f.read()
        except Exception as e:
            logger.warning(f"[TursoCache] File read error: {e}")
            return False

        available = [i for i in range(len(self.databases)) if not self._full[i]]
        if not available:
            logger.warning("[TursoCache] Saari DBs full hain, save skip.")
            return False

        start = self._index % len(available)
        ordered = available[start:] + available[:start]
        self._index += 1
        key = _key(video_id, video)

        for i in ordered:
            try:
                client = self._client(i)
                await self._ensure_table(i, client)
                await client.execute(
                    "INSERT OR REPLACE INTO songs (video_id, ext, data) VALUES (?, ?, ?)",
                    [key, ext, data],
                )
                logger.info(f"[TursoCache DB{i}] Saved: {key}")
                return True
            except Exception as e:
                if self._is_full_error(e):
                    logger.warning(f"[TursoCache DB{i}] Full! Skipping from now on.")
                    self._full[i] = True
                else:
                    logger.warning(f"[TursoCache DB{i}] Write error: {e}")
                    await self._drop_client(i)

        logger.warning("[TursoCache] Kisi bhi DB me save nahi ho paya.")
        return False

    async def write_to_disk(self, video_id: str, ext: str, data: bytes, download_dir: str) -> str:
        os.makedirs(download_dir, exist_ok=True)
        filename = f"{download_dir}/{video_id}.{ext}"
        tmp = filename + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, filename)
        return filename
