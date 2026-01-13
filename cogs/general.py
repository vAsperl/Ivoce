import discord
from discord.ext import commands
import os
import platform
import time


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

    def _format_duration(self, seconds):
        seconds = max(0, int(seconds))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def _format_bytes(self, value):
        if value is None:
            return None
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _get_uptime_seconds(self):
        try:
            if os.name == "nt":
                import ctypes

                return int(ctypes.windll.kernel32.GetTickCount64() / 1000)
            with open("/proc/uptime", "r", encoding="utf-8") as fh:
                return int(float(fh.read().split()[0]))
        except (OSError, ValueError, IndexError):
            return None

    def _get_memory_usage(self):
        try:
            import psutil

            vm = psutil.virtual_memory()
            return int(vm.used), int(vm.total)
        except Exception:
            pass
        if os.name == "nt":
            try:
                import ctypes

                class MemoryStatusEx(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                status = MemoryStatusEx()
                status.dwLength = ctypes.sizeof(MemoryStatusEx)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    used = int(status.ullTotalPhys - status.ullAvailPhys)
                    return used, int(status.ullTotalPhys)
            except Exception:
                return None, None
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                data = fh.read().splitlines()
            mem_total = None
            mem_available = None
            for line in data:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1]) * 1024
            if mem_total is not None and mem_available is not None:
                return mem_total - mem_available, mem_total
        except (OSError, ValueError, IndexError):
            pass
        return None, None

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

    @commands.command()
    async def system(self, ctx):
        os_label = f"{platform.system()} {platform.release()}".strip()
        uptime_seconds = self._get_uptime_seconds()
        uptime_text = self._format_duration(uptime_seconds) if uptime_seconds is not None else "Unavailable"
        used_mem, total_mem = self._get_memory_usage()
        if used_mem is not None and total_mem is not None:
            mem_text = f"{self._format_bytes(used_mem)} / {self._format_bytes(total_mem)}"
        else:
            mem_text = "Unavailable"
        embed = discord.Embed(
            title="System Info",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Operating system",
            value=f"`{os_label or 'Unknown'}`",
            inline=False,
        )
        embed.add_field(
            name="Uptime",
            value=f"`{uptime_text}`",
            inline=False,
        )
        embed.add_field(
            name="RAM usage",
            value=f"`{mem_text}`",
            inline=False,
        )
        await ctx.send(embed=embed)

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
