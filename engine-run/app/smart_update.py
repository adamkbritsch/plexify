"""LLM-powered self-healing for SpotiFLAC's git-main auto-updates.

When the daily update breaks SpotiFLAC in a way the deterministic future-annotations
patch can't fix, this asks Claude (Opus) to read the broken module(s) + the import
error and return the MINIMAL edits that restore `import SpotiFLAC`. It is:

  • Sandboxed   — only writes files inside the installed SpotiFLAC package dir.
  • Verified    — re-imports after applying; rolls back if it didn't help.
  • Economical  — only ever called when an update genuinely breaks the import (rare),
                  and only after the free deterministic patch has already failed.
  • Opt-in      — disabled unless an Anthropic API key is saved in Settings.

Uses the official Anthropic SDK with claude-opus-4-8 (adaptive thinking, high effort)
and structured outputs so the response is a validated {path, new_content} list.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time

log = logging.getLogger(__name__)

MODEL_DEFAULT = "claude-opus-4-8"
_MAX_FILES = 8
_MAX_INPUT_BYTES = 70000


def _cfg():
    try:
        from .db import get_config
    except ImportError:  # standalone import (diagnostics)
        from app.db import get_config
    return {
        "enabled": (get_config("smart_update_enabled", "0") or "0") == "1",
        "api_key": get_config("anthropic_api_key", "") or "",
        "model": (get_config("anthropic_model", MODEL_DEFAULT) or MODEL_DEFAULT),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["enabled"] and c["api_key"])


def check_key() -> dict:
    """Cheap validation for the Settings 'Test' button — one tiny API call."""
    c = _cfg()
    if not c["api_key"]:
        return {"ok": False, "error": "no API key saved"}
    try:
        import anthropic
    except Exception as e:
        return {"ok": False, "error": f"anthropic SDK not installed (rebuild needed): {e}"}
    try:
        client = anthropic.Anthropic(api_key=c["api_key"])
        r = client.messages.create(
            model=c["model"], max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return {"ok": True, "model": c["model"], "reply": txt.strip()[:40]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _spotiflac_dir():
    for sp in list(sys.path):
        try:
            cand = os.path.join(sp, "SpotiFLAC")
            if os.path.isdir(cand):
                return os.path.realpath(cand)
        except Exception:
            pass
    return None


def _collect_files(error_text: str, root: str):
    """Files referenced in the traceback first, then the rest of the package, bounded."""
    import re as _re
    ordered = []
    for m in _re.findall(r'File "([^"]*SpotiFLAC[^"]*\.py)"', error_text or ""):
        rp = os.path.realpath(m)
        if rp.startswith(root + os.sep) and os.path.isfile(rp) and rp not in ordered:
            ordered.append(rp)
    for dp, _d, fs in os.walk(root):
        for fn in sorted(fs):
            if fn.endswith(".py"):
                p = os.path.join(dp, fn)
                if p not in ordered:
                    ordered.append(p)
    files, total = [], 0
    for p in ordered[:_MAX_FILES]:
        try:
            s = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if total + len(s) > _MAX_INPUT_BYTES:
            continue
        total += len(s)
        files.append({"path": p, "content": s})
    return files


def _verify_import():
    import subprocess as _sp
    try:
        r = _sp.run(["python", "-c", "import SpotiFLAC"], capture_output=True, text=True, timeout=45)
        return r.returncode == 0, (r.stderr or "")[-1500:]
    except Exception as e:
        return False, str(e)[:300]


def llm_repair(error_text: str) -> dict:
    """Ask Claude to fix a broken SpotiFLAC import. Returns a result dict."""
    out = {"applied": False, "files_changed": [], "error": None, "model": None, "explanation": None}
    c = _cfg()
    if not (c["enabled"] and c["api_key"]):
        out["error"] = "smart update not configured"
        return out
    root = _spotiflac_dir()
    if not root:
        out["error"] = "SpotiFLAC package dir not found"
        return out
    files = _collect_files(error_text, root)
    if not files:
        out["error"] = "no source files to send"
        return out
    try:
        import anthropic
    except Exception as e:
        out["error"] = f"anthropic SDK not installed (rebuild needed): {e}"
        return out

    out["model"] = c["model"]
    system = (
        "You are repairing a third-party Python package (SpotiFLAC) that fails to IMPORT "
        "after an automated upstream update. You are given the import error and the source of "
        "the relevant modules. Return the MINIMAL edits that make `import SpotiFLAC` succeed — "
        "typically a missing import, a bad relative import, or a small syntax/typo fix. Do NOT "
        "refactor, do NOT change behavior, do NOT remove functionality. Only fix what breaks "
        "the import. Return the full new content of each file you change, unchanged except for "
        "the minimal fix."
    )
    files_blob = "\n\n".join(f"### FILE: {f['path']}\n```python\n{f['content']}\n```" for f in files)
    user_msg = (
        f"Import error / traceback:\n```\n{(error_text or '')[-4000:]}\n```\n\n"
        f"Relevant source files:\n{files_blob}\n\nReturn only the files you changed."
    )
    schema = {
        "type": "object",
        "properties": {
            "changed_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "new_content": {"type": "string"},
                    },
                    "required": ["path", "new_content"],
                    "additionalProperties": False,
                },
            },
            "explanation": {"type": "string"},
        },
        "required": ["changed_files", "explanation"],
        "additionalProperties": False,
    }
    try:
        client = anthropic.Anthropic(api_key=c["api_key"])
        resp = client.messages.create(
            model=c["model"],
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        out["error"] = f"anthropic call failed: {str(e)[:200]}"
        return out

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
    except Exception as e:
        out["error"] = f"could not parse LLM response: {e}"
        return out
    changes = data.get("changed_files") or []
    out["explanation"] = (data.get("explanation") or "")[:500]
    if not changes:
        out["error"] = "LLM proposed no changes"
        return out

    # Apply — sandboxed to the SpotiFLAC dir, with a backup for rollback.
    bak = f"/tmp/spotiflac_llm_bak_{int(time.time())}"
    applied = []  # (path, backup_path|None)
    for ch in changes:
        p = os.path.realpath(ch.get("path", ""))
        nc = ch.get("new_content")
        if not (p.startswith(root + os.sep) and p.endswith(".py")):
            log.warning("smart_update: refusing out-of-sandbox path %s", p)
            continue
        if not isinstance(nc, str) or not nc.strip():
            continue
        try:
            os.makedirs(bak, exist_ok=True)
            bpath = None
            if os.path.isfile(p):
                bpath = os.path.join(bak, p.lstrip("/").replace("/", "__"))
                shutil.copy2(p, bpath)
            with open(p, "w", encoding="utf-8") as f:
                f.write(nc)
            applied.append((p, bpath))
        except Exception:
            log.exception("smart_update: write failed for %s", p)
    if not applied:
        out["error"] = "no in-sandbox changes applied"
        return out

    ok, err = _verify_import()
    if ok:
        out["applied"] = True
        out["files_changed"] = [p for p, _b in applied]
        log.warning("smart_update: Claude repaired SpotiFLAC import — %d file(s): %s",
                    len(applied), out["explanation"])
        return out

    # Didn't help — roll back so we never leave it worse than the deterministic patch left it.
    for p, bpath in applied:
        if bpath and os.path.isfile(bpath):
            try:
                shutil.copy2(bpath, p)
            except Exception:
                log.exception("smart_update: rollback failed for %s", p)
    out["error"] = "LLM repair did not fix the import (rolled back): " + (err or "")[-200:]
    log.warning("smart_update: LLM repair didn't fix import; rolled back")
    return out
