import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import discord
from discord.ext import commands
from dotenv import load_dotenv

LOCK_FILE = ".lock"
DEFAULT_LAVALINK_JAR = "Lavalink.jar"
DEFAULT_LAVALINK_DOWNLOAD_URL = (
    "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar"
)


class _BelowErrorFilter(logging.Filter):
    def filter(self, record):
        return record.levelno < logging.ERROR


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


def _ensure_lavalink(jar_path, download_url):
    """Download Lavalink on first run, leaving existing installations untouched."""
    if os.path.isfile(jar_path):
        return True

    destination = os.path.abspath(jar_path)
    partial = f"{destination}.part"
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)

    print(f"Lavalink was not found at {jar_path}; downloading it...")
    try:
        request = urllib.request.Request(
            download_url,
            headers={"User-Agent": "Ivoce-Lavalink-Installer/1.0"},
        )
        with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as output:
            shutil.copyfileobj(response, output)

        # JAR files are ZIP archives. This catches HTML error pages and empty downloads.
        with open(partial, "rb") as downloaded:
            if downloaded.read(4) != b"PK\x03\x04":
                raise ValueError("the downloaded file is not a valid JAR")

        os.replace(partial, destination)
        print(f"Lavalink downloaded to {jar_path}.")
        return True
    except Exception as exc:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass
        print(f"Unable to download Lavalink: {exc}")
        return False

async def main():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as fh:
                raw_pid = fh.read().strip()
            pid = int(raw_pid) if raw_pid else None
        except (OSError, ValueError):
            pid = None
        if pid:
            try:
                os.kill(pid, 0)
                print("Another instance of the bot is already running.")
                return
            except OSError:
                pass
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

    lavalink_proc = None
    lavalink_log = None
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))

        load_dotenv()
        token = os.getenv('DISCORD_TOKEN')

        start_lavalink = not _env_flag("DISABLE_LAVALINK", default=False)
        if start_lavalink:
            lavalink_jar = os.getenv("LAVALINK_JAR", DEFAULT_LAVALINK_JAR)
            lavalink_download_url = os.getenv(
                "LAVALINK_DOWNLOAD_URL", DEFAULT_LAVALINK_DOWNLOAD_URL
            )
            lavalink_available = _ensure_lavalink(lavalink_jar, lavalink_download_url)
            java_exec = shutil.which("java")
            if java_exec and lavalink_available:
                try:
                    lavalink_log = open("lavalink.log", "w", encoding="utf-8")
                    lavalink_proc = subprocess.Popen(
                        [java_exec, "-jar", lavalink_jar],
                        stdout=lavalink_log,
                        stderr=subprocess.STDOUT,
                    )
                    print("Lavalink process started alongside the bot.")
                except Exception as exc:
                    print(f"Unable to start Lavalink locally: {exc}")
            else:
                if not java_exec:
                    print("Java executable not found in PATH; install Java to run Lavalink.")

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

        command_prefix = os.getenv("COMMAND_PREFIX", "#").strip() or "#"
        bot = commands.Bot(command_prefix=command_prefix, intents=intents, help_command=None)

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
        handler.addFilter(_BelowErrorFilter())

        error_handler = logging.FileHandler(
            filename='error.log',
            encoding='utf-8',
            mode='w',
        )
        error_handler.setLevel(logging.ERROR)
        if handler.formatter:
            error_handler.setFormatter(handler.formatter)
        logging.getLogger().addHandler(error_handler)
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
        if lavalink_log:
            lavalink_log.close()

if __name__ == '__main__':
    asyncio.run(main())
