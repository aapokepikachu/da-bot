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

import aiohttp
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
# post the self-assign role menu via ?rolemenu.
# If not set, both of those features are disabled.
_raw_manager_role = os.getenv("MANAGER_ROLE_ID", "").strip()
MANAGER_ROLE_ID = int(_raw_manager_role) if _raw_manager_role.isdigit() else None

# Optional: self-assignable roles shown by ?rolemenu, as
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
        # Shared HTTP client session for the free public APIs behind
        # /catfact, /meow, /quote, /define, /weather, /translate, and the
        # /hug /slap /pat reaction gifs. Created in setup_hook (needs a
        # running event loop) and closed in close() on shutdown.
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        )

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

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        await super().close()


bot = MiloClient()


async def _fetch_json(url: str, **kwargs):
    """GET a URL and return parsed JSON, or None on any failure. Used by all
    the free-API-backed commands below so each one can fail gracefully
    instead of crashing if an external API is slow, down, or rate-limited."""
    try:
        async with bot.session.get(url, **kwargs) as resp:
            if resp.status != 200:
                logger.warning("GET %s returned status %s", url, resp.status)
                return None
            return await resp.json(content_type=None)
    except Exception:
        logger.exception("Failed to fetch %s", url)
        return None


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
    """Posted once by ?rolemenu. Each button's custom_id encodes the role ID
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
        f"• 🎭 Letting you pick your own roles\n"
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


# ---------------------------------------------------------------------------
# Cat Corner: /catfact, /meow, /purr
# ---------------------------------------------------------------------------
PURR_RESPONSES = [
    "{emoji} Purrrrrr~",
    "{emoji} /ᐠ. .ᐟ\\ゝ Meow.",
    "{emoji} I knead you to know this moment was purr-fectly timed.",
    "{emoji} =^..^= *stretches*",
    "{emoji} Feline fine, thanks for asking!",
    "{emoji} /ᐠ˵ •⩊• ˵マ I regret nothing.",
    "{emoji} That's paws-itively the best thing I've heard all day.",
    "{emoji} 乁( •_• )ㄏ *stares into your soul, then naps*",
    "{emoji} This is fur real my favorite conversation.",
    "{emoji} /ᐠ - ˕ -マ Purr purr.",
    "{emoji} No thoughts, just vibes and a sunbeam.",
    "{emoji} I'm feline a little mischievous right meow.",
    "{emoji} ⩊• *chases a dust particle, loses interest immediately*",
    "{emoji} Claw-some. Absolutely claw-some.",
    "{emoji} /ᐠ.ᆺ.ᐟ\\ noot noot, said the cat, incorrectly.",
]


@bot.tree.command(name="catfact", description="Get a random cat fact.")
async def catfact_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://catfact.ninja/fact")
    fact = data.get("fact") if data else None
    if not fact:
        await interaction.followup.send(
            f"{MILO_EMOJI} Hmm, my whiskers couldn't catch a fact this time. Try again?"
        )
        return
    await interaction.followup.send(f"🐱 {fact}")


@bot.tree.command(name="meow", description="Get a random cat photo.")
async def meow_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://cataas.com/cat?json=true")
    image_path = data.get("url") if data else None
    if not image_path:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch a cat photo right now — try again in a bit!"
        )
        return
    image_url = f"https://cataas.com{image_path}" if image_path.startswith("/") else image_path
    embed = discord.Embed(color=discord.Color.green())
    embed.set_image(url=image_url)
    embed.set_footer(text="via cataas.com")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="purr", description="A random cat-ism from Milo.")
async def purr_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.send_message(random.choice(PURR_RESPONSES).format(emoji=MILO_EMOJI))


# ---------------------------------------------------------------------------
# Games: /rps, /trivia
# ---------------------------------------------------------------------------
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


@bot.tree.command(name="rps", description="Play rock-paper-scissors against Milo.")
@app_commands.choices(
    choice=[
        app_commands.Choice(name="Rock", value="rock"),
        app_commands.Choice(name="Paper", value="paper"),
        app_commands.Choice(name="Scissors", value="scissors"),
    ]
)
async def rps_slash(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    player_pick = choice.value
    milo_pick = random.choice(["rock", "paper", "scissors"])

    if player_pick == milo_pick:
        result = "It's a tie!"
    elif RPS_BEATS[player_pick] == milo_pick:
        result = "You win! 🎉"
    else:
        result = f"Milo wins! {MILO_EMOJI}"

    await interaction.response.send_message(
        f"{RPS_EMOJI[player_pick]} vs {RPS_EMOJI[milo_pick]} — **{result}**"
    )


TRIVIA_QUESTIONS = [
    {
        "question": "What is a group of cats called?",
        "options": ["A clowder", "A pack", "A herd", "A gaggle"],
        "answer": 0,
    },
    {
        "question": "What is the largest planet in our solar system?",
        "options": ["Saturn", "Neptune", "Jupiter", "Uranus"],
        "answer": 2,
    },
    {
        "question": "How many hearts does an octopus have?",
        "options": ["1", "2", "3", "9"],
        "answer": 2,
    },
    {
        "question": "What language has the most native speakers worldwide?",
        "options": ["English", "Spanish", "Hindi", "Mandarin Chinese"],
        "answer": 3,
    },
    {
        "question": "Cats can't taste which flavor?",
        "options": ["Sour", "Bitter", "Sweet", "Salty"],
        "answer": 2,
    },
    {
        "question": "What's the smallest country in the world?",
        "options": ["Monaco", "Vatican City", "San Marino", "Liechtenstein"],
        "answer": 1,
    },
    {
        "question": "How many bones are in the human body?",
        "options": ["186", "206", "226", "246"],
        "answer": 1,
    },
    {
        "question": "What do you call a baby cat?",
        "options": ["Cub", "Pup", "Kit", "Kitten"],
        "answer": 3,
    },
    {
        "question": "Which planet is known as the Red Planet?",
        "options": ["Venus", "Mars", "Jupiter", "Mercury"],
        "answer": 1,
    },
    {
        "question": "What's the fastest land animal?",
        "options": ["Lion", "Cheetah", "Pronghorn", "Greyhound"],
        "answer": 1,
    },
]


class TriviaButton(discord.ui.Button):
    def __init__(self, label: str, index: int, correct_index: int):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.index = index
        self.correct_index = correct_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.index == self.correct_index:
            await interaction.response.send_message(
                f"✅ Correct, {interaction.user.mention}! {MILO_EMOJI}", ephemeral=True
            )
        else:
            correct_letter = "ABCD"[self.correct_index]
            await interaction.response.send_message(
                f"❌ Not quite, {interaction.user.mention} — the answer was **{correct_letter}**.",
                ephemeral=True,
            )


class TriviaView(discord.ui.View):
    def __init__(self, correct_index: int):
        super().__init__(timeout=60)  # Short-lived -- no need to persist across restarts.
        for i, letter in enumerate("ABCD"):
            self.add_item(TriviaButton(letter, i, correct_index))


@bot.tree.command(name="trivia", description="Answer a random trivia question.")
async def trivia_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    q = random.choice(TRIVIA_QUESTIONS)
    options_text = "\n".join(
        f"**{letter}.** {opt}" for letter, opt in zip("ABCD", q["options"])
    )
    embed = discord.Embed(
        title="🧠 Trivia Time!",
        description=f"{q['question']}\n\n{options_text}",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="You have 60 seconds. Answers are shown only to you.")
    view = TriviaView(q["answer"])
    await interaction.response.send_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Reaction commands: /hug, /slap, /pat
# ---------------------------------------------------------------------------
async def _send_reaction_gif(
    interaction: discord.Interaction, reaction: str, target: discord.Member, verb: str
) -> None:
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json(f"https://api.otakugifs.xyz/gif?reaction={reaction}")
    gif_url = data.get("url") if data else None
    text = f"{interaction.user.mention} {verb} {target.mention}! {MILO_EMOJI}"

    if gif_url:
        embed = discord.Embed(description=text, color=discord.Color.green())
        embed.set_image(url=gif_url)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(text)


@bot.tree.command(name="hug", description="Give someone a hug.")
@app_commands.describe(user="Who to hug")
async def hug_slash(interaction: discord.Interaction, user: discord.Member):
    await _send_reaction_gif(interaction, "hug", user, "hugs")


@bot.tree.command(name="slap", description="Slap someone (playfully!).")
@app_commands.describe(user="Who to slap")
async def slap_slash(interaction: discord.Interaction, user: discord.Member):
    await _send_reaction_gif(interaction, "slap", user, "slaps")


@bot.tree.command(name="pat", description="Give someone a headpat.")
@app_commands.describe(user="Who to pat")
async def pat_slash(interaction: discord.Interaction, user: discord.Member):
    await _send_reaction_gif(interaction, "pat", user, "pats")


@bot.tree.command(name="wave", description="Wave at someone.")
@app_commands.describe(user="Who to wave at")
async def wave_slash(interaction: discord.Interaction, user: discord.Member):
    await _send_reaction_gif(interaction, "wave", user, "waves at")


@bot.tree.command(name="fistbump", description="Fist bump someone.")
@app_commands.describe(user="Who to fist bump")
async def fistbump_slash(interaction: discord.Interaction, user: discord.Member):
    # otakugifs.xyz's category for this is "brofist" rather than "fistbump".
    await _send_reaction_gif(interaction, "brofist", user, "fist bumps")


@bot.tree.command(name="nudge", description="Nudge someone.")
@app_commands.describe(user="Who to nudge")
async def nudge_slash(interaction: discord.Interaction, user: discord.Member):
    # otakugifs.xyz's category for this is "poke" rather than "nudge".
    await _send_reaction_gif(interaction, "poke", user, "nudges")


@bot.tree.command(name="handhold", description="Hold hands with someone.")
@app_commands.describe(user="Who to hold hands with")
async def handhold_slash(interaction: discord.Interaction, user: discord.Member):
    await _send_reaction_gif(interaction, "handhold", user, "holds hands with")


# otakugifs.xyz's full set of reaction categories, confirmed against its own
# API wrapper docs. The dedicated commands above (hug, slap, pat, wave,
# fistbump, nudge, handhold) cover the most common ones; /react below covers
# every category via type-to-search autocomplete, since Discord caps a fixed
# dropdown at 25 choices and there are nearly 70 of these.
ALL_REACTIONS = [
    "airkiss", "angrystare", "bite", "bleh", "blush", "brofist", "celebrate",
    "cheers", "clap", "confused", "cool", "cry", "cuddle", "dance", "drool",
    "evillaugh", "facepalm", "handhold", "happy", "headbang", "hug", "huh",
    "kiss", "laugh", "lick", "love", "mad", "nervous", "no", "nom",
    "nosebleed", "nuzzle", "nyah", "pat", "peek", "pinch", "poke", "pout",
    "punch", "roll", "run", "sad", "scared", "shout", "shrug", "shy", "sigh",
    "sip", "slap", "sleep", "slowclap", "smack", "smile", "smug", "sneeze",
    "sorry", "stare", "stop", "surprised", "sweat", "thumbsup", "tickle",
    "tired", "wave", "wink", "woah", "yawn", "yay", "yes",
]


async def _reaction_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current_lower = current.strip().lower()
    matches = [r for r in ALL_REACTIONS if current_lower in r][:25]
    return [app_commands.Choice(name=r, value=r) for r in matches]


@bot.tree.command(name="react", description="Send any anime reaction gif at someone (start typing to search).")
@app_commands.describe(reaction="Which reaction (start typing to search all 67)", user="Who to react at")
@app_commands.autocomplete(reaction=_reaction_autocomplete)
async def react_slash(interaction: discord.Interaction, reaction: str, user: discord.Member):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    reaction_clean = reaction.strip().lower()
    if reaction_clean not in ALL_REACTIONS:
        await interaction.response.send_message(
            f"`{reaction}` isn't a reaction I know — start typing in the `reaction` "
            "field and pick one of the suggestions.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    data = await _fetch_json(f"https://api.otakugifs.xyz/gif?reaction={reaction_clean}")
    gif_url = data.get("url") if data else None
    text = f"{interaction.user.mention} reacts with **{reaction_clean}** at {user.mention}! {MILO_EMOJI}"

    if gif_url:
        embed = discord.Embed(description=text, color=discord.Color.green())
        embed.set_image(url=gif_url)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(text)


# ---------------------------------------------------------------------------
# Lookups: /quote, /define, /weather, /translate
# ---------------------------------------------------------------------------
@bot.tree.command(name="quote", description="Get a random inspirational quote.")
async def quote_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://zenquotes.io/api/random")
    quote_text = author = None
    if isinstance(data, list) and data:
        quote_text = data[0].get("q")
        author = data[0].get("a")

    if not quote_text:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch a quote right now — try again soon."
        )
        return
    await interaction.followup.send(f"💬 *\"{quote_text}\"*\n— {author or 'Unknown'}")


@bot.tree.command(name="define", description="Look up a word's definition.")
@app_commands.describe(word="The word to define")
async def define_slash(interaction: discord.Interaction, word: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    clean_word = word.strip()
    data = await _fetch_json(f"https://api.dictionaryapi.dev/api/v2/entries/en/{clean_word}")

    if not data or not isinstance(data, list):
        await interaction.followup.send(f"Couldn't find a definition for **{clean_word}**.")
        return

    entry = data[0]
    meanings = entry.get("meanings", [])
    if not meanings:
        await interaction.followup.send(f"Couldn't find a definition for **{clean_word}**.")
        return

    embed = discord.Embed(title=f"📖 {entry.get('word', clean_word)}", color=discord.Color.blurple())
    phonetic = entry.get("phonetic")
    if phonetic:
        embed.description = phonetic

    for meaning in meanings[:3]:
        part_of_speech = meaning.get("partOfSpeech", "meaning")
        defs = meaning.get("definitions", [])
        if defs:
            embed.add_field(name=part_of_speech, value=defs[0].get("definition", "—"), inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="weather", description="Get current weather for a location.")
@app_commands.describe(location="City name, e.g. 'London' or 'Bengaluru'")
async def weather_slash(interaction: discord.Interaction, location: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    geo = await _fetch_json(
        f"https://geocoding-api.open-meteo.com/v1/search?name={location}&count=1"
    )
    results = geo.get("results") if geo else None
    if not results:
        await interaction.followup.send(f"Couldn't find a location called **{location}**.")
        return

    place = results[0]
    lat, lon = place.get("latitude"), place.get("longitude")
    place_name = place.get("name", location)
    country = place.get("country", "")

    forecast = await _fetch_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    )
    current = forecast.get("current_weather") if forecast else None
    if not current:
        await interaction.followup.send(f"Couldn't fetch weather for **{place_name}**.")
        return

    embed = discord.Embed(
        title=f"🌦️ Weather in {place_name}, {country}".strip(", "),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Temperature", value=f"{current.get('temperature')}°C")
    embed.add_field(name="Wind Speed", value=f"{current.get('windspeed')} km/h")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="translate", description="Translate text to another language.")
@app_commands.describe(text="Text to translate", to="Target language code, e.g. 'es', 'fr', 'hi'")
async def translate_slash(interaction: discord.Interaction, text: str, to: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    params = {"q": text, "langpair": f"en|{to.strip()}"}
    data = await _fetch_json("https://api.mymemory.translated.net/get", params=params)
    translated = None
    if data:
        translated = data.get("responseData", {}).get("translatedText")

    if not translated:
        await interaction.followup.send(
            "Couldn't translate that — try a different language code (e.g. `es`, `fr`, `hi`)."
        )
        return

    embed = discord.Embed(color=discord.Color.blurple())
    embed.add_field(name="Original", value=text, inline=False)
    embed.add_field(name=f"Translated ({to})", value=translated, inline=False)
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Utility: /avatar, /firstmessage, /color
# ---------------------------------------------------------------------------
@bot.tree.command(name="avatar", description="Show a member's full-size avatar.")
@app_commands.describe(user="Whose avatar to show (defaults to you)")
async def avatar_slash(interaction: discord.Interaction, user: discord.Member | None = None):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    member = user or interaction.user
    embed = discord.Embed(title=f"{member.display_name}'s avatar", color=discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="firstmessage", description="Jump to the first message in a channel.")
@app_commands.describe(channel="Which channel (defaults to this one)")
async def firstmessage_slash(
    interaction: discord.Interaction, channel: discord.TextChannel | None = None
):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("That's not a text channel.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        first_message = [msg async for msg in target_channel.history(limit=1, oldest_first=True)]
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read history in that channel."
        )
        return

    if not first_message:
        await interaction.followup.send(f"{target_channel.mention} has no messages yet.")
        return

    msg = first_message[0]
    await interaction.followup.send(
        f"📜 The first message in {target_channel.mention} was by **{msg.author}**: {msg.jump_url}"
    )


@bot.tree.command(name="color", description="Show a color swatch from a hex code.")
@app_commands.describe(hex_code="Hex color code, e.g. 'ff6600' or '#00ff00'")
async def color_slash(interaction: discord.Interaction, hex_code: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    clean_hex = hex_code.strip().lstrip("#").lower()
    if len(clean_hex) == 3:
        clean_hex = "".join(c * 2 for c in clean_hex)
    if len(clean_hex) != 6 or any(c not in "0123456789abcdef" for c in clean_hex):
        await interaction.response.send_message(
            "That doesn't look like a valid hex code. Try something like `ff6600`.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title=f"#{clean_hex.upper()}", color=int(clean_hex, 16))
    embed.set_thumbnail(url=f"https://singlecolorimage.com/get/{clean_hex}/200x200")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# More fun: /riddle, /slots, /emojify, /yesno, /catbreed
# ---------------------------------------------------------------------------
RIDDLES = [
    {
        "question": "I speak without a mouth and hear without ears. I have no body, but I come alive with wind. What am I?",
        "answer": "An echo",
    },
    {
        "question": "The more you take, the more you leave behind. What am I?",
        "answer": "Footsteps",
    },
    {
        "question": "What has keys but no locks, space but no room, and you can enter but not go inside?",
        "answer": "A keyboard",
    },
    {
        "question": "I'm tall when I'm young and short when I'm old. What am I?",
        "answer": "A candle",
    },
    {
        "question": "What has a head and a tail but no body?",
        "answer": "A coin",
    },
    {
        "question": "What can travel around the world while staying in a corner?",
        "answer": "A stamp",
    },
    {
        "question": "What has to be broken before you can use it?",
        "answer": "An egg",
    },
    {
        "question": "I have cities, but no houses. I have mountains, but no trees. I have water, but no fish. What am I?",
        "answer": "A map",
    },
    {
        "question": "What gets wetter the more it dries?",
        "answer": "A towel",
    },
    {
        "question": "What has one eye but can't see?",
        "answer": "A needle",
    },
]


class RiddleRevealButton(discord.ui.Button):
    def __init__(self, answer: str):
        super().__init__(label="Reveal Answer", style=discord.ButtonStyle.secondary, emoji="🔍")
        self.answer = answer

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"🔍 The answer is: **{self.answer}**", ephemeral=True
        )


class RiddleView(discord.ui.View):
    def __init__(self, answer: str):
        super().__init__(timeout=120)  # Short-lived -- no need to persist across restarts.
        self.add_item(RiddleRevealButton(answer))


@bot.tree.command(name="riddle", description="Get a random riddle. Answer is hidden behind a button.")
async def riddle_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    riddle = random.choice(RIDDLES)
    embed = discord.Embed(
        title="🧩 Riddle me this...",
        description=riddle["question"],
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Click below to reveal the answer (only you'll see it).")
    view = RiddleView(riddle["answer"])
    await interaction.response.send_message(embed=embed, view=view)


SLOT_SYMBOLS = ["🍒", "🍋", "🍉", "🍇", "⭐", "7️⃣"]


@bot.tree.command(name="slots", description="Spin the emoji slot machine.")
async def slots_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    reels_text = " | ".join(reels)

    if reels[0] == reels[1] == reels[2]:
        result = f"🎉 JACKPOT! All three match! {MILO_EMOJI}"
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        result = "✨ Almost! Two matched."
    else:
        result = "No match — try again!"

    await interaction.response.send_message(f"🎰 [ {reels_text} ]\n{result}")


def _emojify_text(text: str) -> str:
    regional_offset = ord("🇦") - ord("a")
    parts = []
    for char in text.lower():
        if char.isalpha() and "a" <= char <= "z":
            parts.append(chr(ord(char) + regional_offset))
        elif char.isdigit():
            parts.append(f"{char}\ufe0f\u20e3")  # keycap digit emoji
        elif char == " ":
            parts.append("   ")  # a little extra gap reads better between emoji
        else:
            parts.append(char)  # leave punctuation etc. as-is
    return "".join(parts)


@bot.tree.command(name="emojify", description="Turn text into regional-indicator emoji letters.")
@app_commands.describe(text="The text to emojify (letters and numbers work best)")
async def emojify_slash(interaction: discord.Interaction, text: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    clean_text = text.strip()
    if not clean_text:
        await interaction.response.send_message("Give me some text to emojify!", ephemeral=True)
        return
    if len(clean_text) > 80:
        clean_text = clean_text[:80]  # Discord renders very long emoji strings poorly

    await interaction.response.send_message(_emojify_text(clean_text))


@bot.tree.command(name="yesno", description="Get a random yes or no answer.")
async def yesno_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://yesno.wtf/api")
    answer = data.get("answer") if data else None
    image_url = data.get("image") if data else None

    if not answer:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't get an answer right now — the universe is undecided."
        )
        return

    embed = discord.Embed(title=answer.upper(), color=discord.Color.green())
    if image_url:
        embed.set_image(url=image_url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="catbreed", description="Learn about a random cat breed.")
async def catbreed_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://api.thecatapi.com/v1/breeds")
    if not data or not isinstance(data, list):
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch cat breeds right now — try again soon."
        )
        return

    breed = random.choice(data)
    name = breed.get("name", "Mystery Cat")
    description = breed.get("description", "No description available.")
    temperament = breed.get("temperament", "Unknown")
    origin = breed.get("origin", "Unknown")
    life_span = breed.get("life_span", "Unknown")

    embed = discord.Embed(
        title=f"🐾 {name}",
        description=description,
        color=discord.Color.green(),
    )
    embed.add_field(name="Temperament", value=temperament, inline=True)
    embed.add_field(name="Origin", value=origin, inline=True)
    embed.add_field(name="Life Span", value=f"{life_span} years", inline=True)

    reference_image_id = breed.get("reference_image_id")
    if reference_image_id:
        embed.set_image(url=f"https://cdn2.thecatapi.com/images/{reference_image_id}.jpg")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="help", description="Shows all Milo commands.")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"Milo Commands {MILO_EMOJI}",
        description="Your friendly cat-themed group-management bot.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🎮 Group & Server",
        value=(
            "`/join game:VBL` / `game:Minecraft` — look for players "
            "(add `message:<text>` for a custom note)\n"
            "`/serverinfo` — info about this server\n"
            "`/userinfo user:<member>` — info about a member\n"
            "`/ping` — check Milo's latency"
        ),
        inline=False,
    )
    embed.add_field(
        name="🐱 Cat Corner",
        value=(
            "`/aboutme` — learn about Milo and who made it\n"
            "`/hi` — say hi and get a cat-themed reply\n"
            "`/purr` — a random cat-ism\n"
            "`/catfact` — a random cat fact\n"
            "`/meow` — a random cat photo\n"
            "`/catbreed` — learn about a random cat breed"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎉 Fun & Games",
        value=(
            "`/8ball question:<text>` — ask the magic 8-ball\n"
            "`/poll question option1 option2 ...` — quick poll with reactions\n"
            "`/coinflip` / `/roll sides:<n>` — flip a coin or roll dice\n"
            "`/rps choice:rock` — rock-paper-scissors vs Milo\n"
            "`/trivia` — answer a random trivia question\n"
            "`/riddle` — a riddle with the answer hidden behind a button\n"
            "`/slots` — spin the emoji slot machine\n"
            "`/yesno` — a random yes/no answer with a reaction gif\n"
            "`/emojify text:<text>` — turn text into emoji letters\n"
            "`/hug user` / `/slap user` / `/pat user` — react at someone\n"
            "`/wave user` / `/fistbump user` / `/nudge user` / `/handhold user` — more reactions\n"
            "`/react reaction:<search> user` — any reaction, type to search"
        ),
        inline=False,
    )
    embed.add_field(
        name="🌍 Lookups",
        value=(
            "`/quote` — a random inspirational quote\n"
            "`/define word:<text>` — dictionary lookup\n"
            "`/weather location:<place>` — current weather\n"
            "`/translate text:<text> to:<lang>` — translate text"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Utility",
        value=(
            "`/avatar user:<member>` — full-size avatar\n"
            "`/firstmessage channel:<#channel>` — jump to a channel's first message\n"
            "`/color hex_code:<code>` — preview a hex color"
        ),
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

    try:
        await message.channel.send(
            content_to_send,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
    except discord.Forbidden:
        logger.error(
            "?send failed in #%s (guild %s): missing permission to send messages there.",
            getattr(message.channel, "name", message.channel.id),
            message.guild.id,
        )
    except discord.HTTPException:
        logger.exception("?send failed to post the message")


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

    try:
        await message.channel.send(embed=embed, view=view)
    except discord.Forbidden:
        # Most likely missing "Embed Links" in this channel. Fall back to a
        # plain-text version (no embed styling) so the buttons still work,
        # rather than failing completely.
        logger.warning(
            "?rolemenu couldn't send an embed in #%s (guild %s) -- likely missing "
            "the 'Embed Links' permission there. Falling back to plain text.",
            getattr(message.channel, "name", message.channel.id),
            message.guild.id,
        )
        plain_text = f"🎭 **Pick your roles!** {MILO_EMOJI}\n\n" + "\n".join(lines)
        try:
            await message.channel.send(plain_text, view=view)
        except discord.Forbidden:
            logger.error(
                "?rolemenu failed completely in #%s (guild %s): missing "
                "permission to send messages there at all.",
                getattr(message.channel, "name", message.channel.id),
                message.guild.id,
            )
    except discord.HTTPException:
        logger.exception("?rolemenu failed to post the role menu")


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
