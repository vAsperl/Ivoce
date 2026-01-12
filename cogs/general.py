import discord
from discord.ext import commands


class HelpView(discord.ui.View):
    def __init__(self, ctx, pages):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.pages = pages
        self.page_index = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_button.disabled = self.page_index <= 0
        self.next_button.disabled = self.page_index >= len(self.pages) - 1

    def _build_embed(self):
        embed = discord.Embed(title="Available Commands")
        cog_name, commands_list = self.pages[self.page_index]
        names = ", ".join(sorted(commands_list))
        embed.add_field(name=cog_name, value=names or "None", inline=False)
        embed.set_footer(text=f"Page {self.page_index + 1} of {len(self.pages)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This help menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = max(0, self.page_index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = min(len(self.pages) - 1, self.page_index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def hello(self, ctx):
        await ctx.send(f"Hello, {ctx.author.mention}!")

    @commands.command()
    async def dm(self, ctx, *, msg):
        await ctx.author.send(f"You said {msg}!")

    @commands.command()
    async def reply(self, ctx):
        await ctx.send(f"{ctx.author.mention}, this is a reply to your command!")

    @commands.command()
    async def poll(self, ctx, *, question):
        embed = discord.Embed(title="Poll", description=question)
        poll_message = await ctx.send(embed=embed)
        await poll_message.add_reaction("👍")
        await poll_message.add_reaction("👎")

    @commands.command(name="help")
    async def help_command(self, ctx):
        commands_by_cog = {}
        for command in self.bot.commands:
            if command.hidden or not command.enabled:
                continue
            try:
                await command.can_run(ctx)
            except commands.CheckFailure:
                continue
            cog_name = command.cog_name or "Other"
            commands_by_cog.setdefault(cog_name, []).append(command.name)

        pages = [(cog, commands_by_cog[cog]) for cog in sorted(commands_by_cog)]
        if not pages:
            await ctx.send("No commands available.")
            return
        if len(pages) == 1:
            embed = discord.Embed(title="Available Commands")
            cog_name, commands_list = pages[0]
            names = ", ".join(sorted(commands_list))
            embed.add_field(name=cog_name, value=names or "None", inline=False)
            await ctx.send(embed=embed)
            return
        view = HelpView(ctx, pages)
        await ctx.send(embed=view._build_embed(), view=view)


async def setup(bot):
    await bot.add_cog(General(bot))
