"""
tgtest.py - Render par bot ki jagah chalao, logs me PASS/FAIL dekho.
Koi secret print nahi hota.

Render: Settings -> Docker Command ->  uv run python3 tgtest.py
(test ke baad Docker Command wapas khali kar do)
"""
import asyncio
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def say(msg):
    print(f"[TGTEST] {msg}", flush=True)


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"tgtest ok")

    def log_message(self, *a):
        pass


def _serve():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8000))), _H).serve_forever()


threading.Thread(target=_serve, daemon=True).start()

# Telegram production DCs (port 443)
DCS = {
    "DC1": "149.154.175.53",
    "DC2": "149.154.167.51",
    "DC3": "149.154.175.100",
    "DC4": "149.154.167.91",
    "DC5": "91.108.56.130",
}


async def tcp_test():
    say("--- 1) Telegram servers tak TCP connection ---")
    ok = 0
    for name, ip in DCS.items():
        t0 = time.time()
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, 443), 6)
            w.close()
            say(f"PASS {name} {ip}:443 ({time.time() - t0:.1f}s)")
            ok += 1
        except Exception as e:
            say(f"FAIL {name} {ip}:443 -> {type(e).__name__}")
    say(f"TCP result: {ok}/{len(DCS)} DC pahunch me")
    return ok


async def http_test():
    say("--- 2) Bot API (HTTPS) getMe ---")
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        say("FAIL BOT_TOKEN env nahi mila")
        return
    try:
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
                say(f"{'PASS' if data.get('ok') else 'FAIL'} getMe ok={data.get('ok')} status={r.status}")
    except Exception as e:
        say(f"FAIL getMe -> {type(e).__name__}: {str(e).replace(token, '***')[:120]}")


def crypto_test():
    say("--- 3) tgcrypto (MTProto encryption) ---")
    try:
        import tgcrypto

        key = os.urandom(32)
        iv = os.urandom(32)
        data = os.urandom(1024)
        enc = tgcrypto.ige256_encrypt(data, key, iv)
        dec = tgcrypto.ige256_decrypt(enc, key, iv)
        say(f"{'PASS' if dec == data else 'FAIL'} ige256 encrypt/decrypt roundtrip")
    except Exception as e:
        say(f"FAIL tgcrypto -> {type(e).__name__}: {e}")


async def login_test():
    say("--- 4) Pyrogram bot login (asli test) ---")
    try:
        import pyrogram

        say(f"pyrogram package version: {pyrogram.__version__}")
    except Exception as e:
        say(f"FAIL pyrogram import -> {e}")
        return
    try:
        api_id = int(os.getenv("API_ID", "0"))
        api_hash = os.getenv("API_HASH", "")
        token = os.getenv("BOT_TOKEN", "")
        say(f"API_ID set={bool(api_id)} API_HASH set={bool(api_hash)} (len={len(api_hash)}) BOT_TOKEN set={bool(token)}")
        c = pyrogram.Client(
            "tgtest", api_id=api_id, api_hash=api_hash, bot_token=token, in_memory=True
        )
        t0 = time.time()
        await asyncio.wait_for(c.start(), 90)
        me = await c.get_me()
        say(f"PASS login ho gaya: @{me.username} ({time.time() - t0:.1f}s)")
        await c.stop()
    except asyncio.TimeoutError:
        say("FAIL login: 90s timeout")
    except Exception as e:
        say(f"FAIL login -> {type(e).__name__}: {str(e)[:160]}")


async def main():
    say(f"python {sys.version.split()[0]}")
    ok = await tcp_test()
    await http_test()
    crypto_test()
    await login_test()
    say("=== TEST KHATAM. Upar ke PASS/FAIL ka screenshot bhejo ===")
    while True:  # service live rakho taaki logs padh sako
        await asyncio.sleep(3600)


asyncio.run(main())
