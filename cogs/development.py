import sys
import time as _time

import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS


def is_developer():
    """App-command check: only users in DEVELOPER_IDS may proceed."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in DEVELOPER_IDS
    return app_commands.check(predicate)


class DevelopmentCog(commands.Cog, name="Development"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Only bot developers can use cog management commands.", ephemeral=True
                )

    # ── Commands ──────────────────────────────────────────────────────────────

    @app_commands.command(name="ping", description="Advanced bot diagnostics (developer only)")
    @is_developer()
    async def ping(self, interaction: discord.Interaction):
        t_start = _time.perf_counter()
        await interaction.response.defer(ephemeral=True)

        # Database latency
        db_start = _time.perf_counter()
        await interaction.client.db.get_guild_settings(interaction.guild.id if interaction.guild else 0)
        db_ms = (_time.perf_counter() - db_start) * 1000

        rtt_ms = (_time.perf_counter() - t_start) * 1000
        ws_ms = self.bot.latency * 1000

        # Uptime
        if hasattr(self.bot, "start_time"):
            delta = discord.utils.utcnow() - self.bot.start_time
            s = int(delta.total_seconds())
            h, rem = divmod(s, 3600)
            m, sec = divmod(rem, 60)
            uptime = f"{h}h {m}m {sec}s"
        else:
            uptime = "N/A"

        guild_count = len(self.bot.guilds)
        user_count = sum(g.member_count or 0 for g in self.bot.guilds)
        cog_count = len(self.bot.cogs)

        def _latency_bar(ms: float) -> str:
            if ms < 100:
                return "🟢"
            if ms < 250:
                return "🟡"
            return "🔴"

        embed = discord.Embed(title="🏓 Pong! — Developer Diagnostics", color=0x5865F2)
        embed.add_field(
            name="Latency",
            value=(
                f"{_latency_bar(ws_ms)} WebSocket  `{ws_ms:.1f} ms`\n"
                f"{_latency_bar(rtt_ms)} Round-trip `{rtt_ms:.1f} ms`\n"
                f"{_latency_bar(db_ms)} Database   `{db_ms:.2f} ms`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Bot",
            value=(
                f"**User:** {self.bot.user.mention} (`{self.bot.user.id}`)\n"
                f"**Uptime:** `{uptime}`\n"
                f"**Guilds:** `{guild_count}` · **Users:** `{user_count}` · **Cogs:** `{cog_count}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Environment",
            value=(
                f"Python `{sys.version.split()[0]}`\n"
                f"discord.py `{discord.__version__}`"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Requested by {interaction.user}")
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sync", description="Clear duplicate guild commands and re-sync globally (developer only)")
    @is_developer()
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Push empty list to this guild — removes any guild-level duplicates
        self.bot.tree.clear_commands(guild=interaction.guild)
        await self.bot.tree.sync(guild=interaction.guild)

        # Re-sync globally
        cmds = await self.bot.tree.sync()
        await interaction.followup.send(
            f"Cleared guild commands and synced **{len(cmds)}** commands globally.",
            ephemeral=True,
        )

    @app_commands.command(name="reload", description="Reload a cog (developer only)")
    @app_commands.describe(cog="Cog to reload, e.g. cogs.panel")
    @is_developer()
    async def reload(self, interaction: discord.Interaction, cog: str):
        try:
            await self.bot.reload_extension(cog)
            await interaction.response.send_message(f"Reloaded `{cog}`.", ephemeral=True)
        except commands.ExtensionNotLoaded:
            await interaction.response.send_message(f"`{cog}` is not loaded.", ephemeral=True)
        except commands.ExtensionNotFound:
            await interaction.response.send_message(f"`{cog}` not found.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(
                f"Error reloading `{cog}`:\n```\n{e}\n```", ephemeral=True
            )

    @app_commands.command(name="load", description="Load a cog (developer only)")
    @app_commands.describe(cog="Cog to load, e.g. cogs.panel")
    @is_developer()
    async def load(self, interaction: discord.Interaction, cog: str):
        try:
            await self.bot.load_extension(cog)
            await interaction.response.send_message(f"Loaded `{cog}`.", ephemeral=True)
        except commands.ExtensionAlreadyLoaded:
            await interaction.response.send_message(f"`{cog}` is already loaded.", ephemeral=True)
        except commands.ExtensionNotFound:
            await interaction.response.send_message(f"`{cog}` not found.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(
                f"Error loading `{cog}`:\n```\n{e}\n```", ephemeral=True
            )

    @app_commands.command(name="unload", description="Unload a cog (developer only)")
    @app_commands.describe(cog="Cog to unload, e.g. cogs.panel")
    @is_developer()
    async def unload(self, interaction: discord.Interaction, cog: str):
        if cog == "cogs.development":
            return await interaction.response.send_message(
                "Cannot unload the development cog.", ephemeral=True
            )
        try:
            await self.bot.unload_extension(cog)
            await interaction.response.send_message(f"Unloaded `{cog}`.", ephemeral=True)
        except commands.ExtensionNotLoaded:
            await interaction.response.send_message(f"`{cog}` is not loaded.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(
                f"Error unloading `{cog}`:\n```\n{e}\n```", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(DevelopmentCog(bot))
