"""
Maintains a bot-curated roster message in each unit's roster channel. A roster
shows the unit's total member count, how many leadership positions are still
available per rank (per TCW:A3 headcount rules), the members sorted into rank
categories, and a structure-capacity aside.

The roster is read-only and passive: it reports the numbers that support
leadership gating, but does not itself enforce anything. That will come in a later update.

Data sources (all read-only):
    co_chat.unit_roles     list   the units in scope
    co_chat.co_role_id     int    Unit CO role (shared identity)
    co_chat.leader_role_id int    Unit Leader role -> the "Commander" category
    ranks.rank_order       list   ordered [{"name", "role_id"}], low -> high

Storage (roster.* namespace in the guild_settings KV store):
    roster.links             dict   {str(unit_role_id): channel_id}
    roster.messages          dict   {str(unit_role_id): message_id}
    roster.refresh_interval  int    minutes between auto-refresh (0 = disabled)
    roster.last_refresh_ts   float  unix timestamp of last refresh

Headcount rules (entitlement ratios, base = total unit members N):
    Corporal   = floor(N / 5)
    Sergeant   = floor(N / 10)
    Lieutenant = floor(N / 40)
    Captain    = floor(N / 160)

Requires the privileged members intent (via Intents.all()) so role membership
is populated.
"""
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DEVELOPER_IDS


# Entitlement divisors and structure capacities, keyed by the canonical rank /
# structure names. Ranks outside this map still display as categories but get
# no entitlement line.
_ENTITLEMENT_DIVISORS = {
    "Corporal": 5,
    "Sergeant": 10,
    "Lieutenant": 40,
    "Captain": 160,
}
_STRUCTURES: List[Tuple[str, int]] = [
    ("Fireteam", 5),
    ("Squad", 10),
    ("Platoon", 40),
    ("Company", 160),
]

# Storage keys (short name -> (full key, default))
_KEYS = {
    'links':            ('roster.links',            {}),
    'messages':         ('roster.messages',         {}),
    'refresh_interval': ('roster.refresh_interval', 60),
    'last_refresh_ts':  ('roster.last_refresh_ts',  0),
}

_MENTIONS_PER_FIELD = 30  # keep each embed field comfortably under the 1024-char cap


# ---------------------------------------------------------------------------
# Permission Helpers (mirrors co_chat / panel gating)
# ---------------------------------------------------------------------------

async def _is_manager(interaction: discord.Interaction) -> bool:
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
    return app_commands.check(_is_manager)


# ---------------------------------------------------------------------------
# Result Type for Refresh Operations
# ---------------------------------------------------------------------------

class RosterResult:
    def __init__(self) -> None:
        self.updated: int = 0
        self.posted: int = 0
        self.cleaned: int = 0
        self.errors: List[str] = []
        self.skipped_reason: Optional[str] = None

    def is_skipped(self) -> bool:
        return self.skipped_reason is not None

    def summary(self) -> str:
        if self.skipped_reason:
            return f"Skipped: {self.skipped_reason}"
        parts = [f"Updated: {self.updated}", f"Posted: {self.posted}"]
        if self.cleaned:
            parts.append(f"Cleaned: {self.cleaned}")
        if self.errors:
            parts.append(f"Errors: {len(self.errors)}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------

def _categorize(
    guild: discord.Guild,
    unit_role: discord.Role,
    rank_order: List[Dict[str, Any]],
    co_role_id: Optional[int],
    leader_role_id: Optional[int],
) -> Tuple[List[Tuple[str, List[discord.Member]]], int]:
    """
    Sort the unit's members into ranked categories, each member once under their
    highest. Returns (ordered [(category_name, members)], total_member_count).

    Category order: Commander, then configured ranks high -> low, then Unit CO
    (edge case), then Trooper.
    """
    unit_members = list(unit_role.members)
    total = len(unit_members)
    assigned = set()
    categories: List[Tuple[str, List[discord.Member]]] = []

    # Commander = Unit Leader role holders within the unit.
    commanders: List[discord.Member] = []
    if leader_role_id:
        for m in unit_members:
            if any(r.id == leader_role_id for r in m.roles):
                commanders.append(m)
                assigned.add(m.id)
    categories.append(("Commander", commanders))

    # Configured ranks, highest first (rank_order is low -> high).
    for rank in reversed(rank_order):
        role_id = rank.get("role_id")
        name = rank.get("name", "Rank")
        members_here: List[discord.Member] = []
        if role_id:
            for m in unit_members:
                if m.id in assigned:
                    continue
                if any(r.id == role_id for r in m.roles):
                    members_here.append(m)
                    assigned.add(m.id)
        categories.append((name, members_here))

    # Unit CO without a rank role (shouldn't happen under default config).
    co_only: List[discord.Member] = []
    if co_role_id:
        for m in unit_members:
            if m.id in assigned:
                continue
            if any(r.id == co_role_id for r in m.roles):
                co_only.append(m)
                assigned.add(m.id)
    if co_only:
        categories.append(("Unit CO", co_only))

    # Everyone else is a Trooper.
    troopers = [m for m in unit_members if m.id not in assigned]
    categories.append(("Trooper", troopers))

    return categories, total


def _entitlements(total: int, rank_order: List[Dict[str, Any]],
                  current_counts: Dict[str, int]) -> List[Tuple[str, int, int, int]]:
    """
    For each configured rank that has an entitlement divisor, return
    (name, available, current, allowed), highest rank first.
    """
    out: List[Tuple[str, int, int, int]] = []
    for rank in reversed(rank_order):
        name = rank.get("name", "")
        divisor = _ENTITLEMENT_DIVISORS.get(name)
        if not divisor:
            continue
        allowed = total // divisor
        current = current_counts.get(name, 0)
        available = max(0, allowed - current)
        out.append((name, available, current, allowed))
    return out


# ---------------------------------------------------------------------------
# Embed Building
# ---------------------------------------------------------------------------

def _add_member_fields(embed: discord.Embed, label: str, members: List[discord.Member]) -> None:
    """Add one or more fields listing member mentions, chunked under the field cap."""
    if not members:
        embed.add_field(name=f"{label} (0)", value="—", inline=False)
        return
    mentions = [m.mention for m in members]
    chunks = [mentions[i:i + _MENTIONS_PER_FIELD] for i in range(0, len(mentions), _MENTIONS_PER_FIELD)]
    for idx, chunk in enumerate(chunks):
        name = f"{label} ({len(members)})" if idx == 0 else f"{label} (cont.)"
        embed.add_field(name=name, value=" ".join(chunk)[:1024], inline=False)


def _build_roster_embed(
    guild: discord.Guild,
    unit_role: discord.Role,
    rank_order: List[Dict[str, Any]],
    co_role_id: Optional[int],
    leader_role_id: Optional[int],
) -> discord.Embed:
    categories, total = _categorize(guild, unit_role, rank_order, co_role_id, leader_role_id)
    current_counts = {name: len(members) for name, members in categories}
    entitlements = _entitlements(total, rank_order, current_counts)

    color = unit_role.color if unit_role.color.value else discord.Color.blurple()
    embed = discord.Embed(title=f"{unit_role.name} — Roster", color=color)

    # Headline
    headline = [f"**Total members:** {total}"]
    if entitlements:
        headline.append("\n**Leadership positions available:**")
        for name, available, current, allowed in entitlements:
            headline.append(f"• {name}: **{available}** available ({current}/{allowed})")
    embed.description = "\n".join(headline)

    # Member categories
    for name, members in categories:
        # Skip the synthetic "Unit CO" line if empty; always show the rest.
        if name == "Unit CO" and not members:
            continue
        _add_member_fields(embed, name, members)

    # Structure aside
    structure_bits = [f"{label} ({total // cap})" for label, cap in _STRUCTURES]
    embed.add_field(name="Structure capacity", value=" · ".join(structure_bits), inline=False)

    embed.set_footer(text="Auto-curated roster")
    embed.timestamp = discord.utils.utcnow()
    return embed


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class RosterCog(commands.Cog, name="Roster"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.auto_refresh_loop.start()

    async def cog_unload(self) -> None:
        self.auto_refresh_loop.cancel()

    # ── Storage Helpers ────────────────────────────────────────────────────────

    async def _load_config(self, guild_id: int) -> Dict[str, Any]:
        raw = await self.bot.db.get_guild_settings(guild_id)
        out: Dict[str, Any] = {}
        for short_name, (full_key, default) in _KEYS.items():
            out[short_name] = raw.get(full_key, default)
        return out

    async def _save(self, guild_id: int, short_name: str, value: Any) -> None:
        await self.bot.db.set_guild_setting(guild_id, _KEYS[short_name][0], value)

    async def _source_data(self, guild_id: int) -> Tuple[List[int], Optional[int], Optional[int], List[Dict[str, Any]]]:
        """Pull the shared identities and rank ladder from co_chat / ranks config."""
        db = self.bot.db
        unit_roles = await db.get_guild_setting(guild_id, "co_chat.unit_roles", [])
        co_role_id = await db.get_guild_setting(guild_id, "co_chat.co_role_id", None)
        leader_role_id = await db.get_guild_setting(guild_id, "co_chat.leader_role_id", None)
        rank_order = await db.get_guild_setting(guild_id, "ranks.rank_order", [])
        return unit_roles, co_role_id, leader_role_id, rank_order

    # ── Core Refresh ───────────────────────────────────────────────────────────

    async def _do_refresh(self, guild: discord.Guild) -> RosterResult:
        result = RosterResult()
        config = await self._load_config(guild.id)
        links: Dict[str, int] = config['links']
        messages: Dict[str, int] = dict(config['messages'])

        if not links:
            result.skipped_reason = "no units linked to roster channels"
            return result

        unit_roles, co_role_id, leader_role_id, rank_order = await self._source_data(guild.id)
        unit_role_set = set(unit_roles)

        messages_changed = False

        # Build / update a roster for each linked unit that is still in scope.
        for role_id_str, channel_id in links.items():
            try:
                role_id = int(role_id_str)
            except (TypeError, ValueError):
                continue

            unit_role = guild.get_role(role_id)
            channel = guild.get_channel(channel_id)

            # Out of scope or broken link -> clean up any stale message.
            if unit_role is None or channel is None or role_id not in unit_role_set:
                old_msg_id = messages.pop(role_id_str, None)
                if old_msg_id is not None:
                    messages_changed = True
                    result.cleaned += 1
                    if channel is not None:
                        try:
                            old = await channel.fetch_message(old_msg_id)
                            await old.delete()
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                if unit_role is None:
                    result.errors.append(f"Linked unit role `{role_id}` no longer exists")
                elif channel is None:
                    result.errors.append(f"Roster channel `{channel_id}` for {unit_role.name} not found")
                continue

            embed = _build_roster_embed(guild, unit_role, rank_order, co_role_id, leader_role_id)

            existing_id = messages.get(role_id_str)
            try:
                if existing_id:
                    try:
                        msg = await channel.fetch_message(existing_id)
                        await msg.edit(embed=embed)
                        result.updated += 1
                        continue
                    except discord.NotFound:
                        pass  # fall through to repost
                msg = await channel.send(embed=embed)
                messages[role_id_str] = msg.id
                messages_changed = True
                result.posted += 1
            except discord.Forbidden:
                result.errors.append(f"Missing permission to post in roster channel for {unit_role.name}")
            except discord.HTTPException as exc:
                result.errors.append(f"Discord error updating {unit_role.name} roster: {exc}")

        if messages_changed:
            await self._save(guild.id, 'messages', messages)
        await self._save(guild.id, 'last_refresh_ts', time.time())
        return result

    # ── Auto-refresh Loop ──────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def auto_refresh_loop(self) -> None:
        for guild in self.bot.guilds:
            try:
                config = await self._load_config(guild.id)
            except Exception as exc:
                print(f"[roster] failed to load config for {guild.id}: {exc}")
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
                print(f"[roster] auto-refresh guild={guild.id}: {result.summary()}")
            except Exception as exc:
                print(f"[roster] auto-refresh failed for {guild.id}: {exc}")

    @auto_refresh_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── /roster ──────────────────────────────────────────────────────────────

    roster_group = app_commands.Group(
        name="roster",
        description="Unit roster display",
        guild_only=True,
    )

    async def _unit_autocomplete(self, interaction: discord.Interaction, current: str):
        unit_roles, _, _, _ = await self._source_data(interaction.guild.id)
        out = []
        for rid in unit_roles:
            role = interaction.guild.get_role(rid)
            if role and current.lower() in role.name.lower():
                out.append(app_commands.Choice(name=role.name, value=str(rid)))
        return out[:25]

    @roster_group.command(name="refresh", description="Rebuild all linked unit rosters now.")
    @manager_only()
    async def cmd_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("Guild only.", ephemeral=True)
            return
        result = await self._do_refresh(interaction.guild)
        embed = discord.Embed(
            title="Roster Refresh",
            description=result.summary(),
            color=discord.Color.orange() if result.is_skipped() else discord.Color.green(),
        )
        if result.errors:
            joined = "\n".join(f"• {e}" for e in result.errors[:10])
            if len(result.errors) > 10:
                joined += f"\n... and {len(result.errors) - 10} more"
            embed.add_field(name="Errors", value=joined, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @roster_group.command(name="show", description="Preview a unit's roster (visible only to you).")
    @app_commands.describe(unit="The unit to preview.")
    @app_commands.autocomplete(unit=_unit_autocomplete)
    @manager_only()
    async def cmd_show(self, interaction: discord.Interaction, unit: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        try:
            role_id = int(unit)
        except (TypeError, ValueError):
            await interaction.response.send_message("Invalid unit selection.", ephemeral=True)
            return
        unit_role = interaction.guild.get_role(role_id)
        if unit_role is None:
            await interaction.response.send_message("That unit role no longer exists.", ephemeral=True)
            return
        _, co_role_id, leader_role_id, rank_order = await self._source_data(interaction.guild.id)
        embed = _build_roster_embed(interaction.guild, unit_role, rank_order, co_role_id, leader_role_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /rosterconfig ──────────────────────────────────────────────────────────

    config_group = app_commands.Group(
        name="rosterconfig",
        description="Configure unit rosters",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @config_group.command(name="set-channel", description="Link a unit to its roster channel.")
    @app_commands.describe(unit_role="The unit role.", channel="The channel its roster lives in.")
    @manager_only()
    async def cmd_set_channel(self, interaction: discord.Interaction,
                              unit_role: discord.Role, channel: discord.TextChannel) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        unit_roles, _, _, _ = await self._source_data(interaction.guild.id)
        config = await self._load_config(interaction.guild.id)
        links = dict(config['links'])
        links[str(unit_role.id)] = channel.id
        await self._save(interaction.guild.id, 'links', links)

        note = ""
        if unit_role.id not in set(unit_roles):
            note = (f"\n⚠️ {unit_role.mention} isn't in the co_chat unit list yet, so its roster "
                    f"won't build until it's added via `/cochat add-unit-role`.")
        await interaction.response.send_message(
            f"✅ {unit_role.mention} roster will be maintained in {channel.mention}.{note}",
            ephemeral=True,
        )

    @config_group.command(name="unlink", description="Remove a unit's roster link and clean up its message.")
    @app_commands.describe(unit_role="The unit role to unlink.")
    @manager_only()
    async def cmd_unlink(self, interaction: discord.Interaction, unit_role: discord.Role) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        links = dict(config['links'])
        messages = dict(config['messages'])
        key = str(unit_role.id)
        if key not in links:
            await interaction.response.send_message(
                f"{unit_role.mention} isn't linked to a roster channel.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(links.pop(key))
        old_msg_id = messages.pop(key, None)
        if channel is not None and old_msg_id is not None:
            try:
                msg = await channel.fetch_message(old_msg_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await self._save(interaction.guild.id, 'links', links)
        await self._save(interaction.guild.id, 'messages', messages)
        await interaction.response.send_message(
            f"✅ Unlinked {unit_role.mention} and removed its roster message.", ephemeral=True)

    @config_group.command(name="set-interval", description="Set auto-refresh interval in minutes (0 to disable).")
    @app_commands.describe(minutes="Minutes between refreshes. 0 disables auto-refresh.")
    @manager_only()
    async def cmd_set_interval(self, interaction: discord.Interaction, minutes: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if minutes < 0:
            await interaction.response.send_message("Interval must be 0 or positive.", ephemeral=True)
            return
        await self._save(interaction.guild.id, 'refresh_interval', minutes)
        msg = "✅ Auto-refresh disabled." if minutes == 0 else f"✅ Auto-refresh set to every {minutes} minute(s)."
        await interaction.response.send_message(msg, ephemeral=True)

    @config_group.command(name="status", description="Show roster configuration.")
    @manager_only()
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        guild = interaction.guild
        config = await self._load_config(guild.id)
        unit_roles, _, _, _ = await self._source_data(guild.id)
        unit_role_set = set(unit_roles)
        embed = discord.Embed(title="Roster Configuration", color=discord.Color.blue())

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
                flag = "" if rid in unit_role_set else " ⚠️ not a co_chat unit"
                lines.append(f"{role_label} → {chan_label}{flag}")
            embed.add_field(name="Links", value="\n".join(lines)[:1024] or "—", inline=False)
        else:
            embed.add_field(name="Links", value="None configured", inline=False)

        interval = config['refresh_interval']
        embed.add_field(
            name="Auto-Refresh",
            value=f"Every {interval} min" if interval and interval > 0 else "Disabled",
            inline=True,
        )
        last_ts = config['last_refresh_ts']
        embed.add_field(
            name="Last Refresh",
            value=f"<t:{int(last_ts)}:R>" if last_ts else "Never",
            inline=True,
        )
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
    await bot.add_cog(RosterCog(bot))
