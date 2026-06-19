"""
Per-unit weekly schedule boards.

Each unit is linked to a schedule channel (same per-unit link model as co_chat
and roster). The board is a header message plus seven day messages (Monday →
Sunday) showing the current week; days with more than 10 events spill into an
overflow message. All messages are edited in place; their IDs are stored so the
board survives restarts.

Events are anchored to a specific week. A one-off shows only in its week; a
recurring event shows every week on its weekday (from its start week) except
weeks listed in its `skip_weeks`. Times are stored as a weekday + time-of-day +
timezone and recomputed per displayed week, so a recurring event keeps the same
local time across DST changes. The board always shows the current week (events
scheduled for future weeks appear once that week arrives).

Editors add/edit/delete events via guided DM flows; members sign up via a per-day
Sign Up button (Discord attaches buttons to messages, not embeds, so one button
serves all of a day's events through an ephemeral picker). RSVPs render inside
each event embed, sorted by the roster's rank ladder, and update just the one
affected day message (not the whole board).

Each Monday a background loop rolls every board to the new week: it drops past
one-off events, prunes stale skips, regenerates recurring events, rebuilds the
boards, pings each unit, and deletes the ping after 10 minutes (restart-safe).

Commands
    /scheduleconfig link|unlink|add-access-role|remove-access-role|status   staff only
    /schedule refresh                  staff only
    /schedule add / edit / delete      editors: developers, management, the unit's
                                       Unit Leader, or holders of a configured access role
    /schedule set-timezone             any member (sets their own input timezone)
    Sign Up button                     any member holding the unit's role

Recurring events support "this week only" vs "the whole series" on both edit and
delete: a one-week delete adds that week to `skip_weeks`; a one-week edit creates
a one-off override for the week and skips the series there.

Storage (all under the `schedule.` namespace in the guild_settings KV store)
    schedule.links          dict   {str(unit_role_id): channel_id}
    schedule.access_roles   list   rank-ladder role IDs allowed to edit a schedule
    schedule.messages       dict   {str(unit_role_id): {"header": id, "mon": id, "mon_of": id?, ...}}
    schedule.events         dict   {event_id: {unit_role_id, weekday, title, description,
                                   image_url, color, start_utc, tz, tod_hour, tod_minute,
                                   recurring, skip_weeks?, week_start, created_by,
                                   rsvps:{confirmed,tentative,declined}}}
    schedule.user_tz        dict   {str(user_id): "IANA/Zone"}
    schedule.week_marker    str    "YYYY-MM-DD" Monday the boards currently reflect
    schedule.pings          dict   {str(unit_role_id): {channel_id, message_id, post_ts}}

Shared identities (read-only):
    co_chat.unit_roles      list   the units in scope
    co_chat.leader_role_id  int    Unit Leader role — always allowed to edit
    co_chat.co_role_id      int    Unit CO role — used in RSVP rank-sorting
    ranks.rank_order        list   the rank ladder used to sort RSVP lists

Requires the privileged members intent (via Intents.all()).
"""
import asyncio
import re
import time
import uuid
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

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
    'week_marker':  ('schedule.week_marker',  None),  # "YYYY-MM-DD" the boards reflect
    'pings':        ('schedule.pings',        {}),    # {str(unit_role_id): {channel_id,message_id,post_ts}}
}

# Maximum weeks ahead an event can be scheduled in the creation flow.
_MAX_WEEKS_AHEAD = 12
# How long the Monday roll-over ping stays before deletion.
_PING_TTL = 600  # seconds (10 minutes)


# ---------------------------------------------------------------------------
# Week-window helpers
# ---------------------------------------------------------------------------

def _current_monday(now: Optional[datetime.datetime] = None) -> datetime.date:
    """Return the date of Monday for the week containing `now` (UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    d = now.date()
    return d - datetime.timedelta(days=d.weekday())  # weekday(): Mon=0


def _week_range_label(monday: datetime.date) -> str:
    """
    A fixed, absolute week label shown identically to every viewer.

    Deliberately NOT a viewer-local <t:…:D> timestamp: a midnight-UTC timestamp
    rendered with :D shows one calendar day earlier for viewers west of UTC, so
    the Monday-first week read as Sun–Sat (e.g. 'June 14 – 20' instead of
    'June 15 – 21') for US members while EU members saw it correctly. A plain
    date string keeps the header consistent for the whole unit.
    """
    sunday = monday + datetime.timedelta(days=6)
    if monday.year != sunday.year:
        return (f"{monday.strftime('%B')} {monday.day}, {monday.year} – "
                f"{sunday.strftime('%B')} {sunday.day}, {sunday.year}")
    if monday.month != sunday.month:
        return (f"{monday.strftime('%B')} {monday.day} – "
                f"{sunday.strftime('%B')} {sunday.day}, {sunday.year}")
    return (f"{monday.strftime('%B')} {monday.day} – "
            f"{sunday.day}, {sunday.year}")


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


def _build_gcal_url(title: str, start_ts: float) -> str:
    """
    Build an 'Add to Google Calendar' template link (default 1h length).
    Only the title and dates are included; the description is intentionally
    omitted so the link stays short and can't bloat the embed's Time field
    (Discord caps each field value at 1024 chars). The full description is
    still shown in the embed body.
    """
    start = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc)
    end = start + _GCAL_DEFAULT_LEN

    def fmt(d: datetime.datetime) -> str:
        return d.strftime("%Y%m%dT%H%M%SZ")

    from urllib.parse import urlencode
    params = {"action": "TEMPLATE", "text": (title or "Event")[:300],
              "dates": f"{fmt(start)}/{fmt(end)}"}
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _hex_to_int(hex_str: Optional[str]) -> Optional[int]:
    if not hex_str:
        return None
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_str.strip())
    return int(m.group(1), 16) if m else None


def _event_start_ts(event: Dict[str, Any], monday: datetime.date) -> float:
    """
    The event's start instant for the given displayed week. When the event stores
    a time-of-day + timezone (M4+), recompute against that week's weekday so a
    recurring event lands at the right local time every week (DST-correct).
    Falls back to the stored absolute start_utc for older records.
    """
    tz = event.get("tz")
    h = event.get("tod_hour")
    mn = event.get("tod_minute")
    wd = event.get("weekday")
    if tz and h is not None and wd in _DAY_KEYS:
        try:
            return _weekday_time_to_utc(monday, _DAY_KEYS.index(wd), int(h), int(mn or 0), tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return float(event.get("start_utc", 0))


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


def _rank_index(member: discord.Member, rank_order: List[Dict[str, Any]],
                co_role_id: Optional[int], leader_role_id: Optional[int]) -> int:
    """
    Position of a member in the roster's rank ordering (lower = higher rank):
    Commander (Unit Leader) → configured ranks high→low → Unit CO edge → Trooper.
    """
    role_ids = {r.id for r in member.roles}
    if leader_role_id and leader_role_id in role_ids:
        return 0
    rev = list(reversed(rank_order))  # high -> low
    for i, rank in enumerate(rev):
        rid = rank.get("role_id")
        if rid and rid in role_ids:
            return 1 + i
    if co_role_id and co_role_id in role_ids:
        return 1 + len(rev)
    return 2 + len(rev)


def _sorted_rsvp_members(guild: discord.Guild, user_ids: List[int],
                         rank_ctx: Optional[Tuple]) -> List[discord.Member]:
    """Resolve IDs to current members and sort them by rank, then display name."""
    members = [guild.get_member(uid) for uid in user_ids]
    members = [m for m in members if m is not None]
    if rank_ctx:
        rank_order, co_role_id, leader_role_id = rank_ctx
        members.sort(key=lambda m: (_rank_index(m, rank_order, co_role_id, leader_role_id),
                                    m.display_name.lower()))
    else:
        members.sort(key=lambda m: m.display_name.lower())
    return members


def _rsvp_field_value(guild: discord.Guild, user_ids: List[int],
                      rank_ctx: Optional[Tuple]) -> str:
    """
    Newline list of attendees (rank-sorted, escaped, capped). Members that are in
    the cache are shown by display name; any that aren't cached fall back to a
    mention so a sign-up never silently disappears from the board.
    """
    members = _sorted_rsvp_members(guild, user_ids, rank_ctx)
    resolved_ids = {m.id for m in members}
    lines = [discord.utils.escape_markdown(m.display_name) for m in members]
    # Append any IDs that couldn't be resolved to a cached member, as mentions.
    for uid in user_ids:
        if uid not in resolved_ids:
            lines.append(f"<@{uid}>")
    if not lines:
        return "—"
    out, shown = [], 0
    length = 0
    for line in lines:
        if length + len(line) + 1 > 1000:
            break
        out.append(line)
        length += len(line) + 1
        shown += 1
    if shown < len(lines):
        out.append(f"…and {len(lines) - shown} more")
    return "\n".join(out)


def _event_embed(guild: discord.Guild, unit_role: Optional[discord.Role],
                 event: Dict[str, Any], rank_ctx: Optional[Tuple] = None) -> discord.Embed:
    """
    Render one event as an embed: title, description, viewer-local time + relative
    + Google Calendar link, optional image, colour, rank-sorted RSVP lists
    (Confirmed / Tentative / Declined), and a created-by footer.
    """
    color = _hex_to_int(event.get("color"))
    if color is None:
        color = unit_role.color.value if (unit_role and unit_role.color.value) else _DEFAULT_COLOR

    embed = discord.Embed(title=event.get("title", "Untitled Event"), color=color)
    if event.get("description"):
        embed.description = event["description"][:4096]

    ts = int(event["start_utc"])
    time_lines = [f"<t:{ts}:F>", f"<t:{ts}:R>"]
    if event.get("recurring"):
        time_lines.append("🔁 Repeats weekly")
    time_lines.append(f"[Add to Google Calendar]({_build_gcal_url(event.get('title'), event['start_utc'])})")
    embed.add_field(name="Time", value="\n".join(time_lines)[:1024], inline=False)

    if event.get("image_url"):
        embed.set_image(url=event["image_url"])

    # RSVP lists (rank-sorted), always shown so the board reflects sign-ups.
    rsvps = event.get("rsvps") or {}
    confirmed = rsvps.get("confirmed", [])
    tentative = rsvps.get("tentative", [])
    declined = rsvps.get("declined", [])
    embed.add_field(name=f"✅ Confirmed ({len(confirmed)})",
                    value=_rsvp_field_value(guild, confirmed, rank_ctx), inline=True)
    embed.add_field(name=f"❓ Tentative ({len(tentative)})",
                    value=_rsvp_field_value(guild, tentative, rank_ctx), inline=True)
    embed.add_field(name=f"❌ Declined ({len(declined)})",
                    value=_rsvp_field_value(guild, declined, rank_ctx), inline=True)

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
# RSVP support (M3)
# ---------------------------------------------------------------------------

_RSVP_BUTTONS = [
    ("confirmed", "Confirmed", discord.ButtonStyle.success, "✅"),
    ("tentative", "Tentative", discord.ButtonStyle.secondary, "❓"),
    ("declined",  "Declined",  discord.ButtonStyle.danger,    "❌"),
    ("withdraw",  "Withdraw",  discord.ButtonStyle.secondary, "🚫"),
]


class _RsvpPickerView(discord.ui.View):
    """
    Ephemeral, per-click picker shown when a unit member taps Sign Up. Lets them
    choose which event on that day (skipped if only one) and a status. Applies the
    RSVP and rebuilds the unit board so the embed updates.
    """

    def __init__(self, cog, guild: discord.Guild, unit_role_id: int,
                 events: List[Dict[str, Any]], user_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.unit_role_id = unit_role_id
        self.events = events
        self.user_id = user_id
        self.selected_event_id: Optional[str] = events[0]["_id"] if len(events) == 1 else None

        if len(events) > 1:
            options = []
            for ev in events[:25]:
                ts = int(ev["start_utc"])
                options.append(discord.SelectOption(
                    label=ev.get("title", "Event")[:100],
                    value=ev["_id"],
                    description=f"starts <t:{ts}:t>"[:100]))
            sel = discord.ui.Select(placeholder="Choose an event", options=options, row=0)
            sel.callback = self._on_select
            self.add_item(sel)

        for status, label, style, emoji in _RSVP_BUTTONS:
            btn = discord.ui.Button(label=label, style=style, emoji=emoji, row=1)
            btn.callback = self._make_status_cb(status)
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _on_select(self, interaction: discord.Interaction):
        self.selected_event_id = interaction.data["values"][0]
        title = next((e.get("title") for e in self.events if e["_id"] == self.selected_event_id), "event")
        await interaction.response.edit_message(
            content=f"Selected **{title}**. Now choose your status:", view=self)

    def _make_status_cb(self, status: str):
        async def cb(interaction: discord.Interaction):
            if not self.selected_event_id:
                await interaction.response.send_message(
                    "Pick an event from the menu first.", ephemeral=True)
                return
            # Acknowledge immediately — the board rebuild below can exceed Discord's
            # 3s interaction deadline, which would otherwise show "Interaction failed".
            await interaction.response.defer()
            ok = await self.cog._apply_rsvp(
                self.guild, self.unit_role_id, self.selected_event_id, self.user_id, status)
            title = next((e.get("title") for e in self.events if e["_id"] == self.selected_event_id), "event")
            if not ok:
                msg = "⚠️ That event no longer exists."
            elif status == "withdraw":
                msg = f"🚫 Withdrawn from **{title}**."
            else:
                msg = f"✅ You're marked **{status}** for **{title}**."
            await interaction.edit_original_response(content=msg, view=None)
            self.stop()
        return cb


class SignupView(discord.ui.View):
    """
    Persistent view attached to each day message that has events. A single static
    custom_id; on click the cog reverse-looks-up which unit/day the message is and
    opens an ephemeral picker. Registered once at cog_load.
    """

    def __init__(self, cog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Sign Up", style=discord.ButtonStyle.primary,
                       emoji="✍️", custom_id="schedule:signup")
    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_signup_click(interaction)


class _EventSelectView(discord.ui.View):
    """Ephemeral event picker for /schedule edit and /schedule delete."""

    def __init__(self, events: List[Dict[str, Any]], user_id: int, on_pick) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.on_pick = on_pick
        options = []
        for ev in events[:25]:
            day = ev.get("weekday", "").upper()
            if ev.get("recurring"):
                desc = f"{day} · weekly"
            else:
                desc = f"{day} · week of {ev.get('week_start', '?')}"
            options.append(discord.SelectOption(
                label=ev.get("title", "Event")[:100],
                value=ev["_id"],
                description=desc[:100]))
        sel = discord.ui.Select(placeholder="Choose an event", options=options)
        sel.callback = self._cb
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _cb(self, interaction: discord.Interaction):
        await self.on_pick(interaction, interaction.data["values"][0])


class _RecurringChoiceView(discord.ui.View):
    """Ephemeral 'this week only / whole series / cancel' choice for recurring events."""

    def __init__(self, user_id: int, on_week, on_series,
                 week_label: str = "This week only", series_label: str = "Whole series") -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        b_week = discord.ui.Button(label=week_label, style=discord.ButtonStyle.primary)
        b_week.callback = self._mk(on_week)
        self.add_item(b_week)
        b_series = discord.ui.Button(label=series_label, style=discord.ButtonStyle.danger)
        b_series.callback = self._mk(on_series)
        self.add_item(b_series)
        b_cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        b_cancel.callback = self._cancel
        self.add_item(b_cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    def _mk(self, cb):
        async def inner(interaction: discord.Interaction):
            await cb(interaction)
            self.stop()
        return inner

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class _DeleteConfirmView(discord.ui.View):
    """Ephemeral confirm for deleting one event."""

    def __init__(self, user_id: int, on_confirm) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------

class ScheduleCog(commands.Cog, name="Schedule"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Register the persistent Sign Up view so its buttons work after restarts.
        self.bot.add_view(SignupView(self))
        self.weekly_loop.start()

    async def cog_unload(self) -> None:
        self.weekly_loop.cancel()

    # ── Monday auto-refresh + ping ───────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def weekly_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                print(f"[schedule] weekly tick failed for {guild.id}: {exc}")

    @weekly_loop.before_loop
    async def _before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        config = await self._load_config(guild.id)
        if config['links']:
            monday = _current_monday()
            iso = monday.isoformat()
            marker = config['week_marker']
            if marker is None:
                # First run for this guild — adopt the current week silently (no ping).
                await self._save(guild.id, 'week_marker', iso)
            elif marker != iso:
                await self._roll_over(guild, monday)
                await self._save(guild.id, 'week_marker', iso)
        await self._cleanup_pings(guild)

    async def _roll_over(self, guild: discord.Guild, monday: datetime.date) -> None:
        """A new week began: drop past one-offs, prune old skips, rebuild + ping each unit."""
        iso = monday.isoformat()
        config = await self._load_config(guild.id)

        # 1) Clean up events from past weeks.
        events = dict(config['events'])
        changed = False
        for eid in list(events.keys()):
            ev = events[eid]
            if not ev.get("recurring"):
                ws = ev.get("week_start")
                if ws and ws < iso:
                    del events[eid]
                    changed = True
            else:
                sw = ev.get("skip_weeks") or []
                pruned = [w for w in sw if w >= iso]
                if len(pruned) != len(sw):
                    ev["skip_weeks"] = pruned
                    events[eid] = ev
                    changed = True
        if changed:
            await self._save(guild.id, 'events', events)

        # 2) Rebuild each in-scope unit board for the new week and ping it.
        unit_roles, _ = await self._source_data(guild.id)
        unit_role_set = set(unit_roles)
        pings = dict(config['pings'])
        for rid_str, channel_id in config['links'].items():
            try:
                rid = int(rid_str)
            except (TypeError, ValueError):
                continue
            unit_role = guild.get_role(rid)
            if unit_role is None or rid not in unit_role_set:
                continue
            await self._build_board(guild, rid)
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            try:
                ping = await channel.send(
                    content=f"📅 {unit_role.mention} — the schedule for the new week is up!",
                    allowed_mentions=discord.AllowedMentions(roles=[unit_role]))
                pings[rid_str] = {"channel_id": channel.id, "message_id": ping.id, "post_ts": time.time()}
            except (discord.Forbidden, discord.HTTPException):
                pass
        await self._save(guild.id, 'pings', pings)

    async def _cleanup_pings(self, guild: discord.Guild) -> None:
        """Delete roll-over pings older than the TTL (restart-safe via stored records)."""
        config = await self._load_config(guild.id)
        pings = dict(config['pings'])
        if not pings:
            return
        now = time.time()
        changed = False
        for rid_str in list(pings.keys()):
            rec = pings[rid_str]
            if now - rec.get("post_ts", 0) >= _PING_TTL:
                channel = guild.get_channel(rec.get("channel_id"))
                if channel is not None:
                    try:
                        m = await channel.fetch_message(rec.get("message_id"))
                        await m.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                del pings[rid_str]
                changed = True
        if changed:
            await self._save(guild.id, 'pings', pings)

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

    async def _rank_ctx(self, guild_id: int) -> Tuple:
        """(rank_order, co_role_id, leader_role_id) for rank-sorting RSVP lists."""
        db = self.bot.db
        rank_order = await db.get_guild_setting(guild_id, "ranks.rank_order", [])
        co_role_id = await db.get_guild_setting(guild_id, "co_chat.co_role_id", None)
        leader_role_id = await db.get_guild_setting(guild_id, "co_chat.leader_role_id", None)
        return rank_order, co_role_id, leader_role_id

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

    async def _events_for_unit(self, guild_id: int, unit_role_id: int,
                               monday: Optional[datetime.date] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Return {weekday_key: [event, ...]} for one unit for the displayed week
        (defaults to the current week), each day's list sorted by start time.

        Inclusion rules:
          - one-off  -> only when its `week_start` equals the displayed week
          - recurring -> every week from its origin week onward, except weeks
                         listed in `skip_weeks`
        Each returned event carries `_id` and a `start_utc` recomputed for the
        displayed week (so recurring events show the right local time weekly).
        """
        monday = monday or _current_monday()
        disp = monday.isoformat()
        config = await self._load_config(guild_id)
        events: Dict[str, Any] = config['events']
        by_day: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _DAY_KEYS}
        for ev_id, ev in events.items():
            if ev.get("unit_role_id") != unit_role_id:
                continue
            day = ev.get("weekday")
            if day not in by_day:
                continue
            if ev.get("recurring"):
                origin = ev.get("week_start")
                if origin and disp < origin:
                    continue  # series hasn't started yet
                if disp in (ev.get("skip_weeks") or []):
                    continue  # this occurrence was removed/overridden
            else:
                if ev.get("week_start") != disp:
                    continue  # one-off for a different week
            copy = {**ev, "_id": ev_id, "start_utc": _event_start_ts(ev, monday)}
            by_day[day].append(copy)
        for day in by_day:
            by_day[day].sort(key=lambda e: e.get("start_utc", 0))
        return by_day

    # ── Core board build ─────────────────────────────────────────────────────────

    def _day_plan(self, guild: discord.Guild, unit_role: Optional[discord.Role],
                  day_key: str, day_label: str, day_events: List[Dict[str, Any]],
                  rank_ctx: Optional[Tuple]) -> Tuple[List[Tuple], set]:
        """
        Build the message plan for ONE day: a primary message and, if it overflows
        10 embeds, one (cont.) message. Returns (entries, owned_slots) where
        owned_slots is everything this day is responsible for (so cleanup can drop a
        now-unused overflow message). The Sign Up view is attached when there are events.
        """
        embeds = [_event_embed(guild, unit_role, ev, rank_ctx) for ev in day_events]
        primary = embeds[:_EMBEDS_PER_MESSAGE]
        overflow = embeds[_EMBEDS_PER_MESSAGE:2 * _EMBEDS_PER_MESSAGE]
        view = SignupView(self) if day_events else None
        entries: List[Tuple] = [(day_key, _day_content(day_label), primary, view)]
        if overflow:
            entries.append((f"{day_key}_of", f"# {day_label} (cont.)", overflow, None))
        owned = {day_key, f"{day_key}_of"}
        return entries, owned

    async def _apply_entries(self, channel: discord.abc.Messageable, msgs: Dict[str, int],
                             entries: List[Tuple], owned_slots: set,
                             result: BoardResult) -> bool:
        """
        Edit-in-place or post each (slot, content, embeds, view) entry, then delete
        any owned slot that is no longer used. Returns whether `msgs` changed.
        """
        changed = False
        entry_slots = {e[0] for e in entries}
        for slot, content, embeds, view in entries:
            existing_id = msgs.get(slot)
            if existing_id:
                try:
                    msg = await channel.fetch_message(existing_id)
                    await msg.edit(content=content, embeds=embeds, view=view)
                    result.updated += 1
                    continue
                except discord.NotFound:
                    pass  # fall through to repost
            msg = await channel.send(content=content, embeds=embeds, view=view)
            msgs[slot] = msg.id
            changed = True
            result.built += 1

        for slot in list(owned_slots):
            if slot not in entry_slots and slot in msgs:
                stale_id = msgs.pop(slot)
                changed = True
                try:
                    old = await channel.fetch_message(stale_id)
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        return changed

    async def _build_board(self, guild: discord.Guild, unit_role_id: int) -> BoardResult:
        """
        Post or edit the header + 7 day messages (plus overflow messages) for one
        unit's schedule channel, rendering the current week. Edited in place where
        possible. Use _rebuild_day for single-day changes (RSVPs, one event).
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
        by_day = await self._events_for_unit(guild.id, unit_role_id, monday)
        rank_ctx = await self._rank_ctx(guild.id)
        msgs: Dict[str, int] = dict(all_messages.get(key, {}))
        changed = False

        try:
            changed |= await self._apply_entries(
                channel, msgs,
                [("header", _header_content(unit_role, monday), [], None)],
                {"header"}, result)
            for day_key, day_label in _DAYS:
                entries, owned = self._day_plan(
                    guild, unit_role, day_key, day_label, by_day.get(day_key, []), rank_ctx)
                changed |= await self._apply_entries(channel, msgs, entries, owned, result)
        except discord.Forbidden:
            result.errors.append(f"Missing permission to post in the schedule channel for {unit_role.name}")
        except discord.HTTPException as exc:
            result.errors.append(f"Discord error building {unit_role.name} board: {exc}")

        if changed:
            all_messages[key] = msgs
            await self._save(guild.id, 'messages', all_messages)
        return result

    async def _rebuild_day(self, guild: discord.Guild, unit_role_id: int, day_key: str) -> None:
        """
        Re-render just one day's message(s) for a unit — the fast path for RSVPs and
        single-event add/edit/delete, avoiding a full 8-message board rebuild.
        """
        config = await self._load_config(guild.id)
        key = str(unit_role_id)
        channel_id = config['links'].get(key)
        if not channel_id:
            return
        unit_role = guild.get_role(unit_role_id)
        channel = guild.get_channel(channel_id)
        if unit_role is None or channel is None:
            return

        monday = _current_monday()
        by_day = await self._events_for_unit(guild.id, unit_role_id, monday)
        rank_ctx = await self._rank_ctx(guild.id)
        day_label = dict(_DAYS)[day_key]
        entries, owned = self._day_plan(
            guild, unit_role, day_key, day_label, by_day.get(day_key, []), rank_ctx)

        all_messages = dict(config['messages'])
        msgs = dict(all_messages.get(key, {}))
        try:
            changed = await self._apply_entries(channel, msgs, entries, owned, BoardResult())
        except (discord.Forbidden, discord.HTTPException):
            changed = False
        if changed:
            all_messages[key] = msgs
            await self._save(guild.id, 'messages', all_messages)

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
        except (asyncio.TimeoutError, TimeoutError):
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

        # How many weeks out (0 = this week). One-off events are anchored to this
        # specific week; recurring events use it as the series start week.
        weeks_out = None
        while weeks_out is None:
            msg = await self._ask(dm, user_id, self._prompt(
                "🗓️ Which week?",
                ["Reply with a number:",
                 "**0** This week",
                 "**1** Next week",
                 f"**2**–**{_MAX_WEEKS_AHEAD}** that many weeks out"]))
            t = msg.content.strip()
            if t.isdigit() and 0 <= int(t) <= _MAX_WEEKS_AHEAD:
                weeks_out = int(t)
            else:
                await dm.send(f"❌ Reply with a number 0–{_MAX_WEEKS_AHEAD}.")
        target_monday = _current_monday() + datetime.timedelta(weeks=weeks_out)

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
        tod_hour = tod_minute = None
        while start_ts is None:
            msg = await self._ask(dm, user_id, self._prompt(
                f"🕒 What time on {day_label.title()}?",
                [f"Your timezone: `{tz_name}`. Change it with `/schedule set-timezone`.",
                 "Examples: `8pm`, `8:30 PM`, `20:00`."]))
            parsed = _parse_time_of_day(msg.content)
            if not parsed:
                await dm.send("❌ I couldn't read that time. Try `8pm` or `20:00`.")
                continue
            tod_hour, tod_minute = parsed
            start_ts = _weekday_time_to_utc(target_monday, day_index, tod_hour, tod_minute, tz_name)

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
        when_label = "this week" if weeks_out == 0 else ("next week" if weeks_out == 1 else f"in {weeks_out} weeks")
        while recurring is None:
            msg = await self._ask(dm, user_id, self._prompt(
                "🔁 Repeat weekly?",
                [f"**1** One-off — only {day_label.title()} ({when_label})",
                 f"**2** Weekly — every {day_label.title()} (starting {when_label})"]))
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
            "tz": tz_name, "tod_hour": tod_hour, "tod_minute": tod_minute,
            "week_start": target_monday.isoformat(),
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

        # Persist + rebuild just the affected day
        config = await self._load_config(guild.id)
        events = dict(config['events'])
        event_id = uuid.uuid4().hex[:12]
        record = {**preview, "rsvps": {"confirmed": [], "tentative": [], "declined": []}}
        if recurring:
            record["skip_weeks"] = []
        events[event_id] = record
        await self._save(guild.id, 'events', events)
        await self._rebuild_day(guild, unit_role_id, day_key)
        if weeks_out == 0:
            await dm.send(f"✅ **{title}** added to {day_label.title()}.")
        else:
            await dm.send(f"✅ **{title}** scheduled for {day_label.title()} ({when_label}). "
                          f"It'll appear on the board when that week comes around.")

    # ── RSVP ───────────────────────────────────────────────────────────────────

    async def _locate_message(self, guild_id: int, message_id: int) -> Tuple[Optional[int], Optional[str]]:
        """Find (unit_role_id, slot) for a board message id, or (None, None)."""
        config = await self._load_config(guild_id)
        for unit_str, slots in config['messages'].items():
            if not isinstance(slots, dict):
                continue
            for slot, mid in slots.items():
                if mid == message_id:
                    try:
                        return int(unit_str), slot
                    except (TypeError, ValueError):
                        return None, None
        return None, None

    async def _apply_rsvp(self, guild: discord.Guild, unit_role_id: int,
                          event_id: str, user_id: int, status: str) -> bool:
        """Set/clear a member's RSVP on one event and rebuild the unit board."""
        config = await self._load_config(guild.id)
        events = dict(config['events'])
        ev = events.get(event_id)
        if not ev:
            return False
        rsvps = ev.get("rsvps") or {"confirmed": [], "tentative": [], "declined": []}
        for k in ("confirmed", "tentative", "declined"):
            rsvps[k] = [u for u in rsvps.get(k, []) if u != user_id]
        if status in ("confirmed", "tentative", "declined"):
            rsvps[status].append(user_id)
        ev["rsvps"] = rsvps
        events[event_id] = ev
        await self._save(guild.id, 'events', events)
        await self._rebuild_day(guild, unit_role_id, ev.get("weekday"))
        return True

    async def _handle_signup_click(self, interaction: discord.Interaction) -> None:
        """Sign Up button callback: reverse-look-up the day, lock to unit members, open picker."""
        guild = interaction.guild
        unit_role_id, slot = await self._locate_message(guild.id, interaction.message.id)
        if unit_role_id is None:
            await interaction.response.send_message(
                "This schedule board looks out of date. Ask staff to run `/schedule refresh`.",
                ephemeral=True)
            return
        unit_role = guild.get_role(unit_role_id)
        if unit_role is None:
            await interaction.response.send_message("That unit no longer exists.", ephemeral=True)
            return
        # Lock RSVPs to members of the unit.
        if unit_role not in interaction.user.roles:
            await interaction.response.send_message(
                f"Only members of {unit_role.mention} can sign up on this schedule.", ephemeral=True)
            return

        day_key = slot[:-3] if slot.endswith("_of") else slot
        by_day = await self._events_for_unit(guild.id, unit_role_id)
        events = by_day.get(day_key, [])
        if not events:
            await interaction.response.send_message(
                "There are no events on this day to sign up for.", ephemeral=True)
            return

        picker = _RsvpPickerView(self, guild, unit_role_id, events, interaction.user.id)
        prompt = ("Choose your status:" if len(events) == 1
                  else "Choose an event, then your status:")
        await interaction.response.send_message(prompt, view=picker, ephemeral=True)

    # ── /schedule delete ─────────────────────────────────────────────────────────

    @schedule_group.command(name="delete", description="Delete an event from a unit's schedule.")
    @app_commands.describe(unit="The unit whose schedule you're editing.")
    @app_commands.autocomplete(unit=_linked_unit_autocomplete)
    async def cmd_delete(self, interaction: discord.Interaction, unit: str) -> None:
        unit_role_id = await self._guard_editor(interaction, unit)
        if unit_role_id is None:
            return
        events = await self._flat_events(interaction.guild.id, unit_role_id)
        if not events:
            await interaction.response.send_message("That unit has no events to delete.", ephemeral=True)
            return

        async def on_pick(inter: discord.Interaction, event_id: str):
            ev = next((e for e in events if e["_id"] == event_id), None)
            title = ev.get("title", "event") if ev else "event"

            if ev and ev.get("recurring"):
                async def del_week(ci: discord.Interaction):
                    await ci.response.defer()
                    await self._delete_event(ci.guild, unit_role_id, event_id, "week")
                    await ci.edit_original_response(
                        content=f"🗑️ Removed **{title}** for this week only (the weekly series continues).",
                        view=None)

                async def del_series(ci: discord.Interaction):
                    await ci.response.defer()
                    await self._delete_event(ci.guild, unit_role_id, event_id, "series")
                    await ci.edit_original_response(
                        content=f"🗑️ Deleted the entire **{title}** weekly series.", view=None)

                await inter.response.edit_message(
                    content=f"**{title}** repeats weekly. Delete which?",
                    view=_RecurringChoiceView(interaction.user.id, del_week, del_series))
                return

            async def on_confirm(ci: discord.Interaction):
                await ci.response.defer()
                await self._delete_event(ci.guild, unit_role_id, event_id, "series")
                await ci.edit_original_response(content=f"🗑️ Deleted **{title}**.", view=None)

            await inter.response.edit_message(
                content=f"Delete **{title}**? This can't be undone.",
                view=_DeleteConfirmView(interaction.user.id, on_confirm))

        await interaction.response.send_message(
            "Pick the event to delete:",
            view=_EventSelectView(events, interaction.user.id, on_pick),
            ephemeral=True)

    # ── /schedule edit ───────────────────────────────────────────────────────────

    @schedule_group.command(name="edit", description="Edit an event on a unit's schedule (guided in DMs).")
    @app_commands.describe(unit="The unit whose schedule you're editing.")
    @app_commands.autocomplete(unit=_linked_unit_autocomplete)
    async def cmd_edit(self, interaction: discord.Interaction, unit: str) -> None:
        unit_role_id = await self._guard_editor(interaction, unit)
        if unit_role_id is None:
            return
        events = await self._flat_events(interaction.guild.id, unit_role_id)
        if not events:
            await interaction.response.send_message("That unit has no events to edit.", ephemeral=True)
            return

        async def on_pick(inter: discord.Interaction, event_id: str):
            try:
                dm = await inter.user.create_dm()
                await dm.send(embed=discord.Embed(
                    title="✏️ Edit event",
                    description="I'll walk you through changing this event. Type `cancel` to stop.",
                    color=_DEFAULT_COLOR))
            except discord.Forbidden:
                await inter.response.edit_message(
                    content="❌ I couldn't DM you. Enable DMs for this server and try again.", view=None)
                return
            await inter.response.edit_message(content="📨 Check your DMs to edit the event.", view=None)
            try:
                await self._run_edit_flow(inter, dm, unit_role_id, event_id)
            except _FlowCancelled:
                await dm.send("❌ Edit cancelled.")
            except (asyncio.TimeoutError, TimeoutError):
                await dm.send("⏰ Timed out. Run `/schedule edit` again to restart.")
            except Exception as exc:  # noqa: BLE001
                await dm.send(f"⚠️ Something went wrong: `{type(exc).__name__}`.")
                raise

        await interaction.response.send_message(
            "Pick the event to edit:",
            view=_EventSelectView(events, interaction.user.id, on_pick),
            ephemeral=True)

    # ── Edit/delete shared helpers ────────────────────────────────────────────────

    async def _guard_editor(self, interaction: discord.Interaction, unit: str) -> Optional[int]:
        """Validate the unit selection and editor permission. Returns unit id or None (and replies)."""
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return None
        try:
            unit_role_id = int(unit)
        except (TypeError, ValueError):
            await interaction.response.send_message("Invalid unit selection.", ephemeral=True)
            return None
        config = await self._load_config(interaction.guild.id)
        if str(unit_role_id) not in config['links']:
            await interaction.response.send_message(
                "That unit isn't linked to a schedule channel.", ephemeral=True)
            return None
        if not await self._can_edit(interaction, unit_role_id):
            await interaction.response.send_message(
                "⛔ You don't have permission to edit this unit's schedule.", ephemeral=True)
            return None
        return unit_role_id

    async def _flat_events(self, guild_id: int, unit_role_id: int) -> List[Dict[str, Any]]:
        """
        Every stored event for a unit (each record once, carrying _id), so editors
        can manage recurring series and future-week one-offs — not just this week.
        Sorted by week, then weekday, then time-of-day.
        """
        config = await self._load_config(guild_id)
        order = {k: i for i, (k, _) in enumerate(_DAYS)}
        out: List[Dict[str, Any]] = []
        for ev_id, ev in config['events'].items():
            if ev.get("unit_role_id") != unit_role_id:
                continue
            out.append({**ev, "_id": ev_id})
        out.sort(key=lambda e: (e.get("week_start", ""), order.get(e.get("weekday"), 9),
                                e.get("tod_hour", 0) or 0, e.get("tod_minute", 0) or 0))
        return out

    async def _delete_event(self, guild: discord.Guild, unit_role_id: int,
                            event_id: str, scope: str) -> Optional[str]:
        """
        Delete an event. scope='series' removes the record; scope='week' on a
        recurring event just skips the current week. Returns the affected weekday.
        """
        config = await self._load_config(guild.id)
        stored = dict(config['events'])
        ev = stored.get(event_id)
        if not ev:
            return None
        day = ev.get("weekday")
        if scope == "week" and ev.get("recurring"):
            skips = list(ev.get("skip_weeks") or [])
            wk = _current_monday().isoformat()
            if wk not in skips:
                skips.append(wk)
            ev["skip_weeks"] = skips
            stored[event_id] = ev
        else:
            stored.pop(event_id, None)
        await self._save(guild.id, 'events', stored)
        await self._rebuild_day(guild, unit_role_id, day)
        return day

    async def _run_edit_flow(self, interaction: discord.Interaction, dm: discord.DMChannel,
                             unit_role_id: int, event_id: str) -> None:
        user_id = interaction.user.id
        config = await self._load_config(interaction.guild.id)
        events = dict(config['events'])
        original = events.get(event_id)
        if not original:
            await dm.send("⚠️ That event no longer exists.")
            return

        # Recurring events: choose scope before editing.
        override_origin: Optional[str] = None  # set when editing one occurrence
        if original.get("recurring"):
            scope = None
            while scope is None:
                r = await self._ask(dm, user_id, self._prompt(
                    "🔁 This event repeats weekly. Edit which?",
                    ["**1** This week only (creates a one-off override for this week)",
                     "**2** The whole series"]))
                t = r.content.strip().lower()
                if t in ("1", "this week", "week"):
                    scope = "week"
                elif t in ("2", "series", "whole", "all"):
                    scope = "series"
                else:
                    await dm.send("❌ Reply with 1 or 2.")
            if scope == "week":
                this_monday = _current_monday()
                work = {**original, "recurring": False,
                        "week_start": this_monday.isoformat(),
                        "rsvps": {"confirmed": [], "tentative": [], "declined": []}}
                work.pop("skip_weeks", None)
                work["start_utc"] = _event_start_ts(original, this_monday)
                override_origin = event_id
            else:
                work = {**original}
        else:
            work = {**original}

        old_day = work.get("weekday")
        tz_cfg = config['user_tz'].get(str(user_id))

        def base_monday(w: Dict[str, Any]) -> datetime.date:
            try:
                return datetime.date.fromisoformat(w.get("week_start"))
            except (TypeError, ValueError):
                return _current_monday()

        while True:
            menu = self._prompt("✏️ What would you like to change?", [
                f"**1** Title — *{work.get('title','')[:40]}*",
                "**2** Description",
                "**3** Day of week",
                "**4** Time",
                "**5** Image",
                "**6** Colour",
                f"**7** Recurring — *{'weekly' if work.get('recurring') else 'one-off'}*",
                "**8** Save & finish",
            ])
            msg = await self._ask(dm, user_id, menu)
            choice = msg.content.strip().lower()

            if choice in ("8", "save", "done", "finish"):
                break
            elif choice == "1":
                r = await self._ask(dm, user_id, self._prompt("✏️ New title", ["Up to 200 characters."]))
                if r.content.strip():
                    work["title"] = r.content.strip()[:200]
                    await dm.send("✅ Title updated.")
            elif choice == "2":
                r = await self._ask(dm, user_id, self._prompt("📝 New description", ["Type `none` to clear it."]))
                work["description"] = None if r.content.strip().lower() == "none" else r.content.strip()[:1600]
                await dm.send("✅ Description updated.")
            elif choice == "3":
                day_lines = [f"**{i+1}** {label.title()}" for i, (_, label) in enumerate(_DAYS)]
                r = await self._ask(dm, user_id, self._prompt("📆 New day", ["Reply with a number:", *day_lines]))
                t = r.content.strip().lower()
                new_idx = None
                if t.isdigit() and 1 <= int(t) <= 7:
                    new_idx = int(t) - 1
                else:
                    for i, (k, label) in enumerate(_DAYS):
                        if t in (k, label.lower()):
                            new_idx = i
                            break
                if new_idx is None:
                    await dm.send("❌ Invalid day; unchanged.")
                else:
                    work["weekday"] = _DAYS[new_idx][0]
                    tz = work.get("tz") or tz_cfg or "UTC"
                    h = work.get("tod_hour", 0) or 0
                    mn = work.get("tod_minute", 0) or 0
                    work["start_utc"] = _weekday_time_to_utc(base_monday(work), new_idx, h, mn, tz)
                    await dm.send(f"✅ Moved to {_DAYS[new_idx][1].title()}.")
            elif choice == "4":
                tz = work.get("tz") or tz_cfg or await self._ensure_timezone(dm, interaction)
                tz_cfg = tz
                r = await self._ask(dm, user_id, self._prompt(
                    "🕒 New time", [f"Your timezone: `{tz}`.", "Examples: `8pm`, `20:00`."]))
                parsed = _parse_time_of_day(r.content)
                if not parsed:
                    await dm.send("❌ Couldn't read that time; unchanged.")
                else:
                    work["tod_hour"], work["tod_minute"] = parsed
                    work["tz"] = tz
                    idx = _DAY_KEYS.index(work["weekday"])
                    work["start_utc"] = _weekday_time_to_utc(base_monday(work), idx, parsed[0], parsed[1], tz)
                    await dm.send("✅ Time updated.")
            elif choice == "5":
                r = await self._ask(dm, user_id, self._prompt(
                    "🖼️ New image", ["Attach an image, paste a URL, or type `none` to clear."]))
                if r.attachments:
                    work["image_url"] = r.attachments[0].url
                elif r.content.strip().lower() == "none":
                    work["image_url"] = None
                elif re.match(r"^https?://", r.content.strip()):
                    work["image_url"] = r.content.strip()
                else:
                    await dm.send("ℹ️ Not a valid image; unchanged.")
                    continue
                await dm.send("✅ Image updated.")
            elif choice == "6":
                r = await self._ask(dm, user_id, self._prompt("🎨 New colour", ["Hex like `#5865F2`, or `none`."]))
                if r.content.strip().lower() == "none":
                    work["color"] = None
                    await dm.send("✅ Colour cleared.")
                elif _hex_to_int(r.content):
                    work["color"] = "#" + r.content.strip().lstrip("#").upper()
                    await dm.send("✅ Colour updated.")
                else:
                    await dm.send("❌ Invalid colour; unchanged.")
            elif choice == "7":
                if override_origin is not None:
                    await dm.send("ℹ️ You're editing a single week; recurrence can only change on the whole series.")
                else:
                    work["recurring"] = not work.get("recurring")
                    if work["recurring"]:
                        work.setdefault("skip_weeks", [])
                    await dm.send(f"✅ Now **{'weekly' if work['recurring'] else 'one-off'}**.")
            else:
                await dm.send("❌ Reply with a number 1–8.")

        # Re-read at save time so we don't clobber concurrent RSVPs.
        fresh = await self._load_config(interaction.guild.id)
        stored = dict(fresh['events'])
        new_day = work.get("weekday")

        if override_origin is not None:
            # Skip this week on the series, add the edited one-off override.
            origin = stored.get(override_origin)
            if origin is not None:
                skips = list(origin.get("skip_weeks") or [])
                wk = _current_monday().isoformat()
                if wk not in skips:
                    skips.append(wk)
                origin["skip_weeks"] = skips
                stored[override_origin] = origin
            stored[uuid.uuid4().hex[:12]] = work
        else:
            if event_id in stored:
                work["rsvps"] = stored[event_id].get("rsvps", work.get("rsvps"))
            stored[event_id] = work

        await self._save(interaction.guild.id, 'events', stored)
        for d in {old_day, new_day}:
            if d:
                await self._rebuild_day(interaction.guild, unit_role_id, d)
        await dm.send(f"✅ Saved changes to **{work.get('title','event')}**.")

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

        marker = config.get("week_marker") or "—"
        embed.set_footer(text=f"Board week: {marker}  ·  weekly auto-refresh active")
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
