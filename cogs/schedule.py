"""
Each unit is linked to a schedule channel (same per-unit link model as co_chat
and roster). The board is a header message plus seven day messages (Monday →
Sunday), each carrying a markdown day header and — from M2 onward — event embeds.
All messages are edited in place; their IDs are stored so the board survives
restarts.

This milestone establishes:
  - configuration commands (link / unlink / access-roles / status)
  - the board skeleton: header + 7 empty day messages, posted once and then
    edited in place by /schedule refresh
  - week-window computation (Mon–Sun) used by the header

Events, the DM creation flow, RSVPs, recurrence, and the Monday auto-refresh
arrive in later milestones. The auto-refresh loop and event storage are
intentionally NOT implemented yet.

Permissions
    /scheduleconfig ...   staff only (developers or the management role)
    /schedule refresh     editors: developers, management, the Unit Leader of
                          the unit, or holders of a configured access role

Storage (all under the `schedule.` namespace in the guild_settings KV store)
    schedule.links          dict   {str(unit_role_id): channel_id}
    schedule.access_roles   list   rank-ladder role IDs allowed to edit a schedule
    schedule.messages       dict   {str(unit_role_id): {"header": id, "mon": id, ...}}

Shared identities (read-only, from co_chat):
    co_chat.unit_roles      list   the units in scope
    co_chat.leader_role_id  int    Unit Leader role — always allowed to edit

Requires the privileged members intent (via Intents.all()).
"""
import time
import datetime
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS


# Day Keys in Board Order (Monday first), paired with their markdown headers.
_DAYS: List[tuple] = [
    ("mon", "MONDAY"),
    ("tue", "TUESDAY"),
    ("wed", "WEDNESDAY"),
    ("thu", "THURSDAY"),
    ("fri", "FRIDAY"),
    ("sat", "SATURDAY"),
    ("sun", "SUNDAY"),
]
_DAY_KEYS = [k for k, _ in _DAYS]

# Storage Keys (short name -> (full key, default))
_KEYS = {
    'links':        ('schedule.links',        {}),
    'access_roles': ('schedule.access_roles', []),
    'messages':     ('schedule.messages',     {}),
}


# ---------------------------------------------------------------------------
# Week-Window Helpers
# ---------------------------------------------------------------------------

def _current_monday(now: Optional[datetime.datetime] = None) -> datetime.date:
    """Return the date of Monday for the week containing `now` (UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    d = now.date()
    return d - datetime.timedelta(days=d.weekday())  # weekday(): Mon=0


def _week_range_label(monday: datetime.date) -> str:
    """A viewer-local week label using Discord timestamps for the Mon and Sun."""
    sunday = monday + datetime.timedelta(days=6)
    # Midnight UTC of each day; rendered date-only and viewer-local via <t:…:D>.
    mon_ts = int(datetime.datetime.combine(
        monday, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())
    sun_ts = int(datetime.datetime.combine(
        sunday, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())
    return f"<t:{mon_ts}:D> – <t:{sun_ts}:D>"


# ---------------------------------------------------------------------------
# Permission Helpers
# ---------------------------------------------------------------------------

async def _is_staff(interaction: discord.Interaction) -> bool:
    """Developers or the configured management role (mirrors rosterconfig gating)."""
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


def staff_only():
    return app_commands.check(_is_staff)


# ---------------------------------------------------------------------------
# Result Type for Refresh Operations
# ---------------------------------------------------------------------------

class BoardResult:
    def __init__(self) -> None:
        self.built: int = 0      # boards posted fresh
        self.updated: int = 0    # boards edited in place
        self.cleaned: int = 0
        self.errors: List[str] = []
        self.skipped_reason: Optional[str] = None

    def is_skipped(self) -> bool:
        return self.skipped_reason is not None

    def summary(self) -> str:
        if self.skipped_reason:
            return f"Skipped: {self.skipped_reason}"
        parts = [f"Built: {self.built}", f"Updated: {self.updated}"]
        if self.cleaned:
            parts.append(f"Cleaned: {self.cleaned}")
        if self.errors:
            parts.append(f"Errors: {len(self.errors)}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Board Rendering (M1: skeleton only — empty day messages)
# ---------------------------------------------------------------------------

def _header_content(unit_role: discord.Role, monday: datetime.date) -> str:
    """The pinned-at-top header message content."""
    return (
        f"# 📅 {unit_role.name} — Weekly Schedule\n"
        f"**Week of** {_week_range_label(monday)}"
    )


def _day_content(day_label: str) -> str:
    """
    A day message's text. M1 renders just the markdown day header; event embeds
    are attached to these same messages from M2 onward.
    """
    return f"# {day_label}"


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class ScheduleCog(commands.Cog, name="Schedule"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Storage Helpers ────────────────────────────────────────────────────────

    async def _load_config(self, guild_id: int) -> Dict[str, Any]:
        raw = await self.bot.db.get_guild_settings(guild_id)
        out: Dict[str, Any] = {}
        for short_name, (full_key, default) in _KEYS.items():
            out[short_name] = raw.get(full_key, default)
        return out

    async def _save(self, guild_id: int, short_name: str, value: Any) -> None:
        await self.bot.db.set_guild_setting(guild_id, _KEYS[short_name][0], value)

    async def _source_data(self, guild_id: int):
        """Shared identities from co_chat: (unit_roles, leader_role_id)."""
        db = self.bot.db
        unit_roles = await db.get_guild_setting(guild_id, "co_chat.unit_roles", [])
        leader_role_id = await db.get_guild_setting(guild_id, "co_chat.leader_role_id", None)
        return unit_roles, leader_role_id

    # ── Editor Gating (per-unit) ────────────────────────────────────────────────

    async def _can_edit(self, interaction: discord.Interaction, unit_role_id: int) -> bool:
        """
        True for developers, the management role, the Unit Leader (always exempt),
        or holders of a configured access role.
        """
        if await _is_staff(interaction):
            return True
        member = interaction.user
        _, leader_role_id = await self._source_data(interaction.guild.id)
        if leader_role_id and any(r.id == leader_role_id for r in member.roles):
            return True
        config = await self._load_config(interaction.guild.id)
        access_ids = set(config['access_roles'])
        if access_ids and any(r.id in access_ids for r in member.roles):
            return True
        return False

    # ── Core Board Build (M1: skeleton) ─────────────────────────────────────────

    async def _build_board(self, guild: discord.Guild, unit_role_id: int) -> BoardResult:
        """
        Post or edit the header + 7 day messages for one unit's schedule channel.
        M1 renders empty day messages; M2+ will attach event embeds here.
        """
        result = BoardResult()
        config = await self._load_config(guild.id)
        links: Dict[str, int] = config['links']
        all_messages: Dict[str, Any] = dict(config['messages'])

        key = str(unit_role_id)
        channel_id = links.get(key)
        if not channel_id:
            result.skipped_reason = "unit not linked to a schedule channel"
            return result

        unit_role = guild.get_role(unit_role_id)
        channel = guild.get_channel(channel_id)
        if unit_role is None:
            result.skipped_reason = f"unit role {unit_role_id} no longer exists"
            return result
        if channel is None:
            result.skipped_reason = f"schedule channel {channel_id} not found"
            return result

        monday = _current_monday()
        msgs: Dict[str, int] = dict(all_messages.get(key, {}))
        changed = False

        # Ordered build: header first, then each day Mon→Sun.
        plan = [("header", _header_content(unit_role, monday))]
        for day_key, day_label in _DAYS:
            plan.append((day_key, _day_content(day_label)))

        try:
            for slot, content in plan:
                existing_id = msgs.get(slot)
                if existing_id:
                    try:
                        msg = await channel.fetch_message(existing_id)
                        await msg.edit(content=content, embeds=[])
                        result.updated += 1
                        continue
                    except discord.NotFound:
                        pass  # fall through to repost
                msg = await channel.send(content=content)
                msgs[slot] = msg.id
                changed = True
                result.built += 1
        except discord.Forbidden:
            result.errors.append(f"Missing permission to post in the schedule channel for {unit_role.name}")
        except discord.HTTPException as exc:
            result.errors.append(f"Discord error building {unit_role.name} board: {exc}")

        if changed:
            all_messages[key] = msgs
            await self._save(guild.id, 'messages', all_messages)
        return result

    async def _build_all(self, guild: discord.Guild) -> BoardResult:
        """Build/refresh every linked, in-scope unit board in this guild."""
        agg = BoardResult()
        config = await self._load_config(guild.id)
        links: Dict[str, int] = config['links']
        if not links:
            agg.skipped_reason = "no units linked to schedule channels"
            return agg

        unit_roles, _ = await self._source_data(guild.id)
        unit_role_set = set(unit_roles)
        all_messages: Dict[str, Any] = dict(config['messages'])
        messages_changed = False

        for role_id_str in list(links.keys()):
            try:
                role_id = int(role_id_str)
            except (TypeError, ValueError):
                continue
            # Stale / out of scope -> clean up its stored message IDs.
            if role_id not in unit_role_set or guild.get_role(role_id) is None:
                if all_messages.pop(role_id_str, None) is not None:
                    messages_changed = True
                    agg.cleaned += 1
                agg.errors.append(f"Unit `{role_id}` is not a current co_chat unit; skipped")
                continue
            r = await self._build_board(guild, role_id)
            agg.built += r.built
            agg.updated += r.updated
            agg.errors.extend(r.errors)

        if messages_changed:
            await self._save(guild.id, 'messages', all_messages)
        return agg

    # ── /schedule ──────────────────────────────────────────────────────────────

    schedule_group = app_commands.Group(
        name="schedule",
        description="Unit weekly schedule",
        guild_only=True,
    )

    @schedule_group.command(name="refresh", description="Rebuild the linked unit schedule boards now.")
    @app_commands.describe(unit="Optional: only refresh this unit's board.")
    @staff_only()
    async def cmd_refresh(self, interaction: discord.Interaction, unit: Optional[discord.Role] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("Guild only.", ephemeral=True)
            return
        if unit is not None:
            result = await self._build_board(interaction.guild, unit.id)
        else:
            result = await self._build_all(interaction.guild)
        embed = discord.Embed(
            title="Schedule Refresh",
            description=result.summary(),
            color=discord.Color.orange() if result.is_skipped() else discord.Color.green(),
        )
        if result.errors:
            joined = "\n".join(f"• {e}" for e in result.errors[:10])
            if len(result.errors) > 10:
                joined += f"\n... and {len(result.errors) - 10} more"
            embed.add_field(name="Notes", value=joined, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /scheduleconfig ──────────────────────────────────────────────────────────

    config_group = app_commands.Group(
        name="scheduleconfig",
        description="Configure unit schedules",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @config_group.command(name="link", description="Link a unit role to its schedule channel.")
    @app_commands.describe(unit_role="The unit role.", channel="The schedule channel.")
    @staff_only()
    async def cmd_link(self, interaction: discord.Interaction,
                       unit_role: discord.Role, channel: discord.TextChannel) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        unit_roles, _ = await self._source_data(interaction.guild.id)
        config = await self._load_config(interaction.guild.id)
        links = dict(config['links'])
        links[str(unit_role.id)] = channel.id
        await self._save(interaction.guild.id, 'links', links)

        note = ""
        if unit_role.id not in set(unit_roles):
            note = (f"\n⚠️ {unit_role.mention} isn't in the co_chat unit list yet, so its board "
                    f"won't build until it's added via `/cochat add-unit-role`.")
        await interaction.response.send_message(
            f"✅ {unit_role.mention} schedule will live in {channel.mention}. "
            f"Run `/schedule refresh` to build the board.{note}",
            ephemeral=True,
        )

    @config_group.command(name="unlink", description="Remove a unit's schedule link and delete its board messages.")
    @app_commands.describe(unit_role="The unit role to unlink.")
    @staff_only()
    async def cmd_unlink(self, interaction: discord.Interaction, unit_role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        links = dict(config['links'])
        all_messages = dict(config['messages'])
        key = str(unit_role.id)
        if key not in links:
            await interaction.response.send_message(
                f"{unit_role.mention} isn't linked to a schedule channel.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(links.pop(key))
        msgs = all_messages.pop(key, {})
        # Best-effort cleanup of the board messages.
        if channel is not None and isinstance(msgs, dict):
            for mid in msgs.values():
                try:
                    m = await channel.fetch_message(mid)
                    await m.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await self._save(interaction.guild.id, 'links', links)
        await self._save(interaction.guild.id, 'messages', all_messages)
        await interaction.response.send_message(
            f"✅ Unlinked {unit_role.mention} and removed its board messages.", ephemeral=True)

    @config_group.command(name="add-access-role", description="Allow holders of this rank/role to edit unit schedules.")
    @app_commands.describe(role="A rank-ladder role allowed to edit schedules. Unit Leaders are always allowed.")
    @staff_only()
    async def cmd_add_access_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        access_roles = list(config['access_roles'])
        if role.id in access_roles:
            await interaction.response.send_message(
                f"{role.mention} is already in the schedule editor list.", ephemeral=True)
            return
        access_roles.append(role.id)
        await self._save(interaction.guild.id, 'access_roles', access_roles)
        await interaction.response.send_message(
            f"✅ Added {role.mention} to the schedule editor list. "
            f"(Unit Leaders can always edit their own schedule.)",
            ephemeral=True,
        )

    @config_group.command(name="remove-access-role", description="Remove a role from the schedule editor list.")
    @app_commands.describe(role="The editor role to remove.")
    @staff_only()
    async def cmd_remove_access_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        access_roles = [r for r in config['access_roles'] if r != role.id]
        if len(access_roles) == len(config['access_roles']):
            await interaction.response.send_message(
                f"{role.mention} is not in the schedule editor list.", ephemeral=True)
            return
        await self._save(interaction.guild.id, 'access_roles', access_roles)
        msg = f"✅ Removed {role.mention} from the schedule editor list."
        if not access_roles:
            msg += " The list is now empty (only staff and Unit Leaders can edit)."
        await interaction.response.send_message(msg, ephemeral=True)

    @config_group.command(name="status", description="Show schedule configuration.")
    @staff_only()
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        guild = interaction.guild
        config = await self._load_config(guild.id)
        unit_roles, _ = await self._source_data(guild.id)
        unit_role_set = set(unit_roles)
        embed = discord.Embed(title="Schedule Configuration", color=discord.Color.blue())

        if config['links']:
            lines = []
            for rid_str, cid in config['links'].items():
                try:
                    rid = int(rid_str)
                except (TypeError, ValueError):
                    continue
                role = guild.get_role(rid)
                channel = guild.get_channel(cid)
                role_label = role.mention if role else f"<deleted: `{rid}`>"
                chan_label = channel.mention if channel else f"<deleted: `{cid}`>"
                built = "built" if str(rid) in config['messages'] else "not built"
                flag = "" if rid in unit_role_set else " ⚠️ not a co_chat unit"
                lines.append(f"{role_label} → {chan_label} ({built}){flag}")
            embed.add_field(name="Links", value="\n".join(lines)[:1024] or "—", inline=False)
        else:
            embed.add_field(name="Links", value="None configured", inline=False)

        if config['access_roles']:
            roles_label = ", ".join(
                (guild.get_role(r).mention if guild.get_role(r) else f"<deleted: `{r}`>")
                for r in config['access_roles']
            )
            embed.add_field(
                name="Editor Roles",
                value=f"{roles_label}\n*(Unit Leaders + staff always allowed)*"[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="Editor Roles", value="Staff + Unit Leaders only", inline=False)

        embed.set_footer(text="M1: board skeleton. Events, RSVPs, and weekly refresh arrive in later milestones.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Error Handler ──────────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                     error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ You do not have permission to use that command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
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
    await bot.add_cog(ScheduleCog(bot))
