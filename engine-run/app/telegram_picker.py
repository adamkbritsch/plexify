"""Telegram source — drives the @BeatSpotBot FLAC search bot as a Telegram USER session.

Telegram bots can't message other bots, so this connects with a stored Telethon
StringSession (a logged-in user account) created from the user's own api_id/api_hash.

@BeatSpotBot is a TEXT-SEARCH music bot (reverse-engineered live 2026-06-01):
  • You send it a plain "artist title" query — NOT a Spotify link (links are ignored).
  • It replies with a numbered results list. Each entry has a quality badge:
        💽 + 🅁 (U+1F141)  → a FLAC/lossless source (Tidal/Qobuz, ISRC filenames)
        💾 + 🅀 (U+1F140)  → an MP3 source (VK etc.), often covers/remixes
  • Each entry has a keycap button (0⃣1⃣ … 1⃣1⃣). Clicking it makes the bot deliver
    the audio document. The numbered button order matches the text-entry order.

So to get FLAC we parse the listing, pick the entry that best matches the requested
title/artist AND carries the 🅁 badge, click it, and download the audio/x-flac file.
Returns the same shape as the Soulseek adapter so it slots into the picker's chain.

Config (Settings → Telegram source):
  telegram_enabled   '0'|'1'
  telegram_api_id     int   (from my.telegram.org)
  telegram_api_hash   str
  telegram_session    str   (Telethon StringSession — generated once via app.tg_login)
  telegram_bot        str   default '@BeatSpotBot'
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

log = logging.getLogger(__name__)

AUDIO_EXT = (".flac", ".m4a", ".alac", ".wav", ".mp3", ".ogg", ".opus")
FLAC_HINTS = ("flac", "lossless", "hi-res", "hires", "24-bit", "24 bit", "cd")

# Per-entry quality badges in the bot's result text.
FLAC_BADGE = "\U0001F141"   # 🅁  → lossless/FLAC source
MP3_BADGE = "\U0001F140"    # 🅀  → MP3 source
KEYCAP = "⃣"      # combining enclosing keycap, present in 0⃣1⃣ style buttons
# The bot numbers entries TWO ways: keycap digits on the buttons (0⃣1⃣) and CIRCLED digits in
# the message text (①① / ⓪① / ⑫). The text format broke the old keycap-only parser → zero
# entries parsed → nothing ever clicked. Accept both.
_CIRCLED_DIGIT = {"⓪": "0"}                                   # ⓪
for _i in range(1, 10):                                            # ①..⑨  (U+2460..U+2468)
    _CIRCLED_DIGIT[chr(0x2460 + _i - 1)] = str(_i)
_CIRCLED_TEN = {chr(0x2469 + (_n - 10)): _n for _n in range(10, 21)}  # ⑩..⑳ precomposed
# A number token = a run of keycap digits (0⃣1⃣), a run of single circled digits (①①),
# or one precomposed circled number (⑩ ⑪ ⑫ …).
_NUM_RE = re.compile(r"(?:[0-9️]*⃣)+|[⓪①-⑨]+|[⑩-⑳]")
# Title noise that means "not the real track" — penalised during matching.
_BAD_WORDS = ("cover", "remix", "mashup", "mash-up", "mash up", "tribute", "karaoke",
              "instrumental", "nightcore", "sped up", "slowed", "8d audio", "reverb",
              "lo-fi", "lofi", "rework", "bootleg", "flip")


def _cfg():
    try:
        from .db import get_config
    except ImportError:  # allow standalone import (diagnostics)
        from app.db import get_config
    return {
        "enabled": (get_config("telegram_enabled", "0") or "0") == "1",
        "api_id": get_config("telegram_api_id", "") or "",
        "api_hash": get_config("telegram_api_hash", "") or "",
        "session": get_config("telegram_session", "") or "",
        "bot": (get_config("telegram_bot", "@BeatSpotBot") or "@BeatSpotBot"),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["enabled"] and c["api_id"] and c["api_hash"] and c["session"])


# @BeatSpotBot rate-limits heavy users and goes SILENT for a "break period". When that
# happens we must stop hammering it (it just prolongs the break and burns picker cycles).
# A total-silence response trips a persisted cooldown; acquire() short-circuits while active.
_BREAK_SECONDS = 30 * 60


def _in_break() -> float:
    try:
        from .db import get_config
        until = float(get_config("telegram_break_until", "0") or "0")
    except Exception:
        return 0.0
    return until if time.time() < until else 0.0


def _set_break(seconds: int) -> None:
    try:
        from .db import set_config
        set_config("telegram_break_until", str(time.time() + seconds))
    except Exception:
        pass


def _clear_break() -> None:
    try:
        from .db import set_config
        set_config("telegram_break_until", "0")
    except Exception:
        pass


def _run(coro, overall_timeout: float = 120.0):
    # HARD CEILING: a hung telethon await (unresponsive/break bot, stalled MTProto socket) has no
    # per-call timeout and would block the calling picker thread FOREVER. With the scheduler at
    # max_instances=2, just two such hangs deadlock the whole pipeline (every picker_tick skipped).
    # asyncio.wait_for cancels the coroutine once it exceeds overall_timeout, so _run always returns.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        if overall_timeout and overall_timeout > 0:
            return loop.run_until_complete(asyncio.wait_for(coro, overall_timeout))
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


# ── result parsing / ranking ────────────────────────────────────────────────
def _num_of(keycap_text: str) -> int:
    """Decode a number token → int. Handles keycap (0⃣1⃣→1), concatenated circled singles
    (①①→11, ⓪①→1) and precomposed circled tens (⑫→12)."""
    tok = keycap_text or ""
    for ch in tok:                       # precomposed ⑩..⑳ is a single glyph
        if ch in _CIRCLED_TEN:
            return _CIRCLED_TEN[ch]
    s = ""
    for ch in tok:
        if ch.isascii() and ch.isdigit():
            s += ch
        elif ch in _CIRCLED_DIGIT:
            s += _CIRCLED_DIGIT[ch]
    try:
        return int(s) if s else -1
    except ValueError:
        return -1


def _parse_results(text: str):
    """Parse the bot's results text into a list of entries in display order.

    Each entry → {num, title, artist, is_flac, raw}. Entry blocks are delimited by
    the leading keycap number (0⃣1⃣ …)."""
    text = text or ""
    nums = [_num_of(tok) for tok in _NUM_RE.findall(text)]
    blocks = _NUM_RE.split(text)[1:]  # first split chunk precedes entry 1
    entries = []
    for i, blk in enumerate(blocks):
        num = nums[i] if i < len(nums) else (i + 1)
        lines = [ln.strip() for ln in blk.split("\n") if ln.strip()]
        title = lines[0] if lines else ""
        artist = ""
        for ln in lines[1:]:
            if ln.startswith("\U0001F5E3"):  # 🗣 speaker = artist line
                artist = ln.lstrip("\U0001F5E3").strip()
                break
        entries.append({
            "num": num,
            "title": title,
            "artist": artist,
            # Badge-based hints. The bot has SEVERAL listing formats: some show 💽+🅁 (FLAC)
            # vs 💾+🅀 (MP3); others (the 💿 album-track format) show NO quality badge at all.
            # So treat 🅁 as "definitely FLAC" and 🅀-without-🅁 as "definitely MP3"; an entry
            # with neither is UNKNOWN and still a valid candidate (the download step verifies).
            "is_flac": FLAC_BADGE in blk,
            "is_mp3": (MP3_BADGE in blk) and (FLAC_BADGE not in blk),
            "raw": blk,
        })
    return entries


def _score(entry, want_title, want_artist):
    """Higher is better. Title match dominates; artist match assists; covers penalised."""
    try:
        from rapidfuzz import fuzz
        tr = fuzz.token_set_ratio(want_title or "", entry["title"] or "")
        ar = fuzz.token_set_ratio(want_artist or "", entry["artist"] or "") if want_artist else 0
    except Exception:
        t1 = (want_title or "").lower(); t2 = (entry["title"] or "").lower()
        tr = 100 if t1 and t1 in t2 else (50 if t1 and t2 and (t1.split()[0] in t2) else 0)
        ar = 0
    score = tr + 0.4 * ar
    low_title = (entry["title"] or "").lower()
    low_want = (want_title or "").lower()
    for w in _BAD_WORDS:
        if w in low_title and w not in low_want:
            score -= 35
    return score


def _rank(entries, want_title, want_artist, flac_only):
    # Under flac_only, drop ONLY entries we KNOW are MP3 (💾/🅀-without-🅁). Keep badged-FLAC
    # entries AND unbadged ones (the 💿 album-track format carries no badge but is FLAC) — the
    # download step verifies the actual file, so an unbadged non-FLAC is downloaded-then-skipped
    # rather than silently missed (the bug where 💿 results were never clicked).
    # DEMO/LIVE BAN: a (Demo)/(Live) take may not fulfil a clean liked song
    # (consistent with the other sources). Allowed when the liked title is one.
    from .version_match import demo_live_banned as _dlb
    cands = [e for e in entries
             if not (flac_only and e.get("is_mp3"))
             and not _dlb(e.get("title") or "", want_title)]
    # FLAC-badged first, then best title/artist match.
    cands.sort(key=lambda e: (1 if e["is_flac"] else 0, _score(e, want_title, want_artist)), reverse=True)
    # Require a real title/artist match so we click the right song.
    return [e for e in cands if _score(e, want_title, want_artist) >= 55]


def _doc_of(m):
    doc = getattr(m, "document", None) or getattr(m, "audio", None)
    if doc is None:
        return None, None, 0
    mime = (getattr(doc, "mime_type", "") or "")
    fname = None
    for a in (getattr(doc, "attributes", []) or []):
        if getattr(a, "file_name", None):
            fname = a.file_name
            break
    size = getattr(doc, "size", 0) or 0
    return fname, mime, size


async def check_session() -> dict:
    """Verify the configured session is authorized + which account it is. For the
    Settings 'Test' button."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    c = _cfg()
    if not (c["api_id"] and c["api_hash"] and c["session"]):
        return {"ok": False, "error": "missing api_id / api_hash / session"}
    try:
        client = TelegramClient(StringSession(c["session"]), int(c["api_id"]), c["api_hash"])
    except Exception as e:
        return {"ok": False, "error": f"bad credentials: {e}"}
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"ok": False, "error": "session not authorized — re-generate it"}
        me = await client.get_me()
        who = ("@" + me.username) if getattr(me, "username", None) else (getattr(me, "first_name", "") or "user")
        bot_ok = True
        try:
            await client.get_entity(c["bot"])
        except Exception:
            bot_ok = False
        return {"ok": True, "account": who, "bot": c["bot"], "bot_reachable": bot_ok}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _scan_history(client, bot, want_title, want_artist, flac_only, limit=400):
    """Find an ALREADY-DELIVERED audio file for this song in the bot chat history.

    The bot has handed us thousands of FLACs over time; they live in the chat regardless of
    whether the bot is currently answering new searches (its 'break' periods). So before
    issuing a new query we look back through recent messages and reuse a delivered file that
    matches the wanted title + artist. Returns the best matching message, or None."""
    try:
        from rapidfuzz import fuzz
    except Exception:
        fuzz = None
    wt = (want_title or "").strip().lower()
    wa = (want_artist or "").strip().lower()
    if not wt:
        return None
    best, best_score = None, 0
    try:
        async for m in client.iter_messages(bot, limit=limit):
            if not getattr(m, "media", None):
                continue
            fname, mime, size = _doc_of(m)
            nm = (fname or "")
            # Bot FLACs are AUDIO messages: title/performer live in DocumentAttributeAudio, not
            # a filename. Pull them so the match haystack actually contains the song + artist.
            atitle = aperf = ""
            doc = getattr(m, "document", None) or getattr(m, "audio", None)
            for a in (getattr(doc, "attributes", []) or []):
                if getattr(a, "title", None) or getattr(a, "performer", None):
                    atitle = getattr(a, "title", "") or ""
                    aperf = getattr(a, "performer", "") or ""
                    break
            is_flac = ("flac" in (mime or "").lower()) or nm.lower().endswith(".flac")
            is_audio = ("audio" in (mime or "").lower()) or nm.lower().endswith(AUDIO_EXT) or bool(atitle)
            if not is_audio or (flac_only and not is_flac):
                continue
            hay = " ".join([nm, atitle, aperf, (m.message or "")]).lower()
            if fuzz:
                tscore = fuzz.token_set_ratio(wt, hay)
                ascore = fuzz.token_set_ratio(wa, hay) if wa else 100
            else:
                tscore = 100 if wt in hay else 0
                ascore = 100 if (not wa or wa in hay) else 0
            # DEMO/LIVE BAN: never reuse a (Demo)/(Live) history file for a clean liked song.
            from .version_match import demo_live_banned as _dlb
            if _dlb(atitle or nm, want_title):
                continue
            # Require a strong TITLE match and (when known) the ARTIST present, so we never
            # grab a same-named different song from the long history.
            if tscore < 86 or (wa and ascore < 80):
                continue
            score = tscore + 0.5 * ascore + (size / 1e9)  # tie-break toward the larger file
            if score > best_score:
                best, best_score = m, score
    except Exception:
        log.exception("telegram: history scan failed")
    return best


async def _acquire_async(query, want_title, want_artist, dest_dir, flac_only, timeout_seconds):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    c = _cfg()
    out = {"success": False, "paths": [], "source": "telegram", "error": None, "peer": c["bot"]}
    try:
        client = TelegramClient(StringSession(c["session"]), int(c["api_id"]), c["api_hash"])
    except Exception as e:
        out["error"] = f"bad credentials: {e}"
        return out
    await client.connect()
    deadline = time.time() + timeout_seconds
    try:
        if not await client.is_user_authorized():
            out["error"] = "telegram session not authorized"
            return out
        bot = await client.get_entity(c["bot"])
        os.makedirs(dest_dir, exist_ok=True)

        # 0) HISTORY HARVEST — reuse an already-delivered file from the chat. Works even while
        # the bot is on a break (no new query needed). Only search live if nothing matches.
        try:
            hm = await _scan_history(client, bot, want_title, want_artist, flac_only)
        except Exception:
            hm = None
        if hm is not None:
            fname, _mime, _sz = _doc_of(hm)
            dest = os.path.join(dest_dir, fname or f"telegram_{hm.id}.flac")
            try:
                await client.download_media(hm, file=dest)
                if os.path.isfile(dest) and os.path.getsize(dest) > 50000:
                    _clear_break()
                    out["paths"] = [dest]
                    out["success"] = True
                    out["note"] = "reused from chat history (no new query)"
                    return out
            except Exception:
                log.exception("telegram: history download failed")

        # 1) Not in history — if the bot is on a break, don't poke it; bail cleanly.
        _bu = _in_break()
        if _bu:
            out["error"] = "not in chat history; bot on break ~%dm" % max(1, int((_bu - time.time()) / 60))
            return out

        sent = await client.send_message(bot, query)
        last_id = sent.id

        # 1) Wait for the results message (the one carrying keycap buttons).
        results = None
        saw_any = False   # did the bot send ANY reply? (none ⇒ it's on a break)
        rdeadline = min(deadline, time.time() + 45)
        while time.time() < rdeadline and results is None:
            await asyncio.sleep(2.5)
            try:
                msgs = await client.get_messages(bot, limit=20, min_id=last_id)
            except Exception:
                continue
            for m in reversed(list(msgs)):
                if m.id <= last_id:
                    continue
                last_id = m.id
                saw_any = True
                btns = getattr(m, "buttons", None)
                _btn_num = bool(btns) and any(
                    (KEYCAP in (getattr(b, "text", "") or "")) or _NUM_RE.search(getattr(b, "text", "") or "")
                    for row in btns for b in row)
                # Detect the results message by numbered BUTTONS (keycap or circled) OR by a
                # numbered results TEXT (①①Title …) — robust to whichever format the bot uses.
                if _btn_num or _NUM_RE.search(m.message or ""):
                    results = m
                    break
        if results is None:
            if not saw_any:
                # The bot said NOTHING — it's on a rate-limit break. Back off so we stop
                # poking it (and stop wasting picker turns) until it recovers.
                _set_break(_BREAK_SECONDS)
                out["error"] = "bot silent — likely on a break; backing off %dm" % (_BREAK_SECONDS // 60)
            else:
                out["error"] = "no search results from bot"
            return out
        _clear_break()   # bot answered → break is over

        # 2) Parse + rank, and map entry number → its keycap button.
        btn_by_num = {}
        for row in results.buttons:
            for b in row:
                tx = getattr(b, "text", "") or ""
                if KEYCAP in tx or _NUM_RE.search(tx):
                    n = _num_of(tx)
                    if n >= 0:
                        btn_by_num[n] = b
        entries = _parse_results(results.message)
        ranked = _rank(entries, want_title, want_artist, flac_only)
        if not ranked:
            out["error"] = "no FLAC match in results"
            return out

        # 3) Click the best candidates in order until one delivers a real FLAC.
        seen = set()
        got = []
        for e in ranked[:4]:
            if time.time() >= deadline:
                break
            btn = btn_by_num.get(e["num"])
            if btn is None:
                continue
            try:
                await btn.click()
            except Exception:
                continue
            fdeadline = min(deadline, time.time() + 40)
            idle_since = time.time()
            while time.time() < fdeadline:
                await asyncio.sleep(2.0)
                try:
                    msgs = await client.get_messages(bot, limit=12, min_id=last_id)
                except Exception:
                    continue
                progressed = False
                for m in reversed(list(msgs)):
                    if m.id <= last_id:
                        continue
                    last_id = m.id
                    progressed = True
                    if not m.media:
                        continue
                    fname, mime, size = _doc_of(m)
                    if fname is None and not mime:
                        continue
                    nm = (fname or "").lower()
                    is_flac = ("flac" in (mime or "").lower()) or nm.endswith(".flac")
                    is_audio = ("audio" in (mime or "").lower()) or nm.endswith(AUDIO_EXT)
                    if not is_audio:
                        continue
                    if flac_only and not is_flac:
                        continue
                    if fname in seen:
                        continue
                    seen.add(fname)
                    dest = os.path.join(dest_dir, fname or f"telegram_{m.id}.flac")
                    try:
                        await client.download_media(m, file=dest)
                        if os.path.isfile(dest) and os.path.getsize(dest) > 50000:
                            got.append(dest)
                    except Exception:
                        log.exception("telegram: download failed for msg %s", m.id)
                if progressed:
                    idle_since = time.time()
                if got and (time.time() - idle_since) > 5:
                    break
            if got:
                break

        out["paths"] = got
        out["success"] = bool(got)
        if not got:
            out["error"] = "selected entry delivered no FLAC within timeout"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return out


def acquire(spotify_url=None, *, artist=None, album=None, sample_song=None,
            dest_dir=None, flac_only=True, timeout_seconds=180) -> dict:
    """Acquire FLAC for a track via @BeatSpotBot.

    The bot is a text-search engine — it ignores Spotify URLs — so we search by
    'artist title'. `sample_song` is the track title to fetch; `album` is a fallback."""
    if not is_configured():
        return {"success": False, "paths": [], "source": "telegram", "error": "telegram source not configured"}
    want_title = (sample_song or album or "").strip()
    want_artist = (artist or "").strip()
    bits = [x for x in (want_artist, want_title) if x]
    query = " ".join(bits).strip()
    if not query:
        return {"success": False, "paths": [], "source": "telegram", "error": "no search query"}
    try:
        return _run(_acquire_async(query, want_title, want_artist, dest_dir, flac_only, timeout_seconds),
                    overall_timeout=timeout_seconds + 30)
    except Exception as e:
        log.exception("telegram acquire failed")
        return {"success": False, "paths": [], "source": "telegram", "error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════════════════════
# NAS-DAEMON DELEGATION (Mac split). On the Mac, the download itself runs on
# the NAS plexify-downloader (:8788); these overrides keep the exact signature
# + return shape of the real implementations above, so picker_tick and every
# other caller is untouched. The implementations above still run verbatim
# inside the daemon on the NAS. (engine/ holds the pristine copy.)
# ═══════════════════════════════════════════════════════════════════════════

_real_acquire = acquire


def acquire(spotify_url=None, *, artist=None, album=None, sample_song=None,
            dest_dir=None, flac_only=True, timeout_seconds=180):
    if os.environ.get("PLEXIFY_DOWNLOADER_DAEMON") == "1":
        return _real_acquire(spotify_url, artist=artist, album=album,
                             sample_song=sample_song, dest_dir=dest_dir,
                             flac_only=flac_only, timeout_seconds=timeout_seconds)
    from .nas_downloader import enqueue_and_wait
    return enqueue_and_wait("telegram", mode="album", dest_dir=dest_dir,
                            artist=artist, album=album, sample_song=sample_song,
                            spotify_url=spotify_url,
                            kwargs={"flac_only": flac_only,
                                    "timeout_seconds": timeout_seconds},
                            timeout_seconds=timeout_seconds)
