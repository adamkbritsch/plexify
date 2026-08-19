"""ETA for the audiobook converter — ported from Visionary's engine/eta.py.

Visionary's estimator exists because a naive "remaining / rate_now" is wrong in three separate
ways, and auto-m4b reproduces all three:

  1. TWO REGIMES.  auto-m4b converts each track (progress observable: N of M finished), then
     ASSEMBLES — concat, chapterise, tag, move — which reports nothing at all and just parks the
     percentage at 97. Extrapolating the convert rate across the assemble phase says "done in
     seconds" and then sits there for minutes. Visionary hit the identical wall after its last
     encoded frame (concat, LUFS, mux, verify) and solved it with an explicit tail term, learned
     rather than guessed.

  2. THE MEAN IS THE WRONG MEAN.  What we predict is `remaining_tracks * mean_seconds_per_track`,
     so the estimator must be the TRACK-weighted mean of spt — equivalently the TIME-weighted
     mean of rate, i.e. sum(tracks)/sum(seconds). Averaging per-tick rates instead is optimistic
     by Jensen's inequality, permanently, and audiobook tracks vary enough in length for that to
     bite. This is why RateBook accumulates totals and never averages ratios.

  3. NOT EVERY INTERVAL IS EVIDENCE.  A daemon restart, the 20s status cache, a wedged converter,
     or a new book starting all produce intervals that look like work but aren't. Visionary's
     hardest-won lesson was that a LONG interval is a hold with a sample stapled to the end of
     it, never slow work — and that banking one inflates the estimate for hours.

What is deliberately NOT ported: the contended/solo two_phase_eta and its learned `k`. That models
one job speeding up when another releases the machine, which needs a known future release point
(Visionary has one: the Resolve gate). Nothing here has that, so importing it would be cargo cult.
The parts that transfer are the rate discipline, the sample gate, and the learned tail.

Learned state is persisted so a NEW book is not blind for its first minutes — the same reason
Visionary persists `k`. Persistence failure is always non-fatal: it degrades to the prior, which
degrades to no ETA, which degrades to what the UI showed before this existed.
"""
from __future__ import annotations

import json
import threading
import time

# ── priors ────────────────────────────────────────────────────────────────────────────────────
# Measured on this NAS (N100, 4 cores) across the existing library: an audiobook track is ~5-10
# minutes of audio and converts in a few seconds. These are only starting points — the first real
# samples move them, and they exist so a brand-new book shows a number immediately.
# Throughput is measured in BYTES of source per second, not tracks per second. Tracks are not a
# unit of work: a book can arrive as 200 chapter files or as ONE 10-hour file, and "1 track left"
# says nothing about whether that means four seconds or forty minutes. This was not hypothetical —
# the first live book was a single m4b and the track model confidently predicted "<1m" for the
# whole conversion. Bytes are proportional to the work ffmpeg actually does, and they are known up
# front for every shape of book.
PRIOR_BYTES_PER_SEC = 6.0 * 1024 * 1024   # ~6 MB/s of source, N100-class hardware
PRIOR_ASSEMBLE_SECS_PER_GB = 90.0    # concat + chapterise + tag + move, per GB of source

# A sample longer than this contains a hold (daemon restart, wedged ffmpeg, machine asleep), not
# slow conversion. Visionary's MAX_IDLE_TICK, scaled for a status poll that is cached for 20s.
MAX_SAMPLE_SECONDS = 300.0
# Physically impossible convert rate — catches a restart (or a rescanned tmpfiles dir) presenting
# many already-finished tracks at once. Real conversion runs ~0.25 tracks/s here, so 5/s is still
# 20x the truth and nowhere near legitimate work; 20/s was loose enough to swallow a moderate
# replay burst, which is the exact failure Visionary's R_MAX_FPS gate exists to stop.
#
# Note this gate is NOT what handles the 20s status cache. A cached poll shows the same `done` for
# several ticks and then jumps — and total-over-total absorbs that correctly, because the quiet
# ticks bank their seconds and the jump banks its tracks. Sum/sum stays right either way, which is
# a second reason the rate is accumulated rather than averaged.
MAX_BYTES_PER_SEC = 400 * 1024 * 1024      # 400 MB/s of source is a replay, not a transcode
# Enough evidence before this book's own rate outranks the learned prior.
MIN_SAMPLE_BYTES = 8 * 1024 * 1024
MIN_SAMPLE_SECONDS = 30.0

MAX_SAMPLES = 30                      # matches Visionary's model window
_LOCK = threading.Lock()


def accept_sample(d_bytes, dt) -> bool:
    """Is this (tracks, seconds) interval real conversion evidence?

    Rejects a backwards or zero clock, progress going backwards (a new book, or a restart that
    cleared tmpfiles), an impossible rate (a restart replaying finished tracks), and any long
    interval (a hold, not slow work — see the module docstring).
    """
    if dt is None or d_bytes is None:
        return False
    if dt <= 0 or d_bytes < 0:
        return False
    if dt > MAX_SAMPLE_SECONDS:
        return False
    if d_bytes == 0:
        return True                    # a short tick with no visible progress IS slow-work evidence
    return (d_bytes / dt) <= MAX_BYTES_PER_SEC


class RateBook:
    """(bytes, seconds) accumulator for ONE book.

    rate = sum(bytes) / sum(seconds) — total over total, never a mean of ratios. See point 2 in
    the module docstring; this specific form is the whole reason the class exists.
    """

    def __init__(self):
        self.bytes = 0.0
        self.seconds = 0.0

    def tick(self, d_bytes, dt) -> bool:
        if not accept_sample(d_bytes, dt):
            return False
        self.bytes += float(d_bytes)
        self.seconds += float(dt)
        return True

    def rate(self):
        """bytes/sec, or None until it has seen enough for the number to mean anything."""
        if self.seconds <= 0 or self.bytes < MIN_SAMPLE_BYTES or self.seconds < MIN_SAMPLE_SECONDS:
            return None
        return self.bytes / self.seconds


def assemble_estimate(src_bytes, secs_per_gb: float = PRIOR_ASSEMBLE_SECS_PER_GB) -> float:
    """Seconds of assemble work still owed once the last track is converted.

    Every term in it (concat, chapter pass, tag write, move) is linear in output size, so it is
    modelled per GB and learned from what actually happens — Visionary's tail_estimate, with GB
    in place of kiloframes.
    """
    try:
        gb = max(0.0, float(src_bytes or 0)) / (1024.0 ** 3)
    except (TypeError, ValueError):
        gb = 0.0
    return max(10.0, gb * float(secs_per_gb))


def estimate(*, phase, done, files, src_bytes, rate, secs_per_gb, elapsed=0.0, stalled=False):
    """Seconds remaining, or None when no honest number can be given.

    Two ways to know how much convert work is left, in order of preference:

      PROGRESS   when the book has several tracks, `done/files` says what fraction of the source
                 has been consumed, so remaining bytes are known directly. Best signal available.

      ELAPSED    when the book is ONE file, `done` is 0 until it is 100% — there is no progress
                 signal at all. So predict the whole job from its size and the learned throughput,
                 and subtract the time already spent. This is the case the track-based version got
                 badly wrong.

    None is a real answer, and returning it matters more than covering every case: a stalled
    converter has no meaningful ETA, and an elapsed-based estimate that has already overrun its
    own prediction has been proven wrong — continuing to show "<1m" while nothing finishes is
    exactly the lie that makes people stop trusting a progress display.
    """
    if stalled:
        return None
    try:
        done_i, files_i = int(done or 0), int(files or 0)
        total_bytes = float(src_bytes or 0)
    except (TypeError, ValueError):
        return None

    tail = assemble_estimate(src_bytes, secs_per_gb)
    if phase == "assembling":
        return tail                      # source is consumed; only the invisible part is left
    if not rate or rate <= 0 or total_bytes <= 0:
        return None

    if files_i > 1 and done_i > 0:
        remaining_bytes = total_bytes * max(0.0, 1.0 - (done_i / files_i))
        return (remaining_bytes / float(rate)) + tail

    predicted_total = total_bytes / float(rate)
    remaining = predicted_total - max(0.0, float(elapsed or 0.0))
    if remaining <= 0:
        return None                      # overran the prediction: say nothing rather than lie
    return remaining + tail


def format_eta(seconds) -> str:
    """Short human form: '4m', '1h 12m', '<1m'."""
    if seconds is None:
        return ""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return "<1m"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


# ── learned model (persisted; never fatal) ────────────────────────────────────────────────────

def _load() -> dict:
    try:
        from .db import get_config
        d = json.loads(get_config("audiobook_eta_model_json", "") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        from .db import set_config
        set_config("audiobook_eta_model_json", json.dumps(d))
    except Exception:
        pass                            # a model we can't persist is a model we relearn


def _median(xs):
    v = sorted(float(x) for x in xs if x and float(x) > 0)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def learned():
    """(bytes_per_sec, assemble_secs_per_gb) — the median of past books, else the priors.

    Median rather than mean: one pathological book (a 200-part collection, a stalled run that
    still finished) should not drag the estimate for every book after it.
    """
    d = _load()
    return (_median(d.get("rate_samples") or []) or PRIOR_BYTES_PER_SEC,
            _median(d.get("assemble_samples") or []) or PRIOR_ASSEMBLE_SECS_PER_GB)


def record(*, rate=None, assemble_secs=None, src_bytes=None) -> None:
    """Bank what a finished book actually did, so the next one starts better informed."""
    with _LOCK:
        d = _load()
        if rate and rate > 0:
            xs = [x for x in (d.get("rate_samples") or []) if isinstance(x, (int, float)) and x > 0]
            xs.append(round(float(rate), 5))
            d["rate_samples"] = xs[-MAX_SAMPLES:]
        if assemble_secs and src_bytes:
            gb = float(src_bytes) / (1024.0 ** 3)
            if gb > 0.05 and assemble_secs > 0:      # ignore sub-50MB noise
                ys = [y for y in (d.get("assemble_samples") or []) if isinstance(y, (int, float)) and y > 0]
                ys.append(round(float(assemble_secs) / gb, 3))
                d["assemble_samples"] = ys[-MAX_SAMPLES:]
        d["version"] = 1
        _save(d)


# ── live tracker (one book at a time; the converter is single-flight) ─────────────────────────

_STATE = {"book": None, "done": 0, "ts": 0.0, "book_rb": None, "convert_started": 0.0,
          "assemble_started": 0.0, "src_bytes": 0}


def _consumed_bytes(done, files, src_bytes) -> float:
    """Source bytes the converter has got through, from the per-track fraction.

    Zero for a single-file book all the way to the end — which is exactly why `estimate` has an
    elapsed-based path and does not depend on this being informative.
    """
    try:
        d, f, b = int(done or 0), int(files or 0), float(src_bytes or 0)
    except (TypeError, ValueError):
        return 0.0
    return b * (d / f) if f > 0 else 0.0


def observe(active) -> "int | None":
    """Feed one converter observation, get back seconds remaining (or None).

    Called from the status path, so it sees exactly the cadence the UI polls at.
    """
    if not active or not active.get("book"):
        _STATE.update(book=None, book_rb=None, convert_started=0.0, assemble_started=0.0)
        return None
    now = time.time()
    book = active.get("book")
    done = int(active.get("done") or 0)
    files = int(active.get("files") or 0)
    phase = active.get("phase")
    src_bytes = int(active.get("src_bytes") or 0)
    prior_rate, spg = learned()

    if _STATE["book"] != book:           # new book: start clean, never carry rate across books
        _STATE.update(book=book, done=done, ts=now, book_rb=RateBook(), convert_started=now,
                      assemble_started=0.0, src_bytes=src_bytes)
        return estimate(phase=phase, done=done, files=files, src_bytes=src_bytes,
                        rate=prior_rate, secs_per_gb=spg, elapsed=0.0,
                        stalled=active.get("stalled"))

    rb = _STATE["book_rb"] or RateBook()
    d_bytes = (_consumed_bytes(done, files, src_bytes)
               - _consumed_bytes(_STATE["done"], files, _STATE["src_bytes"] or src_bytes))
    rb.tick(d_bytes, now - float(_STATE["ts"] or now))
    _STATE.update(done=done, ts=now, book_rb=rb, src_bytes=src_bytes or _STATE["src_bytes"])

    rate = rb.rate() or prior_rate       # this book's own throughput once it is trustworthy
    elapsed = now - float(_STATE["convert_started"] or now)

    if phase == "assembling":
        if not _STATE["assemble_started"]:
            _STATE["assemble_started"] = now
            # The convert phase just ended — bank what it actually achieved. Prefer the whole-job
            # measurement (total bytes over total convert time) over the in-book rate: it is the
            # only one a single-file book can produce, and it is what the next book needs.
            if elapsed > MIN_SAMPLE_SECONDS and src_bytes > 0:
                record(rate=src_bytes / elapsed)
    elif _STATE["assemble_started"]:
        _STATE["assemble_started"] = 0.0

    return estimate(phase=phase, done=done, files=files, src_bytes=src_bytes,
                    rate=rate, secs_per_gb=spg, elapsed=elapsed,
                    stalled=active.get("stalled"))


def note_finished(src_bytes=None) -> None:
    """Called when a book leaves the converter, to learn how long assembling really took."""
    started = float(_STATE.get("assemble_started") or 0)
    if started:
        record(assemble_secs=time.time() - started,
               src_bytes=src_bytes or _STATE.get("src_bytes"))
    _STATE.update(book=None, book_rb=None, assemble_started=0.0)
