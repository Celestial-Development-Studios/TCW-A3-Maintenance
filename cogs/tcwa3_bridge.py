"""TCWA3 Bridge API slash commands.

These commands connect Discord identity to the TCWA3 backend without giving the
Discord bot authority over roster rank/unit, XP, credits, quests, achievements,
or marketplace ownership.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS
from tcwa3_bridge import Tcwa3BridgeConfigError, Tcwa3BridgeError


def _role_names(member: discord.Member) -> List[str]:
    return [role.name for role in member.roles if not role.is_default()]


def _member_payload(member: discord.Member) -> Dict[str, Any]:
    return {
        "discord_id": str(member.id),
        "guild_id": str(member.guild.id),
        "discord_username": str(member),
        "nickname": member.nick or member.display_name,
        "roles": _role_names(member),
    }


async def _is_management_or_dev(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if interaction.user.id in DEVELOPER_IDS:
        return True
    config = await interaction.client.db.get_guild_config(interaction.guild.id)
    if not config or not config.get("management_role_id"):
        return interaction.user.guild_permissions.administrator
    role = interaction.guild.get_role(config["management_role_id"])
    if role:
        return role in interaction.user.roles
    return interaction.user.guild_permissions.administrator


def management_or_dev():
    return app_commands.check(_is_management_or_dev)


def _bridge_error_text(exc: Exception) -> str:
    if isinstance(exc, Tcwa3BridgeConfigError):
        return str(exc)
    if isinstance(exc, Tcwa3BridgeError):
        if exc.error == "invalid_bot_auth":
            return "TCWA3 rejected the bot id or timestamp. Check TCWA3_BOT_ID and server time."
        if exc.error == "invalid_bot_signature":
            return "TCWA3 rejected the request signature. Check TCWA3_BOT_SECRET."
        return f"TCWA3 returned HTTP {exc.status}: {exc}"
    return f"TCWA3 bridge request failed: {type(exc).__name__}"


def _status_embed(title: str, description: str, color: int = 0x5865F2) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="TCWA3 Discord bridge")
    return embed


class Tcwa3BridgeCog(commands.Cog, name="TCWA3 Bridge"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(
        name="tcwa3",
        description="TCWA3 website and Discord link tools",
        guild_only=True,
    )

    async def _sync_members(self, members: Iterable[discord.Member]) -> Dict[str, Any]:
        payload = [_member_payload(member) for member in members if not member.bot]
        if not payload:
            return {"status": "skipped", "updated": 0, "unlinked": 0}
        return await self.bot.tcwa3.member_sync(payload)

    @group.command(name="link", description="Create a private TCWA3 Discord-to-Steam link code.")
    async def link(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            payload = _member_payload(interaction.user)
            data = await self.bot.tcwa3.create_link_code(payload)
            await self.bot.db.save_tcwa3_link_request(
                interaction.guild.id,
                interaction.user.id,
                link_id=str(data.get("link_id", "")),
                discord_username=payload["discord_username"],
                nickname=payload.get("nickname"),
                status=str(data.get("status", "pending")),
                expires_at=data.get("expires_at"),
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=_status_embed("TCWA3 Link Unavailable", _bridge_error_text(exc), 0xED4245),
                ephemeral=True,
            )
            return

        code = data.get("code")
        expires_at = data.get("expires_at")
        expiry = f"\nExpires: <t:{int(expires_at)}:R>" if isinstance(expires_at, int) else ""
        if code:
            description = (
                f"Your private link code is:\n\n`{code}`\n\n"
                "Sign in on the TCWA3 website profile page, open Discord Link, "
                "and paste this code there."
                f"{expiry}"
            )
            color = 0x57F287
            title = "TCWA3 Link Code"
        else:
            description = (
                "TCWA3 already has an active pending code for you, but the backend "
                "does not reveal existing codes again for safety. Use `/tcwa3 link-status` "
                "or wait for the pending code to expire, then request a new one."
                f"{expiry}"
            )
            color = 0xFEE75C
            title = "Existing TCWA3 Link Pending"
        await interaction.followup.send(embed=_status_embed(title, description, color), ephemeral=True)

    @group.command(name="link-status", description="Check whether your latest TCWA3 link code was claimed.")
    async def link_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await self.bot.db.get_tcwa3_link_request(interaction.guild.id, interaction.user.id)
        if not request:
            await interaction.followup.send(
                embed=_status_embed(
                    "No TCWA3 Link Request",
                    "Use `/tcwa3 link` to create a private Discord-to-Steam link code.",
                    0xFEE75C,
                ),
                ephemeral=True,
            )
            return

        try:
            data = await self.bot.tcwa3.link_status(request["link_id"])
            status = str(data.get("status", "unknown"))
            await self.bot.db.update_tcwa3_link_status(interaction.guild.id, interaction.user.id, status)
        except Exception as exc:
            await interaction.followup.send(
                embed=_status_embed("TCWA3 Status Unavailable", _bridge_error_text(exc), 0xED4245),
                ephemeral=True,
            )
            return

        color = 0x57F287 if status == "claimed" else 0xFEE75C if status == "pending" else 0xED4245
        await interaction.followup.send(
            embed=_status_embed(
                "TCWA3 Link Status",
                f"Latest link status: **{status}**.\nLink id: `{request['link_id']}`",
                color,
            ),
            ephemeral=True,
        )

    @group.command(name="sync-me", description="Refresh your Discord metadata on TCWA3 if you are linked.")
    async def sync_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self._sync_members([interaction.user])
        except Exception as exc:
            await interaction.followup.send(
                embed=_status_embed("TCWA3 Sync Failed", _bridge_error_text(exc), 0xED4245),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_status_embed(
                "TCWA3 Sync Complete",
                f"Updated: **{result.get('updated', 0)}**\nUnlinked: **{result.get('unlinked', 0)}**",
                0x57F287,
            ),
            ephemeral=True,
        )

    @group.command(name="sync-member", description="Staff: sync one member's Discord metadata to TCWA3.")
    @app_commands.describe(member="Member to sync to TCWA3.")
    @management_or_dev()
    async def sync_member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self._sync_members([member])
        except Exception as exc:
            await interaction.followup.send(
                embed=_status_embed("TCWA3 Sync Failed", _bridge_error_text(exc), 0xED4245),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_status_embed(
                "TCWA3 Member Sync",
                f"Member: {member.mention}\nUpdated: **{result.get('updated', 0)}**\n"
                f"Unlinked: **{result.get('unlinked', 0)}**",
                0x57F287,
            ),
            ephemeral=True,
        )

    @group.command(name="sync-guild", description="Staff: sync Discord metadata for guild members.")
    @app_commands.describe(limit="Maximum non-bot members to sync this run, default 250.")
    @management_or_dev()
    async def sync_guild(self, interaction: discord.Interaction, limit: Optional[int] = 250) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        limit = max(1, min(int(limit or 250), 1000))
        members = [member for member in interaction.guild.members if not member.bot][:limit]
        total_updated = 0
        total_unlinked = 0
        try:
            for start in range(0, len(members), 100):
                result = await self._sync_members(members[start:start + 100])
                total_updated += int(result.get("updated", 0))
                total_unlinked += int(result.get("unlinked", 0))
        except Exception as exc:
            await interaction.followup.send(
                embed=_status_embed("TCWA3 Guild Sync Failed", _bridge_error_text(exc), 0xED4245),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_status_embed(
                "TCWA3 Guild Sync Complete",
                f"Scanned: **{len(members)}**\nUpdated linked profiles: **{total_updated}**\n"
                f"Unlinked members skipped by TCWA3: **{total_unlinked}**",
                0x57F287,
            ),
            ephemeral=True,
        )

    @group.command(name="bridge-status", description="Staff: show TCWA3 bridge configuration state.")
    @management_or_dev()
    async def bridge_status(self, interaction: discord.Interaction) -> None:
        configured = self.bot.tcwa3.configured
        description = (
            f"API: `{self.bot.tcwa3.base_url}`\n"
            f"Bot id: `{self.bot.tcwa3.bot_id or 'not set'}`\n"
            f"Secret: `{'configured' if self.bot.tcwa3.secret else 'missing'}`"
        )
        await interaction.response.send_message(
            embed=_status_embed(
                "TCWA3 Bridge Status",
                description,
                0x57F287 if configured else 0xFEE75C,
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "You do not have permission to use that TCWA3 command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Tcwa3BridgeCog(bot))
