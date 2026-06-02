"""
Panel cog — self-role panels, ticket panels, ticket lifecycle management.

Ticket statuses:  open → closing → closed
                         └─(reopen)─┘
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

CLOSE_DELAY = 3600  # seconds (60 minutes)


async def user_is_staff(interaction: discord.Interaction) -> bool:
    """True if the user holds the management or staff role."""
    config = await interaction.client.db.get_guild_config(interaction.guild.id)
    if not config:
        return interaction.user.guild_permissions.administrator
    for key in ("management_role_id", "staff_role_id"):
        rid = config.get(key)
        if rid:
            role = interaction.guild.get_role(rid)
            if role and role in interaction.user.roles:
                return True
    return False


async def management_check(interaction: discord.Interaction) -> bool:
    """True if the user is a developer or holds the management role."""
    if interaction.user.id in DEVELOPER_IDS:
        return True
    config = await interaction.client.db.get_guild_config(interaction.guild.id)
    if not config or not config.get("management_role_id"):
        return interaction.user.guild_permissions.administrator
    role = interaction.guild.get_role(config["management_role_id"])
    return role in interaction.user.roles if role else interaction.user.guild_permissions.administrator


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT VIEW — Self-role
# ─────────────────────────────────────────────────────────────────────────────

class SelfRoleButton(discord.ui.Button):
    def __init__(self, panel_id: int, role_id: int, label: str):
        super().__init__(
            label=label[:80],
            custom_id=f"sr_{panel_id}_{role_id}",
            style=discord.ButtonStyle.primary,
        )
        self.panel_id = panel_id
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        panel = await interaction.client.db.get_self_role_panel(self.panel_id)
        if not panel or not panel["enabled"]:
            return await interaction.response.send_message(
                "This panel is currently disabled.", ephemeral=True
            )
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message(
                "This role no longer exists.", ephemeral=True
            )
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Self-role panel")
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await interaction.user.add_roles(role, reason="Self-role panel")
                await interaction.response.send_message(f"Added **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to manage that role.", ephemeral=True
            )


class SelfRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT VIEW — Ticket panel buttons (open a ticket)
# ─────────────────────────────────────────────────────────────────────────────

class TicketButton(discord.ui.Button):
    def __init__(self, panel_id: int, category: str, label: str, index: int):
        STYLES = {
            "caleb": discord.ButtonStyle.danger,
            "support": discord.ButtonStyle.primary,
            "general": discord.ButtonStyle.secondary,
            "join": discord.ButtonStyle.success,
        }
        super().__init__(
            label=label[:80],
            custom_id=f"tk_{panel_id}_{category}_{index}",
            style=STYLES.get(category.lower(), discord.ButtonStyle.primary),
        )
        self.panel_id = panel_id
        self.category = category.lower()

    async def callback(self, interaction: discord.Interaction):
        panel = await interaction.client.db.get_ticket_panel(self.panel_id)
        if not panel or not panel["enabled"]:
            return await interaction.response.send_message(
                "This ticket panel is currently disabled.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        config = await interaction.client.db.get_guild_config(interaction.guild.id)
        role: Optional[discord.Role] = None
        if config:
            if self.category in ("caleb", "join") and config.get("management_role_id"):
                role = interaction.guild.get_role(config["management_role_id"])
            elif self.category in ("support", "general") and config.get("staff_role_id"):
                role = interaction.guild.get_role(config["staff_role_id"])

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                manage_channels=True, read_message_history=True
            ),
        }
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        CATEGORY_NAMES = {
            "caleb": "Tickets — Caleb",
            "support": "Tickets — Support",
            "general": "Tickets — General",
            "join": "Tickets — Join the Team",
        }
        cat_name = CATEGORY_NAMES.get(self.category, f"Tickets — {self.category.title()}")
        discord_cat = discord.utils.get(interaction.guild.categories, name=cat_name)
        if not discord_cat:
            try:
                discord_cat = await interaction.guild.create_category(cat_name)
            except discord.Forbidden:
                return await interaction.followup.send(
                    "I don't have permission to create a category.", ephemeral=True
                )

        ch_name = f"ticket-{interaction.user.name}"[:100]
        try:
            channel = await discord_cat.create_text_channel(ch_name, overwrites=overwrites)
        except discord.Forbidden:
            return await interaction.followup.send(
                "I don't have permission to create a ticket channel.", ephemeral=True
            )

        ticket_id = await interaction.client.db.create_ticket(
            interaction.guild.id, channel.id, interaction.user.id,
            self.panel_id, self.category
        )

        embed = discord.Embed(
            title=f"Ticket #{ticket_id}",
            description=(
                f"Welcome {interaction.user.mention}!\n"
                "Please describe your issue and a staff member will be with you shortly."
            ),
            color=0x5865F2,
        )
        embed.add_field(name="Category", value=self.category.title(), inline=True)
        embed.add_field(name="Opened By", value=interaction.user.mention, inline=True)
        embed.set_footer(text=f"Ticket ID: {ticket_id}")

        mentions = interaction.user.mention
        msg = await channel.send(
            content=mentions, embed=embed, view=TicketControlView()
        )
        # Persist the control message ID so we can update it later
        await interaction.client.db.update_ticket_field(
            channel.id, ticket_msg_id=msg.id
        )

        await interaction.followup.send(
            f"Your ticket has been created: {channel.mention}", ephemeral=True
        )


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT VIEW — Ticket control (claim / close / delete)
# ─────────────────────────────────────────────────────────────────────────────

class TicketControlView(discord.ui.View):
    """
    Posted inside every new ticket channel.
    All three buttons share the same persistent view instance (timeout=None).
    Each callback looks up the current ticket by interaction.channel.id.
    """

    def __init__(self):
        super().__init__(timeout=None)

    # ── Claim ────────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Claim Ticket",
        custom_id="ticket_ctrl_claim",
        style=discord.ButtonStyle.primary,
        emoji="🔖",
        row=0,
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "No ticket record found for this channel.", ephemeral=True
            )
        if not await user_is_staff(interaction):
            return await interaction.response.send_message(
                "Only staff can claim tickets.", ephemeral=True
            )
        if ticket["status"] not in ("open",):
            return await interaction.response.send_message(
                "This ticket is no longer open.", ephemeral=True
            )
        if ticket.get("claimed_by"):
            name = f"<@{ticket['claimed_by']}>"
            return await interaction.response.send_message(
                f"Already claimed by {name}.", ephemeral=True
            )

        await interaction.client.db.update_ticket_field(
            interaction.channel.id, claimed_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"🔖 Ticket claimed by {interaction.user.mention}."
        )

    # ── Close ────────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Close Ticket",
        custom_id="ticket_ctrl_close",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        row=0,
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "No ticket record found for this channel.", ephemeral=True
            )

        is_creator = ticket["user_id"] == interaction.user.id
        is_staff = await user_is_staff(interaction)
        if not is_creator and not is_staff:
            return await interaction.response.send_message(
                "You cannot close this ticket.", ephemeral=True
            )

        if ticket["status"] == "closing":
            closed_at = datetime.datetime.fromisoformat(ticket["closed_at"])
            elapsed = (datetime.datetime.utcnow() - closed_at).total_seconds()
            remaining = max(0, CLOSE_DELAY - elapsed)
            mins, secs = divmod(int(remaining), 60)
            return await interaction.response.send_message(
                f"Already closing — deletes in **{mins}m {secs}s**.", ephemeral=True
            )
        if ticket["status"] == "closed":
            return await interaction.response.send_message(
                "This ticket is already closed.", ephemeral=True
            )

        panel_cog: PanelCog = interaction.client.get_cog("Panel")
        await panel_cog._initiate_close(interaction, ticket)

    # ── Delete ───────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Delete Ticket",
        custom_id="ticket_ctrl_delete",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        row=0,
    )
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "No ticket record found for this channel.", ephemeral=True
            )
        if not await user_is_staff(interaction):
            return await interaction.response.send_message(
                "Only staff can delete tickets.", ephemeral=True
            )

        await interaction.response.send_message(
            "Are you sure you want to **permanently delete** this ticket right now?",
            view=ConfirmDeleteView(),
            ephemeral=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT VIEW — Reopen (posted with the close notice)
# ─────────────────────────────────────────────────────────────────────────────

class ReopenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reopen Ticket",
        custom_id="ticket_ctrl_reopen",
        style=discord.ButtonStyle.success,
        emoji="🔓",
    )
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "Ticket not found.", ephemeral=True
            )
        if ticket["status"] != "closing":
            return await interaction.response.send_message(
                "This ticket is not in a closing state.", ephemeral=True
            )

        # Cancel the scheduled deletion task
        panel_cog: PanelCog = interaction.client.get_cog("Panel")
        task = panel_cog._closing_tasks.pop(interaction.channel.id, None)
        if task:
            task.cancel()

        # Reset DB state
        await interaction.client.db.update_ticket_field(
            interaction.channel.id,
            status="open",
            closed_at=None,
            close_msg_id=None,
        )

        # Edit the close message (this message) to show the ticket was reopened
        reopened_embed = discord.Embed(
            title="🔓 Ticket Reopened",
            description=f"Reopened by {interaction.user.mention}. The deletion has been cancelled.",
            color=0x57F287,
        )
        await interaction.response.edit_message(embed=reopened_embed, view=None)
        await interaction.channel.send("🎫 This ticket is open again.")


# ─────────────────────────────────────────────────────────────────────────────
# TEMPORARY VIEW — Confirm ticket deletion
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Yes, delete now", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        panel_cog: PanelCog = interaction.client.get_cog("Panel")
        task = panel_cog._closing_tasks.pop(interaction.channel.id, None)
        if task:
            task.cancel()

        await interaction.client.db.update_ticket_field(
            interaction.channel.id, status="closed"
        )
        await interaction.response.edit_message(
            content="Deleting ticket in 3 seconds…", view=None
        )
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(
                reason=f"Ticket force-deleted by {interaction.user}"
            )
        except Exception:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)


# ─────────────────────────────────────────────────────────────────────────────
# MODALS
# ─────────────────────────────────────────────────────────────────────────────

class SelfRolePanelModal(discord.ui.Modal, title="Create Self Role Panel"):
    panel_title = discord.ui.TextInput(
        label="Panel Title", placeholder="e.g. Choose Your Roles", max_length=256
    )
    panel_description = discord.ui.TextInput(
        label="Description",
        placeholder="Select your roles below.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1024,
    )

    async def on_submit(self, interaction: discord.Interaction):
        all_roles = sorted(
            [
                {"id": r.id, "name": r.name}
                for r in interaction.guild.roles
                if not r.is_default() and not r.managed
            ],
            key=lambda r: r["name"].lower(),
        )
        if not all_roles:
            return await interaction.response.send_message(
                "No assignable roles found.", ephemeral=True
            )
        view = RoleSelectView(
            title=self.panel_title.value,
            description=self.panel_description.value or "",
            all_roles=all_roles,
        )
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )


class EditSelfRolePanelModal(discord.ui.Modal):
    def __init__(self, panel: dict):
        super().__init__(title="Edit Self Role Panel")
        self.panel = panel
        self.panel_title_input = discord.ui.TextInput(
            label="Panel Title", default=panel["title"][:256], max_length=256
        )
        self.panel_desc_input = discord.ui.TextInput(
            label="Description",
            default=(panel["description"] or "")[:1024],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1024,
        )
        self.add_item(self.panel_title_input)
        self.add_item(self.panel_desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_title = self.panel_title_input.value
        new_desc = self.panel_desc_input.value or ""
        all_roles = sorted(
            [
                {"id": r.id, "name": r.name}
                for r in interaction.guild.roles
                if not r.is_default() and not r.managed
            ],
            key=lambda r: r["name"].lower(),
        )
        if not all_roles:
            return await interaction.response.send_message(
                "No assignable roles found.", ephemeral=True
            )
        current_role_ids = {r["id"] for r in json.loads(self.panel.get("roles", "[]"))}
        view = RoleSelectView(
            title=new_title,
            description=new_desc,
            all_roles=all_roles,
            edit_panel=self.panel,
            pre_selected=current_role_ids,
        )
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )


class TicketPanelModal(discord.ui.Modal, title="Create Ticket Panel"):
    panel_title = discord.ui.TextInput(
        label="Panel Title", placeholder="e.g. Support Tickets", max_length=256
    )
    panel_description = discord.ui.TextInput(
        label="Description",
        placeholder="Click a button below to open a ticket.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1024,
    )

    async def on_submit(self, interaction: discord.Interaction):
        view = TicketButtonBuilderView(
            title=self.panel_title.value,
            description=self.panel_description.value or "",
        )
        embed = view.build_preview_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view._message = await interaction.original_response()


class AddTicketButtonModal(discord.ui.Modal, title="Add Ticket Button"):
    def __init__(self, builder_view: TicketButtonBuilderView):
        super().__init__()
        self.builder_view = builder_view
        self.button_label = discord.ui.TextInput(
            label="Button Label",
            placeholder="e.g. Open Support Ticket",
            max_length=80,
        )
        self.add_item(self.button_label)

    async def on_submit(self, interaction: discord.Interaction):
        view = CategorySelectView(
            label=self.button_label.value,
            builder_view=self.builder_view,
        )
        await interaction.response.send_message(
            "Select the category for this button:", view=view, ephemeral=True
        )


class EditTicketPanelModal(discord.ui.Modal):
    def __init__(self, panel: dict):
        super().__init__(title="Edit Ticket Panel")
        self.panel = panel
        self.panel_title_input = discord.ui.TextInput(
            label="Panel Title", default=panel["title"][:256], max_length=256
        )
        self.panel_desc_input = discord.ui.TextInput(
            label="Description",
            default=(panel["description"] or "")[:1024],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1024,
        )
        self.add_item(self.panel_title_input)
        self.add_item(self.panel_desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_title = self.panel_title_input.value
        new_desc = self.panel_desc_input.value or ""
        existing_buttons = json.loads(self.panel.get("buttons", "[]"))
        view = TicketButtonEditorView(
            title=new_title,
            description=new_desc,
            panel=self.panel,
            existing_buttons=existing_buttons,
        )
        await interaction.response.send_message(
            embed=view.build_preview_embed(), view=view, ephemeral=True
        )
        view._message = await interaction.original_response()


# ─────────────────────────────────────────────────────────────────────────────
# TEMPORARY / EPHEMERAL VIEWS — creation flows
# ─────────────────────────────────────────────────────────────────────────────

class RoleSearchModal(discord.ui.Modal, title="Search Roles"):
    search_input = discord.ui.TextInput(
        label="Role name (partial match works)",
        placeholder="e.g. mod, admin, vip…",
        max_length=50,
    )

    def __init__(self, parent: "RoleSelectView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        self.parent.filter_term = self.search_input.value.strip()
        self.parent.page = 0
        self.parent._rebuild()
        await interaction.response.edit_message(
            embed=self.parent.build_embed(), view=self.parent
        )


class RoleSelectView(discord.ui.View):
    PAGE_SIZE = 23

    def __init__(
        self,
        title: str,
        description: str,
        all_roles: list,
        edit_panel: Optional[dict] = None,
        pre_selected: Optional[set] = None,
    ):
        super().__init__(timeout=300)
        self.title = title
        self.description = description
        self.all_roles = all_roles          # [{"id": int, "name": str}, ...]  full server list
        self.selected: set[int] = set(pre_selected) if pre_selected else set()
        self.edit_panel = edit_panel
        self.filter_term = ""
        self.page = 0
        self._rebuild()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _filtered(self) -> list:
        if not self.filter_term:
            return self.all_roles
        term = self.filter_term.lower()
        return [r for r in self.all_roles if term in r["name"].lower()]

    def _page_roles(self) -> list:
        filtered = self._filtered()
        start = self.page * self.PAGE_SIZE
        return filtered[start : start + self.PAGE_SIZE]

    def _total_pages(self) -> int:
        return max(1, -(-len(self._filtered()) // self.PAGE_SIZE))

    # ── View rebuilder ────────────────────────────────────────────────────────

    def _rebuild(self):
        self.clear_items()
        page_roles = self._page_roles()
        total_pages = self._total_pages()

        # Row 0: role dropdown
        if page_roles:
            options = [
                discord.SelectOption(
                    label=r["name"][:100],
                    value=str(r["id"]),
                    description="✅ Selected" if r["id"] in self.selected else None,
                    default=r["id"] in self.selected,
                )
                for r in page_roles
            ]
            sel = discord.ui.Select(
                placeholder=f"Roles — page {self.page + 1} of {total_pages}",
                options=options,
                min_values=0,
                max_values=len(options),
                row=0,
            )
            sel.callback = self._on_select
            self.add_item(sel)
        else:
            empty = discord.ui.Select(
                placeholder="No roles match that search",
                options=[discord.SelectOption(label="No results", value="_none_")],
                disabled=True,
                row=0,
            )
            self.add_item(empty)

        # Row 1: navigation + search (max 4 buttons to stay within Discord's limit)
        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page == 0),
            row=1,
        )
        prev_btn.callback = self._prev
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page + 1 >= total_pages),
            row=1,
        )
        next_btn.callback = self._next
        self.add_item(next_btn)

        search_btn = discord.ui.Button(
            label="🔍 Search", style=discord.ButtonStyle.primary, row=1
        )
        search_btn.callback = self._open_search
        self.add_item(search_btn)

        if self.filter_term:
            clear_btn = discord.ui.Button(
                label="✖ Clear Filter", style=discord.ButtonStyle.secondary, row=1
            )
            clear_btn.callback = self._clear_filter
            self.add_item(clear_btn)

        # Row 2: confirm done
        done_btn = discord.ui.Button(
            label=f"✅ Done  ({len(self.selected)} selected)",
            style=discord.ButtonStyle.success,
            disabled=(len(self.selected) == 0),
            row=2,
        )
        done_btn.callback = self._done
        self.add_item(done_btn)

    # ── Embed ─────────────────────────────────────────────────────────────────

    def build_embed(self) -> discord.Embed:
        filtered = self._filtered()
        total_pages = self._total_pages()
        embed = discord.Embed(
            title="Step 2 — Select Roles",
            description=(
                "Use the dropdown to pick roles, **🔍 Search** to filter by name, "
                "and the arrows to browse pages.\n"
                "Selections carry over as you navigate — press **Done** when finished."
            ),
            color=0x5865F2,
        )
        filter_line = f"Filter: `{self.filter_term}`" if self.filter_term else "No filter active"
        embed.add_field(
            name="Browse",
            value=(
                f"{filter_line}  •  Page **{self.page + 1} / {total_pages}**  "
                f"•  {len(filtered)} role(s) visible"
            ),
            inline=False,
        )
        if self.selected:
            names = [f"• {r['name']}" for r in self.all_roles if r["id"] in self.selected]
            overflow = len(names) - 20
            display = "\n".join(names[:20])
            if overflow > 0:
                display += f"\n…and {overflow} more"
            embed.add_field(
                name=f"Selected [{len(self.selected)}]", value=display, inline=False
            )
        else:
            embed.add_field(name="Selected [0]", value="Nothing selected yet.", inline=False)
        return embed

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_select(self, interaction: discord.Interaction):
        page_ids = {r["id"] for r in self._page_roles()}
        chosen_ids = {int(v) for v in interaction.data["values"] if v != "_none_"}
        # Remove this page's roles from accumulated set, then add what's now selected
        self.selected -= page_ids
        self.selected |= chosen_ids
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _open_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RoleSearchModal(parent=self))

    async def _clear_filter(self, interaction: discord.Interaction):
        self.filter_term = ""
        self.page = 0
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _done(self, interaction: discord.Interaction):
        roles = [r for r in self.all_roles if r["id"] in self.selected]
        if self.edit_panel:
            view = SelfRoleEditConfirmView(
                title=self.title, description=self.description,
                roles=roles, panel=self.edit_panel,
            )
        else:
            view = SelfRoleConfirmView(
                title=self.title, description=self.description, roles=roles
            )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class SelfRoleConfirmView(discord.ui.View):
    def __init__(self, title: str, description: str, roles: list):
        super().__init__(timeout=300)
        self.title = title
        self.description = description
        self.roles = roles
        self._target_channel = None

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Preview — {self.title}",
            description=self.description or "Select a role below:",
            color=0x5865F2,
        )
        role_lines = "\n".join(f"• {r['name']}" for r in self.roles) or "None"
        embed.add_field(name="Roles (will become buttons)", value=role_lines, inline=False)
        embed.set_footer(text="Choose a channel, then click Confirm to post.")
        return embed

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select channel to post panel in",
        channel_types=[discord.ChannelType.text],
    )
    async def channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ):
        raw = select.values[0]
        self._target_channel = raw.resolve() or interaction.guild.get_channel(raw.id)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._target_channel:
            return await interaction.response.send_message(
                "Please select a channel first.", ephemeral=True
            )
        channel = self._target_channel
        embed = discord.Embed(
            title=self.title,
            description=self.description or "Click a button to toggle a role.",
            color=0x5865F2,
        )
        msg = await channel.send(embed=embed)
        panel_id = await interaction.client.db.create_self_role_panel(
            interaction.guild.id, channel.id, msg.id,
            self.title, self.description, self.roles,
        )
        sr_view = SelfRoleView()
        for r in self.roles:
            sr_view.add_item(SelfRoleButton(panel_id, r["id"], r["name"]))
        interaction.client.add_view(sr_view)
        await msg.edit(embed=embed, view=sr_view)
        await interaction.response.edit_message(
            content=f"Self-role panel posted in {channel.mention}!",
            embed=None, view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌", row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Panel creation cancelled.", embed=None, view=None
        )


class SelfRoleEditConfirmView(discord.ui.View):
    """Confirm step when editing an existing self-role panel (no channel picker needed)."""

    def __init__(self, title: str, description: str, roles: list, panel: dict):
        super().__init__(timeout=300)
        self.title = title
        self.description = description
        self.roles = roles
        self.panel = panel

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Save Preview — {self.title}",
            description=self.description or "Select a role below:",
            color=0x5865F2,
        )
        role_lines = "\n".join(f"• {r['name']}" for r in self.roles) or "None"
        embed.add_field(name="Roles (will become buttons)", value=role_lines, inline=False)
        embed.set_footer(text="Click Save Changes to apply, or Cancel to discard.")
        return embed

    @discord.ui.button(label="Save Changes", style=discord.ButtonStyle.success, emoji="💾", row=0)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.db.update_self_role_panel(
            self.panel["id"],
            title=self.title,
            description=self.description,
            roles=json.dumps(self.roles),
        )
        panel = await interaction.client.db.get_self_role_panel(self.panel["id"])
        if panel and panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    msg = await ch.fetch_message(panel["message_id"])
                    embed = discord.Embed(
                        title=self.title,
                        description=self.description or "Click a button to toggle a role.",
                        color=0x5865F2,
                    )
                    sr_view = SelfRoleView()
                    for r in self.roles:
                        sr_view.add_item(SelfRoleButton(self.panel["id"], r["id"], r["name"]))
                    interaction.client.add_view(sr_view)
                    await msg.edit(embed=embed, view=sr_view)
            except Exception:
                pass
        await interaction.response.edit_message(
            content=f"Panel **{self.title}** updated.", embed=None, view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Edit cancelled.", embed=None, view=None
        )


class CategorySelectView(discord.ui.View):
    def __init__(self, label: str, builder_view: TicketButtonBuilderView):
        super().__init__(timeout=120)
        self.label = label
        self.builder_view = builder_view

    @discord.ui.select(
        placeholder="Select button category",
        options=[
            discord.SelectOption(
                label="Caleb", value="caleb",
                description="Management — requires management role", emoji="👑"
            ),
            discord.SelectOption(
                label="Support", value="support",
                description="Support staff tickets", emoji="🎧"
            ),
            discord.SelectOption(
                label="General", value="general",
                description="General staff tickets", emoji="📋"
            ),
            discord.SelectOption(
                label="Join the Team", value="join",
                description="Recruitment — management access", emoji="📝"
            ),
        ],
    )
    async def category_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        category = select.values[0]
        self.builder_view.buttons.append({"text": self.label, "category": category})
        if self.builder_view._message:
            try:
                await self.builder_view._message.edit(
                    embed=self.builder_view.build_preview_embed()
                )
            except Exception:
                pass
        await interaction.response.edit_message(
            content=f"Added **{self.label}** ({category.title()}) — go back to continue.",
            view=None,
        )


class TicketButtonBuilderView(discord.ui.View):
    def __init__(self, title: str, description: str):
        super().__init__(timeout=600)
        self.title = title
        self.description = description
        self.buttons: list = []
        self._target_channel = None
        self._message = None

    def build_preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Preview — {self.title}",
            description=self.description or "Click a button below to open a ticket.",
            color=0x5865F2,
        )
        lines = (
            "\n".join(f"• **{b['text']}** → {b['category'].title()}" for b in self.buttons)
            if self.buttons else "None added yet"
        )
        embed.add_field(name="Buttons", value=lines, inline=False)
        embed.set_footer(text="Add buttons, select a channel, then confirm.")
        return embed

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.buttons) >= 25:
            return await interaction.response.send_message(
                "Maximum of 25 buttons reached.", ephemeral=True
            )
        await interaction.response.send_modal(AddTicketButtonModal(builder_view=self))

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select channel to post panel in",
        channel_types=[discord.ChannelType.text],
        row=1,
    )
    async def channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ):
        raw = select.values[0]
        self._target_channel = raw.resolve() or interaction.guild.get_channel(raw.id)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._target_channel:
            return await interaction.response.send_message(
                "Please select a channel first.", ephemeral=True
            )
        if not self.buttons:
            return await interaction.response.send_message(
                "Please add at least one button.", ephemeral=True
            )
        channel = self._target_channel
        embed = discord.Embed(
            title=self.title,
            description=self.description or "Click a button below to open a ticket.",
            color=0x5865F2,
        )
        msg = await channel.send(embed=embed)
        panel_id = await interaction.client.db.create_ticket_panel(
            interaction.guild.id, channel.id, msg.id,
            self.title, self.description, self.buttons,
        )
        tk_view = TicketView()
        for i, btn_data in enumerate(self.buttons):
            tk_view.add_item(
                TicketButton(panel_id, btn_data["category"], btn_data["text"], i)
            )
        interaction.client.add_view(tk_view)
        await msg.edit(embed=embed, view=tk_view)
        await interaction.response.edit_message(
            content=f"Ticket panel posted in {channel.mention}!",
            embed=None, view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌", row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Panel creation cancelled.", embed=None, view=None
        )


class RemoveTicketButtonView(discord.ui.View):
    """Ephemeral dropdown to remove a button from a TicketButtonEditorView."""

    def __init__(self, editor_view: TicketButtonEditorView):
        super().__init__(timeout=120)
        self.editor_view = editor_view
        options = [
            discord.SelectOption(
                label=b["text"][:100],
                value=str(i),
                description=b["category"].title(),
            )
            for i, b in enumerate(editor_view.buttons[:25])
        ]
        sel = discord.ui.Select(placeholder="Select button to remove", options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        removed = self.editor_view.buttons.pop(idx)
        if self.editor_view._message:
            try:
                await self.editor_view._message.edit(
                    embed=self.editor_view.build_preview_embed()
                )
            except Exception:
                pass
        await interaction.response.edit_message(
            content=f"Removed **{removed['text']}** button. Go back to continue.",
            view=None,
        )


class TicketButtonEditorView(discord.ui.View):
    """
    Edit-mode equivalent of TicketButtonBuilderView.
    Pre-populated with existing buttons; saves in-place without a channel picker.
    """

    def __init__(self, title: str, description: str, panel: dict, existing_buttons: list):
        super().__init__(timeout=600)
        self.title = title
        self.description = description
        self.panel = panel
        self.buttons: list = list(existing_buttons)
        self._message = None

    def build_preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Edit Preview — {self.title}",
            description=self.description or "Click a button below to open a ticket.",
            color=0x5865F2,
        )
        lines = (
            "\n".join(f"• **{b['text']}** → {b['category'].title()}" for b in self.buttons)
            if self.buttons else "None added yet"
        )
        embed.add_field(name="Buttons", value=lines, inline=False)
        embed.set_footer(text="Add/remove buttons, then click Save Changes.")
        return embed

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.buttons) >= 25:
            return await interaction.response.send_message(
                "Maximum of 25 buttons reached.", ephemeral=True
            )
        await interaction.response.send_modal(AddTicketButtonModal(builder_view=self))

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.secondary, emoji="➖", row=0)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.buttons:
            return await interaction.response.send_message(
                "No buttons to remove.", ephemeral=True
            )
        await interaction.response.send_message(
            "Select a button to remove:",
            view=RemoveTicketButtonView(editor_view=self),
            ephemeral=True,
        )

    @discord.ui.button(label="Save Changes", style=discord.ButtonStyle.success, emoji="💾", row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.buttons:
            return await interaction.response.send_message(
                "Please add at least one button.", ephemeral=True
            )
        await interaction.client.db.update_ticket_panel(
            self.panel["id"],
            title=self.title,
            description=self.description,
            buttons=json.dumps(self.buttons),
        )
        panel = await interaction.client.db.get_ticket_panel(self.panel["id"])
        if panel and panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    msg = await ch.fetch_message(panel["message_id"])
                    embed = discord.Embed(
                        title=self.title,
                        description=self.description or "Click a button below to open a ticket.",
                        color=0x5865F2,
                    )
                    tk_view = TicketView()
                    for i, btn_data in enumerate(self.buttons):
                        tk_view.add_item(
                            TicketButton(self.panel["id"], btn_data["category"], btn_data["text"], i)
                        )
                    interaction.client.add_view(tk_view)
                    await msg.edit(embed=embed, view=tk_view)
            except Exception:
                pass
        await interaction.response.edit_message(
            content=f"Panel **{self.title}** updated.", embed=None, view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Edit cancelled.", embed=None, view=None
        )


# ─────────────────────────────────────────────────────────────────────────────
# PANEL MANAGEMENT VIEWS (admin UI — ephemeral)
# ─────────────────────────────────────────────────────────────────────────────

class PanelSelectView(discord.ui.View):
    def __init__(self, panels: list, action: str, panel_type: str):
        super().__init__(timeout=120)
        self.action = action
        self.panel_type = panel_type
        if panels:
            options = [
                discord.SelectOption(
                    label=f"[{p['id']}] {p['title'][:85]}",
                    value=str(p["id"]),
                    description="Enabled" if p["enabled"] else "Disabled",
                )
                for p in panels[:25]
            ]
            sel = discord.ui.Select(
                placeholder=f"Select panel to {action}", options=options
            )
            sel.callback = self._on_select
            self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        panel_id = int(interaction.data["values"][0])
        db = interaction.client.db

        if self.panel_type == "selfrole":
            get = db.get_self_role_panel
            toggle = db.toggle_self_role_panel
            delete = db.delete_self_role_panel
            EditModal = EditSelfRolePanelModal
        else:
            get = db.get_ticket_panel
            toggle = db.toggle_ticket_panel
            delete = db.delete_ticket_panel
            EditModal = EditTicketPanelModal

        panel = await get(panel_id)
        if not panel:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)

        if self.action == "delete":
            if panel.get("channel_id") and panel.get("message_id"):
                try:
                    ch = interaction.guild.get_channel(panel["channel_id"])
                    if ch:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.delete()
                except Exception:
                    pass
            await delete(panel_id)
            await interaction.response.edit_message(
                content=f"Panel **{panel['title']}** deleted.", embed=None, view=None
            )
        elif self.action == "enable":
            await toggle(panel_id, True)
            await interaction.response.edit_message(
                content=f"Panel **{panel['title']}** enabled.", embed=None, view=None
            )
        elif self.action == "disable":
            await toggle(panel_id, False)
            await interaction.response.edit_message(
                content=f"Panel **{panel['title']}** disabled.", embed=None, view=None
            )
        elif self.action == "edit":
            await interaction.response.send_modal(EditModal(panel=panel))


class SelfRoleManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SelfRolePanelModal())

    @discord.ui.button(label="List", style=discord.ButtonStyle.primary, emoji="📋", row=0)
    async def list_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_self_role_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message(
                "No self-role panels found.", ephemeral=True
            )
        embed = discord.Embed(title="Self Role Panels", color=0x5865F2)
        for p in panels:
            roles = json.loads(p["roles"])
            role_names = ", ".join(r["name"] for r in roles) or "None"
            status = "✅ Enabled" if p["enabled"] else "❌ Disabled"
            embed.add_field(
                name=f"[ID {p['id']}] {p['title']}  —  {status}",
                value=f"Roles: {role_names[:200]}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_self_role_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No panels to edit.", ephemeral=True)
        await interaction.response.send_message(
            "Select a panel to edit:",
            view=PanelSelectView(panels, "edit", "selfrole"),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_self_role_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No panels to delete.", ephemeral=True)
        await interaction.response.send_message(
            "Select a panel to delete:",
            view=PanelSelectView(panels, "delete", "selfrole"),
            ephemeral=True,
        )

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_self_role_panels(interaction.guild.id)
        disabled = [p for p in panels if not p["enabled"]]
        if not disabled:
            return await interaction.response.send_message(
                "No disabled panels found.", ephemeral=True
            )
        await interaction.response.send_message(
            "Select a panel to enable:",
            view=PanelSelectView(disabled, "enable", "selfrole"),
            ephemeral=True,
        )

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_self_role_panels(interaction.guild.id)
        enabled = [p for p in panels if p["enabled"]]
        if not enabled:
            return await interaction.response.send_message(
                "No enabled panels found.", ephemeral=True
            )
        await interaction.response.send_message(
            "Select a panel to disable:",
            view=PanelSelectView(enabled, "disable", "selfrole"),
            ephemeral=True,
        )


class TicketPanelManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketPanelModal())

    @discord.ui.button(label="List", style=discord.ButtonStyle.primary, emoji="📋", row=0)
    async def list_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_ticket_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message(
                "No ticket panels found.", ephemeral=True
            )
        embed = discord.Embed(title="Ticket Panels", color=0x5865F2)
        for p in panels:
            btns = json.loads(p["buttons"])
            btn_list = ", ".join(f"{b['text']} ({b['category']})" for b in btns) or "None"
            status = "✅ Enabled" if p["enabled"] else "❌ Disabled"
            embed.add_field(
                name=f"[ID {p['id']}] {p['title']}  —  {status}",
                value=f"Buttons: {btn_list[:200]}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_ticket_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No panels to edit.", ephemeral=True)
        await interaction.response.send_message(
            "Select a panel to edit:",
            view=PanelSelectView(panels, "edit", "ticket"),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_ticket_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No panels to delete.", ephemeral=True)
        await interaction.response.send_message(
            "Select a panel to delete:",
            view=PanelSelectView(panels, "delete", "ticket"),
            ephemeral=True,
        )

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_ticket_panels(interaction.guild.id)
        disabled = [p for p in panels if not p["enabled"]]
        if not disabled:
            return await interaction.response.send_message(
                "No disabled panels found.", ephemeral=True
            )
        await interaction.response.send_message(
            "Select a panel to enable:",
            view=PanelSelectView(disabled, "enable", "ticket"),
            ephemeral=True,
        )

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        panels = await interaction.client.db.get_ticket_panels(interaction.guild.id)
        enabled = [p for p in panels if p["enabled"]]
        if not enabled:
            return await interaction.response.send_message(
                "No enabled panels found.", ephemeral=True
            )
        await interaction.response.send_message(
            "Select a panel to disable:",
            view=PanelSelectView(enabled, "disable", "ticket"),
            ephemeral=True,
        )


class ConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Ticket Panels", style=discord.ButtonStyle.primary, emoji="🎫")
    async def ticket_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Ticket Panel Management",
            description="Create, list, edit, enable, disable, or delete ticket panels.",
            color=0x5865F2,
        )
        await interaction.response.edit_message(embed=embed, view=TicketPanelManagementView())

    @discord.ui.button(label="Self Role Panels", style=discord.ButtonStyle.secondary, emoji="🏷️")
    async def self_role_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Self Role Panel Management",
            description="Create, list, edit, enable, disable, or delete self-role panels.",
            color=0x5865F2,
        )
        await interaction.response.edit_message(embed=embed, view=SelfRoleManagementView())


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class PanelCog(commands.Cog, name="Panel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Maps channel_id → asyncio.Task for pending ticket deletions.
        self._closing_tasks: dict[int, asyncio.Task] = {}

    # ── Startup ───────────────────────────────────────────────────────────────

    async def cog_load(self):
        # Re-register all persistent self-role panel views
        for panel in await self.bot.db.get_all_self_role_panels():
            sr_view = SelfRoleView()
            for r in json.loads(panel["roles"]):
                sr_view.add_item(SelfRoleButton(panel["id"], r["id"], r["name"]))
            self.bot.add_view(sr_view)

        # Re-register all persistent ticket panel views
        for panel in await self.bot.db.get_all_ticket_panels():
            tk_view = TicketView()
            for i, b in enumerate(json.loads(panel["buttons"])):
                tk_view.add_item(TicketButton(panel["id"], b["category"], b["text"], i))
            self.bot.add_view(tk_view)

        # One shared instance handles every ticket channel
        self.bot.add_view(TicketControlView())
        self.bot.add_view(ReopenTicketView())

        # Resume any close countdowns that were running before a restart
        for ticket in await self.bot.db.get_closing_tickets():
            if not ticket.get("closed_at"):
                continue
            try:
                closed_at = datetime.datetime.fromisoformat(ticket["closed_at"])
                elapsed = (datetime.datetime.utcnow() - closed_at).total_seconds()
                remaining = max(0.0, CLOSE_DELAY - elapsed)
                task = asyncio.create_task(
                    self._delete_ticket_after(
                        ticket["channel_id"], ticket["guild_id"], remaining
                    )
                )
                self._closing_tasks[ticket["channel_id"]] = task
            except Exception:
                pass

    async def cog_unload(self):
        for task in self._closing_tasks.values():
            task.cancel()
        self._closing_tasks.clear()

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "You don't have permission to use this command.", ephemeral=True
                )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _delete_ticket_after(
        self, channel_id: int, guild_id: int, delay: float
    ):
        """Background task: wait `delay` seconds then delete the ticket channel."""
        try:
            await asyncio.sleep(delay)
            ticket = await self.bot.db.get_ticket_by_channel(channel_id)
            if ticket and ticket["status"] == "closing":
                await self.bot.db.update_ticket_field(channel_id, status="closed")
                guild = self.bot.get_guild(guild_id)
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        await channel.delete(
                            reason="Ticket auto-deleted 60 minutes after close"
                        )
        except asyncio.CancelledError:
            pass
        finally:
            self._closing_tasks.pop(channel_id, None)

    async def _initiate_close(
        self, interaction: discord.Interaction, ticket: dict
    ):
        """Shared logic for both the Close button and /ticket close command."""
        now = datetime.datetime.utcnow().isoformat()

        close_embed = discord.Embed(
            title="🔒 Ticket Closed",
            description=(
                f"Closed by {interaction.user.mention}.\n\n"
                f"This ticket will be **automatically deleted in 60 minutes**.\n"
                "Click **Reopen Ticket** below to cancel the deletion."
            ),
            color=0xFEE75C,
        )
        close_embed.set_footer(text="Reopen within 60 minutes to keep the ticket.")

        await interaction.response.send_message(
            embed=close_embed, view=ReopenTicketView()
        )
        msg = await interaction.original_response()

        await interaction.client.db.update_ticket_field(
            interaction.channel.id,
            status="closing",
            closed_at=now,
            close_msg_id=msg.id,
        )

        task = asyncio.create_task(
            self._delete_ticket_after(
                interaction.channel.id, interaction.guild.id, CLOSE_DELAY
            )
        )
        self._closing_tasks[interaction.channel.id] = task

    # ── /config ───────────────────────────────────────────────────────────────

    @app_commands.command(name="config", description="Configure bot panels")
    @app_commands.check(management_check)
    async def config(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Bot Configuration",
            description="Select a panel type to manage.",
            color=0x5865F2,
        )
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(
            embed=embed, view=ConfigView(), ephemeral=True
        )

    # ── /ticket group ─────────────────────────────────────────────────────────

    ticket = app_commands.Group(
        name="ticket", description="Manage the current ticket channel"
    )

    @ticket.command(name="claim", description="Claim this ticket (staff only)")
    async def ticket_claim(self, interaction: discord.Interaction):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This command can only be used inside a ticket channel.", ephemeral=True
            )
        if not await user_is_staff(interaction):
            return await interaction.response.send_message(
                "Only staff can claim tickets.", ephemeral=True
            )
        if ticket["status"] not in ("open",):
            return await interaction.response.send_message(
                "This ticket is no longer open.", ephemeral=True
            )
        if ticket.get("claimed_by"):
            return await interaction.response.send_message(
                f"Already claimed by <@{ticket['claimed_by']}>.", ephemeral=True
            )
        await interaction.client.db.update_ticket_field(
            interaction.channel.id, claimed_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"🔖 Ticket claimed by {interaction.user.mention}."
        )

    @ticket.command(name="close", description="Close this ticket (60-min deletion window)")
    async def ticket_close(self, interaction: discord.Interaction):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This command can only be used inside a ticket channel.", ephemeral=True
            )
        is_creator = ticket["user_id"] == interaction.user.id
        is_staff = await user_is_staff(interaction)
        if not is_creator and not is_staff:
            return await interaction.response.send_message(
                "You cannot close this ticket.", ephemeral=True
            )
        if ticket["status"] == "closing":
            closed_at = datetime.datetime.fromisoformat(ticket["closed_at"])
            elapsed = (datetime.datetime.utcnow() - closed_at).total_seconds()
            remaining = max(0, CLOSE_DELAY - elapsed)
            mins, secs = divmod(int(remaining), 60)
            return await interaction.response.send_message(
                f"Already closing — deletes in **{mins}m {secs}s**.", ephemeral=True
            )
        if ticket["status"] == "closed":
            return await interaction.response.send_message(
                "This ticket is already closed.", ephemeral=True
            )
        await self._initiate_close(interaction, ticket)

    @ticket.command(name="delete", description="Immediately delete this ticket channel (staff only)")
    async def ticket_delete(self, interaction: discord.Interaction):
        ticket = await interaction.client.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This command can only be used inside a ticket channel.", ephemeral=True
            )
        if not await user_is_staff(interaction):
            return await interaction.response.send_message(
                "Only staff can delete tickets.", ephemeral=True
            )
        await interaction.response.send_message(
            "Are you sure you want to **permanently delete** this ticket right now?",
            view=ConfirmDeleteView(),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PanelCog(bot))
