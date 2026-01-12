import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time

import discord
from discord.ext import commands
from dotenv import load_dotenv

LOCK_FILE = ".lock"

def _env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def _wait_for_lavalink(host, port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False

async def main():
    if os.path.exists(LOCK_FILE):
        print("Another instance of the bot is already running.")
        return

    lavalink_proc = None
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))

        load_dotenv()
        token = os.getenv('DISCORD_TOKEN')

        start_lavalink = not _env_flag("DISABLE_LAVALINK", default=False)
        if start_lavalink:
            java_exec = shutil.which("java")
            if java_exec:
                try:
                    lavalink_proc = subprocess.Popen(
                        [java_exec, "-jar", "Lavalink.jar"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT,
                    )
                    print("Lavalink process started alongside the bot.")
                except Exception as exc:
                    print(f"Unable to start Lavalink locally: {exc}")
            else:
                print("Java executable not found in PATH; start Lavalink manually.")

            lavalink_host = os.getenv("LAVALINK_HOST", "127.0.0.1")
            lavalink_port = int(os.getenv("LAVALINK_PORT", "2333"))
            if not _wait_for_lavalink(lavalink_host, lavalink_port, timeout=15):
                print(f"Lavalink is not reachable at {lavalink_host}:{lavalink_port}. Bot will not start.")
                return
        else:
            print("Lavalink startup disabled via DISABLE_LAVALINK.")

        handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        bot = commands.Bot(command_prefix='?', intents=intents, help_command=None)

        # Load cogs
        enabled_cogs = os.getenv("ENABLE_COGS", "").strip()
        disabled_cogs = os.getenv("DISABLE_COGS", "").strip()
        enabled_set = None
        if enabled_cogs:
            enabled_set = {name.strip().lower() for name in enabled_cogs.split(",") if name.strip()}
        disabled_set = {name.strip().lower() for name in disabled_cogs.split(",") if name.strip()}
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                cog_name = filename[:-3]
                cog_key = cog_name.lower()
                if enabled_set is not None and cog_key not in enabled_set:
                    print(f"Skipped {filename} (not in ENABLE_COGS).")
                    continue
                if cog_key in disabled_set:
                    print(f"Skipped {filename} (in DISABLE_COGS).")
                    continue
                try:
                    await bot.load_extension(f'cogs.{cog_name}')
                    print(f'Loaded {filename}')
                except Exception as e:
                    print(f'Failed to load {filename}: {e}')

        discord.utils.setup_logging(handler=handler, level=logging.DEBUG)
        await bot.start(token)
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        if lavalink_proc and lavalink_proc.poll() is None:
            lavalink_proc.terminate()
            try:
                lavalink_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                lavalink_proc.kill()

if __name__ == '__main__':
    asyncio.run(main())
