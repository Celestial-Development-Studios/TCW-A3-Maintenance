"""
Automatically grants and revokes access to unit leadership channels based on
member roles. Members holding the configured "Unit CO" role plus a configured
unit role get a member-level permission overwrite on the leadership channel
linked to that unit role.

Access is granted per *member*, not per role. This avoids fragile Allow/Deny
role-stacking on the channel (which breaks once a member holds more than one
unit role, or once there are many units) and keeps each leadership channel's
permission list explicit and auditable.

Commands
    /cochat refresh                  Manually sync CO chat access against current roles.
    /cochat status                   Show current configuration and managed-overwrite count.
    /cochat set-co-role <role>       Set the Unit CO role (primary qualifier for access).
    /cochat set-leader-role [role]   Set the Unit Leader role (second qualifier). Pass nothing to clear.
    /cochat add-unit-role <role>     Add a unit role to the tracking list.
    /cochat remove-unit-role <role>  Remove a unit role and drop its channel link.
    /cochat link <channel> <role>    Link a leadership channel to a unit role.
    /cochat unlink <channel>         Remove a channel link (access persists until next refresh).
    /cochat set-interval <minutes>   Set auto-refresh interval in minutes (0 to disable).

All /cochat commands require the management role or developer status.

Storage uses the generic per-guild key/value API on the bot's Database
(`bot.db`). All keys are namespaced under `co_chat.` to avoid collisions.

Storage keys:
    co_chat.co_role_id          int     The Unit CO role ID
    co_chat.leader_role_id      int     The Unit Leader role ID (optional, second qualifier)
    co_chat.unit_roles          list    Unit role IDs in scope
    co_chat.links               dict    {str(role_id): channel_id} -- JSON requires str keys
    co_chat.refresh_interval    int     Minutes between auto-refresh (0 = disabled)
    co_chat.managed_overwrites  list    [[channel_id, user_id], ...] -- entries the bot has added
    co_chat.last_refresh_ts     float   Unix timestamp of last successful refresh

The managed_overwrites set is the key safety mechanism: the bot will only ever
remove channel overwrites it knows it added. Manual overwrites added by admins
are never touched. Only overwrites that were *successfully* applied are recorded
as managed, so a transient failure is retried on the next refresh rather than
being silently treated as done.

Requires the privileged members intent (used here via `Intents.all()`) so that
`co_role.members` and `member.roles` are populated.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DEVELOPER_IDS


# ---------------------------------------------------------------------------
# Permission Helpers
# ---------------------------------------------------------------------------

async def _is_manager(interaction: discord.Interaction) -> bool:
    """
    True for bot developers or holders of the configured management role.

    Mirrors the gating used by `/config` in cogs/panel.py: developers always
    pass; otherwise the management role is required, falling back to the Discord
    administrator permission when no management role has been configured yet.
    """
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


def manager_only():
    """app_commands.check decorator restricting a command to developers/management."""
    return app_commands.check(_is_manager)


# ---------------------------------------------------------------------------
# Storage Key Registry (short name -> (full key, default))
# ---------------------------------------------------------------------------

_KEYS = {
    'co_role_id':         ('co_chat.co_role_id',         None),
    'leader_role_id':     ('co_chat.leader_role_id',     None),
    'unit_roles':         ('co_chat.unit_roles',         []),
    'links':              ('co_chat.links',              {}),
    'refresh_interval':   ('co_chat.refresh_interval',   0),
    'managed_overwrites': ('co_chat.managed_overwrites', []),
    'last_refresh_ts':    ('co_chat.last_refresh_ts',    0),
}


# ---------------------------------------------------------------------------
# Result Type for Refresh Operations
# ---------------------------------------------------------------------------

class RefreshResult:
    def __init__(self) -> None:
        self.added: int = 0
        self.removed: int = 0
        self.errors: List[str] = []
        self.skipped_reason: Optional[str] = None

    def is_skipped(self) -> bool:
        return self.skipped_reason is not None

    def summary(self) -> str:
        if self.skipped_reason:
            return f"Skipped: {self.skipped_reason}"
        parts = [f"Added: {self.added}", f"Removed: {self.removed}"]
        if self.errors:
            parts.append(f"Errors: {len(self.errors)}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class CoChatCog(commands.Cog, name="CoChat"):
    """Manages access to unit leadership channels based on CO + unit roles."""

    # Throttle: sleep briefly every N permission edits to respect rate limits.
    BATCH_SIZE = 5
    BATCH_DELAY = 0.5  # seconds

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.auto_refresh_loop.start()

    async def cog_unload(self) -> None:
        self.auto_refresh_loop.cancel()

    # -----------------------------------------------------------------------
    # Storage Helpers
    # -----------------------------------------------------------------------

    async def _load_config(self, guild_id: int) -> Dict[str, Any]:
        """Load all co_chat settings for a guild, applying defaults for missing keys."""
        raw = await self.bot.db.get_guild_settings(guild_id)
        out: Dict[str, Any] = {}
        for short_name, (full_key, default) in _KEYS.items():
            out[short_name] = raw.get(full_key, default)
        return out

    async def _save(self, guild_id: int, short_name: str, value: Any) -> None:
        full_key = _KEYS[short_name][0]
        await self.bot.db.set_guild_setting(guild_id, full_key, value)

    # -----------------------------------------------------------------------
    # Core Reconciliation Logic
    # -----------------------------------------------------------------------

    async def _do_refresh(self, guild: discord.Guild) -> RefreshResult:
        """Reconcile channel access against the configured rules for one guild."""
        result = RefreshResult()
        config = await self._load_config(guild.id)

        co_role_id = config['co_role_id']
        if not co_role_id:
            result.skipped_reason = "no CO role configured"
            return result

        co_role = guild.get_role(co_role_id)
        if not co_role:
            result.skipped_reason = f"CO role {co_role_id} not found in guild"
            return result

        unit_roles: List[int] = config['unit_roles']
        if not unit_roles:
            result.skipped_reason = "no unit roles configured"
            return result

        # Build map: unit_role_id -> channel (skipping deleted/unlinked)
        links_raw: Dict[str, int] = config['links']
        role_to_channel: Dict[int, discord.abc.GuildChannel] = {}
        for role_id_str, channel_id in links_raw.items():
            try:
                role_id = int(role_id_str)
            except (TypeError, ValueError):
                continue
            if role_id not in unit_roles:
                continue  # link exists but role isn't in scope
            channel = guild.get_channel(channel_id)
            if channel is None:
                result.errors.append(f"Linked channel {channel_id} not found (role <@&{role_id}>)")
                continue
            role_to_channel[role_id] = channel

        # Compute the desired set: (channel_id, member_id) tuples.
        # A member qualifies if they hold a linked unit role AND hold either the
        # Unit CO role or the (optional) Unit Leader role. Unit Leaders are a
        # one-to-one identity that the CO role (many-to-one) does not cover, so
        # without this a Unit Leader who isn't also a CO can't see their own
        # leadership channel.
        qualifying_members: Dict[int, discord.Member] = {m.id: m for m in co_role.members}
        leader_role_id = config['leader_role_id']
        if leader_role_id:
            leader_role = guild.get_role(leader_role_id)
            if leader_role:
                for m in leader_role.members:
                    qualifying_members[m.id] = m
            else:
                result.errors.append(f"Leader role {leader_role_id} not found in guild")

        desired: Set[Tuple[int, int]] = set()
        for member in qualifying_members.values():
            for role in member.roles:
                if role.id in role_to_channel:
                    desired.add((role_to_channel[role.id].id, member.id))

        # Load the managed set (entries the bot previously applied)
        managed_raw: List[List[int]] = config['managed_overwrites']
        managed: Set[Tuple[int, int]] = set()
        for entry in managed_raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                try:
                    managed.add((int(entry[0]), int(entry[1])))
                except (TypeError, ValueError):
                    continue

        additions = desired - managed
        removals = managed - desired

        ops = 0

        # Apply additions: grant access. Only record entries that actually apply.
        for channel_id, user_id in additions:
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue  # deleted between map build and apply
            member = guild.get_member(user_id)
            if member is None:
                continue  # member left
            try:
                await channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    reason="CO Chat: granted via role sync",
                )
                managed.add((channel_id, user_id))
                result.added += 1
            except discord.Forbidden:
                result.errors.append(f"Forbidden adding {member} to #{channel.name}")
            except discord.HTTPException as exc:
                result.errors.append(f"HTTP error adding {member} to #{channel.name}: {exc}")

            ops += 1
            if ops % self.BATCH_SIZE == 0:
                await asyncio.sleep(self.BATCH_DELAY)

        # Apply removals: revoke access. Use discord.Object so members who left
        # still get cleaned up.
        for channel_id, user_id in removals:
            channel = guild.get_channel(channel_id)
            if channel is None:
                # Channel is gone, so its overwrite is gone too -- stop tracking it.
                managed.discard((channel_id, user_id))
                continue
            member = guild.get_member(user_id)
            target: discord.abc.Snowflake = member if member is not None else discord.Object(id=user_id)
            try:
                await channel.set_permissions(
                    target,
                    overwrite=None,
                    reason="CO Chat: revoked via role sync",
                )
                managed.discard((channel_id, user_id))
                result.removed += 1
            except discord.NotFound:
                managed.discard((channel_id, user_id))  # overwrite already gone, fine
            except discord.Forbidden:
                name = getattr(member, 'display_name', str(user_id))
                result.errors.append(f"Forbidden removing {name} from #{channel.name}")
            except discord.HTTPException as exc:
                name = getattr(member, 'display_name', str(user_id))
                result.errors.append(f"HTTP error removing {name} from #{channel.name}: {exc}")

            ops += 1
            if ops % self.BATCH_SIZE == 0:
                await asyncio.sleep(self.BATCH_DELAY)

        # Persist the actually-applied managed set
        new_managed = [[c_id, u_id] for (c_id, u_id) in managed]
        await self._save(guild.id, 'managed_overwrites', new_managed)
        await self._save(guild.id, 'last_refresh_ts', time.time())

        return result

    # -----------------------------------------------------------------------
    # Background Auto-refresh
    # -----------------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def auto_refresh_loop(self) -> None:
        """Check each guild every minute; run refresh if its interval has elapsed."""
        for guild in self.bot.guilds:
            try:
                config = await self._load_config(guild.id)
            except Exception as exc:
                print(f"[co_chat] failed to load config for {guild.id}: {exc}")
                continue

            interval = config['refresh_interval']
            if not isinstance(interval, (int, float)) or interval <= 0:
                continue

            now = time.time()
            last = config['last_refresh_ts'] or 0
            if (now - last) < (interval * 60):
                continue

            try:
                result = await self._do_refresh(guild)
                print(f"[co_chat] auto-refresh guild={guild.id}: {result.summary()}")
            except Exception as exc:
                print(f"[co_chat] auto-refresh failed for {guild.id}: {exc}")

    @auto_refresh_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # -----------------------------------------------------------------------
    # Slash Commands
    # -----------------------------------------------------------------------

    group = app_commands.Group(
        name='cochat',
        description='CO chat access management',
        guild_only=True,
    )

    @group.command(name='refresh', description='Manually sync CO chat access against current roles.')
    @manager_only()
    async def cmd_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("This command must be used in a guild.", ephemeral=True)
            return
        result = await self._do_refresh(interaction.guild)
        embed = discord.Embed(
            title="CO Chat Refresh",
            color=discord.Color.orange() if result.is_skipped() else discord.Color.green(),
        )
        embed.description = result.summary()
        if result.errors:
            joined = "\n".join(f"• {e}" for e in result.errors[:10])
            if len(result.errors) > 10:
                joined += f"\n... and {len(result.errors) - 10} more"
            embed.add_field(name="Errors", value=joined, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name='status', description='Show current CO chat configuration.')
    @manager_only()
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        guild = interaction.guild
        config = await self._load_config(guild.id)
        embed = discord.Embed(title="CO Chat Configuration", color=discord.Color.blue())

        # CO role
        co_role = guild.get_role(config['co_role_id']) if config['co_role_id'] else None
        embed.add_field(
            name="Unit CO Role",
            value=co_role.mention if co_role else ("Not set" if not config['co_role_id'] else f"<deleted: `{config['co_role_id']}`>"),
            inline=False,
        )

        # Leader role (optional second qualifier)
        leader_role = guild.get_role(config['leader_role_id']) if config['leader_role_id'] else None
        embed.add_field(
            name="Unit Leader Role",
            value=leader_role.mention if leader_role else ("Not set" if not config['leader_role_id'] else f"<deleted: `{config['leader_role_id']}`>"),
            inline=False,
        )

        # Unit roles + links
        if config['unit_roles']:
            lines = []
            for rid in config['unit_roles']:
                role = guild.get_role(rid)
                role_label = role.mention if role else f"<deleted: `{rid}`>"
                channel_id = config['links'].get(str(rid))
                if channel_id:
                    channel = guild.get_channel(channel_id)
                    chan_label = channel.mention if channel else f"<deleted: `{channel_id}`>"
                else:
                    chan_label = "*not linked*"
                lines.append(f"{role_label} → {chan_label}")
            embed.add_field(
                name=f"Unit Roles ({len(config['unit_roles'])})",
                value="\n".join(lines)[:1024] or "—",
                inline=False,
            )
        else:
            embed.add_field(name="Unit Roles", value="None configured", inline=False)

        # Auto-refresh
        interval = config['refresh_interval']
        embed.add_field(
            name="Auto-Refresh",
            value=f"Every {interval} min" if interval and interval > 0 else "Disabled",
            inline=True,
        )

        # Counts
        embed.add_field(
            name="Managed Entries",
            value=str(len(config['managed_overwrites'])),
            inline=True,
        )

        # Last refresh
        last_ts = config['last_refresh_ts']
        if last_ts:
            embed.add_field(name="Last Refresh", value=f"<t:{int(last_ts)}:R>", inline=True)
        else:
            embed.add_field(name="Last Refresh", value="Never", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name='link', description='Link a leadership channel to a unit role.')
    @app_commands.describe(channel='The leadership channel.', role='The unit role to link to this channel.')
    @manager_only()
    async def cmd_link(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        if role.id not in config['unit_roles']:
            await interaction.response.send_message(
                f"⚠️ {role.mention} is not in the unit roles list. Add it first with `/cochat add-unit-role`.",
                ephemeral=True,
            )
            return
        links = dict(config['links'])
        links[str(role.id)] = channel.id
        await self._save(interaction.guild.id, 'links', links)
        await interaction.response.send_message(
            f"✅ Linked {role.mention} → {channel.mention}.",
            ephemeral=True,
        )

    @group.command(name='unlink', description='Remove a leadership channel link.')
    @app_commands.describe(channel='The leadership channel to unlink.')
    @manager_only()
    async def cmd_unlink(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        links = dict(config['links'])
        removed_role_ids = [rid for rid, cid in links.items() if cid == channel.id]
        if not removed_role_ids:
            await interaction.response.send_message(
                f"No links found for {channel.mention}.",
                ephemeral=True,
            )
            return
        for rid in removed_role_ids:
            links.pop(rid, None)
        await self._save(interaction.guild.id, 'links', links)
        await interaction.response.send_message(
            f"✅ Removed {len(removed_role_ids)} link(s) for {channel.mention}.\n"
            f"*Note: existing access remains until next refresh.*",
            ephemeral=True,
        )

    @group.command(name='set-co-role', description='Set the Unit CO role.')
    @app_commands.describe(role='The Unit CO role.')
    @manager_only()
    async def cmd_set_co_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await self._save(interaction.guild.id, 'co_role_id', role.id)
        await interaction.response.send_message(
            f"✅ Unit CO role set to {role.mention}.",
            ephemeral=True,
        )

    @group.command(name='set-leader-role', description='Set the Unit Leader role (second qualifier for channel access).')
    @app_commands.describe(role='The Unit Leader role. Pass nothing to clear it.')
    @manager_only()
    async def cmd_set_leader_role(self, interaction: discord.Interaction, role: Optional[discord.Role] = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if role is None:
            await self._save(interaction.guild.id, 'leader_role_id', None)
            await interaction.response.send_message("✅ Unit Leader role cleared.", ephemeral=True)
            return
        await self._save(interaction.guild.id, 'leader_role_id', role.id)
        await interaction.response.send_message(
            f"✅ Unit Leader role set to {role.mention}. Leaders holding a linked unit role "
            f"will get channel access on the next refresh.",
            ephemeral=True,
        )

    @group.command(name='add-unit-role', description='Add a role to the unit roles list.')
    @app_commands.describe(role='The unit role to track.')
    @manager_only()
    async def cmd_add_unit_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        unit_roles = list(config['unit_roles'])
        if role.id in unit_roles:
            await interaction.response.send_message(
                f"{role.mention} is already in the unit roles list.",
                ephemeral=True,
            )
            return
        unit_roles.append(role.id)
        await self._save(interaction.guild.id, 'unit_roles', unit_roles)
        await interaction.response.send_message(
            f"✅ Added {role.mention} to unit roles.",
            ephemeral=True,
        )

    @group.command(name='remove-unit-role', description='Remove a role from the unit roles list.')
    @app_commands.describe(role='The unit role to stop tracking.')
    @manager_only()
    async def cmd_remove_unit_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        unit_roles = list(config['unit_roles'])
        if role.id not in unit_roles:
            await interaction.response.send_message(
                f"{role.mention} is not in the unit roles list.",
                ephemeral=True,
            )
            return
        unit_roles.remove(role.id)
        await self._save(interaction.guild.id, 'unit_roles', unit_roles)

        # Also drop any links for this role
        links = dict(config['links'])
        had_link = links.pop(str(role.id), None) is not None
        if had_link:
            await self._save(interaction.guild.id, 'links', links)

        msg = f"✅ Removed {role.mention} from unit roles."
        if had_link:
            msg += " Linked channel was also unlinked."
        await interaction.response.send_message(msg, ephemeral=True)

    @group.command(name='set-interval', description='Set the auto-refresh interval in minutes (0 to disable).')
    @app_commands.describe(minutes='Minutes between auto-refreshes. 0 disables auto-refresh.')
    @manager_only()
    async def cmd_set_interval(self, interaction: discord.Interaction, minutes: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if minutes < 0:
            await interaction.response.send_message("Interval must be 0 or positive.", ephemeral=True)
            return
        await self._save(interaction.guild.id, 'refresh_interval', minutes)
        if minutes == 0:
            msg = "✅ Auto-refresh disabled."
        else:
            msg = f"✅ Auto-refresh set to every {minutes} minute(s)."
        await interaction.response.send_message(msg, ephemeral=True)

    # -----------------------------------------------------------------------
    # Error Handler for the Group
    # -----------------------------------------------------------------------

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ You do not have permission to use that command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        # Unhandled error: surface a short message and re-raise for the log
        try:
            msg = f"⚠️ An error occurred: `{type(error).__name__}`"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CoChatCog(bot))
