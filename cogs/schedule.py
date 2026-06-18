"""
Per-unit weekly schedule boards.

Each unit is linked to a schedule channel (same per-unit link model as co_chat
and roster). The board is a header message plus seven day messages (Monday →
Sunday). Each day message carries a markdown day header and the event embeds for
that day; days with more than 10 events spill into an overflow message. All
messages are edited in place; their IDs are stored so the board survives restarts.

Editors add events through a guided DM flow (timezone is captured once per user,
then reused). Events are stored in the KV store and rendered under their weekday.

Implemented so far (M1 + M2):
  - configuration commands (link / unlink / access-roles / status)
  - the board: header + 7 day messages, event embeds per day, overflow handling
  - /schedule add — guided DM creation flow with per-user timezone capture
  - /schedule set-timezone
  - week-window computation (Mon–Sun) used by the header

RSVPs (M3), edit/delete (M3), weekly recurrence + the Monday auto-refresh (M4)
arrive in later milestones. The `recurring` flag is captured now but its weekly
regeneration/cleanup is dormant until M4; there is intentionally no auto-refresh
loop yet.

Permissions
    /scheduleconfig ...     staff only (developers or the management role)
    /schedule refresh       staff only
    /schedule add           editors: developers, management, the unit's Unit
                            Leader, or holders of a configured access role
    /schedule set-timezone  any member (sets their own input timezone)

Storage (all under the `schedule.` namespace in the guild_settings KV store)
    schedule.links          dict   {str(unit_role_id): channel_id}
    schedule.access_roles   list   rank-ladder role IDs allowed to edit a schedule
    schedule.messages       dict   {str(unit_role_id): {"header": id, "mon": id, "mon_of": id?, ...}}
    schedule.events         dict   {event_id: {unit_role_id, weekday, title, description,
                                   image_url, color, start_utc, recurring, created_by,
                                   week_start, rsvps:{confirmed,tentative,declined}}}
    schedule.user_tz        dict   {str(user_id): "IANA/Zone"}

Shared identities (read-only, from co_chat):
    co_chat.unit_roles      list   the units in scope
    co_chat.leader_role_id  int    Unit Leader role — always allowed to edit

Requires the privileged members intent (via Intents.all()).
"""
import re
import time
import uuid
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS

# Default embed colour when an event has none and the unit role is uncoloured.
_DEFAULT_COLOR = 0x5865F2
# Default event length used only for the "Add to Google Calendar" link.
_GCAL_DEFAULT_LEN = datetime.timedelta(hours=1)
# How long the creation DM flow waits at each step before timing out.
_FLOW_TIMEOUT = 300  # seconds
# Discord allows at most 10 embeds per message; events beyond this on one day
# spill into an overflow message (auto-extend, per design).
_EMBEDS_PER_MESSAGE = 10


# Day keys in board order (Monday first), paired with their markdown headers.
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

# Storage keys (short name -> (full key, default))
_KEYS = {
    'links':        ('schedule.links',        {}),
    'access_roles': ('schedule.access_roles', []),
    'messages':     ('schedule.messages',     {}),
    'events':       ('schedule.events',       {}),    # {event_id: {...}}
    'user_tz':      ('schedule.user_tz',      {}),    # {str(user_id): "IANA/Zone"}
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
# Time-of-day parsing & timezone conversion
# ---------------------------------------------------------------------------

_TIME_PATTERNS = [
    "%I:%M %p",  # 8:30 PM
    "%I %p",     # 8 PM
    "%I:%M%p",   # 8:30PM
    "%I%p",      # 8PM
    "%H:%M",     # 20:00
    "%H",        # 20
]


def _parse_time_of_day(text: str) -> Optional[Tuple[int, int]]:
    """Parse a clock time like '8pm', '8:30 PM', '20:00' -> (hour, minute) or None."""
    s = text.strip().lower().replace(".", "")
    s = re.sub(r"\s+", " ", s)
    # Normalise '8 p m' edge and bare 'am/pm' spacing already handled by patterns.
    for fmt in _TIME_PATTERNS:
        try:
            dt = datetime.datetime.strptime(s.upper() if "%p" in fmt.upper() else s, fmt)
            return dt.hour, dt.minute
        except ValueError:
            continue
    return None


def _validate_timezone(name: str) -> Optional[str]:
    """Return the canonical zone name if valid, else None."""
    name = name.strip()
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _weekday_time_to_utc(monday: datetime.date, weekday_index: int,
                         hour: int, minute: int, tz_name: str) -> float:
    """
    Combine the current week's <weekday_index> (Mon=0) with a wall-clock time in
    <tz_name>, returning the absolute UTC unix timestamp.
    """
    target_date = monday + datetime.timedelta(days=weekday_index)
    tz = ZoneInfo(tz_name)
    local_dt = datetime.datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, tzinfo=tz,
    )
    return local_dt.timestamp()


def _build_gcal_url(title: str, description: Optional[str], start_ts: float) -> str:
    """Build an 'Add to Google Calendar' template link (default 1h length)."""
    start = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc)
    end = start + _GCAL_DEFAULT_LEN

    def fmt(d: datetime.datetime) -> str:
        return d.strftime("%Y%m%dT%H%M%SZ")

    from urllib.parse import urlencode
    params = {"action": "TEMPLATE", "text": title or "Event",
              "dates": f"{fmt(start)}/{fmt(end)}"}
    if description:
        params["details"] = description
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _hex_to_int(hex_str: Optional[str]) -> Optional[int]:
    if not hex_str:
        return None
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_str.strip())
    return int(m.group(1), 16) if m else None


# ---------------------------------------------------------------------------
# Permission helpers
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
# Result type for refresh operations
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
# Board rendering (M1: skeleton only — empty day messages)
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


def _event_embed(guild: discord.Guild, unit_role: Optional[discord.Role],
                 event: Dict[str, Any]) -> discord.Embed:
    """
    Render one event as an embed: title, description, viewer-local time + relative
    + Google Calendar link, optional image, colour, and a created-by footer.
    (RSVP fields/buttons are added in M3.)
    """
    color = _hex_to_int(event.get("color"))
    if color is None:
        color = unit_role.color.value if (unit_role and unit_role.color.value) else _DEFAULT_COLOR

    embed = discord.Embed(title=event.get("title", "Untitled Event"), color=color)
    if event.get("description"):
        embed.description = event["description"]

    ts = int(event["start_utc"])
    time_lines = [f"<t:{ts}:F>", f"<t:{ts}:R>"]
    if event.get("recurring"):
        time_lines.append("🔁 Repeats weekly")
    time_lines.append(f"[Add to Google Calendar]({_build_gcal_url(event.get('title'), event.get('description'), event['start_utc'])})")
    embed.add_field(name="Time", value="\n".join(time_lines), inline=False)

    if event.get("image_url"):
        embed.set_image(url=event["image_url"])

    creator = guild.get_member(event["created_by"]) if event.get("created_by") else None
    creator_name = creator.display_name if creator else "Unknown"
    embed.set_footer(text=f"Added by {creator_name}")
    return embed


# ---------------------------------------------------------------------------
# DM flow support
# ---------------------------------------------------------------------------

class _FlowCancelled(Exception):
    """Raised inside the creation flow when the user types `cancel`."""


class _ConfirmView(discord.ui.View):
    """A transient Confirm/Cancel view for the creation flow's final step."""

    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=_FLOW_TIMEOUT)
        self.user_id = user_id
        self.value: Optional[bool] = None  # None=timeout, True=confirm, False=cancel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.edit_message(view=None)
        self.stop()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------

class ScheduleCog(commands.Cog, name="Schedule"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Storage helpers ────────────────────────────────────────────────────────

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

    # ── Editor gating (per-unit) ────────────────────────────────────────────────

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

    # ── Event grouping ───────────────────────────────────────────────────────────

    async def _events_for_unit(self, guild_id: int, unit_role_id: int) -> Dict[str, List[Dict[str, Any]]]:
        """
        Return {weekday_key: [event, ...]} for one unit, each day's list sorted by
        start time. M2 renders every stored event for the unit under its weekday;
        week-scoping and one-off cleanup arrive with the Monday refresh in M4.
        """
        config = await self._load_config(guild_id)
        events: Dict[str, Any] = config['events']
        by_day: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _DAY_KEYS}
        for ev in events.values():
            if ev.get("unit_role_id") != unit_role_id:
                continue
            day = ev.get("weekday")
            if day in by_day:
                by_day[day].append(ev)
        for day in by_day:
            by_day[day].sort(key=lambda e: e.get("start_utc", 0))
        return by_day

    # ── Core board build ─────────────────────────────────────────────────────────

    async def _build_board(self, guild: discord.Guild, unit_role_id: int) -> BoardResult:
        """
        Post or edit the header + 7 day messages (plus overflow messages for days
        with more than 10 events) for one unit's schedule channel. Event embeds are
        rendered under each day; everything is edited in place where possible.
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
        by_day = await self._events_for_unit(guild.id, unit_role_id)
        msgs: Dict[str, int] = dict(all_messages.get(key, {}))
        changed = False

        # Build an ordered plan of (slot, content, embeds). Each day yields one
        # primary message and, when it overflows 10 embeds, one overflow message.
        plan: List[Tuple[str, Optional[str], List[discord.Embed]]] = [
            ("header", _header_content(unit_role, monday), []),
        ]
        used_slots = {"header"}
        for day_key, day_label in _DAYS:
            day_events = by_day.get(day_key, [])
            embeds = [_event_embed(guild, unit_role, ev) for ev in day_events]
            primary = embeds[:_EMBEDS_PER_MESSAGE]
            overflow = embeds[_EMBEDS_PER_MESSAGE:2 * _EMBEDS_PER_MESSAGE]
            plan.append((day_key, _day_content(day_label), primary))
            used_slots.add(day_key)
            if overflow:
                plan.append((f"{day_key}_of", f"# {day_label} (cont.)", overflow))
                used_slots.add(f"{day_key}_of")

        try:
            for slot, content, embeds in plan:
                existing_id = msgs.get(slot)
                if existing_id:
                    try:
                        msg = await channel.fetch_message(existing_id)
                        await msg.edit(content=content, embeds=embeds)
                        result.updated += 1
                        continue
                    except discord.NotFound:
                        pass  # fall through to repost
                msg = await channel.send(content=content, embeds=embeds)
                msgs[slot] = msg.id
                changed = True
                result.built += 1

            # Delete any stale overflow messages no longer needed (a day shrank).
            for slot in list(msgs.keys()):
                if slot not in used_slots:
                    stale_id = msgs.pop(slot)
                    changed = True
                    try:
                        old = await channel.fetch_message(stale_id)
                        await old.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
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

    async def _linked_unit_autocomplete(self, interaction: discord.Interaction, current: str):
        config = await self._load_config(interaction.guild.id)
        out = []
        for rid_str in config['links'].keys():
            try:
                rid = int(rid_str)
            except (TypeError, ValueError):
                continue
            role = interaction.guild.get_role(rid)
            if role and current.lower() in role.name.lower():
                out.append(app_commands.Choice(name=role.name, value=str(rid)))
        return out[:25]

    @schedule_group.command(name="set-timezone", description="Set your timezone for entering event times.")
    @app_commands.describe(timezone="An IANA timezone, e.g. America/New_York or Europe/Berlin.")
    async def cmd_set_timezone(self, interaction: discord.Interaction, timezone: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        canonical = _validate_timezone(timezone)
        if not canonical:
            await interaction.response.send_message(
                f"⚠️ `{timezone}` isn't a valid IANA timezone. Examples: `America/New_York`, "
                f"`Europe/London`, `Australia/Sydney`.", ephemeral=True)
            return
        config = await self._load_config(interaction.guild.id)
        user_tz = dict(config['user_tz'])
        user_tz[str(interaction.user.id)] = canonical
        await self._save(interaction.guild.id, 'user_tz', user_tz)
        await interaction.response.send_message(
            f"✅ Your timezone is set to `{canonical}`. Event times you enter will be read in this zone.",
            ephemeral=True)

    @schedule_group.command(name="add", description="Add an event to a unit's schedule (guided in DMs).")
    @app_commands.describe(unit="The unit whose schedule you're adding to.")
    @app_commands.autocomplete(unit=_linked_unit_autocomplete)
    async def cmd_add(self, interaction: discord.Interaction, unit: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        try:
            unit_role_id = int(unit)
        except (TypeError, ValueError):
            await interaction.response.send_message("Invalid unit selection.", ephemeral=True)
            return

        config = await self._load_config(interaction.guild.id)
        if str(unit_role_id) not in config['links']:
            await interaction.response.send_message(
                "That unit isn't linked to a schedule channel.", ephemeral=True)
            return
        if not await self._can_edit(interaction, unit_role_id):
            await interaction.response.send_message(
                "⛔ You don't have permission to edit this unit's schedule.", ephemeral=True)
            return

        # Open a DM channel up front so we can fail fast if DMs are closed.
        try:
            dm = await interaction.user.create_dm()
            await dm.send(embed=discord.Embed(
                title="📅 New schedule event",
                description="Let's set up your event. You can type `cancel` at any time to stop.",
                color=_DEFAULT_COLOR))
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I couldn't DM you. Enable **Direct Messages** for this server and try again.",
                ephemeral=True)
            return

        await interaction.response.send_message("📨 Check your DMs to build the event.", ephemeral=True)
        try:
            await self._run_creation_flow(interaction, dm, unit_role_id)
        except _FlowCancelled:
            await dm.send("❌ Event creation cancelled.")
        except TimeoutError:
            await dm.send("⏰ Timed out. Run `/schedule add` again to restart.")
        except Exception as exc:  # noqa: BLE001 — surface to user, re-raise for logs
            await dm.send(f"⚠️ Something went wrong: `{type(exc).__name__}`.")
            raise

    # ── DM creation flow ───────────────────────────────────────────────────────

    async def _ask(self, dm: discord.DMChannel, user_id: int,
                   embed: discord.Embed) -> discord.Message:
        """Send a prompt embed and wait for the user's next DM. Honors `cancel`."""
        await dm.send(embed=embed)

        def check(m: discord.Message) -> bool:
            return m.author.id == user_id and m.guild is None

        msg = await self.bot.wait_for("message", check=check, timeout=_FLOW_TIMEOUT)
        if msg.content.strip().lower() == "cancel":
            raise _FlowCancelled()
        return msg

    @staticmethod
    def _prompt(title: str, lines: List[str]) -> discord.Embed:
        e = discord.Embed(title=title, description="\n".join(lines), color=_DEFAULT_COLOR)
        e.set_footer(text="Type `cancel` to stop.")
        return e

    async def _ensure_timezone(self, dm: discord.DMChannel, interaction: discord.Interaction) -> str:
        config = await self._load_config(interaction.guild.id)
        user_tz = dict(config['user_tz'])
        tz = user_tz.get(str(interaction.user.id))
        if tz:
            return tz
        while True:
            msg = await self._ask(dm, interaction.user.id, self._prompt(
                "🌍 What's your timezone?",
                ["Enter an IANA timezone so I can read your event times correctly.",
                 "Examples: `America/New_York`, `Europe/London`, `Australia/Sydney`.",
                 "Find yours: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>"]))
            canonical = _validate_timezone(msg.content)
            if canonical:
                user_tz[str(interaction.user.id)] = canonical
                await self._save(interaction.guild.id, 'user_tz', user_tz)
                await dm.send(f"✅ Timezone set to `{canonical}`.")
                return canonical
            await dm.send("❌ That isn't a valid IANA timezone. Try again.")

    async def _run_creation_flow(self, interaction: discord.Interaction,
                                 dm: discord.DMChannel, unit_role_id: int) -> None:
        user_id = interaction.user.id
        tz_name = await self._ensure_timezone(dm, interaction)

        # Day of week
        day_index = None
        day_lines = [f"**{i+1}** {label.title()}" for i, (_, label) in enumerate(_DAYS)]
        while day_index is None:
            msg = await self._ask(dm, user_id, self._prompt(
                "📆 Which day?", ["Reply with a number:", *day_lines]))
            txt = msg.content.strip().lower()
            if txt.isdigit() and 1 <= int(txt) <= 7:
                day_index = int(txt) - 1
            else:
                for i, (k, label) in enumerate(_DAYS):
                    if txt in (k, label.lower()):
                        day_index = i
                        break
            if day_index is None:
                await dm.send("❌ Reply with a number 1–7 (or the day name).")
        day_key, day_label = _DAYS[day_index]

        # Title
        title = None
        while not title:
            msg = await self._ask(dm, user_id, self._prompt(
                "✏️ Event title", ["Up to 200 characters."]))
            if msg.content.strip():
                title = msg.content.strip()[:200]
            else:
                await dm.send("❌ Title can't be empty.")

        # Description
        msg = await self._ask(dm, user_id, self._prompt(
            "📝 Description", ["Type `none` for no description. Up to 1600 characters."]))
        description = None if msg.content.strip().lower() == "none" else msg.content.strip()[:1600]

        # Time of day
        start_ts = None
        while start_ts is None:
            msg = await self._ask(dm, user_id, self._prompt(
                f"🕒 What time on {day_label.title()}?",
                [f"Your timezone: `{tz_name}`. Change it with `/schedule set-timezone`.",
                 "Examples: `8pm`, `8:30 PM`, `20:00`."]))
            parsed = _parse_time_of_day(msg.content)
            if not parsed:
                await dm.send("❌ I couldn't read that time. Try `8pm` or `20:00`.")
                continue
            hour, minute = parsed
            start_ts = _weekday_time_to_utc(_current_monday(), day_index, hour, minute, tz_name)

        # Optional image
        msg = await self._ask(dm, user_id, self._prompt(
            "🖼️ Image (optional)", ["Attach an image, paste an image URL, or type `none`."]))
        image_url = None
        if msg.attachments:
            image_url = msg.attachments[0].url
        elif msg.content.strip().lower() != "none" and re.match(r"^https?://", msg.content.strip()):
            image_url = msg.content.strip()

        # Optional colour
        msg = await self._ask(dm, user_id, self._prompt(
            "🎨 Colour (optional)", ["Enter a hex colour like `#5865F2`, or type `none`."]))
        color_hex = None
        if msg.content.strip().lower() != "none":
            if _hex_to_int(msg.content):
                color_hex = "#" + msg.content.strip().lstrip("#").upper()
            else:
                await dm.send("ℹ️ Not a valid hex colour; using the default.")

        # Recurring
        recurring = None
        while recurring is None:
            msg = await self._ask(dm, user_id, self._prompt(
                "🔁 Repeat weekly?",
                [f"**1** One-off — only this {day_label.title()}",
                 f"**2** Weekly — every {day_label.title()}"]))
            t = msg.content.strip().lower()
            if t in ("1", "no", "n", "one-off", "oneoff"):
                recurring = False
            elif t in ("2", "yes", "y", "weekly"):
                recurring = True
            else:
                await dm.send("❌ Reply with 1 or 2.")

        # Confirmation
        guild = interaction.guild
        unit_role = guild.get_role(unit_role_id)
        preview = {
            "unit_role_id": unit_role_id, "weekday": day_key, "title": title,
            "description": description, "image_url": image_url, "color": color_hex,
            "start_utc": start_ts, "recurring": recurring, "created_by": user_id,
        }
        confirm_embed = _event_embed(guild, unit_role, preview)
        confirm_embed.title = f"Confirm: {title}"
        view = _ConfirmView(user_id)
        await dm.send(content=f"Add this to **{unit_role.name if unit_role else unit_role_id}** "
                              f"on **{day_label.title()}**?",
                      embed=confirm_embed, view=view)
        await view.wait()
        if not view.value:
            await dm.send("❌ Event creation cancelled." if view.value is False else "⏰ Confirmation timed out.")
            return

        # Persist + rebuild
        config = await self._load_config(guild.id)
        events = dict(config['events'])
        event_id = uuid.uuid4().hex[:12]
        events[event_id] = {
            **preview,
            "week_start": _current_monday().isoformat(),
            "rsvps": {"confirmed": [], "tentative": [], "declined": []},
        }
        await self._save(guild.id, 'events', events)
        result = await self._build_board(guild, unit_role_id)
        if result.errors:
            await dm.send(f"✅ Event saved, but the board had issues: {result.errors[0]}")
        else:
            await dm.send(f"✅ **{title}** added to {day_label.title()}.")

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

        embed.set_footer(text="RSVPs, edit/delete, and the weekly auto-refresh arrive in later milestones.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Error handler ──────────────────────────────────────────────────────────

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
