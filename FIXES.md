# Fixes (is update me kya badla)

## Crash fixes
- youtube.py: `valid()`, `invalid()`, `save_cookies()` add kiye (pehle missing the -> link se /play crash).
- __main__.py: `await stop()` (pehle create_task tha, shutdown chalta hi nahi tha). Boot par yt-dlp version log.
- plugins/__init__.py: `all_modules` ab sorted list (frozenset me load order random tha -> autoplay/preload patch order random).
- calls.py `play_next`: None check pehle, error message pehle phir recursion.

## yt-dlp / download
- pyproject: yt-dlp >= 2026.8.19, readme path fix, stray `pyproject` file hata di.
- Dockerfile + setup: `uv lock --upgrade-package yt-dlp` (setup ka `--frozen` hata diya).
- yt-dlp fallback: sahi final filepath, mp4 merge, 720p cap, noplaylist, retries, duration filter,
  cookie fail hone par bina cookie retry, version-age warning.
- API: .part -> atomic rename, 429/403 par 1 ghante ka block, connect timeout 20s (Render cold start),
  URL env se override (OpusURL1..3).
- Per-video lock (play + prefetch same gaana do baar download nahi karte), disk par pehle se file ho to reuse.
- Cache: audio/video alag key, ek client reuse, parallel read (8s timeout), 15MB se bade file skip.
- Search/playlist: None-safe `make_track()`.

## Autoplay (rewrite)
- Agla gaana pehle queue me, phir normal play_next -> bot VC chhodke dobara join nahi karta.
- Artist (channel) + cleaned title se queries, hardcoded "2024" hata, same gaana skip, 10 min cap.
- Download fail par agla candidate, 3 lagatar fail par ruk jata hai.
- Preloaded mode active ho to autoplay beech me nahi aata.

## Preloaded
- Real prefetch: agla gaana current chalte hue download hota hai (gap nahi).
- User ki queue clear nahi hoti (sirf current replace hota hai).
- Playlist badalne / stop par purana prefetch cancel + file cleanup.

## Aapko ek kaam karna hai
`uv.lock` offline regenerate nahi ho saka. Apne system par chalao aur commit karo:
    uv lock --upgrade-package yt-dlp
Aur Render par `OpusKey1..3` ke saath `COOKIES_URL` set karo (cookies.txt abhi khali hai).
