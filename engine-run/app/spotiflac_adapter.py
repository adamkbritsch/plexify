"""spotiflac_adapter.py — thin wrapper around SpotiFLAC for Plexify.

Gives callers a clean, never-raising interface:

    result = acquire(spotify_url, dest_dir, ...)

Thread-cancellation caveat
--------------------------
`acquire()` runs SpotiFLAC inside a ThreadPoolExecutor worker and calls
`future.result(timeout=...)`.  On timeout we stop *waiting*, but the
underlying download thread cannot be forcibly killed (Python has no thread
kill API).  The orphaned thread will finish (or error) on its own; files it
drops into dest_dir after the timeout are simply ignored by the caller
because we snapshot directory contents at the moment `acquire()` returns.
In practice SpotiFLAC respects SIGTERM sent to the process, so full-process
restarts clean up reliably.  For the APScheduler / single-process deployment
model here, this is acceptable.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Optional

# SpotiFLAC is installed ONLY on the NAS daemon (deliberately excluded from the Mac
# venv). A bare module-level import made `import spotiflac_adapter` — and therefore
# EVERY picker_tick album acquisition on the Mac — crash with ModuleNotFoundError
# before the NAS-DAEMON delegation override below could take over (found 2026-07-05:
# every claim died post-claim, leaving zombie 'downloading' rows and in_flight=0).
try:
    from SpotiFLAC import DownloadOptions, SpotiflacDownloader
except ImportError:                     # Mac split: the delegation override handles acquire()
    DownloadOptions = SpotiflacDownloader = None

log = logging.getLogger(__name__)

# ── public types ─────────────────────────────────────────────────────────────

@dataclass
class AcquireResult:
    success: bool
    paths: list[str] = field(default_factory=list)   # absolute paths to delivered files
    bytes_total: int = 0                               # sum of file sizes
    provider: Optional[str] = None                    # "tidal"/"qobuz"/"deezer"/"amazon" — best-effort
    quality_requested: str = ""
    error: Optional[str] = None                       # exception message if not success
    duration_seconds: float = 0.0                     # wallclock time
    raw_stdout_tail: str = ""                         # last ~30 lines of SpotiFLAC output


# ── internals ────────────────────────────────────────────────────────────────

_DEFAULT_SERVICES = ["qobuz", "deezer", "amazon", "tidal"]  # Tidal last — mirrors flaky

# Providers that appear in SpotiFLAC's stdout banner, e.g.:
#   📡  QOBUZ  ·  api.zarz.moe  ·  27
_PROVIDER_MARKERS = {
    "QOBUZ":   "qobuz",
    "TIDAL":   "tidal",
    "DEEZER":  "deezer",
    "AMAZON":  "amazon",
}


def _walk_files(directory: str) -> set[str]:
    """Return the set of all absolute file paths under *directory* (recursive)."""
    found: set[str] = set()
    for root, _dirs, files in os.walk(directory):
        for name in files:
            found.add(os.path.join(root, name))
    return found


def _parse_provider(text: str) -> Optional[str]:
    """Best-effort: scan stdout text for a provider banner line."""
    for line in text.splitlines():
        upper = line.upper()
        for marker, canonical in _PROVIDER_MARKERS.items():
            if marker in upper:
                return canonical
    return None


def _tail(text: str, n_lines: int = 30) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:]) if len(lines) > n_lines else text


def _run_spotiflac(
    spotify_url: str,
    dest_dir: str,
    services: list[str],
    quality: str,
    qobuz_token: Optional[str] = None,
) -> tuple[str, str]:
    """Execute SpotiFLAC in-process, capturing stdout + stderr.

    Returns (stdout_text, stderr_text).
    Raises on any SpotiFLAC-internal exception.

    If qobuz_token is set, it is forwarded to SpotiFLAC's DownloadOptions and
    passed to BOTH tidal and qobuz providers — unlocking the authenticated
    download paths instead of the (unauthorized, currently-broken) scraper paths.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    _opts_kwargs = dict(
        output_dir=dest_dir,
        services=services,
        quality=quality,
        allow_fallback=True,
    )
    if qobuz_token:
        _opts_kwargs["qobuz_token"] = qobuz_token
    opts = DownloadOptions(**_opts_kwargs)
    downloader = SpotiflacDownloader(opts)

    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        downloader.run(spotify_url)

    return stdout_buf.getvalue(), stderr_buf.getvalue()


# ── public API ───────────────────────────────────────────────────────────────

def acquire(
    spotify_url: str,
    dest_dir: str,
    services: list[str] = None,
    quality: str = "HI_RES_LOSSLESS",
    timeout_seconds: int = 600,
) -> AcquireResult:
    """Download a Spotify track, album, or playlist URL to dest_dir.

    Wraps SpotiFLAC in-process with stdout/stderr capture, file-discovery
    diffing, provider detection, and timing.

    Never raises — any exception is converted to AcquireResult(success=False).

    Args:
        spotify_url:      Full Spotify URL (track / album / playlist).
        dest_dir:         Absolute path where files should land.
                services:    Provider priority list; default is qobuz/deezer/amazon/tidal.
                 Tidal is last because its mirror endpoints are unreliable.
                 Tidal is last because its mirror endpoints are unreliable.
        quality:          SpotiFLAC quality string; default "HI_RES_LOSSLESS".
        timeout_seconds:  Wall-clock timeout.  On expiry we stop waiting;
                          the background thread may continue (see module docstring).

    Returns:
        AcquireResult with all fields populated.
    """
    if SpotiflacDownloader is None:     # SpotiFLAC not installed (Mac split organizer)
        return AcquireResult(success=False, error="SpotiFLAC not installed on this host",
                             duration_seconds=0.0)

    if services is None:
        services = list(_DEFAULT_SERVICES)

    os.makedirs(dest_dir, exist_ok=True)

    t0 = time.monotonic()
    before: set[str] = _walk_files(dest_dir)

    stdout_text = ""
    stderr_text = ""
    error_msg: Optional[str] = None
    success = False

    # FAIL-FAST: don't use `with ThreadPoolExecutor(...) as pool:` — its __exit__
    # calls shutdown(wait=True) which BLOCKS until the underlying SpotiFLAC
    # thread finishes naturally. That meant our 90s "timeout" was actually
    # "90s + however long the abandoned thread takes to die" (often +40-60s).
    # Manually shutdown(wait=False, cancel_futures=True) so we return at exactly
    # the timeout. The orphan thread becomes a daemon zombie that the OS
    # reaps when the process exits — acceptable.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spotiflac")
    try:
        # Read Qobuz token from config — unlocks authenticated Tidal+Qobuz paths
        _qobuz_tok = None
        try:
            from .db import get_config as _gc
            _qobuz_tok = (_gc("spotiflac_qobuz_token", "") or "").strip() or None
        except Exception:
            pass
        future = pool.submit(_run_spotiflac, spotify_url, dest_dir, services, quality, _qobuz_tok)
        try:
            stdout_text, stderr_text = future.result(timeout=timeout_seconds)
            success = True
        except FuturesTimeoutError:
            error_msg = f"spotiflac timed out after {timeout_seconds}s"
            log.warning("acquire: %s — url=%s", error_msg, spotify_url)
    except Exception as exc:                          # broad catch — never raise
        error_msg = str(exc) or repr(exc)
        # Expected source-misses (a track simply isn't on a given scraper provider)
        # are NORMAL, not crashes. Logging a full traceback for each one flooded the
        # container log with millions of lines. Log those as one concise WARNING; keep
        # full tracebacks only for genuinely unexpected errors.
        _ml = error_msg.lower()
        if any(k in _ml for k in ("could not resolve", "via any method", "not found",
                                  "no match", "unavailable", "no download", "404")):
            log.warning("acquire: source miss (%s) — url=%s", error_msg[:160], spotify_url)
        else:
            log.exception("acquire: unexpected error for url=%s", spotify_url)
    finally:
        # Don't wait for orphan thread; let it die when process exits.
        pool.shutdown(wait=False, cancel_futures=True)

    duration = time.monotonic() - t0

    # Merge both streams for debugging; stdout usually has the interesting bits
    combined = stdout_text + ("\n--- stderr ---\n" + stderr_text if stderr_text.strip() else "")
    tail = _tail(combined, 30)

    # Discover new files written to dest_dir (handles nested album subdirs)
    after: set[str] = _walk_files(dest_dir)
    new_files = sorted(
        p for p in (after - before)
        if os.path.splitext(p)[1].lower() in {".flac", ".mp3"}
    )

    bytes_total = 0
    for p in new_files:
        try:
            bytes_total += os.path.getsize(p)
        except OSError:
            pass

    if success and not new_files:
        # SpotiFLAC returned cleanly but nothing landed — treat as soft failure
        success = False
        error_msg = "SpotiFLAC completed without producing any FLAC/MP3 files"
        log.warning("acquire: no output files for url=%s (dest_dir=%s)", spotify_url, dest_dir)

    provider = _parse_provider(combined)

    log.info(
        "acquire: success=%s provider=%s files=%d bytes=%d duration=%.1fs url=%s",
        success, provider, len(new_files), bytes_total, duration, spotify_url,
    )

    return AcquireResult(
        success=success,
        paths=new_files,
        bytes_total=bytes_total,
        provider=provider,
        quality_requested=quality,
        error=error_msg,
        duration_seconds=duration,
        raw_stdout_tail=tail,
    )


# ═══════════════════════════════════════════════════════════════════════════
# NAS-DAEMON DELEGATION (Mac split). On the Mac, the download itself runs on
# the NAS plexify-downloader (:8788); these overrides keep the exact signature
# + return shape of the real implementations above, so picker_tick and every
# other caller is untouched. The implementations above still run verbatim
# inside the daemon on the NAS. (engine/ holds the pristine copy.)
# ═══════════════════════════════════════════════════════════════════════════

_real_acquire = acquire


def acquire(spotify_url, dest_dir, services=None, quality="HI_RES_LOSSLESS",
            timeout_seconds=600):
    if os.environ.get("PLEXIFY_DOWNLOADER_DAEMON") == "1":
        return _real_acquire(spotify_url, dest_dir, services=services,
                             quality=quality, timeout_seconds=timeout_seconds)
    from .nas_downloader import enqueue_and_wait
    kw = {"quality": quality, "timeout_seconds": timeout_seconds}
    if services is not None:
        kw["services"] = services
    r = enqueue_and_wait("spotiflac", mode="album", dest_dir=dest_dir,
                         spotify_url=spotify_url, kwargs=kw,
                         timeout_seconds=timeout_seconds)
    return AcquireResult(success=bool(r.get("success")), paths=r.get("paths") or [],
                         bytes_total=r.get("bytes_total", 0), provider="nas-daemon",
                         quality_requested=quality, error=r.get("error"))
