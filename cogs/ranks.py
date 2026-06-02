"""
Request-based promotion, demotion, and unit-removal commands that route through
an approval channel before any role change is applied.

Commands
    /promote [users] [rank] [reason]   request a promotion to a rank
    /demote  [user]  [rank] [reason]   request a demotion to a rank
    /remove  [users] [unit_role] [reason]   request removal from a unit
    /rankconfig ...                    configure the system (dev/admin only)

Workflow
    A command posts one request embed per target to the configured request
    channel, each with APPROVE / APPROVE W/ REASON / DENY / DENY W/ REASON
    buttons. Authorised approvers (developers, guild admins, or a configured
    approver role) act on each request independently. On approval the role
    change is applied; the requester is always DM'd the outcome, and the target
    is DM'd on approval.

Storage (all under the `ranks.` namespace in the guild_settings KV store)
    ranks.rank_order          list   ordered [{"name": str, "role_id": int}], low -> high
    ranks.additional_roles    dict   {rank_name: [role_id, ...]} granted alongside a rank
    ranks.runner_role_id      int    role allowed to run /promote /demote /remove
    ranks.approver_role_ids   list   roles allowed to approve/deny requests
    ranks.request_channel_id  int    channel requests are posted to
    ranks.request.<msg_id>    dict   a pending request record (see _new_request)

The Unit CO and Unit Leader identities are shared with the co_chat cog
(`co_chat.co_role_id`, `co_chat.leader_role_id`) rather than duplicated here:
removal strips Unit CO if held, and /remove's abuse gate uses the Unit Leader
role to verify the runner leads the unit named in the command.
"""
import time
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS


# Default ladder, low -> high. role_id is 0 until configured via /rankconfig.
_DEFAULT_RANK_NAMES = ["Corporal", "Sergeant", "Lieutenant", "Captain"]

# Storage keys (short name -> (full key, default))
_KEYS = {
    'rank_order':         ('ranks.rank_order',         []),
    'additional_roles':   ('ranks.additional_roles',   {}),
    'runner_role_id':     ('ranks.runner_role_id',     None),
    'approver_role_ids':  ('ranks.approver_role_ids',  []),
    'request_channel_id': ('ranks.request_channel_id', None),
}

_REQUEST_PREFIX = "ranks.request."


# ---------------------------------------------------------------------------
# Permission Helpers
# ---------------------------------------------------------------------------

async def _guild_management_role_id(interaction: discord.Interaction) -> Optional[int]:
    config = await interaction.client.db.get_guild_config(interaction.guild.id)
    if config:
        return config.get("management_role_id")
    return None


async def _is_dev_or_admin(interaction: discord.Interaction) -> bool:
    """Developers and guild administrators — the always-allowed baseline."""
    if interaction.user.id in DEVELOPER_IDS:
        return True
    if interaction.guild is None:
        return False
    return interaction.user.guild_permissions.administrator


def _has_role(member: discord.Member, role_id: Optional[int]) -> bool:
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)


def _is_dev_or_admin_user(member: discord.Member, bot: commands.Bot) -> bool:
    """Dev/admin check against a Member object (not an Interaction)."""
    if member.id in DEVELOPER_IDS:
        return True
    return member.guild_permissions.administrator


# ---------------------------------------------------------------------------
# Storage Helpers (module-level so views can use them without a cog ref)
# ---------------------------------------------------------------------------

async def _load_config(bot: commands.Bot, guild_id: int) -> Dict[str, Any]:
    raw = await bot.db.get_guild_settings(guild_id)
    out: Dict[str, Any] = {}
    for short_name, (full_key, default) in _KEYS.items():
        out[short_name] = raw.get(full_key, default)
    return out


async def _save(bot: commands.Bot, guild_id: int, short_name: str, value: Any) -> None:
    await bot.db.set_guild_setting(guild_id, _KEYS[short_name][0], value)


async def _get_request(bot: commands.Bot, guild_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    return await bot.db.get_guild_setting(guild_id, f"{_REQUEST_PREFIX}{message_id}")


async def _save_request(bot: commands.Bot, guild_id: int, message_id: int, record: Dict[str, Any]) -> None:
    await bot.db.set_guild_setting(guild_id, f"{_REQUEST_PREFIX}{message_id}", record)


async def _delete_request(bot: commands.Bot, guild_id: int, message_id: int) -> None:
    await bot.db.delete_guild_setting(guild_id, f"{_REQUEST_PREFIX}{message_id}")


# ---------------------------------------------------------------------------
# Rank Ladder Utilities
# ---------------------------------------------------------------------------

def _rank_names(config: Dict[str, Any]) -> List[str]:
    return [r["name"] for r in config['rank_order']]


def _rank_role_id(config: Dict[str, Any], rank_name: str) -> Optional[int]:
    for r in config['rank_order']:
        if r["name"] == rank_name:
            return r.get("role_id") or None
    return None


def _all_rank_role_ids(config: Dict[str, Any]) -> List[int]:
    return [r["role_id"] for r in config['rank_order'] if r.get("role_id")]


def _additional_role_ids(config: Dict[str, Any], rank_name: str) -> List[int]:
    return [int(x) for x in config['additional_roles'].get(rank_name, []) if x]


# ---------------------------------------------------------------------------
# DM Helper
# ---------------------------------------------------------------------------

async def _safe_dm(bot: commands.Bot, user_id: int, embed: discord.Embed) -> bool:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        return False


# ---------------------------------------------------------------------------
# Request Record
# ---------------------------------------------------------------------------

def _new_request(
    *,
    req_type: str,             # "promote" | "demote" | "remove"
    guild_id: int,
    requester_id: int,
    target_id: int,
    reason: str,
    rank_name: Optional[str] = None,   # promote/demote
    unit_role_id: Optional[int] = None,  # remove
) -> Dict[str, Any]:
    return {
        "type": req_type,
        "guild_id": guild_id,
        "requester_id": requester_id,
        "target_id": target_id,
        "reason": reason,
        "rank_name": rank_name,
        "unit_role_id": unit_role_id,
        "status": "pending",       # pending | approved | denied
        "created_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Role Application — the actual rank/membership mutation on approval
# ---------------------------------------------------------------------------

async def _apply_decision(
    bot: commands.Bot,
    guild: discord.Guild,
    record: Dict[str, Any],
) -> str:
    """
    Apply the approved role change. Returns a short human-readable result line
    used in the result embeds. Raises nothing — failures are returned as text.
    """
    member = guild.get_member(record["target_id"])
    if member is None:
        return "⚠️ Target is no longer in the server; no changes applied."

    config = await _load_config(bot, guild.id)
    req_type = record["type"]

    try:
        if req_type in ("promote", "demote"):
            rank_name = record["rank_name"]
            new_rank_role_id = _rank_role_id(config, rank_name)
            if not new_rank_role_id:
                return f"⚠️ Rank '{rank_name}' has no role configured; no changes applied."
            new_rank_role = guild.get_role(new_rank_role_id)
            if new_rank_role is None:
                return f"⚠️ Configured role for '{rank_name}' no longer exists; no changes applied."

            # Strip every *other* configured rank role, add the target rank role.
            all_rank_ids = set(_all_rank_role_ids(config))
            to_remove = [r for r in member.roles if r.id in all_rank_ids and r.id != new_rank_role_id]

            # Additional roles for this rank (kept if already held; added if not).
            add_roles = [new_rank_role]
            for rid in _additional_role_ids(config, rank_name):
                role = guild.get_role(rid)
                if role and role not in member.roles:
                    add_roles.append(role)

            if to_remove:
                await member.remove_roles(*to_remove, reason=f"Rank {req_type}: {rank_name}")
            await member.add_roles(*add_roles, reason=f"Rank {req_type}: {rank_name}")
            return f"✅ {member.mention} is now **{rank_name}**."

        elif req_type == "remove":
            unit_role_id = record["unit_role_id"]
            unit_role = guild.get_role(unit_role_id) if unit_role_id else None
            if unit_role is None:
                return "⚠️ Unit role no longer exists; no changes applied."

            to_strip = []
            if unit_role in member.roles:
                to_strip.append(unit_role)

            # Strip Unit CO if held (shared identity from co_chat).
            co_role_id = await bot.db.get_guild_setting(guild.id, "co_chat.co_role_id")
            if co_role_id:
                co_role = guild.get_role(co_role_id)
                if co_role and co_role in member.roles:
                    to_strip.append(co_role)

            if to_strip:
                await member.remove_roles(*to_strip, reason="Unit removal")
            stripped = ", ".join(r.name for r in to_strip) if to_strip else "nothing (roles not held)"
            return f"✅ {member.mention} removed from **{unit_role.name}** (stripped: {stripped})."

        return "⚠️ Unknown request type; no changes applied."

    except discord.Forbidden:
        return "⚠️ I lack permission to modify this member's roles (check role hierarchy)."
    except discord.HTTPException as exc:
        return f"⚠️ Discord error applying change: {exc}"


# ---------------------------------------------------------------------------
# Embed Builders
# ---------------------------------------------------------------------------

_TYPE_TITLES = {
    "promote": "Promotion Request",
    "demote":  "Demotion Request",
    "remove":  "Unit Removal Request",
}
_TYPE_COLORS = {
    "promote": 0x57F287,
    "demote":  0xFEE75C,
    "remove":  0xED4245,
}


def _request_embed(guild: discord.Guild, record: Dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=_TYPE_TITLES.get(record["type"], "Request"),
        color=_TYPE_COLORS.get(record["type"], 0x5865F2),
    )
    target = guild.get_member(record["target_id"])
    embed.add_field(name="Target", value=target.mention if target else f"`{record['target_id']}`", inline=True)
    requester = guild.get_member(record["requester_id"])
    embed.add_field(name="Requested by", value=requester.mention if requester else f"`{record['requester_id']}`", inline=True)

    if record["type"] in ("promote", "demote"):
        embed.add_field(name="Rank", value=record["rank_name"] or "—", inline=True)
    elif record["type"] == "remove":
        unit_role = guild.get_role(record["unit_role_id"]) if record.get("unit_role_id") else None
        embed.add_field(name="Unit", value=unit_role.mention if unit_role else f"`{record.get('unit_role_id')}`", inline=True)

    embed.add_field(name="Reason", value=record["reason"][:1024], inline=False)
    embed.set_footer(text="Pending — awaiting an authorised approver.")
    return embed


def _result_embed(guild: discord.Guild, record: Dict[str, Any], decided_by: discord.abc.User,
                  approved: bool, decision_reason: Optional[str], outcome: Optional[str]) -> discord.Embed:
    embed = discord.Embed(
        title=f"{_TYPE_TITLES.get(record['type'], 'Request')} — {'Approved' if approved else 'Denied'}",
        color=0x57F287 if approved else 0xED4245,
    )
    target = guild.get_member(record["target_id"])
    embed.add_field(name="Target", value=target.mention if target else f"`{record['target_id']}`", inline=True)
    if record["type"] in ("promote", "demote"):
        embed.add_field(name="Rank", value=record["rank_name"] or "—", inline=True)
    elif record["type"] == "remove":
        unit_role = guild.get_role(record["unit_role_id"]) if record.get("unit_role_id") else None
        embed.add_field(name="Unit", value=unit_role.mention if unit_role else f"`{record.get('unit_role_id')}`", inline=True)
    embed.add_field(name="Original reason", value=record["reason"][:1024], inline=False)
    embed.add_field(name=f"{'Approved' if approved else 'Denied'} by", value=decided_by.mention, inline=True)
    if decision_reason:
        embed.add_field(name="Decision note", value=decision_reason[:1024], inline=False)
    if approved and outcome:
        embed.add_field(name="Result", value=outcome, inline=False)
    return embed


# ---------------------------------------------------------------------------
# Reason Modal (used by APPROVE W/ REASON and DENY W/ REASON)
# ---------------------------------------------------------------------------

class ReasonModal(discord.ui.Modal):
    def __init__(self, view: "ApprovalView", approve: bool):
        super().__init__(title="Approve with reason" if approve else "Deny with reason")
        self.view = view
        self.approve = approve
        self.reason_input = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.view._finalise(interaction, self.approve, str(self.reason_input.value))


# ---------------------------------------------------------------------------
# Approval view — one persistent instance handles every pending request.
# The request is resolved from the message the buttons are attached to, so
# custom_ids are static and the view survives restarts.
# ---------------------------------------------------------------------------

class ApprovalView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _is_approver(self, interaction: discord.Interaction) -> bool:
        if await _is_dev_or_admin(interaction):
            return True
        config = await _load_config(self.bot, interaction.guild.id)
        approver_ids = set(config['approver_role_ids'])
        if not approver_ids:
            return False
        return any(r.id in approver_ids for r in interaction.user.roles)

    async def _load_record_or_close(self, interaction: discord.Interaction) -> Optional[Dict[str, Any]]:
        record = await _get_request(self.bot, interaction.guild.id, interaction.message.id)
        if record is None or record.get("status") != "pending":
            # Stale/orphaned message — disable the buttons so it can't be reused.
            try:
                await interaction.response.edit_message(
                    content="This request is no longer pending.", view=None
                )
            except discord.InteractionResponded:
                pass
            return None
        return record

    async def _finalise(self, interaction: discord.Interaction, approved: bool,
                        decision_reason: Optional[str]) -> None:
        record = await _get_request(self.bot, interaction.guild.id, interaction.message.id)
        if record is None or record.get("status") != "pending":
            msg = "This request is no longer pending."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        guild = interaction.guild
        outcome = None
        if approved:
            outcome = await _apply_decision(self.bot, guild, record)

        record["status"] = "approved" if approved else "denied"

        # Update the request message in-place with the result, buttons removed.
        result_embed = _result_embed(guild, record, interaction.user, approved, decision_reason, outcome)
        if interaction.response.is_done():
            await interaction.message.edit(embed=result_embed, view=None)
        else:
            await interaction.response.edit_message(embed=result_embed, view=None)

        # DM the requester (always).
        req_dm = _result_embed(guild, record, interaction.user, approved, decision_reason, outcome)
        req_dm.title = f"Your {record['type']} request was {'approved' if approved else 'denied'}"
        await _safe_dm(self.bot, record["requester_id"], req_dm)

        # DM the target on every decided outcome (approve or deny), per spec.
        target_dm = discord.Embed(
            title=f"{_TYPE_TITLES.get(record['type'], 'Request')} — {'Approved' if approved else 'Denied'}",
            color=0x57F287 if approved else 0xED4245,
        )
        if record["type"] in ("promote", "demote"):
            target_dm.add_field(name="Rank", value=record["rank_name"] or "—", inline=True)
        elif record["type"] == "remove":
            unit_role = guild.get_role(record["unit_role_id"]) if record.get("unit_role_id") else None
            target_dm.add_field(name="Unit", value=unit_role.name if unit_role else "—", inline=True)
        target_dm.add_field(name="Server", value=guild.name, inline=True)
        if decision_reason:
            target_dm.add_field(name="Reason", value=decision_reason[:1024], inline=False)
        if approved and outcome:
            target_dm.add_field(name="Result", value=outcome, inline=False)
        await _safe_dm(self.bot, record["target_id"], target_dm)

        # Request resolved — drop the record.
        await _delete_request(self.bot, guild.id, interaction.message.id)

    # ── Buttons ────────────────────────────────────────────────────────────

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="ranks:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_approver(interaction):
            await interaction.response.send_message("You aren't authorised to act on this request.", ephemeral=True)
            return
        if await self._load_record_or_close(interaction) is None:
            return
        await self._finalise(interaction, approved=True, decision_reason=None)

    @discord.ui.button(label="Approve w/ Reason", style=discord.ButtonStyle.success, custom_id="ranks:approve_reason")
    async def approve_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_approver(interaction):
            await interaction.response.send_message("You aren't authorised to act on this request.", ephemeral=True)
            return
        record = await _get_request(self.bot, interaction.guild.id, interaction.message.id)
        if record is None or record.get("status") != "pending":
            await interaction.response.send_message("This request is no longer pending.", ephemeral=True)
            return
        await interaction.response.send_modal(ReasonModal(self, approve=True))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="ranks:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_approver(interaction):
            await interaction.response.send_message("You aren't authorised to act on this request.", ephemeral=True)
            return
        if await self._load_record_or_close(interaction) is None:
            return
        await self._finalise(interaction, approved=False, decision_reason=None)

    @discord.ui.button(label="Deny w/ Reason", style=discord.ButtonStyle.danger, custom_id="ranks:deny_reason")
    async def deny_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_approver(interaction):
            await interaction.response.send_message("You aren't authorised to act on this request.", ephemeral=True)
            return
        record = await _get_request(self.bot, interaction.guild.id, interaction.message.id)
        if record is None or record.get("status") != "pending":
            await interaction.response.send_message("This request is no longer pending.", ephemeral=True)
            return
        await interaction.response.send_modal(ReasonModal(self, approve=False))


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class RanksCog(commands.Cog, name="Ranks"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Register the persistent approval view so buttons work after a restart.
        self.bot.add_view(ApprovalView(self.bot))

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                     error: app_commands.AppCommandError):
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

    # ── Shared gating ────────────────────────────────────────────────────────

    async def _can_run(self, interaction: discord.Interaction) -> bool:
        if await _is_dev_or_admin(interaction):
            return True
        config = await _load_config(self.bot, interaction.guild.id)
        runner_id = config['runner_role_id']
        return _has_role(interaction.user, runner_id)

    async def _rank_autocomplete(self, interaction: discord.Interaction, current: str):
        config = await _load_config(self.bot, interaction.guild.id)
        names = _rank_names(config)
        return [
            app_commands.Choice(name=n, value=n)
            for n in names if current.lower() in n.lower()
        ][:25]

    # ── Submission / fan-out ───────────────────────────────────────────────────

    async def _post_request(self, guild: discord.Guild, record: Dict[str, Any]) -> Optional[int]:
        """Post one request embed to the configured channel. Returns message id or None."""
        config = await _load_config(self.bot, guild.id)
        channel_id = config['request_channel_id']
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            return None
        embed = _request_embed(guild, record)
        msg = await channel.send(embed=embed, view=ApprovalView(self.bot))
        await _save_request(self.bot, guild.id, msg.id, record)
        return msg.id

    async def _submit_targets(
        self,
        interaction: discord.Interaction,
        req_type: str,
        targets: List[discord.Member],
        reason: str,
        *,
        rank_name: Optional[str] = None,
        unit_role_id: Optional[int] = None,
    ) -> None:
        config = await _load_config(self.bot, interaction.guild.id)
        if not config['request_channel_id'] or interaction.guild.get_channel(config['request_channel_id']) is None:
            await interaction.followup.send(
                "⚠️ No valid request channel configured. Set one with `/rankconfig set-channel`.",
                ephemeral=True,
            )
            return

        posted, failed = 0, 0
        for target in targets:
            record = _new_request(
                req_type=req_type,
                guild_id=interaction.guild.id,
                requester_id=interaction.user.id,
                target_id=target.id,
                reason=reason,
                rank_name=rank_name,
                unit_role_id=unit_role_id,
            )
            msg_id = await self._post_request(interaction.guild, record)
            if msg_id:
                posted += 1
            else:
                failed += 1

        summary = f"✅ Submitted {posted} request(s)."
        if failed:
            summary += f" {failed} failed to post."
        if len(targets) > 1:
            summary += " Each target is a separate request, all sharing your reason."
        await interaction.followup.send(summary, ephemeral=True)

    # ── /promote ───────────────────────────────────────────────────────────────

    @app_commands.command(name="promote", description="Request a promotion for one or more members.")
    @app_commands.describe(
        users="Member(s) to promote. Mention one or more.",
        rank="Target rank.",
        reason="Reason for the promotion.",
    )
    @app_commands.autocomplete(rank=_rank_autocomplete)
    @app_commands.guild_only()
    async def promote(self, interaction: discord.Interaction, users: str, rank: str, reason: str):
        if not await self._can_run(interaction):
            await interaction.response.send_message("⛔ You aren't authorised to run this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await _load_config(self.bot, interaction.guild.id)
        if rank not in _rank_names(config):
            await interaction.followup.send(f"⚠️ '{rank}' is not a configured rank.", ephemeral=True)
            return
        members = await self._resolve_members(interaction, users)
        if not members:
            await interaction.followup.send("⚠️ No valid members found in `users`.", ephemeral=True)
            return
        await self._submit_targets(interaction, "promote", members, reason, rank_name=rank)

    # ── /demote ──────────────────────────────────────────────────────────────

    @app_commands.command(name="demote", description="Request a demotion for a member.")
    @app_commands.describe(
        user="Member to demote.",
        rank="Target (lower) rank.",
        reason="Reason for the demotion.",
    )
    @app_commands.autocomplete(rank=_rank_autocomplete)
    @app_commands.guild_only()
    async def demote(self, interaction: discord.Interaction, user: discord.Member, rank: str, reason: str):
        if not await self._can_run(interaction):
            await interaction.response.send_message("⛔ You aren't authorised to run this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await _load_config(self.bot, interaction.guild.id)
        if rank not in _rank_names(config):
            await interaction.followup.send(f"⚠️ '{rank}' is not a configured rank.", ephemeral=True)
            return
        await self._submit_targets(interaction, "demote", [user], reason, rank_name=rank)

    # ── /remove ──────────────────────────────────────────────────────────────

    @app_commands.command(name="remove", description="Request removal of member(s) from a unit you lead.")
    @app_commands.describe(
        users="Member(s) to remove. Mention one or more.",
        unit_role="The unit role to remove them from. You must be its Unit Leader.",
        reason="Reason for removal.",
    )
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, users: str,
                     unit_role: discord.Role, reason: str):
        if not await self._can_run(interaction):
            await interaction.response.send_message("⛔ You aren't authorised to run this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Abuse gate: the runner must be a Unit Leader AND hold the named unit role.
        leader_role_id = await self.bot.db.get_guild_setting(interaction.guild.id, "co_chat.leader_role_id")
        if not leader_role_id:
            await interaction.followup.send(
                "⚠️ No Unit Leader role is configured (set it with `/cochat set-leader-role`). "
                "Removal is locked until then.",
                ephemeral=True,
            )
            return
        if not _is_dev_or_admin_user(interaction.user, self.bot):
            if not _has_role(interaction.user, leader_role_id):
                await interaction.followup.send(
                    "⛔ Only a Unit Leader can remove members.", ephemeral=True
                )
                return
            if not _has_role(interaction.user, unit_role.id):
                await interaction.followup.send(
                    f"⛔ You can only remove members from a unit you belong to. "
                    f"You don't hold {unit_role.mention}.",
                    ephemeral=True,
                )
                return

        members = await self._resolve_members(interaction, users)
        if not members:
            await interaction.followup.send("⚠️ No valid members found in `users`.", ephemeral=True)
            return

        # Every target must actually be in the named unit.
        in_unit = [m for m in members if _has_role(m, unit_role.id)]
        not_in_unit = [m for m in members if not _has_role(m, unit_role.id)]
        if not in_unit:
            await interaction.followup.send(
                f"⚠️ None of the targets hold {unit_role.mention}; nothing to remove.",
                ephemeral=True,
            )
            return

        await self._submit_targets(interaction, "remove", in_unit, reason, unit_role_id=unit_role.id)
        if not_in_unit:
            skipped = ", ".join(m.mention for m in not_in_unit)
            await interaction.followup.send(
                f"ℹ️ Skipped (not in {unit_role.mention}): {skipped}", ephemeral=True
            )

    # ── Member resolution helper ───────────────────────────────────────────────

    async def _resolve_members(self, interaction: discord.Interaction, raw: str) -> List[discord.Member]:
        """Parse mentions / raw IDs from the users string into unique guild members."""
        import re
        ids = set(int(x) for x in re.findall(r"(\d{15,25})", raw))
        members: List[discord.Member] = []
        seen = set()
        for uid in ids:
            if uid in seen:
                continue
            m = interaction.guild.get_member(uid)
            if m is not None:
                members.append(m)
                seen.add(uid)
        return members


# ---------------------------------------------------------------------------
# /rankconfig group  (developer / guild-admin only)
# ---------------------------------------------------------------------------

class RankConfigCog(commands.Cog, name="RankConfig"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(
        name="rankconfig",
        description="Configure the rank & membership system",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                     error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ You do not have permission to use that command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        raise error

    def _check(self, interaction: discord.Interaction) -> bool:
        return _is_dev_or_admin_user(interaction.user, self.bot)

    @group.command(name="seed-defaults", description="Seed the default rank ladder (Corporal→Sergeant→Lieutenant→Captain).")
    async def seed_defaults(self, interaction: discord.Interaction):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        if config['rank_order']:
            await interaction.response.send_message(
                "⚠️ A rank order already exists. Use `/rankconfig set-rank-role` to fill in roles, "
                "or `/rankconfig clear-ranks` first.", ephemeral=True)
            return
        order = [{"name": n, "role_id": 0} for n in _DEFAULT_RANK_NAMES]
        await _save(self.bot, interaction.guild.id, 'rank_order', order)
        await interaction.response.send_message(
            "✅ Seeded default ladder: " + " → ".join(_DEFAULT_RANK_NAMES) +
            "\nNow assign each a role with `/rankconfig set-rank-role`.", ephemeral=True)

    @group.command(name="set-rank-role", description="Map a rank name to its Discord role.")
    @app_commands.describe(rank="Rank name (must be in the ladder).", role="Role to assign for this rank.")
    async def set_rank_role(self, interaction: discord.Interaction, rank: str, role: discord.Role):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        order = config['rank_order']
        found = False
        for r in order:
            if r["name"].lower() == rank.lower():
                r["role_id"] = role.id
                found = True
                break
        if not found:
            await interaction.response.send_message(
                f"⚠️ '{rank}' isn't in the ladder. Add it with `/rankconfig add-rank`.", ephemeral=True)
            return
        await _save(self.bot, interaction.guild.id, 'rank_order', order)
        await interaction.response.send_message(f"✅ '{rank}' → {role.mention}.", ephemeral=True)

    @group.command(name="add-rank", description="Append a rank to the top of the ladder.")
    @app_commands.describe(name="Rank name.", role="Role for this rank.")
    async def add_rank(self, interaction: discord.Interaction, name: str, role: discord.Role):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        order = config['rank_order']
        if any(r["name"].lower() == name.lower() for r in order):
            await interaction.response.send_message(f"⚠️ '{name}' already exists.", ephemeral=True)
            return
        order.append({"name": name, "role_id": role.id})
        await _save(self.bot, interaction.guild.id, 'rank_order', order)
        await interaction.response.send_message(
            f"✅ Added '{name}' → {role.mention} at the top of the ladder.", ephemeral=True)

    @group.command(name="remove-rank", description="Remove a rank from the ladder.")
    @app_commands.describe(name="Rank name to remove.")
    async def remove_rank(self, interaction: discord.Interaction, name: str):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        order = [r for r in config['rank_order'] if r["name"].lower() != name.lower()]
        if len(order) == len(config['rank_order']):
            await interaction.response.send_message(f"⚠️ '{name}' not found.", ephemeral=True)
            return
        await _save(self.bot, interaction.guild.id, 'rank_order', order)
        # also drop additional-role config for it
        addl = config['additional_roles']
        addl.pop(name, None)
        await _save(self.bot, interaction.guild.id, 'additional_roles', addl)
        await interaction.response.send_message(f"✅ Removed '{name}'.", ephemeral=True)

    @group.command(name="set-order", description="Set the full rank order, low→high, by comma-separated names.")
    @app_commands.describe(order="Comma-separated rank names, lowest first. e.g. Corporal, Sergeant, Lieutenant, Captain")
    async def set_order(self, interaction: discord.Interaction, order: str):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        existing = {r["name"].lower(): r for r in config['rank_order']}
        names = [n.strip() for n in order.split(",") if n.strip()]
        if not names:
            await interaction.response.send_message("⚠️ Provide at least one rank name.", ephemeral=True)
            return
        new_order = []
        for n in names:
            if n.lower() in existing:
                new_order.append(existing[n.lower()])
            else:
                new_order.append({"name": n, "role_id": 0})
        await _save(self.bot, interaction.guild.id, 'rank_order', new_order)
        await interaction.response.send_message(
            "✅ Order set: " + " → ".join(r["name"] for r in new_order) +
            "\n(Any new names need a role via `/rankconfig set-rank-role`.)", ephemeral=True)

    @group.command(name="set-additional-roles", description="Set roles granted alongside a rank (comma-separated mentions/IDs).")
    @app_commands.describe(rank="Rank name.", roles="Comma-separated role mentions or IDs. Empty to clear.")
    async def set_additional_roles(self, interaction: discord.Interaction, rank: str, roles: str = ""):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        import re
        config = await _load_config(self.bot, interaction.guild.id)
        if rank not in _rank_names(config):
            await interaction.response.send_message(f"⚠️ '{rank}' is not a configured rank.", ephemeral=True)
            return
        ids = [int(x) for x in re.findall(r"(\d{15,25})", roles)]
        addl = config['additional_roles']
        addl[rank] = ids
        await _save(self.bot, interaction.guild.id, 'additional_roles', addl)
        if ids:
            mentions = ", ".join(f"<@&{i}>" for i in ids)
            await interaction.response.send_message(f"✅ '{rank}' also grants: {mentions}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Cleared additional roles for '{rank}'.", ephemeral=True)

    @group.command(name="default-additional-co", description="Set every rank's additional role to the Unit CO role (the default).")
    async def default_additional_co(self, interaction: discord.Interaction):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        co_role_id = await self.bot.db.get_guild_setting(interaction.guild.id, "co_chat.co_role_id")
        if not co_role_id:
            await interaction.response.send_message(
                "⚠️ No Unit CO role configured (set it with `/cochat set-co-role`).", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        addl = config['additional_roles']
        for name in _rank_names(config):
            addl[name] = [co_role_id]
        await _save(self.bot, interaction.guild.id, 'additional_roles', addl)
        await interaction.response.send_message(
            f"✅ Every rank now also grants <@&{co_role_id}> on promotion/demotion.", ephemeral=True)

    @group.command(name="set-runner-role", description="Set the role allowed to run /promote /demote /remove.")
    @app_commands.describe(role="Role allowed to run the commands. Pass nothing to clear.")
    async def set_runner_role(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        await _save(self.bot, interaction.guild.id, 'runner_role_id', role.id if role else None)
        await interaction.response.send_message(
            f"✅ Runner role set to {role.mention}." if role else "✅ Runner role cleared (devs/admins only).",
            ephemeral=True)

    @group.command(name="add-approver-role", description="Add a role allowed to approve/deny requests.")
    @app_commands.describe(role="Approver role.")
    async def add_approver_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        ids = list(config['approver_role_ids'])
        if role.id in ids:
            await interaction.response.send_message(f"{role.mention} is already an approver.", ephemeral=True)
            return
        ids.append(role.id)
        await _save(self.bot, interaction.guild.id, 'approver_role_ids', ids)
        await interaction.response.send_message(f"✅ Added {role.mention} as an approver.", ephemeral=True)

    @group.command(name="remove-approver-role", description="Remove a role from the approver list.")
    @app_commands.describe(role="Approver role to remove.")
    async def remove_approver_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        config = await _load_config(self.bot, interaction.guild.id)
        ids = [i for i in config['approver_role_ids'] if i != role.id]
        await _save(self.bot, interaction.guild.id, 'approver_role_ids', ids)
        await interaction.response.send_message(f"✅ Removed {role.mention} from approvers.", ephemeral=True)

    @group.command(name="set-channel", description="Set the channel that promote/demote/remove requests are sent to.")
    @app_commands.describe(channel="The request/approval channel.")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        await _save(self.bot, interaction.guild.id, 'request_channel_id', channel.id)
        await interaction.response.send_message(f"✅ Requests will be posted to {channel.mention}.", ephemeral=True)

    @group.command(name="status", description="Show the current rank system configuration.")
    async def status(self, interaction: discord.Interaction):
        if not self._check(interaction):
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return
        guild = interaction.guild
        config = await _load_config(self.bot, guild.id)
        embed = discord.Embed(title="Rank System Configuration", color=0x5865F2)

        if config['rank_order']:
            lines = []
            for i, r in enumerate(config['rank_order'], 1):
                role = guild.get_role(r.get("role_id")) if r.get("role_id") else None
                role_label = role.mention if role else "*no role*"
                addl = _additional_role_ids(config, r["name"])
                addl_label = (" + " + ", ".join(f"<@&{a}>" for a in addl)) if addl else ""
                lines.append(f"{i}. **{r['name']}** → {role_label}{addl_label}")
            embed.add_field(name="Ladder (low → high)", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Ladder", value="Not configured — run `/rankconfig seed-defaults`.", inline=False)

        runner = guild.get_role(config['runner_role_id']) if config['runner_role_id'] else None
        embed.add_field(name="Runner role", value=runner.mention if runner else "Devs/admins only", inline=True)

        if config['approver_role_ids']:
            embed.add_field(name="Approvers", value=", ".join(f"<@&{i}>" for i in config['approver_role_ids']), inline=True)
        else:
            embed.add_field(name="Approvers", value="Devs/admins only", inline=True)

        chan = guild.get_channel(config['request_channel_id']) if config['request_channel_id'] else None
        embed.add_field(name="Request channel", value=chan.mention if chan else "Not set", inline=True)

        leader_id = await self.bot.db.get_guild_setting(guild.id, "co_chat.leader_role_id")
        embed.add_field(
            name="Unit Leader role (for /remove)",
            value=f"<@&{leader_id}>" if leader_id else "Not set — `/cochat set-leader-role`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RanksCog(bot))
    await bot.add_cog(RankConfigCog(bot))
