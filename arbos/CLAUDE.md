# Arbos — Agent guidance

## Package structure

Arbos is a Python package (`arbos/`). Key modules:
- `main.py` — entry point, startup orchestration
- `bot.py` — Discord bot, slash commands, message handling
- `goals.py` — goal persistence, agent step loop
- `runner.py` — Claude subprocess management
- `prompt.py` — prompt building, chatlog, operator prompts
- `config.py` — paths, constants, provider configuration
- `state.py` — GoalState dataclass, shared mutable state
- `discord_api.py` — Discord REST/async messaging
- `proxy.py` — Chutes translation proxy (Anthropic ↔ OpenAI)
- `env.py` — encrypted .env management
- `redact.py` — secret redaction
- `log.py` — logging utilities
- `cli.py` — send/sendfile/encrypt CLI subcommands

## Log review tasks

For health checks and log reviews, focus on:
- `context/workspace/*/goals.json` — active goals and their state
- `context/workspace/*/goals/*/STATE.md` — per-goal state
- `context/logs/runs/*/*/` — recent run logs and rollouts
- `context/logs/chat/*/` — workspace chat history (JSONL)
