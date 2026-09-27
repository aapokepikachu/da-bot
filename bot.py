"""
Da Bot - a simple, single-server Discord bot.

Features:
- Slash commands (/creator, /join, /help) using discord.py 2.x
- A persistent "Interested" button on /join messages that survives bot restarts
  WITHOUT a database (the author's user ID is encoded in the button's custom_id)
- A tiny built-in HTTP server (aiohttp) so this can run as a Render Free Web Service
- No database, no local persistent files -- everything is derived from
  environment variables and Discord interaction data.

Run locally:
    python bot.py

Required environment variables (see .env.example):
    DISCORD_TOKEN       - your bot's token (KEEP SECRET)
    CREATOR_USER_ID      - your Discord user ID, used for the /creator link
    ALLOWED_GUILD_ID     - (optional) restrict the bot to a single server
    PORT                 - (optional) HTTP port, Render sets this automatically
"""

import asyncio
import logging
import os

import discord
from aiohttp import web
from discord import app_commands

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

# Custom status shown under the bot's name in the member list (right sidebar).
# Discord calls this a "custom status" activity; there's no separate way for
# a bot to set an "About Me" bio via the API, so this is the closest
# equivalent and it's visible right where you want it.
BOT_CUSTOM_STATUS = "I am in Da Game GNG server! AAPoke made me!!"

# ---------------------------------------------------------------------------
# Auto-response architecture (kept for future extensibility)
# Add new automatic text responses here without touching the rest of the bot.
# NOTE: this requires the privileged "Message Content" intent to be enabled
# both in code (intents.message_content = True below) and in the Discord
# Developer Portal. It is left OFF by default since slash commands don't
# need it and nothing currently uses this dict.
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
intents.guilds = True
# Message Content is a privileged intent that is NOT needed for slash
# commands or button interactions -- only for reading plain "?" style
# text commands. Leave this False unless you actually populate
# AUTO_RESPONSES above, in which case set it to True here AND enable
# "Message Content Intent" in the Developer Portal's Bot tab.
intents.message_content = False


class DaBot(discord.Client):
    """A plain Client (not commands.Bot) since we only use slash commands --
    this avoids discord.py's "Message Content intent is missing" warning,
    which is specific to the prefix-command-oriented commands.Bot class and
    doesn't apply to us."""

    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if ALLOWED_GUILD_ID:
            guild_obj = discord.Object(id=ALLOWED_GUILD_ID)
            # Copies globally-defined commands into this guild and syncs
            # them there specifically, which makes them show up almost
            # instantly (a global sync can take up to an hour to propagate).
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info("Synced slash commands to guild %s", ALLOWED_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info(
                "Synced slash commands globally (can take up to an hour to "
                "appear everywhere; set ALLOWED_GUILD_ID for instant sync)."
            )


bot = DaBot()


class JoinView(discord.ui.View):
    """View shown under a /join message.

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
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="creator", description="Shows who made Da Bot.")
async def creator_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    if not CREATOR_USER_ID:
        await interaction.response.send_message(
            "aapoke made me (creator profile link is not configured)"
        )
        return

    profile_url = f"https://discord.com/users/{CREATOR_USER_ID}"
    # NOTE: Discord only renders [text](url) markdown links as clickable
    # inside embeds, not in plain message content, so we use an embed here.
    embed = discord.Embed(description=f"[aapoke]({profile_url}) made me")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="join", description="Look for players for a game.")
@app_commands.describe(
    game="Which game are you looking for players for?",
    message="Optional extra message to include (e.g. 'Need 2 more players')",
)
@app_commands.choices(
    game=[
        app_commands.Choice(name="VBL", value="vbl"),
        app_commands.Choice(name="Minecraft", value="minecraft"),
    ]
)
async def join_slash(
    interaction: discord.Interaction,
    game: app_commands.Choice[str],
    message: str | None = None,
):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    custom_message = (message or "").strip()
    # Safety cap so a huge paste can't break the message (Discord's own limit
    # is 2000 chars per message; this keeps well under that).
    if len(custom_message) > 300:
        custom_message = custom_message[:300]

    author_mention = interaction.user.mention

    if game.value == "vbl":
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

    view = JoinView(interaction.user.id)
    await interaction.response.send_message(content, view=view)


@bot.tree.command(name="help", description="Shows all Da Bot commands.")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="Da Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="/creator", value="Shows who made Da Bot.", inline=False)
    embed.add_field(
        name="/join game:VBL",
        value="Looks for VBL players.",
        inline=False,
    )
    embed.add_field(
        name="/join game:Minecraft",
        value="Looks for Minecraft players.",
        inline=False,
    )
    embed.add_field(
        name="/join game:<game> message:<text>",
        value="Looks for players and includes an optional custom message.",
        inline=False,
    )
    embed.add_field(name="/help", value="Shows this command list.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Slash command error handling
# ---------------------------------------------------------------------------
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    logger.error("Slash command error in '%s': %s", interaction.command, error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "⚠️ Something went wrong running that command.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ Something went wrong running that command.", ephemeral=True
            )
    except Exception:
        # Never let error reporting itself crash the bot.
        pass


# ---------------------------------------------------------------------------
# Auto-responses (only fires if you enable Message Content intent above and
# populate AUTO_RESPONSES; harmless no-op otherwise)
# ---------------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not AUTO_RESPONSES:
        return

    if message.guild is not None and not is_allowed_guild(message.guild.id):
        return

    content_lower = message.content.strip().lower()
    if content_lower in AUTO_RESPONSES:
        await message.channel.send(AUTO_RESPONSES[content_lower])


# ---------------------------------------------------------------------------
# Persistent button handling (no database)
#
# This raw on_interaction listener fires for every interaction the bot
# receives, regardless of whether a matching View object exists in memory,
# so it keeps working after restarts as long as the message + button exist.
# ---------------------------------------------------------------------------
@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if interaction.type != discord.InteractionType.component:
            return  # Slash commands are handled by the tree, not here.

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

    # Set an online status with a custom status message, so the bot shows
    # up as online with a bio-like line under its name in the member list.
    # Safe to call again on every reconnect (idempotent).
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.CustomActivity(name=BOT_CUSTOM_STATUS),
        )
    except Exception:
        logger.exception("Failed to set bot presence/status")

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
