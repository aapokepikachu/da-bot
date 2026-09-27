"""
Da Bot - a simple, single-server Discord bot.

Features:
- Prefix commands (?creator, ?join, ?help) using discord.py 2.x
- A persistent "Interested" button on ?join messages that survives bot restarts
  WITHOUT a database (the author's user ID is encoded in the button's custom_id)
- A tiny built-in HTTP server (aiohttp) so this can run as a Render Free Web Service
- No database, no local persistent files -- everything is derived from
  environment variables and Discord interaction data.

Run locally:
    python bot.py

Required environment variables (see .env.example):
    DISCORD_TOKEN       - your bot's token (KEEP SECRET)
    CREATOR_USER_ID      - your Discord user ID, used for the ?creator link
    ALLOWED_GUILD_ID     - (optional) restrict the bot to a single server
    PORT                 - (optional) HTTP port, Render sets this automatically
"""

import asyncio
import logging
import os
import re

import discord
from aiohttp import web
from discord.ext import commands

# python-dotenv lets us load a local .env file for local development.
# On Render, environment variables are provided directly by the platform,
# so load_dotenv() simply does nothing there (no .env file exists).
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("da_bot")

# ---------------------------------------------------------------------------
# Configuration (from environment variables only -- never hardcode secrets)
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CREATOR_USER_ID = os.getenv("CREATOR_USER_ID", "").strip()

_raw_allowed_guild = os.getenv("ALLOWED_GUILD_ID", "").strip()
ALLOWED_GUILD_ID = int(_raw_allowed_guild) if _raw_allowed_guild.isdigit() else None

PORT = int(os.getenv("PORT", "10000"))

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is required. "
        "Set it in your .env file (local) or in Render's Environment settings."
    )

# Role IDs are fixed by the spec and must never be replaced with role names.
VBL_ROLE_MENTION = "<@&1532265584069185597>"
MINECRAFT_ROLE_MENTION = "<@&1536962762952146944>"

# Prefix used to identify our "Interested" button clicks inside custom_id.
JOIN_BUTTON_PREFIX = "join_interested:"

JOIN_USAGE = (
    "**Usage:** `?join game:<vbl|minecraft> message:<optional message>`\n"
    "**Example:** `?join game:vbl message:Need 2 more players`\n"
    "Supported games: **VBL** and **Minecraft**."
)

# ---------------------------------------------------------------------------
# Auto-response architecture (section 12)
# Add new automatic text responses here without touching the rest of the bot.
# Keys are matched against the lowercased, stripped message content.
# Left empty by default -- only add entries the user actually wants.
# ---------------------------------------------------------------------------
AUTO_RESPONSES: dict[str, str] = {
    # "hello": "Hello!",
    # "dino dance": "https://example.com/dino.gif",
}

# ---------------------------------------------------------------------------
# Guild restriction helper
# ---------------------------------------------------------------------------
def is_allowed_guild(guild_id: int | None) -> bool:
    """Return True if commands/interactions from this guild should be handled.

    If ALLOWED_GUILD_ID is not set, every guild is allowed.
    """
    if ALLOWED_GUILD_ID is None:
        return True
    return guild_id == ALLOWED_GUILD_ID


# ---------------------------------------------------------------------------
# Discord bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # Required (privileged) to read "?..." prefix commands
intents.guilds = True

bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)


class JoinView(discord.ui.View):
    """View shown under a ?join message.

    The button has NO callback of its own -- all handling happens in the
    global on_interaction listener below. That is what lets this keep working
    after a bot restart: we don't need to re-register this exact View object,
    we only need to read the author's ID back out of the custom_id whenever
    Discord sends us the click.
    """

    def __init__(self, author_id: int):
        super().__init__(timeout=None)  # timeout=None -> button never expires
        button = discord.ui.Button(
            label="Interested",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"{JOIN_BUTTON_PREFIX}{author_id}",
        )
        self.add_item(button)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.command(name="creator")
async def creator_command(ctx: commands.Context):
    """Shows who made Da Bot, with a clickable link to their Discord profile."""
    if not CREATOR_USER_ID:
        await ctx.send("aapoke made me (creator profile link is not configured)")
        return

    profile_url = f"https://discord.com/users/{CREATOR_USER_ID}"
    # NOTE: Discord only renders [text](url) markdown links as clickable
    # inside embeds, not in plain message content, so we use an embed here.
    embed = discord.Embed(description=f"[aapoke]({profile_url}) made me")
    await ctx.send(embed=embed)


@bot.command(name="join")
async def join_command(ctx: commands.Context, *, args: str = None):
    """Looks for players. Usage: ?join game:<vbl|minecraft> message:<optional>"""
    if not is_allowed_guild(ctx.guild.id if ctx.guild else None):
        return  # This server isn't the configured one -- ignore quietly.

    if not args:
        await ctx.send(JOIN_USAGE)
        return

    game_match = re.search(r"game:\s*(\S+)", args, re.IGNORECASE)
    if not game_match:
        await ctx.send(JOIN_USAGE)
        return

    game = game_match.group(1).strip().lower()
    if game not in ("vbl", "minecraft"):
        await ctx.send(
            "❌ Unsupported game. Supported games are: **VBL** and **Minecraft**.\n"
            f"{JOIN_USAGE}"
        )
        return

    # Capture everything after "message:" to the end of the input, so
    # multi-word custom messages are preserved as-is.
    message_match = re.search(r"message:\s*(.*)", args, re.IGNORECASE | re.DOTALL)
    custom_message = message_match.group(1).strip() if message_match else ""

    # Safety cap so a huge paste can't break the message (Discord's own limit
    # is 2000 chars per message; this keeps well under that).
    if len(custom_message) > 300:
        custom_message = custom_message[:300]

    author_mention = ctx.author.mention

    if game == "vbl":
        content = (
            f"🏐 **{author_mention}** is looking for players!\n"
            f"{VBL_ROLE_MENTION}\n"
            f"Join the lobby now!"
        )
    else:  # minecraft
        content = (
            f"**{author_mention}** is looking for players!\n"
            f"{MINECRAFT_ROLE_MENTION}\n"
            f"Join the Server now!"
        )

    if custom_message:
        content += f"\n{custom_message}"

    view = JoinView(ctx.author.id)
    await ctx.send(content, view=view)


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Shows the list of available commands."""
    embed = discord.Embed(title="Da Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="?creator", value="Shows who made Da Bot.", inline=False)
    embed.add_field(name="?join game:vbl", value="Looks for VBL players.", inline=False)
    embed.add_field(
        name="?join game:minecraft", value="Looks for Minecraft players.", inline=False
    )
    embed.add_field(
        name="?join game:vbl message:your message",
        value="Looks for players and includes an optional custom message.",
        inline=False,
    )
    embed.add_field(name="?help", value="Shows this command list.", inline=False)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Auto-responses + command dispatch
# ---------------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is not None and not is_allowed_guild(message.guild.id):
        return  # Wrong server -- ignore everything, including auto-responses.

    content_lower = message.content.strip().lower()
    if content_lower in AUTO_RESPONSES:
        await message.channel.send(AUTO_RESPONSES[content_lower])

    # Always let discord.py process prefix commands too.
    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Persistent button handling (no database)
#
# We deliberately do NOT rely on the View's own callback dispatch for
# cross-restart persistence, because that requires re-registering a View
# with matching custom_ids at startup -- which we can't do without knowing
# every past join-author ID (i.e. without a database). Instead, this raw
# on_interaction listener fires for every interaction the bot receives,
# regardless of whether a matching View object exists in memory, so it
# keeps working after restarts as long as the message + button still exist.
# ---------------------------------------------------------------------------
@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        if not custom_id.startswith(JOIN_BUTTON_PREFIX):
            return

        if not is_allowed_guild(interaction.guild_id):
            await interaction.response.send_message(
                "This bot is not configured for this server.", ephemeral=True
            )
            return

        raw_id = custom_id[len(JOIN_BUTTON_PREFIX):]
        try:
            author_id = int(raw_id)
        except ValueError:
            logger.error("Received malformed join button custom_id: %r", custom_id)
            await interaction.response.send_message(
                "Sorry, I couldn't read who posted this request.", ephemeral=True
            )
            return

        clicker_id = interaction.user.id

        if clicker_id == author_id:
            await interaction.response.send_message(
                "You can't join your own request!", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"<@{clicker_id}> is joining <@{author_id}>"
        )

    except Exception:
        logger.exception("Unexpected error handling a button interaction")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong handling that button.", ephemeral=True
                )
        except Exception:
            # If we can't even send the error message, just swallow it --
            # we must never let a button click crash the bot.
            pass


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    # Unknown commands and wrong-server check failures are ignored quietly.
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing arguments.\n{JOIN_USAGE}")
        return

    if isinstance(error, commands.CommandInvokeError):
        logger.error("Command '%s' raised an error: %s", ctx.command, error.original)
    else:
        logger.error("Command '%s' raised an error: %s", ctx.command, error)

    await ctx.send("⚠️ Something went wrong running that command.")


# ---------------------------------------------------------------------------
# Lifecycle logging
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Connected to %d guild(s).", len(bot.guilds))
    if ALLOWED_GUILD_ID:
        logger.info("Restricted to guild ID: %s", ALLOWED_GUILD_ID)
    else:
        logger.info("ALLOWED_GUILD_ID not set -- bot will respond in any guild it's in.")
    logger.info("Da Bot is ready.")


@bot.event
async def on_disconnect():
    logger.warning("Disconnected from Discord Gateway. discord.py will try to reconnect.")


@bot.event
async def on_resumed():
    logger.info("Discord Gateway session resumed.")


# ---------------------------------------------------------------------------
# Tiny HTTP server so Render's Free Web Service sees an open port.
# Runs concurrently with the Discord Gateway connection in the same process.
# ---------------------------------------------------------------------------
async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="Da Bot is running.")


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_http_server() -> None:
    app = web.Application()
    app.add_routes(
        [
            web.get("/", handle_root),
            web.get("/health", handle_health),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("HTTP health server listening on 0.0.0.0:%d", PORT)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    logger.info("Starting Da Bot...")
    await start_http_server()

    logger.info("Connecting to Discord...")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down Da Bot (KeyboardInterrupt).")
