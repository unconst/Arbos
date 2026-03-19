"""Discord REST and async messaging — send, edit, file upload, attachment download."""

import asyncio
import base64
import os
from datetime import datetime
from pathlib import Path

import requests

from arbos.config import CHUTES_API_KEY, files_dir
from arbos.log import log
from arbos.redact import redact_secrets
from arbos import state

DISCORD_API = "https://discord.com/api/v10"


def rest_send(channel_id: int, text: str) -> int | None:
    """Send a message via Discord REST API. Returns message ID or None."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        return None
    text = redact_secrets(text)[:2000]
    try:
        resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"content": text},
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return int(resp.json()["id"])
    except Exception as exc:
        log(f"discord REST send failed: {str(exc)[:120]}")
    return None


def rest_edit(channel_id: int, message_id: int, text: str) -> bool:
    """Edit a message via Discord REST API."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        return False
    text = redact_secrets(text)[:2000]
    try:
        resp = requests.patch(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
            json={"content": text},
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


def rest_send_file(channel_id: int, file_path: str, caption: str = "") -> bool:
    """Send a file via Discord REST API."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        return False
    caption = redact_secrets(caption)[:2000]
    try:
        data = {}
        if caption:
            data["content"] = caption
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                data=data,
                files={"file": (Path(file_path).name, f)},
                headers={"Authorization": f"Bot {token}"},
                timeout=60,
            )
        return resp.status_code in (200, 201)
    except Exception as exc:
        log(f"discord REST file send failed: {str(exc)[:120]}")
        return False


def send_new(channel_id: int, text: str) -> int | None:
    """Send a new Discord message. Uses async bridge if available, REST fallback."""
    text = redact_secrets(text)[:2000]
    if (state.discord_client and state.discord_loop
            and state.discord_loop.is_running()
            and state.discord_async_failures < state.DISCORD_ASYNC_MAX_FAILURES):
        try:
            async def _do():
                ch = state.discord_client.get_channel(channel_id)
                if not ch:
                    ch = await state.discord_client.fetch_channel(channel_id)
                msg = await ch.send(text)
                return msg.id
            future = asyncio.run_coroutine_threadsafe(_do(), state.discord_loop)
            result = future.result(timeout=15)
            state.discord_async_failures = 0
            return result
        except Exception as exc:
            state.discord_async_failures += 1
            log(f"discord send failed (async, {type(exc).__name__}): {str(exc)[:120]}")
    return rest_send(channel_id, text)


def edit_text(channel_id: int, message_id: int, text: str) -> bool:
    """Edit a Discord message. Uses async bridge if available, REST fallback."""
    text = redact_secrets(text)[:2000]
    if (state.discord_client and state.discord_loop
            and state.discord_loop.is_running()
            and state.discord_async_failures < state.DISCORD_ASYNC_MAX_FAILURES):
        try:
            async def _do():
                ch = state.discord_client.get_channel(channel_id)
                if not ch:
                    ch = await state.discord_client.fetch_channel(channel_id)
                msg = await ch.fetch_message(message_id)
                await msg.edit(content=text)
            future = asyncio.run_coroutine_threadsafe(_do(), state.discord_loop)
            future.result(timeout=15)
            state.discord_async_failures = 0
            return True
        except Exception as exc:
            state.discord_async_failures += 1
            log(f"discord edit failed (async, {type(exc).__name__}): {str(exc)[:120]}")
    return rest_edit(channel_id, message_id, text)


def send_file(channel_id: int, file_path: str, caption: str = "") -> bool:
    """Send a file to a Discord channel."""
    caption = redact_secrets(caption)[:2000]
    if (state.discord_client and state.discord_loop
            and state.discord_loop.is_running()
            and state.discord_async_failures < state.DISCORD_ASYNC_MAX_FAILURES):
        try:
            import discord as _dc
            async def _do():
                ch = state.discord_client.get_channel(channel_id)
                if not ch:
                    ch = await state.discord_client.fetch_channel(channel_id)
                await ch.send(content=caption or None, file=_dc.File(file_path))
            future = asyncio.run_coroutine_threadsafe(_do(), state.discord_loop)
            future.result(timeout=60)
            state.discord_async_failures = 0
            return True
        except Exception as exc:
            state.discord_async_failures += 1
            log(f"discord file send failed (async, {type(exc).__name__}): {str(exc)[:120]}")
    return rest_send_file(channel_id, file_path, caption)


def download_attachment(url: str, filename: str, workspace: int) -> Path:
    """Download a Discord attachment and save it to the workspace files directory."""
    fdir = files_dir(workspace)
    fdir.mkdir(parents=True, exist_ok=True)
    save_path = fdir / filename
    if save_path.exists():
        stem, suffix = save_path.stem, save_path.suffix
        ts = datetime.now().strftime("%H%M%S")
        save_path = fdir / f"{stem}_{ts}{suffix}"
    resp = requests.get(url, timeout=60)
    save_path.write_bytes(resp.content)
    log(f"saved discord file: {save_path.name} ({len(resp.content)} bytes)")
    return save_path


def transcribe_voice(file_path: str, fmt: str = "ogg") -> str:
    """Transcribe audio via Chutes Whisper Large V3 STT endpoint."""
    try:
        with open(file_path, "rb") as f:
            b64_audio = base64.b64encode(f.read()).decode("utf-8")

        resp = requests.post(
            "https://chutes-whisper-large-v3.chutes.ai/transcribe",
            headers={
                "Authorization": f"Bearer {CHUTES_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"language": None, "audio_b64": b64_audio},
            timeout=90,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("text", "") if isinstance(data, dict) else str(data)
            if text.strip():
                log(f"whisper transcription ok ({len(text)} chars)")
                return text.strip()
            return "(voice transcription returned empty -- send text instead)"
        log(f"whisper STT failed: status={resp.status_code} body={resp.text[:200]}")
        return "(voice transcription unavailable -- send text instead)"
    except Exception as exc:
        log(f"transcription failed: {str(exc)[:200]}")
        return "(voice transcription unavailable -- send text instead)"
