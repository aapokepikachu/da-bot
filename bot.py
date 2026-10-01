"""
Milo - a simple, single-server, cat-themed Discord bot.

Features:
- Slash commands (/aboutme, /join, /ping, /hi, /8ball, /poll, /coinflip,
  /roll, /serverinfo, /userinfo, /help) using discord.py 2.x -- these all
  show up in Discord's "/" command list
- A persistent "Interested" button on /join messages that survives bot
  restarts WITHOUT a database (the author's user ID is encoded in the
  button's custom_id)
- A cat-themed welcome message when new members join
- A self-assign roles system (button-based, no database) posted via the
  hidden "?rolemenu" command
- Two hidden, Manager-role-only text commands: "?send" (post an announcement
  as Milo) and "?rolemenu" (post the self-assign roles menu) -- both are
  intentionally plain text commands, not slash commands, so neither ever
  shows up in Discord's "/" command list and neither is listed in /help
- A tiny built-in HTTP server (aiohttp) so this can run as a Render Free Web Service
- No database, no local persistent files -- everything is derived from
  environment variables and Discord interaction/event data.

Run locally:
    python bot.py

See .env.example for every environment variable this bot uses.
"""

import asyncio
import logging
import os
import random
import time

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
logger = logging.getLogger("milo")

# ---------------------------------------------------------------------------
# Configuration (from environment variables only -- never hardcode secrets)
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CREATOR_USER_ID = os.getenv("CREATOR_USER_ID", "").strip()

# The server's actual custom :Milo: emoji. Defaults to the real one you gave
# us, but stays overridable via env in case you ever move servers.
MILO_EMOJI = os.getenv("MILO_EMOJI", "<:Milo:1554818642804482078>").strip()

_raw_allowed_guild = os.getenv("ALLOWED_GUILD_ID", "").strip()
ALLOWED_GUILD_ID = int(_raw_allowed_guild) if _raw_allowed_guild.isdigit() else None

# Optional: channel where new-member welcome messages get posted.
# If not set, the welcome feature is simply skipped (logged once at startup).
_raw_welcome_channel = os.getenv("WELCOME_CHANNEL_ID", "").strip()
WELCOME_CHANNEL_ID = int(_raw_welcome_channel) if _raw_welcome_channel.isdigit() else None

# Optional: the one role allowed to use the hidden "?send" command AND to
# post the self-assign role menu via /rolemenu.
# If not set, both of those features are disabled.
_raw_manager_role = os.getenv("MANAGER_ROLE_ID", "").strip()
MANAGER_ROLE_ID = int(_raw_manager_role) if _raw_manager_role.isdigit() else None

# Optional: self-assignable roles shown by /rolemenu, as
# "ROLE_ID|Label|Emoji,ROLE_ID|Label|Emoji,...". Emoji is optional per entry.
# A pipe delimiter is used (not a colon) so custom emoji codes like
# <:Gamer:123456789012345678>, which already contain colons, parse cleanly.
_raw_self_roles = os.getenv("SELF_ROLES", "").strip()
SELF_ROLES: list[tuple[int, str, str | None]] = []
if _raw_self_roles:
    for _entry in _raw_self_roles.split(","):
        _entry = _entry.strip()
        if not _entry:
            continue
        _parts = _entry.split("|")
        if len(_parts) < 2 or not _parts[0].strip().isdigit():
            logger.warning("Skipping malformed SELF_ROLES entry: %r", _entry)
            continue
        _role_id = int(_parts[0].strip())
        _label = _parts[1].strip()
        _emoji = _parts[2].strip() if len(_parts) > 2 and _parts[2].strip() else None
        SELF_ROLES.append((_role_id, _label, _emoji))
    if len(SELF_ROLES) > 25:
        logger.warning(
            "SELF_ROLES has %d entries; only the first 25 fit on one button menu.",
            len(SELF_ROLES),
        )
        SELF_ROLES = SELF_ROLES[:25]

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

# Prefix used to identify self-assign role button clicks inside custom_id.
SELF_ROLE_BUTTON_PREFIX = "self_role:"

# Custom status shown under the bot's name in the member list (right sidebar).
BOT_CUSTOM_STATUS = "I am in Da Game GNG server! AAPoke made me!!"

# Simple in-memory cooldown for /join, so one person can't spam it.
# Resets on restart -- that's fine, it's just spam prevention, not data
# that needs to survive.
JOIN_COOLDOWN_SECONDS = 30
_join_cooldowns: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Cat-themed content pools (no database -- just plain Python lists)
# ---------------------------------------------------------------------------
HI_RESPONSES = [
    "{emoji} Hi! *stretches paws* How are you doing today?",
    "{emoji} Meow! Just woke up from a nap — how's it going?",
    "{emoji} Hey there, friend! Milo's tail is wagging — how are you?",
    "{emoji} *purrs softly* Hi! Hope your day's been as cozy as a sunbeam.",
    "{emoji} Hiya! Chasing a laser pointer earlier, now I'm all yours — how are you?",
    "{emoji} Hi hi! Whiskers twitching with excitement to chat. How are you?",
    "{emoji} Meow meow! Milo reporting in — how's your day treating you?",
    "{emoji} *kneads the couch* Hi! What's new with you?",
    "{emoji} Hey! Just knocked something off a shelf for fun. How are you doing?",
    "{emoji} Hi there! Curled up in a sunny spot, ready to chat. How are you?",
    "{emoji} Prrrt? Oh hi! Didn't see you there. How's it going?",
    "{emoji} Hi! Milo here, mid-zoomies. How are you feeling today?",
    "{emoji} *slow blink* Hi friend. How's your day been so far?",
    "{emoji} Hey hey! Just finished a nap in a cardboard box. How are you?",
    "{emoji} Meow! Batting at some yarn but always time to say hi. How are you?",
    "{emoji} Hi! Sniffing around for treats and good vibes. How's it going with you?",
    "{emoji} *ears perk up* Oh, hi! How are you doing today?",
    "{emoji} Hiya! Milo's on watch duty by the window. How are you?",
    "{emoji} Hey! Just had a snack, feeling great. How about you?",
    "{emoji} Meow-morning! (or afternoon, or evening — who's counting) How are you?",
    "{emoji} Hi! Practicing my best cat loaf pose. How's your day going?",
    "{emoji} *tail flick* Hi there! What's up with you today?",
    "{emoji} Hi! Just supervised some very important box-sitting. How are you?",
    "{emoji} Meow! Milo's here, paws crossed you're having a great day.",
    "{emoji} Hey friend! Chasing dust motes in the sunlight. How are you?",
]

MAGIC_8BALL_RESPONSES = [
    "It is certain.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
]

WELCOME_MESSAGES = [
    "{emoji} Yo GUYS! {user} just joined — everybody say hi and give 'em a paw-five! 🐾",
    "{emoji} *perks up ears* Look who wandered in — welcome, {user}! Make yourself at home.",
    "{emoji} Meow! {user} has joined the pack — say hi, everyone!",
    "{emoji} New friend alert! {user} just walked in, go say hi! 🐾",
    "{emoji} *stretches and pads over* Welcome {user}! Milo's already claiming you as a new best friend.",
    "{emoji} Yo GUYS! {user} slid into the server — roll out the welcome mat!",
    "{emoji} Hiss— just kidding! Welcome {user}, glad you're here. Say hi, everyone!",
    "{emoji} A new human (or cat?) has appeared: {user}! Everyone say hi!",
    "{emoji} *purrs loudly* Welcome aboard, {user}! Milo approves.",
    "{emoji} Yo GUYS! {user} just joined — someone get the treats, we've got a guest!",
]

# ---------------------------------------------------------------------------
# Auto-response architecture (kept for future extensibility)
# Requires the privileged "Message Content" intent, which is now enabled
# below because the hidden "?send" command also needs it.
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
# Message Content is a privileged intent, now required because the hidden
# "?send" command reads plain message text. Must also be enabled in the
# Discord Developer Portal's Bot tab.
intents.message_content = True
# Server Members is a privileged intent, required so on_member_join fires
# reliably for the welcome message. Must also be enabled in the Developer
# Portal's Bot tab.
intents.members = True


class MiloClient(discord.Client):
    """discord.Client subclass so we can sync the slash command tree once at
    startup, while still handling the one hidden text command ("?send") via
    a normal on_message listener."""

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

        if not WELCOME_CHANNEL_ID:
            logger.info("WELCOME_CHANNEL_ID not set -- welcome messages are disabled.")
        if not MANAGER_ROLE_ID:
            logger.info("MANAGER_ROLE_ID not set -- the hidden ?send command is disabled.")


bot = MiloClient()


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
            emoji="✔️",  # A plain check mark reads cleaner than a boxed emoji
            style=discord.ButtonStyle.success,  # success = Discord's green button style
            custom_id=f"{JOIN_BUTTON_PREFIX}{author_id}",
        )
        self.add_item(button)


class SelfRoleView(discord.ui.View):
    """Posted once by /rolemenu. Each button's custom_id encodes the role ID
    directly (e.g. "self_role:123456789012345678"), so clicking it keeps
    working forever -- even after a bot restart -- with no database, using
    the exact same trick as the Interested button above."""

    def __init__(self):
        super().__init__(timeout=None)  # timeout=None -> buttons never expire
        for role_id, label, emoji in SELF_ROLES:
            button = discord.ui.Button(
                label=label,
                emoji=emoji,  # None is fine -- Discord just shows no emoji
                style=discord.ButtonStyle.secondary,
                custom_id=f"{SELF_ROLE_BUTTON_PREFIX}{role_id}",
            )
            self.add_item(button)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="aboutme", description="Learn about Milo.")
async def aboutme_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    if CREATOR_USER_ID:
        profile_url = f"https://discord.com/users/{CREATOR_USER_ID}"
        # NOTE: Discord only renders [text](url) markdown links as clickable
        # inside embeds, not in plain message content, so we use an embed here.
        creator_line = f"[AAPoke]({profile_url}) is my creator"
    else:
        creator_line = "AAPoke is my creator (profile link not configured)"

    description = (
        f"Hi, I'm Milo! {MILO_EMOJI} A cat-themed group-management bot, through and through.\n\n"
        f"Here's what I get up to around here:\n"
        f"• 🏐 Rounding up players for game lobbies with `/join`\n"
        f"• 🎭 Letting you pick your own roles with `/rolemenu`\n"
        f"• 🐾 Saying hi, flipping coins, rolling dice, answering the magic 8-ball, and running polls\n"
        f"• 👋 Welcoming new members the moment they join\n\n"
        f"I run on pure vibes and whatever's happening right now — no database, no memory banks, "
        f"just me, my whiskers, and the occasional nap.\n\n"
        f"{creator_line}"
    )
    embed = discord.Embed(description=description, color=discord.Color.green())
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

    # Simple in-memory cooldown so one person can't spam /join.
    now = time.monotonic()
    last_used = _join_cooldowns.get(interaction.user.id)
    if last_used is not None and (now - last_used) < JOIN_COOLDOWN_SECONDS:
        remaining = round(JOIN_COOLDOWN_SECONDS - (now - last_used))
        await interaction.response.send_message(
            f"⏳ Please wait {remaining}s before using `/join` again.", ephemeral=True
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
    # Explicitly allow role mentions so the ping actually notifies members
    # of that role (not just a highlighted, silent mention). If members
    # still aren't notified after this, it's a server-side permission
    # issue -- see the README's "Role isn't notifying anyone" section.
    await interaction.response.send_message(
        content,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
    )
    _join_cooldowns[interaction.user.id] = now


@bot.tree.command(name="ping", description="Check Milo's latency.")
async def ping_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency: **{latency_ms}ms**")


@bot.tree.command(name="hi", description="Say hi to Milo!")
async def hi_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    response = random.choice(HI_RESPONSES).format(emoji=MILO_EMOJI)
    await interaction.response.send_message(response)


@bot.tree.command(name="8ball", description="Ask Milo the magic 8-ball a question.")
@app_commands.describe(question="What do you want to ask?")
async def eightball_slash(interaction: discord.Interaction, question: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    answer = random.choice(MAGIC_8BALL_RESPONSES)
    await interaction.response.send_message(
        f"🎱 **Q:** {question}\n**A:** {answer}"
    )


NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


@bot.tree.command(name="poll", description="Post a quick poll with up to 5 options.")
@app_commands.describe(
    question="The poll question",
    option1="First option",
    option2="Second option",
    option3="Third option (optional)",
    option4="Fourth option (optional)",
    option5="Fifth option (optional)",
)
async def poll_slash(
    interaction: discord.Interaction,
    question: str,
    option1: str,
    option2: str,
    option3: str | None = None,
    option4: str | None = None,
    option5: str | None = None,
):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    options = [o for o in [option1, option2, option3, option4, option5] if o]
    lines = [f"{NUMBER_EMOJIS[i]} {opt}" for i, opt in enumerate(options)]
    embed = discord.Embed(
        title=f"📊 {question}",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Poll started by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)
    sent_message = await interaction.original_response()
    for i in range(len(options)):
        try:
            await sent_message.add_reaction(NUMBER_EMOJIS[i])
        except discord.HTTPException:
            logger.warning("Failed to add reaction %s to poll message", NUMBER_EMOJIS[i])


@bot.tree.command(name="coinflip", description="Flip a coin.")
async def coinflip_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    result = random.choice(["Heads", "Tails"])
    await interaction.response.send_message(f"🪙 The coin lands on... **{result}**!")


@bot.tree.command(name="roll", description="Roll some dice.")
@app_commands.describe(
    sides="How many sides per die? (default 6)",
    count="How many dice to roll? (default 1, max 20)",
)
async def roll_slash(
    interaction: discord.Interaction,
    sides: app_commands.Range[int, 2, 1000] = 6,
    count: app_commands.Range[int, 1, 20] = 1,
):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    rolls = [random.randint(1, sides) for _ in range(count)]
    rolls_text = ", ".join(str(r) for r in rolls)
    total_text = f" (total: {sum(rolls)})" if count > 1 else ""
    await interaction.response.send_message(
        f"🎲 Rolled {count}d{sides}: **{rolls_text}**{total_text}"
    )


@bot.tree.command(name="serverinfo", description="Show info about this server.")
async def serverinfo_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works inside a server.", ephemeral=True
        )
        return
    if not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    guild = interaction.guild
    embed = discord.Embed(title=f"📋 {guild.name}", color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=str(guild.owner) if guild.owner else "Unknown")
    embed.add_field(name="Members", value=str(guild.member_count))
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="D"))
    embed.add_field(name="Roles", value=str(len(guild.roles)))
    embed.add_field(name="Text Channels", value=str(len(guild.text_channels)))
    embed.add_field(name="Voice Channels", value=str(len(guild.voice_channels)))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="Show info about a server member.")
@app_commands.describe(user="Whose info to show (defaults to you)")
async def userinfo_slash(
    interaction: discord.Interaction, user: discord.Member | None = None
):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    member = user or interaction.user
    embed = discord.Embed(title=f"👤 {member.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Username", value=str(member))
    embed.add_field(name="User ID", value=str(member.id))
    embed.add_field(
        name="Account Created", value=discord.utils.format_dt(member.created_at, style="D")
    )
    if isinstance(member, discord.Member) and member.joined_at:
        embed.add_field(
            name="Joined Server", value=discord.utils.format_dt(member.joined_at, style="D")
        )
        top_role = member.top_role.mention if member.top_role else "None"
        embed.add_field(name="Top Role", value=top_role)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Shows all Milo commands.")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"Milo Commands {MILO_EMOJI}",
        description="Your friendly cat-themed group-management bot.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="/aboutme", value="Learn about Milo and who made it.", inline=False
    )
    embed.add_field(name="/join game:VBL", value="Looks for VBL players.", inline=False)
    embed.add_field(
        name="/join game:Minecraft", value="Looks for Minecraft players.", inline=False
    )
    embed.add_field(
        name="/join game:<game> message:<text>",
        value="Looks for players and includes an optional custom message.",
        inline=False,
    )
    embed.add_field(name="/hi", value="Say hi to Milo and get a cat-themed reply.", inline=False)
    embed.add_field(name="/8ball question:<text>", value="Ask the magic 8-ball a question.", inline=False)
    embed.add_field(
        name="/poll question option1 option2 ...",
        value="Posts a poll (up to 5 options) with number-reaction voting.",
        inline=False,
    )
    embed.add_field(name="/coinflip", value="Flips a coin.", inline=False)
    embed.add_field(name="/roll sides:<n> count:<n>", value="Rolls dice.", inline=False)
    embed.add_field(name="/serverinfo", value="Shows info about this server.", inline=False)
    embed.add_field(name="/userinfo user:<member>", value="Shows info about a member.", inline=False)
    embed.add_field(name="/ping", value="Checks Milo's latency.", inline=False)
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
# Hidden "?send" and "?rolemenu" commands + auto-responses
#
# Both are deliberately plain text commands, not slash commands, so neither
# ever appears in Discord's "/" picker and neither is listed in /help. Only
# members with MANAGER_ROLE_ID can use either one. Anyone else typing them
# is silently ignored -- their existence isn't revealed.
# ---------------------------------------------------------------------------
async def _handle_send_command(message: discord.Message) -> None:
    if not MANAGER_ROLE_ID:
        return  # Feature disabled -- no manager role configured.
    if message.guild is None or not isinstance(message.author, discord.Member):
        return  # Only works inside a server, where roles exist.

    author_role_ids = {role.id for role in message.author.roles}
    if MANAGER_ROLE_ID not in author_role_ids:
        return  # Not authorized -- stay silent.

    content_to_send = message.content[len("?send "):].strip()
    if not content_to_send:
        return

    # Try to remove the original command message to keep the channel clean
    # and keep it a bit more "hidden" -- harmless if the bot lacks
    # permission to delete it.
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    await message.channel.send(
        content_to_send,
        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
    )


async def _handle_rolemenu_command(message: discord.Message) -> None:
    if not MANAGER_ROLE_ID:
        return  # Feature disabled -- no manager role configured.
    if message.guild is None or not isinstance(message.author, discord.Member):
        return  # Only works inside a server, where roles exist.

    author_role_ids = {role.id for role in message.author.roles}
    if MANAGER_ROLE_ID not in author_role_ids:
        return  # Not authorized -- stay silent.

    if not SELF_ROLES:
        return  # Nothing configured to post -- stay silent.

    # Clean up the trigger message, same as ?send does.
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    lines = []
    for _role_id, label, emoji in SELF_ROLES:
        prefix = f"{emoji} " if emoji else "• "
        lines.append(f"{prefix}**{label}**")

    embed = discord.Embed(
        title=f"🎭 Pick your roles! {MILO_EMOJI}",
        description=(
            "Click a button below to add a role. Click it again to remove it — "
            "toggle on, toggle off, as many times as you like.\n\n" + "\n".join(lines)
        ),
        color=discord.Color.green(),
    )
    view = SelfRoleView()
    await message.channel.send(embed=embed, view=view)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is not None and not is_allowed_guild(message.guild.id):
        return

    if message.content.startswith("?send "):
        await _handle_send_command(message)
        return

    if message.content.strip().lower() == "?rolemenu":
        await _handle_rolemenu_command(message)
        return

    if not AUTO_RESPONSES:
        return
    content_lower = message.content.strip().lower()
    if content_lower in AUTO_RESPONSES:
        await message.channel.send(AUTO_RESPONSES[content_lower])


# ---------------------------------------------------------------------------
# Cat-themed welcome message for new members (no database -- posted live)
# ---------------------------------------------------------------------------
@bot.event
async def on_member_join(member: discord.Member):
    if not is_allowed_guild(member.guild.id):
        return
    if not WELCOME_CHANNEL_ID:
        return

    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel is None:
        logger.warning(
            "WELCOME_CHANNEL_ID %s not found in guild %s", WELCOME_CHANNEL_ID, member.guild.id
        )
        return

    text = random.choice(WELCOME_MESSAGES).format(emoji=MILO_EMOJI, user=member.mention)
    try:
        await channel.send(text)
    except discord.Forbidden:
        logger.warning("Missing permission to send welcome message in channel %s", WELCOME_CHANNEL_ID)


# ---------------------------------------------------------------------------
# Persistent button handling (no database)
#
# This raw on_interaction listener fires for every interaction the bot
# receives, regardless of whether a matching View object exists in memory,
# so it keeps working after restarts as long as the message + button exist.
# ---------------------------------------------------------------------------
async def _handle_join_interested_click(interaction: discord.Interaction, custom_id: str) -> None:
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

    await interaction.response.send_message(f"<@{clicker_id}> is joining <@{author_id}>")


async def _handle_self_role_click(interaction: discord.Interaction, custom_id: str) -> None:
    if not is_allowed_guild(interaction.guild_id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    raw_id = custom_id[len(SELF_ROLE_BUTTON_PREFIX):]
    try:
        role_id = int(raw_id)
    except ValueError:
        logger.error("Received malformed self-role button custom_id: %r", custom_id)
        await interaction.response.send_message(
            "Sorry, that role button looks broken.", ephemeral=True
        )
        return

    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "This only works inside a server.", ephemeral=True
        )
        return

    role = guild.get_role(role_id)
    if role is None:
        await interaction.response.send_message(
            "That role doesn't exist anymore.", ephemeral=True
        )
        return

    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Self-assign role button (removed)")
            await interaction.response.send_message(f"➖ Removed **{role.name}**.", ephemeral=True)
        else:
            await member.add_roles(role, reason="Self-assign role button (added)")
            await interaction.response.send_message(f"➕ Gave you **{role.name}**! {MILO_EMOJI}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to manage that role -- make sure Milo's role "
            "is above it in Server Settings → Roles, and that Milo has the "
            "Manage Roles permission.",
            ephemeral=True,
        )


@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if interaction.type != discord.InteractionType.component:
            return  # Slash commands are handled by the tree, not here.

        data = interaction.data or {}
        custom_id = data.get("custom_id", "")

        if custom_id.startswith(JOIN_BUTTON_PREFIX):
            await _handle_join_interested_click(interaction, custom_id)
        elif custom_id.startswith(SELF_ROLE_BUTTON_PREFIX):
            await _handle_self_role_click(interaction, custom_id)
        # else: not a button we recognize -- ignore silently.

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

    logger.info("Milo is ready.")


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
    return web.Response(text="Milo is running.")


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
    logger.info("Starting Milo...")
    await start_http_server()

    logger.info("Connecting to Discord...")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down Milo (KeyboardInterrupt).")
