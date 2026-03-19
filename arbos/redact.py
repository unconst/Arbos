"""Secret redaction — strips known secrets and key patterns from text."""

import os
import re

_SECRET_KEY_WORDS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "SEED", "CREDENTIAL"}

_SECRET_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9_\-]{20,}'),
    re.compile(r'sk_[a-zA-Z0-9_\-]{20,}'),
    re.compile(r'sk-proj-[a-zA-Z0-9_\-]{20,}'),
    re.compile(r'sk-or-v1-[a-fA-F0-9]{20,}'),
    re.compile(r'ghp_[a-zA-Z0-9]{20,}'),
    re.compile(r'gho_[a-zA-Z0-9]{20,}'),
    re.compile(r'hf_[a-zA-Z0-9]{20,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'cpk_[a-zA-Z0-9._\-]{20,}'),
    re.compile(r'crsr_[a-zA-Z0-9]{20,}'),
    re.compile(r'dckr_pat_[a-zA-Z0-9_\-]{10,}'),
    re.compile(r'sn\d+_[a-zA-Z0-9_]{10,}'),
    re.compile(r'tpn-[a-zA-Z0-9_\-]{10,}'),
    re.compile(r'wandb_v\d+_[a-zA-Z0-9]{10,}'),
    re.compile(r'basilica_[a-zA-Z0-9]{20,}'),
    re.compile(r'MT[A-Za-z0-9]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{20,}'),
]

_env_secrets: set[str] = set()


def _load_env_secrets() -> set[str]:
    """Build redaction blocklist from env vars whose names suggest secrets."""
    secrets = set()
    for key, val in os.environ.items():
        if len(val) < 16:
            continue
        key_upper = key.upper()
        if any(w in key_upper for w in _SECRET_KEY_WORDS):
            secrets.add(val)
    return secrets


def reload_env_secrets():
    global _env_secrets
    _env_secrets = _load_env_secrets()


def redact_secrets(text: str) -> str:
    """Strip known secrets and common key patterns from outgoing text."""
    for secret in _env_secrets:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text
