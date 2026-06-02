import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS


class GlobalCommandsCog(commands.Cog, name="Global"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /help ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="Show all bot commands and how to use them")
    async def help(self, interaction: discord.Interaction):
        bot = interaction.client
        is_dev = interaction.user.id in DEVELOPER_IDS

        embed = discord.Embed(
            description=(
                "Below is every command available. "
                "Some are restricted by role — see the labels next to each section."
            ),
            color=0x5865F2,
        )
        embed.set_author(
            name=f"{bot.user.name}  —  Help",
            icon_url=bot.user.display_avatar.url,
        )
        embed.set_thumbnail(url=bot.user.display_avatar.url)

        # ── Panel Management ──────────────────────────────────────────────────
        embed.add_field(
            name="🔧  Panel Management",
            value=(
                "> Requires the **Management Role**\n"
                "`/config`  —  Open the full panel management UI\n"
                "┕ **Ticket Panels** — Create, edit, enable/disable panels\n"
                "┕ **Self Role Panels** — Create, edit, enable/disable panels"
            ),
            inline=False,
        )

        # ── Ticket Commands ───────────────────────────────────────────────────
        embed.add_field(
            name="🎫  Ticket Commands",
            value=(
                "> Run inside a ticket channel\n"
                "`/ticket claim`  —  Claim this ticket as yours `[Staff]`\n"
                "`/ticket close`  —  Close the ticket (60-min deletion window)\n"
                "`/ticket delete` —  Immediately delete the channel `[Staff]`"
            ),
            inline=False,
        )

        # ── Ticket Buttons ────────────────────────────────────────────────────
        embed.add_field(
            name="🖱️  In-Ticket Buttons",
            value=(
                "Every ticket channel has three buttons:\n"
                "🔖 **Claim Ticket** — Assigns the ticket to you `[Staff]`\n"
                "🔒 **Close Ticket** — Starts 60-min countdown, shows Reopen button\n"
                "🗑️ **Delete Ticket** — Instant delete with confirmation `[Staff]`\n"
                "🔓 **Reopen Ticket** — Cancels the deletion timer *(on close message)*"
            ),
            inline=False,
        )

        # ── Self Role Panels ──────────────────────────────────────────────────
        embed.add_field(
            name="🏷️  Self Role Panels",
            value=(
                "Panels are created via `/config` → **Self Role Panels**\n"
                "Each role becomes a button — clicking it toggles the role on/off\n"
                "Panels and button states survive bot restarts automatically"
            ),
            inline=False,
        )

        # ── General / Info ────────────────────────────────────────────────────
        embed.add_field(
            name="🌐  Information",
            value=(
                "`/userinfo [member]`  —  Username, ID, roles, status, activity,\n"
                "              join date, account age, avatar & more\n"
                "`/serverinfo`  —  Members, channels, roles, boosts,\n"
                "              verification, features, icon & banner"
            ),
            inline=False,
        )

        # ── Role Assignment (developer-visible always, shown to others as locked) ─
        embed.add_field(
            name="⚙️  Role Assignment  `[Developer Only]`",
            value=(
                "`/assignrole management <role>`  —  Set the management role\n"
                " Grants `/config` access and access to Caleb ticket channels\n"
                "`/assignrole staff <role>`  —  Set the staff role\n"
                " Grants access to Support and General ticket channels"
            ),
            inline=False,
        )

        # ── Broadcast ─────────────────────────────────────────────────────────
        embed.add_field(
            name="📡  Broadcast  `[Management]`",
            value=(
                "`/image <attachment|url>`  —  Send an image to the image channel\n"
                "`/video <attachment|url>`  —  Send a video or YouTube link to the video channel\n"
                "`/register image <channel>`  —  Set the image broadcast channel `[Developer]`\n"
                "`/register video <channel>`  —  Set the video broadcast channel `[Developer]`\n"
                "`/register relay <channel>`  —  Set the relay broadcast channel `[Developer]`"
            ),
            inline=False,
        )

        # ── Role Blacklist ────────────────────────────────────────────────────
        embed.add_field(
            name="🚫  Role Blacklist  `[Developer Only]`",
            value=(
                "`/roleblacklist add <user> <role>`  —  Blacklist a user from a role\n"
                "`/roleblacklist remove <user> <role>`  —  Remove a blacklist entry\n"
                "`/roleblacklist list`  —  View all active blacklist entries"
            ),
            inline=False,
        )

        # ── Cog Management (only shown to developers) ─────────────────────────
        if is_dev:
            embed.add_field(
                name="🛠️  Cog Management  `[Developer Only]`",
                value=(
                    "`/reload <cog>`  —  Hot-reload a cog  *(e.g. `cogs.panel`)*\n"
                    "`/load <cog>`  —  Load a cog that isn't currently running\n"
                    "`/unload <cog>`  —  Unload a running cog"
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                f"Ticket categories: Caleb (management) • Support (staff) • General (staff)  "
                f"│  All panels persist across restarts"
            )
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /userinfo ─────────────────────────────────────────────────────────────

    @app_commands.command(name="userinfo", description="Show detailed info about a user")
    @app_commands.describe(member="Member to look up (defaults to you)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user

        STATUS = {
            discord.Status.online: "🟢 Online",
            discord.Status.idle: "🟡 Idle",
            discord.Status.dnd: "🔴 Do Not Disturb",
            discord.Status.offline: "⚫ Offline",
        }

        activity_str = "None"
        if member.activity:
            a = member.activity
            if isinstance(a, discord.Game):
                activity_str = f"Playing **{a.name}**"
            elif isinstance(a, discord.Streaming):
                activity_str = f"Streaming **{a.name}**"
            elif isinstance(a, discord.Listening):
                activity_str = f"Listening to **{a.title}** by {a.artist}"
            elif isinstance(a, discord.Watching):
                activity_str = f"Watching **{a.name}**"
            elif isinstance(a, discord.CustomActivity):
                activity_str = str(a) or "Custom status"
            else:
                activity_str = str(getattr(a, "name", a))

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]

        color = member.color if member.color.value else discord.Color(0x5865F2)
        embed = discord.Embed(title=f"User Info — {member}", color=color)
        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(name="Username", value=str(member), inline=True)
        embed.add_field(name="Display Name", value=member.display_name, inline=True)
        embed.add_field(name="ID", value=str(member.id), inline=True)

        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Status", value=STATUS.get(member.status, "Unknown"), inline=True)
        embed.add_field(name="Activity", value=activity_str, inline=True)

        created_ts = discord.utils.format_dt(member.created_at, "F")
        created_rel = discord.utils.format_dt(member.created_at, "R")
        embed.add_field(name="Account Created", value=f"{created_ts} ({created_rel})", inline=False)

        if member.joined_at:
            joined_ts = discord.utils.format_dt(member.joined_at, "F")
            joined_rel = discord.utils.format_dt(member.joined_at, "R")
            embed.add_field(name="Joined Server", value=f"{joined_ts} ({joined_rel})", inline=False)

        if member.nick:
            embed.add_field(name="Nickname", value=member.nick, inline=True)

        if member.premium_since:
            embed.add_field(
                name="Boosting Since",
                value=discord.utils.format_dt(member.premium_since, "F"),
                inline=True,
            )

        if roles:
            role_str = " ".join(roles)
            if len(role_str) > 1024:
                role_str = f"{len(roles)} roles"
            embed.add_field(name=f"Roles [{len(roles)}]", value=role_str, inline=False)

        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    # ── /serverinfo ───────────────────────────────────────────────────────────

    @app_commands.command(name="serverinfo", description="Show detailed info about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild

        embed = discord.Embed(
            title=g.name,
            description=g.description or "",
            color=0x5865F2,
        )
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        if g.banner:
            embed.set_image(url=g.banner.url)

        embed.add_field(name="Server ID", value=str(g.id), inline=True)
        embed.add_field(
            name="Owner",
            value=g.owner.mention if g.owner else "Unknown",
            inline=True,
        )
        embed.add_field(name="Locale", value=str(g.preferred_locale), inline=True)

        created_ts = discord.utils.format_dt(g.created_at, "F")
        created_rel = discord.utils.format_dt(g.created_at, "R")
        embed.add_field(name="Created", value=f"{created_ts} ({created_rel})", inline=False)

        bots = sum(1 for m in g.members if m.bot)
        humans = (g.member_count or 0) - bots
        embed.add_field(
            name=f"Members [{g.member_count}]",
            value=f"Humans: {humans}\nBots: {bots}",
            inline=True,
        )

        embed.add_field(
            name=f"Channels [{len(g.text_channels) + len(g.voice_channels)}]",
            value=(
                f"Text: {len(g.text_channels)}\n"
                f"Voice: {len(g.voice_channels)}\n"
                f"Categories: {len(g.categories)}"
            ),
            inline=True,
        )

        embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        embed.add_field(name="Emojis", value=f"{len(g.emojis)}/{g.emoji_limit}", inline=True)
        embed.add_field(name="Stickers", value=f"{len(g.stickers)}/{g.sticker_limit}", inline=True)

        boosts = g.premium_subscription_count or 0
        embed.add_field(
            name="Boost",
            value=f"Level {g.premium_tier}  ({boosts} boosts)",
            inline=True,
        )

        VERIFY = {
            discord.VerificationLevel.none: "None",
            discord.VerificationLevel.low: "Low",
            discord.VerificationLevel.medium: "Medium",
            discord.VerificationLevel.high: "High",
            discord.VerificationLevel.highest: "Highest",
        }
        embed.add_field(
            name="Verification", value=VERIFY.get(g.verification_level, "Unknown"), inline=True
        )

        NOTIF = {
            discord.NotificationLevel.all_messages: "All Messages",
            discord.NotificationLevel.only_mentions: "Only Mentions",
        }
        embed.add_field(
            name="Default Notifications",
            value=NOTIF.get(g.default_notifications, "Unknown"),
            inline=True,
        )

        if g.features:
            feat_str = ", ".join(f.replace("_", " ").title() for f in g.features)
            embed.add_field(name="Features", value=feat_str[:1024], inline=False)

        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalCommandsCog(bot))
