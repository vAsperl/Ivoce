import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from daily_logs import DailyLogHandler, ExcludeLoggerFilter, remove_expired_log_folders

PROJECT_DIR = Path(__file__).resolve().parent
LOCK_FILE = PROJECT_DIR / ".lock"
LAVALINK_JAR = PROJECT_DIR / "Lavalink.jar"
LAVALINK_VERSION_FILE = PROJECT_DIR / ".lavalink-version"
DEFAULT_LAVALINK_RELEASE_API_URL = (
    "https://api.github.com/repos/lavalink-devs/Lavalink/releases/latest"
)


def _forward_lavalink_output(process, logger):
    if process.stdout is None:
        return
    for line in process.stdout:
        message = line.rstrip()
        if not message:
            continue
        level = logging.ERROR if any(
            marker in message.upper() for marker in ("ERROR", "FATAL", "EXCEPTION")
        ) else logging.INFO
        logger.log(level, message)

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

def _ensure_lavalink():
    temporary_jar = LAVALINK_JAR.with_suffix(".jar.download")
    jar_exists = LAVALINK_JAR.is_file()
    auto_update = _env_flag("LAVALINK_AUTO_UPDATE", default=True)

    if jar_exists and not auto_update:
        print("Lavalink automatic updates are disabled.")
        return True

    try:
        release_api_url = os.getenv(
            "LAVALINK_RELEASE_API_URL", DEFAULT_LAVALINK_RELEASE_API_URL
        ).strip()
        release_request = urllib.request.Request(
            release_api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Ivoce-Lavalink-Updater",
            },
        )
        with urllib.request.urlopen(release_request, timeout=15) as response:
            release = json.load(response)

        latest_version = str(release["tag_name"]).strip()
        assets = release.get("assets", [])
        jar_asset = next(
            (asset for asset in assets if asset.get("name") == "Lavalink.jar"),
            None,
        )
        if not latest_version or not jar_asset:
            raise OSError("the latest GitHub release has no Lavalink.jar asset")

        installed_version = ""
        try:
            installed_version = LAVALINK_VERSION_FILE.read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            pass

        if jar_exists and installed_version == latest_version:
            print(f"Lavalink {installed_version} is already up to date.")
            return True

        action = "Updating" if jar_exists else "Downloading"
        current = f" from {installed_version}" if installed_version else ""
        print(f"{action} Lavalink{current} to {latest_version}...")
        download_url = os.getenv(
            "LAVALINK_DOWNLOAD_URL", jar_asset["browser_download_url"]
        ).strip()
        download_request = urllib.request.Request(
            download_url,
            headers={"User-Agent": "Ivoce-Lavalink-Updater"},
        )
        with urllib.request.urlopen(download_request, timeout=60) as response:
            with temporary_jar.open("wb") as output:
                shutil.copyfileobj(response, output)

        if temporary_jar.stat().st_size == 0:
            raise OSError("the downloaded file is empty")
        with temporary_jar.open("rb") as downloaded_file:
            if downloaded_file.read(2) != b"PK":
                raise OSError("the downloaded file is not a valid JAR")

        temporary_jar.replace(LAVALINK_JAR)
        LAVALINK_VERSION_FILE.write_text(latest_version + "\n", encoding="utf-8")
        print(f"Lavalink {latest_version} installed at {LAVALINK_JAR}.")
        return True
    except (KeyError, OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        try:
            temporary_jar.unlink()
        except FileNotFoundError:
            pass
        if jar_exists:
            print(f"Unable to check for Lavalink updates; using the installed JAR: {exc}")
            return True
        print(f"Unable to install Lavalink: {exc}")
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
    lavalink_log_thread = None
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))

        load_dotenv()
        token = os.getenv('DISCORD_TOKEN')

        log_root = Path(os.getenv("LOG_DIR", str(PROJECT_DIR / "logs")))
        retention_days = int(os.getenv("LOG_RETENTION_DAYS", "30"))
        removed_log_folders = remove_expired_log_folders(log_root, retention_days)
        for folder in removed_log_folders:
            print(f"Removed expired log folder: {folder}")

        log_format = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
        )
        discord_handler = DailyLogHandler(log_root, "discord")
        discord_handler.setFormatter(log_format)
        discord_handler.addFilter(ExcludeLoggerFilter("poker", "lavalink.process"))
        discord.utils.setup_logging(handler=discord_handler, level=logging.DEBUG)

        error_handler = DailyLogHandler(log_root, "error")
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(log_format)
        logging.getLogger().addHandler(error_handler)

        lavalink_logger = logging.getLogger("lavalink.process")
        lavalink_logger.setLevel(logging.INFO)
        lavalink_handler = DailyLogHandler(log_root, "lavalink")
        lavalink_handler.setFormatter(log_format)
        lavalink_logger.addHandler(lavalink_handler)

        start_lavalink = not _env_flag("DISABLE_LAVALINK", default=False)
        if start_lavalink:
            if not _ensure_lavalink():
                print("Lavalink installation failed. Bot will not start.")
                return

            java_exec = shutil.which("java")
            if java_exec:
                try:
                    lavalink_proc = subprocess.Popen(
                        [java_exec, "-jar", str(LAVALINK_JAR)],
                        cwd=PROJECT_DIR,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                    lavalink_log_thread = threading.Thread(
                        target=_forward_lavalink_output,
                        args=(lavalink_proc, lavalink_logger),
                        name="lavalink-log-forwarder",
                        daemon=True,
                    )
                    lavalink_log_thread.start()
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
        if lavalink_log_thread and lavalink_log_thread.is_alive():
            lavalink_log_thread.join(timeout=2)

if __name__ == '__main__':
    asyncio.run(main())
