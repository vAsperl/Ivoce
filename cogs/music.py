import asyncio
import logging
import os
import random
import time

import discord
from discord.ext import commands
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional

try:
    import pomice
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    pomice = None

from cogs.games import CurrencyManager


@dataclass
class PomiceNodeSpec:
    identifier: str
    host: str
    port: int
    password: str
    secure: bool = False
    region: Optional[str] = None

class TransportControls(discord.ui.View):
    LOOP_ORDER = ["off", "single", "all"]
    LOOP_LABELS = {
        "off": "🔁 Off",
        "single": "🔁 Single",
        "all": "🔁 Queue",
    }

    def __init__(self, music_cog, state):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.state = state
        self.loop_button.label = self.LOOP_LABELS.get(state.loop_mode, "Loop: Off")

    def _voice_client(self, interaction):
        if interaction.guild is None:
            return None
        return interaction.guild.voice_client

    def _is_pomice_voice_client(self, voice_client):
        return self.music_cog._is_pomice_player(voice_client)

    def _is_paused(self, voice_client):
        if not voice_client:
            return False
        if self._is_pomice_voice_client(voice_client):
            return voice_client.is_paused
        return voice_client.is_paused()

    def _is_playing(self, voice_client):
        if not voice_client:
            return False
        if self._is_pomice_voice_client(voice_client):
            return voice_client.is_playing
        return voice_client.is_playing()

    async def _set_pause_state(self, voice_client, paused):
        if not voice_client:
            return
        if self._is_pomice_voice_client(voice_client):
            await voice_client.set_pause(paused)
            return
        if paused:
            voice_client.pause()
        else:
            voice_client.resume()

    async def _stop_voice_client(self, voice_client):
        if not voice_client:
            return
        if self._is_pomice_voice_client(voice_client):
            await voice_client.stop()
        else:
            voice_client.stop()

    def sync_play_pause(self, voice_client):
        if self._is_paused(voice_client):
            self.play_pause_button.label = "▶️ Resume"
            self.play_pause_button.style = discord.ButtonStyle.success
        else:
            self.play_pause_button.label = "⏸️ Pause"
            self.play_pause_button.style = discord.ButtonStyle.secondary

    async def _reply(self, interaction, message):
        try:
            await interaction.response.send_message(message, ephemeral=True)
        except discord.errors.InteractionResponded:
            pass

    async def _followup(self, interaction, message):
        try:
            await interaction.followup.send(message, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.secondary)
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._voice_client(interaction)
        if not vc or not (self._is_playing(vc) or self._is_paused(vc)):
            await self._reply(interaction, "Nothing is currently playing.")
            return
        paused = self._is_paused(vc)
        target_pause = not paused
        try:
            await self._set_pause_state(vc, target_pause)
        except Exception:
            return
        if target_pause:
            button.label = "▶️ Resume"
            button.style = discord.ButtonStyle.success
            followup_text = "Playback paused."
        else:
            button.label = "⏸️ Pause"
            button.style = discord.ButtonStyle.secondary
            followup_text = "Playback resumed."
        try:
            await interaction.response.edit_message(view=self)
        except discord.errors.InteractionResponded:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
        await self._followup(interaction, followup_text)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.danger)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._voice_client(interaction)
        if not vc or not (self._is_playing(vc) or self._is_paused(vc)):
            await self._reply(interaction, "Nothing is playing to skip.")
            return
        try:
            await self._stop_voice_client(vc)
        except Exception:
            pass
        await self._reply(interaction, "Skipped to the next track.")

    @discord.ui.button(label="Loop: Off", style=discord.ButtonStyle.primary)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.state:
            await self._reply(interaction, "Loop state unavailable.")
            return
        current = self.state.loop_mode
        idx = self.LOOP_ORDER.index(current)
        next_mode = self.LOOP_ORDER[(idx + 1) % len(self.LOOP_ORDER)]
        self.state.loop_mode = next_mode
        button.label = self.LOOP_LABELS[next_mode]
        embed_kwargs = {"view": self}
        entry = self.state.current_entry
        if entry:
            embed = self.music_cog._build_now_playing_embed(entry, len(self.state.queue), self.state.loop_mode)
            embed_kwargs["embed"] = embed
        edit_success = False
        now = time.time()
        try:
            await interaction.response.edit_message(**embed_kwargs)
            edit_success = True
        except discord.HTTPException:
            if interaction.message:
                try:
                    await interaction.message.edit(**embed_kwargs)
                    edit_success = True
                except discord.HTTPException:
                    pass
        if edit_success and entry:
            entry['last_embed_edit'] = now
        await self._reply(interaction, f"Loop mode set to {next_mode}.")

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.state:
            await self._reply(interaction, "Shuffle state unavailable.")
            return
        async with self.state.lock:
            if not self.state.queue:
                await self._reply(interaction, "Queue is empty, nothing to shuffle.")
                return
            queue_items = list(self.state.queue)
            random.shuffle(queue_items)
            self.state.queue = deque(queue_items)
        await self._reply(interaction, "Queue shuffled.")


class GuildPlaybackState:
    def __init__(self):
        self.queue = deque()
        self.lock = asyncio.Lock()
        self.is_playing = False
        self.current_entry = None
        self.loop_mode = "off"
        self.idle_disconnect_task = None
        self.empty_voice_task = None
        self.manual_disconnect = False


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Music(commands.Cog):
    IDLE_DISCONNECT_DELAY = 15
    EMPTY_VC_SHUTDOWN_DELAY = 30

    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('discord.music')
        self.guild_states = {}
        self.pomice_pool = pomice.NodePool() if pomice else None
        self.pomice_nodes = self._load_pomice_node_specs()
        self._pomice_nodes_ready = False
        self._pomice_nodes_started = False
        self.pomice_player_cls = pomice.Player if pomice else None
        self.play_reward = _env_int("MUSIC_PLAY_REWARD", 10)
        self.play_reward_min_duration = _env_int("MUSIC_REWARD_MIN_SECONDS", 60)
        self.play_reward_repeat_limit = _env_int("MUSIC_REWARD_REPEAT_LIMIT", 3)
        self.play_reward_streaks = {}
        self.play_reward_batch_size = _env_int("MUSIC_REWARD_BATCH_SIZE", 5)
        self.play_reward_batch_amount = _env_int("MUSIC_REWARD_BATCH_AMOUNT", 50)
        self.play_reward_counts = {}
        self.disable_loop_rewards = _env_flag("DISABLE_LOOP_REWARDS", default=False)

    def _load_pomice_node_specs(self):
        raw = os.getenv("POMICE_NODES", "").strip()
        if not raw:
            return []
        specs = []
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [part.strip() for part in chunk.split("|")]
            if len(parts) < 4:
                continue
            identifier, host, port, password = parts[:4]
            secure = False
            region = None
            if len(parts) >= 5:
                secure = parts[4].lower() in ("1", "true", "yes")
            if len(parts) >= 6:
                region = parts[5]
            try:
                port_value = int(port)
            except ValueError:
                continue
            specs.append(PomiceNodeSpec(
                identifier=identifier or "MAIN",
                host=host,
                port=port_value,
                password=password,
                secure=secure,
                region=region,
            ))
        return specs

    async def start_pomice_nodes(self):
        if self._pomice_nodes_started:
            return
        if not self.pomice_pool or not self.pomice_nodes:
            return
        for spec in self.pomice_nodes:
            kwargs = {
                "bot": self.bot,
                "host": spec.host,
                "port": spec.port,
                "password": spec.password,
                "identifier": spec.identifier,
                "secure": spec.secure,
            }
            if spec.region:
                kwargs["region"] = spec.region
            await self.pomice_pool.create_node(**kwargs)
        self._pomice_nodes_ready = True
        self._pomice_nodes_started = True

    def _should_use_pomice(self):
        return bool(pomice and self.pomice_pool and self.pomice_nodes and self._pomice_nodes_ready)

    def _vc_is_playing(self, voice_client):
        if not voice_client:
            return False
        if self._is_pomice_player(voice_client):
            return voice_client.is_playing
        return voice_client.is_playing()

    def _vc_is_paused(self, voice_client):
        if not voice_client:
            return False
        if self._is_pomice_player(voice_client):
            return voice_client.is_paused
        return voice_client.is_paused()

    def _get_state(self, guild):
        if guild is None:
            return None
        state = self.guild_states.get(guild.id)
        if state is None:
            state = GuildPlaybackState()
            self.guild_states[guild.id] = state
        return state

    def _should_leave_voice(self, voice_client):
        if voice_client is None or voice_client.channel is None:
            return False
        members = [
            member for member in voice_client.channel.members
            if member.id != voice_client.user.id and not member.bot
        ]
        return len(members) == 0

    async def _maybe_disconnect_if_empty(self, guild):
        voice_client = guild.voice_client
        if voice_client and self._should_leave_voice(voice_client):
            try:
                await voice_client.disconnect()
            except (discord.HTTPException, discord.Forbidden):
                pass

    def _cancel_idle_disconnect(self, state):
        if not state:
            return
        task = state.idle_disconnect_task
        if task and not task.done():
            task.cancel()
        state.idle_disconnect_task = None

    def _cancel_empty_voice_shutdown(self, state):
        if not state:
            return
        task = state.empty_voice_task
        if task and not task.done():
            task.cancel()
        state.empty_voice_task = None

    def _schedule_idle_disconnect(self, guild, state):
        if not guild or not state:
            return
        self._cancel_idle_disconnect(state)
        self._cancel_empty_voice_shutdown(state)

        async def _task():
            try:
                await asyncio.sleep(self.IDLE_DISCONNECT_DELAY)
                voice_client = guild.voice_client
                if not voice_client or voice_client.channel is None:
                    return
                if self._vc_is_playing(voice_client) or self._vc_is_paused(voice_client):
                    return
                if not self._should_leave_voice(voice_client):
                    return
                await voice_client.disconnect()
            except asyncio.CancelledError:
                return
            except (discord.HTTPException, discord.Forbidden):
                pass

        state.idle_disconnect_task = asyncio.create_task(_task())

    async def _stop_playback_due_to_empty(self, guild, state):
        if not guild or not state:
            return
        async with state.lock:
            pending_entries = list(state.queue)
            state.queue.clear()
            current_entry = state.current_entry
            state.current_entry = None
            state.is_playing = False
        if current_entry:
            current_entry['stopped_due_to_empty_vc'] = True
            self._cancel_now_playing_timestamp_updates(current_entry)
        voice_client = guild.voice_client
        if voice_client:
            if self._vc_is_playing(voice_client):
                voice_client.stop()
            try:
                await voice_client.disconnect()
            except (discord.HTTPException, discord.Forbidden):
                pass

    def _schedule_empty_voice_shutdown(self, guild, state):
        if not guild or not state:
            return
        self._cancel_idle_disconnect(state)
        self._cancel_empty_voice_shutdown(state)

        async def _task():
            try:
                await asyncio.sleep(self.EMPTY_VC_SHUTDOWN_DELAY)
                voice_client = guild.voice_client
                if not voice_client or voice_client.channel is None:
                    return
                if not self._should_leave_voice(voice_client):
                    return
                await self._stop_playback_due_to_empty(guild, state)
            except asyncio.CancelledError:
                return
            except (discord.HTTPException, discord.Forbidden):
                pass
            finally:
                state.empty_voice_task = None

        state.empty_voice_task = asyncio.create_task(_task())

    def _format_progress(self, downloaded, total, eta):
        if not total or total <= 0:
            return "Downloading... 0%"
        pct = min(100, max(0, int(downloaded * 100 / total)))
        bar_len = 20
        filled = int(bar_len * pct / 100)
        bar = "[" + "#" * filled + "-" * (bar_len - filled) + "]"
        eta_text = f" ETA {int(eta)}s" if eta is not None else ""
        return f"Downloading... {bar} {pct}%{eta_text}"

    def _format_duration(self, seconds):
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "Unknown"
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    async def _safe_delete_message(self, message):
        if not message:
            return
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _delete_loading_message(self, entry):
        if not entry:
            return
        msg = entry.pop('loading_message', None)
        await self._safe_delete_message(msg)

    def _build_queue_added_embed(self, entry, position):
        title = entry.get('title') or entry['url']
        metadata = entry.get('metadata') or {}
        embed = discord.Embed(
            title="Track queued",
            description=title,
            color=discord.Color.green()
        )
        embed.add_field(name="Position", value=f"#{position}", inline=True)
        duration = metadata.get('duration')
        if duration:
            embed.add_field(name="Duration", value=self._format_duration(duration), inline=True)
        requester = entry.get('requester')
        if requester:
            embed.add_field(name="Requested by", value=requester.display_name, inline=True)
        uploader = metadata.get('uploader')
        if uploader:
            embed.add_field(name="Uploader", value=uploader, inline=True)
        thumbnail = metadata.get('thumbnail')
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        embed.set_footer(text="Added to queue")
        return embed

    def _build_status_embed(self, title, description=None, *, color=None, footer=None):
        embed = discord.Embed(
            title=title,
            description=description or "",
            color=color or discord.Color.blurple()
        )
        embed.set_footer(text=footer or "Music player")
        return embed

    def _get_elapsed_time(self, entry):
        start_time = entry.get('start_time')
        if not start_time:
            return None
        elapsed = time.time() - start_time
        return max(0.0, elapsed)

    def _build_progress_bar(self, elapsed, duration, length=12):
        if not duration or duration <= 0 or elapsed is None:
            return None
        ratio = min(1.0, max(0.0, elapsed / duration))
        filled = int(length * ratio)
        if filled == 0 and ratio > 0:
            filled = 1
        filled = min(length, filled)
        empty = length - filled
        return "▰" * filled + "▱" * empty

    def _build_progress_value(self, entry):
        metadata = entry.get('metadata') or {}
        duration = metadata.get('duration')
        elapsed = self._get_elapsed_time(entry)
        if duration:
            elapsed = elapsed or 0.0
            elapsed_cap = min(duration, elapsed)
            bar = self._build_progress_bar(elapsed_cap, duration)
            line = f"{self._format_duration(elapsed_cap)} / {self._format_duration(duration)}"
            if bar:
                line += f"\n`{bar}`"
            return line
        if elapsed is not None:
            return f"{self._format_duration(elapsed)} / Unknown duration"
        return "Waiting to start"

    def _build_now_playing_embed(self, entry, queue_length, loop_mode):
        title = entry.get('title') or entry['url']
        metadata = entry.get('metadata') or {}
        link = metadata.get('webpage_url') or entry.get('url')
        embed = discord.Embed(
            title=title,
            url=link,
            description="Now playing",
            color=discord.Color.blurple()
        )
        requester_display = entry['requester'].display_name
        avatar_url = None
        try:
            avatar_url = entry['requester'].display_avatar.url
        except AttributeError:
            avatar_url = None
        embed.set_author(name=requester_display, icon_url=avatar_url)
        upcoming = max(0, queue_length)
        embed.add_field(
            name="Queue length",
            value=f"{upcoming} track(s) waiting",
            inline=True
        )
        embed.add_field(
            name="Loop mode",
            value=loop_mode.capitalize(),
            inline=True
        )
        thumbnail = metadata.get('thumbnail')
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        duration = metadata.get('duration')
        if duration:
            embed.add_field(
                name="Duration",
                value=self._format_duration(duration),
                inline=True
            )
        uploader = metadata.get('uploader')
        if uploader:
            embed.add_field(
                name="Uploader",
                value=uploader,
                inline=True
            )
        progress_value = self._build_progress_value(entry)
        if progress_value:
            embed.add_field(
                name="Progress",
                value=progress_value,
                inline=True
            )
        return embed

    def _format_queue_entry_title(self, entry):
        metadata = entry.get('metadata') or {}
        title = entry.get('title') or metadata.get('title') or entry.get('url') or "Unknown title"
        link = metadata.get('webpage_url') or entry.get('url')
        safe_title = discord.utils.escape_markdown(title)
        if link and (link.startswith("http://") or link.startswith("https://")):
            return f"[{safe_title}]({link})"
        return safe_title

    async def _refresh_now_playing_embed(self, entry, state):
        if not state or state.current_entry is not entry:
            return
        message = entry.get('now_playing_message')
        if not message:
            return
        embed = self._build_now_playing_embed(entry, len(state.queue), state.loop_mode)
        view = entry.get('now_playing_view')
        if view:
            guild = entry.get('guild')
            voice_client = guild.voice_client if guild else None
            view.sync_play_pause(voice_client)
        try:
            await message.edit(embed=embed, view=view)
            entry['last_embed_edit'] = time.time()
        except discord.HTTPException:
            pass

    async def _timestamp_update_loop(self, entry, state):
        try:
            while True:
                await asyncio.sleep(10)
                if not state or state.current_entry is not entry:
                    break
                await self._refresh_now_playing_embed(entry, state)
        except asyncio.CancelledError:
            pass
        finally:
            entry.pop('timestamp_task', None)

    def _start_now_playing_timestamp_updates(self, entry, state):
        if not entry:
            return
        task = entry.get('timestamp_task')
        if task and not task.done():
            return
        entry['timestamp_task'] = asyncio.create_task(self._timestamp_update_loop(entry, state))

    def _cancel_now_playing_timestamp_updates(self, entry):
        if not entry:
            return
        task = entry.pop('timestamp_task', None)
        if task and not task.done():
            task.cancel()

    async def _send_now_playing_embed(self, text_channel, entry, state, embed, view, replace=False):
        existing = entry.get('now_playing_message')
        if replace and existing:
            try:
                await existing.delete()
            except discord.HTTPException:
                pass
            entry.pop('now_playing_message', None)
            existing = None
        now = time.time()
        last_edit = entry.get('last_embed_edit', 0)
        force_refresh = entry.pop('force_embed_refresh', False)
        if existing:
            if not force_refresh and now - last_edit < 5:
                return existing
        if existing:
            try:
                entry['now_playing_view'] = view
                await existing.edit(embed=embed, view=view)
                entry['last_embed_edit'] = now
                self._start_now_playing_timestamp_updates(entry, state)
                return existing
            except discord.HTTPException:
                pass
        msg = await text_channel.send(embed=embed, view=view)
        entry['now_playing_message'] = msg
        entry['last_embed_edit'] = now
        entry['now_playing_view'] = view
        self._start_now_playing_timestamp_updates(entry, state)
        return msg

    def _build_queue_embed(self, state):
        embed = discord.Embed(title="Queue", color=discord.Color.green())
        if not state:
            embed.description = "Nothing is playing right now."
            return embed

        current = state.current_entry
        if current:
            now_requester = current['requester'].display_name
            now_title = self._format_queue_entry_title(current)
            embed.add_field(
                name="Now playing",
                value=f"{now_title}\nRequested by {now_requester}",
                inline=False,
            )
        else:
            embed.description = "Nothing is playing right now."

        queue_lines = []
        for idx, entry in enumerate(list(state.queue)[:10], start=1):
            title = entry.get('title') or entry['url']
            requester = entry['requester'].display_name
            queue_title = self._format_queue_entry_title(entry)
            queue_lines.append(f"{idx}. {queue_title} ({requester})")
        if queue_lines:
            embed.add_field(
                name="Upcoming",
                value="\n".join(queue_lines),
                inline=False,
            )
        elif not current:
            embed.add_field(
                name="Upcoming",
                value="Queue is empty.",
                inline=False,
            )

        embed.set_footer(text=f"Loop mode: {state.loop_mode.capitalize()}, total {len(state.queue)} tracks waiting")
        return embed

    def _build_usage_embed(self, usage, example=None):
        description = f"Usage: {usage}"
        embed = self._build_status_embed(
            "Missing required input",
            description,
            color=discord.Color.orange(),
            footer="Music player",
        )
        if example:
            embed.add_field(name="Example", value=example, inline=False)
        return embed

    @commands.command(aliases=["p"])
    async def play(self, ctx, *, url):
        self.logger.info(f"Play command invoked by {ctx.author} in {ctx.guild.name}")
        if not pomice:
            await ctx.send("Lavalink is not available. Pomice is not installed.")
            return

        if ctx.author.voice is None:
            self.logger.warning(f"{ctx.author} is not in a voice channel.")
            await ctx.send("You are not in a voice channel.")
            return

        voice_channel = ctx.author.voice.channel
        self.logger.info(f"User is in voice channel: {voice_channel.name}")

        if not url or not url.strip():
            await ctx.send("Please provide a URL to play.")
            return
        await self._safe_delete_message(ctx.message)

        entry = {
            'url': url,
            'requester': ctx.author,
            'guild': ctx.guild,
            'voice_channel': voice_channel,
            'text_channel': ctx.channel,
            'title': None,
            'metadata': None,
            'loading_message': None,
            'state': None,
        }
        pomice_track = await self._resolve_pomice_track(entry, ctx)
        if pomice_track:
            entry['pomice_track'] = pomice_track

        state = self._get_state(ctx.guild)
        entry['state'] = state
        state.manual_disconnect = False
        async with state.lock:
            queue_position = len(state.queue) + 1
            state.queue.append(entry)
            should_ack_queue = len(state.queue) > 1 or state.is_playing

        if not should_ack_queue:
            track_line = entry['url'] if not entry.get('title') else self._format_queue_entry_title(entry)
            description = (
                f"{track_line}\n"
                f"Requested by {entry['requester'].display_name}"
            )
            embed = self._build_status_embed(
                "Loading track...",
                description,
                color=discord.Color.orange(),
                footer="Preparing your playback"
            )
            entry['loading_message'] = await ctx.send(embed=embed)
        else:
            embed = self._build_queue_added_embed(entry, queue_position)
            await ctx.send(embed=embed)

        await self._start_next_in_queue(state, ctx.guild)

    @play.error
    async def play_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            embed = self._build_usage_embed("?play <url or search>", "?play https://example.com")
            await ctx.send(embed=embed)
            return
        raise error

    @commands.command(name="clear")
    async def clear(self, ctx):
        self.logger.info("Clear command invoked.")
        state = self._get_state(ctx.guild)
        if not state:
            await ctx.send("Nothing is queued right now.")
            return
        self._cancel_idle_disconnect(state)
        self._cancel_empty_voice_shutdown(state)
        async with state.lock:
            pending_entries = list(state.queue)
            state.queue.clear()
            state.is_playing = False

        current_entry = state.current_entry
        self._cancel_now_playing_timestamp_updates(current_entry)
        state.current_entry = None
        if ctx.voice_client:
            if self._vc_is_playing(ctx.voice_client) or self._vc_is_paused(ctx.voice_client):
                self.logger.info("Stopping playback.")
                if self._is_pomice_player(ctx.voice_client):
                    await ctx.voice_client.stop()
                else:
                    ctx.voice_client.stop()
        embed = self._build_status_embed(
            "Queue cleared",
            "Stopped playback and cleared the queue.",
            color=discord.Color.orange(),
            footer="Use ?play to start a new track"
        )
        await ctx.send(embed=embed)


    @commands.command()
    async def leave(self, ctx):
        self.logger.info("Leave command invoked.")
        state = self._get_state(ctx.guild)
        if not state:
            await ctx.send("Nothing is queued right now.")
            return
        self._cancel_idle_disconnect(state)
        self._cancel_empty_voice_shutdown(state)
        async with state.lock:
            pending_entries = list(state.queue)
            state.queue.clear()
            state.is_playing = False
            state.manual_disconnect = True

        if ctx.voice_client:
            self.logger.info("Disconnecting from voice channel.")
            await ctx.voice_client.disconnect()
        else:
            self.logger.warning("Not in a voice channel.")
            await ctx.send("I am not in a voice channel.")
        current_entry = state.current_entry
        self._cancel_now_playing_timestamp_updates(current_entry)
        state.current_entry = None
        embed = self._build_status_embed(
            "Disconnected",
            "Left voice channel and cleared the queue.",
            color=discord.Color.orange(),
            footer="Use ?play to start a new track"
        )
        await ctx.send(embed=embed)

    @commands.command(name="queue")
    async def queue_list(self, ctx):
        """List the currently playing track plus upcoming songs."""
        state = self._get_state(ctx.guild)
        embed = self._build_queue_embed(state)
        await ctx.send(embed=embed)

    @commands.command(name="remove")
    async def remove_from_queue(self, ctx, pos: int):
        """Remove a track from the queue by position (1-based)."""
        state = self._get_state(ctx.guild)
        if not state:
            await ctx.send("Nothing is queued right now.")
            return
        async with state.lock:
            if not state.queue:
                await ctx.send("Queue is empty.")
                return
            if pos < 1 or pos > len(state.queue):
                await ctx.send(f"Position must be between 1 and {len(state.queue)}.")
                return
            queue_list = list(state.queue)
            removed = queue_list.pop(pos - 1)
            state.queue = deque(queue_list)
        title = removed.get('title') or removed.get('url')
        embed = self._build_status_embed(
            "Removed from queue",
            f"{title}\nRequested by {removed['requester'].display_name}",
            color=discord.Color.orange(),
            footer=f"Removed position #{pos}"
        )
        await ctx.send(embed=embed)

    @remove_from_queue.error
    async def remove_from_queue_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            embed = self._build_usage_embed("?remove <position>", "?remove 2")
            await ctx.send(embed=embed)
            return
        raise error

    @commands.command(name="np")
    async def now_playing_command(self, ctx):
        """Re-send the now-playing embed with controls."""
        state = self._get_state(ctx.guild)
        entry = state.current_entry if state else None
        if not entry:
            await ctx.send("Nothing is currently playing.")
            return

        queue_len = len(state.queue) if state else 0
        embed = self._build_now_playing_embed(entry, queue_len, state.loop_mode if state else "off")
        view = TransportControls(self, state)
        view.sync_play_pause(ctx.guild.voice_client if ctx.guild else None)
        await self._send_now_playing_embed(ctx.channel, entry, state, embed, view, replace=True)

    async def _start_next_in_queue(self, state, guild):
        async with state.lock:
            if state.manual_disconnect:
                return
            if state.is_playing or not state.queue:
                return
            entry = state.queue.popleft()
            state.is_playing = True
            state.current_entry = entry

        self._cancel_idle_disconnect(state)

        voice_channel = entry['voice_channel']
        text_channel = entry['text_channel']

        try:
            await self._play_entry_with_pomice(entry, state, guild, voice_channel, text_channel)
        except Exception as e:
            self.logger.error(f"An error occurred while handling the queue: {e}", exc_info=True)
            await self._delete_loading_message(entry)
            try:
                await text_channel.send(f"Playback failed: {e}")
            except (discord.HTTPException, discord.Forbidden):
                pass
            await self._complete_entry(state, entry)

    def _is_pomice_player(self, voice_client):
        if not pomice or not self.pomice_player_cls:
            return False
        return isinstance(voice_client, self.pomice_player_cls)

    async def _ensure_pomice_player_connection(self, guild, voice_channel):
        if not pomice:
            return None
        player = guild.voice_client
        if player is not None and not self._is_pomice_player(player):
            try:
                await player.disconnect()
            except (discord.HTTPException, discord.Forbidden):
                pass
            player = None
        if player is None:
            player = await voice_channel.connect(cls=pomice.Player)
        elif player.channel != voice_channel:
            await player.move_to(voice_channel)
        return player

    def _extract_pomice_track(self, results):
        if not results:
            return None
        if pomice and isinstance(results, pomice.Playlist):
            return results.tracks[0] if results.tracks else None
        if isinstance(results, list) or isinstance(results, tuple):
            return results[0] if results else None
        return results

    async def _play_entry_with_pomice(self, entry, state, guild, voice_channel, text_channel):
        if not pomice:
            raise RuntimeError("Pomice is not available.")
        player = await self._ensure_pomice_player_connection(guild, voice_channel)
        if player is None:
            raise RuntimeError("Unable to connect to Pomice player.")
        track = entry.get('pomice_track')
        if not track:
            results = await player.get_tracks(query=entry['url'])
            track = self._extract_pomice_track(results)
            if track is None:
                raise RuntimeError("No tracks found for that query.")
            self._apply_pomice_track_metadata(entry, track)
        await player.play(track=track)
        entry['pomice_track'] = track
        entry['start_time'] = time.time()
        await self._delete_loading_message(entry)
        embed = self._build_now_playing_embed(entry, len(state.queue), state.loop_mode)
        view = TransportControls(self, state)
        view.sync_play_pause(guild.voice_client if guild else None)
        entry['force_embed_refresh'] = bool(entry.get('now_playing_message'))
        await self._send_now_playing_embed(text_channel, entry, state, embed, view)
        title = track.title if hasattr(track, "title") else entry.get('title')
        self.logger.info(f"Sent now playing embed for {title}")

    def _apply_pomice_track_metadata(self, entry, track):
        if not track:
            return
        title = getattr(track, "title", None)
        uri = getattr(track, "uri", None)
        author = getattr(track, "author", None)
        length = getattr(track, "length", None)
        thumbnail = getattr(track, "thumbnail", None)
        entry['title'] = title or entry.get('title')
        entry['metadata'] = {
            'title': title,
            'webpage_url': uri or entry.get('url'),
            'url': uri or entry.get('url'),
            'duration': int(length / 1000) if isinstance(length, (int, float)) and length > 0 else None,
            'uploader': author,
            'thumbnail': thumbnail,
            'id': getattr(track, "identifier", None),
        }

    def _reward_track_key(self, entry):
        metadata = entry.get('metadata') or {}
        key = (
            metadata.get('id')
            or metadata.get('webpage_url')
            or metadata.get('url')
            or entry.get('url')
            or entry.get('title')
            or ""
        )
        return key.strip().lower()

    def _get_currency_manager(self):
        games_cog = self.bot.get_cog("Games")
        if games_cog and hasattr(games_cog, "currency"):
            return games_cog.currency
        return CurrencyManager(os.getenv("GAMES_DATAFILE", "games_currency.json"), start_balance=100)

    async def _maybe_award_play_reward(self, entry, elapsed=None):
        if self.play_reward <= 0:
            return
        if self.disable_loop_rewards and entry.get("state") and entry["state"].loop_mode != "off":
            return
        requester = entry.get('requester')
        if not requester:
            return
        metadata = entry.get('metadata') or {}
        duration = metadata.get('duration')
        if not duration or duration < self.play_reward_min_duration:
            return
        if elapsed is None or elapsed + 2 < duration:
            return
        track_key = self._reward_track_key(entry)
        if not track_key:
            return
        streak = self.play_reward_streaks.get(requester.id)
        if streak and streak["key"] == track_key:
            streak["count"] += 1
        else:
            streak = {"key": track_key, "count": 1}
        self.play_reward_streaks[requester.id] = streak
        if streak["count"] > self.play_reward_repeat_limit:
            return
        currency = self._get_currency_manager()
        new_balance = currency.adjust(requester.id, self.play_reward)
        if self.play_reward_batch_size > 0 and self.play_reward_batch_amount > 0:
            new_count = self.play_reward_counts.get(requester.id, 0) + 1
            self.play_reward_counts[requester.id] = new_count
            if new_count % self.play_reward_batch_size == 0:
                multiplier = new_count // self.play_reward_batch_size
                bonus = self.play_reward_batch_amount * multiplier
                new_balance = currency.adjust(requester.id, bonus)
                text_channel = entry.get('text_channel')
                if text_channel:
                    try:
                        embed = discord.Embed(
                            title="Milestone Reward",
                            description=(
                                f"{requester.mention} hit {new_count} songs played.\n"
                                f"Bonus: {bonus} credits."
                            ),
                            color=discord.Color.green(),
                        )
                        embed.add_field(
                            name="Keep It Going",
                            value=(
                                "Finish full-length tracks to stack rewards.\n"
                                "Loop rewards may be disabled by the server."
                            ),
                            inline=False,
                        )
                        await text_channel.send(embed=embed)
                    except (discord.HTTPException, discord.Forbidden):
                        pass
        self.logger.info(
            "Rewarded %s with %s credits for track %s (balance=%s).",
            requester.id,
            self.play_reward,
            track_key,
            new_balance,
        )

    async def _resolve_pomice_track(self, entry, ctx=None):
        if not pomice or not self._should_use_pomice():
            return None
        try:
            node = pomice.NodePool.get_node()
        except Exception:
            return None
        try:
            results = await node.get_tracks(query=entry['url'], ctx=ctx)
            track = self._extract_pomice_track(results)
            if track:
                self._apply_pomice_track_metadata(entry, track)
            return track
        except Exception as exc:
            self.logger.warning("Pomice track lookup failed for queue metadata: %s", exc)
            return None

    async def _complete_entry(self, state, entry):
        if entry and entry.get('stopped_due_to_empty_vc'):
            entry.pop('stopped_due_to_empty_vc', None)
            return
        elapsed = None
        if entry:
            start_time = entry.get('start_time')
            if start_time:
                elapsed = max(0, time.time() - start_time)
        if entry and elapsed is not None:
            await self._maybe_award_play_reward(entry, elapsed=elapsed)
        self._cancel_now_playing_timestamp_updates(entry)
        requeue_front = state.loop_mode == "single"
        requeue_back = state.loop_mode == "all"
        should_requeue = requeue_front or requeue_back
        reused = False
        if should_requeue:
            if entry.get('pomice_track') or entry.get('url'):
                async with state.lock:
                    if requeue_front:
                        state.queue.appendleft(entry)
                    else:
                        state.queue.append(entry)
                reused = True

        async with state.lock:
            state.is_playing = False
            state.current_entry = None

        await self._start_next_in_queue(state, entry['guild'])

        async with state.lock:
            queue_empty = not state.queue and not state.is_playing
            loop_active = state.loop_mode != "off"
        if queue_empty and entry and entry.get('text_channel') and not loop_active:
            try:
                description = self._format_queue_entry_title(entry)
                embed = self._build_status_embed(
                    "Playback finished",
                    description,
                    color=discord.Color.green(),
                    footer="Queue is empty"
                )
                await entry['text_channel'].send(embed=embed)
            except (discord.HTTPException, discord.Forbidden):
                pass
        if queue_empty and not loop_active:
            self._schedule_idle_disconnect(entry['guild'], state)

    async def _on_track_end(self, state, entry, error):
        if error:
            self.logger.error(f"Player error: {error}", exc_info=True)
            await entry['text_channel'].send(f"Player error: {error}")
        await self._complete_entry(state, entry)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                state = self._get_state(member.guild)
                state.manual_disconnect = True
                self._cancel_idle_disconnect(state)
                self._cancel_empty_voice_shutdown(state)
                current_entry = state.current_entry
                self._cancel_now_playing_timestamp_updates(current_entry)
                async with state.lock:
                    state.is_playing = False
                    state.current_entry = None
                return
        if member.bot:
            return
        if before.channel == after.channel:
            return
        guild = member.guild
        voice_client = guild.voice_client
        if not voice_client or voice_client.channel is None:
            return
        state = self._get_state(guild)
        if not self._should_leave_voice(voice_client):
            self._cancel_idle_disconnect(state)
            self._cancel_empty_voice_shutdown(state)
            return
        if self._vc_is_playing(voice_client) or self._vc_is_paused(voice_client):
            self._schedule_empty_voice_shutdown(guild, state)
            return
        self._schedule_idle_disconnect(guild, state)

    @commands.Cog.listener()
    async def on_ready(self):
        await self.start_pomice_nodes()

    @commands.Cog.listener()
    async def on_pomice_track_end(self, player, track, reason):
        if not self._should_use_pomice():
            return
        guild = player.guild
        state = self._get_state(guild)
        if not state:
            return
        entry = state.current_entry
        if not entry:
            return
        await self._complete_entry(state, entry)


async def setup(bot):
        music = Music(bot)
        await bot.add_cog(music)
