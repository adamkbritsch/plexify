"""One-time Telegram session generator.

Run inside the container:

    docker compose exec plexify python -m app.tg_login

It logs into Telegram as YOUR user account (required because bots can't message
@BeatSpotBot), prints the resulting Telethon StringSession, AND saves it straight
into Plexify's config so you don't have to copy the long string by hand. Your
phone number, login code, and 2FA password are entered interactively here and are
never stored — only the resulting session token is.

api_id / api_hash are read from Settings → Telegram source if already saved there,
otherwise it asks for them.
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
    except Exception as e:  # pragma: no cover
        print(f"Telethon is not installed in this image: {e}", file=sys.stderr)
        print("Rebuild the container so requirements.txt (with Telethon) is baked in.", file=sys.stderr)
        return 2

    from .db import get_config, set_config

    api_id = (get_config("telegram_api_id", "") or "").strip()
    api_hash = (get_config("telegram_api_hash", "") or "").strip()

    if not api_id:
        api_id = input("Telegram API ID (from my.telegram.org): ").strip()
    else:
        print(f"Using saved API ID: {api_id}")
    if not api_hash:
        api_hash = input("Telegram API hash: ").strip()
    else:
        print("Using saved API hash.")

    if not api_id or not api_hash:
        print("API ID and API hash are both required.", file=sys.stderr)
        return 2

    try:
        api_id_int = int(api_id)
    except ValueError:
        print(f"API ID must be a number, got: {api_id!r}", file=sys.stderr)
        return 2

    print("\nSigning in — you'll be asked for your phone number, then the code Telegram sends you")
    print("(and your 2FA password if you have one set).\n")

    # .start() runs the interactive phone/code/2FA flow synchronously.
    with TelegramClient(StringSession(), api_id_int, api_hash) as client:
        session_str = client.session.save()
        me = client.get_me()
        who = ("@" + me.username) if getattr(me, "username", None) else (getattr(me, "first_name", "") or "user")

    # Persist credentials + session so Settings is fully populated.
    set_config("telegram_api_id", api_id)
    set_config("telegram_api_hash", api_hash)
    set_config("telegram_session", session_str)

    print("\n" + "=" * 60)
    print(f"Logged in as {who}. Session saved to Plexify config.")
    print("=" * 60)
    print("\nIf you'd rather paste it into Settings manually, here it is:\n")
    print(session_str)
    print("\nNext: open Settings → Telegram source, tick 'Enable', and Save.")
    print("Then click 'Test session' to confirm @BeatSpotBot is reachable.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
