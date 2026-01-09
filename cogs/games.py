import discord
from discord.ext import commands
import itertools
import random
import asyncio
import json
import os


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


class PokerBetModal(discord.ui.Modal):
    def __init__(self, cog, ctx, user_id):
        super().__init__(title="Poker Bet")
        self.cog = cog
        self.ctx = ctx
        self.user_id = user_id
        self.amount = discord.ui.TextInput(
            label="Bet amount",
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
        await self.cog._handle_poker_action(interaction, "bet", amount=amount)


class PokerView(discord.ui.View):
    def __init__(self, cog, ctx, user_id, timeout=120):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        game = self.cog.poker_games.get(self.user_id)
        if not game or not game.get("message"):
            return
        for item in self.children:
            item.disabled = True
        embed = self.cog._poker_status_embed(self.ctx, game, footer_text="Hand timed out.")
        await game["message"].edit(embed=embed, view=self)
        self.cog.poker_games.pop(self.user_id, None)

    @discord.ui.button(label="Check", style=discord.ButtonStyle.secondary)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_poker_action(interaction, "check")

    @discord.ui.button(label="Bet", style=discord.ButtonStyle.primary)
    async def bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PokerBetModal(self.cog, self.ctx, self.user_id))

    @discord.ui.button(label="All-in", style=discord.ButtonStyle.danger)
    async def allin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_poker_action(interaction, "allin")

    @discord.ui.button(label="Fold", style=discord.ButtonStyle.secondary)
    async def fold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_poker_action(interaction, "fold")


class Games(commands.Cog):
    RANK_ORDER = "23456789TJQKA"
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
        self.poker_games = {}

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
        embed = discord.Embed(
            title=f"Micro Poker - {self._poker_stage_label(stage)}",
            description=f"{ctx.author.mention} vs. the bot",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Your hand",
            value=self._format_cards(game["user_cards"]),
            inline=False,
        )
        community = game["community"]
        embed.add_field(
            name="Community",
            value=self._format_cards(community) if community else "No cards yet.",
            inline=False,
        )
        if stage == "showdown":
            embed.add_field(
                name="Bot hand",
                value=self._format_cards(game["bot_cards"]),
                inline=False,
            )
        embed.add_field(
            name="Bot",
            value=game.get("bot_status", "Waiting..."),
            inline=True,
        )
        embed.add_field(
            name="Your total bet",
            value=f"{game['user_total_bet']} credits",
            inline=True,
        )
        if footer_text:
            embed.set_footer(text=footer_text)
        return embed

    async def _bot_think(self, ctx):
        await asyncio.sleep(random.uniform(0.8, 2.4))

    async def _update_interaction(self, interaction, embed, view=None):
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def _finish_poker(self, interaction, game, embed, message_text=None):
        view = game.get("view")
        if view:
            for item in view.children:
                item.disabled = True
        self.poker_games.pop(interaction.user.id, None)
        await self._update_interaction(interaction, embed, view=view)
        if message_text:
            await interaction.followup.send(message_text)

    async def _advance_stage(self, game):
        if game["stage"] == "preflop":
            game["community"].append(game["deck"].pop())
            game["stage"] = "flop"
            return "Flop dealt. Your move."
        if game["stage"] == "flop":
            game["community"].append(game["deck"].pop())
            game["stage"] = "turn"
            return "Turn dealt. Your move."
        if game["stage"] == "turn":
            game["community"].append(game["deck"].pop())
            game["stage"] = "river"
            return "River dealt. Your move."
        return None

    async def _handle_poker_action(self, interaction, action, amount=None):
        user_id = interaction.user.id
        game = self.poker_games.get(user_id)
        if not game:
            await interaction.response.send_message("You don't have an active hand. Start one with ?poker <bet>.", ephemeral=True)
            return
        if game.get("locked"):
            await interaction.response.send_message("Hold on, finishing the last action.", ephemeral=True)
            return
        game["locked"] = True
        view = game.get("view")

        if action == "fold":
            game["bot_status"] = "You folded."
            embed = self._poker_status_embed(game["ctx"], game, footer_text="Hand over.")
            await self._finish_poker(interaction, game, embed)
            return

        if action == "bet":
            if amount is None or amount <= 0:
                await interaction.response.send_message("Bet amount must be positive.", ephemeral=True)
                game["locked"] = False
                return
            current = self.currency.get_balance(user_id)
            if amount > current:
                await interaction.response.send_message("You don't have enough credits for that bet.", ephemeral=True)
                game["locked"] = False
                return
            self.currency.adjust(user_id, -amount)
            game["user_total_bet"] += amount
            game["bot_status"] = "Thinking..."
            embed = self._poker_status_embed(game["ctx"], game, footer_text="Bot is deciding...")
            await self._update_interaction(interaction, embed, view=view)
            await self._bot_think(game["ctx"])

            if random.random() < 0.2:
                payout = game["user_total_bet"] * 2
                self.currency.adjust(user_id, payout)
                game["bot_status"] = "Bot folds."
                embed = self._poker_status_embed(game["ctx"], game, footer_text=f"You win {game['user_total_bet']} credits!")
                await self._finish_poker(interaction, game, embed)
                return
            game["bot_status"] = "Bot calls."

        elif action in ("allin", "all-in"):
            current = self.currency.get_balance(user_id)
            if current <= 0:
                await interaction.response.send_message("You don't have any credits to go all-in.", ephemeral=True)
                game["locked"] = False
                return
            self.currency.adjust(user_id, -current)
            game["user_total_bet"] += current
            game["bot_status"] = "Thinking..."
            embed = self._poker_status_embed(game["ctx"], game, footer_text="Bot is deciding...")
            await self._update_interaction(interaction, embed, view=view)
            await self._bot_think(game["ctx"])

            if random.random() < 0.35:
                payout = game["user_total_bet"] * 2
                self.currency.adjust(user_id, payout)
                game["bot_status"] = "Bot folds."
                embed = self._poker_status_embed(game["ctx"], game, footer_text=f"You win {game['user_total_bet']} credits!")
                await self._finish_poker(interaction, game, embed)
                return
            game["bot_status"] = "Bot calls your all-in."

        elif action == "check":
            game["bot_status"] = "Thinking..."
            embed = self._poker_status_embed(game["ctx"], game, footer_text="Bot is deciding...")
            await self._update_interaction(interaction, embed, view=view)
            await self._bot_think(game["ctx"])

            if random.random() < 0.1:
                payout = game["user_total_bet"] * 2
                self.currency.adjust(user_id, payout)
                game["bot_status"] = "Bot folds."
                embed = self._poker_status_embed(game["ctx"], game, footer_text=f"You win {game['user_total_bet']} credits!")
                await self._finish_poker(interaction, game, embed)
                return
            game["bot_status"] = "Bot checks."
        else:
            await interaction.response.send_message("Invalid action.", ephemeral=True)
            game["locked"] = False
            return

        if game["stage"] == "river":
            game["stage"] = "showdown"
            user_best = self._best_hand(game["user_cards"] + game["community"])
            bot_best = self._best_hand(game["bot_cards"] + game["community"])
            user_wins = self._compare_hands(user_best, bot_best)

            if user_wins:
                payout = game["user_total_bet"] * 2
                self.currency.adjust(user_id, payout)
                result_text = f"You win {game['user_total_bet']} credits!"
            elif user_best == bot_best:
                payout = game["user_total_bet"]
                self.currency.adjust(user_id, payout)
                result_text = "It's a tie! Bet returned."
            else:
                result_text = f"You lose {game['user_total_bet']} credits."

            game["bot_status"] = "Showdown."
            embed = self._poker_status_embed(game["ctx"], game, footer_text=result_text)
            embed.add_field(
                name="Your hand rank",
                value=self.CATEGORY_NAMES[user_best[0]],
                inline=True,
            )
            embed.add_field(
                name="Bot hand rank",
                value=self.CATEGORY_NAMES[bot_best[0]],
                inline=True,
            )
            await self._finish_poker(interaction, game, embed)
            return

        footer = await self._advance_stage(game)
        embed = self._poker_status_embed(game["ctx"], game, footer_text=footer)
        await self._update_interaction(interaction, embed, view=view)
        game["locked"] = False

    @commands.command()
    async def balance(self, ctx):
        bal = self.currency.get_balance(ctx.author.id)
        await ctx.send(f"{ctx.author.mention}, you have {bal} credits.")

    @commands.command()
    async def cheat(self, ctx, amount: int):
        if ctx.author.id != 255365914898333707:
            await ctx.send("You can't use this command.")
            return
        if amount <= 0:
            await ctx.send("Amount must be positive.")
            return
        new_balance = self.currency.adjust(ctx.author.id, amount)
        await ctx.send(f"Cheat applied. New balance: {new_balance} credits.")

    @commands.command()
    async def poker(self, ctx, *args):
        user_id = ctx.author.id
        if not args:
            await ctx.send("Usage: ?poker <bet>")
            return

        if args[0].isdigit():
            if user_id in self.poker_games:
                await ctx.send("You already have a poker hand in progress. Use the buttons on the last poker message.")
                return
            bet = int(args[0])
            if bet <= 0:
                await ctx.send("You need to bet a positive amount.")
                return
            current = self.currency.get_balance(user_id)
            if bet > current:
                await ctx.send("You don't have enough credits for that bet.")
                return

            deck = self._build_deck()
            random.shuffle(deck)
            user_cards = [deck.pop() for _ in range(2)]
            bot_cards = [deck.pop() for _ in range(2)]
            community = [deck.pop() for _ in range(2)]
            self.currency.adjust(user_id, -bet)
            game = {
                "deck": deck,
                "user_cards": user_cards,
                "bot_cards": bot_cards,
                "community": community,
                "stage": "preflop",
                "user_total_bet": bet,
                "bot_status": "Waiting...",
                "locked": False,
                "ctx": ctx,
            }
            self.poker_games[user_id] = game
            embed = self._poker_status_embed(ctx, game, footer_text="Your move: check, bet, all-in, or fold.")
            view = PokerView(self, ctx, user_id)
            message = await ctx.send(embed=embed, view=view)
            game["message"] = message
            game["view"] = view
            return

        await ctx.send("Use the buttons on the last poker message to act.")
        return


async def setup(bot):
    await bot.add_cog(Games(bot))
