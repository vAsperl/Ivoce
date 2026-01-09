import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio

LOCK_FILE = ".lock"

async def main():
    if os.path.exists(LOCK_FILE):
        print("Another instance of the bot is already running.")
        return

    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))

        load_dotenv()
        token = os.getenv('DISCORD_TOKEN')

        handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        bot = commands.Bot(command_prefix='?', intents=intents)

        # Load cogs
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await bot.load_extension(f'cogs.{filename[:-3]}')
                    print(f'Loaded {filename}')
                except Exception as e:
                    print(f'Failed to load {filename}: {e}')

        discord.utils.setup_logging(handler=handler, level=logging.DEBUG)
        await bot.start(token)
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)

if __name__ == '__main__':
    asyncio.run(main())