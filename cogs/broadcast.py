# Merge Relay Bot


import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS

# Role pinged on every broadcast
BROADCAST_ROLE_ID = 1496496458160148480


# ── Permission helpers ────────────────────────────────────────────────────────

def is_developer():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in DEVELOPER_IDS
    return app_commands.check(predicate)


async def _is_management_or_dev(interaction: discord.Interaction) -> bool:
    if interaction.user.id in DEVELOPER_IDS:
        return True
    if interaction.guild is None:
        return False
    config = await interaction.client.db.get_guild_config(interaction.guild.id)
    if not config or not config.get("management_role_id"):
        return interaction.user.guild_permissions.administrator
    role = interaction.guild.get_role(config["management_role_id"])
    if role:
        return role in interaction.user.roles
    return interaction.user.guild_permissions.administrator


def management_or_dev():
    return app_commands.check(_is_management_or_dev)


# ── Cog ───────────────────────────────────────────────────────────────────────

class BroadcastCog(commands.Cog, name="Broadcast"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = "You don't have permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /register group ───────────────────────────────────────────────────────

    register = app_commands.Group(
        name="register",
        description="Register channels for broadcast features",
    )

    @register.command(name="relay", description="Set the channel for relay broadcasts")
    @app_commands.describe(channel="Channel to receive relay broadcasts")
    @is_developer()
    async def register_relay(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await interaction.client.db.set_guild_setting(
            interaction.guild.id, "broadcast.relay_channel_id", channel.id
        )
        embed = discord.Embed(
            title="Relay Channel Registered",
            description=f"Relay broadcasts will be sent to {channel.mention}.",
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @register.command(name="image", description="Set the channel for image broadcasts")
    @app_commands.describe(channel="Channel to receive image broadcasts")
    @is_developer()
    async def register_image(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await interaction.client.db.set_guild_setting(
            interaction.guild.id, "broadcast.image_channel_id", channel.id
        )
        embed = discord.Embed(
            title="Image Channel Registered",
            description=f"Image broadcasts will be sent to {channel.mention}.",
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @register.command(name="video", description="Set the channel for video broadcasts")
    @app_commands.describe(channel="Channel to receive video broadcasts")
    @is_developer()
    async def register_video(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await interaction.client.db.set_guild_setting(
            interaction.guild.id, "broadcast.video_channel_id", channel.id
        )
        embed = discord.Embed(
            title="Video Channel Registered",
            description=f"Video broadcasts will be sent to {channel.mention}.",
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /image ────────────────────────────────────────────────────────────────

    @app_commands.command(name="image", description="Broadcast an image to the registered image channel")
    @app_commands.describe(
        attachment="An image file to broadcast",
        url="A direct image URL to broadcast",
    )
    @management_or_dev()
    async def broadcast_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment = None,
        url: str = None,
    ):
        if not attachment and not url:
            await interaction.response.send_message(
                "Please provide either an image attachment or a URL.", ephemeral=True
            )
            return

        channel_id = await interaction.client.db.get_guild_setting(
            interaction.guild.id, "broadcast.image_channel_id"
        )
        if not channel_id:
            await interaction.response.send_message(
                "No image channel registered. Use `/register image` first.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message(
                "The registered image channel no longer exists. Please re-register with `/register image`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        ping = f"<:transmission:1501693676538298498> | <@&1496496458160148480>"

        if attachment:
            file = await attachment.to_file()
            await channel.send(content=ping, file=file)
        else:
            await channel.send(content=f"{ping}\n{url}")

        await interaction.followup.send(
            f"Image broadcast sent to {channel.mention}.", ephemeral=True
        )

    # ── /video ────────────────────────────────────────────────────────────────

    @app_commands.command(name="video", description="Broadcast a video to the registered video channel")
    @app_commands.describe(
        attachment="A video file to broadcast",
        url="A YouTube link or direct video URL to broadcast",
    )
    @management_or_dev()
    async def broadcast_video(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment = None,
        url: str = None,
    ):
        if not attachment and not url:
            await interaction.response.send_message(
                "Please provide either a video attachment or a URL.", ephemeral=True
            )
            return

        channel_id = await interaction.client.db.get_guild_setting(
            interaction.guild.id, "broadcast.video_channel_id"
        )
        if not channel_id:
            await interaction.response.send_message(
                "No video channel registered. Use `/register video` first.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message(
                "The registered video channel no longer exists. Please re-register with `/register video`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        ping = f"<:transmission:1501693676538298498> | <@&1496496458160148480>"

        if attachment:
            file = await attachment.to_file()
            await channel.send(content=ping, file=file)
        else:
            await channel.send(content=f"{ping}\n{url}")

        await interaction.followup.send(
            f"Video broadcast sent to {channel.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BroadcastCog(bot))
