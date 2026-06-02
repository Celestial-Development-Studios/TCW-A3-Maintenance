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
    "cogs.co_chat"
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

        # Global sync — propagates to all servers (up to 1 hour on Discord's side)
        await self.tree.sync()
        print("[+] Slash commands synced globally.")

    async def on_ready(self):
        print(f"[+] Logged in as {self.user}  (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the server"
            )
        )
        # Guild sync — copies global commands to every server instantly
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"[+] Guild sync: {guild.name}")
            except Exception as e:
                print(f"[-] Guild sync failed for {guild.name}: {e}")

    async def close(self):
        await self.db.close()
        await super().close()


async def main():
    async with Bot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
