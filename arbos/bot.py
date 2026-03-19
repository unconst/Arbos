"""Discord bot — slash commands, message handlers, event routing."""

import asyncio
import subprocess
import sys
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands

from arbos.config import (
    WORKING_DIR, DISCORD_BOT_TOKEN, DISCORD_GUILD_ID,
    workspace_dir, goal_dir, goal_file, state_file, inbox_file,
    goal_runs_dir, ENV_ENC_FILE,
)
from arbos.log import log
from arbos.redact import redact_secrets, reload_env_secrets
from arbos.prompt import log_chat, build_operator_prompt, goal_status_label
from arbos.runner import run_agent_streaming
from arbos.goals import save_goals, summarize_goal
from arbos.discord_api import download_attachment
from arbos.env import (
    list_env_keys, delete_env_key, save_to_encrypted_env, process_pending_env,
)
from arbos.state import GoalState, workspaces, goals_lock
import arbos.state as state


def _kill_child_procs():
    from arbos.main import kill_child_procs
    kill_child_procs()


def run_bot():
    """Run the Discord bot with slash commands and message handlers."""
    if not DISCORD_BOT_TOKEN:
        log("DISCORD_BOT_TOKEN not set; add it to .env and restart")
        sys.exit(1)
    if not DISCORD_GUILD_ID:
        log("DISCORD_GUILD_ID not set; add it to .env and restart")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    guild_obj = discord.Object(id=DISCORD_GUILD_ID)

    class ArbosBot(discord.Client):
        def __init__(self):
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self):
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            self.tree.on_error = self._on_tree_error
            log("discord slash commands synced")

        async def _on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
            log(f"slash command error: {error}")
            msg = f"Error: {str(error)[:1800]}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        async def on_ready(self):
            log(f"discord bot ready as {self.user} (guild={DISCORD_GUILD_ID})")
            guild = self.get_guild(DISCORD_GUILD_ID)
            if not guild:
                return
            for ch in guild.text_channels:
                workspace_dir(ch.id).mkdir(parents=True, exist_ok=True)
                state.channel_names[ch.id] = ch.name
            log(f"ensured workspace dirs for {len(guild.text_channels)} channel(s)")
            for ch in guild.text_channels:
                if ch.name == "general":
                    await ch.send(
                        "**Arbos online.** @Arbos to give me instructions.\n\n"
                        "`/goal <name> <description>` — start a new goal\n"
                        "`/bash <command>` · `/env` · `/restart`"
                    )
                    break

        async def on_guild_channel_create(self, channel):
            if not isinstance(channel, discord.TextChannel):
                return
            if channel.guild.id != DISCORD_GUILD_ID:
                return
            ws_dir = workspace_dir(channel.id)
            ws_dir.mkdir(parents=True, exist_ok=True)
            await channel.send(
                f"**Workspace ready.** This channel is a workspace — everything here is shared.\n\n"
                f"Repos you clone, files you create, and data you generate all live in this workspace "
                f"and are accessible to every goal thread.\n\n"
                f"`/goal <name> <description>` — start a new goal\n"
                f"@mention me to chat or give instructions."
            )
            log(f"workspace created for channel {channel.name} ({channel.id})")

        async def on_message(self, message: discord.Message):
            if message.author == self.user or message.author.bot:
                return
            if not message.guild or message.guild.id != DISCORD_GUILD_ID:
                return
            if message.author.id != message.guild.owner_id:
                return
            if message.content.startswith("/"):
                return

            is_thread = isinstance(message.channel, discord.Thread)
            if is_thread:
                workspace = message.channel.parent_id
                thread_id = message.channel.id
            else:
                workspace = message.channel.id
                thread_id = 0

            user_text = message.content or ""

            if message.attachments:
                for att in message.attachments:
                    saved = download_attachment(att.url, att.filename, workspace)
                    size_kb = att.size / 1024 if att.size else saved.stat().st_size / 1024
                    att_info = f"\n[Sent file: {saved.name}] saved to {saved} ({size_kb:.1f} KB)"
                    is_text_file = False
                    try:
                        content = saved.read_text(errors="strict")
                        if len(content) <= 8000:
                            att_info += f"\n[File contents]:\n{content}"
                            is_text_file = True
                    except (UnicodeDecodeError, ValueError):
                        pass
                    if not is_text_file:
                        att_info += "\n(Binary file -- not included inline. Read it from the saved path if needed.)"
                    user_text += att_info

            if not user_text.strip():
                return

            mentioned = self.user in message.mentions

            replied_to_content = None
            if not mentioned and message.reference and message.reference.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg.author == self.user:
                        mentioned = True
                        replied_to_content = ref_msg.content
                except discord.NotFound:
                    pass

            if not mentioned:
                if is_thread and thread_id:
                    inf = inbox_file(workspace, thread_id)
                    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    entry = f"\n[{timestamp}] {message.author.display_name}: {user_text}\n"
                    existing = inf.read_text() if inf.exists() else ""
                    inf.write_text(existing + entry)
                return

            if replied_to_content:
                user_text = f"[Replying to Arbos message: \"{replied_to_content[:1000]}\"]\n\n{user_text}"

            is_general = not is_thread and message.channel.name == "general"

            log_chat(workspace, "user", user_text[:1000])
            prompt = build_operator_prompt(workspace, user_text, thread_id=thread_id, is_general=is_general)

            thinking_msg = await message.channel.send("thinking...")

            _agent_cwd = str(WORKING_DIR) if is_general else None

            def _run():
                response = run_agent_streaming(message.channel.id, thinking_msg.id, prompt, workspace=workspace, cwd=_agent_cwd)
                log_chat(workspace, "bot", response[:1000])
                if process_pending_env():
                    reload_env_secrets()
                    log("loaded pending env vars from .env.pending")

            await asyncio.to_thread(_run)

    bot = ArbosBot()

    # ── Slash commands ───────────────────────────────────────────────────────

    def _owner_only():
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.guild and interaction.user.id == interaction.guild.owner_id:
                return True
            await interaction.response.send_message("Only the server owner can use this command.", ephemeral=True)
            return False
        return app_commands.check(predicate)

    @bot.tree.command(name="goal", description="Create a new goal thread", guild=guild_obj)
    @_owner_only()
    @app_commands.describe(name="Thread name", message="Goal description")
    async def cmd_thread(interaction: discord.Interaction, name: str, message: str):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use /goal in a text channel, not inside a thread.", ephemeral=True)
            return

        await interaction.response.defer()
        workspace = interaction.channel.id

        try:
            thread = await interaction.channel.create_thread(
                name=name, type=discord.ChannelType.public_thread,
            )
            thread_id = thread.id

            summary = await asyncio.to_thread(summarize_goal, message)

            with goals_lock:
                ws = workspaces.setdefault(workspace, {})
                gs = GoalState(
                    thread_id=thread_id, workspace=workspace,
                    thread_name=name, summary=summary, started=True,
                )
                ws[thread_id] = gs
                gdir = goal_dir(workspace, thread_id)
                gdir.mkdir(parents=True, exist_ok=True)
                goal_file(workspace, thread_id).write_text(message)
                state_file(workspace, thread_id).write_text("")
                inbox_file(workspace, thread_id).write_text("")
                goal_runs_dir(workspace, thread_id).mkdir(parents=True, exist_ok=True)
                save_goals(workspace)

            await thread.send(
                f"{interaction.user.mention} **Goal created**: {summary}\n\n{message}\n\n"
                "Messages here go to the goal's inbox. @mention me to chat directly.\n"
                "`/pause` · `/unpause` · `/force` · `/delay <minutes>` · `/delete`"
            )
            await interaction.followup.send(f"Thread **{name}** created and started: {summary}")
            log(f"goal created ws={workspace} t={thread_id}: {summary}")
        except Exception as exc:
            log(f"thread creation failed: {str(exc)[:500]}")
            await interaction.followup.send(f"Failed to create thread: {str(exc)[:1900]}")

    @bot.tree.command(name="pause", description="Pause this thread's goal", guild=guild_obj)
    @_owner_only()
    async def cmd_pause(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /pause inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with goals_lock:
            ws = workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            if gs.paused:
                await interaction.response.send_message("Already paused.", ephemeral=True)
                return
            gs.paused = True
            save_goals(workspace)
        await interaction.response.send_message(f"Goal paused. Use /unpause to resume.")
        log(f"goal paused ws={workspace} t={thread_id}")

    @bot.tree.command(name="unpause", description="Resume this thread's goal", guild=guild_obj)
    @_owner_only()
    async def cmd_unpause(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /unpause inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with goals_lock:
            ws = workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            if not gs.paused:
                await interaction.response.send_message("Not paused.", ephemeral=True)
                return
            gs.paused = False
            gs.wake.set()
            save_goals(workspace)
        await interaction.response.send_message(f"Goal resumed: {gs.summary}")
        log(f"goal unpaused ws={workspace} t={thread_id}")

    @bot.tree.command(name="force", description="Force the next step to run immediately", guild=guild_obj)
    @_owner_only()
    async def cmd_force(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /force inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with goals_lock:
            ws = workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            if gs.paused:
                await interaction.response.send_message("Goal is paused. Use /unpause first.", ephemeral=True)
                return
            gs.force_next = True
            gs.wake.set()
        await interaction.response.send_message("Forcing next step immediately.")
        log(f"goal forced ws={workspace} t={thread_id}")

    @bot.tree.command(name="delay", description="Set step delay for this thread's goal", guild=guild_obj)
    @_owner_only()
    @app_commands.describe(minutes="Delay between steps in minutes")
    async def cmd_delay(interaction: discord.Interaction, minutes: int):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /delay inside a goal thread.", ephemeral=True)
            return
        if minutes < 0:
            await interaction.response.send_message("Delay must be >= 0.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        seconds = minutes * 60
        with goals_lock:
            ws = workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            gs.delay = seconds
            save_goals(workspace)
        await interaction.response.send_message(f"Delay set to {minutes}m.")
        log(f"goal delay set ws={workspace} t={thread_id} delay={minutes}m ({seconds}s)")

    @bot.tree.command(name="delete", description="Delete this thread's goal", guild=guild_obj)
    @_owner_only()
    async def cmd_delete(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /delete inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with goals_lock:
            ws = workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            gs.stop_event.set()
            gs.wake.set()
            gs.started = False
            bg_thread = gs.thread
            del ws[thread_id]
            save_goals(workspace)
        if bg_thread and bg_thread.is_alive():
            bg_thread.join(timeout=5)
        import shutil
        gdir = goal_dir(workspace, thread_id)
        if gdir.exists():
            shutil.rmtree(gdir, ignore_errors=True)
        await interaction.response.send_message(f"Goal deleted. Removing thread...")
        try:
            await interaction.channel.delete()
        except Exception:
            pass
        log(f"goal deleted ws={workspace} t={thread_id}")

    @bot.tree.command(name="env", description="Manage env vars: /env (list), /env -d KEY (delete), /env KEY VALUE (set)", guild=guild_obj)
    @_owner_only()
    @app_commands.describe(key="Key name, or '-d' to delete (followed by key in value param)", value="Value to set, or key to delete when key is '-d'")
    async def cmd_env(interaction: discord.Interaction, key: str = None, value: str = None):
        import os
        env_path = WORKING_DIR / ".env"
        try:
            if key is None:
                keys = list_env_keys()
                if keys:
                    listing = "\n".join(f"• `{k}`" for k in sorted(keys))
                    await interaction.response.send_message(f"**Environment variables:**\n{listing}", ephemeral=True)
                else:
                    await interaction.response.send_message("No environment variables set.", ephemeral=True)
                return

            if key == "-d":
                if not value:
                    await interaction.response.send_message("Usage: `/env -d KEY`", ephemeral=True)
                    return
                del_key = value
                delete_env_key(del_key)
                await interaction.response.send_message(f"Deleted `{del_key}`.", ephemeral=True)
                log(f"env var deleted via /env: {del_key}")
                return

            if value is None:
                await interaction.response.send_message("Usage: `/env KEY VALUE` to set, `/env -d KEY` to delete, `/env` to list.", ephemeral=True)
                return

            if env_path.exists():
                content = env_path.read_text()
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
                env_path.write_text("\n".join(lines) + "\n")
            elif ENV_ENC_FILE.exists():
                save_to_encrypted_env(key, value)
            else:
                env_path.write_text(f"{key}='{value}'\n")
            os.environ[key] = value
            reload_env_secrets()
            await interaction.response.send_message(f"Set `{key}` successfully.", ephemeral=True)
            log(f"env var set via /env: {key}")
        except Exception as exc:
            await interaction.response.send_message(f"Failed: {str(exc)[:200]}", ephemeral=True)
            log(f"env cmd failed: {str(exc)[:200]}")

    @bot.tree.command(name="restart", description="Restart arbos (pm2)", guild=guild_obj)
    @_owner_only()
    async def cmd_restart(interaction: discord.Interaction):
        from arbos.config import RESTART_FLAG
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /restart in a channel, not a thread.", ephemeral=True)
            return
        await interaction.response.send_message("Restarting -- killing agent and exiting for pm2...")
        log("restart requested via /restart command")
        _kill_child_procs()
        RESTART_FLAG.touch()

    @bot.tree.command(name="bash", description="Run a bash command in the workspace", guild=guild_obj)
    @_owner_only()
    @app_commands.describe(command="The bash command to run")
    async def cmd_bash(interaction: discord.Interaction, command: str):
        await interaction.response.defer()
        is_thread = isinstance(interaction.channel, discord.Thread)
        workspace = interaction.channel.parent_id if is_thread else interaction.channel.id
        bash_cwd = workspace_dir(workspace)
        bash_cwd.mkdir(parents=True, exist_ok=True)

        def _run_bash():
            try:
                result = subprocess.run(
                    command, shell=True, cwd=bash_cwd,
                    capture_output=True, text=True, timeout=120,
                )
                out = result.stdout or ""
                err = result.stderr or ""
                parts = []
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"stderr:\n{err.strip()}")
                if not parts:
                    parts.append("(no output)")
                output = "\n".join(parts)
                output = redact_secrets(output)
                rc = result.returncode
                return rc, output
            except subprocess.TimeoutExpired:
                return -1, "(command timed out after 120s)"
            except Exception as exc:
                return -1, f"Error: {str(exc)[:500]}"

        rc, output = await asyncio.to_thread(_run_bash)
        header = f"$ `{command}` (rc={rc})\n"
        body = f"```\n{output[:1900 - len(header)]}\n```"
        await interaction.followup.send(header + body)
        log(f"bash command: {command!r} rc={rc}")

    @bot.tree.command(name="help", description="Show available commands", guild=guild_obj)
    @_owner_only()
    async def cmd_help(interaction: discord.Interaction):
        if isinstance(interaction.channel, discord.Thread):
            help_text = """**Thread commands:**
• `/pause` - Pause this goal
• `/unpause` - Resume this goal
• `/force` - Force the next step to run immediately
• `/delay` - Set step delay (minutes)
• `/delete` - Delete this goal
• `/help` - Show this help message"""
        else:
            help_text = """**Channel commands:**
• `/goal` - Create a new goal thread (auto-starts)
• `/bash` - Run a bash command in the workspace
• `/env` - Manage env vars (list / set / delete)
• `/restart` - Restart arbos (pm2)
• `/help` - Show this help message"""
        await interaction.response.send_message(help_text)

    # ── Start bot ────────────────────────────────────────────────────────────

    state.discord_client = bot
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state.discord_loop = loop

    async def runner():
        async with bot:
            await bot.start(DISCORD_BOT_TOKEN)

    try:
        loop.run_until_complete(runner())
    except Exception as exc:
        log(f"discord bot error: {str(exc)[:200]}")
    finally:
        loop.close()
