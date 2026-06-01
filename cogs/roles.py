import discord
from discord import app_commands
from discord.ext import commands

from config import DEVELOPER_IDS


def is_developer():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in DEVELOPER_IDS
    return app_commands.check(predicate)


class RolesCog(commands.Cog, name="Roles"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Only bot developers can use role assignment commands.", ephemeral=True
                )

    assignrole = app_commands.Group(
        name="assignrole",
        description="Assign key server roles for the bot to use",
    )

    @assignrole.command(name="management", description="Set the management role (used for Caleb tickets)")
    @app_commands.describe(role="The management role")
    @is_developer()
    async def assign_management(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.client.db.set_management_role(interaction.guild.id, role.id)
        embed = discord.Embed(
            title="Management Role Set",
            description=(
                f"Management role set to {role.mention}.\n"
                "This role will have access to **Caleb** ticket channels.\n"
                "Members with this role can also use `/config`."
            ),
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @assignrole.command(name="staff", description="Set the staff role (used for Support & General tickets)")
    @app_commands.describe(role="The staff role")
    @is_developer()
    async def assign_staff(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.client.db.set_staff_role(interaction.guild.id, role.id)
        embed = discord.Embed(
            title="Staff Role Set",
            description=(
                f"Staff role set to {role.mention}.\n"
                "This role will have access to **Support** and **General** ticket channels."
            ),
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))
