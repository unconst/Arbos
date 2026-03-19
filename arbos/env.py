"""Encrypted .env management — loading, encrypting, decrypting, pending env vars."""

import base64
import os
import sys
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import load_dotenv

_WORKING_DIR = Path(__file__).resolve().parent.parent
_ENV_ENC_FILE = _WORKING_DIR / ".env.enc"
_CONTEXT_DIR = _WORKING_DIR / "context"
_ENV_PENDING_FILE = _CONTEXT_DIR / ".env.pending"
_pending_env_lock = threading.Lock()
_initialized = False


def _derive_fernet_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"arbos-env-v1", iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt_env_file(bot_token: str):
    """Encrypt .env -> .env.enc and delete the plaintext file."""
    env_path = _WORKING_DIR / ".env"
    plaintext = env_path.read_bytes()
    f = Fernet(_derive_fernet_key(bot_token))
    _ENV_ENC_FILE.write_bytes(f.encrypt(plaintext))
    os.chmod(str(_ENV_ENC_FILE), 0o600)
    env_path.unlink()


def decrypt_env_content(bot_token: str) -> str:
    """Decrypt .env.enc and return plaintext (never written to disk)."""
    f = Fernet(_derive_fernet_key(bot_token))
    return f.decrypt(_ENV_ENC_FILE.read_bytes()).decode()


def _load_encrypted_env(bot_token: str) -> bool:
    if not _ENV_ENC_FILE.exists():
        return False
    try:
        content = decrypt_env_content(bot_token)
    except InvalidToken:
        return False
    for line in content.splitlines():
        line = line.split("#")[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    return True


def save_to_encrypted_env(key: str, value: str):
    """Add/update a single key in the encrypted env file."""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token or not _ENV_ENC_FILE.exists():
        return
    try:
        content = decrypt_env_content(bot_token)
    except InvalidToken:
        return
    lines = content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.split("#")[0].strip()
        if stripped.startswith(f"{key}="):
            lines[i] = f"{key}='{value}'"
            updated = True
            break
    if not updated:
        lines.append(f"{key}='{value}'")
    f = Fernet(_derive_fernet_key(bot_token))
    _ENV_ENC_FILE.write_bytes(f.encrypt("\n".join(lines).encode()))
    os.environ[key] = value


def get_env_lines() -> tuple[list[str], str]:
    """Return (lines, source) from whichever env file exists."""
    env_path = _WORKING_DIR / ".env"
    if env_path.exists():
        return env_path.read_text().splitlines(), "plain"
    if _ENV_ENC_FILE.exists():
        bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if bot_token:
            try:
                return decrypt_env_content(bot_token).splitlines(), "enc"
            except InvalidToken:
                pass
    return [], "none"


def write_env_lines(lines: list[str], source: str):
    env_path = _WORKING_DIR / ".env"
    if source == "plain":
        env_path.write_text("\n".join(lines) + "\n")
    elif source == "enc":
        bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if bot_token:
            f = Fernet(_derive_fernet_key(bot_token))
            _ENV_ENC_FILE.write_bytes(f.encrypt("\n".join(lines).encode()))


def list_env_keys() -> list[str]:
    lines, _ = get_env_lines()
    keys = []
    for line in lines:
        stripped = line.split("#")[0].strip()
        if "=" in stripped:
            keys.append(stripped.split("=", 1)[0].strip())
    return keys


def delete_env_key(key: str):
    lines, source = get_env_lines()
    new_lines = [l for l in lines if not l.split("#")[0].strip().startswith(f"{key}=")]
    write_env_lines(new_lines, source)
    os.environ.pop(key, None)


def init_env():
    """Load environment from .env (plaintext) or .env.enc (encrypted). Idempotent."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    env_path = _WORKING_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        return

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if _ENV_ENC_FILE.exists() and bot_token:
        if _load_encrypted_env(bot_token):
            return
        print("ERROR: failed to decrypt .env.enc — wrong DISCORD_BOT_TOKEN?", file=sys.stderr)
        sys.exit(1)

    if _ENV_ENC_FILE.exists() and not bot_token:
        print("ERROR: .env.enc exists but DISCORD_BOT_TOKEN not set.", file=sys.stderr)
        print("Pass it as an env var: DISCORD_BOT_TOKEN=xxx arbos", file=sys.stderr)
        sys.exit(1)


def process_pending_env() -> bool:
    """Pick up env vars written to .env.pending and persist them. Returns True if any loaded."""
    with _pending_env_lock:
        if not _ENV_PENDING_FILE.exists():
            return False
        content = _ENV_PENDING_FILE.read_text().strip()
        _ENV_PENDING_FILE.unlink(missing_ok=True)
        if not content:
            return False

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            os.environ[k] = v

        env_path = _WORKING_DIR / ".env"
        if env_path.exists():
            with open(env_path, "a") as f:
                f.write("\n" + content + "\n")
        elif _ENV_ENC_FILE.exists():
            bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
            if bot_token:
                try:
                    existing = decrypt_env_content(bot_token)
                except InvalidToken:
                    existing = ""
                new_content = existing.rstrip() + "\n" + content + "\n"
                enc = Fernet(_derive_fernet_key(bot_token))
                _ENV_ENC_FILE.write_bytes(enc.encrypt(new_content.encode()))

        return True
