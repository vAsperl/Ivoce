import discord
from discord.ext import commands


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


async def setup(bot):
    await bot.add_cog(General(bot))
