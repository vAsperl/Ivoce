import asyncio
import json
import logging
import os
import random
import shlex
import shutil
import tempfile
import time

import discord
from discord.ext import commands, tasks
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional

try:
    import pomice
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    pomice = None

try:
    import yt_dlp
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yt_dlp = None

try:
    import imageio_ffmpeg
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    imageio_ffmpeg = None

from cogs.games import CurrencyManager


def _env_str(name, default):
    value = os.getenv(name, default)
    return value if value is not None else default


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
        state = self.state
        entry = state.current_entry if state else None
        if not entry:
            await self._reply(interaction, "Nothing is playing to skip.")
            return
        requester = entry.get('requester')
        listener_count = self.music_cog._voice_listener_count(vc)
        if listener_count <= 1:
            if requester and interaction.user.id != requester.id:
                entry['force_reward'] = True
            entry['skipped'] = True
            outcome = self.music_cog._build_skip_vote_embed(
                1,
                1,
                status="passed",
                entry=entry,
                voter_mentions=[interaction.user.mention],
            )
            await interaction.channel.send(embed=outcome)
            try:
                await self.music_cog._stop_voice_client(vc)
            except Exception:
                pass
            if state:
                await self.music_cog._reset_skip_vote(state)
            await self._reply(interaction, "Skipped to the next track.")
            return

        async with state.lock:
            if interaction.user.id in state.skip_votes:
                await self._reply(interaction, "You already voted to skip.")
                return
            state.skip_votes.add(interaction.user.id)
            votes = len(state.skip_votes)
            required = self.music_cog._skip_votes_required(listener_count)

        force_skip_cost = self.music_cog._force_skip_cost(state, listener_count)
        embed = self.music_cog._build_skip_vote_embed(
            votes,
            required,
            force_skip_cost=force_skip_cost,
            listener_count=listener_count,
            entry=entry,
            voter_mentions=self.music_cog._skip_voter_mentions(
                interaction.guild, state.skip_votes
            ),
        )
        view = self.music_cog._build_skip_vote_view(state, force_skip_cost)
        await self.music_cog._set_skip_vote_message(state, interaction, embed, view=view)

        if votes >= required:
            if requester and any(voter_id != requester.id for voter_id in state.skip_votes):
                entry['force_reward'] = True
            outcome = self.music_cog._build_skip_vote_embed(
                votes,
                required,
                status="passed",
                force_skip_cost=force_skip_cost,
                listener_count=listener_count,
                entry=entry,
                voter_mentions=self.music_cog._skip_voter_mentions(
                    interaction.guild, state.skip_votes
                ),
            )
            await self.music_cog._set_skip_vote_message(state, interaction, outcome, view=view)
            entry['skipped'] = True
            try:
                await self.music_cog._stop_voice_client(vc)
            except Exception:
                pass
            if state:
                await self.music_cog._reset_skip_vote(state)
            await self._reply(interaction, "Skip vote passed. Skipping...")
            return
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        return

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


class SkipVoteView(discord.ui.View):
    def __init__(self, music_cog, state, cost):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.state = state
        self._set_cost_label(cost)

    def _set_cost_label(self, cost):
        self.force_skip_button.label = f"💸 Force Skip (RM {cost})"

    def _voice_client(self, interaction):
        if interaction.guild is None:
            return None
        return interaction.guild.voice_client

    async def _reply(self, interaction, message):
        try:
            await interaction.response.send_message(message, ephemeral=True)
        except discord.errors.InteractionResponded:
            pass

    @discord.ui.button(label="💸 Force Skip", style=discord.ButtonStyle.danger)
    async def force_skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._voice_client(interaction)
        if not vc or not (self.music_cog._vc_is_playing(vc) or self.music_cog._vc_is_paused(vc)):
            await self._reply(interaction, "Nothing is playing to skip.")
            return
        state = self.state
        entry = state.current_entry if state else None
        if not entry:
            await self._reply(interaction, "Nothing is playing to skip.")
            return
        listener_count = self.music_cog._voice_listener_count(vc)
        cost = self.music_cog._force_skip_cost(state, listener_count)
        currency = self.music_cog._get_currency_manager()
        balance = currency.get_balance(interaction.user.id)
        if balance < cost:
            await self._reply(
                interaction,
                f"You need RM {cost} to force skip. Balance: RM {balance}.",
            )
            return
        currency.adjust(interaction.user.id, -cost)
        if state:
            async with state.lock:
                state.force_skip_uses += 1
        requester = entry.get('requester')
        if requester and interaction.user.id != requester.id:
            entry['force_reward'] = True
        entry['skipped'] = True
        outcome = self.music_cog._build_skip_vote_embed(
            1,
            1,
            status="passed",
            entry=entry,
            voter_mentions=[interaction.user.mention],
        )
        await self.music_cog._set_skip_vote_message(state, interaction, outcome)
        try:
            await self.music_cog._stop_voice_client(vc)
        except Exception:
            pass
        if state:
            await self.music_cog._reset_skip_vote(state)
        await self._reply(interaction, f"Force skip used. RM {cost} charged.")


class QueueView(discord.ui.View):
    def __init__(self, music_cog, state, page=1):
        super().__init__(timeout=120)
        self.music_cog = music_cog
        self.state = state
        self.page = page
        self._sync_buttons()

    def _sync_buttons(self):
        total_pages = self.music_cog._queue_page_count(self.state)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= total_pages

    async def _edit(self, interaction):
        embed = self.music_cog._build_queue_embed(self.state, page=self.page)
        self._sync_buttons()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.errors.InteractionResponded:
            try:
                await interaction.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 1:
            self.page -= 1
        await self._edit(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_pages = self.music_cog._queue_page_count(self.state)
        if self.page < total_pages:
            self.page += 1
        await self._edit(interaction)


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
        self.skip_votes = set()
        self.skip_message = None
        self.skip_view = None
        self.force_skip_uses = 0
        self.backend_disconnect_event = None


class MusicBackendSelector(discord.ui.View):
    def __init__(self, music_cog, guild_id, current_mode):
        super().__init__(timeout=60)
        self.music_cog = music_cog
        self.guild_id = guild_id
        for option in self.backend_select.options:
            option.default = option.value == current_mode

    @discord.ui.select(
        placeholder="Choose the music playback mode",
        options=[
            discord.SelectOption(
                label="Lavalink + yt-dlp fallback",
                value="lavalink",
                description="Use Lavalink first and yt-dlp if playback fails.",
                emoji="🌐",
            ),
            discord.SelectOption(
                label="yt-dlp only",
                value="yt-dlp",
                description="Bypass Lavalink and stream directly through FFmpeg.",
                emoji="🎵",
            ),
        ],
    )
    async def backend_select(self, interaction, select):
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "This selector belongs to another server.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can change the music mode.", ephemeral=True
            )
            return

        mode = select.values[0]
        self.music_cog._set_music_backend_mode(self.guild_id, mode)
        for option in select.options:
            option.default = option.value == mode
        embed = self.music_cog._build_music_mode_embed(mode, changed=True)
        await interaction.response.edit_message(embed=embed, view=self)


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
        self.play_reward_streaks_path = _env_str(
            "MUSIC_REWARD_STREAKS_FILE",
            "data/music_reward_streaks.json",
        )
        self._load_play_reward_streaks()
        self.play_reward_batch_size = _env_int("MUSIC_REWARD_BATCH_SIZE", 5)
        self.play_reward_batch_amount = _env_int("MUSIC_REWARD_BATCH_AMOUNT", 50)
        self.play_reward_counts = {}
        self.disable_loop_rewards = _env_flag("DISABLE_LOOP_REWARDS", default=False)
        self.force_skip_base_cost = _env_int("MUSIC_FORCE_SKIP_BASE_COST", 100)
        self.ffmpeg_executable = self._find_ffmpeg_executable()
        self.music_backend_modes_path = _env_str(
            "MUSIC_BACKEND_MODES_FILE",
            "data/music_backend_modes.json",
        )
        self.music_backend_modes = self._load_music_backend_modes()

    def _load_music_backend_modes(self):
        path = self.music_backend_modes_path
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(guild_id): mode
            for guild_id, mode in data.items()
            if mode in {"lavalink", "yt-dlp"}
        }

    def _save_music_backend_modes(self):
        path = self.music_backend_modes_path
        if not path:
            return
        try:
            dir_path = os.path.dirname(path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.music_backend_modes, fh, indent=2, sort_keys=True)
        except OSError as exc:
            self.logger.warning("Unable to save music backend modes: %s", exc)

    def _get_music_backend_mode(self, guild):
        guild_id = guild.id if guild is not None else None
        return self.music_backend_modes.get(str(guild_id), "lavalink")

    def _set_music_backend_mode(self, guild_id, mode):
        if mode not in {"lavalink", "yt-dlp"}:
            raise ValueError(f"Unsupported music backend mode: {mode}")
        self.music_backend_modes[str(guild_id)] = mode
        self._save_music_backend_modes()

    def _build_music_mode_embed(self, mode, changed=False):
        if mode == "yt-dlp":
            title = "Music mode: yt-dlp only"
            description = (
                "New tracks will bypass Lavalink and stream directly with "
                "yt-dlp and FFmpeg."
            )
        else:
            title = "Music mode: Lavalink + fallback"
            description = (
                "New tracks will use Lavalink first, with yt-dlp and FFmpeg "
                "as the automatic fallback."
            )
        if changed:
            description += "\n\nThe current track is unchanged."
        return self._build_status_embed(
            title,
            description,
            color=discord.Color.blurple(),
            footer="Admin music settings",
        )

    def _find_ffmpeg_executable(self):
        configured = os.getenv("FFMPEG_EXECUTABLE", "").strip()
        if configured:
            return configured
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
        if imageio_ffmpeg:
            try:
                return imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as exc:
                self.logger.warning("Bundled ffmpeg could not be located: %s", exc)
        return "ffmpeg"

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

    def _load_play_reward_streaks(self):
        path = self.play_reward_streaks_path
        if not path or not os.path.exists(path):
            self.play_reward_streaks = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            self.play_reward_streaks = {}
            return
        if not isinstance(data, dict):
            self.play_reward_streaks = {}
            return
        normalized = {}
        for user_id, streak in data.items():
            if not isinstance(streak, dict):
                continue
            key = streak.get("key")
            count = streak.get("count")
            if not isinstance(key, str):
                continue
            try:
                count_value = int(count)
            except (TypeError, ValueError):
                continue
            if count_value <= 0:
                continue
            try:
                user_key = int(user_id)
            except (TypeError, ValueError):
                continue
            normalized[user_key] = {"key": key, "count": count_value}
        self.play_reward_streaks = normalized

    def _save_play_reward_streaks(self):
        path = self.play_reward_streaks_path
        if not path:
            return
        try:
            dir_path = os.path.dirname(path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            data = {str(user_id): streak for user_id, streak in self.play_reward_streaks.items()}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

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
            try:
                await self.pomice_pool.create_node(**kwargs)
                self._pomice_nodes_ready = True
            except Exception as exc:
                self.logger.warning(
                    "Unable to connect to Lavalink node %s: %s",
                    spec.identifier,
                    exc,
                )
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

    async def _stop_voice_client(self, voice_client):
        if not voice_client:
            return
        if self._is_pomice_player(voice_client):
            await voice_client.stop()
        else:
            voice_client.stop()

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
        return not any(
            not member.bot for member in voice_client.channel.members
        )

    def _voice_listener_count(self, voice_client):
        if voice_client is None or voice_client.channel is None:
            return 0
        return sum(1 for member in voice_client.channel.members if not member.bot)

    def _skip_votes_required(self, listener_count):
        return max(1, (listener_count // 2) + 1)

    def _force_skip_cost(self, state, listener_count):
        skip_uses = getattr(state, "force_skip_uses", 0)
        multiplier = 2 ** skip_uses
        user_multiplier = max(1, listener_count - 1)
        return self.force_skip_base_cost * multiplier * user_multiplier

    def _skip_voter_mentions(self, guild, voter_ids):
        mentions = []
        for voter_id in voter_ids:
            member = guild.get_member(voter_id) if guild else None
            mentions.append(member.mention if member else f"<@{voter_id}>")
        return mentions

    def _build_skip_vote_embed(
        self,
        votes,
        required,
        status=None,
        force_skip_cost=None,
        listener_count=None,
        entry=None,
        voter_mentions=None,
    ):
        if status == "passed":
            title = "Track Skipped"
            color = discord.Color.green()
            description = self._format_queue_entry_title(entry) if entry else "Unknown track"
        elif status == "failed":
            title = "Skip Vote Failed"
            color = discord.Color.red()
            description = "Not enough votes to skip."
        else:
            title = "Skip Vote In Progress"
            color = discord.Color.orange()
            description = self._format_queue_entry_title(entry) if entry else "Vote to skip the current track."
        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="Votes", value=f"{votes}/{required}", inline=True)
        embed.add_field(name="Needed", value="More than half", inline=True)
        if voter_mentions:
            embed.add_field(
                name="Skipped by" if status == "passed" else "Voted to skip",
                value="\n".join(voter_mentions),
                inline=False,
            )
        if entry:
            thumbnail = (entry.get('metadata') or {}).get('thumbnail')
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
        return embed

    def _build_skip_vote_view(self, state, force_skip_cost):
        return SkipVoteView(self, state, force_skip_cost)

    async def _set_skip_vote_message(self, state, interaction, embed, view=None):
        if not state:
            return
        if state.skip_message:
            try:
                await state.skip_message.edit(embed=embed, view=view)
                state.skip_view = view
                return
            except (discord.HTTPException, discord.Forbidden):
                state.skip_message = None
                state.skip_view = None
        try:
            state.skip_message = await interaction.channel.send(embed=embed, view=view)
            state.skip_view = view
        except (discord.HTTPException, discord.Forbidden, AttributeError):
            state.skip_message = None
            state.skip_view = None

    async def _reset_skip_vote(self, state):
        if not state:
            return
        message = None
        view = None
        async with state.lock:
            state.skip_votes.clear()
            message = state.skip_message
            view = state.skip_view
            state.skip_message = None
            state.skip_view = None
        if message and view:
            for item in view.children:
                item.disabled = True
            try:
                await message.edit(view=view)
            except (discord.HTTPException, discord.Forbidden):
                pass

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

    def _build_playlist_added_embed(self, name, count, position, requester):
        title = name or "Playlist"
        embed = discord.Embed(
            title="Playlist queued",
            description=title,
            color=discord.Color.green()
        )
        embed.add_field(name="Tracks added", value=str(count), inline=True)
        embed.add_field(name="Starting position", value=f"#{position}", inline=True)
        if requester:
            embed.add_field(name="Requested by", value=requester.display_name, inline=True)
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
        backend = entry.get('playback_backend')
        if backend:
            embed.add_field(
                name="Playback backend",
                value=backend,
                inline=True,
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
                await self._maybe_autoadvance_if_stopped(entry, state)
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

    def _update_stop_tracker(self, entry, voice_client):
        if not entry or not voice_client:
            return
        if self._vc_is_playing(voice_client) or self._vc_is_paused(voice_client):
            entry.pop('last_stop_time', None)
            return
        entry.setdefault('last_stop_time', time.time())

    async def _maybe_autoadvance_if_stopped(self, entry, state):
        if not entry or not state or state.current_entry is not entry:
            return
        if entry.get('_auto_advancing'):
            return
        guild = entry.get('guild')
        voice_client = guild.voice_client if guild else None
        if not voice_client:
            return
        self._update_stop_tracker(entry, voice_client)
        if self._vc_is_playing(voice_client) or self._vc_is_paused(voice_client):
            return

        metadata = entry.get('metadata') or {}
        duration = metadata.get('duration')
        elapsed = self._get_elapsed_time(entry)
        if duration and elapsed is not None and elapsed + 2 < duration:
            return

        if duration is None:
            last_stop = entry.get('last_stop_time')
            if last_stop is None:
                return
            if time.time() - last_stop < 10:
                return
            if elapsed is not None and elapsed < 30:
                return

        entry['_auto_advancing'] = True
        try:
            self.logger.info("Auto-advancing track after stop: %s", entry.get('title') or entry.get('url'))
            await self._complete_entry(state, entry)
        finally:
            entry.pop('_auto_advancing', None)

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

    def _queue_page_count(self, state, page_size=10):
        if not state:
            return 1
        total = len(state.queue)
        return max(1, (total + page_size - 1) // page_size)

    def _build_queue_embed(self, state, page=1):
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

        page_size = 10
        total_pages = self._queue_page_count(state, page_size=page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = start + page_size
        queue_lines = []
        for idx, entry in enumerate(list(state.queue)[start:end], start=start + 1):
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

        footer = f"Loop mode: {state.loop_mode.capitalize()}, total {len(state.queue)} tracks waiting"
        if total_pages > 1:
            footer += f" • Page {page}/{total_pages}"
        embed.set_footer(text=footer)
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
        if not pomice and not yt_dlp:
            await ctx.send("Music playback is unavailable. Install pomice or yt-dlp.")
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
        backend_mode = self._get_music_backend_mode(ctx.guild)
        if backend_mode == "yt-dlp" and not yt_dlp:
            await ctx.send("yt-dlp mode is selected, but yt-dlp is not installed.")
            return
        await self._safe_delete_message(ctx.message)

        base_entry = {
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
        entries = []
        results = None
        if backend_mode == "lavalink":
            results = await self._resolve_pomice_results(url, ctx)
        playlist = results if pomice and isinstance(results, pomice.Playlist) else None
        if playlist and getattr(playlist, "tracks", None):
            entries = [self._build_entry_from_track(base_entry, track) for track in playlist.tracks]
        else:
            entry = dict(base_entry)
            pomice_track = None
            if results is not None:
                pomice_track = self._extract_pomice_track(results)
            elif backend_mode == "lavalink":
                pomice_track = await self._resolve_pomice_track(entry, ctx)
            if pomice_track:
                entry['pomice_track'] = pomice_track
                if not entry.get('metadata'):
                    self._apply_pomice_track_metadata(entry, pomice_track)
            entries = [entry]

        state = self._get_state(ctx.guild)
        state.manual_disconnect = False
        async with state.lock:
            queue_position = len(state.queue) + 1
            should_ack_queue = len(state.queue) > 0 or state.is_playing
            for entry in entries:
                entry['state'] = state
                state.queue.append(entry)

        first_entry = entries[0]
        if playlist and entries:
            playlist_name = getattr(playlist, "name", None) or getattr(playlist, "title", None)
            if not should_ack_queue:
                description = (
                    f"{playlist_name or 'Playlist'}\n"
                    f"Requested by {first_entry['requester'].display_name}"
                )
                embed = self._build_status_embed(
                    "Loading playlist...",
                    description,
                    color=discord.Color.orange(),
                    footer="Preparing your playback"
                )
                first_entry['loading_message'] = await ctx.send(embed=embed)
            else:
                embed = self._build_playlist_added_embed(
                    playlist_name,
                    len(entries),
                    queue_position,
                    first_entry.get('requester'),
                )
                await ctx.send(embed=embed)
        else:
            if not should_ack_queue:
                track_line = first_entry['url'] if not first_entry.get('title') else self._format_queue_entry_title(first_entry)
                description = (
                    f"{track_line}\n"
                    f"Requested by {first_entry['requester'].display_name}"
                )
                embed = self._build_status_embed(
                    "Loading track...",
                    description,
                    color=discord.Color.orange(),
                    footer="Preparing your playback"
                )
                first_entry['loading_message'] = await ctx.send(embed=embed)
            else:
                embed = self._build_queue_added_embed(first_entry, queue_position)
                await ctx.send(embed=embed)

        await self._start_next_in_queue(state, ctx.guild)

    @commands.command(name="musicmode", aliases=["musicbackend"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def music_mode(self, ctx):
        """Choose Lavalink-with-fallback or standalone yt-dlp playback."""
        mode = self._get_music_backend_mode(ctx.guild)
        embed = self._build_music_mode_embed(mode)
        view = MusicBackendSelector(self, ctx.guild.id, mode)
        await ctx.send(embed=embed, view=view)

    @music_mode.error
    async def music_mode_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Only server administrators can change the music mode.")
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Music mode can only be changed inside a server.")
            return
        raise error

    @play.error
    async def play_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            prefix = ctx.prefix or "?"
            embed = self._build_usage_embed(
                f"{prefix}play <url or search>",
                f"{prefix}play https://example.com",
            )
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
            footer=f"Use {ctx.prefix or '?'}play to start a new track"
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
            return
        current_entry = state.current_entry
        self._cancel_now_playing_timestamp_updates(current_entry)
        state.current_entry = None
        embed = self._build_status_embed(
            "Disconnected",
            "Left voice channel and cleared the queue.",
            color=discord.Color.orange(),
            footer=f"Use {ctx.prefix or '?'}play to start a new track"
        )
        await ctx.send(embed=embed)

    @commands.command(name="queue", aliases=["q"])
    async def queue_list(self, ctx):
        """List the currently playing track plus upcoming songs."""
        state = self._get_state(ctx.guild)
        embed = self._build_queue_embed(state, page=1)
        view = None
        if state and len(state.queue) > 10:
            view = QueueView(self, state, page=1)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="loop")
    async def loop_command(self, ctx, mode: str = None):
        """Set the loop mode, or cycle it when no mode is provided."""
        state = self._get_state(ctx.guild)
        modes = ("off", "single", "all")
        mode_aliases = {
            "track": "single",
            "song": "single",
            "queue": "all",
        }

        if mode is None:
            current = state.loop_mode if state.loop_mode in modes else "off"
            mode = modes[(modes.index(current) + 1) % len(modes)]
        else:
            mode = mode_aliases.get(mode.lower(), mode.lower())
            if mode not in modes:
                prefix = ctx.prefix or "?"
                embed = self._build_usage_embed(
                    f"{prefix}loop [off|single|all]",
                    f"{prefix}loop single",
                )
                await ctx.send(embed=embed)
                return

        state.loop_mode = mode
        if state.current_entry:
            await self._refresh_now_playing_embed(state.current_entry, state)

        descriptions = {
            "off": "Looping is disabled.",
            "single": "The current track will repeat.",
            "all": "The full queue will repeat.",
        }
        embed = self._build_status_embed(
            f"Loop: {mode.capitalize()}",
            descriptions[mode],
            color=discord.Color.blurple(),
            footer="Modes: Off • Single • All",
        )
        await ctx.send(embed=embed)

    @commands.command(name="skip", aliases=["s"])
    async def skip_command(self, ctx):
        """Vote to skip the current track."""
        voice_client = ctx.guild.voice_client if ctx.guild else None
        if not voice_client or not (
            self._vc_is_playing(voice_client) or self._vc_is_paused(voice_client)
        ):
            await ctx.send("Nothing is playing to skip.")
            return

        state = self._get_state(ctx.guild)
        entry = state.current_entry if state else None
        if not entry:
            await ctx.send("Nothing is playing to skip.")
            return

        requester = entry.get('requester')
        listener_count = self._voice_listener_count(voice_client)
        if listener_count <= 1:
            if requester and ctx.author.id != requester.id:
                entry['force_reward'] = True
            entry['skipped'] = True
            outcome = self._build_skip_vote_embed(
                1,
                1,
                status="passed",
                entry=entry,
                voter_mentions=[ctx.author.mention],
            )
            await ctx.send(embed=outcome)
            try:
                await self._stop_voice_client(voice_client)
            except Exception:
                pass
            await self._reset_skip_vote(state)
            return

        async with state.lock:
            if ctx.author.id in state.skip_votes:
                await ctx.send("You already voted to skip.")
                return
            state.skip_votes.add(ctx.author.id)
            votes = len(state.skip_votes)
            required = self._skip_votes_required(listener_count)

        force_skip_cost = self._force_skip_cost(state, listener_count)
        embed = self._build_skip_vote_embed(
            votes,
            required,
            force_skip_cost=force_skip_cost,
            listener_count=listener_count,
            entry=entry,
            voter_mentions=self._skip_voter_mentions(ctx.guild, state.skip_votes),
        )
        view = self._build_skip_vote_view(state, force_skip_cost)
        await self._set_skip_vote_message(state, ctx, embed, view=view)

        if votes >= required:
            if requester and any(voter_id != requester.id for voter_id in state.skip_votes):
                entry['force_reward'] = True
            outcome = self._build_skip_vote_embed(
                votes,
                required,
                status="passed",
                force_skip_cost=force_skip_cost,
                listener_count=listener_count,
                entry=entry,
                voter_mentions=self._skip_voter_mentions(ctx.guild, state.skip_votes),
            )
            await self._set_skip_vote_message(state, ctx, outcome, view=view)
            entry['skipped'] = True
            try:
                await self._stop_voice_client(voice_client)
            except Exception:
                pass
            await self._reset_skip_vote(state)

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
            prefix = ctx.prefix or "?"
            embed = self._build_usage_embed(
                f"{prefix}remove <position>",
                f"{prefix}remove 2",
            )
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

    @commands.command()
    async def vcdebug(self, ctx):
        if ctx.author.id != 255365914898333707:
            embed = self._build_status_embed(
                "Access denied",
                "You don't have access to this command.",
                color=discord.Color.red(),
                footer="Voice debug",
            )
            await ctx.send(embed=embed)
            return
        voice_client = ctx.guild.voice_client if ctx.guild else None
        if not voice_client or not voice_client.channel:
            embed = self._build_status_embed(
                "VC Debug",
                "Bot is not connected to a voice channel.",
                color=discord.Color.orange(),
                footer="Voice debug",
            )
            await ctx.send(embed=embed)
            return
        channel = voice_client.channel
        members = list(channel.members)
        human_members = [m for m in members if not m.bot]
        bot_members = [m for m in members if m.bot]
        embed = self._build_status_embed(
            "VC Debug",
            f"Channel: {channel.name} ({channel.id})",
            color=discord.Color.blurple(),
            footer="Voice debug",
        )
        embed.add_field(name="Total Members", value=str(len(members)), inline=True)
        embed.add_field(name="Humans", value=str(len(human_members)), inline=True)
        embed.add_field(name="Bots", value=str(len(bot_members)), inline=True)
        if human_members:
            names = ", ".join(m.display_name for m in human_members[:20])
            if len(human_members) > 20:
                names += " ..."
            embed.add_field(name="Humans List", value=names, inline=False)
        if bot_members:
            names = ", ".join(m.display_name for m in bot_members[:20])
            if len(bot_members) > 20:
                names += " ..."
            embed.add_field(name="Bots List", value=names, inline=False)
        await ctx.send(embed=embed)

    async def _start_next_in_queue(self, state, guild):
        await self._reset_skip_vote(state)
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
            if self._get_music_backend_mode(guild) == "yt-dlp":
                entry['standalone_ytdlp'] = True
                await self._play_entry_with_ytdlp(
                    entry, state, guild, voice_channel, text_channel
                )
                return
            try:
                await self._play_entry_with_pomice(entry, state, guild, voice_channel, text_channel)
            except Exception as lavalink_error:
                self.logger.warning(
                    "Lavalink playback failed for %s; trying yt-dlp/ffmpeg: %s",
                    entry.get('url'),
                    lavalink_error,
                )
                if not await self._switch_entry_to_fallback(
                    state, entry, reason=str(lavalink_error)
                ):
                    return
        except Exception as e:
            self.logger.error(f"An error occurred while handling the queue: {e}", exc_info=True)
            await self._delete_loading_message(entry)
            try:
                await text_channel.send(f"Playback failed: {e}")
            except (discord.HTTPException, discord.Forbidden):
                pass
            await self._complete_entry(state, entry)

    async def _ensure_native_voice_connection(self, guild, voice_channel):
        voice_client = guild.voice_client
        if voice_client is not None and self._is_pomice_player(voice_client):
            state = self._get_state(guild)
            disconnect_event = asyncio.Event()
            state.backend_disconnect_event = disconnect_event
            try:
                await voice_client.disconnect()
                try:
                    await asyncio.wait_for(disconnect_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "Discord did not confirm the Lavalink voice disconnect within 5 seconds."
                    )
            finally:
                state.backend_disconnect_event = None
            voice_client = None
        if voice_client is None:
            voice_client = await voice_channel.connect(timeout=15, reconnect=False)
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
        return voice_client

    def _extract_ytdlp_info(self, query):
        if not yt_dlp:
            raise RuntimeError("yt-dlp is not installed.")
        if not query.startswith(("http://", "https://")):
            query = f"ytsearch1:{query}"
        options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(query, download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        if entries is not None:
            info = next((item for item in entries if item), None)
        if not info or not info.get("url"):
            raise RuntimeError("yt-dlp could not find a playable audio stream.")
        return info

    async def _play_entry_with_ytdlp(self, entry, state, guild, voice_channel, text_channel):
        standalone = bool(entry.get('standalone_ytdlp'))
        await self._update_fallback_status(
            entry,
            "yt-dlp: resolving track" if standalone else "Fallback: resolving track",
            "yt-dlp is locating a playable audio stream...",
            discord.Color.orange(),
        )
        info = await asyncio.to_thread(self._extract_ytdlp_info, entry['url'])
        await self._update_fallback_status(
            entry,
            "yt-dlp: connecting" if standalone else "Fallback: connecting",
            "Audio stream found. Connecting through Discord native voice...",
            discord.Color.orange(),
        )
        voice_client = await self._ensure_native_voice_connection(guild, voice_channel)
        metadata = {
            'title': info.get('title'),
            'webpage_url': info.get('webpage_url') or info.get('original_url') or entry['url'],
            'url': info.get('webpage_url') or info.get('original_url') or entry['url'],
            'duration': info.get('duration'),
            'uploader': info.get('uploader') or info.get('channel'),
            'thumbnail': info.get('thumbnail'),
            'id': info.get('id'),
        }
        entry['metadata'] = metadata
        entry['title'] = metadata['title'] or entry.get('title') or entry['url']
        entry['playback_backend'] = 'yt-dlp/ffmpeg'

        before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        http_headers = info.get('http_headers') or {}
        if http_headers:
            header_blob = "".join(
                f"{name}: {value}\r\n"
                for name, value in http_headers.items()
                if value is not None and name.lower() != "accept-encoding"
            )
            if header_blob:
                before_options += f" -headers {shlex.quote(header_blob)}"

        ffmpeg_stderr = tempfile.TemporaryFile()
        entry['ffmpeg_stderr'] = ffmpeg_stderr
        source = discord.FFmpegPCMAudio(
            info['url'],
            executable=self.ffmpeg_executable,
            stderr=ffmpeg_stderr,
            before_options=before_options,
            options="-vn",
        )
        loop = asyncio.get_running_loop()

        def after_playback(error):
            async def finish():
                playback_error = error
                stderr_text = ""
                stderr_handle = entry.pop('ffmpeg_stderr', None)
                if stderr_handle:
                    try:
                        stderr_handle.flush()
                        stderr_handle.seek(0)
                        stderr_text = stderr_handle.read().decode(errors='replace').strip()
                    except (OSError, ValueError):
                        pass
                    finally:
                        stderr_handle.close()
                if playback_error and stderr_text:
                    playback_error = RuntimeError(
                        f"{playback_error}\nffmpeg stderr:\n{stderr_text[-3500:]}"
                    )
                if playback_error:
                    error_text = str(playback_error).strip() or type(playback_error).__name__
                    entry['ffmpeg_failure_text'] = error_text
                    await self._update_fallback_status(
                        entry,
                        "Fallback failed",
                        error_text[-4000:],
                        discord.Color.red(),
                    )
                if state.current_entry is entry:
                    await self._on_track_end(state, entry, playback_error)

            asyncio.run_coroutine_threadsafe(finish(), loop)

        await self._update_fallback_status(
            entry,
            "yt-dlp: starting ffmpeg" if standalone else "Fallback: starting ffmpeg",
            f"Starting **{discord.utils.escape_markdown(entry['title'])}**...",
            discord.Color.orange(),
        )
        voice_client.play(source, after=after_playback)
        entry['start_time'] = time.time()
        await asyncio.sleep(1)
        if not voice_client.is_playing() and not voice_client.is_paused():
            failure_text = entry.pop('ffmpeg_failure_text', None)
            raise RuntimeError(
                failure_text or "ffmpeg stopped before fallback playback could begin."
            )
        await self._delete_loading_message(entry)
        embed = self._build_now_playing_embed(entry, len(state.queue), state.loop_mode)
        view = TransportControls(self, state)
        view.sync_play_pause(voice_client)
        entry['force_embed_refresh'] = bool(entry.get('now_playing_message'))
        await self._send_now_playing_embed(text_channel, entry, state, embed, view)
        await self._update_fallback_status(
            entry,
            "yt-dlp active" if standalone else "Fallback active",
            (
                f"Now playing **{discord.utils.escape_markdown(entry['title'])}**\n"
                "Source: yt-dlp • Audio: ffmpeg"
            ),
            discord.Color.green(),
        )
        self.logger.info("Playing with yt-dlp/ffmpeg: %s", entry['title'])

    async def _update_fallback_status(self, entry, title, description, color):
        text_channel = entry.get('text_channel')
        if not text_channel:
            return
        embed = self._build_status_embed(
            title,
            description,
            color=color,
            footer=(
                "Standalone yt-dlp playback"
                if entry.get('standalone_ytdlp')
                else "Lavalink fallback progress"
            ),
        )
        message = entry.get('fallback_status_message')
        if message:
            try:
                await message.edit(embed=embed)
                return
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                entry.pop('fallback_status_message', None)
        try:
            entry['fallback_status_message'] = await text_channel.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden):
            pass

    async def _switch_entry_to_fallback(self, state, entry, reason=None):
        if not state or not entry or state.current_entry is not entry:
            return False
        if entry.get('playback_backend') == 'yt-dlp/ffmpeg':
            return True
        if entry.get('_fallback_attempted') or entry.get('_switching_to_fallback'):
            return False

        entry['_fallback_attempted'] = True
        entry['_switching_to_fallback'] = True
        if reason:
            self.logger.warning(
                "Switching %s to yt-dlp/ffmpeg after Lavalink failure: %s",
                entry.get('title') or entry.get('url'),
                reason,
            )
        try:
            await self._play_entry_with_ytdlp(
                entry,
                state,
                entry['guild'],
                entry['voice_channel'],
                entry['text_channel'],
            )
            return True
        except Exception as exc:
            stderr_handle = entry.pop('ffmpeg_stderr', None)
            if stderr_handle:
                try:
                    stderr_handle.close()
                except OSError:
                    pass
            error_text = str(exc).strip() or type(exc).__name__
            self.logger.error("yt-dlp/ffmpeg fallback failed: %s", error_text, exc_info=True)
            await self._update_fallback_status(
                entry,
                "Fallback failed",
                error_text,
                discord.Color.red(),
            )
            try:
                await entry['text_channel'].send(f"Fallback playback failed: {error_text}")
            except (discord.HTTPException, discord.Forbidden):
                pass
            if state.current_entry is entry:
                await self._complete_entry(state, entry)
            return False
        finally:
            entry.pop('_switching_to_fallback', None)

    def _is_pomice_player(self, voice_client):
        if not pomice or not self.pomice_player_cls:
            return False
        return isinstance(voice_client, self.pomice_player_cls)

    async def _ensure_pomice_player_connection(self, guild, voice_channel):
        if not pomice:
            return None
        player = guild.voice_client
        if player is not None and not self._is_pomice_player(player):
            state = self._get_state(guild)
            disconnect_event = asyncio.Event()
            state.backend_disconnect_event = disconnect_event
            try:
                await player.disconnect()
                try:
                    await asyncio.wait_for(disconnect_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "Discord did not confirm the native voice disconnect within 5 seconds."
                    )
            except (discord.HTTPException, discord.Forbidden):
                pass
            finally:
                state.backend_disconnect_event = None
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

    def _build_entry_from_track(self, base_entry, track):
        entry = dict(base_entry)
        entry['url'] = getattr(track, "uri", None) or base_entry['url']
        entry['title'] = getattr(track, "title", None) or entry['url']
        entry['metadata'] = None
        entry['pomice_track'] = track
        self._apply_pomice_track_metadata(entry, track)
        return entry

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
        force_reward = bool(entry.pop('force_reward', False))
        if not force_reward and (elapsed is None or elapsed + 2 < duration):
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
        self._save_play_reward_streaks()
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
                                f"Bonus: RM {bonus}."
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
            "Rewarded %s with RM %s for track %s (balance=%s).",
            requester.id,
            self.play_reward,
            track_key,
            new_balance,
        )

    async def _resolve_pomice_results(self, url, ctx=None):
        if not pomice or not self._should_use_pomice():
            return None
        try:
            node = pomice.NodePool.get_node()
        except Exception:
            return None
        try:
            return await node.get_tracks(query=url, ctx=ctx)
        except Exception as exc:
            self.logger.warning("Pomice track lookup failed for queue metadata: %s", exc)
            return None

    async def _resolve_pomice_track(self, entry, ctx=None):
        results = await self._resolve_pomice_results(entry['url'], ctx=ctx)
        track = self._extract_pomice_track(results)
        if track:
            self._apply_pomice_track_metadata(entry, track)
        return track

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
        should_requeue = (requeue_front or requeue_back) and not entry.get('skipped')
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
        if (
            queue_empty
            and entry
            and entry.get('text_channel')
            and not loop_active
            and not entry.get('skipped')
        ):
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
                disconnect_event = state.backend_disconnect_event
                if disconnect_event:
                    if not disconnect_event.is_set():
                        disconnect_event.set()
                    # Switching between the native Discord voice client and
                    # Pomice deliberately disconnects the bot. Preserve the
                    # active entry and queue during that backend handoff.
                    return
                if state.current_entry and state.current_entry.get('_switching_to_fallback'):
                    return
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
        if not self._empty_vc_watchdog.is_running():
            self._empty_vc_watchdog.start()

    @tasks.loop(minutes=1)
    async def _empty_vc_watchdog(self):
        for guild in self.bot.guilds:
            voice_client = guild.voice_client
            if not voice_client or not voice_client.channel:
                continue
            if not self._should_leave_voice(voice_client):
                continue
            state = self._get_state(guild)
            if self._vc_is_playing(voice_client) or self._vc_is_paused(voice_client):
                if not state.empty_voice_task or state.empty_voice_task.done():
                    self._schedule_empty_voice_shutdown(guild, state)
                continue
            try:
                await voice_client.disconnect()
            except (discord.HTTPException, discord.Forbidden):
                pass

    @commands.Cog.listener()
    async def on_pomice_track_exception(self, player, track, exception):
        guild = getattr(player, "guild", None)
        state = self._get_state(guild)
        entry = state.current_entry if state else None
        if isinstance(exception, dict):
            details = (
                exception.get("cause")
                or exception.get("message")
                or exception.get("causeStackTrace")
                or str(exception)
            )
            severity = exception.get("severity")
        else:
            details = getattr(exception, "cause", None) or getattr(exception, "message", None) or str(exception)
            severity = getattr(exception, "severity", None)
        if entry:
            entry['_track_exception_logged'] = True
        self.logger.error(
            "Lavalink track exception\nTitle: %s\nSeverity: %s\nCause: %s",
            getattr(track, "title", None),
            severity,
            details,
        )
        if entry:
            await self._switch_entry_to_fallback(state, entry, reason=details)

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
        # A delayed Lavalink end event may arrive after native playback has begun.
        if entry.get('playback_backend') == 'yt-dlp/ffmpeg':
            return
        metadata = entry.get("metadata") or {}
        duration = metadata.get("duration")
        elapsed = self._get_elapsed_time(entry)
        title = metadata.get("title") or entry.get("title") or entry.get("url")
        self.logger.info(
            "Track end: title=%s duration=%s elapsed=%s reason=%s",
            title,
            duration,
            None if elapsed is None else round(elapsed, 2),
            getattr(reason, "name", str(reason)),
        )
        current_track = entry.get("pomice_track")
        if current_track and track:
            current_id = getattr(current_track, "identifier", None)
            ended_id = getattr(track, "identifier", None)
            if current_id and ended_id and current_id != ended_id:
                return
        reason_name = getattr(reason, "name", str(reason)).upper()
        if entry.get('_switching_to_fallback', False):
            return
        if reason_name == "REPLACED":
            return
        if reason_name in {"STOPPED", "CLEANUP"} and state.manual_disconnect:
            return
        ended_early = bool(
            duration
            and elapsed is not None
            and elapsed + 2 < duration
            and not entry.get('skipped')
            and reason_name in {"FINISHED", "STOPPED", "CLEANUP"}
        )
        should_fallback = reason_name == "LOADFAILED" or ended_early
        if should_fallback:
            if not entry.pop('_track_exception_logged', False):
                self.logger.error(
                    "Lavalink playback ended unexpectedly: title=%s reason=%s "
                    "elapsed=%s duration=%s",
                    title,
                    reason_name,
                    elapsed,
                    duration,
                )
            if await self._switch_entry_to_fallback(
                state,
                entry,
                reason=(
                    f"track ended with {reason_name} after "
                    f"{0 if elapsed is None else round(elapsed, 1)}s/{duration}s"
                ),
            ):
                return
            if state.current_entry is not entry:
                return
        await self._complete_entry(state, entry)


async def setup(bot):
        music = Music(bot)
        await bot.add_cog(music)
