import asyncio

import discord
from discord.ext import commands

from config import TOKEN
from database import Database

COGS = [
    "cogs.panel",
    "cogs.development",
    "cogs.global_commands",
    "cogs.roles",
    "cogs.co_chat",
]


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.all(),
            help_command=None,
        )
        self.db = Database()

    async def setup_hook(self):
        await self.db.init()

        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f"[+] Loaded {cog}")
            except Exception as exc:
                print(f"[-] Failed to load {cog}: {exc}")

        # Global sync — single source of truth for all commands.
        await self.tree.sync()
        print("[+] Slash commands synced globally.")

    async def on_ready(self):
        print(f"[+] Logged in as {self.user}  (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the server"
            )
        )
        # Wipe any guild-level command registrations that cause duplicates.
        # Pushing an empty list to each guild removes old guild-specific copies
        # so only the global commands remain visible.
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"[+] Cleared guild commands: {guild.name}")
            except Exception as e:
                print(f"[-] Could not clear guild commands for {guild.name}: {e}")

    async def close(self):
        await self.db.close()
        await super().close()


async def main():
    async with Bot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
