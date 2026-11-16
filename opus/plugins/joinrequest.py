# ╔══════════════════════════════════════════════╗
# ║             OpusMusic Bot                  ║
# ║      Advanced Telegram Music System         ║
# ╚══════════════════════════════════════════════╝

import html
import io

import aiohttp
from pyrogram import enums, filters, types

from opus import app, config, logger
from opus.helpers import rich
from opus.plugins.start import get_start_img
#&&

# ══════════════════════════════════════════════════════════════
# JOIN REQUEST wale message ki photo yahan hard-code karo.
# Seedha image link (.jpg / .png) yahan paste karo, jaise catbox ka link.
# Khali chhoda to START_IMG use hogi.
RQ_IMG_URL = "https://files.catbox.moe/payr5b.png"
# ══════════════════════════════════════════════════════════════

_pending: dict[str, dict] = {}


def _key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def _request_buttons(chat_id: int, user_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([
        [
            types.InlineKeyboardButton(
                text="✅ 𝐀𝖼𝖼𝖾𝗉𝗍", callback_data=f"jreq accept {chat_id} {user_id}",
                style=enums.ButtonStyle.SUCCESS,
            ),
            types.InlineKeyboardButton(
                text="❌ 𝐑𝖾𝗃𝖾𝖼𝗍", callback_data=f"jreq reject {chat_id} {user_id}",
                style=enums.ButtonStyle.DANGER,
            ),
        ],
    ])


def _joinrequest_buttons() -> types.InlineKeyboardMarkup:
    rows = [
        [
            types.InlineKeyboardButton(
                text="➕ 𝐀𝖽𝖽 𝐌𝖾",
                url=f"https://t.me/{app.username}?startgroup=true",
                style=enums.ButtonStyle.SUCCESS,
            )
        ],
        [
            types.InlineKeyboardButton(text="ꜱᴜᴘᴘᴏʀᴛ", url=config.SUPPORT_CHAT,
                                       style=enums.ButtonStyle.SUCCESS),
            types.InlineKeyboardButton(text="ᴜᴘᴅᴀᴛᴇ", url=config.SUPPORT_CHANNEL,
                                       style=enums.ButtonStyle.DANGER),
        ],
    ]
    if config.SURPRISE_URL:
        rows.append([
            types.InlineKeyboardButton(
                text="🎁 𝐓𝖺𝗉 𝐓𝗈 𝐂𝗅𝖺𝗂𝗆 𝐘𝗈𝗎𝗋 𝐘𝗈𝗎𝗋 𝐒𝖾𝗋𝗉𝗋𝗂𝗌𝖾", url=config.SURPRISE_URL,
                style=enums.ButtonStyle.SUCCESS,
            )
        ])
    return types.InlineKeyboardMarkup(rows)


# -- Request aate hi user ko welcome DM ---------------------------------------
# Telegram join request bhejne wale user ko bot sirf thodi der (kuch minute)
# DM kar sakta hai, isliye ye message request aate hi bhejna zaroori hai.

_img_cache: dict[str, bytes] = {}


async def _fetch_img(url: str):
    """Telegram URL se photo na utha paye (WEBPAGE_CURL_FAILED) to bot khud download karta hai."""
    data = _img_cache.get(url)
    if data is None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return None
                    data = await r.read()
        except Exception as e:
            logger.warning("[JoinReq] image download fail: %r", e)
            return None
        if not data or len(data) > 10 * 1024 * 1024:
            return None
        _img_cache[url] = data
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    bio = io.BytesIO(data)
    bio.name = f"start.{ext if ext in ('jpg', 'jpeg', 'png', 'webp') else 'jpg'}"
    return bio


async def _send_welcome(target_id: int, first_name: str, chat_title: str) -> None:
    name = html.escape(first_name or "ᴜꜱᴇʀ")
    title = html.escape(chat_title)
    text = (
        f"<blockquote>💐 ʜᴇʏ {name} 👋</blockquote>\n"
        f"<blockquote>🏵 ᴛʜᴀɴᴋ ʏᴏᴜ ꜰᴏʀ ʀᴇǫᴜᴇꜱᴛɪɴɢ <b>{title}</b>\n\n"
        f"ɪ'ᴍ {html.escape(app.name)} - ᴀ ʜɪɢʜ ǫᴜᴀʟɪᴛʏ ᴍᴜꜱɪᴄ ꜱᴛʀᴇᴀᴍɪɴɢ ʙᴏᴛ "
        f"ꜰᴏʀ ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘꜱ & ᴄʜᴀɴɴᴇʟꜱ 🚀\n\n"
        f"ᴊᴜꜱᴛ ꜱᴇɴᴅ /start ᴛᴏ ꜱᴇᴇ ʙᴏᴛ ᴍᴇɴᴜ ᴀɴᴅ ᴄᴏᴍᴍᴀɴᴅꜱ 📋</blockquote>"
    )
    markup = _joinrequest_buttons()
    img = RQ_IMG_URL.strip() or get_start_img()

    # 1) Rich message (start msg jaisa: photo + quote + rich buttons)
    #    pehle URL se, phir bot ke khud download kiye photo se, phir bina photo ke
    #    (buttons rich hi rehte hain)
    for attempt in range(3):
        try:
            if attempt == 0:
                photo = img
            elif attempt == 1:
                if not img:
                    continue
                photo = await _fetch_img(img)
                if photo is None:
                    continue
            else:
                photo = None
            if await rich.send_menu(target_id, photo, text, markup):
                return
        except Exception as e:
            logger.warning("[JoinReq] rich menu fail (try %s): %r", attempt + 1, e)

    # 2) Classic: photo (URL, phir bot ka khud ka download)
    if img:
        try:
            await app.send_photo(target_id, img, caption=text, reply_markup=markup)
            return
        except Exception as e:
            logger.warning("[JoinReq] photo(url) fail: %r", e)
        data = await _fetch_img(img)
        if data:
            try:
                await app.send_photo(target_id, data, caption=text, reply_markup=markup)
                return
            except Exception as e:
                logger.warning("[JoinReq] photo(file) fail: %r", e)

    # 3) sirf text
    try:
        await app.send_message(target_id, text, reply_markup=markup)
    except Exception as e:
        logger.warning("[JoinReq] welcome DM fail (%s): %r", target_id, e)


# -- Naya join request aane par -> sabhi owners ko DM -------------------------

@app.on_chat_join_request()
async def joinrequest_notify_owner(_, request: types.ChatJoinRequest):
    try:
        user = request.from_user
        if not user or user.is_bot:
            return

        chat = request.chat
        chat_title = chat.title or "ɢʀᴏᴜᴘ"
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        username = f"@{user.username}" if user.username else "ɴᴏɴᴇ"

        text = (
            f"📥 <b>ɴᴇᴡ ᴊᴏɪɴ ʀᴇǫᴜᴇsᴛ</b>\n\n"
            f"👤 ɴᴀᴍᴇ: {full_name}\n"
            f"🆔 ᴜsᴇʀ ɪᴅ: <code>{user.id}</code>\n"
            f"🔗 ᴜsᴇʀɴᴀᴍᴇ: {username}\n"
            f"💬 ᴄʜᴀᴛ: {chat_title} (<code>{chat.id}</code>)\n\n"
            f"ᴀᴄᴄᴇᴘᴛ ᴏʀ ʀᴇᴊᴇᴄᴛ ᴛʜɪs ʀᴇǫᴜᴇsᴛ?"
        )

        # User ko turant welcome DM (request aate hi)
        await _send_welcome(
            getattr(request, "user_chat_id", None) or user.id,
            user.first_name, chat_title,
        )

        key = _key(chat.id, user.id)
        _pending[key] = {
            "messages": {},  # owner_id -> message_id
            "chat_title": chat_title,
            "full_name": full_name,
            "handled": False,
        }

        for owner_id in config.OWNER_ID:
            try:
                markup = _request_buttons(chat.id, user.id)
                sent = await rich.send_menu(owner_id, None, text, markup)
                if sent is None:
                    sent = await app.send_message(
                        chat_id=owner_id, text=text, reply_markup=markup
                    )
                _pending[key]["messages"][owner_id] = sent.id
            except Exception:
                # Owner ne bot ko start nahi kiya ya DM band hai - skip.
                continue

    except Exception:
        pass


# -- Owner ke Approve/Reject dabane par ---------------------------------------

@app.on_callback_query(filters.regex("^jreq ") & ~app.bl_users)
async def joinrequest_decision(_, query: types.CallbackQuery):
    try:
        _, action, chat_id, user_id = query.data.split()
        chat_id, user_id = int(chat_id), int(user_id)
    except Exception:
        return await query.answer()

    if query.from_user.id not in config.OWNER_ID:
        return await query.answer("ʏᴇ ʙᴜᴛᴛᴏɴ ꜱɪʀꜰ ᴏᴡɴᴇʀ ᴋᴇ ʟɪʏᴇ ʜᴀɪ.", show_alert=True)

    key = _key(chat_id, user_id)
    entry = _pending.get(key)

    if not entry or entry["handled"]:
        try:
            _t = "⚠️ ʏᴇ ʀᴇǫᴜᴇsᴛ ᴘᴀʜᴀʟᴇ ʜɪ ᴋɪꜱɪ ᴀᴜʀ ᴏᴡɴᴇʀ ɴᴇ ʜᴀɴᴅʟᴇ ᴋᴀʀ ᴅɪ ʜᴀɪ."
            if not await rich.finish_menu(query.message.chat.id, query.message.id, _t):
                await query.edit_message_text(_t)
        except Exception:
            pass
        return await query.answer()

    entry["handled"] = True
    decided_by = query.from_user.mention

    if action == "accept":
        try:
            await app.approve_chat_join_request(chat_id, user_id)
        except Exception:
            for owner_id, msg_id in entry["messages"].items():
                try:
                    _t = "❌ ʀᴇǫᴜᴇsᴛ ᴀᴄᴄᴇᴘᴛ ɴᴀʜɪ ʜᴏ ᴘᴀᴀʏɪ (ʙᴏᴛ ᴋᴇ ᴘᴀᴀꜱ ᴘᴇʀᴍɪꜱꜱɪᴏɴ ɴᴀʜɪ ʜᴀɪ)."
                    if not await rich.finish_menu(owner_id, msg_id, _t):
                        await app.edit_message_text(owner_id, msg_id, _t)
                except Exception:
                    pass
            _pending.pop(key, None)
            return await query.answer()

        result_text = f"✅ ʀᴇǫᴜᴇsᴛ ᴀᴄᴄᴇᴘᴛ ᴋᴀʀ ᴅɪ ɢᴀʏɪ ʙʏ {decided_by}"
    else:
        try:
            await app.decline_chat_join_request(chat_id, user_id)
        except Exception:
            pass
        result_text = f"❌ ʀᴇǫᴜᴇsᴛ ʀᴇᴊᴇᴄᴛ ᴋᴀʀ ᴅɪ ɢᴀʏɪ ʙʏ {decided_by}"

    for owner_id, msg_id in entry["messages"].items():
        try:
            if not await rich.finish_menu(owner_id, msg_id, result_text):
                await app.edit_message_text(owner_id, msg_id, result_text)
        except Exception:
            pass

    _pending.pop(key, None)
    await query.answer()

