# ╔══════════════════════════════════════════════╗
# ║             OpusMusic Bot                  ║
# ║      Advanced Telegram Music System         ║
# ╚══════════════════════════════════════════════╝
#
#  Feature: AutoPlay
#  Queue khatam hone par related song automatically play kare.
#
#  /autoplay on  → enable
#  /autoplay off → disable
#
#  Flow (naya): gaana khatam -> agla related gaana PEHLE queue me daalte hain
#  -> phir original play_next usse normal tareeke se chalata hai.
#  Isse bot VC chhodke dobara join nahi karta (gap/flicker nahi).

import random
import re
import types as _types
from datetime import datetime

from py_yt import VideosSearch
from pyrogram import filters, types

from opus import anon, app, config, db, lang, logger, queue, yt
from opus.helpers import can_manage_vc

MAX_SONG_SEC = 600      # autoplay me 10 min se lambe video (jukebox/mix) skip
MIN_SONG_SEC = 60
MAX_FAIL_STREAK = 3     # lagatar itne autoplay gaane play na hon to ruk jao
MAX_USED_IDS = 300


# ── MongoDB helpers ──────────────────────────────────────────────────────────

_autoplay_cache: dict[int, bool] = {}


async def is_autoplay(chat_id: int) -> bool:
    if chat_id in _autoplay_cache:
        return _autoplay_cache[chat_id]
    doc = await db.db.autoplay.find_one({"_id": chat_id})
    state = bool(doc.get("enabled", False)) if doc else False
    _autoplay_cache[chat_id] = state
    return state


async def set_autoplay(chat_id: int, enabled: bool) -> None:
    _autoplay_cache[chat_id] = enabled
    await db.db.autoplay.update_one(
        {"_id": chat_id}, {"$set": {"enabled": enabled}}, upsert=True,
    )


# ── Track memory ─────────────────────────────────────────────────────────────

_last: dict[int, dict] = {}        # chat_id -> {"title", "channel", "id"}
_used_ids: dict[int, set] = {}
_fail_streak: dict[int, int] = {}

_NOISE = re.compile(
    r"\(.*?\)|\[.*?\]|official|video|audio|lyrics?|lyrical|full song|"
    r"hd|4k|remix|song|music|feat\.?|ft\.?",
    re.IGNORECASE,
)


def _clean(title: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", title or "")).strip()


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", "", (text or "").lower())


def remember_track(chat_id: int, track) -> None:
    """Jo gaana khatam hua use yaad rakho (agla gaana isi par based hoga)."""
    if not track:
        return
    used = _used_ids.setdefault(chat_id, set())
    if len(used) > MAX_USED_IDS:
        used.clear()
    if getattr(track, "id", None):
        used.add(track.id)
    _last[chat_id] = {
        "title": getattr(track, "title", "") or "",
        "channel": getattr(track, "channel_name", "") or "",
        "id": getattr(track, "id", ""),
    }


def _build_queries(last: dict) -> list[str]:
    year = datetime.now().year
    title = _clean(last.get("title", ""))
    channel = (last.get("channel") or "").replace("VEVO", "").replace("- Topic", "").strip()

    queries = []
    if channel:
        queries.append(f"{channel} songs")           # same artist/label ka vibe
    if title:
        queries.append(f"songs like {title}")
        queries.append(f"{title} similar songs")
    genre = [
        f"latest hindi songs {year}",
        "best bollywood songs",
        "hindi hit songs playlist",
        "popular hindi songs",
    ]
    queries.append(random.choice(genre))
    return queries


async def find_related(chat_id: int):
    """Related Track dhoondo (None = nahi mila). Download yahan nahi hota."""
    last = _last.get(chat_id)
    if not last:
        return None

    used = _used_ids.setdefault(chat_id, set())
    last_norm = _norm(_clean(last.get("title", "")))
    limit = min(config.DURATION_LIMIT, MAX_SONG_SEC)

    for query in _build_queries(last):
        try:
            results = (await VideosSearch(query, limit=20).next()).get("result", [])
        except Exception as e:
            logger.error(f"AutoPlay search error: {e}")
            continue

        random.shuffle(results)
        for data in results:
            track = yt.make_track(data, 0, video=False)
            if not track or track.id in used:
                continue
            if not (MIN_SONG_SEC <= track.duration_sec <= limit):
                continue
            # same gaana (ya uska lyrics/audio version) dobara na aaye
            full_title = _norm(data.get("title", ""))
            if last_norm and len(last_norm) > 4 and last_norm in full_title:
                continue
            return track
    return None


def _preload_active(chat_id: int) -> bool:
    """Preloaded mode chal raha ho to wahi agla gaana deta hai, autoplay nahi."""
    try:
        from opus.plugins import preload
        return chat_id in preload._state
    except Exception:
        return False


# ── Patch anon.play_next ─────────────────────────────────────────────────────

_original_play_next = anon.play_next.__func__


async def _patched_play_next(self, chat_id: int) -> None:
    current = queue.get_current(chat_id)

    # Jo gaana abhi khatam/skip hua: sach me play hua tha? (media.time=1 tab set hota hai)
    if current:
        if getattr(current, "time", 0):
            _fail_streak[chat_id] = 0
            remember_track(chat_id, current)
        else:
            _fail_streak[chat_id] = _fail_streak.get(chat_id, 0) + 1
            _used_ids.setdefault(chat_id, set()).add(current.id)

    try:
        needs_autoplay = (
            queue.get_next(chat_id, check=True) is None
            and not await db.get_loop(chat_id)
            and not _preload_active(chat_id)
            and _fail_streak.get(chat_id, 0) < MAX_FAIL_STREAK
            and await is_autoplay(chat_id)
        )
        if needs_autoplay:
            await _queue_related(chat_id)
    except Exception as e:
        logger.error(f"AutoPlay error in {chat_id}: {e}")

    await _original_play_next(self, chat_id)


async def _queue_related(chat_id: int) -> None:
    last = _last.get(chat_id)
    if not last:
        return

    _lang = await lang.get_lang(chat_id)
    msg = None
    try:
        msg = await app.send_message(
            chat_id=chat_id,
            text=_lang["autoplay_searching"].format(last["title"] or "..."),
        )
    except Exception:
        pass

    track = await find_related(chat_id)

    if not track:
        if msg:
            try:
                await msg.edit_text(_lang["autoplay_not_found"])
            except Exception:
                pass
        return

    track.user = app.name
    queue.add(chat_id, track)
    _used_ids.setdefault(chat_id, set()).add(track.id)

    if msg:  # original play_next apna "playing next" message khud bhejta hai
        try:
            await msg.delete()
        except Exception:
            pass


anon.play_next = _types.MethodType(_patched_play_next, anon)


# ── /autoplay command ─────────────────────────────────────────────────────────

@app.on_message(
    filters.command(["autoplay", "ap"])
    & filters.group
    & ~app.bl_users
)
@lang.language()
@can_manage_vc
async def autoplay_cmd(_, m: types.Message):
    chat_id = m.chat.id

    if len(m.command) < 2:
        state = await is_autoplay(chat_id)
        key = "autoplay_on" if state else "autoplay_off"
        return await m.reply_text(m.lang["autoplay_status"].format(m.lang[key]))

    sub = m.command[1].lower()

    if sub in ("on", "enable", "1", "true"):
        await set_autoplay(chat_id, True)
        return await m.reply_text(m.lang["autoplay_enabled"])

    if sub in ("off", "disable", "0", "false"):
        await set_autoplay(chat_id, False)
        return await m.reply_text(m.lang["autoplay_disabled"])

    return await m.reply_text(m.lang["autoplay_usage"])
