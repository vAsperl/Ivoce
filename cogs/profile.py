import asyncio
import io
import logging
import re
import time
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.gothic_font_path = "assets/fonts/UnifrakturCook-Regular.ttf"

    def _games(self):
        return self.bot.get_cog("Games")

    def _load_font(self, size, *, bold=False, candidates=None):
        if candidates is None:
            candidates = [
                self.gothic_font_path,
                "DejaVuSerif-Bold.ttf" if bold else "DejaVuSerif.ttf",
                "LiberationSerif-Bold.ttf" if bold else "LiberationSerif-Regular.ttf",
                "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
            ]
        for font_name in candidates:
            try:
                self.logger.debug("Profile font loaded: %s (size=%s, bold=%s)", font_name, size, bold)
                return ImageFont.truetype(font_name, size)
            except OSError:
                continue
        self.logger.debug("Profile font fallback: default (size=%s, bold=%s)", size, bold)
        return ImageFont.load_default()

    def _normalize_imgur_url(self, url):
        if not url:
            return None
        match = re.match(
            r"^https?://(?:i\.)?imgur\.com/([A-Za-z0-9]+)(?:\.([a-zA-Z0-9]+))?/?$",
            url.strip(),
        )
        if not match:
            return None
        image_id = match.group(1)
        ext = match.group(2) or "png"
        return f"https://i.imgur.com/{image_id}.{ext}"

    async def _fetch_image(self, url):
        if not url:
            return None
        timeout = aiohttp.ClientTimeout(total=8)
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        self.logger.debug("Profile bg fetch failed: status=%s url=%s", resp.status, url)
                        return None
                    content_type = resp.headers.get("Content-Type", "")
                    if not content_type.startswith("image/"):
                        self.logger.debug("Profile bg fetch failed: content_type=%s url=%s", content_type, url)
                        return None
                    data = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self.logger.exception("Profile bg fetch exception: url=%s", url)
            return None
        try:
            image = Image.open(io.BytesIO(data)).convert("RGBA")
            self.logger.debug("Profile bg fetch ok: url=%s size=%s", url, image.size)
            return image
        except OSError:
            self.logger.debug("Profile bg fetch failed: invalid image url=%s", url)
            return None

    def _center_crop(self, image, width, height):
        bg_w, bg_h = image.size
        scale = max(width / bg_w, height / bg_h)
        new_w = int(bg_w * scale)
        new_h = int(bg_h * scale)
        image = image.resize((new_w, new_h))
        left = max(0, (new_w - width) // 2)
        top = max(0, (new_h - height) // 2)
        return image.crop((left, top, left + width, top + height))

    def _truncate_text(self, draw, text, font, max_width):
        if draw.textlength(text, font=font) <= max_width:
            return text
        trimmed = text
        while trimmed and draw.textlength(f"{trimmed}...", font=font) > max_width:
            trimmed = trimmed[:-1]
        return f"{trimmed}..." if trimmed else text

    async def _render_profile_card(self, user, games):
        width, height = 800, 420
        base = Image.new("RGBA", (width, height), (245, 246, 250, 255))
        profile = games.poker_profiles.get(str(user.id), {})
        bg_url = self._normalize_imgur_url(profile.get("profile_bg"))
        self.logger.debug("Profile render: user=%s bg_url=%s", user.id, bg_url)
        bg = await self._fetch_image(bg_url) if bg_url else None
        if bg:
            bg = self._center_crop(bg, width, height)
            base.paste(bg, (0, 0))
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 30))
            base = Image.alpha_composite(base, overlay)
        draw = ImageDraw.Draw(base)

        avatar_bytes = await user.display_avatar.read()
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        avatar = avatar.resize((110, 110))
        mask = Image.new("L", (110, 110), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, 110, 110), fill=255)
        avatar_x = (width - 110) // 2
        avatar_y = 70
        base.paste(avatar, (avatar_x, avatar_y), mask)

        name_font = self._load_font(36)
        tag_font = self._load_font(22)
        label_font = self._load_font(18)
        value_font = self._load_font(26)
        small_value_font = self._load_font(22)

        name_text = user.display_name
        name_w = draw.textlength(name_text, font=name_font)
        name_x = (width - name_w) / 2
        draw.text((name_x, 190), name_text, fill=(0, 0, 0, 255), font=name_font)
        tag_text = f"@{user.name}"
        tag_w = draw.textlength(tag_text, font=tag_font)
        tag_x = (width - tag_w) / 2
        draw.text((tag_x, 224), tag_text, fill=(0, 0, 0, 255), font=tag_font)

        balance = games.currency.get_balance(user.id)
        actions = profile.get("actions", 0)
        allin = profile.get("allin", 0)
        allin_rate = f"{(allin / actions * 100):.1f}%" if actions else "N/A"
        last_action_ts = profile.get("last_action_ts")
        last_action_text = "No data"
        if last_action_ts:
            elapsed = max(0, int(time.time() - int(last_action_ts)))
            last_action_text = f"{games._format_cooldown(elapsed)} ago"

        now = int(time.time())
        last_claim = int(games.daily_claims.get(str(user.id), 0) or 0)
        remaining = max(0, games.DAILY_COOLDOWN - (now - last_claim))
        daily_text = "Ready" if remaining <= 0 else f"In {games._format_cooldown(remaining)}"

        fields = [
            ("Balance", f"RM {balance}"),
            ("Daily", daily_text),
            ("Poker Actions", str(actions)),
            ("All-in Rate", allin_rate),
            ("Last Poker", last_action_text),
        ]
        grid_left = 40
        grid_top = 250
        grid_gap_x = 20
        grid_gap_y = 18
        card_w = (width - (grid_left * 2) - grid_gap_x) // 2
        card_h = 70
        total_rows = (len(fields) + 1) // 2
        total_h = total_rows * card_h + (total_rows - 1) * grid_gap_y
        grid_top = grid_top + max(0, (height - grid_top - total_h - 30) // 2)
        for idx, (label, value) in enumerate(fields):
            col = idx % 2
            row = idx // 2
            x = grid_left + col * (card_w + grid_gap_x)
            y = grid_top + row * (card_h + grid_gap_y)
            draw.rounded_rectangle(
                (x, y, x + card_w, y + card_h),
                radius=12,
                fill=(255, 255, 255, 170),
            )
            draw.text((x + 16, y + 12), label.upper(), fill=(0, 0, 0, 255), font=label_font)
            max_value_width = card_w - 32
            value_text = self._truncate_text(draw, value, value_font, max_value_width)
            value_w = draw.textlength(value_text, font=value_font)
            value_x = x + (card_w - value_w) / 2
            draw.text((value_x, y + 36), value_text, fill=(0, 0, 0, 255), font=value_font)

        output = io.BytesIO()
        base.save(output, format="PNG")
        output.seek(0)
        return output

    @commands.command(name="profile")
    async def profile(self, ctx, user: Optional[discord.User] = None):
        games = self._games()
        if not games:
            await ctx.send("Profile system is unavailable right now.")
            return
        target = user or ctx.author
        try:
            image_fp = await self._render_profile_card(target, games)
        except Exception:
            await ctx.send("Couldn't build the profile card right now.")
            return
        await ctx.send(file=discord.File(fp=image_fp, filename="profile.png"))

    @commands.command(name="profilebg")
    async def profilebg(self, ctx, url: Optional[str] = None):
        games = self._games()
        if not games:
            await ctx.send("Profile system is unavailable right now.")
            return
        if not url:
            await ctx.send("Usage: `?profilebg <imgur link>`")
            return
        normalized = self._normalize_imgur_url(url)
        if not normalized:
            await ctx.send("Please use a direct Imgur link (imgur.com or i.imgur.com).")
            return
        profile = games.poker_profiles.get(str(ctx.author.id), {"actions": 0, "allin": 0})
        profile["profile_bg"] = normalized
        games.poker_profiles[str(ctx.author.id)] = profile
        games._save_poker_profiles()
        await ctx.send("Profile background updated.")


async def setup(bot):
    await bot.add_cog(Profile(bot))
