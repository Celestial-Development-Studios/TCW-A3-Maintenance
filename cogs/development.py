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
