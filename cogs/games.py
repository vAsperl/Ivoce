import discord
from discord.ext import commands
import itertools
import random
import asyncio
import json
import os
import time
import re


class CurrencyManager:
    def __init__(self, path, start_balance=100):
        self.path = path
        self.start_balance = start_balance
        self._balances = self._load_balances()

    def _load_balances(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._balances, fh)
        except OSError:
            pass

    def get_balance(self, user_id):
        return self._balances.get(str(user_id), self.start_balance)

    def adjust(self, user_id, amount):
        key = str(user_id)
        balance = self.get_balance(user_id)
        balance += amount
        self._balances[key] = max(balance, 0)
        self._save()
        return self._balances[key]

    def is_new_user(self, user_id):
        return str(user_id) not in self._balances

    def ensure_balance(self, user_id, balance):
        key = str(user_id)
        if key in self._balances:
            return self._balances[key]
        self._balances[key] = max(int(balance), 0)
        self._save()
        return self._balances[key]


class PokerBetModal(discord.ui.Modal):
    def __init__(self, cog, ctx, user_id, action="bet"):
        title = "Poker Bet" if action == "bet" else "Poker Raise"
        super().__init__(title=title)
        self.cog = cog
        self.ctx = ctx
        self.user_id = user_id
        self.action = action
        input_label = "Bet amount" if action == "bet" else "Raise amount"
        self.amount = discord.ui.TextInput(
            label=input_label,
            placeholder="10",
            min_length=1,
            max_length=10,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.amount.value).strip())
        except ValueError:
            await interaction.response.send_message("Enter a valid whole number.", ephemeral=True)
            return
        await self.cog._handle_poker_action(interaction, self.action, amount=amount)


class PokerView(discord.ui.View):
    def __init__(self, cog, ctx, user_id, opponent_id=None, timeout=120):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.user_id = user_id
        self.opponent_id = opponent_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in {self.user_id, self.opponent_id}:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return False
        game = self.cog.poker_games.get(interaction.user.id)
        if not game:
            await interaction.response.send_message("That hand is no longer active.", ephemeral=True)
            return False
        actor = self.cog._player_key(game, interaction.user.id)
        if not actor:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return False
        if game.get("turn") != actor:
            await interaction.response.send_message("It's not your turn yet.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        game = self.cog.poker_games.get(self.user_id)
        if not game or not game.get("message"):
            return
        for item in self.children:
            item.disabled = True
        refund_note = "Hand timed out."
        user_refund = game.get("user_total_bet", 0)
        if user_refund:
            self.cog.currency.adjust(game["user_id"], user_refund)
        opponent_id = game.get("opponent_id")
        opponent_refund = game.get("bot_total_bet", 0) if opponent_id else 0
        if opponent_id and opponent_refund:
            self.cog.currency.adjust(opponent_id, opponent_refund)
        if user_refund or opponent_refund:
            refund_note = "Hand timed out. Bets refunded."
        embed = self.cog._poker_status_embed(self.ctx, game, footer_text=refund_note)
        await game["message"].edit(embed=embed, view=self)
        self.cog.poker_games.pop(self.user_id, None)

    @discord.ui.button(label="Check", style=discord.ButtonStyle.secondary)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_poker_action(interaction, "check")

    @discord.ui.button(label="Bet", style=discord.ButtonStyle.primary)
    async def bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.poker_games.get(interaction.user.id)
        action = "bet"
        if game:
            actor = self.cog._player_key(game, interaction.user.id)
            if actor and self.cog._amount_to_call(game, actor) > 0:
                action = "raise"
        await interaction.response.send_modal(PokerBetModal(self.cog, self.ctx, self.user_id, action=action))

    @discord.ui.button(label="All-in", style=discord.ButtonStyle.danger)
    async def allin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_poker_action(interaction, "allin")

    @discord.ui.button(label="Fold", style=discord.ButtonStyle.secondary)
    async def fold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_poker_action(interaction, "fold")

    @discord.ui.button(label="Show Cards", style=discord.ButtonStyle.secondary)
    async def show_cards(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.poker_games.get(interaction.user.id)
        if not game:
            await interaction.response.send_message("That hand is no longer active.", ephemeral=True)
            return
        actor = self.cog._player_key(game, interaction.user.id)
        if not actor:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return
        cards = game["user_cards"] if actor == "user" else game["bot_cards"]
        community = game.get("community", [])
        all_cards = cards + community
        if len(all_cards) >= 5:
            rank, _ = self.cog._best_hand(all_cards)
            hand_label = self.cog.CATEGORY_NAMES[rank]
        else:
            counts = {}
            for card in all_cards:
                counts[card[0]] = counts.get(card[0], 0) + 1
            count_values = sorted(counts.values(), reverse=True)
            if count_values and count_values[0] >= 4:
                hand_label = "Four of a Kind"
            elif count_values and count_values[0] == 3:
                hand_label = "Three of a Kind"
            elif count_values.count(2) >= 2:
                hand_label = "Two Pair"
            elif count_values.count(2) == 1:
                hand_label = "Pair"
            else:
                hand_label = "High Card"
        await interaction.response.send_message(
            f"Your cards: {self.cog._format_cards(cards)}\nHand: {hand_label}",
            ephemeral=True,
        )


class Games(commands.Cog):
    RANK_ORDER = "23456789TJQKA"
    DAILY_REWARD = 1000
    DAILY_COOLDOWN = 60 * 60 * 24
    CATEGORY_NAMES = [
        "High Card",
        "Pair",
        "Two Pair",
        "Three of a Kind",
        "Straight",
        "Flush",
        "Full House",
        "Four of a Kind",
        "Straight Flush",
        "Royal Flush",
    ]

    def __init__(self, bot):
        self.bot = bot
        data_file = os.getenv("GAMES_DATAFILE", "games_currency.json")
        self.currency = CurrencyManager(data_file, start_balance=100)
        self.daily_path = os.getenv("GAMES_DAILY_DATAFILE", "games_daily.json")
        self.daily_claims = self._load_daily_claims()
        self.poker_starter_path = os.getenv("GAMES_POKER_STARTER_DATAFILE", "games_poker_starters.json")
        self.poker_starters = self._load_poker_starters()
        self.persona_path = os.getenv("POKER_PERSONA_PATH", "data/poker_persona.json")
        self.persona_lines = self._load_persona_lines()
        self.poker_profile_path = os.getenv("POKER_PROFILE_PATH", "data/poker_profiles.json")
        self.poker_profiles = self._load_poker_profiles()
        self.poker_games = {}

    def _load_persona_lines(self):
        if not os.path.exists(self.persona_path):
            return {}
        try:
            with open(self.persona_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError):
            return {}

    def _load_poker_starters(self):
        if not os.path.exists(self.poker_starter_path):
            return {}
        try:
            with open(self.poker_starter_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_poker_starters(self):
        try:
            with open(self.poker_starter_path, "w", encoding="utf-8") as fh:
                json.dump(self.poker_starters, fh)
        except OSError:
            pass

    def _load_poker_profiles(self):
        if not os.path.exists(self.poker_profile_path):
            return {}
        try:
            with open(self.poker_profile_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_poker_profiles(self):
        try:
            with open(self.poker_profile_path, "w", encoding="utf-8") as fh:
                json.dump(self.poker_profiles, fh)
        except OSError:
            pass

    def _record_player_action(self, user_id, action):
        profile = self.poker_profiles.get(str(user_id), {"actions": 0, "allin": 0})
        profile["actions"] = profile.get("actions", 0) + 1
        if action in ("allin", "all-in"):
            profile["allin"] = profile.get("allin", 0) + 1
        profile["last_action_ts"] = int(time.time())
        self.poker_profiles[str(user_id)] = profile
        self._save_poker_profiles()

    def _adjust_fold_chance(self, game, fold_chance):
        user_id = game.get("user_id")
        profile = self.poker_profiles.get(str(user_id))
        if not profile:
            return fold_chance
        actions = profile.get("actions", 0)
        if actions < 5:
            return fold_chance
        allin_rate = profile.get("allin", 0) / max(actions, 1)
        reduction = min(0.4, allin_rate * 0.5)
        jitter = random.uniform(0.85, 1.15)
        adjusted = fold_chance * (1 - reduction) * jitter
        return max(0.01, min(0.95, adjusted))

    def _pick_persona_line(self, category, *, game=None):
        lines = self.persona_lines.get(category, [])
        personality = None
        if game:
            personality = game.get("bot_personality")
        if isinstance(lines, dict):
            if personality and personality in lines and isinstance(lines[personality], list):
                lines = lines[personality]
            else:
                lines = lines.get("default", [])
        if not lines:
            return None
        return random.choice(lines)

    def _split_persona_line(self, line):
        if not line:
            return []
        parts = re.split(r"\{delay=([0-9]+(?:\.[0-9]+)?)\}", line)
        segments = []
        text = parts[0].strip()
        if text:
            segments.append((text, 0))
        idx = 1
        while idx < len(parts):
            try:
                delay = float(parts[idx])
            except ValueError:
                delay = 0
            text = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
            if text:
                segments.append((text, delay))
            idx += 2
        return segments

    def _render_persona_line(self, game, line):
        if not line:
            return line
        personality = game.get("bot_personality", "passive")
        return line.replace("{behavior}", personality)

    async def _send_persona_message(self, ctx, name, avatar_url, line, game=None):
        if not line:
            return
        if game:
            line = self._render_persona_line(game, line)
        segments = self._split_persona_line(line)
        if not segments:
            return
        try:
            webhook = await ctx.channel.create_webhook(name="poker-persona")
        except (discord.Forbidden, discord.HTTPException):
            for text, delay in segments:
                if delay:
                    await asyncio.sleep(delay)
                embed = discord.Embed(description=text, color=discord.Color.blurple())
                if name:
                    embed.set_author(name=name, icon_url=avatar_url)
                await ctx.send(embed=embed)
            return
        try:
            for text, delay in segments:
                if delay:
                    await asyncio.sleep(delay)
                await webhook.send(content=text, username=name or "Poker", avatar_url=avatar_url)
        except discord.HTTPException:
            for text, delay in segments:
                if delay:
                    await asyncio.sleep(delay)
                embed = discord.Embed(description=text, color=discord.Color.blurple())
                if name:
                    embed.set_author(name=name, icon_url=avatar_url)
                await ctx.send(embed=embed)
        finally:
            try:
                await webhook.delete()
            except discord.HTTPException:
                pass

    def _load_daily_claims(self):
        if not os.path.exists(self.daily_path):
            return {}
        try:
            with open(self.daily_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_daily_claims(self):
        try:
            with open(self.daily_path, "w", encoding="utf-8") as fh:
                json.dump(self.daily_claims, fh)
        except OSError:
            pass

    def _format_cooldown(self, seconds):
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, _ = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _build_usage_embed(self, usage, example=None):
        description = f"Usage: {usage}"
        embed = discord.Embed(
            title="Missing required input",
            description=description,
            color=discord.Color.orange(),
        )
        if example:
            embed.add_field(name="Example", value=example, inline=False)
        return embed

    def _build_deck(self):
        return [rank + suit for rank in self.RANK_ORDER for suit in "♠♥♦♣"]

    def _format_cards(self, cards):
        return " ".join(cards)

    def _rank_values(self, cards):
        values = [self.RANK_ORDER.index(card[0]) for card in cards]
        values.sort(reverse=True)
        return values

    def _is_straight(self, values):
        unique_vals = sorted(set(values))
        if len(unique_vals) < 5:
            return False, None
        for i in range(len(unique_vals) - 4):
            window = unique_vals[i : i + 5]
            if window[-1] - window[0] == 4 and len(window) == 5:
                return True, window[-1]
        if set([0, 1, 2, 3, 12]).issubset(set(values)):
            return True, 3
        return False, None

    def _evaluate_hand(self, cards):
        values = self._rank_values(cards)
        suits = [card[1] for card in cards]
        counts = {}
        for card in cards:
            counts[card[0]] = counts.get(card[0], 0) + 1
        sorted_counts = sorted(counts.items(), key=lambda x: (-x[1], -self.RANK_ORDER.index(x[0])))
        is_flush = len(set(suits)) == 1
        straight, high_straight = self._is_straight(values)

        if is_flush and straight and high_straight == 12:
            return 9, [12]
        if is_flush and straight:
            return 8, [high_straight]
        if sorted_counts[0][1] == 4:
            quad = self.RANK_ORDER.index(sorted_counts[0][0])
            kicker = max(self.RANK_ORDER.index(k) for k, v in counts.items() if v == 1)
            return 7, [quad, kicker]
        if sorted_counts[0][1] == 3 and sorted_counts[1][1] == 2:
            triple = self.RANK_ORDER.index(sorted_counts[0][0])
            pair = self.RANK_ORDER.index(sorted_counts[1][0])
            return 6, [triple, pair]
        if is_flush:
            return 5, values
        if straight:
            return 4, [high_straight]
        if sorted_counts[0][1] == 3:
            triple = self.RANK_ORDER.index(sorted_counts[0][0])
            kickers = [
                self.RANK_ORDER.index(rank)
                for rank, count in sorted_counts[1:]
                for _ in range(count)
            ]
            return 3, [triple] + kickers
        if sorted_counts[0][1] == 2 and sorted_counts[1][1] == 2:
            high_pair = self.RANK_ORDER.index(sorted_counts[0][0])
            low_pair = self.RANK_ORDER.index(sorted_counts[1][0])
            kicker = self.RANK_ORDER.index(sorted_counts[2][0])
            return 2, [high_pair, low_pair, kicker]
        if sorted_counts[0][1] == 2:
            pair_value = self.RANK_ORDER.index(sorted_counts[0][0])
            kickers = [
                self.RANK_ORDER.index(rank)
                for rank, count in sorted_counts[1:]
                for _ in range(count)
            ]
            return 1, [pair_value] + kickers
        return 0, values

    def _compare_hands(self, user_hand, bot_hand):
        user_rank, user_breakers = user_hand
        bot_rank, bot_breakers = bot_hand
        if user_rank != bot_rank:
            return user_rank > bot_rank
        return user_breakers > bot_breakers

    def _is_pvp(self, game):
        return bool(game.get("opponent_id"))

    def _player_key(self, game, user_id):
        if user_id == game.get("user_id"):
            return "user"
        if game.get("opponent_id") == user_id:
            return "bot"
        return None

    def _other_player(self, player):
        return "bot" if player == "user" else "user"

    def _player_display_name(self, game, player):
        if player == "user":
            return game.get("player_name")
        opponent_name = game.get("opponent_name")
        return opponent_name or game.get("bot_shadow_name", "Bot")

    def _player_avatar(self, game, player):
        if player == "user":
            return game.get("player_avatar")
        opponent_avatar = game.get("opponent_avatar")
        return opponent_avatar or game.get("bot_shadow_avatar")

    def _player_balance(self, game, player):
        if player == "user":
            return self.currency.get_balance(game["user_id"])
        if self._is_pvp(game):
            return self.currency.get_balance(game["opponent_id"])
        return game.get("bot_bankroll", 0)

    def _adjust_player_balance(self, game, player, amount):
        if player == "user":
            return self.currency.adjust(game["user_id"], amount)
        if self._is_pvp(game):
            new_balance = self.currency.adjust(game["opponent_id"], amount)
            game["bot_bankroll"] = new_balance
            return new_balance
        game["bot_bankroll"] = max(0, game.get("bot_bankroll", 0) + amount)
        return game["bot_bankroll"]

    def _best_hand(self, cards):
        best = None
        for combo in itertools.combinations(cards, 5):
            hand = self._evaluate_hand(list(combo))
            if best is None or self._compare_hands(hand, best):
                best = hand
        return best

    def _poker_stage_label(self, stage):
        return {
            "preflop": "Pre-Flop",
            "flop": "Flop",
            "turn": "Turn",
            "river": "River",
            "showdown": "Showdown",
        }.get(stage, "Poker")

    def _poker_status_embed(self, ctx, game, footer_text=None):
        stage = game["stage"]
        opponent_label = "Opponent" if self._is_pvp(game) else "Bot"
        embed = discord.Embed(
            title=f"Micro Poker - {self._poker_stage_label(stage)}",
            description=f"{ctx.author.mention} vs. {self._player_display_name(game, 'bot')}",
            color=discord.Color.blurple(),
        )
        community = game["community"]
        embed.add_field(
            name="Community",
            value=self._format_cards(community) if community else "No cards yet.",
            inline=False,
        )
        if stage == "showdown":
            embed.add_field(
                name="Player hand",
                value=self._format_cards(game["user_cards"]),
                inline=False,
            )
            embed.add_field(
                name=f"{opponent_label} hand",
                value=self._format_cards(game["bot_cards"]),
                inline=False,
            )
        embed.add_field(
            name=opponent_label,
            value=game.get("bot_status", "Waiting..."),
            inline=True,
        )
        embed.add_field(
            name="Your total bet",
            value=f"RM {game['user_total_bet']}",
            inline=True,
        )
        embed.add_field(
            name="Pot",
            value=f"RM {game.get('pot', game['user_total_bet'])}",
            inline=True,
        )
        if footer_text:
            embed.set_footer(text=footer_text)
        shadow_name = game.get("bot_shadow_name", "Bot")
        if not self._is_pvp(game) and not shadow_name.endswith(" [BOT]"):
            shadow_name = f"{shadow_name} [BOT]"
        embed.set_author(name=shadow_name, icon_url=game.get("bot_shadow_avatar"))
        turn_player = game.get("turn", "user")
        thumb_avatar = self._player_avatar(game, turn_player)
        if thumb_avatar:
            embed.set_thumbnail(url=thumb_avatar)
        return embed

    def _select_bot_shadow(self, ctx):
        shadow_name = "Bot"
        shadow_avatar = None
        guild = ctx.guild
        if guild:
            candidates = [m for m in guild.members if not m.bot and m.id != ctx.author.id]
            if not candidates:
                candidates = [m for m in guild.members if m.id != ctx.author.id]
            if candidates:
                member = random.choice(candidates)
                shadow_name = member.display_name or member.name
                shadow_avatar = member.display_avatar.url
        return shadow_name, shadow_avatar

    def _choose_bot_personality(self):
        options = ["aggressive", "passive", "coward"]
        weights = [0.25, 0.5, 0.25]
        return random.choices(options, weights, k=1)[0]

    def _should_bot_allin(self, game, amount_to_call):
        personality = game.get("bot_personality", "passive")
        if amount_to_call <= 0:
            return True
        if game.get("bot_bankroll", 0) <= 0:
            return False
        if personality == "aggressive":
            return True
        if personality == "coward":
            return False
        return random.random() < 0.5

    def _bot_allin_chance(self, game):
        personality = game.get("bot_personality", "passive")
        return {
            "aggressive": 0.35,
            "passive": 0.15,
            "coward": 0.05,
        }.get(personality, 0.15)

    def _amount_to_call(self, game, player):
        if player == "user":
            return max(0, game.get("current_bet", 0) - game.get("user_round_bet", 0))
        return max(0, game.get("current_bet", 0) - game.get("bot_round_bet", 0))

    def _sync_poker_view(self, game):
        view = game.get("view")
        if not view:
            return
        turn_player = game.get("turn", "user")
        turn_locked = (turn_player != "user") and not self._is_pvp(game)
        for item in view.children:
            item.disabled = turn_locked
        to_call = self._amount_to_call(game, turn_player)
        if to_call > 0:
            view.check.label = "Call"
            view.bet.label = "Raise"
        else:
            view.check.label = "Check"
            view.bet.label = "Bet"
        if not turn_locked:
            view.bet.disabled = game.get("raise_count", 0) >= game.get("max_raises", 10)

    def _record_bot_call_round(self, game, allow_partial=False):
        amount_to_call = self._amount_to_call(game, "bot")
        if amount_to_call <= 0:
            return True, False
        bot_bankroll = game.get("bot_bankroll", 0)
        if bot_bankroll < amount_to_call:
            if not allow_partial or bot_bankroll <= 0:
                return False, False
            contribution = bot_bankroll
        else:
            contribution = amount_to_call
        game["bot_total_bet"] = game.get("bot_total_bet", 0) + contribution
        game["bot_round_bet"] = game.get("bot_round_bet", 0) + contribution
        game["bot_bankroll"] = bot_bankroll - contribution
        game["pot"] = game.get("pot", 0) + contribution
        all_in = game["bot_bankroll"] == 0
        if all_in:
            game["bot_all_in"] = True
        return True, all_in

    def _record_bot_raise(self, game, amount):
        if amount <= 0:
            return 0
        bot_bankroll = game.get("bot_bankroll", 0)
        contribution = min(amount, bot_bankroll)
        game["bot_total_bet"] = game.get("bot_total_bet", 0) + contribution
        game["bot_round_bet"] = game.get("bot_round_bet", 0) + contribution
        game["bot_bankroll"] = bot_bankroll - contribution
        game["pot"] = game.get("pot", 0) + contribution
        if game["bot_bankroll"] == 0:
            game["bot_all_in"] = True
        return contribution

    def _reset_round(self, game):
        game["user_round_bet"] = 0
        game["bot_round_bet"] = 0
        game["current_bet"] = 0
        game["raise_count"] = 0
        game["awaiting_call"] = None
        game["user_acted"] = False
        game["bot_acted"] = False
        if game["stage"] == "preflop":
            game["turn"] = game.get("sb_player", "user")
        else:
            game["turn"] = game.get("bb_player", "bot")

    async def _bot_think(self, game):
        multipliers = {
            "aggressive": 0.8,
            "passive": 1.1,
            "coward": 1.4,
        }
        multiplier = multipliers.get(game.get("bot_personality", "passive"), 1.0)
        delay = random.uniform(1.2, 2.6) * multiplier
        await asyncio.sleep(delay)

    async def _update_interaction(self, interaction, embed, view=None):
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def _update_game_message(self, game, embed):
        message = game.get("message")
        view = game.get("view")
        if not message:
            return
        await message.edit(embed=embed, view=view)

    def _turn_prompt(self, game, player):
        if not self._is_pvp(game) and player == "bot":
            return "Bot is deciding..."
        to_call = self._amount_to_call(game, player)
        action_text = "call, raise, all-in, or fold" if to_call > 0 else "check, bet, all-in, or fold"
        if not self._is_pvp(game) and player == "user":
            return f"Your move: {action_text}."
        name = self._player_display_name(game, player)
        return f"{name}'s move: {action_text}."

    async def _resolve_showdown(self, interaction, game):
        game["stage"] = "showdown"
        user_best = self._best_hand(game["user_cards"] + game["community"])
        bot_best = self._best_hand(game["bot_cards"] + game["community"])
        user_wins = self._compare_hands(user_best, bot_best)
        user_id = interaction.user.id if interaction else game["ctx"].author.id
        persona_name = game.get("bot_shadow_name", "Bot")
        persona_avatar = game.get("bot_shadow_avatar")

        if self._is_pvp(game):
            opponent_id = game["opponent_id"]
            pot = game.get("pot", 0)
            user_name = self._player_display_name(game, "user")
            opponent_name = self._player_display_name(game, "bot")
            if user_wins:
                self.currency.adjust(game["user_id"], pot)
                result_text = f"{user_name} wins RM {pot}!"
            elif user_best == bot_best:
                split = pot // 2
                self.currency.adjust(game["user_id"], split)
                self.currency.adjust(opponent_id, pot - split)
                result_text = "It's a tie! Pot split."
            else:
                self.currency.adjust(opponent_id, pot)
                result_text = f"{opponent_name} wins RM {pot}!"
        else:
            if user_wins:
                payout = game["user_total_bet"] * 2
                self.currency.adjust(user_id, payout)
                result_text = f"You win RM {game['user_total_bet']}!"
            elif user_best == bot_best:
                payout = game["user_total_bet"]
                self.currency.adjust(user_id, payout)
                result_text = "It's a tie! Bet returned."
            else:
                result_text = f"You lose RM {game['user_total_bet']}."

        game["bot_status"] = "Showdown."
        embed = self._poker_status_embed(game["ctx"], game, footer_text=result_text)
        user_label = "Player hand rank" if self._is_pvp(game) else "Your hand rank"
        bot_label = "Opponent hand rank" if self._is_pvp(game) else "Bot hand rank"
        embed.add_field(
            name=user_label,
            value=self.CATEGORY_NAMES[user_best[0]],
            inline=True,
        )
        embed.add_field(
            name=bot_label,
            value=self.CATEGORY_NAMES[bot_best[0]],
            inline=True,
        )
        persona_line = None
        if not self._is_pvp(game):
            category = None
            if user_wins:
                category = "lose"
            elif user_best != bot_best:
                category = "win"
            else:
                category = "tie"
            persona_line = self._pick_persona_line(category, game=game) if category else None
        await self._finish_poker(interaction, game, embed)
        if persona_line:
            await self._send_persona_message(game["ctx"], persona_name, persona_avatar, persona_line, game=game)

    async def _maybe_finish_round(self, interaction, game, *, delay_on_advance=0):
        awaiting_player = game.get("awaiting_call")
        if awaiting_player:
            if (
                game.get(f"{awaiting_player}_all_in")
                or game.get(f"{awaiting_player}_allin_capped")
                or self._player_balance(game, awaiting_player) <= 0
            ):
                game["awaiting_call"] = None
            else:
                return False
        if not (game.get("user_acted") and game.get("bot_acted")):
            return False
        if (
            game.get("user_all_in")
            or game.get("bot_all_in")
            or (game.get("user_allin_capped") and game.get("bot_allin_capped"))
        ):
            self._deal_to_river(game)
            await self._resolve_showdown(interaction, game)
            return True
        if game.get("user_round_bet") != game.get("bot_round_bet"):
            return False
        if game["stage"] == "river":
            await self._resolve_showdown(interaction, game)
            return True
        if delay_on_advance:
            await asyncio.sleep(delay_on_advance)
        footer = await self._advance_stage(game)
        if game.get("turn") == "bot" and not self._is_pvp(game):
            game["bot_status"] = "Bot is deciding..."
        else:
            game["bot_status"] = "Waiting..."
        embed = self._poker_status_embed(game["ctx"], game, footer_text=footer)
        self._sync_poker_view(game)
        if interaction:
            await self._update_interaction(interaction, embed, view=game.get("view"))
        else:
            await self._update_game_message(game, embed)
        if game["turn"] == "bot" and not self._is_pvp(game):
            await self._bot_take_turn(interaction, game)
        return True

    async def _finish_poker(self, interaction, game, embed, message_text=None):
        view = game.get("view")
        if view:
            for item in view.children:
                item.disabled = True
        user_id = interaction.user.id if interaction else game["ctx"].author.id
        self.poker_games.pop(user_id, None)
        opponent_id = game.get("opponent_id")
        if opponent_id:
            self.poker_games.pop(opponent_id, None)
        if interaction:
            await self._update_interaction(interaction, embed, view=view)
            if message_text:
                await interaction.followup.send(message_text)
        else:
            await self._update_game_message(game, embed)
            if message_text:
                await game["ctx"].send(message_text)

    def _deal_flop(self, game):
        game["community"].extend([game["deck"].pop() for _ in range(3)])
        game["stage"] = "flop"

    def _deal_turn(self, game):
        game["community"].append(game["deck"].pop())
        game["stage"] = "turn"

    def _deal_river(self, game):
        game["community"].append(game["deck"].pop())
        game["stage"] = "river"

    def _deal_to_river(self, game):
        while game["stage"] != "river":
            if game["stage"] == "preflop":
                self._deal_flop(game)
            elif game["stage"] == "flop":
                self._deal_turn(game)
            elif game["stage"] == "turn":
                self._deal_river(game)

    def _refresh_fold_chance(self, game):
        base_odds = {
            "preflop": 0.02,
            "flop": 0.08,
            "turn": 0.15,
            "river": 0.25,
        }
        base = base_odds.get(game.get("stage"), 0.15)
        variance = random.uniform(0.5, 1.5)
        adjusted = max(0.0, min(1.0, base * variance))
        game["fold_chance"] = adjusted
        return adjusted

    async def _advance_stage(self, game):
        if game["stage"] == "preflop":
            self._deal_flop(game)
            self._reset_round(game)
            self._refresh_fold_chance(game)
            return "Flop dealt. Your move."
        if game["stage"] == "flop":
            self._deal_turn(game)
            self._reset_round(game)
            self._refresh_fold_chance(game)
            return "Turn dealt. Your move."
        if game["stage"] == "turn":
            self._deal_river(game)
            self._reset_round(game)
            self._refresh_fold_chance(game)
            return "River dealt. Your move."
        return None

    async def _bot_take_turn(self, interaction, game):
        if self._is_pvp(game):
            game["locked"] = False
            return
        if game.get("bot_acted") and not game.get("awaiting_call"):
            game["locked"] = False
            await self._maybe_finish_round(interaction, game, delay_on_advance=1.0)
            return
        game["locked"] = True
        game["bot_status"] = "Bot is deciding..."
        thinking_embed = self._poker_status_embed(game["ctx"], game, footer_text=self._turn_prompt(game, "bot"))
        self._sync_poker_view(game)
        if interaction:
            await self._update_interaction(interaction, thinking_embed, view=game.get("view"))
        else:
            await self._update_game_message(game, thinking_embed)
        await self._bot_think(game)
        to_call = self._amount_to_call(game, "bot")

        if game.get("bot_all_in"):
            game["bot_acted"] = True
            game["awaiting_call"] = None
        elif to_call > 0:
            bot_shoved = False
            max_bet = game.get("max_bet", 0)
            cap_call = max_bet and game.get("current_bet", 0) >= max_bet
            if not cap_call:
                fold_chance = game.get("fold_chance")
                if fold_chance is None:
                    fold_chance = self._refresh_fold_chance(game)
                fold_chance = self._adjust_fold_chance(game, fold_chance)
                if random.random() < fold_chance:
                    user_id = interaction.user.id if interaction else game["ctx"].author.id
                    payout = game["user_total_bet"] * 2
                    self.currency.adjust(user_id, payout)
                    game["bot_status"] = "Bot folds."
                    line = self._pick_persona_line("fold", game=game)
                    persona_name = game.get("bot_shadow_name")
                    persona_avatar = game.get("bot_shadow_avatar")
                    embed = self._poker_status_embed(game["ctx"], game, footer_text=f"You win RM {game['user_total_bet']}!")
                    await self._finish_poker(interaction, game, embed)
                    await self._send_persona_message(game["ctx"], persona_name, persona_avatar, line, game=game)
                    return
            if cap_call and game.get("bot_bankroll", 0) > 0:
                target_total = max_bet
                contribution = max(0, target_total - game.get("bot_round_bet", 0))
                amount = min(contribution, game.get("bot_bankroll", 0))
                contributed = self._record_bot_raise(game, amount) if amount > 0 else 0
                if contributed > 0:
                    game["current_bet"] = game.get("bot_round_bet", game.get("current_bet", 0))
                    game["raise_count"] = game.get("raise_count", 0) + 1
                    game["awaiting_call"] = "user"
                    game["bot_allin_capped"] = True
                    game["bot_status"] = "Bot goes all-in."
                    game["bot_acted"] = True
                    bot_shoved = True
            allow_partial = cap_call
            if to_call > game.get("bot_bankroll", 0):
                allow_partial = cap_call or self._should_bot_allin(game, to_call)
                if not allow_partial:
                    user_id = interaction.user.id if interaction else game["ctx"].author.id
                    payout = game["user_total_bet"] * 2
                    self.currency.adjust(user_id, payout)
                    game["bot_status"] = "Bot folds."
                    line = self._pick_persona_line("fold", game=game)
                    persona_name = game.get("bot_shadow_name")
                    persona_avatar = game.get("bot_shadow_avatar")
                    embed = self._poker_status_embed(game["ctx"], game, footer_text=f"You win RM {game['user_total_bet']}!")
                    await self._finish_poker(interaction, game, embed)
                    await self._send_persona_message(game["ctx"], persona_name, persona_avatar, line, game=game)
                    return
            bet_allowed = game.get("raise_count", 0) < game.get("max_raises", 10)
            max_bet = game.get("max_bet", 0)
            if bet_allowed and max_bet and random.random() < self._bot_allin_chance(game):
                target_total = max_bet
                contribution = max(0, target_total - game.get("bot_round_bet", 0))
                amount = min(contribution, game.get("bot_bankroll", 0))
                contributed = self._record_bot_raise(game, amount) if amount > 0 else 0
                if contributed > 0:
                    game["current_bet"] = game.get("bot_round_bet", game.get("current_bet", 0))
                    game["raise_count"] = game.get("raise_count", 0) + 1
                    game["awaiting_call"] = "user"
                    if max_bet and game.get("bot_round_bet", 0) >= max_bet and game.get("bot_bankroll", 0) > 0:
                        game["bot_allin_capped"] = True
                    game["bot_status"] = "Bot goes all-in."
                    game["bot_acted"] = True
                    bot_shoved = True
            if bot_shoved:
                pass
            else:
                success, all_in = self._record_bot_call_round(game, allow_partial=allow_partial)
                if not success:
                    user_id = interaction.user.id if interaction else game["ctx"].author.id
                    payout = game["user_total_bet"] * 2
                    self.currency.adjust(user_id, payout)
                    game["bot_status"] = "Bot folds."
                    line = self._pick_persona_line("fold", game=game)
                    persona_name = game.get("bot_shadow_name")
                    persona_avatar = game.get("bot_shadow_avatar")
                    embed = self._poker_status_embed(game["ctx"], game, footer_text=f"You win RM {game['user_total_bet']}!")
                    await self._finish_poker(interaction, game, embed)
                    await self._send_persona_message(game["ctx"], persona_name, persona_avatar, line, game=game)
                    return
                game["bot_status"] = "Bot is all-in." if all_in else "Bot calls."
                game["bot_acted"] = True
                game["awaiting_call"] = None
        else:
            bot_shoved = False
            bet_allowed = game.get("raise_count", 0) < game.get("max_raises", 10)
            max_bet = game.get("max_bet", 0)
            if bet_allowed and max_bet and random.random() < self._bot_allin_chance(game):
                target_total = max_bet
                contribution = max(0, target_total - game.get("bot_round_bet", 0))
                amount = min(contribution, game.get("bot_bankroll", 0))
                contributed = self._record_bot_raise(game, amount) if amount > 0 else 0
                if contributed > 0:
                    game["current_bet"] = game.get("bot_round_bet", game.get("current_bet", 0))
                    game["raise_count"] = game.get("raise_count", 0) + 1
                    game["awaiting_call"] = "user"
                    if max_bet and game.get("bot_round_bet", 0) >= max_bet and game.get("bot_bankroll", 0) > 0:
                        game["bot_allin_capped"] = True
                    game["bot_status"] = "Bot goes all-in."
                    bot_shoved = True
            if bot_shoved:
                game["bot_acted"] = True
            elif bet_allowed and game.get("bot_bankroll", 0) > 0 and random.random() < 0.35:
                min_bet = game.get("min_bet", 0)
                current_bet = game.get("current_bet", 0)
                target_bet = current_bet + min_bet if current_bet > 0 else min_bet
                max_bet = game.get("max_bet", 0)
                if max_bet and target_bet > max_bet:
                    target_bet = max_bet
                contribution = max(0, target_bet - game.get("bot_round_bet", 0))
                amount = min(contribution, game.get("bot_bankroll", 0))
                contributed = self._record_bot_raise(game, amount) if amount > 0 else 0
                if contributed > 0:
                    game["current_bet"] = game.get("bot_round_bet", game.get("current_bet", 0))
                    game["raise_count"] = game.get("raise_count", 0) + 1
                    game["awaiting_call"] = "user"
                    action = "raises" if current_bet > 0 else "bets"
                    game["bot_status"] = f"Bot {action} {contributed}."
                else:
                    game["bot_status"] = "Bot checks."
            else:
                game["bot_status"] = "Bot checks."
            game["bot_acted"] = True

        game["turn"] = "user"
        embed = self._poker_status_embed(game["ctx"], game, footer_text=self._turn_prompt(game, "user"))
        self._sync_poker_view(game)
        if interaction:
            await self._update_interaction(interaction, embed, view=game.get("view"))
        else:
            await self._update_game_message(game, embed)
        game["locked"] = False
        await self._maybe_finish_round(interaction, game, delay_on_advance=1.0)

    async def _handle_poker_action(self, interaction, action, amount=None):
        user_id = interaction.user.id
        game = self.poker_games.get(user_id)
        if not game:
            await interaction.response.send_message("You don't have an active hand. Start one with ?poker <bet>.", ephemeral=True)
            return
        if game.get("locked"):
            await interaction.response.send_message("Hold on, finishing the last action.", ephemeral=True)
            return
        actor = self._player_key(game, user_id)
        if not actor:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return
        if game.get("turn") != actor:
            await interaction.response.send_message("It's not your turn yet.", ephemeral=True)
            return
        game["locked"] = True
        view = game.get("view")
        to_call = self._amount_to_call(game, actor)
        effective_action = action
        if action == "check" and to_call > 0:
            effective_action = "call"
        if action == "bet" and to_call > 0:
            effective_action = "raise"
        if action == "raise" and to_call == 0:
            effective_action = "bet"

        if effective_action == "fold":
            opponent = self._other_player(actor)
            winner_id = game["user_id"] if opponent == "user" else game.get("opponent_id")
            if self._is_pvp(game):
                pot = game.get("pot", 0)
                if winner_id:
                    self.currency.adjust(winner_id, pot)
                game["bot_status"] = f"{self._player_display_name(game, actor)} folded."
                embed = self._poker_status_embed(game["ctx"], game, footer_text=f"{self._player_display_name(game, opponent)} wins RM {pot}!")
                self._record_player_action(user_id, effective_action)
                await self._finish_poker(interaction, game, embed)
                return
            game["bot_status"] = "You folded."
            embed = self._poker_status_embed(game["ctx"], game, footer_text="Hand over.")
            line = self._pick_persona_line("fold", game=game)
            persona_name = game.get("bot_shadow_name")
            persona_avatar = game.get("bot_shadow_avatar")
            self._record_player_action(user_id, effective_action)
            await self._finish_poker(interaction, game, embed)
            await self._send_persona_message(game["ctx"], persona_name, persona_avatar, line, game=game)
            return

        if effective_action in ("bet", "raise"):
            if amount is None or amount <= 0:
                await interaction.response.send_message("Bet amount must be positive.", ephemeral=True)
                game["locked"] = False
                return
            min_bet = game.get("min_bet", 0)
            if min_bet and amount < min_bet:
                await interaction.response.send_message(
                    f"Minimum bet is RM {min_bet}.",
                    ephemeral=True,
                )
                game["locked"] = False
                return
            if game.get("raise_count", 0) >= game.get("max_raises", 10):
                await interaction.response.send_message("Max raises reached for this round.", ephemeral=True)
                game["locked"] = False
                return
            current_balance = self._player_balance(game, actor)
            if amount > current_balance:
                await interaction.response.send_message("You don't have enough RM for that bet.", ephemeral=True)
                game["locked"] = False
                return
            round_key = f"{actor}_round_bet"
            total_key = f"{actor}_total_bet"
            new_round_bet = game.get(round_key, 0) + amount
            if new_round_bet <= game.get("current_bet", 0):
                await interaction.response.send_message("Raise must exceed the current bet.", ephemeral=True)
                game["locked"] = False
                return
            min_raise_to = game.get("current_bet", 0) + min_bet if min_bet else 0
            if min_bet and new_round_bet < min_raise_to:
                await interaction.response.send_message(
                    f"Minimum raise is RM {min_bet}.",
                    ephemeral=True,
                )
                game["locked"] = False
                return
            max_bet = game.get("max_bet", 0)
            if max_bet and new_round_bet > max_bet:
                await interaction.response.send_message(f"That exceeds the max bet of RM {max_bet}.", ephemeral=True)
                game["locked"] = False
                return
            self._adjust_player_balance(game, actor, -amount)
            game[total_key] = game.get(total_key, 0) + amount
            game[round_key] = new_round_bet
            game["pot"] = game.get("pot", 0) + amount
            if self._player_balance(game, actor) == 0:
                game[f"{actor}_all_in"] = True
            game["current_bet"] = new_round_bet
            game["raise_count"] = game.get("raise_count", 0) + 1
            game["awaiting_call"] = self._other_player(actor)
            game[f"{actor}_acted"] = True
            game["turn"] = self._other_player(actor)
            if self._is_pvp(game):
                action_word = "raises" if effective_action == "raise" else "bets"
                game["bot_status"] = f"Last action: {self._player_display_name(game, actor)} {action_word} {amount}."
            else:
                game["bot_status"] = "Waiting..."
            footer_text = self._turn_prompt(game, game["turn"])
            embed = self._poker_status_embed(game["ctx"], game, footer_text=footer_text)
            self._sync_poker_view(game)
            await self._update_interaction(interaction, embed, view=view)
            self._record_player_action(user_id, effective_action)
            if not self._is_pvp(game):
                await self._bot_take_turn(interaction, game)
            else:
                finished = await self._maybe_finish_round(interaction, game)
                if finished:
                    game["locked"] = False
                    return
                game["locked"] = False
            return

        if effective_action in ("allin", "all-in"):
            current_balance = self._player_balance(game, actor)
            if current_balance <= 0:
                await interaction.response.send_message("You don't have any RM to go all-in.", ephemeral=True)
                game["locked"] = False
                return
            round_key = f"{actor}_round_bet"
            total_key = f"{actor}_total_bet"
            max_bet = game.get("max_bet", 0)
            current_round_bet = game.get(round_key, 0)
            max_allowed_total = max_bet if max_bet else current_round_bet + current_balance
            target_total = min(current_round_bet + current_balance, max_allowed_total)
            amount_to_allin = target_total - current_round_bet
            if amount_to_allin <= 0:
                await interaction.response.send_message(
                    f"You're already at the max bet of RM {max_bet}.",
                    ephemeral=True,
                )
                game["locked"] = False
                return
            if max_bet and target_total >= max_bet and current_balance > amount_to_allin:
                game[f"{actor}_allin_capped"] = True
            self._adjust_player_balance(game, actor, -amount_to_allin)
            game[total_key] = game.get(total_key, 0) + amount_to_allin
            game[round_key] = current_round_bet + amount_to_allin
            game["pot"] = game.get("pot", 0) + amount_to_allin
            game[f"{actor}_all_in"] = amount_to_allin >= current_balance
            if game[round_key] > game.get("current_bet", 0):
                game["current_bet"] = game[round_key]
                game["raise_count"] = game.get("raise_count", 0) + 1
                game["awaiting_call"] = self._other_player(actor)
            else:
                game["awaiting_call"] = None
            game[f"{actor}_acted"] = True
            game["turn"] = self._other_player(actor)
            if self._is_pvp(game):
                player_name = self._player_display_name(game, actor)
                game["bot_status"] = f"Last action: {player_name} went all-in for {amount_to_allin}."
            else:
                game["bot_status"] = "Waiting..."
            footer_text = self._turn_prompt(game, game["turn"])
            embed = self._poker_status_embed(game["ctx"], game, footer_text=footer_text)
            self._sync_poker_view(game)
            await self._update_interaction(interaction, embed, view=view)
            self._record_player_action(user_id, effective_action)
            if not self._is_pvp(game):
                await self._bot_take_turn(interaction, game)
            else:
                finished = await self._maybe_finish_round(interaction, game)
                if finished:
                    game["locked"] = False
                    return
                game["locked"] = False
            return

        if effective_action == "call":
            amount_to_call = to_call
            current_balance = self._player_balance(game, actor)
            if amount_to_call > current_balance:
                amount_to_call = current_balance
                game[f"{actor}_all_in"] = True
            if amount_to_call > 0:
                round_key = f"{actor}_round_bet"
                total_key = f"{actor}_total_bet"
                self._adjust_player_balance(game, actor, -amount_to_call)
                game[total_key] = game.get(total_key, 0) + amount_to_call
                game[round_key] = game.get(round_key, 0) + amount_to_call
                game["pot"] = game.get("pot", 0) + amount_to_call
            game[f"{actor}_acted"] = True
            game["awaiting_call"] = None
        elif effective_action == "check":
            game[f"{actor}_acted"] = True
        else:
            await interaction.response.send_message("Invalid action.", ephemeral=True)
            game["locked"] = False
            return
        if self._is_pvp(game):
            player_name = self._player_display_name(game, actor)
            if effective_action == "call":
                game["bot_status"] = f"Last action: {player_name} called {amount_to_call}."
            else:
                game["bot_status"] = f"Last action: {player_name} checked."

        self._record_player_action(user_id, effective_action)
        if game.get("bot_acted") and game.get("user_acted") and not game.get("awaiting_call"):
            finished = await self._maybe_finish_round(interaction, game)
            if finished:
                game["locked"] = False
                return

        game["turn"] = self._other_player(actor)
        if not self._is_pvp(game):
            game["bot_status"] = "Waiting..."
        footer_text = self._turn_prompt(game, game["turn"])
        embed = self._poker_status_embed(game["ctx"], game, footer_text=footer_text)
        self._sync_poker_view(game)
        await self._update_interaction(interaction, embed, view=view)
        if not self._is_pvp(game):
            await self._bot_take_turn(interaction, game)
        else:
            game["locked"] = False

    @commands.command(aliases=["bal"])
    async def balance(self, ctx):
        bal = self.currency.get_balance(ctx.author.id)
        embed = discord.Embed(
            title="Balance",
            description=f"{ctx.author.mention}, you have RM {bal}.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Tips",
            value=(
                "• Use `?daily` for a daily reward.\n"
                "• Play full-length songs with `?play` to earn RM.\n"
                "• Avoid replaying the same song repeatedly."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)


    @commands.command(aliases=["lb"])
    async def leaderboard(self, ctx):
        balances = getattr(self.currency, "_balances", {})
        if not balances:
            await ctx.send("No balances recorded yet.")
            return
        entries = []
        for user_id, balance in balances.items():
            try:
                balance_value = int(balance)
            except (TypeError, ValueError):
                continue
            entries.append((int(user_id), balance_value))
        entries.sort(key=lambda item: item[1], reverse=True)
        top = entries[:10]
        lines = []
        for idx, (user_id, balance_value) in enumerate(top, start=1):
            member = ctx.guild.get_member(user_id) if ctx.guild else None
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"{idx}. {name} — RM {balance_value}")
        embed = discord.Embed(
            title="RM Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)

    @commands.command()
    async def daily(self, ctx):
        user_key = str(ctx.author.id)
        now = int(time.time())
        last_claim = int(self.daily_claims.get(user_key, 0) or 0)
        if last_claim:
            elapsed = now - last_claim
            remaining = self.DAILY_COOLDOWN - elapsed
            if remaining > 0:
                await ctx.send(
                    f"You already claimed your daily. Try again in {self._format_cooldown(remaining)}."
                )
                return
        new_balance = self.currency.adjust(ctx.author.id, self.DAILY_REWARD)
        self.daily_claims[user_key] = now
        self._save_daily_claims()
        await ctx.send(
            f"Daily claimed! You received RM {self.DAILY_REWARD}. New balance: RM {new_balance}."
        )

    @commands.command()
    async def donate(self, ctx, *args):
        if len(args) < 2:
            example = "?donate 10 @user\n?donate 10 123456789012345678"
            embed = self._build_usage_embed("?donate <amount> <@user or user_id>", example)
            await ctx.send(embed=embed)
            return
        try:
            amount = int(str(args[0]).strip())
        except ValueError:
            await ctx.send("Amount must be a whole number.")
            return
        target_arg = " ".join(str(part) for part in args[1:]).strip()
        target = None
        try:
            target = await commands.MemberConverter().convert(ctx, target_arg)
        except commands.BadArgument:
            if target_arg.isdigit() and ctx.guild:
                try:
                    target = await ctx.guild.fetch_member(int(target_arg))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    target = None
        if not target:
            await ctx.send("I couldn't find that user.")
            return
        if amount <= 0:
            await ctx.send("Amount must be positive.")
            return
        if target.id == ctx.author.id:
            await ctx.send("You can't donate to yourself.")
            return
        if target.bot:
            await ctx.send("You can't donate to bots.")
            return
        current_balance = self.currency.get_balance(ctx.author.id)
        if amount > current_balance:
            await ctx.send("You don't have enough RM for that donation.")
            return
        new_balance = self.currency.adjust(ctx.author.id, -amount)
        self.currency.adjust(target.id, amount)
        embed = discord.Embed(
            title="Donation Sent",
            description=(
                f"{ctx.author.display_name} sent RM {amount} to {target.display_name}.\n"
                f"New balance: RM {new_balance}."
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.command()
    async def cheat(self, ctx, amount: int, target: discord.Member = None):
        if ctx.author.id != 255365914898333707:
            await ctx.send("You can't use this command.")
            return
        if amount <= 0:
            await ctx.send("Amount must be positive.")
            return
        target = target or ctx.author
        new_balance = self.currency.adjust(target.id, amount)
        await ctx.send(f"Cheat applied to {target.mention}. New balance: RM {new_balance}.")

    @commands.command()
    async def poker(self, ctx, *args):
        user_id = ctx.author.id
        user_key = str(user_id)
        starting_balance = 1000
        if user_key not in self.poker_starters:
            current_balance = self.currency.get_balance(user_id)
            starter_credit = max(starting_balance - current_balance, 0)
            if starter_credit:
                self.currency.adjust(user_id, starter_credit)
                embed = discord.Embed(
                    title="Welcome to Micro Poker",
                    description=(
                        f"{ctx.author.mention}, you’ve been credited with RM {starter_credit} "
                        f"to reach the RM {starting_balance} starter bankroll."
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(
                    name="Commands",
                    value=(
                        "• `?poker <bet> [@user]` to start a hand\n"
                        "• `?balance` or `?bal` to check RM\n"
                        "• `?daily` for a daily reward"
                    ),
                    inline=False,
                )
                await ctx.send(embed=embed)
            self.poker_starters[user_key] = int(time.time())
            self._save_poker_starters()
        if not args:
            example = "?poker 10\n?poker 10 @user"
            embed = self._build_usage_embed("?poker <bet> [@user]", example)
            await ctx.send(embed=embed)
            return

        if args[0].isdigit():
            if user_id in self.poker_games:
                await ctx.send("You already have a poker hand in progress. Use the buttons on the last poker message.")
                return
            opponent = None
            if len(args) > 1:
                if ctx.message.mentions:
                    opponent = ctx.message.mentions[0]
                else:
                    converter = commands.MemberConverter()
                    try:
                        opponent = await converter.convert(ctx, " ".join(args[1:]))
                    except commands.BadArgument:
                        await ctx.send("I couldn't find that user.")
                        return
                if opponent.id == user_id:
                    await ctx.send("You can't challenge yourself.")
                    return
                if opponent.bot:
                    await ctx.send("You can't challenge a bot right now.")
                    return
                if opponent.id in self.poker_games:
                    await ctx.send("That user already has a poker hand in progress.")
                    return
            min_bet = int(args[0])
            if min_bet <= 0:
                await ctx.send("You need to bet a positive amount.")
                return
            current = self.currency.get_balance(user_id)
            if current < min_bet:
                await ctx.send(f"You need at least RM {min_bet} to cover the big blind.")
                return
            opponent_balance = None
            if opponent:
                opponent_balance = self.currency.get_balance(opponent.id)
                if opponent_balance < min_bet:
                    await ctx.send(f"{opponent.display_name} needs at least RM {min_bet} to cover the big blind.")
                    return

            deck = self._build_deck()
            random.shuffle(deck)
            user_cards = [deck.pop() for _ in range(2)]
            bot_cards = [deck.pop() for _ in range(2)]
            community = []
            player_balance = current
            max_bankroll = int(player_balance * 1.5)
            calculated_bankroll = int(player_balance * random.uniform(0.5, 1.5))
            if opponent:
                bot_bankroll = opponent_balance
                bot_shadow_name = opponent.display_name
                bot_shadow_avatar = opponent.display_avatar.url
                bot_personality = None
            else:
                bot_bankroll = max(min_bet, min(calculated_bankroll, max_bankroll))
                bot_shadow_name, bot_shadow_avatar = self._select_bot_shadow(ctx)
                bot_personality = self._choose_bot_personality()
                line = self._pick_persona_line("pre_game", game={"bot_personality": bot_personality})
                await self._send_persona_message(
                    ctx,
                    bot_shadow_name,
                    bot_shadow_avatar,
                    line,
                    game={"bot_personality": bot_personality},
                )
                await asyncio.sleep(2)
            player_avatar = ctx.author.display_avatar.url
            sb_amount = max(1, min_bet // 2)
            bb_amount = min_bet
            sb_player = random.choice(["user", "bot"])
            bb_player = "bot" if sb_player == "user" else "user"
            user_total_bet = 0
            bot_total_bet = 0
            user_round_bet = 0
            bot_round_bet = 0
            pot = 0

            if sb_player == "user":
                self.currency.adjust(user_id, -sb_amount)
                user_total_bet += sb_amount
                user_round_bet += sb_amount
                pot += sb_amount
            else:
                sb_contrib = min(sb_amount, bot_bankroll)
                if opponent:
                    self.currency.adjust(opponent.id, -sb_contrib)
                bot_bankroll -= sb_contrib
                bot_total_bet += sb_contrib
                bot_round_bet += sb_contrib
                pot += sb_contrib

            if bb_player == "user":
                self.currency.adjust(user_id, -bb_amount)
                user_total_bet += bb_amount
                user_round_bet += bb_amount
                pot += bb_amount
            else:
                bb_contrib = min(bb_amount, bot_bankroll)
                if opponent:
                    self.currency.adjust(opponent.id, -bb_contrib)
                bot_bankroll -= bb_contrib
                bot_total_bet += bb_contrib
                bot_round_bet += bb_contrib
                pot += bb_contrib

            shadow_name = bot_shadow_name
            if not opponent and not shadow_name.endswith(" [BOT]"):
                shadow_name = f"{shadow_name} [BOT]"
            if opponent:
                opponent_mention = opponent.mention
                sb_name = ctx.author.mention if sb_player == "user" else opponent_mention
                bb_name = ctx.author.mention if bb_player == "user" else opponent_mention
            else:
                sb_name = ctx.author.mention if sb_player == "user" else shadow_name
                bb_name = ctx.author.mention if bb_player == "user" else shadow_name
            await ctx.send(
                f"{sb_name} posts a small blind of RM {sb_amount}.\n"
                f"{bb_name} posts a big blind of RM {bb_amount}. Now dealing cards..."
            )

            game = {
                "deck": deck,
                "user_cards": user_cards,
                "bot_cards": bot_cards,
                "community": community,
                "stage": "preflop",
                "min_bet": min_bet,
                "max_bet": min_bet * 10,
                "small_blind": sb_amount,
                "big_blind": bb_amount,
                "sb_player": sb_player,
                "bb_player": bb_player,
                "user_total_bet": user_total_bet,
                "bot_total_bet": bot_total_bet,
                "user_round_bet": user_round_bet,
                "bot_round_bet": bot_round_bet,
                "current_bet": bb_amount,
                "raise_count": 0,
                "max_raises": 10,
                "awaiting_call": None,
                "user_acted": False,
                "bot_acted": False,
                "turn": sb_player,
                "pot": pot,
                "user_id": user_id,
                "opponent_id": opponent.id if opponent else None,
                "player_name": ctx.author.display_name,
                "opponent_name": opponent.display_name if opponent else None,
                "opponent_avatar": opponent.display_avatar.url if opponent else None,
                "bot_bankroll": bot_bankroll,
                "bot_personality": bot_personality,
                "bot_all_in": bot_bankroll == 0,
                "user_all_in": self.currency.get_balance(user_id) == 0,
                "bot_allin_capped": False,
                "user_allin_capped": False,
                "bot_status": "Waiting...",
                "bot_shadow_name": bot_shadow_name,
                "bot_shadow_avatar": bot_shadow_avatar,
                "player_avatar": player_avatar,
                "fold_chance": None,
                "locked": False,
                "ctx": ctx,
            }
            self._refresh_fold_chance(game)
            self.poker_games[user_id] = game
            if opponent:
                self.poker_games[opponent.id] = game
            footer = self._turn_prompt(game, game["turn"])
            embed = self._poker_status_embed(ctx, game, footer_text=footer)
            view = PokerView(self, ctx, user_id, opponent_id=opponent.id if opponent else None)
            game["view"] = view
            self._sync_poker_view(game)
            message = await ctx.send(embed=embed, view=view)
            game["message"] = message
            if game["turn"] == "bot" and not opponent:
                await self._bot_take_turn(None, game)
            return

        await ctx.send("Use the buttons on the last poker message to act.")
        return


async def setup(bot):
    await bot.add_cog(Games(bot))
