"""CLI subcommands — send, sendfile, encrypt (used by agents during goal steps)."""

import json
import os
import sys
from pathlib import Path

from arbos.config import WORKING_DIR, step_msg_file, ENV_ENC_FILE
from arbos.discord_api import rest_send, rest_edit, rest_send_file
from arbos.prompt import log_chat


def send(args: list[str]):
    """CLI: arbos send 'message' [--file path]

    Within a step, all sends are consolidated into a single Discord message.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Send a Discord message to the operator")
    parser.add_argument("message", nargs="?", help="Message text to send")
    parser.add_argument("--file", help="Send contents of a file instead")
    parsed = parser.parse_args(args)

    if not parsed.message and not parsed.file:
        parser.error("Provide a message or --file")

    if parsed.file:
        text = Path(parsed.file).read_text()
    else:
        text = parsed.message

    workspace = int(os.environ.get("ARBOS_WORKSPACE", "0"))
    thread_id = int(os.environ.get("ARBOS_THREAD_ID", "0"))

    if not workspace or not thread_id:
        print("ARBOS_WORKSPACE and ARBOS_THREAD_ID must be set", file=sys.stderr)
        sys.exit(1)

    smf = step_msg_file(workspace, thread_id)
    smf.parent.mkdir(parents=True, exist_ok=True)

    if smf.exists():
        try:
            state = json.loads(smf.read_text())
            msg_id = int(state["msg_id"])
            channel_id = int(state.get("channel_id", thread_id))
            prev_text = state.get("text", "")
        except (json.JSONDecodeError, KeyError):
            msg_id = None
            channel_id = thread_id
            prev_text = ""
    else:
        msg_id = None
        channel_id = thread_id
        prev_text = ""

    if msg_id:
        combined = (prev_text + "\n\n" + text).strip()
        if rest_edit(channel_id, msg_id, combined):
            smf.write_text(json.dumps({"msg_id": msg_id, "channel_id": channel_id, "text": combined}))
            log_chat(workspace, "bot", combined[:1000])
            print(f"Edited step message ({len(combined)} chars)")
        else:
            new_id = rest_send(channel_id, text)
            if new_id:
                smf.write_text(json.dumps({"msg_id": new_id, "channel_id": channel_id, "text": text}))
                log_chat(workspace, "bot", text[:1000])
                print(f"Sent new message ({len(text)} chars)")
            else:
                print("Failed to send", file=sys.stderr)
                sys.exit(1)
    else:
        new_id = rest_send(channel_id, text)
        if new_id:
            smf.write_text(json.dumps({"msg_id": new_id, "channel_id": channel_id, "text": text}))
            log_chat(workspace, "bot", text[:1000])
            print(f"Sent ({len(text)} chars)")
        else:
            print("Failed to send (check DISCORD_BOT_TOKEN)", file=sys.stderr)
            sys.exit(1)


def sendfile(args: list[str]):
    """CLI: arbos sendfile path/to/file [--caption 'text']"""
    import argparse
    parser = argparse.ArgumentParser(description="Send a file to the operator via Discord")
    parser.add_argument("path", help="Path to the file to send")
    parser.add_argument("--caption", default="", help="Caption for the file")
    parsed = parser.parse_args(args)

    file_path = Path(parsed.path)
    if not file_path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    thread_id = int(os.environ.get("ARBOS_THREAD_ID", "0"))
    if not thread_id:
        print("ARBOS_THREAD_ID must be set", file=sys.stderr)
        sys.exit(1)

    ok = rest_send_file(thread_id, str(file_path), caption=parsed.caption)
    if ok:
        print(f"Sent file: {file_path.name}")
    else:
        print("Failed to send (check DISCORD_BOT_TOKEN)", file=sys.stderr)
        sys.exit(1)


def encrypt():
    """CLI: arbos encrypt — encrypt .env -> .env.enc"""
    from dotenv import load_dotenv
    from arbos.env import encrypt_env_file

    env_path = WORKING_DIR / ".env"
    if not env_path.exists():
        if ENV_ENC_FILE.exists():
            print(".env.enc already exists (already encrypted)")
        else:
            print(".env not found, nothing to encrypt")
        return

    load_dotenv(env_path)
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        print("DISCORD_BOT_TOKEN must be set in .env", file=sys.stderr)
        sys.exit(1)
    encrypt_env_file(bot_token)
    print("Encrypted .env -> .env.enc, deleted plaintext.")
    print(f"On future starts: DISCORD_BOT_TOKEN='{bot_token}' arbos")
