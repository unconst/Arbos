#!/usr/bin/env bash
#
# Arbos — one-command install
#
# Usage:
#   ./run.sh
#   curl -fsSL <url>/run.sh | bash   (interactive)
#

set -e
set -o pipefail

# ── Colors ───────────────────────────────────────────────────────────────────

if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    GREEN=$'\033[0;32m' RED=$'\033[0;31m' CYAN=$'\033[0;36m'
    BOLD=$'\033[1m' DIM=$'\033[2m' NC=$'\033[0m'
else
    GREEN='' RED='' CYAN='' BOLD='' DIM='' NC=''
fi

ok()  { printf "  ${GREEN}+${NC} %s\n" "$1"; }
err() { printf "  ${RED}x${NC} %s\n" "$1"; }
die() { err "$1"; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# ── Spinner ──────────────────────────────────────────────────────────────────

spin() {
    local pid=$1 msg="$2" i=0 chars='|/-\'
    printf "\033[?25l" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${CYAN}%s${NC} %s" "${chars:$((i%4)):1}" "$msg"
        sleep 0.1 2>/dev/null || sleep 1
        i=$((i+1))
    done
    printf "\033[?25h" 2>/dev/null || true
    wait "$pid" 2>/dev/null; local code=$?
    if [ $code -eq 0 ]; then
        printf "\r  ${GREEN}+${NC} %s\n" "$msg"
    else
        printf "\r  ${RED}x${NC} %s\n" "$msg"
    fi
    return $code
}

run() {
    local msg="$1"; shift
    local tmp_out=$(mktemp) tmp_err=$(mktemp)
    "$@" >"$tmp_out" 2>"$tmp_err" &
    local pid=$!
    if ! spin $pid "$msg"; then
        if [ -s "$tmp_err" ]; then
            printf "\n    ${RED}${BOLD}stderr:${NC}\n"
            while IFS= read -r l; do printf "    ${DIM}%s${NC}\n" "$l"; done < "$tmp_err"
        elif [ -s "$tmp_out" ]; then
            printf "\n    ${RED}${BOLD}output:${NC}\n"
            tail -20 "$tmp_out" | while IFS= read -r l; do printf "    ${DIM}%s${NC}\n" "$l"; done
        fi
        printf "\n"
        rm -f "$tmp_out" "$tmp_err"
        return 1
    fi
    rm -f "$tmp_out" "$tmp_err"
}

# ── Detect context ───────────────────────────────────────────────────────────

REPO_URL="https://github.com/unconst/Arbos.git"
INSTALL_DIR=""

if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -z "$INSTALL_DIR" ] || [ ! -f "$INSTALL_DIR/pyproject.toml" ]; then
    INSTALL_DIR="$PWD/Arbos"
fi

HAS_TTY=false
if [ -t 0 ] || { [ -e /dev/tty ] && (echo >/dev/tty) 2>/dev/null; }; then
    HAS_TTY=true
fi

# ── Banner ───────────────────────────────────────────────────────────────────

printf "\n${CYAN}${BOLD}"
printf "      _         _               \n"
printf "     / \\   _ __| |__   ___  ___ \n"
printf "    / _ \\ | '__| '_ \\ / _ \\/ __|\n"
printf "   / ___ \\| |  | |_) | (_) \\__ \\\\\n"
printf "  /_/   \\_\\_|  |_.__/ \\___/|___/\n"
printf "${NC}\n"

# ── 1. Detect package manager ────────────────────────────────────────────────

pkg_install() {
    if command_exists apt-get; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "$@"
    elif command_exists dnf; then
        sudo dnf install -y -q "$@"
    elif command_exists yum; then
        sudo yum install -y -q "$@"
    elif command_exists pacman; then
        sudo pacman -S --noconfirm --needed "$@"
    elif command_exists brew; then
        brew install "$@"
    else
        die "No supported package manager found (apt/dnf/yum/pacman/brew)"
    fi
}

# ── 2. Install prerequisites ────────────────────────────────────────────────

printf "  ${BOLD}Installing prerequisites${NC}\n\n"

for cmd in git python3 curl; do
    if command_exists "$cmd"; then
        ok "$cmd"
    else
        run "Installing $cmd" pkg_install "$cmd"
        command_exists "$cmd" || die "Failed to install $cmd"
    fi
done

printf "\n"

# ── 3. Clone repo ───────────────────────────────────────────────────────────

printf "  ${BOLD}Cloning repo${NC}\n\n"

if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
    ok "Project already exists at $INSTALL_DIR"
else
    if [ -d "$INSTALL_DIR" ]; then
        die "$INSTALL_DIR exists but has no pyproject.toml — remove it first or set INSTALL_DIR"
    fi
    run "Cloning $REPO_URL → $INSTALL_DIR" git clone "$REPO_URL" "$INSTALL_DIR"
    [ -f "$INSTALL_DIR/pyproject.toml" ] || die "Clone failed — pyproject.toml not found"
fi

printf "\n"

# ── 4. Install tooling ──────────────────────────────────────────────────────

printf "  ${BOLD}Installing tooling${NC}\n\n"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

# uv
if command_exists uv; then
    ok "uv already installed"
else
    run "Installing uv" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command_exists uv || die "uv install failed"
fi

# Claude Code CLI
if command_exists claude; then
    ok "Claude Code already installed"
else
    if command_exists npm; then
        run "Installing Claude Code" npm install -g @anthropic-ai/claude-code
    else
        die "npm required to install Claude Code CLI"
    fi
    command_exists claude || die "'claude' command not found — install via: npm i -g @anthropic-ai/claude-code"
fi

# PATH persistence
SHELL_RC="$HOME/.bashrc"
[[ -n "${ZSH_VERSION:-}" ]] && SHELL_RC="$HOME/.zshrc"
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    if ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        ok "Added ~/.local/bin to PATH in $SHELL_RC"
    fi
fi

printf "\n"

# ── 5. Python environment ───────────────────────────────────────────────────

printf "  ${BOLD}Setting up project${NC}\n\n"

cd "$INSTALL_DIR"

if [ ! -d ".venv" ]; then
    run "Creating Python environment" uv venv .venv
else
    ok "Python environment exists"
fi

source .venv/bin/activate
run "Installing dependencies" uv pip install -e .

mkdir -p context/runs context/chat

printf "\n"

# ── 6. Choose provider ───────────────────────────────────────────────────────

printf "  ${BOLD}LLM Provider${NC}\n\n"

touch "$INSTALL_DIR/.env"

PROVIDER=""
if grep -q "^PROVIDER=" "$INSTALL_DIR/.env" 2>/dev/null; then
    PROVIDER=$(grep "^PROVIDER=" "$INSTALL_DIR/.env" | head -1 | cut -d= -f2 | tr -d "' \"")
    ok "Provider already set: $PROVIDER"
elif [ "$HAS_TTY" = true ]; then
    printf "  ${DIM}Pick your inference backend:${NC}\n\n"
    printf "    ${BOLD}1)${NC} Chutes     ${DIM}— cheap multi-model pool via chutes.ai${NC}\n"
    printf "    ${BOLD}2)${NC} OpenRouter  ${DIM}— Claude Opus 4.6 via openrouter.ai${NC}\n\n"
    printf "  ${CYAN}Choice [1]:${NC} "
    read -r _choice </dev/tty 2>/dev/null || _choice=""
    case "$_choice" in
        2) PROVIDER="openrouter" ;;
        *) PROVIDER="chutes" ;;
    esac
    echo "PROVIDER=$PROVIDER" >> "$INSTALL_DIR/.env"
    ok "Provider set to $PROVIDER"
else
    PROVIDER="chutes"
    echo "PROVIDER=$PROVIDER" >> "$INSTALL_DIR/.env"
    ok "Provider defaulted to chutes (no TTY)"
fi

printf "\n"

# ── 7. API keys ──────────────────────────────────────────────────────────────

printf "  ${BOLD}API Keys${NC}\n\n"

ask_key() {
    local key_name="$1" prompt_text="$2" help_text="$3" required="$4"

    if grep -q "^${key_name}=" "$INSTALL_DIR/.env" 2>/dev/null; then
        ok "$key_name already set"
        return 0
    fi

    if [ -n "${!key_name:-}" ]; then
        echo "${key_name}=${!key_name}" >> "$INSTALL_DIR/.env"
        ok "$key_name saved (from environment)"
        return 0
    fi

    if [ "$HAS_TTY" != true ]; then
        if [ "$required" = "required" ]; then
            die "No TTY — set $key_name in .env or environment and re-run"
        else
            return 0
        fi
    fi

    [ -n "$help_text" ] && printf "  ${DIM}%s${NC}\n\n" "$help_text"
    printf "  ${CYAN}%s:${NC} " "$prompt_text"
    read -r _val </dev/tty 2>/dev/null || _val=""

    if [ -z "$_val" ]; then
        if [ "$required" = "required" ]; then
            die "$key_name is required"
        else
            ok "$key_name skipped"
            return 0
        fi
    fi

    echo "${key_name}=${_val}" >> "$INSTALL_DIR/.env"
    ok "$key_name saved"
}

if [ "$PROVIDER" = "openrouter" ]; then
    ask_key "OPENROUTER_API_KEY" \
        "OpenRouter API key" \
        "Get yours at: https://openrouter.ai/keys" \
        "required"
else
    ask_key "CHUTES_API_KEY" \
        "Chutes API key" \
        "Get yours at: https://chutes.ai — sign up and generate an API key" \
        "required"
fi

printf "\n"

ask_key "TAU_BOT_TOKEN" \
    "Telegram bot token" \
    "Create a bot via @BotFather on Telegram, then paste the token here" \
    "required"

printf "\n"

# ── 7.5. OpenViking context management ──────────────────────────────────────

printf "  ${BOLD}Context Management${NC}\n\n"

OPENVIKING_ENABLED=""
if grep -q "^OPENVIKING_ENABLED=" "$INSTALL_DIR/.env" 2>/dev/null; then
    OPENVIKING_ENABLED=$(grep "^OPENVIKING_ENABLED=" "$INSTALL_DIR/.env" | head -1 | cut -d= -f2 | tr -d "' \"")
    ok "OpenViking already configured: $OPENVIKING_ENABLED"
elif [ "$HAS_TTY" = true ]; then
    printf "  ${DIM}OpenViking provides structured, searchable context management for your agent.${NC}\n"
    printf "  ${DIM}When enabled it replaces flat STATE.md files with a context database.${NC}\n\n"
    printf "    ${BOLD}1)${NC} Enable   ${DIM}— use OpenViking for persistent context${NC}\n"
    printf "    ${BOLD}2)${NC} Disable  ${DIM}— use flat STATE.md files (default)${NC}\n\n"
    printf "  ${CYAN}Choice [2]:${NC} "
    read -r _ov_choice </dev/tty 2>/dev/null || _ov_choice=""
    case "$_ov_choice" in
        1)
            OPENVIKING_ENABLED="true"
            echo "OPENVIKING_ENABLED=true" >> "$INSTALL_DIR/.env"
            ok "OpenViking enabled"

            printf "\n  ${DIM}Do you have an existing OpenViking instance?${NC}\n\n"
            printf "  ${CYAN}OpenViking URL (leave empty to set up locally):${NC} "
            read -r _ov_url </dev/tty 2>/dev/null || _ov_url=""

            if [ -n "$_ov_url" ]; then
                echo "OPENVIKING_URL=$_ov_url" >> "$INSTALL_DIR/.env"
                ok "OpenViking URL saved: $_ov_url"

                printf "\n  ${CYAN}OpenViking API key (leave empty if none):${NC} "
                read -r _ov_key </dev/tty 2>/dev/null || _ov_key=""
                if [ -n "$_ov_key" ]; then
                    echo "OPENVIKING_API_KEY=$_ov_key" >> "$INSTALL_DIR/.env"
                    ok "OpenViking API key saved"
                else
                    ok "No API key (public instance)"
                fi

                run "Installing OpenViking SDK" uv pip install openviking

                # Write ovcli.conf pointing at the remote instance
                mkdir -p "$HOME/.openviking"
                cat > "$HOME/.openviking/ovcli.conf" <<OVCLI
{
  "url": "$_ov_url",
  "timeout": 60.0,
  "output": "table"
}
OVCLI
                ok "Wrote ~/.openviking/ovcli.conf"
            else
                # Local setup: install openviking and start server via pm2
                _ov_url="http://localhost:1933"
                echo "OPENVIKING_URL=$_ov_url" >> "$INSTALL_DIR/.env"

                run "Installing OpenViking" uv pip install openviking

                mkdir -p "$HOME/.openviking"

                # Write ovcli.conf for localhost
                cat > "$HOME/.openviking/ovcli.conf" <<OVCLI
{
  "url": "http://localhost:1933",
  "timeout": 60.0,
  "output": "table"
}
OVCLI
                ok "Wrote ~/.openviking/ovcli.conf"

                # Build ov.conf — need embedding + VLM config
                printf "\n  ${DIM}Local OpenViking needs an embedding model and VLM.${NC}\n"
                printf "  ${DIM}An OpenAI-compatible API key is recommended.${NC}\n\n"

                ask_key "OPENVIKING_VLM_API_KEY" \
                    "OpenAI API key for OpenViking VLM/embeddings" \
                    "Used for semantic indexing. Get one at https://platform.openai.com" \
                    "required"

                _vlm_key=$(grep "^OPENVIKING_VLM_API_KEY=" "$INSTALL_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d "' \"")

                cat > "$HOME/.openviking/ov.conf" <<OVCONF
{
  "storage": {
    "workspace": "$HOME/openviking_workspace"
  },
  "log": {
    "level": "INFO",
    "output": "stdout"
  },
  "embedding": {
    "dense": {
      "api_base": "https://api.openai.com/v1",
      "api_key": "$_vlm_key",
      "provider": "openai",
      "dimension": 3072,
      "model": "text-embedding-3-large"
    },
    "max_concurrent": 10
  },
  "vlm": {
    "api_base": "https://api.openai.com/v1",
    "api_key": "$_vlm_key",
    "provider": "openai",
    "model": "gpt-4o-mini",
    "max_concurrent": 100
  }
}
OVCONF
                ok "Wrote ~/.openviking/ov.conf"

                mkdir -p "$HOME/openviking_workspace"

                # Start openviking-server via pm2
                OV_LAUNCH="$HOME/.openviking/ov-launch.sh"
                cat > "$OV_LAUNCH" <<OVLAUNCH
#!/usr/bin/env bash
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:/usr/local/bin:\$PATH"
export OPENVIKING_CONFIG_FILE="\$HOME/.openviking/ov.conf"
export OPENVIKING_CLI_CONFIG_FILE="\$HOME/.openviking/ovcli.conf"
cd "$INSTALL_DIR"
source .venv/bin/activate
exec openviking-server 2>&1
OVLAUNCH
                chmod +x "$OV_LAUNCH"

                pm2 delete "openviking" 2>/dev/null || true
                pm2 start "$OV_LAUNCH" \
                    --name "openviking" \
                    --log "$INSTALL_DIR/logs/openviking.log" \
                    --time \
                    --restart-delay 5000

                sleep 3
                if pm2 pid "openviking" >/dev/null 2>&1 && [ -n "$(pm2 pid "openviking")" ]; then
                    ok "OpenViking server running on localhost:1933"
                else
                    err "OpenViking server may not have started — check: pm2 logs openviking"
                fi
            fi
            ;;
        *)
            OPENVIKING_ENABLED="false"
            echo "OPENVIKING_ENABLED=false" >> "$INSTALL_DIR/.env"
            ok "OpenViking disabled (using STATE.md)"
            ;;
    esac
else
    OPENVIKING_ENABLED="false"
    echo "OPENVIKING_ENABLED=false" >> "$INSTALL_DIR/.env"
    ok "OpenViking disabled (no TTY)"
fi

printf "\n"

# ── 8. Start Arbos ───────────────────────────────────────────────────────────

printf "  ${BOLD}Starting Arbos${NC}\n\n"

if ! command_exists claude; then
    die "'claude' command not found in PATH — install via: npm i -g @anthropic-ai/claude-code"
fi
ok "Claude Code found at $(which claude)"

LAUNCH_SCRIPT="$INSTALL_DIR/.arbos-launch.sh"
cat > "$LAUNCH_SCRIPT" <<LAUNCH
#!/usr/bin/env bash
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$HOME/.npm-global/bin:/usr/local/bin:\$PATH"
export OPENVIKING_CONFIG_FILE="\$HOME/.openviking/ov.conf"
export OPENVIKING_CLI_CONFIG_FILE="\$HOME/.openviking/ovcli.conf"
cd "$INSTALL_DIR"
set -a; [ -f .env ] && source .env; set +a
source .venv/bin/activate
exec python3 arbos.py 2>&1
LAUNCH
chmod +x "$LAUNCH_SCRIPT"

PM2_NAME="arbos"

# Install pm2 if needed
if ! command_exists pm2; then
    if ! command_exists npm && ! command_exists npx; then
        if command_exists brew; then
            run "Installing Node.js" brew install node
        elif command_exists apt-get; then
            run "Installing Node.js" bash -c "sudo apt-get update -qq && sudo apt-get install -y -qq nodejs npm"
        else
            die "npm/node required for pm2 — install Node.js first"
        fi
    fi
    run "Installing pm2" npm install -g pm2
    command_exists pm2 || die "pm2 install failed"
fi

# Stop existing instance if running
pm2 delete "$PM2_NAME" 2>/dev/null || true

pm2 start "$LAUNCH_SCRIPT" \
    --name "$PM2_NAME" \
    --cwd "$INSTALL_DIR" \
    --log "$INSTALL_DIR/logs/arbos.log" \
    --time \
    --restart-delay 10000

pm2 save 2>/dev/null || true

sleep 2
if pm2 pid "$PM2_NAME" >/dev/null 2>&1 && [ -n "$(pm2 pid "$PM2_NAME")" ]; then
    ok "Arbos running"
else
    err "Arbos may not have started — check logs:"
    printf "    ${DIM}pm2 logs $PM2_NAME${NC}\n"
fi

# ── Done ─────────────────────────────────────────────────────────────────
printf "\n"
printf "  ${GREEN}${BOLD}Arbos is live${NC}\n"
printf "\n"
printf "  ${DIM}logs${NC}     pm2 logs $PM2_NAME\n"
printf "  ${DIM}status${NC}   pm2 status\n"
printf "  ${DIM}restart${NC}  pm2 restart $PM2_NAME\n"
printf "\n"
printf "  ${BOLD}Next steps — open Telegram and message your bot:${NC}\n"
printf "    Just tell it what you want in plain language, e.g.:\n"
printf "    • \"I want you to build a SOTA quant trading system.\"\n"
printf "    • \"What's the status of my trading system?\"\n"
printf "    • \"Set the goal to ...\"\n"
printf "    • \"Send a message to the trading system.\"\n"
printf "    • \"...\"\n"
printf "\n"
