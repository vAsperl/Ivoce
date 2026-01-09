from discord.ext import commands

class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"Bot is ready and online! {self.bot.user.name}")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        print(f"Welcome to the server {member.name}!")

    ###@commands.Cog.listener()
    ###async def on_message(self, message):
    ###    if message.author == self.bot.user:
    ###        return
    
    ###    if "shit" in message.content.lower():
    ###        await message.delete()
    ###        await message.channel.send(f"{message.author.mention}, don't use that word!")

async def setup(bot):
    await bot.add_cog(Events(bot))
