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
import io
import json
import logging
import math
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# ---------------------------------------------------------------------------
# The points currency: its NAME and EMOJI come from the environment, so you can call
# it "Milo Chips", "Treats", whatever. Every message in the bot is built from these.
#   POINTS_NAME           plural name, default "points"          e.g. Milo Chips
#   POINTS_NAME_SINGULAR  optional; otherwise a trailing "s" is dropped for "1 ..."
#   POINTS_EMOJI          a custom emoji ID (digits), a full <:name:id> / <a:name:id>,
#                         or a normal emoji like 🪙. Default: none.
# Slash COMMAND names (/points, /pay ...) are fixed; only the wording changes.
# ---------------------------------------------------------------------------
def _parse_emoji(raw: str) -> str:
    raw = raw.strip()
    return f"<:points:{raw}>" if raw.isdigit() else raw


def _singular(name: str) -> str:
    return name[:-1] if len(name) > 1 and name.lower().endswith("s") and not name.lower().endswith("ss") else name


PTS_NAME = " ".join(os.getenv("POINTS_NAME", "").split())[:24] or "points"
PTS_ONE = " ".join(os.getenv("POINTS_NAME_SINGULAR", "").split())[:24] or _singular(PTS_NAME)
PTS_EMOJI = _parse_emoji(os.getenv("POINTS_EMOJI", ""))
PTS_CAP = PTS_NAME[:1].upper() + PTS_NAME[1:]          # for the start of a sentence / a title


def pname(n: int = 2) -> str:
    """The currency's name for this amount: '1 chip', '5 chips'."""
    return PTS_ONE if n == 1 else PTS_NAME


def pamt(n: int) -> str:
    """Plain text, no emoji -- for footers, buttons, modals and logs (they can't show custom emoji)."""
    return f"{n:,} {pname(n)}"


def pfmt(n: int) -> str:
    """Amount + name, then the emoji if one is set -- for normal messages."""
    return pamt(n) + (f" {PTS_EMOJI}" if PTS_EMOJI else "")


def pcompact(n: int, bold: bool = False) -> str:
    """Emoji ONLY when one is set (else the name) -- for tight spots like leaderboards."""
    num = f"**{n:,}**" if bold else f"{n:,}"
    return f"{num} {PTS_EMOJI}" if PTS_EMOJI else f"{num} {pname(n)}"

# Optional: TheCatAPI started requiring an API key on /v1/breeds around late
# September 2026 (it used to work unauthenticated). Free to get at
# thecatapi.com/signup. Without it, /catbreed is disabled with a clear
# message rather than failing silently.
CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()

# Optional: free API key from console.groq.com, needed for /ask. Groq is an
# unrelated company to xAI/SpaceX despite the name sounding like "Grok" --
# sign up there, not at x.ai or spacex.com.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# When this process started, for the uptime line in /ping.
START_TIME = time.time()

# Optional: channels where /ask is allowed, as comma-separated channel IDs
# (e.g. ASK_CHANNEL_IDS=111111111111111111,222222222222222222). Lets you keep
# /ask out of busy channels like #spam so it doesn't burn the shared daily
# limit. Blank = /ask works in every channel. Threads inside an allowed
# channel are allowed too.
ASK_CHANNEL_IDS: set[int] = set()
for _tok in re.split(r"[,\s]+", os.getenv("ASK_CHANNEL_IDS", "").strip()):
    if not _tok:
        continue
    if _tok.isdigit():
        ASK_CHANNEL_IDS.add(int(_tok))
    else:
        logger.warning("Ignoring invalid ASK_CHANNEL_IDS entry: %r", _tok)

# Per-person cooldown for /ask. The AI limit is shared by the whole server, so
# this stops one person from using up everyone's share.
ASK_COOLDOWN_SECONDS = 15
_ask_cooldowns: dict[int, float] = {}

# Which Groq model /ask uses. Groq retired llama-3.1-8b-instant on
# August 16, 2026 (that's what caused a 404 here), and recommends
# openai/gpt-oss-20b as its replacement. Model names change over time, so
# this is overridable via the GROQ_MODEL env var -- if Groq retires this one
# too, set GROQ_MODEL in Render to the new name instead of editing code.
# Current list: https://console.groq.com/docs/models
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-20b"

# Milo's birthday, shown in /aboutme and told to the /ask AI. One constant so
# changing it later is a one-word edit.
MILO_BIRTHDAY = "27 September 2026"

_MILO_PERSONA = (
    "You are Milo, a male (he/him) cat-themed Discord bot in the Da Game GNG "
    f"server, created by AAPoke. Birthday: {MILO_BIRTHDAY} (if asked your age, "
    "give the birthday, no date math). Only mention your gender if asked. You "
    "help with game lobbies, role menus, trivia, and chat.\n"
    "Talk like a playful cat: sprinkle in 'meow', 'mrrp', 'purr', cat puns, and "
    "small actions like *stretches*, but still give a correct, helpful answer. "
)
_MILO_IDENTITY_RULES = (
    "If asked who you are, say you're Milo. If someone sincerely asks whether "
    "you're a bot or AI, admit you're an AI-powered Discord bot with a cat "
    "personality. Never claim to be human."
)
# Quick answers for /ask (cheap on the shared token budget).
MILO_SYSTEM_PROMPT = (
    _MILO_PERSONA
    + "Keep replies to 2-4 sentences, under 600 characters, plain text. "
    + _MILO_IDENTITY_RULES
)
# Deeper answers for the "Think harder" / "Second opinion" buttons.
MILO_THINK_PROMPT = (
    _MILO_PERSONA
    + "This is a question someone wants answered more carefully: reason it "
    "through, then give a thorough but readable answer in up to about 1,500 "
    "characters, plain text, short paragraphs, no headings. "
    + _MILO_IDENTITY_RULES
)

_raw_allowed_guild = os.getenv("ALLOWED_GUILD_ID", "").strip()
ALLOWED_GUILD_ID = int(_raw_allowed_guild) if _raw_allowed_guild.isdigit() else None

# Optional: channel where new-member welcome messages get posted.
# If not set, the welcome feature is simply skipped (logged once at startup).
_raw_welcome_channel = os.getenv("WELCOME_CHANNEL_ID", "").strip()
WELCOME_CHANNEL_ID = int(_raw_welcome_channel) if _raw_welcome_channel.isdigit() else None

# Optional: the Manager role. Can use BOTH hidden commands: "?send" and
# "?rolemenu". If not set, ?rolemenu is disabled entirely (and ?send only
# works for MILO_ROLE_ID below, if that's set).
_raw_manager_role = os.getenv("MANAGER_ROLE_ID", "").strip()
MANAGER_ROLE_ID = int(_raw_manager_role) if _raw_manager_role.isdigit() else None

# Optional: a second, more limited role. Can use "?send" ONLY -- not
# "?rolemenu", which stays Manager-only. If not set, only MANAGER_ROLE_ID
# can use ?send.
_raw_milo_role = os.getenv("MILO_ROLE_ID", "").strip()
MILO_ROLE_ID = int(_raw_milo_role) if _raw_milo_role.isdigit() else None

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

# Optional: the games /join can look for players in, as
# "ROLE_ID|Name|Emoji|Call to action,ROLE_ID|Name|Emoji|Call to action,...".
# Only the role ID and name are required. Emoji (shown before the ping) and the
# call-to-action line (default "Join now!") are optional. Avoid commas in the
# call to action. If JOIN_GAMES isn't set, the original two games are used so
# nothing changes until you set it.
_raw_join_games = os.getenv("JOIN_GAMES", "").strip()
JOIN_GAMES: list[tuple[int, str, str | None, str]] = []
if _raw_join_games:
    for _entry in _raw_join_games.split(","):
        _entry = _entry.strip()
        if not _entry:
            continue
        _parts = [_p.strip() for _p in _entry.split("|")]
        if len(_parts) < 2 or not _parts[0].isdigit() or not _parts[1]:
            logger.warning("Skipping malformed JOIN_GAMES entry: %r", _entry)
            continue
        JOIN_GAMES.append((
            int(_parts[0]),
            _parts[1][:100],
            _parts[2] if len(_parts) > 2 and _parts[2] else None,
            _parts[3] if len(_parts) > 3 and _parts[3] else "Join now!",
        ))
    if len(JOIN_GAMES) > 25:
        logger.warning("JOIN_GAMES has %d entries; only the first 25 fit in a Discord dropdown.", len(JOIN_GAMES))
        JOIN_GAMES = JOIN_GAMES[:25]
if not JOIN_GAMES:
    JOIN_GAMES = [
        (1532265584069185597, "VBL", "🏐", "Join the lobby now!"),
        (1536962762952146944, "Minecraft", None, "Join the Server now!"),
    ]

PORT = int(os.getenv("PORT", "10000"))

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is required. "
        "Set it in your .env file (local) or in Render's Environment settings."
    )

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
# Discord "blocked" handling
#
# On shared hosts (like Render's free tier) Cloudflare sometimes blocks the host's IP
# for a while: every Discord REST call then fails with "429 ... blocked from accessing
# our API temporarily" (error code 0). That is NOT something Milo caused and it clears
# by itself. What Milo does about it: stay quiet (one short log line, not a traceback),
# don't fire extra doomed requests, and retry the few IMPORTANT posts with a back-off.
# ---------------------------------------------------------------------------
_discord_block_until = 0.0


def _is_discord_block(exc: BaseException) -> bool:
    exc = getattr(exc, "original", exc)
    return (isinstance(exc, discord.HTTPException) and exc.status == 429
            and (getattr(exc, "code", 1) == 0 or "blocked from accessing" in str(exc)))


def discord_blocked() -> bool:
    return time.time() < _discord_block_until


def _note_discord_block(where: str) -> None:
    global _discord_block_until
    now = time.time()
    if now >= _discord_block_until:
        logger.warning(
            "Discord is temporarily blocking this host's IP address (seen in %s). That's a Discord/Cloudflare limit on "
            "the shared host, not a Milo bug, and it clears on its own. Backing off for 2 minutes.", where)
    _discord_block_until = now + 120


async def _retry_discord(make_call, what: str, delays=(60, 180, 420)) -> bool:
    """Run an IMPORTANT Discord call. If Discord is blocking us (or having a server error), wait and try again,
    a few times at most. Returns True once it worked. `make_call` builds a fresh call each attempt."""
    for attempt in range(len(delays) + 1):
        try:
            await make_call()
            return True
        except discord.HTTPException as exc:
            blocked = _is_discord_block(exc)
            if blocked:
                _note_discord_block(what)
            if (blocked or exc.status >= 500) and attempt < len(delays):
                await asyncio.sleep(delays[attempt])
                continue
            logger.warning("Couldn't complete '%s': %s", what, exc)
            return False
        except Exception:
            logger.warning("Couldn't complete '%s'", what, exc_info=True)
            return False
    return False



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
    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        exc = sys.exc_info()[1]
        if exc is not None and _is_discord_block(exc):
            _note_discord_block(f"the {event_method} handler")
            return
        await super().on_error(event_method, *args, **kwargs)

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
            timeout=aiohttp.ClientTimeout(total=10)
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
            logger.info("MANAGER_ROLE_ID not set -- the hidden ?rolemenu command is disabled.")
        if THINK_TIERS:
            logger.info(
                "Think buttons enabled: %s",
                ", ".join(f"{t.label} -> {t.model}" for t in THINK_TIERS),
            )
            if LIMIT_STORE.persistent:
                logger.info("Think limits are stored in Upstash Redis (survive restarts).")
            else:
                logger.warning(
                    "Think limits are kept in memory only -- a restart/redeploy resets "
                    "everyone's daily uses. Set UPSTASH_REDIS_REST_URL and "
                    "UPSTASH_REDIS_REST_TOKEN to make them permanent."
                )
        if not MANAGER_ROLE_ID and not MILO_ROLE_ID:
            logger.info("Neither MANAGER_ROLE_ID nor MILO_ROLE_ID set -- the hidden ?send command is disabled.")
        if not MANAGER_ROLE_ID:
            logger.info("MANAGER_ROLE_ID not set -- the hidden ?updateDB command is disabled.")
        # Load points (or switch to memory-only with a loud warning) and start
        # the background autosave. /slots, /points and /leaderboard stay
        # politely unavailable until the load succeeds.
        POINTS.start()
        if LOG_CHANNEL_ID:
            task = asyncio.create_task(_log_startup_and_daily_snapshots())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        else:
            logger.info("LOG_CHANNEL_ID not set -- the manager log channel is disabled.")
        sched_task = asyncio.create_task(_gamblin_scheduler())
        _background_tasks.add(sched_task)
        sched_task.add_done_callback(_background_tasks.discard)
        react_task = asyncio.create_task(_react_refresher())
        _background_tasks.add(react_task)
        react_task.add_done_callback(_background_tasks.discard)
        lottery_task = asyncio.create_task(_lottery_loop())
        _background_tasks.add(lottery_task)
        lottery_task.add_done_callback(_background_tasks.discard)
        if RESET_ANNOUNCE_CHANNEL_ID:
            reset_task = asyncio.create_task(_daily_reset_announcer())
            _background_tasks.add(reset_task)
            reset_task.add_done_callback(_background_tasks.discard)
        else:
            logger.info("RESET_ANNOUNCE_CHANNEL_ID not set -- the midnight announcement is disabled.")

    async def close(self) -> None:
        # Last chance to save: Render gives ~30 seconds after SIGTERM. The save
        # needs the HTTP session, so it must happen BEFORE the session closes.
        POINTS.stop()
        await _gamblin_shutdown()
        try:
            ok, people, detail = await asyncio.wait_for(POINTS.flush("shutdown"), timeout=20)
            if ok and detail != "nothing unsaved":
                logger.info("Shutdown save done: %d people's points saved.", people)
            elif not ok and POINTS.store is not None and POINTS.ready:
                logger.error("SHUTDOWN SAVE FAILED -- recent points may be lost: %s", detail)
        except asyncio.TimeoutError:
            logger.error("SHUTDOWN SAVE TIMED OUT -- recent points may be lost.")
        except Exception:
            logger.exception("Unexpected error during the shutdown save")
        try:
            await asyncio.wait_for(log_event(f"🔴 **Milo is shutting down** (redeploy or restart) — {PTS_NAME} were saved first."), timeout=4)
        except Exception:
            pass
        if self.session is not None:
            await self.session.close()
        await super().close()


bot = MiloClient()


async def _fetch_json(url: str, retries: int = 1, **kwargs):
    """GET a URL and return parsed JSON, or None on any failure. Used by all
    the free-API-backed commands below so each one can fail gracefully
    instead of crashing if an external API is slow, down, or rate-limited.

    Free public APIs occasionally have a single slow/dropped request without
    actually being down, so a timeout gets one automatic retry (default) --
    everything else (404, 429, connection refused, etc.) fails immediately
    without retrying, since retrying those wastes the deferred-response
    window for no benefit."""
    for attempt in range(retries + 1):
        try:
            async with bot.session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    logger.warning("GET %s returned status %s", url, resp.status)
                    return None
                return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            if attempt < retries:
                logger.warning("Timed out fetching %s, retrying...", url)
                continue
            logger.warning("Timed out fetching %s after %d attempt(s)", url, retries + 1)
            return None
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
        f"• 🎰 Handing out {PTS_NAME} in `/slots` (and keeping a leaderboard)\n"
        f"• 🐾 Saying hi, flipping coins, rolling dice, answering the magic 8-ball, and running polls\n"
        f"• 👋 Welcoming new members the moment they join\n\n"
        f"I run on pure vibes and whatever's happening right now — no database, no memory banks, "
        f"just me, my whiskers, and the occasional nap.\n\n"
        f"🎂 My birthday is {MILO_BIRTHDAY}.\n"
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
    game=[app_commands.Choice(name=_g[1], value=str(_i)) for _i, _g in enumerate(JOIN_GAMES)]
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

    try:
        role_id, _game_name, game_emoji, call_to_action = JOIN_GAMES[int(game.value)]
    except (ValueError, IndexError):
        await interaction.response.send_message(
            "That game isn't on the list anymore — try `/join` again.", ephemeral=True
        )
        return

    content = (
        f"{game_emoji + ' ' if game_emoji else ''}**{author_mention}** is looking for players!\n"
        f"<@&{role_id}>\n"
        f"{call_to_action}"
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


def _ping_dot(ms: float | None) -> str:
    if ms is None:
        return "⚪"
    if ms < 150:
        return "🟢"
    if ms < 350:
        return "🟡"
    return "🔴"


def _fmt_ms(ms: float | None) -> str:
    return "unavailable" if ms is None else f"{round(ms)} ms"


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


async def _timed_get_ms(url: str) -> float | None:
    """Time a plain GET (until the full response is read). None on failure."""
    try:
        start = time.perf_counter()
        async with bot.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            await resp.read()
        return (time.perf_counter() - start) * 1000
    except Exception:
        return None


@bot.tree.command(name="ping", description="Check Milo's latency: gateway, APIs, web server, average, uptime.")
async def ping_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    # How long Discord took to hand this command to Milo (created_at is the
    # moment Discord made the interaction).
    delivery_ms = max(
        0.0, (discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000
    )

    # 1) Gateway (WebSocket) -- the live connection Discord keeps open to Milo.
    raw_latency = bot.latency
    gateway_ms = raw_latency * 1000 if math.isfinite(raw_latency) else None

    # 2) Message round trip -- send a reply and time how long Discord takes
    #    to confirm it. Doubles as the "give Discord a reply" step.
    t0 = time.perf_counter()
    await interaction.response.send_message("🏓 Pinging...")
    response_ms = (time.perf_counter() - t0) * 1000

    # 3) Discord's API, called as Milo (needs his token).
    try:
        t0 = time.perf_counter()
        await asyncio.wait_for(bot.application_info(), timeout=5)
        api_auth_ms = (time.perf_counter() - t0) * 1000
    except Exception:
        api_auth_ms = None

    # 4) Discord's API, public endpoint (no token) -- plain HTTP reachability.
    api_public_ms = await _timed_get_ms("https://discord.com/api/v10/gateway")

    # 5) Milo's own little web server (the one Render/UptimeRobot poke). If
    #    this is slow, Milo himself is busy, not the network.
    internal_ms = await _timed_get_ms(f"http://127.0.0.1:{PORT}/health")

    # Average of the pings that actually travel to Discord. Command delivery
    # (one-way, clock-dependent) and the internal check are shown but kept
    # out of the average on purpose.
    discord_pings = [
        m for m in (gateway_ms, api_auth_ms, api_public_ms, response_ms) if m is not None
    ]
    average_ms = sum(discord_pings) / len(discord_pings) if discord_pings else None

    def line(label: str, ms: float | None) -> str:
        return f"{_ping_dot(ms)} **{label}:** {_fmt_ms(ms)}"

    if average_ms is None:
        color = discord.Color.blurple()
    elif average_ms < 150:
        color = discord.Color.green()
    elif average_ms < 350:
        color = discord.Color.gold()
    else:
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"🏓 Pong! {MILO_EMOJI}",
        description="\n".join(
            [
                line("Gateway (WebSocket)", gateway_ms),
                line("Discord API (as Milo)", api_auth_ms),
                line("Discord API (public HTTP)", api_public_ms),
                line("Message round trip", response_ms),
                line("Command delivery", delivery_ms),
                line("Milo's own web server", internal_ms),
            ]
        ),
        color=color,
    )
    embed.add_field(
        name="📊 Average (Discord pings)",
        value=f"{_ping_dot(average_ms)} {_fmt_ms(average_ms)}",
        inline=True,
    )
    embed.add_field(
        name="⏱️ Uptime", value=_format_uptime(time.time() - START_TIME), inline=True
    )
    embed.set_footer(text="🟢 under 150 ms · 🟡 under 350 ms · 🔴 slower · ⚪ unavailable")
    await interaction.edit_original_response(content=None, embed=embed)


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
# /grouptrivia: a YAGPDB-style group round. Everyone can answer the same
# question; what each person picked stays hidden, but their name is added to
# a visible "answered so far" list the moment they lock one in. After 60
# seconds the correct answer is revealed and everyone who got it right is
# mentioned. No database -- all state lives in memory for the 60-second
# window only, the same way /trivia and /riddle already work, just extended
# to track multiple participants instead of answering once and vanishing.
# ---------------------------------------------------------------------------
GROUP_TRIVIA_WINDOW_SECONDS = 60
# Leaves comfortable margin under Discord's hard 2000-char message cap when
# chunking winner mentions across multiple messages.
_MENTION_CHUNK_LIMIT = 1900


class GroupTriviaButton(discord.ui.Button):
    def __init__(self, label: str, index: int):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GroupTriviaView = self.view  # type: ignore[assignment]
        if view is None:
            return

        if interaction.user.id in view.answers:
            await interaction.response.send_message(
                "You've already locked in an answer for this round!", ephemeral=True
            )
            return

        view.answers[interaction.user.id] = self.index
        view.participant_order.append(interaction.user.id)

        await interaction.response.send_message(
            "✅ Your answer is locked in! What you picked stays secret until time's up.",
            ephemeral=True,
        )

        try:
            await view.refresh_public_message()
        except (discord.NotFound, discord.HTTPException):
            pass


class GroupTriviaView(discord.ui.View):
    def __init__(self, question: dict):
        super().__init__(timeout=GROUP_TRIVIA_WINDOW_SECONDS)
        self.question = question
        self.answers: dict[int, int] = {}  # user_id -> chosen index (kept secret)
        self.participant_order: list[int] = []  # user_id, in the order they answered
        self.message: discord.Message | None = None  # filled in right after sending
        for i, letter in enumerate("ABCD"):
            if i < len(question["options"]):
                self.add_item(GroupTriviaButton(letter, i))

    def build_embed(self, *, revealed: bool = False) -> discord.Embed:
        options_text = "\n".join(
            f"**{letter}.** {opt}" for letter, opt in zip("ABCD", self.question["options"])
        )
        if revealed:
            correct_letter = "ABCD"[self.question["answer"]]
            correct_text = self.question["options"][self.question["answer"]]
            status = f"⏰ Time's up! The correct answer was **{correct_letter}. {correct_text}**."
            color = discord.Color.green()
        else:
            status = (
                f"You have {GROUP_TRIVIA_WINDOW_SECONDS} seconds. Pick an answer below -- "
                "what you pick stays secret until time's up!"
            )
            color = discord.Color.blurple()

        embed = discord.Embed(
            title="🧠 Group Trivia!",
            description=f"{self.question['question']}\n\n{options_text}\n\n{status}",
            color=color,
        )

        count = len(self.participant_order)
        if count:
            if revealed:
                embed.add_field(name="Participants", value=f"{count} people answered.", inline=False)
            else:
                mentions = " ".join(f"<@{uid}>" for uid in self.participant_order)
                # Keep this field itself under Discord's 1024-char field
                # limit -- if a huge group pushes past that, show a count
                # instead of truncating mentions mid-tag.
                if len(mentions) > 1000:
                    mentions = f"{count} people have answered so far!"
                embed.add_field(name=f"Answered so far ({count})", value=mentions, inline=False)

        return embed

    async def refresh_public_message(self) -> None:
        if self.message is not None:
            await self.message.edit(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(embed=self.build_embed(revealed=True), view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

        if self.message is None:
            return

        correct_index = self.question["answer"]
        correct_letter = "ABCD"[correct_index]
        winner_ids = [uid for uid, choice in self.answers.items() if choice == correct_index]

        if not winner_ids:
            no_winner_text = (
                "Nobody answered in time!" if not self.participant_order
                else "Nobody got it right this time! Better luck next round."
            )
            try:
                await self.message.reply(no_winner_text)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                pass
            return

        await _send_group_trivia_winners(self.message, winner_ids, correct_letter)


async def _send_group_trivia_winners(
    message: discord.Message, winner_ids: list[int], correct_letter: str
) -> None:
    """Announces winners, splitting across multiple messages if the mention
    list would blow past Discord's 2000-char message limit (a big enough
    group genuinely can)."""
    header = f"🎉 Correct answer was **{correct_letter}**! Winners: "
    continuation_header = "🎉 Winners (continued): "

    chunks: list[str] = []
    current = header
    for uid in winner_ids:
        mention = f"<@{uid}> "
        if len(current) + len(mention) > _MENTION_CHUNK_LIMIT:
            chunks.append(current.rstrip())
            current = continuation_header
        current += mention
    chunks.append(current.rstrip())

    try:
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
    except (discord.NotFound, discord.HTTPException, discord.Forbidden):
        pass


@bot.tree.command(
    name="grouptrivia",
    description="Start a group trivia round -- everyone can answer, winners revealed after 60s.",
)
async def grouptrivia_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    blocked = gamblin_block_text("`/grouptrivia`")
    if blocked:
        await interaction.response.send_message(blocked, ephemeral=True)
        return

    question = random.choice(TRIVIA_QUESTIONS)
    view = GroupTriviaView(question)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# Reactions: /react -- several SFW anime-GIF APIs behind one command
#
# For each reaction, Milo picks RANDOMLY among the sources that have it, and if that
# source fails it quietly tries another (a source that keeps failing is paused for a
# while). A reaction only one source has still works -- it just uses that source.
# nekos.best and Gifukai publish their own category lists, which Milo reads at startup
# and every 6 hours (the lists below are the verified fallback). Nothing NSFW is used.
#
#   otakugifs.xyz  keyless   https://api.otakugifs.xyz/gif?reaction=<name>
#   nekos.best     keyless   https://nekos.best/api/v2/<category>  (needs a real User-Agent)
#   gifukai.com    keyless   https://api.gifukai.com/v1/<action>
#   purrbot.site   keyless   https://api.purrbot.site/v2/img/sfw/<category>/<gif|img>
# ---------------------------------------------------------------------------
REACT_DISABLE = {x.strip().lower() for x in os.getenv("REACT_DISABLE", "").split(",") if x.strip()}   # whole sources to switch off
REACT_BLOCK = {x.strip().lower() for x in os.getenv("REACT_BLOCK", "").split(",") if x.strip()}         # single reactions to hide
API_USER_AGENT = os.getenv("API_USER_AGENT", "").strip() or (
    f"MiloCatBot/1.0 (https://discord.com/users/{os.getenv('CREATOR_USER_ID', '1061069702287339590').strip()})"
)
_CAT_OK = re.compile(r"^[a-z0-9]{2,24}$")
# A safety net for names we DISCOVER at runtime (the fixed lists below are already SFW-only):
_CAT_BAD = re.compile(r"(nsfw|hentai|lewd|ecchi|porn|nude|naked|boob|blowjob|handjob|anal|sex|fuck|kill|trap)")

_OTAKU_CATS = [
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
_NEKOSBEST_GIFS = [
    "angry", "baka", "bite", "bleh", "blowkiss", "blush", "bonk", "bored", "carry", "clap", "confused",
    "cry", "cuddle", "dance", "facepalm", "feed", "handhold", "handshake", "happy", "highfive", "hug",
    "kabedon", "kick", "kiss", "lappillow", "laugh", "lurk", "nod", "nom", "nope", "nya", "pat", "peck",
    "poke", "pout", "punch", "run", "salute", "shake", "shoot", "shocked", "shrug", "sip", "slap", "sleep",
    "smile", "smug", "spin", "stare", "tableflip", "teehee", "think", "thumbsup", "tickle", "wag", "wave",
    "wink", "yawn", "yeet",
]
_NEKOSBEST_IMGS = ["husbando", "kitsune", "neko", "waifu"]
_GIFUKAI_ACTIONS = ["pat", "hug", "kiss", "cry", "sleep", "blush", "pout"]       # more are discovered at runtime
_PURRBOT_GIFS = ["angry", "bite", "blush", "comfy", "cry", "cuddle", "dance", "eevee", "fluff", "hug", "kiss",
                 "lay", "lick", "neko", "pat", "poke", "pout", "slap", "smile", "tickle"]
_PURRBOT_IMGS = ["holo", "kitsune", "okami", "senko", "shiro"]

# provider -> {"label", "cats": {reaction name: (remote name, "gif" | "img")}}
REACT_PROVIDERS: dict[str, dict] = {
    "otakugifs": {"label": "otakugifs.xyz", "cats": {c: (c, "gif") for c in _OTAKU_CATS}},
    "nekosbest": {"label": "nekos.best", "cats": {**{c: (c, "gif") for c in _NEKOSBEST_GIFS}, **{c: (c, "img") for c in _NEKOSBEST_IMGS}}},
    "gifukai": {"label": "gifukai.com", "cats": {c: (c, "gif") for c in _GIFUKAI_ACTIONS}},
    "purrbot": {"label": "purrbot.site", "cats": {**{("wag" if c == "tail" else c): (c, "gif") for c in _PURRBOT_GIFS + ["tail"]},
                                                  **{c: (c, "img") for c in _PURRBOT_IMGS}}},
}
REACT_CATALOG: dict[str, list[tuple[str, str, str]]] = {}      # reaction -> [(provider, remote, kind), ...]


def _rebuild_react_catalog() -> None:
    catalog: dict[str, list[tuple[str, str, str]]] = {}
    for prov, info in REACT_PROVIDERS.items():
        if prov in REACT_DISABLE:
            continue
        for cat, (remote, kind) in info["cats"].items():
            if cat not in REACT_BLOCK:
                catalog.setdefault(cat, []).append((prov, remote, kind))
    REACT_CATALOG.clear()
    REACT_CATALOG.update(catalog)


_rebuild_react_catalog()


def _good_url(value) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") and len(value) < 500 else None


async def _react_get(url: str, ua: bool = False):
    kwargs = {"timeout": aiohttp.ClientTimeout(total=5)}
    if ua:
        kwargs["headers"] = {"User-Agent": API_USER_AGENT}
    return await _fetch_json(url, retries=0, **kwargs)


async def _rf_otakugifs(remote: str, kind: str):
    data = await _react_get(f"https://api.otakugifs.xyz/gif?reaction={remote}")
    url = _good_url(data.get("url")) if isinstance(data, dict) else None
    return {"url": url} if url else None


async def _rf_nekosbest(remote: str, kind: str):
    data = await _react_get(f"https://nekos.best/api/v2/{remote}", ua=True)
    results = data.get("results") if isinstance(data, dict) else None
    first = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
    url = _good_url(first.get("url")) if first else None
    return {"url": url, "anime": first.get("anime_name"), "artist": first.get("artist_name")} if url else None


async def _rf_gifukai(remote: str, kind: str):
    data = await _react_get(f"https://api.gifukai.com/v1/{remote}", ua=True)
    url = _good_url(data.get("url")) if isinstance(data, dict) else None
    return {"url": url, "anime": data.get("anime")} if url else None


async def _rf_purrbot(remote: str, kind: str):
    data = await _react_get(f"https://api.purrbot.site/v2/img/sfw/{remote}/{'gif' if kind == 'gif' else 'img'}", ua=True)
    url = _good_url(data.get("link")) if isinstance(data, dict) and not data.get("error") else None
    return {"url": url} if url else None


REACT_FETCHERS = {"otakugifs": _rf_otakugifs, "nekosbest": _rf_nekosbest, "gifukai": _rf_gifukai,
                  "purrbot": _rf_purrbot}

# --- circuit breakers: a (source, reaction) pair that fails 3x is paused 30 min; a whole source
# --- that fails 6x in a row is paused 10 min. If EVERYTHING for a reaction is paused, we try anyway.
_react_pair_fails: dict[tuple[str, str], int] = {}
_react_pair_off: dict[tuple[str, str], float] = {}
_react_prov_fails: dict[str, int] = {}
_react_prov_off: dict[str, float] = {}


def _react_available(prov: str, cat: str) -> bool:
    now = time.time()
    return _react_pair_off.get((prov, cat), 0) <= now and _react_prov_off.get(prov, 0) <= now


def _react_ok(prov: str, cat: str) -> None:
    _react_pair_fails.pop((prov, cat), None)
    _react_prov_fails[prov] = 0


def _react_bad(prov: str, cat: str) -> None:
    now = time.time()
    n = _react_pair_fails.get((prov, cat), 0) + 1
    _react_pair_fails[(prov, cat)] = n
    if n >= 3:
        _react_pair_off[(prov, cat)] = now + 1800
        _react_pair_fails.pop((prov, cat), None)
        logger.warning("/react: pausing %s for '%s' for 30 minutes (3 failures)", prov, cat)
    pf = _react_prov_fails.get(prov, 0) + 1
    _react_prov_fails[prov] = pf
    if pf >= 6:
        _react_prov_off[prov] = now + 600
        _react_prov_fails[prov] = 0
        logger.warning("/react: pausing the whole %s source for 10 minutes (6 failures in a row)", prov)


async def _react_fetch(cat: str):
    """Returns (result dict, provider key, kind) or None. Random source first; up to 3 sources tried."""
    options = list(REACT_CATALOG.get(cat, []))
    live = [o for o in options if _react_available(o[0], cat)]
    options = live or options
    random.shuffle(options)
    for prov, remote, kind in options[:3]:
        result = await REACT_FETCHERS[prov](remote, kind)
        if result:
            _react_ok(prov, cat)
            return result, prov, kind
        _react_bad(prov, cat)
    return None


async def _react_refresh() -> None:
    """Read the live category lists from nekos.best and Gifukai (both document an endpoint for it)."""
    data = await _react_get("https://nekos.best/api/v2/endpoints", ua=True)
    if isinstance(data, dict):
        cats = {}
        for name, info in data.items():
            if isinstance(name, str) and _CAT_OK.match(name) and not _CAT_BAD.search(name) and isinstance(info, dict):
                cats[name] = (name, "gif" if info.get("format") == "gif" else "img")
        if len(cats) >= 10:                                    # ignore anything that looks truncated
            REACT_PROVIDERS["nekosbest"]["cats"] = dict(list(cats.items())[:200])
    data = await _react_get("https://api.gifukai.com/v1/actions", ua=True)
    actions = data.get("actions") if isinstance(data, dict) else None
    names = list(actions) if isinstance(actions, (dict, list)) else []
    names = [n for n in names if isinstance(n, str) and _CAT_OK.match(n) and not _CAT_BAD.search(n)]
    if len(names) >= 3:
        REACT_PROVIDERS["gifukai"]["cats"] = {n: (n, "gif") for n in names[:200]}
    _rebuild_react_catalog()
    logger.info("/react ready: %d reactions across %d sources", len(REACT_CATALOG),
                len([p for p in REACT_PROVIDERS if p not in REACT_DISABLE]))


async def _react_refresher() -> None:
    """Background task: refresh the category lists now and every 6 hours."""
    await bot.wait_until_ready()
    while True:
        try:
            await _react_refresh()
        except Exception:
            logger.exception("Couldn't refresh the /react category lists (the built-in lists still work)")
        await asyncio.sleep(6 * 3600)


_POPULAR_REACTIONS = ["hug", "pat", "kiss", "slap", "cry", "wave", "dance", "cuddle", "poke", "bite", "blush",
                      "wink", "laugh", "smile", "happy", "nom", "highfive", "handhold", "bonk", "yeet",
                      "facepalm", "smug", "pout", "sleep", "stare"]


@bot.tree.command(name="reactions", description="See every reaction you can use with /react.")
async def reactions_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    names = [f"{c} 🖼️" if all(kind == "img" for _, _, kind in REACT_CATALOG[c]) else c for c in sorted(REACT_CATALOG)]
    embed = discord.Embed(
        title=f"🎭 {len(names)} reactions for /react",
        description=" · ".join(names)[:4000],
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use /react, type any of these names, then pick the person · 🖼️ = a still picture, not a gif")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _reaction_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    needle = current.strip().lower().removesuffix("🖼️").strip()
    cats = sorted(REACT_CATALOG)
    if not needle:
        # Discord shows at most 25 suggestions, so lead with the popular ones (not just a-z).
        ordered = [c for c in _POPULAR_REACTIONS if c in REACT_CATALOG]
        ordered += [c for c in cats if c not in ordered]
    else:
        ordered = [c for c in cats if c.startswith(needle)] + [c for c in cats if needle in c and not c.startswith(needle)]
    out = []
    for c in ordered[:25]:
        pic_only = all(kind == "img" for _, _, kind in REACT_CATALOG[c])
        out.append(app_commands.Choice(name=f"{c} 🖼️" if pic_only else c, value=c))
    return out


@bot.tree.command(name="react", description="Send an anime reaction gif at someone (start typing to search).")
@app_commands.describe(reaction="Which reaction (start typing to search)", user="Who to react at")
@app_commands.autocomplete(reaction=_reaction_autocomplete)
async def react_slash(interaction: discord.Interaction, reaction: str, user: discord.Member):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    reaction_clean = reaction.strip().lower().removesuffix("🖼️").strip()
    if reaction_clean not in REACT_CATALOG:
        await interaction.response.send_message(
            f"`{reaction}` isn't a reaction I know — start typing in the `reaction` "
            "field and pick one of the suggestions.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    fetched = await _react_fetch(reaction_clean)
    if fetched is None:
        await interaction.followup.send(
            f"{interaction.user.mention} reacts with **{reaction_clean}** at {user.mention}! {MILO_EMOJI}\n"
            "*(the gif sites are all being shy right now — try again in a bit)*"
        )
        return
    result, prov, kind = fetched
    if kind == "img":
        text = f"{interaction.user.mention} sends a **{reaction_clean}** pic to {user.mention}! {MILO_EMOJI}"
    else:
        text = f"{interaction.user.mention} reacts with **{reaction_clean}** at {user.mention}! {MILO_EMOJI}"
    embed = discord.Embed(description=text, color=discord.Color.green())
    embed.set_image(url=result["url"])
    credit = [f"via {REACT_PROVIDERS[prov]['label']}"]
    if isinstance(result.get("anime"), str) and result["anime"].strip():
        credit.append(result["anime"].strip()[:80])
    if kind == "img" and isinstance(result.get("artist"), str) and result["artist"].strip():
        credit.append(f"art by {result['artist'].strip()[:60]}")
    embed.set_footer(text=" · ".join(credit))
    await interaction.followup.send(embed=embed)


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


# ---------------------------------------------------------------------------
# /weather
#
# Open-Meteo's free API limits by IP address, and on shared hosting (Render)
# every app on the same IP shares that quota -- so it regularly answers 429
# ("too many requests") even though nothing is wrong with Milo or Open-Meteo.
# Defences: (1) a backup provider (MET Norway), (2) a short cache so repeat
# lookups don't spend quota at all.
# ---------------------------------------------------------------------------
WEATHER_CACHE_SECONDS = 600       # weather barely changes in 10 minutes
GEOCODE_CACHE_SECONDS = 86400     # places don't move
_weather_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_geocode_cache: dict[str, tuple[float, list]] = {}


def _prune_cache(cache: dict, max_entries: int = 500) -> None:
    if len(cache) > max_entries:
        for key in sorted(cache, key=lambda k: cache[k][0])[: len(cache) - max_entries]:
            del cache[key]


async def _weather_open_meteo(lat: float, lon: float) -> dict | None:
    data = await _fetch_json(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon, "current_weather": "true"},
    )
    current = data.get("current_weather") if isinstance(data, dict) else None
    if not current or current.get("temperature") is None:
        return None
    return {"temp": current["temperature"], "wind": current.get("windspeed"), "source": "Open-Meteo"}


async def _weather_metno(lat: float, lon: float) -> dict | None:
    # MET Norway asks every client to identify itself with a descriptive
    # User-Agent that includes a way to contact the developer.
    contact = f"+https://discord.com/users/{CREATOR_USER_ID}" if CREATOR_USER_ID else "+https://discord.com"
    data = await _fetch_json(
        "https://api.met.no/weatherapi/locationforecast/2.0/compact",
        params={"lat": f"{float(lat):.4f}", "lon": f"{float(lon):.4f}"},
        headers={"User-Agent": f"MiloDiscordBot/1.0 ({contact})"},
    )
    try:
        details = data["properties"]["timeseries"][0]["data"]["instant"]["details"]
        temp = details["air_temperature"]
    except (KeyError, IndexError, TypeError):
        return None
    wind_ms = details.get("wind_speed")  # metres/second -> km/h
    return {
        "temp": temp,
        "wind": round(wind_ms * 3.6, 1) if wind_ms is not None else None,
        "source": "MET Norway",
    }


WEATHER_PROVIDERS = [("Open-Meteo", _weather_open_meteo), ("MET Norway", _weather_metno)]


async def _get_weather(lat: float, lon: float) -> dict | None:
    key = (round(float(lat), 2), round(float(lon), 2))
    cached = _weather_cache.get(key)
    if cached and time.time() - cached[0] < WEATHER_CACHE_SECONDS:
        return cached[1]
    for name, provider in WEATHER_PROVIDERS:
        try:
            result = await provider(lat, lon)
        except Exception:
            logger.exception("Weather provider %s crashed", name)
            result = None
        if result:
            _weather_cache[key] = (time.time(), result)
            _prune_cache(_weather_cache)
            return result
        logger.warning("Weather provider %s gave no result; trying the next one", name)
    return None


def _pick_place(results: list[dict], hint: str) -> dict:
    """Choose among geocoding matches. With 'Bengaluru, India' the part after
    the comma narrows it to the match whose country/state/code fits."""
    hint = hint.strip().lower()
    if hint:
        for r in results:
            fields = {str(r.get(k, "")).lower() for k in ("country", "country_code", "admin1")} - {""}
            if hint in fields or (len(hint) >= 3 and any(hint in f for f in fields)):
                return r
    return results[0]


async def _geocode(name: str) -> list[dict] | None:
    """Matches for a place name, [] if there are none, None if the lookup failed."""
    key = name.lower()
    cached = _geocode_cache.get(key)
    if cached and time.time() - cached[0] < GEOCODE_CACHE_SECONDS:
        return cached[1]
    geo = await _fetch_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 10, "language": "en"},
    )
    if geo is None:
        return None
    results = geo.get("results") or []
    if results:
        _geocode_cache[key] = (time.time(), results)
        _prune_cache(_geocode_cache)
    return results


@bot.tree.command(name="weather", description="Get current weather for a place.")
@app_commands.describe(location="City, e.g. 'Bengaluru, India' or 'Sao Paulo' (add a country after a comma to be precise)")
async def weather_slash(interaction: discord.Interaction, location: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    name, _, hint = location.partition(",")
    name = name.strip() or location.strip()
    if not name:
        await interaction.response.send_message("Tell me a place to look up!", ephemeral=True)
        return

    await interaction.response.defer()
    results = await _geocode(name)
    if results is None:
        await interaction.followup.send(
            f"{MILO_EMOJI} I couldn't look that place up right now -- the free map service "
            "is busy. Try again in a few minutes."
        )
        return
    if not results:
        await interaction.followup.send(
            f"Couldn't find a place called **{name}**. Check the spelling "
            "(e.g. `Bengaluru`, not `Banglore`)."
        )
        return

    place = _pick_place(results, hint)
    place_name = place.get("name", name)
    admin1 = place.get("admin1")
    title_parts = [place_name]
    if admin1 and admin1 != place_name:
        title_parts.append(admin1)
    if place.get("country"):
        title_parts.append(place["country"])

    weather = await _get_weather(place["latitude"], place["longitude"])
    if not weather:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch the weather for **{place_name}** right now -- the free "
            "weather services limit busy servers like Milo's. Try again in a few minutes."
        )
        return

    embed = discord.Embed(
        title=f"🌦️ Weather in {', '.join(title_parts)}", color=discord.Color.blurple()
    )
    embed.add_field(name="Temperature", value=f"{weather['temp']}°C")
    if weather.get("wind") is not None:
        embed.add_field(name="Wind Speed", value=f"{weather['wind']} km/h")
    embed.set_footer(text=f"Source: {weather['source']}")
    await interaction.followup.send(embed=embed)


# Free, keyless translate providers get overwhelmed fast on shared hosting
# IPs like Render's -- many different apps hammer the same handful of free
# endpoints, so any single one can rate-limit the whole IP range regardless
# of how little *this* bot calls it. Trying several independent providers in
# order means one being blocked doesn't take the command down entirely.
async def _try_google_translate(text: str, target_lang: str) -> str | None:
    params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": text}
    data = await _fetch_json("https://translate.googleapis.com/translate_a/single", params=params)
    if data and isinstance(data, list) and data and data[0]:
        try:
            return "".join(seg[0] for seg in data[0] if seg and seg[0]) or None
        except (IndexError, TypeError):
            return None
    return None


async def _try_mymemory_translate(text: str, target_lang: str) -> str | None:
    params = {"q": text, "langpair": f"en|{target_lang}"}
    data = await _fetch_json("https://api.mymemory.translated.net/get", params=params)
    if data:
        translated = data.get("responseData", {}).get("translatedText")
        # MyMemory sometimes returns a 200 with an error/quota message
        # sitting in the translatedText field instead of a real translation.
        if translated and "QUOTA" not in translated.upper() and "MYMEMORY WARNING" not in translated.upper():
            return translated
    return None


async def _try_libretranslate(text: str, target_lang: str) -> str | None:
    payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
    try:
        async with bot.session.post("https://libretranslate.com/translate", json=payload) as resp:
            if resp.status != 200:
                logger.warning("LibreTranslate returned status %s", resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception:
        logger.exception("LibreTranslate request failed")
        return None
    return data.get("translatedText") if data else None


TRANSLATE_PROVIDERS = [
    ("Google Translate", _try_google_translate),
    ("MyMemory", _try_mymemory_translate),
    ("LibreTranslate", _try_libretranslate),
]


@bot.tree.command(name="translate", description="Translate text to another language.")
@app_commands.describe(text="Text to translate", to="Target language code, e.g. 'es', 'fr', 'hi'")
async def translate_slash(interaction: discord.Interaction, text: str, to: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    target_lang = to.strip().lower()

    translated = None
    used_provider = None
    for provider_name, provider_fn in TRANSLATE_PROVIDERS:
        try:
            translated = await provider_fn(text, target_lang)
        except Exception:
            logger.exception("%s translate provider raised an exception", provider_name)
            translated = None
        if translated:
            used_provider = provider_name
            break

    if not translated:
        logger.warning(
            "All translate providers failed for text=%r to=%r", text, to
        )
        await interaction.followup.send(
            "Couldn't translate that — all translation services seem to be rate-limited "
            "or down right now. Try again in a bit, or try a different language code "
            "(e.g. `es`, `fr`, `hi`)."
        )
        return

    logger.info("Translated via %s", used_provider)
    embed = discord.Embed(color=discord.Color.blurple())
    embed.add_field(name="Original", value=text, inline=False)
    embed.add_field(name=f"Translated ({target_lang})", value=translated, inline=False)
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


# Matches any Discord markup token wrapped in angle brackets: custom emoji
# (<:name:id> / <a:name:id>), user/role/channel mentions (<@id>, <@&id>,
# <#id>), and timestamps (<t:...>). These need to pass through completely
# untouched -- shredding their letters/digits into emoji breaks them so
# Discord can no longer recognize them at all.
_DISCORD_TOKEN_RE = re.compile(r"(<[^<>]+>)")


def _emojify_text(text: str) -> str:
    regional_offset = ord("🇦") - ord("a")
    # Two regional-indicator letters placed directly next to each other get
    # auto-merged into a country flag by Discord's renderer (e.g. "H"+"I"
    # becomes a flag instead of two boxed letters). A zero-width space
    # between them is invisible but breaks that adjacency, so each letter
    # renders on its own.
    zwsp = "\u200b"

    def emojify_plain(segment: str) -> str:
        parts = []
        for char in segment.lower():
            if char.isalpha() and "a" <= char <= "z":
                parts.append(chr(ord(char) + regional_offset) + zwsp)
            elif char.isdigit():
                parts.append(f"{char}\ufe0f\u20e3")  # keycap digit emoji
            elif char == " ":
                parts.append("  ")  # a little extra gap reads better between emoji
            else:
                parts.append(char)  # leave punctuation, unicode emoji, etc. as-is
        return "".join(parts)

    segments = _DISCORD_TOKEN_RE.split(text)
    result = []
    for segment in segments:
        if _DISCORD_TOKEN_RE.fullmatch(segment):
            result.append(segment)  # a Discord token -- leave completely untouched
        else:
            result.append(emojify_plain(segment))
    return "".join(result)


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

    if not CAT_API_KEY:
        await interaction.response.send_message(
            f"{MILO_EMOJI} `/catbreed` needs a free TheCatAPI key to work -- ask "
            "whoever runs this bot to grab one at thecatapi.com/signup and set "
            "`CAT_API_KEY`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    data = await _fetch_json(
        "https://api.thecatapi.com/v1/breeds",
        headers={"x-api-key": CAT_API_KEY},
    )
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


# ---------------------------------------------------------------------------
# Even more fun: /fortune, /compliment, /icebreaker, /mock, /tableflip, /unflip
# All pure local logic -- no network calls, so nothing here can ever break
# the way /define did.
# ---------------------------------------------------------------------------
FORTUNES = [
    "A thrilling time is in your near future.",
    "The best way to predict the future is to create it.",
    "Your hard work is about to pay off in an unexpected way.",
    "A pleasant surprise is waiting for you this week.",
    "Good things come to those who wait... but better things come to those who work for it.",
    "An old friend will bring good news your way.",
    "Now is the time to try something new.",
    "Your creativity will lead you to new horizons.",
    "A small act of kindness today will come back to you tenfold.",
    "The next chapter of your life is about to begin.",
    "You will find success where you least expect it.",
    "Trust the timing of your life.",
    "A great change is coming -- embrace it.",
    "Someone is thinking of you right now.",
    "Your patience will soon be rewarded.",
    f"{MILO_EMOJI} A cat's purr is a sign that good fortune is near.",
    "Opportunity is knocking -- don't let it walk away.",
    "What you focus on grows. Choose wisely.",
    "A conversation today could change your week.",
    "You are closer to your goal than you think.",
    "Say yes to the thing that scares you a little.",
    "The universe rewards those who show up consistently.",
    "A fresh start is closer than it appears.",
    "Someone you haven't met yet will become important to you.",
    "Your next idea is better than you give it credit for.",
    "Slow progress is still progress.",
    "The risk you're avoiding might be the one worth taking.",
    "Laughter today brings luck tomorrow.",
    "A door you thought was closed is actually unlocked.",
    "You're about to learn something that changes your perspective.",
]

COMPLIMENTS = [
    "has really solid taste.",
    "makes this server better just by being in it.",
    "is probably the funniest person in this chat.",
    "gives great advice when it actually matters.",
    "has main character energy.",
    "is more talented than they realize.",
    "always knows exactly what to say.",
    "is the friend everyone wishes they had.",
    "has impeccable vibes.",
    "deserves more credit than they get.",
    "brightens up every conversation they're in.",
    "is quietly one of the most reliable people here.",
    "has a way of making hard days easier.",
    "notices the little things most people miss.",
    "is someone people genuinely enjoy being around.",
    "has great instincts.",
    "always shows up when it counts.",
    "has a talent for making people feel welcome.",
    "brings a calm energy to the chaos.",
    "is effortlessly cool.",
    "has one of the best senses of humor around here.",
    "makes everyone else's ideas better.",
    "is criminally underrated.",
    "has a gift for turning strangers into friends.",
    "just has that main character energy, no notes.",
]

ICEBREAKERS = [
    "What's a hobby you picked up recently?",
    "What's the best game you've played this year?",
    "If you could instantly master one skill, what would it be?",
    "What's a small thing that always makes your day better?",
    "What's your most-used emoji and why?",
    "What's a movie or show you could rewatch forever?",
    "If you had a free weekend with no obligations, what would you do?",
    "What's something you're weirdly good at?",
    "What's the last thing that made you laugh out loud?",
    "What's a food you could never get tired of?",
    "If this server had a mascot other than Milo, what would it be?",
    "What's a game or hobby you want to get into but haven't started yet?",
    "What's a skill you think everyone should learn?",
    "What's the most useless talent you have?",
    "What's a piece of advice you'd give your younger self?",
    "What's your go-to comfort food?",
    "What's a place you've always wanted to visit?",
    "What's a book, show, or game that changed how you think?",
    "What's your ideal way to spend a rainy day?",
    "What's a trend you just don't get?",
    "If you could have dinner with anyone, living or fictional, who would it be?",
    "What's the best piece of advice you've ever received?",
    "What's something you're really proud of?",
    "What's a childhood memory that still makes you smile?",
    "If you could instantly learn any language, which would you pick?",
]


@bot.tree.command(name="fortune", description="Get a fortune-cookie-style message.")
async def fortune_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.send_message(f"🥠 {random.choice(FORTUNES)}")


@bot.tree.command(name="compliment", description="Give someone a random compliment.")
@app_commands.describe(user="Who to compliment (defaults to you)")
async def compliment_slash(interaction: discord.Interaction, user: discord.Member | None = None):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    target = user or interaction.user
    await interaction.response.send_message(
        f"✨ {target.mention} {random.choice(COMPLIMENTS)}"
    )


@bot.tree.command(name="icebreaker", description="Get a random conversation-starter question.")
async def icebreaker_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.send_message(f"💬 {random.choice(ICEBREAKERS)}")


@bot.tree.command(name="mock", description="sPoNgEbOb CaSe your text.")
@app_commands.describe(text="The text to mock")
async def mock_slash(interaction: discord.Interaction, text: str):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    clean_text = text.strip()
    if not clean_text:
        await interaction.response.send_message("Give me some text to mock!", ephemeral=True)
        return
    if len(clean_text) > 300:
        clean_text = clean_text[:300]

    mocked = "".join(
        char.upper() if i % 2 == 0 else char.lower()
        for i, char in enumerate(clean_text)
    )
    await interaction.response.send_message(mocked)


@bot.tree.command(name="tableflip", description="(╯°□°)╯︵ ┻━┻")
async def tableflip_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.send_message("(╯°□°)╯︵ ┻━┻")


@bot.tree.command(name="unflip", description="┬─┬ ノ( ゜-゜ノ)")
async def unflip_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.send_message("┬─┬ ノ( ゜-゜ノ)")


# /throw mirrors YAGPDB's own /fun throw command (same probability bands and
# message format, confirmed from its public source code): 5% chance of a
# triple throw, 10% chance of a double throw, otherwise a single throw.
THROWABLE_ITEMS = [
    "a remote-controlled car that only goes backwards",
    "a Roomba that's given up on life",
    "a Wi-Fi router with exactly one bar of signal",
    "a Jenga tower mid-collapse",
    "a fax machine from 1998",
    "a Magic 8-Ball that only ever says 'ask again later'",
    "a Bluetooth speaker stuck on 3% battery",
    "a printer that's perpetually out of toner",
    "a Tamagotchi that is deeply disappointed in you",
    "a GPS that keeps recalculating",
    "a microwave that beeps exactly once at 3am for no reason",
    f"a disapproving cat stare {MILO_EMOJI}",
    "a Roku remote with no batteries in it",
    "a rubber chicken with a pulley in the middle",
    "a Nokia 3310 (it survives the throw, obviously)",
    "a Dyson vacuum that's purely for decoration",
    "an aux cord that doesn't actually connect to anything",
    "a fidget spinner, five years too late",
    "a CAPTCHA that insists you're a robot",
    "the sound of a 56k dial-up modem",
    "a participation trophy",
    "a to-do list with everything crossed off except the one important thing",
    "a single AirPod -- the left one, it's always the left one",
    "an email that just says 'per my last email'",
    "a Zoom call with someone's cat walking across the keyboard",
    "a slice of leftover pizza that's somehow still good",
    "a Roomba that's stuck fighting a cat",
    "a Discord notification at 3am",
    "a printer jam of truly biblical proportions",
    "a 'you have 1 new message' notification from 2014",
    "a Monopoly board mid-argument",
    "a Scooby-Doo villain mask",
    "a cardboard cutout of a disappointed parent",
    "a chair with one wheel that won't stop spinning",
]


@bot.tree.command(name="throw", description="Throw something at someone (or a random nearby person).")
@app_commands.describe(user="Who to throw at (leave blank for a random nearby person)")
async def throw_slash(interaction: discord.Interaction, user: discord.Member | None = None):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    target = user.mention if user else "a random person nearby"

    roll = random.randint(0, 99)
    if roll < 5:
        items = random.sample(THROWABLE_ITEMS, 3)
        text = (
            f"🎯 TRIPLE THROW! Threw **{items[0]}**, **{items[1]}**, and "
            f"**{items[2]}** at {target}!"
        )
    elif roll < 15:
        items = random.sample(THROWABLE_ITEMS, 2)
        text = f"🎯 DOUBLE THROW! Threw **{items[0]}** and **{items[1]}** at {target}!"
    else:
        item = random.choice(THROWABLE_ITEMS)
        text = f"🎯 Threw **{item}** at {target}!"

    await interaction.response.send_message(text)


# ---------------------------------------------------------------------------
# More free-API fun: /joke, /advice, /comic, /meme
# Picked for having long, stable track records (unlike dictionaryapi.dev,
# which turned out to have a chronic multi-week outage -- see /define's
# removal in an earlier version of this file).
# ---------------------------------------------------------------------------
@bot.tree.command(name="joke", description="Get a random dad joke.")
async def joke_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json(
        "https://icanhazdadjoke.com/", headers={"Accept": "application/json"}
    )
    joke = data.get("joke") if data else None
    if not joke:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch a joke right now — try again soon."
        )
        return
    await interaction.followup.send(f"😹 {joke}")


@bot.tree.command(name="advice", description="Get a random piece of life advice.")
async def advice_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://api.adviceslip.com/advice")
    advice = data.get("slip", {}).get("advice") if data else None
    if not advice:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch advice right now — try again soon."
        )
        return
    await interaction.followup.send(f"💡 {advice}")


@bot.tree.command(name="comic", description="Get a random xkcd comic.")
async def comic_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    latest = await _fetch_json("https://xkcd.com/info.0.json")
    max_num = latest.get("num") if latest else None
    if not max_num:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch a comic right now — try again soon."
        )
        return

    comic_num = random.randint(1, max_num)
    data = await _fetch_json(f"https://xkcd.com/{comic_num}/info.0.json")
    if not data or not data.get("img"):
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch a comic right now — try again soon."
        )
        return

    embed = discord.Embed(
        title=f"xkcd #{data.get('num')}: {data.get('title', '')}",
        color=discord.Color.blurple(),
    )
    embed.set_image(url=data["img"])
    if data.get("alt"):
        embed.set_footer(text=data["alt"][:2048])
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="meme", description="Get a random meme.")
async def meme_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    data = await _fetch_json("https://meme-api.com/gimme")
    image_url = data.get("url") if data else None
    title = data.get("title") if data else None
    if not image_url:
        await interaction.followup.send(
            f"{MILO_EMOJI} Couldn't fetch a meme right now — try again soon."
        )
        return

    embed = discord.Embed(title=title or "Random meme", color=discord.Color.green())
    embed.set_image(url=image_url)
    if data.get("subreddit"):
        embed.set_footer(text=f"r/{data['subreddit']}")
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /ask: quick AI-backed Q&A via Groq (free tier, no credit card).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /ask "think" tiers
#
# Under a normal /ask answer, optional buttons re-ask the SAME question with a
# smarter Groq model. Each button is a "tier" turned on by an env var:
#
#   GROQ_THINK_MODEL   -> 🧠 "Think harder"   (e.g. openai/gpt-oss-120b)
#   GROQ_SECOND_MODEL  -> 🔮 "Second opinion" (e.g. qwen/qwen3.8-27b)
#
# Groq tracks free-tier limits PER MODEL, so each tier spends its own separate
# allowance and never touches the fast model's. Same GROQ_API_KEY for all.
#
# Anti-bypass rules (all enforced in _handle_think_click / _claim_think):
#   * only the person who ran /ask can press the buttons
#   * buttons stop working 10 minutes after the answer (old answers error out)
#   * one press per answer per tier, even across restarts when Upstash is set
#   * a per-person daily limit, a server-wide daily cap, and a short cooldown
#   * one think at a time per tier (also keeps Groq's tokens-per-minute happy)
# Failed calls refund everything, so an error never costs anyone a use.
# ---------------------------------------------------------------------------
GROQ_THINK_MODEL = os.getenv("GROQ_THINK_MODEL", "").strip()
GROQ_SECOND_MODEL = os.getenv("GROQ_SECOND_MODEL", "").strip()
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
# "Daily" limits reset at midnight in this timezone (IANA name).
LIMIT_RESET_TZ = os.getenv("LIMIT_RESET_TZ", "").strip() or "Asia/Kolkata"

THINK_BUTTON_PREFIX = "think:"
THINK_BUTTON_WINDOW_SECONDS = 10 * 60


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r (using %d)", name, raw, default)
        return default


@dataclass(frozen=True)
class AskTier:
    key: str            # goes into button custom_ids and storage keys
    label: str          # button text
    emoji: str
    model: str
    style: discord.ButtonStyle
    user_daily: int     # uses per person per day
    global_daily: int   # uses by the whole server per day
    cooldown: int       # seconds between one person's uses
    max_tokens: int     # reply budget (reasoning tokens count too)
    effort: str         # reasoning effort, only sent to gpt-oss models


def _build_tiers() -> list[AskTier]:
    # Defaults assume a small server (~10 people) and Groq's free allowance of
    # 200,000 tokens/day per model; a think costs roughly 2,000-3,500 tokens.
    tiers: list[AskTier] = []
    if GROQ_THINK_MODEL:
        tiers.append(AskTier(
            "smart", "Think harder", "🧠", GROQ_THINK_MODEL, discord.ButtonStyle.primary,
            _env_int("THINK_USER_DAILY", 3), _env_int("THINK_GLOBAL_DAILY", 40),
            60, 3000, "high",
        ))
    if GROQ_SECOND_MODEL:
        tiers.append(AskTier(
            "second", "Second opinion", "🔮", GROQ_SECOND_MODEL, discord.ButtonStyle.secondary,
            _env_int("SECOND_USER_DAILY", 2), _env_int("SECOND_GLOBAL_DAILY", 25),
            60, 2500, "medium",
        ))
    return tiers


THINK_TIERS: list[AskTier] = _build_tiers()
_TIER_BY_KEY: dict[str, AskTier] = {t.key: t for t in THINK_TIERS}
_think_locks: dict[str, asyncio.Lock] = {}


# ---- "what day is it" in the reset timezone -------------------------------
def _load_limit_tz():
    try:
        return ZoneInfo(LIMIT_RESET_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning("Unknown LIMIT_RESET_TZ=%r -- falling back to UTC.", LIMIT_RESET_TZ)
        return timezone.utc


_LIMIT_TZ = _load_limit_tz()


def _day_info(now: datetime | None = None) -> tuple[str, int]:
    """(today's date in the reset timezone, seconds until the next midnight there)."""
    now = (now or datetime.now(_LIMIT_TZ)).astimezone(_LIMIT_TZ)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return now.strftime("%Y-%m-%d"), max(1, int((tomorrow - now).total_seconds()))


def _format_wait(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


# ---- where the limit counters live -----------------------------------------
class StoreError(Exception):
    """The limit store couldn't be reached or gave a bad answer."""


class MemoryStore:
    """Counters kept in Milo's own memory. Zero setup, but they reset whenever
    Milo restarts or redeploys -- so a restart hands everyone fresh uses."""

    persistent = False

    def __init__(self) -> None:
        self._data: dict[str, list] = {}  # key -> [count, expires_at]

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, (_, exp) in self._data.items() if exp <= now]:
            del self._data[k]

    def _get(self, key: str):
        entry = self._data.get(key)
        if entry is not None and entry[1] <= time.time():
            del self._data[key]
            return None
        return entry

    async def incr(self, key: str, ttl: int) -> int:
        if len(self._data) > 2000:
            self._purge()
        entry = self._get(key)
        if entry is None:
            self._data[key] = [1, time.time() + ttl]
            return 1
        entry[0] += 1
        return entry[0]

    async def decr(self, key: str) -> None:
        entry = self._get(key)
        if entry is not None:
            entry[0] = max(0, entry[0] - 1)

    async def set_nx(self, key: str, ttl: int) -> bool:
        if self._get(key) is not None:
            return False
        self._data[key] = [1, time.time() + ttl]
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def ttl(self, key: str) -> int:
        entry = self._get(key)
        return max(1, int(entry[1] - time.time())) if entry else 0


class UpstashStore:
    """Counters kept in Upstash Redis (free tier) over its REST API, so they
    survive restarts. Talks plain HTTPS -- no extra Python package needed."""

    persistent = True

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _cmd(self, *args):
        try:
            async with bot.session.post(
                self._url,
                json=list(args),
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200 or not isinstance(data, dict) or "error" in data:
                    raise StoreError(f"Upstash replied {resp.status}: {str(data)[:200]}")
                return data.get("result")
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"Upstash request failed: {exc!r}") from exc

    async def incr(self, key: str, ttl: int) -> int:
        count = int(await self._cmd("INCR", key))
        if count == 1:  # first use -> start the expiry clock
            await self._cmd("EXPIRE", key, ttl)
        return count

    async def decr(self, key: str) -> None:
        await self._cmd("DECR", key)

    async def set_nx(self, key: str, ttl: int) -> bool:
        return (await self._cmd("SET", key, "1", "NX", "EX", ttl)) == "OK"

    async def delete(self, key: str) -> None:
        await self._cmd("DEL", key)

    async def ttl(self, key: str) -> int:
        remaining = await self._cmd("TTL", key)
        return max(1, int(remaining)) if remaining is not None and int(remaining) > 0 else 0

    async def pipeline(self, commands: list[list]) -> list:
        """Send several commands in ONE request. Upstash still bills each
        command separately, but a save finishes in a single round trip -- which
        matters when Render only gives us ~30 seconds at shutdown. Replies come
        back in order, each either {"result": ...} or {"error": "..."}; the
        commands run one after another and are NOT all-or-nothing."""
        try:
            async with bot.session.post(
                f"{self._url}/pipeline",
                json=commands,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200 or not isinstance(data, list) or len(data) != len(commands):
                    raise StoreError(f"Upstash pipeline replied {resp.status}: {str(data)[:200]}")
                return data
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"Upstash pipeline failed: {exc!r}") from exc


LIMIT_STORE = UpstashStore(UPSTASH_URL, UPSTASH_TOKEN) if (UPSTASH_URL and UPSTASH_TOKEN) else MemoryStore()


# ---- claiming a use ----------------------------------------------------------
class ThinkClaim:
    """The result of trying to spend one think. `error` is None on success.
    On success, call release() if the AI call then fails, to refund every
    counter this claim touched."""

    def __init__(self, tier: AskTier) -> None:
        self.tier = tier
        self.error: str | None = None
        self.retry = 0            # seconds until trying again makes sense
        self.user_used = 0        # how many of today's uses this person has spent
        self._undo: list = []

    async def release(self) -> None:
        while self._undo:
            undo = self._undo.pop()
            try:
                await undo()
            except StoreError:
                logger.warning("Couldn't roll back a think limit counter")


async def _claim_think(tier: AskTier, user_id: int, message_id: int) -> ThinkClaim:
    """Check every rule and, if all pass, reserve one use. Order matters: the
    most informative refusal wins (daily limit before cooldown, etc.)."""
    claim = ThinkClaim(tier)
    day, secs_to_reset = _day_info()
    user_key = f"think:{tier.key}:{user_id}:{day}"
    global_key = f"thinkglobal:{tier.key}:{day}"
    cooldown_key = f"thinkcd:{tier.key}:{user_id}"
    message_key = f"thinkmsg:{tier.key}:{message_id}"

    async def refuse(code: str, retry: int = 0) -> ThinkClaim:
        claim.error, claim.retry = code, retry
        await claim.release()
        return claim

    try:
        used = await LIMIT_STORE.incr(user_key, secs_to_reset + 60)
        claim._undo.append(lambda: LIMIT_STORE.decr(user_key))
        if used > tier.user_daily:
            return await refuse("user_limit", secs_to_reset)
        claim.user_used = used

        total = await LIMIT_STORE.incr(global_key, secs_to_reset + 60)
        claim._undo.append(lambda: LIMIT_STORE.decr(global_key))
        if total > tier.global_daily:
            return await refuse("global_limit", secs_to_reset)

        if not await LIMIT_STORE.set_nx(cooldown_key, tier.cooldown):
            return await refuse("cooldown", await LIMIT_STORE.ttl(cooldown_key) or 1)
        claim._undo.append(lambda: LIMIT_STORE.delete(cooldown_key))

        # One press per answer per tier (the key outlives the button window).
        if not await LIMIT_STORE.set_nx(message_key, THINK_BUTTON_WINDOW_SECONDS * 2):
            return await refuse("used")
        claim._undo.append(lambda: LIMIT_STORE.delete(message_key))
    except StoreError:
        logger.exception("Limit store failed while claiming a think")
        claim.error = "store"
        await claim.release()
    return claim


# ---------------------------------------------------------------------------
# Points economy: /slots, /points, /leaderboard, and the hidden ?updateDB
#
# HOW SAVING WORKS ("memory first, saved in the background")
#   * Everything people do (spins, points, rank) is read and changed in Milo's
#     memory, so commands are instant and cost ZERO Upstash commands -- spam
#     can't touch the database budget.
#   * Changes are saved to Upstash as DELTAS ("+40 since last save"), never as
#     totals ("balance = 140"). Deltas can't overwrite someone's points with a
#     stale number -- important because Render runs the old and new Milo side
#     by side for ~60 seconds during a deploy.
#   * Saves happen: every POINTS_SAVE_MINUTES (default 5), when Render shuts
#     Milo down (SIGTERM, ~30 s grace), and on demand with ?updateDB.
#   * On startup Milo LOADS everyone's points before /slots works. If loading
#     fails, points switch off with a friendly message (they never start from
#     zero and then overwrite the real data), and loading retries every minute.
#   * Daily counters reset at midnight in LIMIT_RESET_TZ (default IST).
#   * Things that are fine to lose stay memory-only: the pity counter and the
#     spin cooldown.
# ---------------------------------------------------------------------------
def _parse_id_set(env_name: str) -> set[int]:
    ids: set[int] = set()
    for tok in re.split(r"[,\s]+", os.getenv(env_name, "").strip()):
        if not tok:
            continue
        if tok.isdigit():
            ids.add(int(tok))
        else:
            logger.warning("Ignoring invalid %s entry: %r", env_name, tok)
    return ids


# Channels where /slots works (blank = anywhere). Keeps spins out of #general.
SLOTS_CHANNEL_IDS: set[int] = _parse_id_set("SLOTS_CHANNEL_IDS")
SLOTS_FREE_SPINS = _env_int("SLOTS_FREE_SPINS", 25)    # free spins per person per day
# Bonus spins come in PACKS: pay SLOTS_PACK_COST points once, get SLOTS_PACK_SPINS spins.
SLOTS_PACK_COST = _env_int("SLOTS_PACK_COST", 5)
SLOTS_PACK_SPINS = _env_int("SLOTS_PACK_SPINS", 10)
# Most bonus spins per person per day (rounded down to whole packs; default 2 packs).
SLOTS_PAID_SPINS = max(SLOTS_PACK_SPINS, (_env_int("SLOTS_PAID_SPINS", 2 * SLOTS_PACK_SPINS) // SLOTS_PACK_SPINS) * SLOTS_PACK_SPINS)
SLOTS_COOLDOWN_SECONDS = 5                             # between one person's spins
SLOTS_PITY_AT = 20                                     # the 20th spin without ANY win always wins small (0 = off)
SLOTS_JACKPOT_PITY_AT = _env_int("SLOTS_JACKPOT_PITY_AT", 200)   # the 200th spin since your last 777 IS a 777 (saved, survives restarts)
SLOT_JACKPOT = "7️⃣"
LEADERBOARD_MIN_POINTS = _env_int("LEADERBOARD_MIN_POINTS", 1000)
POINTS_SAVE_SECONDS = _env_int("POINTS_SAVE_MINUTES", 5) * 60
POINTS_RESYNC_DELAY = 120   # one re-load after startup, once Render's old/new overlap is over
_PIPELINE_CHUNK = 200

# Pays only when all three reels match. Each triple is a 1-in-216 chance.
# Mid tiers were trimmed when the 200-spin jackpot guarantee was added, so the average
# income per spin stays about the same (~1.18 points) instead of jumping by a quarter.
SLOT_PAYOUTS = {"7️⃣": 100, "⭐": 25, "🍇": 12, "🍉": 8, "🍋": 6, "🍒": 4}
SLOT_SYMBOLS = list(SLOT_PAYOUTS)
SLOT_PITY_SYMBOLS = ["🍒", "🍋"]   # pity only ever pays the LOW tier, so jackpots stay rare

POINTS_KEY_BALANCE = "milo:points:balance"   # HASH  user id -> current balance
POINTS_KEY_EARNED = "milo:points:earned"     # ZSET  user id -> lifetime points earned (the leaderboard)
POINTS_KEY_DAY = "milo:points:day:"          # + YYYY-MM-DD, HASH "<id>:f"/"<id>:p" -> spins used, "<id>:s" -> points /pay'd that day
POINTS_KEY_DAILY = "milo:points:daily"       # HASH  user id -> "YYYY-MM-DD:streak" (last /daily claim)
POINTS_KEY_STATS = "milo:points:stats"       # HASH  user id -> JSON of Gamblin stats
POINTS_KEY_JP = "milo:points:jackpot"        # HASH  user id -> spins since their last 777 (the 200-spin guarantee)
POINTS_KEY_FROZEN = "milo:points:frozen"     # SET   user ids a manager has frozen
POINTS_KEY_SHOP = "milo:points:shop"         # HASH  user id -> JSON {"own": [item ids], "eq": {category: item id}}
POINTS_KEY_SCHED = "milo:gamblin:schedule"   # STRING JSON list of scheduled Gamblin games
LOTTERY_KEY_STATE = "milo:lottery:state"     # STRING JSON {"carry": jackpot, "drawn": last drawn day, "last": last result}
LOTTERY_KEY_TICKETS = "milo:lottery:t:"      # + YYYY-MM-DD, HASH "<id>:<n>" -> "a,b,c" (that day's tickets)
LOTTERY_RANGE = 15                            # numbers 1-15 (15 buttons fit Discord's 5x5 limit)
LOTTERY_PICKS = 3
LOTTERY_COST = _env_int("LOTTERY_TICKET_COST", 10)
LOTTERY_MAX_TICKETS = _env_int("LOTTERY_MAX_TICKETS", 5)
LOTTERY_SEED = _env_int("LOTTERY_JACKPOT_SEED", 50)      # the jackpot restarts here after a win
LOTTERY_PRIZE_2 = LOTTERY_COST * 4                       # 2 matches pays 4x a ticket; 1 match refunds the ticket
LOTTERY_CUT = max(1, (LOTTERY_COST * 3 + 10) // 20)     # ~15% of each ticket feeds the jackpot (rounded to nearest)
_raw_lottery_channel = os.getenv("LOTTERY_CHANNEL_ID", "").strip()
LOTTERY_CHANNEL_ID = int(_raw_lottery_channel) if _raw_lottery_channel.isdigit() else None

_POINTS_UNAVAILABLE = (
    f"{MILO_EMOJI} My {PTS_NAME} book isn't open right now (I'm having trouble reaching "
    "my storage). Give me a minute and try again!"
)


def _to_int(value) -> int:
    return int(float(value))


def frozen_block(uid: int) -> str | None:
    """A friendly note if a manager froze this person (no earning or gambling), else None."""
    if uid in POINTS.frozen:
        return f"🧊 Your {PTS_NAME} are frozen, so you can't earn, gamble or trade right now. Ask a manager if you think that's a mistake. {MILO_EMOJI}"
    return None


def _flat_pairs(result) -> list[tuple[str, object]]:
    """Redis replies for hashes/sorted sets arrive as a flat [k, v, k, v...]
    list (or, in some modes, pairs or a dict). Normalise to (key, value)."""
    if not result:
        return []
    if isinstance(result, dict):
        return [(str(k), v) for k, v in result.items()]
    if isinstance(result, list):
        if isinstance(result[0], (list, tuple)):
            return [(str(p[0]), p[1]) for p in result]
        return [(str(result[i]), result[i + 1]) for i in range(0, len(result) - 1, 2)]
    return []


class PointsManager:
    def __init__(self) -> None:
        self.ready = False
        self.balance: dict[int, int] = {}
        self.earned: dict[int, int] = {}
        self.spins: dict[tuple[str, int], list[int]] = {}      # (day, user) -> [free used, bonus used]
        # Changes made since the last successful save (what gets sent to Upstash):
        self._p_bal: dict[int, int] = {}
        self._p_earn: dict[int, int] = {}
        self._p_spins: dict[tuple[str, int], list[int]] = {}
        self._expire_sent: set[str] = set()
        # Saved state added with /daily, /pay, the 200-spin guarantee, stats, freeze and scheduling:
        self.daily: dict[int, str] = {}                        # user -> "YYYY-MM-DD:streak"
        self.stats: dict[int, dict] = {}                       # user -> Gamblin stats
        self.jp: dict[int, int] = {}                           # user -> spins since their last 777
        self.frozen: set[int] = set()
        self.sent: dict[tuple[str, int], int] = {}             # (day, user) -> points /pay'd that day
        self.schedule: list[dict] = []                         # [{"ts": unix, "kind": "duck", "by": user id}]
        self._p_daily: dict[int, str] = {}
        self._p_stats: set[int] = set()
        self._p_jp: dict[int, int] = {}
        self._p_frozen: dict[int, bool] = {}
        self._p_sent: dict[tuple[str, int], int] = {}
        self._p_sched: str | None = None
        self.tickets: dict[str, dict[int, dict[int, tuple]]] = {}   # lottery: day -> user -> {n: (a, b, c)}
        self._p_tickets: dict[tuple[str, int, int], str] = {}
        self._expire_tk: set[str] = set()
        self.lottery: dict = {"carry": LOTTERY_SEED, "drawn": "", "last": None}
        self._p_lstate: str | None = None
        self.jp_wins: dict[tuple[str, int], int] = {}              # (day, user) -> 777s hit that day (for the midnight shout-out)
        self._p_jpw: dict[tuple[str, int], int] = {}
        self.shop: dict[int, dict] = {}                            # user -> {"own": [ids], "eq": {category: id}} (cosmetics they bought)
        self._p_shop: set[int] = set()
        # Memory-only, fine to lose:
        self.pity: dict[int, int] = {}
        self.last_spin: dict[int, float] = {}
        self._io_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_saved: float | None = None
        self.last_error: str | None = None
        self._save_fails = 0

    @property
    def store(self) -> "UpstashStore | None":
        return LIMIT_STORE if isinstance(LIMIT_STORE, UpstashStore) else None

    # ---- reading / changing (instant, no database calls) --------------------
    def used(self, uid: int, day: str | None = None) -> tuple[int, int]:
        free, bonus = self.spins.get((day or _day_info()[0], uid), (0, 0))
        return free, bonus

    def points(self, uid: int) -> int:
        return self.balance.get(uid, 0)

    def total_earned(self, uid: int) -> int:
        return self.earned.get(uid, 0)

    def record_spin(self, uid: int, *, paid: bool, cost: int, payout: int, day: str | None = None,
                    hit_777: bool = False) -> None:
        key = (day or _day_info()[0], uid)
        idx = 1 if paid else 0
        self.spins.setdefault(key, [0, 0])[idx] += 1
        self._p_spins.setdefault(key, [0, 0])[idx] += 1
        self.add_balance(uid, payout - (cost if paid else 0))
        if payout:
            self.earned[uid] = self.earned.get(uid, 0) + payout
            self._p_earn[uid] = self._p_earn.get(uid, 0) + payout
        # the 200-spin guarantee counter: +1 per spin, back to 0 on any 777
        if hit_777:
            self.jp_wins[key] = self.jp_wins.get(key, 0) + 1
            self._p_jpw[key] = self._p_jpw.get(key, 0) + 1
        live = self.jp.get(uid, 0)
        change = -live if hit_777 else 1
        if change:
            self.jp[uid] = live + change
            self._p_jp[uid] = self._p_jp.get(uid, 0) + change

    # ---- /daily, /pay, stats, freeze, schedule, backup ----------------------
    def daily_state(self, uid: int) -> tuple[str, int]:
        """(last claim day, current streak) -- ("", 0) if they've never claimed."""
        day, _, streak = self.daily.get(uid, "").partition(":")
        return (day, int(streak)) if streak.isdigit() else ("", 0)

    def claim_daily(self, uid: int, day: str, streak: int, reward: int) -> None:
        value = f"{day}:{streak}"
        self.daily[uid] = value
        self._p_daily[uid] = value
        self.add_balance(uid, reward)
        self.add_earned(uid, reward)

    def add_balance(self, uid: int, delta: int) -> None:
        if delta:
            self.balance[uid] = self.balance.get(uid, 0) + delta
            self._p_bal[uid] = self._p_bal.get(uid, 0) + delta

    def sent_today(self, uid: int, day: str | None = None) -> int:
        return self.sent.get((day or _day_info()[0], uid), 0)

    def record_pay(self, sender: int, recipient: int, amount: int, day: str, fee: int = 0) -> None:
        self.add_balance(sender, -(amount + fee))          # the fee is burned: nobody receives it
        self.add_balance(recipient, amount)
        self.sent[(day, sender)] = self.sent.get((day, sender), 0) + amount
        self._p_sent[(day, sender)] = self._p_sent.get((day, sender), 0) + amount

    def jackpot_winners(self, day: str) -> dict[int, int]:
        """Who hit 777 on that day, and how many times."""
        return {u: n for (d, u), n in self.jp_wins.items() if d == day and n > 0}

    def jp_count(self, uid: int) -> int:
        return self.jp.get(uid, 0)

    def record_session(self, uid: int, game: str, net: int, best_win: int, wagered: int) -> None:
        st = self.stats.setdefault(uid, {"s": 0, "net": 0, "bw": 0, "bs": 0, "wg": 0, "gm": {}})
        st["s"] += 1
        st["net"] += net
        st["wg"] += wagered
        st["bw"] = max(st["bw"], best_win)
        st["bs"] = max(st["bs"], net)
        st["gm"][game] = st["gm"].get(game, 0) + 1
        self._p_stats.add(uid)

    def ticket_count(self, uid: int, day: str) -> int:
        return len(self.tickets.get(day, {}).get(uid, {}))

    def tickets_sold(self, day: str) -> int:
        return sum(len(v) for v in self.tickets.get(day, {}).values())

    def _mark_lottery(self) -> None:
        self._p_lstate = json.dumps(self.lottery, separators=(",", ":"))

    def buy_ticket(self, uid: int, day: str, picks: tuple, cost: int, cut: int) -> None:
        """Take the cost, record the ticket (saved), and feed the jackpot."""
        self.add_balance(uid, -cost)
        mine = self.tickets.setdefault(day, {}).setdefault(uid, {})
        n = max(mine, default=-1) + 1
        mine[n] = tuple(picks)
        self._p_tickets[(day, uid, n)] = ",".join(str(x) for x in picks)
        self.lottery["carry"] += cut
        self._mark_lottery()

    def finish_lottery(self, day: str, carry: int, last: dict | None) -> None:
        self.lottery["drawn"], self.lottery["carry"] = day, carry
        if last is not None:
            self.lottery["last"] = last
        self._mark_lottery()

    # ---- shop (cosmetic designs) ----------------------------------------------
    def shop_state(self, uid: int) -> dict:
        return self.shop.get(uid) or {"own": [], "eq": {}}

    def owns(self, uid: int, item_id: str) -> bool:
        return item_id in self.shop_state(uid)["own"]

    def _shop_edit(self, uid: int) -> dict:
        st = self.shop.setdefault(uid, {"own": [], "eq": {}})
        self._p_shop.add(uid)
        return st

    def buy_item(self, uid: int, item_id: str, price: int) -> None:
        """Take the price, add the item, and equip it. Saved with the normal autosave."""
        self.add_balance(uid, -price)
        self.grant_item(uid, item_id, equip=True)

    def grant_item(self, uid: int, item_id: str, equip: bool = False) -> None:
        st = self._shop_edit(uid)
        if item_id not in st["own"]:
            st["own"] = sorted(st["own"] + [item_id])
        if equip:
            st["eq"][SHOP_ITEMS[item_id]["cat"]] = item_id

    def revoke_item(self, uid: int, item_id: str) -> bool:
        st = self.shop_state(uid)
        if item_id not in st["own"]:
            return False
        st = self._shop_edit(uid)
        st["own"] = [i for i in st["own"] if i != item_id]
        st["eq"] = {c: i for c, i in st["eq"].items() if i != item_id}
        return True

    def equip(self, uid: int, cat: str, item_id: str | None) -> None:
        """Equip a design you own, or None to go back to Classic."""
        st = self._shop_edit(uid)
        if item_id is None:
            st["eq"].pop(cat, None)
        else:
            st["eq"][cat] = item_id

    def set_frozen(self, uid: int, flag: bool) -> bool:
        """Returns False if nothing changed."""
        if (uid in self.frozen) == flag:
            return False
        (self.frozen.add if flag else self.frozen.discard)(uid)
        self._p_frozen[uid] = flag
        return True

    def set_schedule(self, items: list[dict]) -> None:
        self.schedule = items
        self._p_sched = json.dumps(items, separators=(",", ":"))

    def backup_data(self) -> dict:
        return {
            "version": 1, "created": int(time.time()), "day": _day_info()[0],
            "balance": {str(u): b for u, b in self.balance.items() if b > 0},
            "earned": {str(u): e for u, e in self.earned.items() if e > 0},
            "jackpot": {str(u): n for u, n in self.jp.items() if n > 0},
            "daily": dict((str(u), v) for u, v in self.daily.items()),
            "stats": {str(u): st for u, st in self.stats.items()},
            "frozen": sorted(self.frozen),
            "shop": {str(u): st for u, st in self.shop.items() if st.get("own")},
        }

    def restore_data(self, data: dict) -> dict[str, int]:
        """Apply a (validated) backup. Only people IN the file are touched, except the
        frozen list, which is set exactly. Saved as ordinary deltas. Returns counts."""
        counts = {"balance": 0, "earned": 0, "jackpot": 0, "daily": 0, "stats": 0, "frozen": 0, "shop": 0}
        for uid_s, value in data.get("balance", {}).items():
            uid, target = int(uid_s), max(0, int(value))
            d = target - self.balance.get(uid, 0)
            if d:
                self.add_balance(uid, d)
                counts["balance"] += 1
        for uid_s, value in data.get("earned", {}).items():
            uid, target = int(uid_s), max(0, int(value))
            d = target - self.earned.get(uid, 0)
            if d:
                self.earned[uid] = target
                self._p_earn[uid] = self._p_earn.get(uid, 0) + d
                counts["earned"] += 1
        for uid_s, value in data.get("jackpot", {}).items():
            uid, target = int(uid_s), max(0, int(value))
            d = target - self.jp.get(uid, 0)
            if d:
                self.jp[uid] = target
                self._p_jp[uid] = self._p_jp.get(uid, 0) + d
                counts["jackpot"] += 1
        for uid_s, value in data.get("daily", {}).items():
            uid = int(uid_s)
            if self.daily.get(uid) != value:
                self.daily[uid] = value
                self._p_daily[uid] = value
                counts["daily"] += 1
        for uid_s, st in data.get("stats", {}).items():
            uid = int(uid_s)
            if self.stats.get(uid) != st:
                self.stats[uid] = st
                self._p_stats.add(uid)
                counts["stats"] += 1
        for uid_s, st in data.get("shop", {}).items():
            uid = int(uid_s)
            if self.shop.get(uid) != st:
                self.shop[uid] = st
                self._p_shop.add(uid)
                counts["shop"] += 1
        wanted = {int(x) for x in data.get("frozen", [])}
        for uid in wanted - self.frozen:
            counts["frozen"] += self.set_frozen(uid, True)
        for uid in self.frozen - wanted:
            counts["frozen"] += self.set_frozen(uid, False)
        return counts

    def reset_earned_to_balance(self) -> int:
        """?resetleaderboard: everyone's all-time 'earned' becomes their current
        balance. Saved as ordinary deltas (ZINCRBY handles negatives). Returns how
        many people changed."""
        changed = 0
        for uid in set(self.earned) | set(self.balance):
            target = max(0, self.balance.get(uid, 0))
            d = target - self.earned.get(uid, 0)
            if d:
                self.earned[uid] = target
                self._p_earn[uid] = self._p_earn.get(uid, 0) + d
                changed += 1
        return changed

    def add_earned(self, uid: int, n: int) -> None:
        """Count winnings towards the all-time leaderboard (balance is untouched)."""
        if n > 0:
            self.earned[uid] = self.earned.get(uid, 0) + n
            self._p_earn[uid] = self._p_earn.get(uid, 0) + n

    def adjust_balance(self, uid: int, delta: int) -> None:
        """Manager change (?give / ?set / ?undo): balance only, never 'earned'."""
        self.add_balance(uid, delta)

    def apply_gamble(self, uid: int, delta: int) -> None:
        """Gamblin result: moves the balance only (the leaderboard credit comes when the game ends)."""
        self.add_balance(uid, delta)

    # ---- leaderboard ----------------------------------------------------------
    def qualified(self) -> list[tuple[int, int]]:
        board = [(u, e) for u, e in self.earned.items() if e >= LEADERBOARD_MIN_POINTS]
        return sorted(board, key=lambda t: (-t[1], t[0]))

    def rank_of(self, uid: int) -> int | None:
        for i, (u, _) in enumerate(self.qualified(), 1):
            if u == uid:
                return i
        return None

    def richest(self, n: int = 5) -> list[tuple[int, int]]:
        """Top current balances (what people are holding right now)."""
        board = [(u, b) for u, b in self.balance.items() if b > 0]
        return sorted(board, key=lambda t: (-t[1], t[0]))[:n]

    def closest(self, n: int = 3) -> list[tuple[int, int]]:
        near = [(u, e) for u, e in self.earned.items() if 0 < e < LEADERBOARD_MIN_POINTS]
        return sorted(near, key=lambda t: (-t[1], t[0]))[:n]

    # ---- saving ------------------------------------------------------------------
    def unsaved_users(self) -> set[int]:
        users = {u for u, d in self._p_bal.items() if d} | {u for u, d in self._p_earn.items() if d}
        users |= {u for (_, u), c in self._p_spins.items() if any(c)}
        users |= set(self._p_daily) | set(self._p_stats) | {u for u, d in self._p_jp.items() if d}
        users |= {u for (_, u), d in self._p_sent.items() if d}
        users |= {u for (_, u, _n) in self._p_tickets}
        users |= {u for (_, u), n in self._p_jpw.items() if n}
        users |= set(self._p_shop)
        return users

    def _build_flush(self) -> tuple[list[list], list[tuple]]:
        cmds: list[list] = []
        meta: list[tuple] = []
        for uid, d in self._p_bal.items():
            if d:
                cmds.append(["HINCRBY", POINTS_KEY_BALANCE, str(uid), d])
                meta.append(("bal", uid, d))
        for uid, d in self._p_earn.items():
            if d:
                cmds.append(["ZINCRBY", POINTS_KEY_EARNED, d, str(uid)])
                meta.append(("earn", uid, d))
        expire_queued: set[str] = set()
        for (day, uid), (free, bonus) in self._p_spins.items():
            key = POINTS_KEY_DAY + day
            if free:
                cmds.append(["HINCRBY", key, f"{uid}:f", free])
                meta.append(("free", (day, uid), free))
            if bonus:
                cmds.append(["HINCRBY", key, f"{uid}:p", bonus])
                meta.append(("paid", (day, uid), bonus))
            if (free or bonus) and day not in self._expire_sent and day not in expire_queued:
                cmds.append(["EXPIRE", key, 172800])   # day hashes tidy themselves up after 2 days
                meta.append(("expire", day, 0))
                expire_queued.add(day)
        for (day, uid), sent in self._p_sent.items():
            key = POINTS_KEY_DAY + day
            if sent:
                cmds.append(["HINCRBY", key, f"{uid}:s", sent])
                meta.append(("sent", (day, uid), sent))
                if day not in self._expire_sent and day not in expire_queued:
                    cmds.append(["EXPIRE", key, 172800])
                    meta.append(("expire", day, 0))
                    expire_queued.add(day)
        for (day, uid), n in self._p_jpw.items():
            key = POINTS_KEY_DAY + day
            if n:
                cmds.append(["HINCRBY", key, f"{uid}:j", n])
                meta.append(("jpw", (day, uid), n))
                if day not in self._expire_sent and day not in expire_queued:
                    cmds.append(["EXPIRE", key, 172800])
                    meta.append(("expire", day, 0))
                    expire_queued.add(day)
        for uid, d in self._p_jp.items():
            if d:
                cmds.append(["HINCRBY", POINTS_KEY_JP, str(uid), d])
                meta.append(("jp", uid, d))
        for uid, value in self._p_daily.items():
            cmds.append(["HSET", POINTS_KEY_DAILY, str(uid), value])
            meta.append(("daily", uid, value))
        for uid in self._p_stats:
            blob = json.dumps(self.stats.get(uid, {}), separators=(",", ":"))
            cmds.append(["HSET", POINTS_KEY_STATS, str(uid), blob])
            meta.append(("stats", uid, blob))
        for uid, flag in self._p_frozen.items():
            cmds.append(["SADD" if flag else "SREM", POINTS_KEY_FROZEN, str(uid)])
            meta.append(("frozen", uid, flag))
        for uid in self._p_shop:
            blob = json.dumps(self.shop.get(uid, {}), separators=(",", ":"))
            cmds.append(["HSET", POINTS_KEY_SHOP, str(uid), blob])
            meta.append(("shop", uid, blob))
        if self._p_sched is not None:
            cmds.append(["SET", POINTS_KEY_SCHED, self._p_sched])
            meta.append(("sched", None, self._p_sched))
        tk_queued: set[str] = set()
        for (day, uid, n), val in self._p_tickets.items():
            key = LOTTERY_KEY_TICKETS + day
            cmds.append(["HSET", key, f"{uid}:{n}", val])
            meta.append(("ticket", (day, uid, n), val))
            if day not in self._expire_tk and day not in tk_queued:
                cmds.append(["EXPIRE", key, 259200])           # a day's tickets tidy themselves up after 3 days
                meta.append(("expire_t", day, 0))
                tk_queued.add(day)
        if self._p_lstate is not None:
            cmds.append(["SET", LOTTERY_KEY_STATE, self._p_lstate])
            meta.append(("lstate", None, self._p_lstate))
        return cmds, meta

    def _apply_saved(self, kind: str, ref, delta: int, result) -> None:
        """A change reached the database: stop counting it as unsaved, and
        re-base the live number on the database's own total (which also
        quietly picks up anything another Milo instance saved meanwhile)."""
        if kind in ("bal", "earn"):
            pending = self._p_bal if kind == "bal" else self._p_earn
            live = self.balance if kind == "bal" else self.earned
            left = pending.get(ref, 0) - delta
            if left:
                pending[ref] = left
            else:
                pending.pop(ref, None)
            live[ref] = _to_int(result) + left
        elif kind in ("free", "paid"):
            idx = 0 if kind == "free" else 1
            pend = self._p_spins.get(ref)
            left = 0
            if pend is not None:
                pend[idx] -= delta
                left = pend[idx]
                if not any(pend):
                    del self._p_spins[ref]
            self.spins.setdefault(ref, [0, 0])[idx] = _to_int(result) + left
        elif kind == "expire":
            self._expire_sent.add(ref)
        elif kind == "jp":
            left = self._p_jp.get(ref, 0) - delta
            if left:
                self._p_jp[ref] = left
            else:
                self._p_jp.pop(ref, None)
            self.jp[ref] = max(0, _to_int(result) + left)
        elif kind == "sent":
            left = self._p_sent.get(ref, 0) - delta
            if left:
                self._p_sent[ref] = left
            else:
                self._p_sent.pop(ref, None)
            self.sent[ref] = _to_int(result) + left
        elif kind == "jpw":
            left = self._p_jpw.get(ref, 0) - delta
            if left:
                self._p_jpw[ref] = left
            else:
                self._p_jpw.pop(ref, None)
            self.jp_wins[ref] = _to_int(result) + left
        elif kind == "daily":
            if self._p_daily.get(ref) == delta:          # unchanged since we sent it
                del self._p_daily[ref]
        elif kind == "stats":
            if json.dumps(self.stats.get(ref, {}), separators=(",", ":")) == delta:
                self._p_stats.discard(ref)
        elif kind == "frozen":
            if self._p_frozen.get(ref) == delta:
                del self._p_frozen[ref]
        elif kind == "shop":
            if json.dumps(self.shop.get(ref, {}), separators=(",", ":")) == delta:
                self._p_shop.discard(ref)
        elif kind == "sched":
            if self._p_sched == delta:
                self._p_sched = None
        elif kind == "ticket":
            if self._p_tickets.get(ref) == delta:
                del self._p_tickets[ref]
        elif kind == "expire_t":
            self._expire_tk.add(ref)
        elif kind == "lstate":
            if self._p_lstate == delta:
                self._p_lstate = None

    def _prune_days(self) -> None:
        yesterday = _day_info(datetime.now(_LIMIT_TZ) - timedelta(days=1))[0]
        for key in [k for k in self.spins if k[0] < yesterday and k not in self._p_spins]:
            del self.spins[key]
        for key in [k for k in self.sent if k[0] < yesterday and k not in self._p_sent]:
            del self.sent[key]
        for key in [k for k in self.jp_wins if k[0] < yesterday and k not in self._p_jpw]:
            del self.jp_wins[key]

    async def flush(self, reason: str) -> tuple[bool, int, str]:
        """Save every unsaved change. Returns (ok, people_saved, detail)."""
        store = self.store
        if store is None:
            return False, 0, "no Upstash database is configured"
        if not self.ready:
            return False, 0, f"{PTS_NAME} haven't been loaded from the database yet"
        async with self._io_lock:
            cmds, meta = self._build_flush()
            if not cmds:
                return True, 0, "nothing unsaved"
            saved: set[int] = set()
            failures = 0
            for start in range(0, len(cmds), _PIPELINE_CHUNK):
                chunk = cmds[start:start + _PIPELINE_CHUNK]
                try:
                    results = await store.pipeline(chunk)
                except StoreError as exc:
                    self.last_error = str(exc)
                    logger.warning("Points save (%s) failed, will retry: %s", reason, exc)
                    return False, len(saved), f"couldn't reach the database ({exc})"
                for item, (kind, ref, delta) in zip(results, meta[start:start + _PIPELINE_CHUNK]):
                    if not isinstance(item, dict) or "error" in item:
                        failures += 1
                        continue
                    self._apply_saved(kind, ref, delta, item.get("result"))
                    if kind in ("bal", "earn"):
                        saved.add(ref)
                    elif kind in ("free", "paid"):
                        saved.add(ref[1])
            self._prune_days()
            if failures:
                self.last_error = f"{failures} change(s) rejected"
                logger.warning("Points save (%s): %d change(s) were rejected", reason, failures)
                return False, len(saved), f"{failures} change(s) were rejected by the database"
            self.last_saved, self.last_error = time.time(), None
            logger.info("Points saved (%s): %d change(s) for %d people", reason, len(cmds), len(saved))
            return True, len(saved), "ok"

    async def hydrate(self) -> None:
        """Load everyone's points from Upstash. Safe to run again later: it
        MERGES (database total + our own unsaved changes) instead of replacing."""
        store = self.store
        if store is None:
            return
        async with self._io_lock:
            day = _day_info()[0]
            results = await store.pipeline([
                ["HGETALL", POINTS_KEY_BALANCE],
                ["ZRANGE", POINTS_KEY_EARNED, 0, -1, "WITHSCORES"],
                ["HGETALL", POINTS_KEY_DAY + day],
                ["HGETALL", POINTS_KEY_DAILY],
                ["HGETALL", POINTS_KEY_STATS],
                ["HGETALL", POINTS_KEY_JP],
                ["SMEMBERS", POINTS_KEY_FROZEN],
                ["GET", POINTS_KEY_SCHED],
                ["HGETALL", LOTTERY_KEY_TICKETS + day],
                ["HGETALL", LOTTERY_KEY_TICKETS + _day_before(day)],
                ["HGETALL", LOTTERY_KEY_TICKETS + _day_before(_day_before(day))],
                ["GET", LOTTERY_KEY_STATE],
                ["HGETALL", POINTS_KEY_SHOP],
            ])
            for item in results:
                if not isinstance(item, dict) or "error" in item:
                    raise StoreError(f"Upstash refused the load: {str(item)[:200]}")
            bal = {int(k): _to_int(v) for k, v in _flat_pairs(results[0].get("result")) if k.isdigit()}
            earn = {int(k): _to_int(v) for k, v in _flat_pairs(results[1].get("result")) if k.isdigit()}
            counts: dict[tuple[int, int], int] = {}
            sent_counts: dict[int, int] = {}
            jpw_counts: dict[int, int] = {}
            for fld, v in _flat_pairs(results[2].get("result")):
                uid_s, _, kind = fld.partition(":")
                if uid_s.isdigit() and kind in ("f", "p"):
                    counts[(int(uid_s), 0 if kind == "f" else 1)] = _to_int(v)
                elif uid_s.isdigit() and kind == "s":
                    sent_counts[int(uid_s)] = _to_int(v)
                elif uid_s.isdigit() and kind == "j":
                    jpw_counts[int(uid_s)] = _to_int(v)
            for uid in set(bal) | set(self._p_bal):
                self.balance[uid] = bal.get(uid, 0) + self._p_bal.get(uid, 0)
            for uid in set(earn) | set(self._p_earn):
                self.earned[uid] = earn.get(uid, 0) + self._p_earn.get(uid, 0)
            for uid in {u for (u, _) in counts} | {u for (d, u) in self._p_spins if d == day}:
                live = self.spins.setdefault((day, uid), [0, 0])
                pend = self._p_spins.get((day, uid), [0, 0])
                for idx in (0, 1):
                    live[idx] = counts.get((uid, idx), 0) + pend[idx]
            for uid in set(sent_counts) | {u for (d, u) in self._p_sent if d == day}:
                self.sent[(day, uid)] = sent_counts.get(uid, 0) + self._p_sent.get((day, uid), 0)
            for uid in set(jpw_counts) | {u for (d, u) in self._p_jpw if d == day}:
                self.jp_wins[(day, uid)] = jpw_counts.get(uid, 0) + self._p_jpw.get((day, uid), 0)
            for uid, value in _flat_pairs(results[3].get("result")):
                if uid.isdigit() and int(uid) not in self._p_daily:
                    self.daily[int(uid)] = str(value)
            for uid, blob in _flat_pairs(results[4].get("result")):
                if uid.isdigit() and int(uid) not in self._p_stats:
                    try:
                        parsed = json.loads(blob)
                        if isinstance(parsed, dict):
                            self.stats[int(uid)] = parsed
                    except (TypeError, ValueError):
                        logger.warning("Skipping unreadable Gamblin stats for %s", uid)
            jp_db = {int(k): _to_int(v) for k, v in _flat_pairs(results[5].get("result")) if k.isdigit()}
            for uid in set(jp_db) | set(self._p_jp):
                self.jp[uid] = max(0, jp_db.get(uid, 0) + self._p_jp.get(uid, 0))
            members = results[6].get("result") or []
            frozen = {int(x) for x in members if str(x).isdigit()}
            for uid, flag in self._p_frozen.items():
                (frozen.add if flag else frozen.discard)(uid)
            self.frozen = frozen
            raw_sched = results[7].get("result")
            if self._p_sched is None and raw_sched:
                try:
                    parsed = json.loads(raw_sched)
                    self.schedule = [x for x in parsed if isinstance(x, dict) and str(x.get("ts", "")).lstrip("-").isdigit()][:10]
                except (TypeError, ValueError):
                    logger.warning("Ignoring unreadable Gamblin schedule in the database")
            for n, tday in enumerate((day, _day_before(day), _day_before(_day_before(day)))):
                merged: dict[tuple[int, int], tuple] = {}
                for fld, v in _flat_pairs(results[8 + n].get("result")):
                    u, _, i = fld.partition(":")
                    if u.isdigit() and i.isdigit():
                        merged[(int(u), int(i))] = tuple(int(x) for x in str(v).split(",") if x.isdigit())
                for (pday, u, i), val in self._p_tickets.items():          # unsaved tickets win
                    if pday == tday:
                        merged[(u, i)] = tuple(int(x) for x in val.split(","))
                by_user: dict[int, dict[int, tuple]] = {}
                for (u, i), picks in merged.items():
                    if len(picks) == LOTTERY_PICKS:
                        by_user.setdefault(u, {})[i] = picks
                if by_user or tday in self.tickets:
                    self.tickets[tday] = by_user
            for uid, blob in _flat_pairs(results[12].get("result")):
                if uid.isdigit() and int(uid) not in self._p_shop:
                    try:
                        cleaned = _clean_shop_state(json.loads(blob))
                    except (TypeError, ValueError):
                        cleaned = None
                    if cleaned:
                        self.shop[int(uid)] = cleaned
            raw_state = results[11].get("result")
            if self._p_lstate is None and raw_state:
                try:
                    parsed = json.loads(raw_state)
                    if isinstance(parsed, dict) and isinstance(parsed.get("carry"), int):
                        self.lottery = {"carry": parsed["carry"], "drawn": str(parsed.get("drawn", "")),
                                        "last": parsed.get("last") if isinstance(parsed.get("last"), dict) else None}
                except (TypeError, ValueError):
                    logger.warning("Ignoring unreadable lottery state in the database")
            self.ready = True
            self.last_error = None

    async def run(self) -> None:
        """Background job: load (retrying), one re-sync after the deploy overlap,
        then save changes every POINTS_SAVE_SECONDS."""
        while not self.ready:
            try:
                await self.hydrate()
                logger.info(
                    "Points loaded from Upstash: %d people, %d on the leaderboard.",
                    len(self.earned), len(self.qualified()),
                )
            except StoreError as exc:
                self.last_error = str(exc)
                logger.warning("Points not loaded yet (%s) -- retrying in 60s", exc)
                await asyncio.sleep(60)
        await asyncio.sleep(POINTS_RESYNC_DELAY)
        try:
            await self.hydrate()
        except StoreError as exc:
            logger.warning("Points re-sync skipped (%s)", exc)
        while True:
            await asyncio.sleep(POINTS_SAVE_SECONDS)
            if GAMBLIN is not None:
                continue  # Gamblin is live: one batched save happens when it ends
            ok, _, detail = await self.flush("autosave")
            if self.store is not None:
                if ok:
                    if self._save_fails >= 2:
                        await log_event("✅ Saving to Upstash works again.")
                    self._save_fails = 0
                else:
                    self._save_fails += 1
                    if self._save_fails == 2:
                        await log_event(
                            f"⚠️ **Saving {PTS_NAME} to Upstash has failed twice in a row** ({detail}). "
                            f"{PTS_CAP} are safe in memory — don't redeploy until this clears; try `?updateDB`."
                        )

    def start(self) -> None:
        if self.store is None:
            self.ready = True
            logger.warning(
                "Points are kept in MEMORY ONLY (no Upstash configured) -- every restart "
                "or redeploy wipes everyone's points. Set UPSTASH_REDIS_REST_URL and "
                "UPSTASH_REDIS_REST_TOKEN to save them."
            )
            return
        self._task = asyncio.create_task(self.run())
        logger.info(
            "Points will be loaded from Upstash and saved every %d min, on shutdown, and on ?updateDB.",
            POINTS_SAVE_SECONDS // 60,
        )

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


POINTS = PointsManager()


# ---- the slot machine ------------------------------------------------------------
def _roll_slots(since_win: int, since_jp: int) -> tuple[list[str], int, str]:
    """One spin. Returns (reels, payout, pity) where pity is "", "soft" or "jackpot".
    since_jp  = spins since this person's last 777 (saved, survives restarts)
    since_win = spins since their last win of any kind (memory only)."""
    if SLOTS_JACKPOT_PITY_AT and since_jp + 1 >= SLOTS_JACKPOT_PITY_AT:
        return [SLOT_JACKPOT] * 3, SLOT_PAYOUTS[SLOT_JACKPOT], "jackpot"     # the 200th spin IS a 777
    if SLOTS_PITY_AT and since_win + 1 >= SLOTS_PITY_AT:
        sym = random.choice(SLOT_PITY_SYMBOLS)
        return [sym] * 3, SLOT_PAYOUTS[sym], "soft"
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    won = reels[0] == reels[1] == reels[2]
    return reels, (SLOT_PAYOUTS[reels[0]] if won else 0), ""


def _spin_precheck(uid: int) -> tuple[str, dict]:
    """Instant memory check for a spin. Returns (status, info):
    "frozen" / "cooldown" / "broke" / "exhausted" -> info["msg"] is the private reply;
    "ok" -> info has paid, cost (points charged by THIS spin: the pack price on a
    pack's first spin, 0 for the rest), day."""
    blocked = frozen_block(uid)
    if blocked:
        return "frozen", {"msg": blocked}
    now = time.time()
    last = POINTS.last_spin.get(uid)
    if last is not None and now - last < SLOTS_COOLDOWN_SECONDS:
        wait = max(1, round(SLOTS_COOLDOWN_SECONDS - (now - last)))
        return "cooldown", {"msg": f"🎰 The reels are still spinning! Try again in {wait}s."}

    day, secs_to_reset = _day_info()
    free_used, bonus_used = POINTS.used(uid, day)
    if free_used < SLOTS_FREE_SPINS:
        return "ok", {"paid": False, "cost": 0, "day": day}
    if bonus_used < SLOTS_PAID_SPINS:
        if bonus_used % SLOTS_PACK_SPINS:                    # inside a pack you already paid for
            return "ok", {"paid": True, "cost": 0, "day": day}
        balance = POINTS.points(uid)
        if balance < SLOTS_PACK_COST:
            return "broke", {"msg": (
                f"🎰 You've used all {SLOTS_FREE_SPINS} free spins today. A bonus pack ({SLOTS_PACK_SPINS} spins) "
                f"costs {pfmt(SLOTS_PACK_COST)} and you have {pamt(balance)}. Fresh free spins in "
                f"{_format_wait(secs_to_reset)}!"
            )}
        return "ok", {"paid": True, "cost": SLOTS_PACK_COST, "day": day}
    return "exhausted", {"msg": (
        f"🎰 You've used all {SLOTS_FREE_SPINS} free spins and {SLOTS_PAID_SPINS} bonus spins "
        f"for today. They come back in {_format_wait(secs_to_reset)}!"
    )}


def _perform_spin(uid: int, *, paid: bool, cost: int, day: str) -> discord.Embed:
    """The actual spin. Instant memory work -- no awaits -- so two quick spins
    can never both sneak under a limit."""
    reels, payout, pity = _spin_core(uid, paid=paid, cost=cost, day=day)

    symbol = reels[0]
    if payout and symbol == SLOT_JACKPOT:
        headline, color = f"💥 **JACKPOT!!!** Triple 7️⃣ — **+{pfmt(payout)}!**", discord.Color.gold()
    elif payout and symbol == "⭐":
        headline, color = f"🌟 **Triple stars!** **+{pfmt(payout)}!**", discord.Color.gold()
    elif payout:
        headline, color = f"🎉 **Triple {symbol}!** **+{pfmt(payout)}!**", discord.Color.green()
    elif len({*reels}) == 2:
        headline, color = "✨ Almost! Two matched.", discord.Color.dark_grey()
    else:
        headline, color = "No match — spin again!", discord.Color.dark_grey()
    if pity == "jackpot":
        headline += f"\n🎯 *Jackpot guarantee! Milo promised a 777 within {SLOTS_JACKPOT_PITY_AT} spins.* {MILO_EMOJI}"
    elif pity == "soft":
        headline += f"\n🍀 *Pity win! Milo felt bad after {SLOTS_PITY_AT - 1} misses.* {MILO_EMOJI}"

    free_used, bonus_used = POINTS.used(uid, day)
    pack_left = (-bonus_used) % SLOTS_PACK_SPINS if paid else 0
    if paid and cost:
        spin_line = f"💰 Bonus pack bought (−{pfmt(cost)}) — {SLOTS_PACK_SPINS} spins"
    elif paid:
        spin_line = "🎁 Bonus pack spin"
    else:
        spin_line = "🆓 Free spin"
    footer = (
        f"Balance: {pamt(POINTS.points(uid))} · Free spins left: {SLOTS_FREE_SPINS - free_used}/{SLOTS_FREE_SPINS} "
        f"· Bonus spins left: {SLOTS_PAID_SPINS - bonus_used}/{SLOTS_PAID_SPINS}"
    )
    if pack_left:
        footer += f" ({pack_left} left in your pack)"
    to_jackpot = SLOTS_JACKPOT_PITY_AT - POINTS.jp_count(uid)
    if SLOTS_JACKPOT_PITY_AT and to_jackpot <= 25:
        footer += f" · 🎯 777 guaranteed in {to_jackpot} spin{'s' if to_jackpot != 1 else ''}"
    else:
        until_pity = SLOTS_PITY_AT - POINTS.pity.get(uid, 0)
        if SLOTS_PITY_AT and not payout and until_pity <= 5:
            footer += f" · 🍀 Win guaranteed within {until_pity} spin{'s' if until_pity != 1 else ''}"
    return _render_spin(style_of(uid, "slots"), reels, headline, spin_line, footer, color, bool(payout))


def _announce_jackpot(uid: int, payout: int, pity: str) -> None:
    """A 777 was just hit: tell RESET_ANNOUNCE_CHANNEL_ID right away (no @everyone). Fire-and-forget, so it
    never slows down or breaks a spin; if there's no channel set or no event loop running, it does nothing."""
    if not RESET_ANNOUNCE_CHANNEL_ID:
        return
    try:
        task = asyncio.get_running_loop().create_task(_post_jackpot_announcement(uid, payout, pity))
    except RuntimeError:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _post_jackpot_announcement(uid: int, payout: int, pity: str) -> None:
    note = f" — the {SLOTS_JACKPOT_PITY_AT}-spin guarantee kicked in!" if pity == "jackpot" else ""
    text = f"💥 **JACKPOT!** <@{uid}> just hit **7️⃣7️⃣7️⃣** in `/slots` and won **{pfmt(payout)}**{note} 🎉"

    async def send() -> None:
        channel = bot.get_channel(RESET_ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(RESET_ANNOUNCE_CHANNEL_ID)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    await _retry_discord(send, "the 777 announcement")


def _spin_core(uid: int, *, paid: bool, cost: int, day: str) -> tuple[list[str], int, str]:
    """One spin's bookkeeping, no message. Instant memory work (no awaits)."""
    POINTS.last_spin[uid] = time.time()
    since_win = POINTS.pity.get(uid, 0)
    reels, payout, pity = _roll_slots(since_win, POINTS.jp_count(uid))
    POINTS.pity[uid] = 0 if payout else since_win + 1
    POINTS.record_spin(uid, paid=paid, cost=cost, payout=payout, day=day, hit_777=reels == [SLOT_JACKPOT] * 3)
    if reels == [SLOT_JACKPOT] * 3:
        _announce_jackpot(uid, payout, pity)
    return reels, payout, pity


_BATCH_MAX = 60                      # never more than this many spins in one go
_SYMBOL_WORD = {"7️⃣": "777", "⭐": "stars", "🍇": "grapes", "🍉": "melons", "🍋": "lemons", "🍒": "cherries"}


def _next_spin_kind(free_used: int, bonus_used: int) -> str | None:
    """What the next spin would be WITHOUT paying for anything: 'free', 'pack' (already paid for), or None."""
    if free_used < SLOTS_FREE_SPINS:
        return "free"
    if bonus_used < SLOTS_PAID_SPINS and bonus_used % SLOTS_PACK_SPINS:
        return "pack"
    return None


def _batch_count(uid: int, day: str) -> int:
    """How many spins could be taken in one go right now (free spins + the rest of a pack you already bought)."""
    free_used, bonus_used = POINTS.used(uid, day)
    n = 0
    while n < _BATCH_MAX and (kind := _next_spin_kind(free_used, bonus_used)):
        free_used, bonus_used = (free_used + 1, bonus_used) if kind == "free" else (free_used, bonus_used + 1)
        n += 1
    return n


def _batch_spin(uid: int, day: str) -> list[dict]:
    """Take every spin that's already paid for, back to back. Instant memory work, so it can't be
    interleaved with anything else. Never buys a new pack by itself."""
    spins: list[dict] = []
    while len(spins) < _BATCH_MAX:
        kind = _next_spin_kind(*POINTS.used(uid, day))
        if kind is None:
            break
        reels, payout, pity = _spin_core(uid, paid=kind == "pack", cost=0, day=day)
        spins.append({"n": len(spins) + 1, "reels": reels, "payout": payout, "pity": pity, "paid": kind == "pack"})
    return spins


def _batch_embed(uid: int, day: str, spins: list[dict]) -> discord.Embed:
    total = sum(sp["payout"] for sp in spins)
    wins = [sp for sp in spins if sp["payout"]]
    best = max(spins, key=lambda sp: sp["payout"])
    result = (f"**{len(wins)}** win{'s' if len(wins) != 1 else ''} · total **{pfmt(total)}**"
              + (f" · best: spin {best['n']} (+{best['payout']})" if wins else " · no wins this time — better luck next spins!"))
    free_used, bonus_used = POINTS.used(uid, day)
    footer = (f"Balance: {pamt(POINTS.points(uid))} · Free spins left: {SLOTS_FREE_SPINS - free_used}/{SLOTS_FREE_SPINS} "
              f"· Bonus spins left: {SLOTS_PAID_SPINS - bonus_used}/{SLOTS_PAID_SPINS}")
    to_jackpot = SLOTS_JACKPOT_PITY_AT - POINTS.jp_count(uid)
    if SLOTS_JACKPOT_PITY_AT and to_jackpot <= 25:
        footer += f" · 🎯 777 guaranteed in {to_jackpot} spin{'s' if to_jackpot != 1 else ''}"
    return _render_batch(style_of(uid, "quick"), spins, result, footer)


def _batch_win_messages(mention: str, spins: list[dict]) -> list[str]:
    """The separate 'X won!' messages: every 777 gets its own, the first few other wins too,
    and any extra wins are rolled into one message so a lucky batch can't flood the channel."""
    msgs: list[str] = []
    others: list[dict] = []
    for sp in spins:
        if not sp["payout"]:
            continue
        if sp["reels"] == [SLOT_JACKPOT] * 3:
            note = f" (the {SLOTS_JACKPOT_PITY_AT}-spin guarantee!)" if sp["pity"] == "jackpot" else ""
            msgs.append(f"💥 **JACKPOT!!!** {mention} hit **7️⃣7️⃣7️⃣** on spin #{sp['n']} and won **{pfmt(sp['payout'])}**{note} 🎉")
        else:
            others.append(sp)
    singles, rest = others[:4], others[4:]
    for sp in singles:
        msgs.append(f"🎉 {mention} won **{pfmt(sp['payout'])}** on spin #{sp['n']}: {' '.join(sp['reels'])} "
                    f"(triple {_SYMBOL_WORD.get(sp['reels'][0], sp['reels'][0])})"
                    + (f"\n🍀 *Pity win! Milo felt bad after {SLOTS_PITY_AT - 1} misses.* {MILO_EMOJI}" if sp["pity"] == "soft" else ""))
    if rest:
        msgs.append(f"🎉 {mention} won on {len(rest)} more spins too: "
                    + ", ".join(f"#{sp['n']} +{sp['payout']}" + (" 🍀 pity" if sp["pity"] == "soft" else "") for sp in rest) + f" (**{pfmt(sum(sp['payout'] for sp in rest))}** more)")
    return msgs


async def _send_batch(interaction: discord.Interaction, uid: int, day: str, spins: list[dict]) -> None:
    """Post the one long result message, then the separate win messages. The spins are already
    applied (in memory) before any of this, so a Discord hiccup can never lose points."""
    try:
        await interaction.followup.send(embed=_batch_embed(uid, day, spins))
        for text in _batch_win_messages(interaction.user.mention, spins):
            await asyncio.sleep(0.7)                                  # gentle on Discord's rate limits
            await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        if _is_discord_block(exc):
            _note_discord_block("a batch spin")
        else:
            logger.warning("Couldn't post a batch spin result (the points were still applied)", exc_info=True)


class SlotsChoiceView(discord.ui.View):
    """'Spin once or spin them all?' -- shown when you have 2+ spins ready. Memory only (60s)."""

    def __init__(self, owner_id: int, n: int) -> None:
        super().__init__(timeout=60)
        self.owner_id, self.n, self.used = owner_id, n, False
        self.all_button.label = f"Spin all {n}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your spin 🐾", ephemeral=True)
            return False
        return True

    async def _check(self, interaction: discord.Interaction):
        """Shared re-check at click time. Returns the precheck info if a free/pack spin can go ahead."""
        if self.used:
            await interaction.response.send_message("Already handled!", ephemeral=True)
            return None
        blocked = gamblin_block_text("`/slots`")
        if blocked or not POINTS.ready:
            self.used = True
            self.stop()
            await interaction.response.edit_message(content=blocked or _POINTS_UNAVAILABLE, view=None)
            return None
        status, info = _spin_precheck(self.owner_id)
        if status != "ok" or info["cost"]:
            self.used = True
            self.stop()
            await interaction.response.edit_message(
                content=info["msg"] if status != "ok" else "That was your last paid-for spin — use `/slots` again to buy a new pack.",
                view=None)
            return None
        self.used = True
        self.stop()
        return info

    @discord.ui.button(label="Spin once", emoji="🎰", style=discord.ButtonStyle.primary)
    async def once_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = await self._check(interaction)
        if info is None:
            return
        embed = _perform_spin(self.owner_id, paid=info["paid"], cost=0, day=info["day"])
        await interaction.response.edit_message(content="🎰 Spun!", view=None)
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException as exc:
            if _is_discord_block(exc):
                _note_discord_block("a spin result")

    @discord.ui.button(label="Spin all", emoji="⚡", style=discord.ButtonStyle.success)
    async def all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = await self._check(interaction)
        if info is None:
            return
        spins = _batch_spin(self.owner_id, info["day"])
        await interaction.response.edit_message(content=f"⚡ Spinning {len(spins)} times…", view=None)
        await _send_batch(interaction, self.owner_id, info["day"], spins)


class BonusSpinConfirmView(discord.ui.View):
    """Private 'are you sure?' shown before a bonus PACK costs points.
    Lives in memory only (30s) -- if it expires, nothing is charged."""

    def __init__(self, owner_id: int, balance: int, bonus_left: int) -> None:
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.used = False
        self.summary = (
            f"💰 **Bonus pack** — **{SLOTS_PACK_SPINS} spins for {pfmt(SLOTS_PACK_COST)}**.\n"
            f"You have {pcompact(balance, True)} · **{bonus_left}/{SLOTS_PAID_SPINS}** bonus spins left today.\n"
            f"Buy the pack and take your first spin?"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your spin 🐾", ephemeral=True)
            return False
        return True

    @discord.ui.button(label=f"Buy pack ({pamt(SLOTS_PACK_COST)})", emoji="🎰", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            await interaction.response.send_message("Already handled!", ephemeral=True)
            return
        self.used = True
        self.stop()
        blocked = gamblin_block_text("`/slots`")
        if blocked:
            await interaction.response.edit_message(content=blocked, view=None)
            return
        if not POINTS.ready:
            await interaction.response.edit_message(content=_POINTS_UNAVAILABLE, view=None)
            return
        # Re-check everything: things may have changed while the prompt was open.
        status, info = _spin_precheck(self.owner_id)
        if status != "ok":
            await interaction.response.edit_message(content=info["msg"], view=None)
            return
        embed = _perform_spin(self.owner_id, paid=info["paid"], cost=info["cost"], day=info["day"])
        await interaction.response.edit_message(content="🎰 Spun!", view=None)
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Couldn't post the confirmed spin result (points were still applied).")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(
            content=f"Cancelled — no {PTS_NAME} spent. 🐾", view=None
        )

    async def on_timeout(self) -> None:
        self.used = True  # nothing charged; the buttons just go dead


@bot.tree.command(name="slots", description=f"Spin the slot machine and win {PTS_NAME}.")
async def slots_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    blocked = gamblin_block_text("`/slots`")
    if blocked:
        await interaction.response.send_message(blocked, ephemeral=True)
        return

    if not _channel_allowed(interaction, SLOTS_CHANNEL_IDS):
        channels = ", ".join(f"<#{cid}>" for cid in sorted(SLOTS_CHANNEL_IDS))
        await interaction.response.send_message(
            f"🎰 You can only use `/slots` in {channels} -- hop over there and spin!",
            ephemeral=True,
        )
        return

    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return

    uid = interaction.user.id
    status, info = _spin_precheck(uid)
    if status != "ok":
        await interaction.response.send_message(info["msg"], ephemeral=True)
        return

    if info["cost"]:
        # Buying a pack costs points -> ask first. Nothing is charged until they confirm.
        bonus_left = SLOTS_PAID_SPINS - POINTS.used(uid, info["day"])[1]
        view = BonusSpinConfirmView(uid, POINTS.points(uid), bonus_left)
        await interaction.response.send_message(view.summary, view=view, ephemeral=True)
        return

    ready = _batch_count(uid, info["day"])
    if ready >= 2:
        # Several spins are ready: offer to take them all in one go.
        await interaction.response.send_message(
            f"🎰 You have **{ready} spins** ready. Spin once, or all {ready} at once?",
            view=SlotsChoiceView(uid, ready), ephemeral=True,
        )
        return

    embed = _perform_spin(uid, paid=info["paid"], cost=0, day=info["day"])
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="points", description=f"Check your {PTS_NAME}, rank, and spins left.")
@app_commands.describe(user=f"Whose {PTS_NAME} to check (defaults to you)")
async def points_slash(interaction: discord.Interaction, user: discord.Member | None = None):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return
    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return

    target = user or interaction.user
    uid = target.id
    earned = POINTS.total_earned(uid)
    rank = POINTS.rank_of(uid)
    if rank is not None:
        board_text = f"#{rank} on the leaderboard"
    else:
        board_text = (
            f"Not on it yet — **{earned:,}/{LEADERBOARD_MIN_POINTS:,}** {PTS_NAME} earned. "
            f"Earn {LEADERBOARD_MIN_POINTS - earned:,} more to get on the leaderboard!"
        )

    embed = discord.Embed(
        title=f"{MILO_EMOJI} {target.display_name}'s {PTS_NAME}", color=discord.Color.blurple()
    )
    embed.add_field(name="💰 Balance", value=pcompact(POINTS.points(uid)))
    embed.add_field(name="🏆 Total earned", value=f"{earned:,}")
    embed.add_field(name="📊 Leaderboard", value=board_text, inline=False)
    if target.id == interaction.user.id:
        day, secs = _day_info()
        free_used, bonus_used = POINTS.used(uid, day)
        embed.add_field(
            name="🎰 Spins today",
            value=(
                f"Free: **{SLOTS_FREE_SPINS - free_used}/{SLOTS_FREE_SPINS}** left · "
                f"Bonus ({SLOTS_PACK_SPINS}-spin pack for {pfmt(SLOTS_PACK_COST)}): **{SLOTS_PAID_SPINS - bonus_used}/{SLOTS_PAID_SPINS}** left\n"
                f"Resets in {_format_wait(secs)}."
            ),
            inline=False,
        )
        embed.add_field(
            name="🎯 777 guarantee",
            value=f"**{POINTS.jp_count(uid)}/{SLOTS_JACKPOT_PITY_AT}** spins since your last 777 — the {SLOTS_JACKPOT_PITY_AT}th is always a 777.",
            inline=False,
        )
    if POINTS.store is None:
        embed.set_footer(text=f"⚠️ {PTS_CAP} aren't being saved yet (no database is set up).")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="See the top earners and the richest players.")
async def leaderboard_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return
    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return

    board = POINTS.qualified()
    embed = discord.Embed(title="🏆 Milo Leaderboard", color=discord.Color.gold())
    medals = ["🥇", "🥈", "🥉"]
    if board:
        lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> — {pcompact(earned, True)}"
            for i, (uid, earned) in enumerate(board[:10])
        ]
        embed.description = "**📈 Top earners (all-time)**\n" + "\n".join(lines)
        footer = (
            f"All-time ranked by total earned · {len(board)} on the board · "
            f"earn {LEADERBOARD_MIN_POINTS:,} to join"
        )
    else:
        near = POINTS.closest()
        text = (
            f"**📈 Top earners (all-time)**\nNobody's on this board yet! **Earn your first "
            f"{pfmt(LEADERBOARD_MIN_POINTS)} to reach it.** Win them in `/slots`! 🎰"
        )
        if near:
            text += "\n\n**Closest so far:**\n" + "\n".join(
                f"<@{uid}> — {earned:,}/{LEADERBOARD_MIN_POINTS:,}" for uid, earned in near
            )
        embed.description = text
        footer = "All-time ranked by total earned"

    rich = POINTS.richest(5)
    if rich:
        rich_lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> — {pcompact(bal, True)}"
            for i, (uid, bal) in enumerate(rich)
        ]
        rich_text = "\n".join(rich_lines)
    else:
        rich_text = f"Nobody's holding any {PTS_NAME} right now."
    embed.add_field(name="💰 Richest right now (current balance)", value=rich_text, inline=False)
    embed.set_footer(text=footer + " · Balance board has no minimum")
    await interaction.response.send_message(embed=embed)


# ---- hidden Manager-only command: ?updateDB --------------------------------------
async def _handle_updatedb_command(message: discord.Message) -> None:
    if not MANAGER_ROLE_ID:
        return
    if message.guild is None or not isinstance(message.author, discord.Member):
        return
    if MANAGER_ROLE_ID not in {role.id for role in message.author.roles}:
        return  # not authorised -- stay silent, like ?send / ?rolemenu

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    if GAMBLIN is not None:
        text = "🎰 Gamblin is running — it saves everything automatically when it ends. Use `?gamblin stop` first if you need to redeploy now."
    elif POINTS.store is None:
        text = (
            f"⚠️ No Upstash database is configured, so {PTS_NAME} only exist in memory and "
            "**WILL be lost on redeploy.** Set `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` first."
        )
    elif not POINTS.ready:
        text = f"❌ {PTS_CAP} haven't been loaded from the database yet, so there's nothing safe to save. Try again in a minute."
    else:
        try:
            ok, people, detail = await asyncio.wait_for(POINTS.flush("manual"), timeout=25)
        except asyncio.TimeoutError:
            ok, people, detail = False, 0, "the database took too long to answer"
        if ok and detail == "nothing unsaved":
            text = "✅ Everything was already saved — nothing new since the last save. **Safe to redeploy.**"
        elif ok:
            text = f"✅ Saved {people} {'person' if people == 1 else 'people'}'s {PTS_NAME} to Upstash. **Safe to redeploy.**"
        else:
            text = f"❌ **NOT saved:** {detail}. **Don't redeploy yet** — try `?updateDB` again in a minute."
    await message.channel.send(text, delete_after=120)
    await log_event(f"💾 `?updateDB` by <@{message.author.id}>: {text}")


def _channel_allowed(interaction: discord.Interaction, allowed_ids: set[int]) -> bool:
    """True if a command may be used in this channel. Always True when the
    allow-list is empty. A thread counts as allowed when the channel it lives
    in is allowed."""
    if not allowed_ids:
        return True
    if interaction.channel_id in allowed_ids:
        return True
    parent_id = getattr(interaction.channel, "parent_id", None)
    return parent_id is not None and parent_id in allowed_ids


def _ask_channel_allowed(interaction: discord.Interaction) -> bool:
    return _channel_allowed(interaction, ASK_CHANNEL_IDS)


def _reasoning_params(model: str, effort: str) -> dict:
    # Only OpenAI's gpt-oss models accept reasoning_effort; sending it to other
    # models can make Groq reject the whole request with a 400.
    return {"reasoning_effort": effort} if "gpt-oss" in model.lower() else {}


_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


async def _call_groq(
    model: str,
    system_prompt: str,
    question: str,
    *,
    max_tokens: int,
    effort: str,
    timeout_s: int = 10,
) -> tuple[str | None, str | None]:
    """Ask one Groq model. Returns (answer, error_reason); error_reason is one
    of "rate_limited_minute", "rate_limited_day", "model_unavailable",
    "error", or None on success."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        # Reasoning models spend hidden "thinking" tokens from this same budget;
        # if it's too small Groq returns a 200 with an EMPTY answer.
        "max_completion_tokens": max_tokens,
        "temperature": 0.8,
        **_reasoning_params(model, effort),
    }
    try:
        async with bot.session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                # Log Groq's own explanation -- a bare status code hides the
                # real reason (retired model, exhausted daily limit, ...).
                try:
                    body = (await resp.text())[:300]
                except Exception:
                    body = "<unreadable>"
                logger.warning("Groq (%s) returned status %s: %s", model, resp.status, body)
                if resp.status == 429:
                    if "per day" in body.lower():
                        return None, "rate_limited_day"
                    return None, "rate_limited_minute"
                if resp.status == 404:
                    return None, "model_unavailable"
                return None, "error"
            data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for Groq (%s)", model)
        return None, "error"
    except Exception:
        logger.exception("Failed to reach Groq")
        return None, "error"

    try:
        choice = data["choices"][0]
        answer = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning("Unexpected Groq response shape: %r", data)
        return None, "error"

    # Some models (e.g. Qwen) put their reasoning inline in <think>...</think>.
    answer = _THINK_TAG_RE.sub("", answer)
    if "<think>" in answer.lower():  # unclosed -> the budget ran out mid-thought
        answer = answer[: answer.lower().index("<think>")]
    answer = answer.strip()

    if not answer:
        logger.warning(
            "Groq (%s) returned no usable content (finish_reason=%s, usage=%s)",
            model, choice.get("finish_reason"), data.get("usage"),
        )
        return None, "error"

    return answer, None


async def _ask_groq(question: str) -> tuple[str | None, str | None]:
    """The quick /ask answer, from the fast model (GROQ_MODEL)."""
    return await _call_groq(
        GROQ_MODEL, MILO_SYSTEM_PROMPT, question, max_tokens=500, effort="low"
    )


@bot.tree.command(name="ask", description="Ask Milo a quick question (AI-powered).")
@app_commands.describe(question="What do you want to ask?")
async def ask_slash(interaction: discord.Interaction, question: app_commands.Range[str, 1, 300]):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This bot is not configured for this server.", ephemeral=True
        )
        return

    blocked = gamblin_block_text("`/ask`")
    if blocked:
        await interaction.response.send_message(blocked, ephemeral=True)
        return

    # Channel restriction: if ASK_CHANNEL_IDS is set, /ask only works there.
    if not _ask_channel_allowed(interaction):
        channel_list = ", ".join(f"<#{cid}>" for cid in sorted(ASK_CHANNEL_IDS))
        await interaction.response.send_message(
            f"{MILO_EMOJI} Mrrp! You can only use `/ask` in {channel_list} -- "
            "hop over there and ask me again!",
            ephemeral=True,
        )
        return

    if not GROQ_API_KEY:
        await interaction.response.send_message(
            f"{MILO_EMOJI} `/ask` needs a free Groq API key to work -- ask whoever "
            "runs this bot to grab one at console.groq.com and set `GROQ_API_KEY`.",
            ephemeral=True,
        )
        return

    # Per-person cooldown (in memory). Checked only after the cheap rejections
    # above, and only *started* once we actually call the AI, so a person who
    # gets turned away isn't penalised.
    now = time.time()
    last_used = _ask_cooldowns.get(interaction.user.id)
    if last_used is not None and (now - last_used) < ASK_COOLDOWN_SECONDS:
        remaining = max(1, round(ASK_COOLDOWN_SECONDS - (now - last_used)))
        await interaction.response.send_message(
            f"{MILO_EMOJI} Easy there! Let me catch my breath -- ask again in {remaining}s.",
            ephemeral=True,
        )
        return
    _ask_cooldowns[interaction.user.id] = now

    await interaction.response.defer()
    answer, error = await _ask_groq(question)
    if error:
        # Don't make someone wait out a cooldown for a question that never
        # got answered (API hiccup, rate limit, etc.).
        _ask_cooldowns.pop(interaction.user.id, None)

    if error == "rate_limited_minute":
        await interaction.followup.send(
            f"{MILO_EMOJI} Mrrp! Too many people are asking me things at once -- "
            "give me a minute and try again."
        )
        return
    if error == "rate_limited_day":
        await interaction.followup.send(
            f"{MILO_EMOJI} *yawns* I've used up all my brainpower for today. "
            "Ask me again a bit later!"
        )
        return
    if error or not answer:
        await interaction.followup.send(
            f"{MILO_EMOJI} Mrrp... my brain's napping. Try again in a bit!"
        )
        return

    # Safety net -- the prompt and token budget already keep replies short,
    # but never trust an external API's output length blindly (Discord embed
    # fields cap at 1024 characters).
    if len(answer) > 1000:
        answer = answer[:1000].rstrip() + "…"

    embed = discord.Embed(color=discord.Color.blurple())
    embed.add_field(name="❓ You asked", value=question, inline=False)
    embed.add_field(name=f"{MILO_EMOJI} Milo says", value=answer, inline=False)
    footer = "AI-generated -- Milo can get things wrong, so double-check anything important."
    send_kwargs: dict = {}
    view = _build_think_view(
        interaction.user.id, int(time.time()), [t.key for t in THINK_TIERS]
    )
    if view is not None:
        footer += " Want a deeper answer? Use a button below."
        send_kwargs["view"] = view
    embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed, **send_kwargs)


def _build_think_view(asker_id: int, created_ts: int, tier_keys: list[str]) -> discord.ui.View | None:
    """The buttons under an /ask answer. Everything the handler needs later
    (which tier, who asked, when) rides inside each button's custom_id, so the
    buttons keep working after a restart with no database."""
    view = discord.ui.View(timeout=None)
    for key in tier_keys:
        tier = _TIER_BY_KEY.get(key)
        if tier is None:
            continue
        view.add_item(
            discord.ui.Button(
                label=tier.label,
                emoji=tier.emoji,
                style=tier.style,
                custom_id=f"{THINK_BUTTON_PREFIX}{tier.key}:{asker_id}:{created_ts}",
            )
        )
    return view if view.children else None


def _think_error_text(tier: AskTier, code: str, retry: int) -> str:
    e, name = MILO_EMOJI, f"{tier.emoji} **{tier.label}**"
    if code == "user_limit":
        return (f"{e} You've used all {tier.user_daily} of your {name} tries for today. "
                f"They come back in about {_format_wait(retry)}.")
    if code == "global_limit":
        return (f"{e} The whole server has used up today's {name} budget. "
                f"It refills in about {_format_wait(retry)}. (Your own tries weren't used.)")
    if code == "cooldown":
        return f"{e} Easy there! Wait {_format_wait(retry)} before another {name}."
    if code == "used":
        return f"{e} This answer already got its {name}. Ask a new question with `/ask`!"
    if code == "busy":
        return f"{e} I'm already deep in thought about someone else's question -- try again in a few seconds."
    if code == "rate_limited_minute":
        return f"{e} Groq says too many requests at once. Try again in a minute -- this try wasn't used."
    if code == "rate_limited_day":
        return f"{e} My {name} brain hit Groq's daily cap. Try again later -- this try wasn't used."
    if code == "model_unavailable":
        return (f"{e} That model isn't available right now (Groq may have retired it). "
                "This try wasn't used -- tell whoever runs Milo.")
    if code == "store":
        return (f"{e} My limit tracker is acting up, so {name} is paused for now. "
                "Normal `/ask` still works!")
    return f"{e} Mrrp... my brain napped before I could finish. This try wasn't used -- try again!"


def _think_embed(tier: AskTier, question: str, answer: str, used: int) -> discord.Embed:
    if len(answer) > 3900:
        answer = answer[:3900].rstrip() + "…"
    embed = discord.Embed(
        title=f"{tier.emoji} {tier.label}", description=answer, color=discord.Color.blurple()
    )
    embed.add_field(name="❓ You asked", value=question[:1000], inline=False)
    embed.set_footer(
        text=f"{tier.model} · {used}/{tier.user_daily} used today · AI-generated, can be wrong"
    )
    return embed


async def _remove_think_button(message: discord.Message, clicked_custom_id: str | None) -> None:
    """Drop the pressed button (or all of them when clicked_custom_id is None),
    keeping any other tier's button that's still available. Best effort."""
    keep: list[str] = []
    if clicked_custom_id is not None:
        _, _, asker, ts = clicked_custom_id.split(":")
        for row in getattr(message, "components", []) or []:
            for child in getattr(row, "children", []) or []:
                cid = getattr(child, "custom_id", None)
                if cid and cid.startswith(THINK_BUTTON_PREFIX) and cid != clicked_custom_id:
                    keep.append(cid.split(":")[1])
        new_view = _build_think_view(int(asker), int(ts), keep)
    else:
        new_view = None
    try:
        await message.edit(view=new_view)
    except discord.HTTPException:
        pass


async def _handle_think_click(interaction: discord.Interaction, custom_id: str) -> None:
    async def say(text: str) -> None:
        """A private (ephemeral) note -- works whether or not we've deferred."""
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    if not is_allowed_guild(interaction.guild_id):
        await say("This bot is not configured for this server.")
        return
    blocked = gamblin_block_text("the think buttons")
    if blocked:
        await say(blocked)
        return

    parts = custom_id.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await say(f"{MILO_EMOJI} That button looks broken -- run `/ask` again.")
        return
    tier = _TIER_BY_KEY.get(parts[1])
    if tier is None:
        await say(f"{MILO_EMOJI} That thinking mode isn't turned on anymore -- run `/ask` again.")
        return
    asker_id, created = int(parts[2]), int(parts[3])
    message = interaction.message

    # Belt and braces: Discord itself records who ran the /ask this message
    # answers. If that disagrees with the button, something is off.
    meta_user = getattr(getattr(message, "interaction_metadata", None), "user", None)
    if meta_user is not None and meta_user.id != asker_id:
        await say(f"{MILO_EMOJI} Nice try -- that button doesn't match this answer.")
        return

    # 1) Only the person who asked.
    if interaction.user.id != asker_id:
        await say(f"{MILO_EMOJI} Only <@{asker_id}> can use this button -- run `/ask` yourself to get your own!")
        return

    # 2) Old answers are dead answers.
    age = time.time() - created
    if age > THINK_BUTTON_WINDOW_SECONDS or age < -60:
        await say(
            f"{MILO_EMOJI} This answer is too old -- these buttons only work for "
            f"{THINK_BUTTON_WINDOW_SECONDS // 60} minutes. Ask again with `/ask`!"
        )
        await _remove_think_button(message, None)
        return

    # 3) The question, read back from the embed Milo posted (users can't edit it).
    question = None
    embeds = getattr(message, "embeds", None) or []
    if embeds and embeds[0].fields and "You asked" in embeds[0].fields[0].name:
        question = embeds[0].fields[0].value
    if not question:
        await say(f"{MILO_EMOJI} I can't read the original question anymore -- run `/ask` again.")
        return

    # 4) One think at a time per tier (keeps Groq's tokens-per-minute happy).
    lock = _think_locks.setdefault(tier.key, asyncio.Lock())
    if lock.locked():
        await say(_think_error_text(tier, "busy", 0))
        return

    # Tell Discord we're working (the limit checks and the model both take a
    # moment), privately, so any errors below are only shown to this person.
    await interaction.response.defer(ephemeral=True, thinking=True)
    if lock.locked():  # someone else slipped in while we were deferring
        await say(_think_error_text(tier, "busy", 0))
        return

    async with lock:
        claim = await _claim_think(tier, asker_id, message.id)
        if claim.error:
            await say(_think_error_text(tier, claim.error, claim.retry))
            return

        answer, error = await _call_groq(
            tier.model, MILO_THINK_PROMPT, question,
            max_tokens=tier.max_tokens, effort=tier.effort, timeout_s=45,
        )
        if error or not answer:
            await claim.release()  # a failed think costs nothing
            await say(_think_error_text(tier, error or "error", 0))
            return

    logger.info("Think (%s) used by user %s (%d/%d today)", tier.key, asker_id, claim.user_used, tier.user_daily)
    embed = _think_embed(tier, question, answer, claim.user_used)

    posted = False
    try:
        await message.reply(embed=embed, mention_author=False)
        posted = True
    except discord.HTTPException:
        try:
            if interaction.channel is not None:
                await interaction.channel.send(embed=embed)
                posted = True
        except discord.HTTPException:
            pass

    if posted:
        try:
            await interaction.delete_original_response()  # remove the private "thinking..." note
        except discord.HTTPException:
            pass
    else:
        # Couldn't post publicly -- at least don't lose a paid-for answer.
        await interaction.followup.send(embed=embed, ephemeral=True)

    await _remove_think_button(message, custom_id)


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
            "`/join game:" + " / game:".join(_g[1] for _g in JOIN_GAMES) + "` — look for players "
            "(add `message:<text>` for a custom note)\n"
            "`/serverinfo` — info about this server\n"
            "`/userinfo user:<member>` — info about a member\n"
            "`/ping` — latency check (gateway, APIs, average, uptime)"
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
            "`/trivia` — answer a random trivia question (solo, private)\n"
            "`/grouptrivia` — everyone can answer; winners revealed after 60s\n"
            "`/riddle` — a riddle with the answer hidden behind a button\n"
            "`/yesno` — a random yes/no answer with a reaction gif\n"
            "`/throw user` — throw something at someone (or nearby, if left blank)\n"
            "`/react reaction:<search> user` — react at someone with a random anime gif "
            "(hug, slap, wave, cry, dance, and 100+ more from 4 sources), type to search · `/reactions` lists them all"
        ),
        inline=False,
    )
    payout_line = " · ".join(f"{sym} {pts}" for sym, pts in SLOT_PAYOUTS.items())
    embed.add_field(
        name=f"🎰 Slots & {PTS_CAP}",
        value=(
            f"`/slots` — spin for {PTS_NAME}: **{SLOTS_FREE_SPINS} free spins a day**, then up to "
            f"{SLOTS_PAID_SPINS} bonus spins ({SLOTS_PACK_SPINS}-spin packs for {pfmt(SLOTS_PACK_COST)}). "
            f"Every {SLOTS_JACKPOT_PITY_AT}th spin without a 777 **is** a 777! Spin one or all at once.\n"
            f"Match all 3 to win: {payout_line}\n"
            "`/points` — your balance, rank and spins left\n"
            f"`/daily` — claim free {PTS_NAME} every day; a streak makes it bigger (+{DAILY_WEEK_BONUS} bonus every 7th day!)\n"
            f"`/pay @user amount` — send {PTS_NAME} to a friend (up to {pamt(PAY_DAILY_CAP)} a day"
            + (f", {PAY_FEE_PERCENT}% fee" if PAY_FEE_PERCENT else "") + ")\n"
            f"`/leaderboard` — top 10 all-time earners (earn {LEADERBOARD_MIN_POINTS:,} to join) + top 5 richest right now"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎲 Gamblin, Lottery & Shop",
        value=(
            "`/gamblinstats` — your Gamblin record + the hall of fame\n"
            "`/lottery` — pick 3 numbers for the daily draw (rollover jackpot)\n"
            f"`/shop` — buy designs for Milo's messages with {PTS_NAME} (preview first!)\n"
            f"Bonus packs ask for confirmation before spending {PTS_NAME}\n"
            "🎲 Managers sometimes run **Gamblin** nights (slots, blackjack, duck race & roulette) — watch for the @everyone ping!"
        ),
        inline=False,
    )
    embed.add_field(
        name="✨ Party & Social",
        value=(
            "`/emojify text:<text>` — turn text into emoji letters\n"
            "`/mock text:<text>` — sPoNgEbOb CaSe your text\n"
            "`/fortune` — a fortune-cookie message\n"
            "`/compliment user` — give someone a compliment\n"
            "`/icebreaker` — a random conversation-starter question\n"
            "`/tableflip` / `/unflip` — (╯°□°)╯︵ ┻━┻ and back"
        ),
        inline=False,
    )
    embed.add_field(
        name="🌍 Lookups",
        value=(
            "`/quote` — a random inspirational quote\n"
            "`/weather location:<place>` — current weather\n"
            "`/translate text:<text> to:<lang>` — translate text\n"
            "`/joke` — a random dad joke\n"
            "`/advice` — random life advice\n"
            "`/comic` — a random xkcd comic\n"
            "`/meme` — a random meme"
        ),
        inline=False,
    )
    ask_help = (
        "`/ask question:<text>` — ask Milo anything (AI-powered, quick questions). "
        "Everyone shares one daily limit, so there's a short cooldown -- if Milo says "
        "he's out of brainpower, try again later!"
    )
    if THINK_TIERS:
        ask_help += (
            "\nUnder your answer: "
            + " / ".join(f"{t.emoji} **{t.label}**" for t in THINK_TIERS)
            + " re-ask the same question with a smarter model. Only you can press them, "
            f"for {THINK_BUTTON_WINDOW_SECONDS // 60} minutes, with limited tries per day."
        )
    embed.add_field(name="🤖 Ask Milo", value=ask_help, inline=False)
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
    name = f"/{interaction.command.qualified_name}" if interaction.command else "a command"
    if _is_discord_block(error):
        _note_discord_block(name)          # replying would just be another request that gets blocked
        return
    logger.error("Slash command error in %s: %s", name, error)
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
# ever appears in Discord's "/" picker and neither is listed in /help.
#   ?send     -> MANAGER_ROLE_ID or MILO_ROLE_ID
#   ?rolemenu -> MANAGER_ROLE_ID only
# Anyone else typing either one is silently ignored -- their existence isn't
# revealed.
# ---------------------------------------------------------------------------
async def _handle_send_command(message: discord.Message) -> None:
    allowed_role_ids = {r for r in (MANAGER_ROLE_ID, MILO_ROLE_ID) if r is not None}
    if not allowed_role_ids:
        return  # Feature disabled -- neither role is configured.
    if message.guild is None or not isinstance(message.author, discord.Member):
        return  # Only works inside a server, where roles exist.

    author_role_ids = {role.id for role in message.author.roles}
    if not (allowed_role_ids & author_role_ids):
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


# ---------------------------------------------------------------------------
# 🎲 GAMBLIN -- a manager-started, button-only betting event
#
#   ?gamblin        (Manager only) -> lobby -> N rounds -> final standings
#   ?gamblin stop   (Manager only) -> ends it early (nothing unresolved is charged)
#
# Everything lives in MEMORY while it runs. A bet is only "held" (locked) until
# its reel stops; balances change at that moment, in memory. While a session is
# live: /slots, /ask (+ think buttons), /grouptrivia and the autosave are paused.
# When it ends, ONE batched save goes to Upstash (about 1 command per player
# whose balance changed) and everything resumes. Gamblin never touches the
# all-time "earned" leaderboard -- only current balances move.
# ---------------------------------------------------------------------------
_raw_gamblin_channel = os.getenv("GAMBLIN_CHANNEL_ID", "").strip()
GAMBLIN_CHANNEL_ID = int(_raw_gamblin_channel) if _raw_gamblin_channel.isdigit() else None
GAMBLIN_LOBBY_SECONDS = _env_int("GAMBLIN_LOBBY_SECONDS", 90)    # join window
GAMBLIN_ROUNDS = _env_int("GAMBLIN_ROUNDS", 3)                   # machines per session
GAMBLIN_BET_SECONDS = _env_int("GAMBLIN_BET_SECONDS", 40)        # window before reel 1
GAMBLIN_REEL_SECONDS = _env_int("GAMBLIN_REEL_SECONDS", 25)      # windows before reels 2 and 3
GAMBLIN_MIN_BET = _env_int("GAMBLIN_MIN_BET", 5)
GAMBLIN_MAX_BET = max(GAMBLIN_MIN_BET, _env_int("GAMBLIN_MAX_BET", 100))
GAMBLIN_MAX_PLAYERS = _env_int("GAMBLIN_MAX_PLAYERS", 25)
GAMBLIN_MAX_MULT = _env_int("GAMBLIN_MAX_MULT", 500)             # cap on the 3-of-a-kind payouts
GAMBLIN_RTP = 0.92                                               # long-run return: 92 back per 100 bet
GAMBLIN_PREFIX = "gmb:"

# The Gamblin reels are WEIGHTED (rare symbols are rarer). Every payout below is
# computed from these exact weights, so the odds shown are the odds you get.
G_SYMS = ["🍒", "🍋", "🍉", "🍇", "⭐", "7️⃣"]
G_WEIGHTS = [24, 21, 18, 16, 12, 9]
G_P = [w / sum(G_WEIGHTS) for w in G_WEIGHTS]


def _floor1(x: float) -> float:
    return math.floor(x * 10 + 1e-9) / 10


def _g_mult(p: float, cap: float | None = None) -> float:
    m = _floor1(GAMBLIN_RTP / p)
    return min(cap, m) if cap else m


def _fx(m: float) -> str:
    return f"x{m:g}"


G_REEL_MULT = [_g_mult(p) for p in G_P]                      # Reel Rush: this one reel shows my symbol
_p_triple_any = sum(p ** 3 for p in G_P)
_p_all_different = 6 * sum(
    G_P[i] * G_P[j] * G_P[k] for i in range(6) for j in range(i + 1, 6) for k in range(j + 1, 6)
)
G_FULL: dict[str, tuple[str, float, float]] = {}                # Jackpot Spin: key -> (label, chance, multiplier)
for _i, _s in enumerate(G_SYMS):
    G_FULL[f"t{_i}"] = (f"3× {_s}", G_P[_i] ** 3, _g_mult(G_P[_i] ** 3, GAMBLIN_MAX_MULT))
G_FULL["any"] = ("Any triple", _p_triple_any, _g_mult(_p_triple_any, GAMBLIN_MAX_MULT))
G_FULL["pair"] = ("Pair or better", 1 - _p_all_different, _g_mult(1 - _p_all_different))

GAMBLIN: "GamblinSession | None" = None


def gamblin_block_text(feature: str) -> str | None:
    """A friendly 'paused' note while Gamblin runs, else None."""
    if GAMBLIN is None:
        return None
    where = f" in <#{GAMBLIN_CHANNEL_ID}>" if GAMBLIN_CHANNEL_ID else ""
    return f"🎲 **Gamblin** is running{where} — {feature} is paused until it's over. Back soon! {MILO_EMOJI}"


def _g_full_wins(pred: str, reels: list[str]) -> bool:
    if pred == "any":
        return reels[0] == reels[1] == reels[2]
    if pred == "pair":
        return len(set(reels)) <= 2
    return reels[0] == reels[1] == reels[2] == G_SYMS[int(pred[1:])]


def _g_odds(p: float) -> str:
    return f"1 in {round(1 / p):,}"


def _gb(label: str, cid: str, style: str = "grey", disabled: bool = False) -> discord.ui.Button:
    styles = {
        "green": discord.ButtonStyle.success, "blue": discord.ButtonStyle.primary,
        "grey": discord.ButtonStyle.secondary, "red": discord.ButtonStyle.danger,
    }
    return discord.ui.Button(label=label, custom_id=cid, style=styles[style], disabled=disabled)


def _gview(*buttons: discord.ui.Button) -> discord.ui.View:
    # Buttons have no callbacks: the raw on_interaction listener handles them.
    view = discord.ui.View(timeout=1800)
    for b in buttons:
        view.add_item(b)
    return view


def _gtarget(t: str, x: str):
    """What a bet is on: (description, chance, multiplier), or None if the id is junk."""
    if t == "f":
        entry = G_FULL.get(x)
        if entry is None:
            return None
        label, p, m = entry
        return f"Jackpot Spin → **{label}**", p, m
    if t.startswith("r") and t[1:].isdigit() and x.isdigit() and 0 <= int(x) < len(G_SYMS) and t[1:] in ("1", "2", "3"):
        i = int(x)
        return f"Reel {t[1:]} → **{G_SYMS[i]}**", G_P[i], G_REEL_MULT[i]
    return None


@dataclass
class GPlayer:
    uid: int
    pending: int = 0                 # points locked in bets that haven't resolved yet
    net: int = 0                     # profit/loss this session
    round_net: int = 0
    played: int = 0                  # bets settled this session (for /gamblinstats)
    wagered: int = 0
    best_win: int = 0
    mode: str | None = None          # "full" or "reel" (locked for the round once chosen)
    full: tuple[str, int] | None = None
    reel: dict = field(default_factory=dict)   # stage -> (symbol index, stake)

    def reset_round(self) -> None:
        self.mode, self.full, self.reel, self.round_net = None, None, {}, 0


def _g_avail(p: GPlayer) -> int:
    return POINTS.points(p.uid) - p.pending


class GamblinSession:
    NAME = "Gamblin"
    EMOJI = "🎰"
    JOIN_ID = "gmb:join"

    @property
    def total_rounds(self) -> int:
        return GAMBLIN_ROUNDS

    def _on_end(self) -> None:
        """Runs once when a session ends (normally OR stopped early): a player's
        POSITIVE session profit counts towards the all-time leaderboard."""
        if getattr(self, "_credited", False):
            return
        self._credited = True
        for p in self.players.values():
            if p.net > 0:
                POINTS.add_earned(p.uid, p.net)
            if p.played:
                POINTS.record_session(p.uid, self.NAME, p.net, p.best_win, p.wagered)

    def __init__(self, channel, started_by: int) -> None:
        self.channel = channel
        self.started_by = started_by
        self.players: dict[int, GPlayer] = {}
        self.phase = "lobby"             # lobby -> betting -> ending
        self.round = 0
        self.stage = 0                   # which reel's betting window (1-3)
        self.window_open = False
        self.deadline = 0.0
        self.reels: list[str] = []
        self.revealed = 0
        self.msg: discord.Message | None = None
        self.lobby_msg: discord.Message | None = None
        self.task: asyncio.Task | None = None
        self.shutting_down = False
        self.aborted = False

    # ---- small helpers ---------------------------------------------------
    async def _send(self, **kw):
        kw.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            return await self.channel.send(**kw)
        except discord.HTTPException:
            logger.warning("Gamblin: couldn't post a message", exc_info=True)
            return None

    async def _edit(self, msg, **kw):
        if msg is None:
            return
        try:
            await msg.edit(**kw)
        except discord.HTTPException:
            logger.warning("Gamblin: couldn't edit a message", exc_info=True)

    def _settle(self, p: GPlayer, stake: int, payout: int) -> int:
        """A bet resolved: release the hold and move the real balance. Returns profit."""
        p.pending -= stake
        delta = payout - stake
        p.played += 1
        p.wagered += stake
        p.best_win = max(p.best_win, delta)
        POINTS.apply_gamble(p.uid, delta)
        p.net += delta
        p.round_net += delta
        return delta

    # ---- the whole show ----------------------------------------------------
    async def run(self) -> None:
        global GAMBLIN
        try:
            await self._lobby()
            if not self.players:
                await self._send(content=f"{self.EMOJI} Nobody joined, so {self.NAME} is cancelled. Everything's back to normal! 🐾")
            else:
                for r in range(1, self.total_rounds + 1):
                    await self._play_round(r)
                    if getattr(self, "stop_early", False):
                        break
                    if r < self.total_rounds:
                        await asyncio.sleep(8)
                await self._finish()
                await self._winner_card()
        except asyncio.CancelledError:
            self.aborted = True
            self.phase = "ending"
            self.window_open = False
            logger.info("Gamblin was stopped early.")
            note = (
                f"{self.EMOJI} Milo is restarting, so {self.NAME} ended early. Bets that hadn't landed yet were never charged."
                if self.shutting_down else
                f"{self.EMOJI} {self.NAME} was ended early by a manager. Bets that hadn't landed yet were never charged."
            )
            await self._send(content=note)
        except Exception:
            logger.exception("Gamblin crashed -- ending the session safely")
            self.aborted = True
            await self._send(content=f"{self.EMOJI} Something broke, so {self.NAME} ended early. Unresolved bets were never charged.")
        finally:
            for p in self.players.values():
                p.pending = 0
            self.phase = "ending"
            self.window_open = False
            if self.msg is not None:
                await self._edit(self.msg, view=None)
            try:
                self._on_end()
            except Exception:
                logger.exception("%s end-of-session hook failed", self.NAME)
            if GAMBLIN is self:
                GAMBLIN = None
        if self.players:
            lines = [f"<@{p.uid}> {p.net:+,}" for p in sorted(self.players.values(), key=lambda q: -q.net)]
            await log_event(
                f"{self.EMOJI} {self.NAME} {'ended early' if self.aborted else 'finished'} — {len(self.players)} players:\n" + "\n".join(lines)
            )
        if not self.shutting_down:
            ok, people, detail = await POINTS.flush("gamblin")
            if ok:
                logger.info("Gamblin: saved to Upstash (%d people).", people)
            else:
                logger.warning("Gamblin: save failed (%s) -- autosave will retry.", detail)

    def _lobby_embed(self, end_ts: int) -> discord.Embed:
        return discord.Embed(
            title="🎰 GAMBLIN is starting!",
            description=(
                "**[ ❔ | ❔ | ❔ ]**\n"
                f"Press **Join** to play with your {PTS_NAME}. Starts <t:{end_ts}:R>.\n\n"
                f"• {GAMBLIN_ROUNDS} rounds · bets {GAMBLIN_MIN_BET}–{pfmt(GAMBLIN_MAX_BET)}\n"
                "• Your winnings count towards the **leaderboard**\n"
                "• Everything is buttons — no typing needed\n"
                "• `/slots` and `/ask` are paused until it's over"
            ),
            color=discord.Color.gold(),
        )

    async def _lobby(self) -> None:
        end_ts = int(time.time()) + GAMBLIN_LOBBY_SECONDS
        embed = self._lobby_embed(end_ts)
        self.lobby_msg = await self._send(
            content="@everyone", embed=embed, view=_gview(_gb(f"Join {self.NAME}", self.JOIN_ID, "green")),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        if self.lobby_msg is None:
            raise RuntimeError("couldn't post the lobby message (check Milo's permissions in the Gamblin channel)")
        await asyncio.sleep(GAMBLIN_LOBBY_SECONDS)
        self.phase = "betting"
        try:
            await self.lobby_msg.delete()
        except discord.HTTPException:
            pass

    def _odds_fields(self, embed: discord.Embed, stage: int) -> None:
        if stage == 1:
            lines = [f"{label} · **{_fx(m)}** · {_g_odds(p)}" for label, p, m in G_FULL.values()]
            embed.add_field(name="🎰 Jackpot Spin — bet on all 3 reels (before reel 1)", value="\n".join(lines), inline=False)
        reel_line = " · ".join(f"{s} **{_fx(m)}**" for s, m in zip(G_SYMS, G_REEL_MULT))
        embed.add_field(name=f"🎚️ Reel Rush — bet on reel {stage}", value=reel_line, inline=False)

    def _round_embed(self, status: str, stage: int | None = None) -> discord.Embed:
        shown = [self.reels[i] if i < self.revealed else "❔" for i in range(3)]
        embed = discord.Embed(
            title=f"🎰 Gamblin — Round {self.round}/{GAMBLIN_ROUNDS}",
            description=f"**[ {' | '.join(shown)} ]**\n{status}",
            color=discord.Color.gold(),
        )
        if stage:
            self._odds_fields(embed, stage)
        embed.set_footer(text=f"{len(self.players)} players · bets {GAMBLIN_MIN_BET}–{pamt(GAMBLIN_MAX_BET)}")
        return embed

    async def _play_round(self, r: int) -> None:
        self.round = r
        self.reels = random.choices(G_SYMS, weights=G_WEIGHTS, k=3)   # same weights as the odds
        self.revealed = 0
        for p in self.players.values():
            p.reset_round()
        self.phase = "betting"
        self.msg = None

        for stage in (1, 2, 3):
            secs = GAMBLIN_BET_SECONDS if stage == 1 else GAMBLIN_REEL_SECONDS
            self.stage = stage
            self.deadline = time.time() + secs
            if stage == 1:
                view = _gview(
                    _gb("🎰 Jackpot Spin (all 3 reels)", "gmb:open:full", "green"),
                    _gb("🎚️ Reel Rush (reel by reel)", "gmb:open:reel", "blue"),
                )
            else:
                view = _gview(_gb(f"🎚️ Bet on reel {stage}", "gmb:open:reel", "blue"))
            status = (
                f"Bets close <t:{int(self.deadline)}:R>\n"
                + ("Pick **Jackpot Spin** (one bet on the whole spin) or **Reel Rush** (bet reel by reel)."
                   if stage == 1 else
                   f"Reel {stage - 1} landed {self.reels[stage - 2]}! **Reel Rush** players can bet on reel {stage} now.")
            )
            embed = self._round_embed(status, stage)
            if self.msg is None:
                self.msg = await self._send(embed=embed, view=view)
                if self.msg is None:
                    raise RuntimeError("couldn't post the round message")
            else:
                await self._edit(self.msg, embed=embed, view=view)

            self.window_open = True
            await asyncio.sleep(secs)
            self.window_open = False                       # sync from here on: no late bets can sneak in

            await self._edit(self.msg, embed=self._round_embed(f"🌀 Reel {stage} is spinning…"), view=None)
            await asyncio.sleep(2.5)
            self.revealed = stage
            await self._reveal(stage)

        await self._edit(self.msg, embed=self._round_embed("Round over!"), view=None)

    async def _reveal(self, stage: int) -> None:
        sym = self.reels[stage - 1]
        lines: list[str] = []
        for p in self.players.values():
            bet = p.reel.get(stage)
            if not bet:
                continue
            idx, stake = bet
            if G_SYMS[idx] == sym:
                payout = math.floor(stake * G_REEL_MULT[idx])
                profit = self._settle(p, stake, payout)
                lines.append(f"✅ <@{p.uid}> bet **{stake}** on {G_SYMS[idx]} → **+{profit}**")
            else:
                self._settle(p, stake, 0)
                lines.append(f"❌ <@{p.uid}> bet **{stake}** on {G_SYMS[idx]} → −{stake}")

        title = f"🎚️ Reel {stage} landed {sym}"
        if stage == 3:
            title = f"🎰 Final: [ {' | '.join(self.reels)} ]"
            lines.append("")
            lines.append("**Jackpot Spin bets**")
            full_lines = []
            for p in self.players.values():
                if not p.full:
                    continue
                pred, stake = p.full
                label, _, mult = G_FULL[pred]
                if _g_full_wins(pred, self.reels):
                    profit = self._settle(p, stake, math.floor(stake * mult))
                    full_lines.append(f"✅ <@{p.uid}> {label} ({_fx(mult)}) → **+{profit}**")
                else:
                    self._settle(p, stake, 0)
                    full_lines.append(f"❌ <@{p.uid}> {label} → −{stake}")
            lines.extend(full_lines or ["No Jackpot Spin bets this round."])
            movers = [p for p in self.players.values() if p.round_net]
            if movers:
                lines.append("")
                lines.append("**Round result**")
                for p in sorted(movers, key=lambda q: -q.round_net):
                    lines.append(f"<@{p.uid}> {p.round_net:+,} → balance **{POINTS.points(p.uid):,}**")
        elif not lines:
            lines = ["No Reel Rush bets on this reel."]
        embed = discord.Embed(title=title, description="\n".join(lines)[:4000], color=discord.Color.gold())
        await self._send(embed=embed)

    async def _winner_card(self) -> None:
        """A separate, specially designed message for the biggest winner (in the design they bought)."""
        if not self.players:
            return
        top = max(self.players.values(), key=lambda p: (p.net, -p.uid))
        if top.net <= 0:
            return
        stats = POINTS.stats.get(top.uid, {})
        await self._send(embed=_render_win_card(
            style_of(top.uid, "win"), top.uid, f"{self.EMOJI} {self.NAME}", top.net, top.best_win,
            top.best_win > stats.get("bw", 0), stats.get("s", 0) + 1))

    async def _finish(self) -> None:
        self.phase = "ending"
        ranked = sorted(self.players.values(), key=lambda q: (-q.net, q.uid))
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{p.uid}> — **{p.net:+,}** (balance {POINTS.points(p.uid):,})"
            + (f" · 🏆 +{p.net:,} to leaderboard" if p.net > 0 else "")
            for i, p in enumerate(ranked)
        ]
        embed = discord.Embed(
            title="🎰 Gamblin is over!",
            description="\n".join(lines)[:4000] + f"\n\n💾 Saving everyone's {PTS_NAME}… `/slots` and `/ask` are back!",
            color=discord.Color.gold(),
        )
        await self._send(embed=embed)


# ---- button handling (raw on_interaction -> here) -------------------------------------
async def _gpanel(interaction: discord.Interaction, text: str, view: discord.ui.View | None = None) -> None:
    """Show a private panel: edit it in place if the click came from one, else open a new one."""
    msg = interaction.message
    if msg is not None and msg.flags.ephemeral:
        await interaction.response.edit_message(content=text, view=view)
    else:
        kwargs = {"view": view} if view is not None else {}
        await interaction.response.send_message(text, ephemeral=True, **kwargs)


def _gcheck(g: GamblinSession, p: GPlayer, t: str, x: str) -> str | None:
    if _gtarget(t, x) is None:
        return "That button looks broken — open a fresh bet from the main message."
    if g.phase != "betting" or not g.window_open:
        return "⏳ Betting is closed right now — wait for the next window."
    if t == "f":
        if g.stage != 1:
            return "🎰 Jackpot Spin bets close before the first reel stops."
        if p.mode == "reel":
            return "🎚️ You're playing Reel Rush this round."
        if p.full:
            return "🔒 You're already locked in for the Jackpot Spin."
    else:
        n = int(t[1:])
        if n != g.stage:
            return "That reel's betting window isn't open anymore."
        if p.mode == "full":
            return "🔒 You're locked into Jackpot Spin — wait for the reels!"
        if n in p.reel:
            return "🔒 You already bet on this reel."
    return None


def _gstake_error(p: GPlayer, amt: int) -> str | None:
    avail = _g_avail(p)
    if amt < GAMBLIN_MIN_BET:
        return f"Minimum bet is {pfmt(GAMBLIN_MIN_BET)}."
    if amt > GAMBLIN_MAX_BET:
        return f"Maximum bet is {pfmt(GAMBLIN_MAX_BET)}."
    if amt > avail:
        return f"You only have {pfmt(avail)} free to bet."
    return None


async def _g_pick_panel(interaction, p: GPlayer, kind: str) -> None:
    if kind == "full":
        lines = [f"{label} — pays **{_fx(m)}** ({_g_odds(pr)})" for label, pr, m in G_FULL.values()]
        text = "🎰 **Jackpot Spin** — one bet on the whole spin. What will the 3 reels show?\n" + "\n".join(lines)
        buttons = [_gb(f"{G_FULL[k][0]} {_fx(G_FULL[k][2])}", f"gmb:pick:f:{k}", "grey") for k in G_FULL]
    else:
        n = GAMBLIN.stage
        lines = [f"{s} — pays **{_fx(m)}** ({pr * 100:.0f}% chance)" for s, m, pr in zip(G_SYMS, G_REEL_MULT, G_P)]
        text = f"🎚️ **Reel Rush — reel {n}.** What will this reel land on?\n" + "\n".join(lines)
        buttons = [_gb(f"{s} {_fx(m)}", f"gmb:pick:r{n}:{i}", "grey") for i, (s, m) in enumerate(zip(G_SYMS, G_REEL_MULT))]
    await _gpanel(interaction, text, _gview(*buttons))


async def _g_stake_panel(interaction, p: GPlayer, t: str, x: str) -> None:
    desc, prob, mult = _gtarget(t, x)
    avail = _g_avail(p)
    cap = min(avail, GAMBLIN_MAX_BET)
    if cap < GAMBLIN_MIN_BET:
        await _gpanel(interaction, f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet (you have {avail:,}).")
        return
    seen: set[int] = set()
    buttons = []
    for pct in (5, 10, 25):
        amt = min(cap, max(GAMBLIN_MIN_BET, avail * pct // 100))
        if amt not in seen:
            seen.add(amt)
            buttons.append(_gb(f"{pct}% · {amt}", f"gmb:conf:{t}:{x}:{amt}", "grey"))
    if cap not in seen:
        label = "All-in" if cap == avail else "Max bet"
        buttons.append(_gb(f"{label} · {cap}", f"gmb:conf:{t}:{x}:{cap}", "red"))
    buttons.append(_gb("✏️ Custom", f"gmb:cust:{t}:{x}", "blue"))
    buttons.append(_gb("← Back", "gmb:open:full" if t == "f" else "gmb:open:reel", "grey"))
    text = (
        f"{desc} · pays **{_fx(mult)}** · {_g_odds(prob)}\n"
        f"You have {pcompact(avail, True)} free (max bet {GAMBLIN_MAX_BET}). How much do you want to bet?"
    )
    await _gpanel(interaction, text, _gview(*buttons))


async def _g_confirm_panel(interaction, p: GPlayer, t: str, x: str, amt: int) -> None:
    desc, prob, mult = _gtarget(t, x)
    payout = math.floor(amt * mult)
    text = (
        f"Lock {pcompact(amt, True)} on {desc}?\n"
        f"Win → **{payout:,}** back (+{payout - amt:,}) · Lose → −{amt:,}\n"
        "⚠️ Once locked, it can't be changed."
    )
    await _gpanel(interaction, text, _gview(
        _gb(f"🔒 Lock it in ({amt})", f"gmb:lock:{t}:{x}:{amt}", "green"),
        _gb("← Back", f"gmb:pick:{t}:{x}", "grey"),
    ))


class GamblinStakeModal(discord.ui.Modal):
    """The popup for typing your own bet amount (a popup, not a chat message)."""

    def __init__(self, t: str, x: str, cap: int) -> None:
        super().__init__(title="Custom bet", timeout=300)
        self.t, self.x = t, x
        self.amount = discord.ui.TextInput(
            label=f"{PTS_CAP} ({GAMBLIN_MIN_BET}–{cap})", placeholder=str(cap), min_length=1, max_length=7,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        g = GAMBLIN
        p = g.players.get(interaction.user.id) if g is not None and type(g) is GamblinSession else None
        if g is None or p is None:
            await interaction.response.send_message("🎰 That Gamblin session is over.", ephemeral=True)
            return
        err = _gcheck(g, p, self.t, self.x)
        raw = str(self.amount.value).strip().replace(",", "")
        if err is None and not raw.isdigit():
            err = "Numbers only please 🐾"
        if err is None:
            err = _gstake_error(p, int(raw))
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await _g_confirm_panel(interaction, p, self.t, self.x, int(raw))


async def handle_gamblin_click(interaction: discord.Interaction, custom_id: str) -> None:
    async def say(text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    if not is_allowed_guild(interaction.guild_id):
        await say("This bot is not configured for this server.")
        return
    g = GAMBLIN
    if g is None or type(g) is not GamblinSession:
        await say("🎰 That Gamblin session is over — catch the next one! 🐾")
        return
    parts = custom_id.split(":")
    action = parts[1] if len(parts) > 1 else ""
    uid = interaction.user.id

    if action == "join":
        if g.phase != "lobby":
            await say("🎰 Gamblin already started — join the next one!")
        elif uid in g.players:
            await say("You're already in! 🎰")
        elif frozen_block(uid):
            await say(frozen_block(uid))
        elif len(g.players) >= GAMBLIN_MAX_PLAYERS:
            await say("🎰 The table is full!")
        elif POINTS.points(uid) < GAMBLIN_MIN_BET:
            await say(f"You need at least {pfmt(GAMBLIN_MIN_BET)} to play (you have {POINTS.points(uid)}). Win some in `/slots`!")
        else:
            g.players[uid] = GPlayer(uid)
            await interaction.response.send_message(
                f"🎰 <@{uid}> is participating! ({len(g.players)} in)",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        return

    p = g.players.get(uid)
    if p is None:
        await say("🎰 You're not in this Gamblin — join the next one!")
        return

    if action == "open" and len(parts) == 3 and parts[2] in ("full", "reel"):
        kind = parts[2]
        t = "f" if kind == "full" else f"r{g.stage}"
        if g.phase != "betting" or not g.window_open:
            await say("⏳ Betting is closed right now — wait for the next window.")
            return
        # Use any valid symbol just to run the shared state checks.
        err = _gcheck(g, p, t, "any" if kind == "full" else "0")
        if err is not None:
            await say(err)
            return
        if _g_avail(p) < GAMBLIN_MIN_BET:
            await say(f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet (you have {_g_avail(p):,}).")
            return
        await _g_pick_panel(interaction, p, kind)
        return

    if action in ("pick", "conf", "lock", "cust") and len(parts) >= 4:
        t, x = parts[2], parts[3]
        err = _gcheck(g, p, t, x)
        if err is not None:
            await say(err)
            return
        if action == "pick":
            await _g_stake_panel(interaction, p, t, x)
            return
        if action == "cust":
            cap = min(_g_avail(p), GAMBLIN_MAX_BET)
            if cap < GAMBLIN_MIN_BET:
                await say(f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet.")
                return
            await interaction.response.send_modal(GamblinStakeModal(t, x, cap))
            return
        if len(parts) != 5 or not parts[4].isdigit():
            await say("That button looks broken — open a fresh bet from the main message.")
            return
        amt = int(parts[4])
        err = _gstake_error(p, amt)
        if err is not None:
            await say(err)
            return
        if action == "conf":
            await _g_confirm_panel(interaction, p, t, x, amt)
            return
        # action == "lock": instant memory work, no awaits between the checks and the commit.
        desc, _, mult = _gtarget(t, x)
        if t == "f":
            p.full, p.mode = (x, amt), "full"
        else:
            p.reel[int(t[1:])], p.mode = (int(x), amt), "reel"
        p.pending += amt
        tail = ("Wait for the reels — good luck! 🍀" if t == "f"
                else "Result comes when the reel stops. You can bet on the next reel when it opens.")
        await _gpanel(interaction, f"🔒 **Locked:** {pfmt(amt)} on {desc} (pays {_fx(mult)}).\n{tail}")
        return

    await say("That button looks broken — open a fresh bet from the main message.")


async def _gamblin_shutdown() -> None:
    """Called from close(): stop a live session cleanly before the final save."""
    g = GAMBLIN
    if g is None or g.task is None:
        return
    g.shutting_down = True
    g.task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(g.task), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass



# ---------------------------------------------------------------------------
# 🃏 BLACKJACK GAMBLIN -- started from ?gamblin (pick "Blackjack", or ?gamblin bj)
#
# Button-only, runs in GAMBLIN_CHANNEL_ID, and halts exactly the same things as
# every other Gamblin game (/slots, /ask + think buttons, /grouptrivia, autosave). Only one
# Gamblin of either kind can run at a time.
#
# Rules: 6 decks, fresh shoe every round. Dealer stands on all 17s. Blackjack pays
# 3:2 (rounded down). Win = 1:1, push = stake back. You can Hit, Stand or Double
# down (first two cards only). No split / insurance / surrender. If the dealer has
# blackjack the round ends at once (you can't double into it).
#
# Leaderboard: a player's POSITIVE session profit is added to their all-time
# "total earned" when the session ends (so winning counts, but you can't farm the
# board by winning and losing over and over within one session).
# ---------------------------------------------------------------------------
GAMBLIN_BJ_ROUNDS = _env_int("GAMBLIN_BJ_ROUNDS", 5)
GAMBLIN_BJ_BET_SECONDS = _env_int("GAMBLIN_BJ_BET_SECONDS", 30)
GAMBLIN_BJ_TURN_SECONDS = _env_int("GAMBLIN_BJ_TURN_SECONDS", 40)
BJ_PREFIX = "bj:"
_BJ_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
_BJ_SUITS = ["♠", "♥", "♦", "♣"]
_BJ_FACE = {"J", "Q", "K"}


def _bj_shoe() -> list[tuple[str, str]]:
    cards = [(r, s) for _ in range(6) for r in _BJ_RANKS for s in _BJ_SUITS]
    random.shuffle(cards)
    return cards


def _bj_total(cards) -> int:
    total = sum(11 if r == "A" else 10 if r in _BJ_FACE or r == "10" else int(r) for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _bj_is_natural(cards) -> bool:
    return len(cards) == 2 and _bj_total(cards) == 21


def _bj_fmt(cards, hide_second: bool = False) -> str:
    shown = [f"`{r}{s}`" for r, s in cards]
    if hide_second and len(shown) >= 2:
        shown = [shown[0], "`🂠`"]
    return " ".join(shown) if shown else "—"


@dataclass
class BJPlayer(GPlayer):
    bet: int = 0                    # total staked this round (doubles if they double down)
    cards: list = field(default_factory=list)
    status: str = ""                # "" = still playing; "stand" / "bust" / "bj"
    doubled: bool = False

    def reset_round(self) -> None:
        super().reset_round()
        self.bet, self.cards, self.status, self.doubled = 0, [], "", False


class BlackjackSession(GamblinSession):
    NAME = "Blackjack"
    EMOJI = "🃏"
    JOIN_ID = "bj:join"

    def __init__(self, channel, started_by: int) -> None:
        super().__init__(channel, started_by)
        self.bj = ""                 # "" -> "bet" -> "deal" -> "play" -> "dealer" -> "done"
        self.dealer: list = []
        self.shoe: list = []
        self.dirty = False
        self.empty_rounds = 0
        self.stop_early = False
        self._credited = False

    @property
    def total_rounds(self) -> int:
        return GAMBLIN_BJ_ROUNDS

    def _lobby_embed(self, end_ts: int) -> discord.Embed:
        return discord.Embed(
            title="🃏 BLACKJACK is starting!",
            description=(
                "**[ `A♠` | `K♥` ]**\n"
                f"Press **Join** to play with your {PTS_NAME}. Starts <t:{end_ts}:R>.\n\n"
                f"• {GAMBLIN_BJ_ROUNDS} rounds vs the dealer · bets {GAMBLIN_MIN_BET}–{pfmt(GAMBLIN_MAX_BET)}\n"
                "• **Hit / Stand / Double** — blackjack pays **3:2**, dealer stands on 17\n"
                "• Your winnings count towards the **leaderboard**\n"
                "• Everything is buttons — no typing needed\n"
                "• `/slots` and `/ask` are paused until it's over"
            ),
            color=discord.Color.dark_green(),
        )

    # ---- drawing the table ----------------------------------------------------
    def _badge(self, p: BJPlayer) -> str:
        return {"": "🤔 thinking…", "stand": "✋ stand", "bust": "💥 bust", "bj": "🂡 BLACKJACK"}[p.status]

    def _table_embed(self, status: str, *, reveal: bool = False, results: dict | None = None) -> discord.Embed:
        e = discord.Embed(
            title=f"🃏 Blackjack — Round {self.round}/{GAMBLIN_BJ_ROUNDS}", color=discord.Color.dark_green()
        )
        lines = [status, ""]
        if self.dealer:
            if reveal:
                lines.append(f"🎩 **Dealer:** {_bj_fmt(self.dealer)} = **{_bj_total(self.dealer)}**")
            else:
                lines.append(f"🎩 **Dealer:** {_bj_fmt(self.dealer, hide_second=True)}")
            lines.append("")
        for p in self.players.values():
            if not p.bet:
                continue
            tag = " ⏫" if p.doubled else ""
            line = f"<@{p.uid}> {_bj_fmt(p.cards)} = **{_bj_total(p.cards)}** · bet {p.bet:,}{tag} · {self._badge(p)}"
            if results and p.uid in results:
                line += f"  →  {results[p.uid]}"
            lines.append(line)
        e.description = "\n".join(lines)[:4000]
        e.set_footer(text=f"{len(self.players)} players · bets {GAMBLIN_MIN_BET}–{pamt(GAMBLIN_MAX_BET)} · blackjack pays 3:2")
        return e

    # ---- one round --------------------------------------------------------------
    async def _play_round(self, r: int) -> None:
        self.round = r
        self.phase = "betting"
        self.bj = "bet"
        self.dealer, self.shoe, self.dirty = [], _bj_shoe(), False
        for p in self.players.values():
            p.reset_round()

        secs = GAMBLIN_BJ_BET_SECONDS
        self.deadline = time.time() + secs
        embed = discord.Embed(
            title=f"🃏 Blackjack — Round {r}/{GAMBLIN_BJ_ROUNDS}",
            description=(
                f"💰 **Place your bets!** Closing <t:{int(self.deadline)}:R>\n"
                f"Press **Place bet** — bets {GAMBLIN_MIN_BET}–{pfmt(GAMBLIN_MAX_BET)}. "
                "New players can sit down any round."
            ),
            color=discord.Color.dark_green(),
        )
        self.msg = await self._send(embed=embed, view=_gview(_gb("💰 Place bet", "bj:bet", "green")))
        if self.msg is None:
            raise RuntimeError("couldn't post the round message")
        self.window_open = True
        await asyncio.sleep(secs)
        self.window_open = False                    # sync from here: no late bets

        bettors = [p for p in self.players.values() if p.bet > 0]
        if not bettors:
            self.empty_rounds += 1
            self.bj = "done"
            note = "Nobody bet this round."
            if self.empty_rounds >= 2:
                self.stop_early = True
                note += " Two quiet rounds in a row, so the table is closing."
            await self._edit(self.msg, embed=self._table_embed(note), view=None)
            return
        self.empty_rounds = 0

        # deal
        self.bj = "deal"
        for p in bettors:
            p.cards = [self.shoe.pop(), self.shoe.pop()]
            if _bj_is_natural(p.cards):
                p.status = "bj"
        self.dealer = [self.shoe.pop(), self.shoe.pop()]
        dealer_bj = _bj_is_natural(self.dealer)

        # player turns
        if not dealer_bj and any(not p.status for p in bettors):
            self.bj = "play"
            self.deadline = time.time() + GAMBLIN_BJ_TURN_SECONDS
            play_view = _gview(_gb("🃏 My hand", "bj:hand", "blue"))
            status = f"🃏 **Your move!** Tap **My hand** to Hit / Stand / Double. Auto-stand <t:{int(self.deadline)}:R>"
            await self._edit(self.msg, embed=self._table_embed(status), view=play_view)
            last = time.time()
            while time.time() < self.deadline and any(not p.status for p in bettors):
                await asyncio.sleep(0.5)
                if self.dirty and time.time() - last >= 3:
                    self.dirty, last = False, time.time()
                    await self._edit(self.msg, embed=self._table_embed(status), view=play_view)
        self.bj = "dealer"                           # sync: no more actions
        for p in bettors:
            if not p.status:
                p.status = "stand"

        # dealer
        await self._edit(self.msg, embed=self._table_embed("🎩 **Dealer reveals…**", reveal=True), view=None)
        await asyncio.sleep(1.5)
        if not dealer_bj and any(p.status == "stand" for p in bettors):
            while _bj_total(self.dealer) < 17:
                self.dealer.append(self.shoe.pop())
                await self._edit(self.msg, embed=self._table_embed("🎩 **Dealer draws…**", reveal=True), view=None)
                await asyncio.sleep(1.5)

        # settle
        dt = _bj_total(self.dealer)
        results: dict[int, str] = {}
        for p in bettors:
            pt, bet = _bj_total(p.cards), p.bet
            if p.status == "bust":
                payout = 0
            elif p.status == "bj" and not dealer_bj:
                payout = bet + bet * 3 // 2
            elif dealer_bj:
                payout = bet if p.status == "bj" else 0
            elif dt > 21 or pt > dt:
                payout = bet * 2
            elif pt == dt:
                payout = bet
            else:
                payout = 0
            profit = self._settle(p, bet, payout)
            results[p.uid] = f"✅ **+{profit:,}**" if profit > 0 else ("🤝 push" if profit == 0 else f"❌ **{profit:,}**")
        self.bj = "done"
        verdict = "🂡 **Dealer has blackjack!**" if dealer_bj else (f"🎩 Dealer busts with {dt}!" if dt > 21 else f"🎩 Dealer stands on {dt}.")
        await self._edit(self.msg, embed=self._table_embed(f"{verdict}\n**Round over!**", reveal=True, results=results), view=None)

    async def _finish(self) -> None:
        self.phase = "ending"
        ranked = sorted(self.players.values(), key=lambda q: (-q.net, q.uid))
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{p.uid}> — **{p.net:+,}** (balance {POINTS.points(p.uid):,})"
            + (f" · 🏆 +{p.net:,} to leaderboard" if p.net > 0 else "")
            for i, p in enumerate(ranked)
        ]
        embed = discord.Embed(
            title="🃏 Blackjack is over!",
            description="\n".join(lines)[:4000] + f"\n\n💾 Saving everyone's {PTS_NAME}… `/slots` and `/ask` are back!",
            color=discord.Color.dark_green(),
        )
        await self._send(embed=embed)


# ---- button handling (raw on_interaction -> here) -------------------------------------
async def _bj_stake_panel(interaction, p: BJPlayer) -> None:
    avail = _g_avail(p)
    cap = min(avail, GAMBLIN_MAX_BET)
    if cap < GAMBLIN_MIN_BET:
        await _gpanel(interaction, f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet (you have {avail:,}).")
        return
    seen: set[int] = set()
    buttons = []
    for pct in (5, 10, 25):
        amt = min(cap, max(GAMBLIN_MIN_BET, avail * pct // 100))
        if amt not in seen:
            seen.add(amt)
            buttons.append(_gb(f"{pct}% · {amt}", f"bj:conf:{amt}", "grey"))
    if cap not in seen:
        label = "All-in" if cap == avail else "Max bet"
        buttons.append(_gb(f"{label} · {cap}", f"bj:conf:{cap}", "red"))
    buttons.append(_gb("✏️ Custom", "bj:cust", "blue"))
    text = (
        f"🃏 **How much do you want to bet this round?**\n"
        f"You have {pcompact(avail, True)} free (max bet {GAMBLIN_MAX_BET}). Blackjack pays 3:2, win pays 1:1."
    )
    await _gpanel(interaction, text, _gview(*buttons))


async def _bj_confirm_panel(interaction, amt: int) -> None:
    text = f"Sit down with {pcompact(amt, True)}?\n⚠️ Once locked, it can't be changed (you can still Double later if you have the {PTS_NAME})."
    await _gpanel(interaction, text, _gview(
        _gb(f"🔒 Lock it in ({amt})", f"bj:lock:{amt}", "green"),
        _gb("← Back", "bj:bet", "grey"),
    ))


def _bj_bet_error(g: "BlackjackSession", p: BJPlayer) -> str | None:
    if g.bj != "bet" or not g.window_open:
        return "⏳ Bets are closed right now — wait for the next round."
    if p.bet:
        return f"🔒 You already bet {p.bet:,} this round."
    return None


async def _bj_hand_panel(interaction, g: "BlackjackSession", p: BJPlayer, note: str = "") -> None:
    total = _bj_total(p.cards)
    lines = []
    if note:
        lines += [note, ""]
    lines.append(f"🎩 Dealer shows: {_bj_fmt(g.dealer, hide_second=True)}" if g.bj == "play" else f"🎩 Dealer: {_bj_fmt(g.dealer)}")
    lines.append(f"🃏 Your hand: {_bj_fmt(p.cards)} = **{total}**")
    lines.append(f"💰 Bet: {p.bet:,}" + (" (doubled)" if p.doubled else ""))
    view = None
    if p.status == "":
        buttons = [_gb("🃏 Hit", "bj:hit", "green"), _gb("✋ Stand", "bj:stand", "grey")]
        if len(p.cards) == 2 and not p.doubled and _g_avail(p) >= p.bet:
            buttons.append(_gb(f"⏫ Double ({p.bet})", "bj:dbl", "red"))
        view = _gview(*buttons)
    else:
        lines.append({"stand": "✋ You're standing — wait for the dealer.", "bust": "💥 Bust!", "bj": "🂡 Blackjack! Waiting for the dealer."}[p.status])
    await _gpanel(interaction, "\n".join(lines), view)


class BJStakeModal(discord.ui.Modal):
    """Popup for typing your own blackjack bet."""

    def __init__(self, cap: int) -> None:
        super().__init__(title="Custom bet", timeout=300)
        self.amount = discord.ui.TextInput(
            label=f"{PTS_CAP} ({GAMBLIN_MIN_BET}–{cap})", placeholder=str(cap), min_length=1, max_length=7,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        g = GAMBLIN
        p = g.players.get(interaction.user.id) if isinstance(g, BlackjackSession) else None
        if p is None:
            await interaction.response.send_message("🃏 That Blackjack table is closed.", ephemeral=True)
            return
        err = _bj_bet_error(g, p)
        raw = str(self.amount.value).strip().replace(",", "")
        if err is None and not raw.isdigit():
            err = "Numbers only please 🐾"
        if err is None:
            err = _gstake_error(p, int(raw))
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await _bj_confirm_panel(interaction, int(raw))


async def handle_bj_click(interaction: discord.Interaction, custom_id: str) -> None:
    async def say(text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    if not is_allowed_guild(interaction.guild_id):
        await say("This bot is not configured for this server.")
        return
    g = GAMBLIN
    if not isinstance(g, BlackjackSession):
        await say("🃏 That Blackjack table is closed — catch the next one! 🐾")
        return
    parts = custom_id.split(":")
    action = parts[1] if len(parts) > 1 else ""
    uid = interaction.user.id

    def try_join() -> str | None:
        """Add the clicker to the table. Returns an error text, or None on success."""
        if uid in g.players:
            return None
        if frozen_block(uid):
            return frozen_block(uid)
        if len(g.players) >= GAMBLIN_MAX_PLAYERS:
            return "🃏 The table is full!"
        if POINTS.points(uid) < GAMBLIN_MIN_BET:
            return f"You need at least {pfmt(GAMBLIN_MIN_BET)} to play (you have {POINTS.points(uid)}). Win some in `/slots`!"
        g.players[uid] = BJPlayer(uid)
        return None

    if action == "join":
        if g.phase != "lobby":
            await say("🃏 Already started — tap **Place bet** on the round message to sit down!")
        elif uid in g.players:
            await say("You're already in! 🃏")
        else:
            err = try_join()
            if err:
                await say(err)
            else:
                await interaction.response.send_message(
                    f"🃏 <@{uid}> is participating! ({len(g.players)} in)",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        return

    if action == "bet":
        if g.bj != "bet" or not g.window_open:
            await say("⏳ Bets are closed right now — wait for the next round.")
            return
        err = try_join()
        if err:
            await say(err)
            return
        p = g.players[uid]
        err = _bj_bet_error(g, p)
        if err:
            await say(err)
            return
        await _bj_stake_panel(interaction, p)
        return

    p = g.players.get(uid)
    if p is None:
        await say("🃏 You're not at this table — tap **Place bet** during the betting window to sit down!")
        return

    if action in ("conf", "lock", "cust"):
        err = _bj_bet_error(g, p)
        if err:
            await say(err)
            return
        if action == "cust":
            cap = min(_g_avail(p), GAMBLIN_MAX_BET)
            if cap < GAMBLIN_MIN_BET:
                await say(f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet.")
                return
            await interaction.response.send_modal(BJStakeModal(cap))
            return
        if len(parts) != 3 or not parts[2].isdigit():
            await say("That button looks broken — tap **Place bet** again.")
            return
        amt = int(parts[2])
        err = _gstake_error(p, amt)
        if err:
            await say(err)
            return
        if action == "conf":
            await _bj_confirm_panel(interaction, amt)
            return
        p.bet = amt                                   # lock: instant memory work, no awaits
        p.pending += amt
        await _gpanel(interaction, f"🔒 **Locked:** {pfmt(amt)}. Cards are dealt when betting closes — good luck! 🍀")
        return

    if action == "hand":
        if not p.bet:
            await say("🃏 You didn't bet this round — sit down next round!")
            return
        if g.bj not in ("play", "dealer", "done"):
            await say("⏳ Cards haven't been dealt yet.")
            return
        await _bj_hand_panel(interaction, g, p)
        return

    if action in ("hit", "stand", "dbl"):
        if g.bj != "play" or not p.bet or p.status:
            await say("⏳ That hand is already finished.")
            return
        note = ""
        if action == "stand":
            p.status = "stand"
            note = "✋ You stand."
        elif action == "hit":
            p.cards.append(g.shoe.pop())
            total = _bj_total(p.cards)
            if total > 21:
                p.status, note = "bust", f"💥 You drew {_bj_fmt(p.cards[-1:])} — bust!"
            elif total == 21:
                p.status, note = "stand", "🎯 21! You stand automatically."
            else:
                note = f"You drew {_bj_fmt(p.cards[-1:])}."
        else:  # double down
            if len(p.cards) != 2 or p.doubled:
                await say("⏫ You can only double on your first two cards.")
                return
            if _g_avail(p) < p.bet:
                await say(f"⏫ You need {p.bet:,} more free {PTS_NAME} to double.")
                return
            extra = p.bet
            p.pending += extra
            p.bet += extra
            p.doubled = True
            p.cards.append(g.shoe.pop())
            total = _bj_total(p.cards)
            p.status = "bust" if total > 21 else "stand"
            note = f"⏫ Doubled! You drew {_bj_fmt(p.cards[-1:])}" + (" — bust!" if total > 21 else ".")
        g.dirty = True
        await _bj_hand_panel(interaction, g, p, note)
        return

    await say("That button looks broken — tap **My hand** again.")



# ---------------------------------------------------------------------------
# 🦆 DUCK RACE + 🎡 ROULETTE + the single ?gamblin game picker
#
#   ?gamblin            (Manager only) -> pick a game with buttons -> lobby -> play
#   ?gamblin duck       (shortcut: slots / bj / duck / roulette)
#   ?gamblin stop       ends whatever is running (any game)
#
# All four games share the same engine: a lobby with an @everyone ping, rounds
# with private betting panels, stakes held until each result lands, a pause on
# /slots + /ask + /grouptrivia + autosave, and ONE batched save at the end.
# ---------------------------------------------------------------------------
GAMBLIN_DUCK_ROUNDS = _env_int("GAMBLIN_DUCK_ROUNDS", 3)
GAMBLIN_DUCK_BET_SECONDS = _env_int("GAMBLIN_DUCK_BET_SECONDS", 30)
GAMBLIN_RL_ROUNDS = _env_int("GAMBLIN_RL_ROUNDS", 4)
GAMBLIN_RL_BET_SECONDS = _env_int("GAMBLIN_RL_BET_SECONDS", 40)
RL_MAX_BETS = 5                      # most bets one player can place per spin
DK_PREFIX = "dk:"
RL_PREFIX = "rl:"


async def _stake_panel(interaction, p: GPlayer, header: str, conf_id, cust_id: str, back_id: str | None = None) -> None:
    """The shared 'how much?' panel: 5% / 10% / 25% / All-in-or-Max / Custom popup."""
    avail = _g_avail(p)
    cap = min(avail, GAMBLIN_MAX_BET)
    if cap < GAMBLIN_MIN_BET:
        await _gpanel(interaction, f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet (you have {avail:,}).")
        return
    seen: set[int] = set()
    buttons = []
    for pct in (5, 10, 25):
        amt = min(cap, max(GAMBLIN_MIN_BET, avail * pct // 100))
        if amt not in seen:
            seen.add(amt)
            buttons.append(_gb(f"{pct}% · {amt}", conf_id(amt), "grey"))
    if cap not in seen:
        label = "All-in" if cap == avail else "Max bet"
        buttons.append(_gb(f"{label} · {cap}", conf_id(cap), "red"))
    buttons.append(_gb("✏️ Custom", cust_id, "blue"))
    if back_id:
        buttons.append(_gb("← Back", back_id, "grey"))
    await _gpanel(
        interaction,
        f"{header}\nYou have {pcompact(avail, True)} free (max bet {GAMBLIN_MAX_BET}). How much?",
        _gview(*buttons),
    )


def _join_table(g: GamblinSession, uid: int, player_cls, emoji: str) -> str | None:
    """Add a person to a running table. Returns an error text, or None on success."""
    if uid in g.players:
        return None
    if frozen_block(uid):
        return frozen_block(uid)
    if len(g.players) >= GAMBLIN_MAX_PLAYERS:
        return f"{emoji} The table is full!"
    if POINTS.points(uid) < GAMBLIN_MIN_BET:
        return f"You need at least {pfmt(GAMBLIN_MIN_BET)} to play (you have {POINTS.points(uid)}). Win some in `/slots`!"
    g.players[uid] = player_cls(uid)
    return None


# =========================================================================
# 🦆 DUCK RACE
# =========================================================================
DUCKS = [("🟥", "Quackers"), ("🟦", "Sir Waddles"), ("🟩", "Puddles"),
         ("🟨", "Drake"), ("🟪", "Beaky"), ("🟧", "Feathers")]
DUCK_WEIGHTS = [28, 22, 18, 14, 10, 8]       # the win chances; shuffled between ducks every race
DUCK_TRACK = 16


@dataclass
class DKPlayer(GPlayer):
    bet: tuple | None = None                  # (duck index, stake)

    def reset_round(self) -> None:
        super().reset_round()
        self.bet = None


class DuckRaceSession(GamblinSession):
    NAME = "Duck Race"
    EMOJI = "🦆"
    JOIN_ID = "dk:join"

    def __init__(self, channel, started_by: int) -> None:
        super().__init__(channel, started_by)
        self.dk = ""                          # "" -> "bet" -> "race" -> "done"
        self.probs: list[float] = []
        self.mults: list[float] = []
        self.empty_rounds = 0
        self.stop_early = False

    @property
    def total_rounds(self) -> int:
        return GAMBLIN_DUCK_ROUNDS

    def _lobby_embed(self, end_ts: int) -> discord.Embed:
        return discord.Embed(
            title="🦆 DUCK RACE is starting!",
            description=(
                "**🟥🟦🟩🟨🟪🟧 — pick your duck!**\n"
                f"Press **Join** to play with your {PTS_NAME}. Starts <t:{end_ts}:R>.\n\n"
                f"• {GAMBLIN_DUCK_ROUNDS} races · bets {GAMBLIN_MIN_BET}–{pfmt(GAMBLIN_MAX_BET)}\n"
                "• 6 ducks with different odds — the long shots pay the most\n"
                "• Your winnings count towards the **leaderboard**\n"
                "• Everything is buttons — no typing needed\n"
                "• `/slots` and `/ask` are paused until it's over"
            ),
            color=discord.Color.yellow(),
        )

    def _lineup(self) -> str:
        return "\n".join(
            f"{DUCKS[i][0]} **{DUCKS[i][1]}** — pays **{_fx(self.mults[i])}** · {self.probs[i] * 100:.0f}% to win"
            for i in range(6)
        )

    def _race_embed(self, status: str, pos: list[int] | None = None) -> discord.Embed:
        e = discord.Embed(title=f"🦆 Duck Race — Race {self.round}/{GAMBLIN_DUCK_ROUNDS}", color=discord.Color.yellow())
        lines = [status, ""]
        if pos is None:
            lines.append(self._lineup())
        else:
            for i in range(6):
                lines.append(f"{DUCKS[i][0]} {'·' * pos[i]}🦆{'·' * (DUCK_TRACK - pos[i])}🏁")
        e.description = "\n".join(lines)
        e.set_footer(text=f"{len(self.players)} players · bets {GAMBLIN_MIN_BET}–{pamt(GAMBLIN_MAX_BET)} · one bet per race")
        return e

    def _finish_order(self) -> list[int]:
        """Finishing order, drawn so that each duck's chance of WINNING is exactly its stated chance."""
        remaining = list(range(6))
        order: list[int] = []
        while remaining:
            pick = random.choices(remaining, weights=[self.probs[i] for i in remaining])[0]
            order.append(pick)
            remaining.remove(pick)
        return order

    async def _play_round(self, r: int) -> None:
        self.round = r
        self.phase = "betting"
        self.dk = "bet"
        weights = DUCK_WEIGHTS[:]
        random.shuffle(weights)                           # a different line-up of odds every race
        self.probs = [w / sum(weights) for w in weights]
        self.mults = [_g_mult(p) for p in self.probs]
        for p in self.players.values():
            p.reset_round()

        secs = GAMBLIN_DUCK_BET_SECONDS
        self.deadline = time.time() + secs
        self.msg = await self._send(
            embed=self._race_embed(f"💰 **Place your bets!** Closing <t:{int(self.deadline)}:R>"),
            view=_gview(_gb("💰 Place bet", "dk:bet", "green")),
        )
        if self.msg is None:
            raise RuntimeError("couldn't post the race message")
        self.window_open = True
        await asyncio.sleep(secs)
        self.window_open = False                          # sync from here: no late bets

        bettors = [p for p in self.players.values() if p.bet]
        if not bettors:
            self.empty_rounds += 1
            self.dk = "done"
            note = "Nobody bet on this race."
            if self.empty_rounds >= 2:
                self.stop_early = True
                note += " Two quiet races in a row, so the track is closing."
            await self._edit(self.msg, embed=self._race_embed(note), view=None)
            return
        self.empty_rounds = 0

        # the race
        self.dk = "race"
        order = self._finish_order()
        winner = order[0]
        ft = {d: 10 + rank + random.random() * 0.5 for rank, d in enumerate(order)}
        pos = [0] * 6
        for t in range(1, math.ceil(ft[winner]) + 2):
            for d in range(6):
                if t >= ft[d]:
                    new = DUCK_TRACK
                else:
                    new = min(DUCK_TRACK - 1, max(0, int(DUCK_TRACK * t / ft[d] + random.uniform(-1, 1))))
                pos[d] = max(pos[d], new)
            await self._edit(self.msg, embed=self._race_embed("🏁 **And they're off!**", pos), view=None)
            await asyncio.sleep(1.3)

        # results
        lines = []
        for p in bettors:
            duck, stake = p.bet
            if duck == winner:
                profit = self._settle(p, stake, math.floor(stake * self.mults[duck]))
                lines.append(f"✅ <@{p.uid}> {DUCKS[duck][0]} bet **{stake:,}** → **+{profit:,}**")
            else:
                self._settle(p, stake, 0)
                lines.append(f"❌ <@{p.uid}> {DUCKS[duck][0]} bet **{stake:,}** → −{stake:,}")
        self.dk = "done"
        medals = ["🥇", "🥈", "🥉"]
        podium = "  ".join(f"{medals[i]} {DUCKS[order[i]][0]} {DUCKS[order[i]][1]}" for i in range(3))
        e = self._race_embed(f"🏆 **{DUCKS[winner][0]} {DUCKS[winner][1]} wins!**\n{podium}", pos)
        e.add_field(name="Results", value="\n".join(lines)[:1000], inline=False)
        await self._edit(self.msg, embed=e, view=None)

    async def _finish(self) -> None:
        await _standings(self, "🦆 Duck Race is over!", discord.Color.yellow())


async def _standings(g: GamblinSession, title: str, color) -> None:
    g.phase = "ending"
    ranked = sorted(g.players.values(), key=lambda q: (-q.net, q.uid))
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{p.uid}> — **{p.net:+,}** (balance {POINTS.points(p.uid):,})"
        + (f" · 🏆 +{p.net:,} to leaderboard" if p.net > 0 else "")
        for i, p in enumerate(ranked)
    ]
    embed = discord.Embed(
        title=title,
        description="\n".join(lines)[:4000] + f"\n\n💾 Saving everyone's {PTS_NAME}… `/slots` and `/ask` are back!",
        color=color,
    )
    await g._send(embed=embed)


def _dk_bet_error(g: "DuckRaceSession", p: DKPlayer) -> str | None:
    if g.dk != "bet" or not g.window_open:
        return "⏳ Bets are closed right now — wait for the next race."
    if p.bet:
        d, stake = p.bet
        return f"🔒 You already bet {stake:,} on {DUCKS[d][0]} {DUCKS[d][1]} this race."
    return None


async def _dk_confirm_panel(interaction, g: "DuckRaceSession", duck: int, amt: int) -> None:
    m = g.mults[duck]
    payout = math.floor(amt * m)
    text = (
        f"Bet **{amt:,}** on {DUCKS[duck][0]} **{DUCKS[duck][1]}** (pays {_fx(m)})?\n"
        f"Win → **{payout:,}** back (+{payout - amt:,}) · Lose → −{amt:,}\n"
        "⚠️ Once locked, it can't be changed."
    )
    await _gpanel(interaction, text, _gview(
        _gb(f"🔒 Lock it in ({amt})", f"dk:lock:{duck}:{amt}", "green"),
        _gb("← Back", f"dk:pick:{duck}", "grey"),
    ))


class DKStakeModal(discord.ui.Modal):
    def __init__(self, duck: int, cap: int) -> None:
        super().__init__(title="Custom bet", timeout=300)
        self.duck = duck
        self.amount = discord.ui.TextInput(
            label=f"{PTS_CAP} ({GAMBLIN_MIN_BET}–{cap})", placeholder=str(cap), min_length=1, max_length=7,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        g = GAMBLIN
        p = g.players.get(interaction.user.id) if isinstance(g, DuckRaceSession) else None
        if p is None:
            await interaction.response.send_message("🦆 That race is over.", ephemeral=True)
            return
        err = _dk_bet_error(g, p)
        raw = str(self.amount.value).strip().replace(",", "")
        if err is None and not raw.isdigit():
            err = "Numbers only please 🐾"
        if err is None:
            err = _gstake_error(p, int(raw))
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await _dk_confirm_panel(interaction, g, self.duck, int(raw))


async def handle_dk_click(interaction: discord.Interaction, custom_id: str) -> None:
    async def say(text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    if not is_allowed_guild(interaction.guild_id):
        await say("This bot is not configured for this server.")
        return
    g = GAMBLIN
    if not isinstance(g, DuckRaceSession):
        await say("🦆 That race is over — catch the next one! 🐾")
        return
    parts = custom_id.split(":")
    action = parts[1] if len(parts) > 1 else ""
    uid = interaction.user.id

    if action == "join":
        if g.phase != "lobby":
            await say("🦆 The race already started — tap **Place bet** on the race message to join in!")
        elif uid in g.players:
            await say("You're already in! 🦆")
        else:
            err = _join_table(g, uid, DKPlayer, "🦆")
            if err:
                await say(err)
            else:
                await interaction.response.send_message(
                    f"🦆 <@{uid}> is participating! ({len(g.players)} in)",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        return

    if action == "bet":
        if g.dk != "bet" or not g.window_open:
            await say("⏳ Bets are closed right now — wait for the next race.")
            return
        err = _join_table(g, uid, DKPlayer, "🦆")
        if err:
            await say(err)
            return
        p = g.players[uid]
        err = _dk_bet_error(g, p)
        if err:
            await say(err)
            return
        buttons = [_gb(f"{DUCKS[i][0]} {DUCKS[i][1]} {_fx(g.mults[i])}", f"dk:pick:{i}", "grey") for i in range(6)]
        await _gpanel(interaction, "🦆 **Pick your duck** (one bet per race):\n" + g._lineup(), _gview(*buttons))
        return

    p = g.players.get(uid)
    if p is None:
        await say("🦆 You're not in this race — tap **Place bet** during the betting window!")
        return
    if action not in ("pick", "conf", "lock", "cust") or len(parts) < 3 or not parts[2].isdigit() or int(parts[2]) > 5:
        await say("That button looks broken — tap **Place bet** again.")
        return
    duck = int(parts[2])
    err = _dk_bet_error(g, p)
    if err:
        await say(err)
        return

    if action == "pick":
        await _stake_panel(
            interaction, p,
            f"{DUCKS[duck][0]} **{DUCKS[duck][1]}** — pays **{_fx(g.mults[duck])}** · {g.probs[duck] * 100:.0f}% to win",
            lambda amt: f"dk:conf:{duck}:{amt}", f"dk:cust:{duck}", "dk:bet",
        )
        return
    if action == "cust":
        cap = min(_g_avail(p), GAMBLIN_MAX_BET)
        if cap < GAMBLIN_MIN_BET:
            await say(f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet.")
            return
        await interaction.response.send_modal(DKStakeModal(duck, cap))
        return
    if len(parts) != 4 or not parts[3].isdigit():
        await say("That button looks broken — tap **Place bet** again.")
        return
    amt = int(parts[3])
    err = _gstake_error(p, amt)
    if err:
        await say(err)
        return
    if action == "conf":
        await _dk_confirm_panel(interaction, g, duck, amt)
        return
    p.bet = (duck, amt)                                   # lock: instant memory work, no awaits
    p.pending += amt
    await _gpanel(interaction, f"🔒 **Locked:** {pfmt(amt)} on {DUCKS[duck][0]} **{DUCKS[duck][1]}** (pays {_fx(g.mults[duck])}). Good luck! 🍀")


# =========================================================================
# 🎡 ROULETTE  (European wheel: one green 0, numbers 0-36, real odds)
# =========================================================================
RL_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def _rl_label(n: int) -> str:
    return f"{n} {'🟢' if n == 0 else '🔴' if n in RL_RED else '⚫'}"


# target key -> (label, wins(n), multiplier, how many of the 37 numbers win)
RL_BETS: dict[str, tuple[str, object, int, int]] = {
    "red": ("🔴 Red", lambda n: n in RL_RED, 2, 18),
    "blk": ("⚫ Black", lambda n: n != 0 and n not in RL_RED, 2, 18),
    "odd": ("Odd", lambda n: n != 0 and n % 2 == 1, 2, 18),
    "even": ("Even", lambda n: n != 0 and n % 2 == 0, 2, 18),
    "low": ("1–18", lambda n: 1 <= n <= 18, 2, 18),
    "high": ("19–36", lambda n: 19 <= n <= 36, 2, 18),
    "d1": ("1st dozen (1–12)", lambda n: 1 <= n <= 12, 3, 12),
    "d2": ("2nd dozen (13–24)", lambda n: 13 <= n <= 24, 3, 12),
    "d3": ("3rd dozen (25–36)", lambda n: 25 <= n <= 36, 3, 12),
}


def _rl_target(t: str):
    """(label, wins(n), multiplier, winning-numbers-out-of-37) or None for a junk id."""
    if t in RL_BETS:
        return RL_BETS[t]
    if t.startswith("n") and t[1:].isdigit() and 0 <= int(t[1:]) <= 36:
        k = int(t[1:])
        return (f"Number {_rl_label(k)}", (lambda n, k=k: n == k), 36, 1)
    return None


@dataclass
class RLPlayer(GPlayer):
    bets: list = field(default_factory=list)      # [(target, stake), ...]

    def reset_round(self) -> None:
        super().reset_round()
        self.bets = []


class RouletteSession(GamblinSession):
    NAME = "Roulette"
    EMOJI = "🎡"
    JOIN_ID = "rl:join"

    def __init__(self, channel, started_by: int) -> None:
        super().__init__(channel, started_by)
        self.rl = ""                          # "" -> "bet" -> "spin" -> "done"
        self.history: list[int] = []
        self.empty_rounds = 0
        self.stop_early = False

    @property
    def total_rounds(self) -> int:
        return GAMBLIN_RL_ROUNDS

    def _lobby_embed(self, end_ts: int) -> discord.Embed:
        return discord.Embed(
            title="🎡 ROULETTE is starting!",
            description=(
                "**Place your bets, ladies and gentlemen!**\n"
                f"Press **Join** to play with your {PTS_NAME}. Starts <t:{end_ts}:R>.\n\n"
                f"• {GAMBLIN_RL_ROUNDS} spins · bets {GAMBLIN_MIN_BET}–{pfmt(GAMBLIN_MAX_BET)} · up to {RL_MAX_BETS} bets a spin\n"
                "• Real European wheel (0–36): Red/Black/Odd/Even/Low/High **x2**, Dozens **x3**, one Number **x36**\n"
                "• Your winnings count towards the **leaderboard**\n"
                "• Everything is buttons — no typing needed\n"
                "• `/slots` and `/ask` are paused until it's over"
            ),
            color=discord.Color.red(),
        )

    def _hist_text(self) -> str:
        return " ".join(_rl_label(n) for n in self.history[-10:][::-1]) or "—"

    def _table_embed(self, status: str, lines: list[str] | None = None) -> discord.Embed:
        e = discord.Embed(title=f"🎡 Roulette — Spin {self.round}/{GAMBLIN_RL_ROUNDS}", color=discord.Color.red())
        e.description = (status + (("\n\n" + "\n".join(lines)) if lines else ""))[:4000]
        e.add_field(name="Last numbers", value=self._hist_text(), inline=False)
        e.set_footer(text=f"{len(self.players)} players · bets {GAMBLIN_MIN_BET}–{pamt(GAMBLIN_MAX_BET)} · max {RL_MAX_BETS} bets a spin")
        return e

    async def _play_round(self, r: int) -> None:
        self.round = r
        self.phase = "betting"
        self.rl = "bet"
        for p in self.players.values():
            p.reset_round()
        secs = GAMBLIN_RL_BET_SECONDS
        self.deadline = time.time() + secs
        self.msg = await self._send(
            embed=self._table_embed(
                f"💰 **Place your bets!** Closing <t:{int(self.deadline)}:R>\n"
                "Press **Place bets** — Red/Black, Odd/Even, Low/High, Dozens, or a single Number."
            ),
            view=_gview(_gb("💰 Place bets", "rl:bet", "green")),
        )
        if self.msg is None:
            raise RuntimeError("couldn't post the table message")
        self.window_open = True
        await asyncio.sleep(secs)
        self.window_open = False                          # sync from here: no late bets

        bettors = [p for p in self.players.values() if p.bets]
        if not bettors:
            self.empty_rounds += 1
            self.rl = "done"
            note = "No bets this spin."
            if self.empty_rounds >= 2:
                self.stop_early = True
                note += " Two quiet spins in a row, so the table is closing."
            await self._edit(self.msg, embed=self._table_embed(note), view=None)
            return
        self.empty_rounds = 0

        self.rl = "spin"
        result = random.randrange(37)
        for _ in range(3):
            await self._edit(self.msg, embed=self._table_embed(f"🎡 **The wheel is spinning…** `{random.randrange(37)}`"), view=None)
            await asyncio.sleep(1.2)

        lines = []
        for p in bettors:
            parts = []
            before = p.round_net
            for t, stake in p.bets:
                label, wins, mult, _ = _rl_target(t)
                if wins(result):
                    self._settle(p, stake, stake * mult)
                    parts.append(f"{label} ✅{stake:,}")
                else:
                    self._settle(p, stake, 0)
                    parts.append(f"{label} ❌{stake:,}")
            net = p.round_net - before
            lines.append(f"<@{p.uid}> **{net:+,}** · " + " · ".join(parts))
        self.history.append(result)
        self.rl = "done"
        status = f"🎡 **The ball lands on {_rl_label(result)}!**"
        await self._edit(self.msg, embed=self._table_embed(status, lines[:25]), view=None)

    async def _finish(self) -> None:
        await _standings(self, "🎡 Roulette is over!", discord.Color.red())


def _rl_bet_error(g: "RouletteSession", p: RLPlayer) -> str | None:
    if g.rl != "bet" or not g.window_open:
        return "⏳ Bets are closed right now — wait for the next spin."
    if len(p.bets) >= RL_MAX_BETS:
        return f"🔒 That's the maximum of {RL_MAX_BETS} bets for this spin."
    return None


def _rl_bets_text(p: RLPlayer) -> str:
    if not p.bets:
        return "No bets yet this spin."
    return "\n".join(f"• {_rl_target(t)[0]} — {pfmt(stake)}" for t, stake in p.bets)


async def _rl_type_panel(interaction, p: RLPlayer) -> None:
    buttons = [_gb(f"{RL_BETS[k][0]} {_fx(RL_BETS[k][2])}", f"rl:pick:{k}", "grey") for k in RL_BETS]
    buttons.append(_gb("🎯 Number x36", "rl:num", "blue"))
    text = (
        "🎡 **What do you want to bet on?**\n"
        f"Your bets ({len(p.bets)}/{RL_MAX_BETS}):\n{_rl_bets_text(p)}"
    )
    await _gpanel(interaction, text, _gview(*buttons))


async def _rl_stake_panel(interaction, p: RLPlayer, t: str) -> None:
    label, _, mult, count = _rl_target(t)
    await _stake_panel(
        interaction, p,
        f"🎡 Bet on **{label}** — pays **{_fx(mult)}** · {count}/37 chance",
        lambda amt: f"rl:conf:{t}:{amt}", f"rl:cust:{t}", "rl:bet",
    )


async def _rl_confirm_panel(interaction, t: str, amt: int) -> None:
    label, _, mult, count = _rl_target(t)
    text = (
        f"Bet **{amt:,}** on **{label}** (pays {_fx(mult)})?\n"
        f"Win → **{amt * mult:,}** back (+{amt * (mult - 1):,}) · Lose → −{amt:,}\n"
        "⚠️ Once locked, it can't be changed."
    )
    await _gpanel(interaction, text, _gview(
        _gb(f"🔒 Lock it in ({amt})", f"rl:lock:{t}:{amt}", "green"),
        _gb("← Back", f"rl:pick:{t}", "grey"),
    ))


class RLStakeModal(discord.ui.Modal):
    def __init__(self, t: str, cap: int) -> None:
        super().__init__(title="Custom bet", timeout=300)
        self.t = t
        self.amount = discord.ui.TextInput(
            label=f"{PTS_CAP} ({GAMBLIN_MIN_BET}–{cap})", placeholder=str(cap), min_length=1, max_length=7,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        g = GAMBLIN
        p = g.players.get(interaction.user.id) if isinstance(g, RouletteSession) else None
        if p is None:
            await interaction.response.send_message("🎡 That table is closed.", ephemeral=True)
            return
        err = _rl_bet_error(g, p)
        raw = str(self.amount.value).strip().replace(",", "")
        if err is None and not raw.isdigit():
            err = "Numbers only please 🐾"
        if err is None:
            err = _gstake_error(p, int(raw))
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await _rl_confirm_panel(interaction, self.t, int(raw))


class RLNumberModal(discord.ui.Modal):
    """Popup to pick the single number (0-36) for a straight-up bet."""

    def __init__(self) -> None:
        super().__init__(title="Bet on a number", timeout=300)
        self.number = discord.ui.TextInput(label="Number (0–36)", placeholder="17", min_length=1, max_length=2)
        self.add_item(self.number)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        g = GAMBLIN
        p = g.players.get(interaction.user.id) if isinstance(g, RouletteSession) else None
        if p is None:
            await interaction.response.send_message("🎡 That table is closed.", ephemeral=True)
            return
        err = _rl_bet_error(g, p)
        raw = str(self.number.value).strip()
        if err is None and not (raw.isdigit() and 0 <= int(raw) <= 36):
            err = "Pick a whole number from 0 to 36."
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await _rl_stake_panel(interaction, p, f"n{int(raw)}")


async def handle_rl_click(interaction: discord.Interaction, custom_id: str) -> None:
    async def say(text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    if not is_allowed_guild(interaction.guild_id):
        await say("This bot is not configured for this server.")
        return
    g = GAMBLIN
    if not isinstance(g, RouletteSession):
        await say("🎡 That table is closed — catch the next one! 🐾")
        return
    parts = custom_id.split(":")
    action = parts[1] if len(parts) > 1 else ""
    uid = interaction.user.id

    if action == "join":
        if g.phase != "lobby":
            await say("🎡 Already started — tap **Place bets** on the table message to join in!")
        elif uid in g.players:
            await say("You're already in! 🎡")
        else:
            err = _join_table(g, uid, RLPlayer, "🎡")
            if err:
                await say(err)
            else:
                await interaction.response.send_message(
                    f"🎡 <@{uid}> is participating! ({len(g.players)} in)",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        return

    if action == "bet":
        if g.rl != "bet" or not g.window_open:
            await say("⏳ Bets are closed right now — wait for the next spin.")
            return
        err = _join_table(g, uid, RLPlayer, "🎡")
        if err:
            await say(err)
            return
        p = g.players[uid]
        err = _rl_bet_error(g, p)
        if err:
            await say(err)
            return
        await _rl_type_panel(interaction, p)
        return

    p = g.players.get(uid)
    if p is None:
        await say("🎡 You're not at this table — tap **Place bets** during the betting window!")
        return
    err = _rl_bet_error(g, p)
    if err:
        await say(err)
        return

    if action == "num":
        await interaction.response.send_modal(RLNumberModal())
        return
    if action not in ("pick", "conf", "lock", "cust") or len(parts) < 3 or _rl_target(parts[2]) is None:
        await say("That button looks broken — tap **Place bets** again.")
        return
    t = parts[2]
    if action == "pick":
        await _rl_stake_panel(interaction, p, t)
        return
    if action == "cust":
        cap = min(_g_avail(p), GAMBLIN_MAX_BET)
        if cap < GAMBLIN_MIN_BET:
            await say(f"You need at least {GAMBLIN_MIN_BET} free {PTS_NAME} to bet.")
            return
        await interaction.response.send_modal(RLStakeModal(t, cap))
        return
    if len(parts) != 4 or not parts[3].isdigit():
        await say("That button looks broken — tap **Place bets** again.")
        return
    amt = int(parts[3])
    err = _gstake_error(p, amt)
    if err:
        await say(err)
        return
    if action == "conf":
        await _rl_confirm_panel(interaction, t, amt)
        return
    p.bets.append((t, amt))                               # lock: instant memory work, no awaits
    p.pending += amt
    more = len(p.bets) < RL_MAX_BETS
    text = f"🔒 **Locked:** {pfmt(amt)} on {_rl_target(t)[0]}.\nYour bets ({len(p.bets)}/{RL_MAX_BETS}):\n{_rl_bets_text(p)}"
    await _gpanel(interaction, text, _gview(_gb("➕ Add another bet", "rl:bet", "blue")) if more else None)


# =========================================================================
# ?gamblin -- ONE command for every game
# =========================================================================
GAMBLIN_GAMES = {
    "slots": ("🎰", "Slots", GamblinSession),
    "bj": ("🃏", "Blackjack", BlackjackSession),
    "duck": ("🦆", "Duck Race", DuckRaceSession),
    "roulette": ("🎡", "Roulette", RouletteSession),
}
_GAMBLIN_ALIASES = {
    "slots": "slots", "slot": "slots", "bj": "bj", "blackjack": "bj", "duck": "duck",
    "ducks": "duck", "duckrace": "duck", "roulette": "roulette", "rl": "roulette",
}


async def _gamblin_preflight():
    """Everything that must be true before a game can start. Returns (channel, error_text)."""
    if GAMBLIN is not None:
        return None, f"{GAMBLIN.EMOJI} {GAMBLIN.NAME} is already running. Use `?gamblin stop` to end it."
    if not GAMBLIN_CHANNEL_ID:
        return None, "⚠️ Set `GAMBLIN_CHANNEL_ID` first so I know where to run Gamblin."
    if not POINTS.ready:
        return None, f"❌ {PTS_CAP} haven't loaded from the database yet. Try again in a minute."
    channel = bot.get_channel(GAMBLIN_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(GAMBLIN_CHANNEL_ID)
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return None, "❌ I can't find the Gamblin channel — check `GAMBLIN_CHANNEL_ID` and my permissions there."
    return channel, None


def _clear_gamblin(session) -> None:
    """Runs whenever a game's task ends, however it ended. Guarantees the 'Gamblin is
    running' pause can never get stuck on (e.g. if a game is stopped before it began)."""
    global GAMBLIN
    if GAMBLIN is session:
        GAMBLIN = None


async def _start_gamblin(kind: str, manager_id: int):
    """Start a game. Returns (channel, error_text)."""
    global GAMBLIN
    channel, err = await _gamblin_preflight()
    if err:
        return None, err
    emoji, name, cls = GAMBLIN_GAMES[kind]
    GAMBLIN = cls(channel, manager_id)
    GAMBLIN.task = asyncio.create_task(GAMBLIN.run())
    _background_tasks.add(GAMBLIN.task)
    GAMBLIN.task.add_done_callback(_background_tasks.discard)
    GAMBLIN.task.add_done_callback(lambda _t, _s=GAMBLIN: _clear_gamblin(_s))   # safety net: the flag ALWAYS clears
    await log_event(f"{emoji} {name} started by <@{manager_id}> in {channel.mention}.")
    return channel, None


class GamblinPicker(discord.ui.View):
    """'Which game?' -- only the manager who typed ?gamblin can press it."""

    def __init__(self, owner_id: int, when_ts: int | None = None) -> None:
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.when_ts = when_ts                      # set -> schedule the pick instead of starting it now
        self.message: discord.Message | None = None
        for key, (emoji, name, _) in GAMBLIN_GAMES.items():
            button = discord.ui.Button(label=name, emoji=emoji, style=discord.ButtonStyle.success)
            button.callback = self._make_callback(key)
            self.add_item(button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the manager who typed this can pick 🐾", ephemeral=True)
            return False
        return True

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.stop()
            emoji, name, _ = GAMBLIN_GAMES[key]
            if self.when_ts:
                await interaction.response.edit_message(content=f"🗓️ Scheduling **{name}**…", view=None)
                await interaction.edit_original_response(content=await _schedule_gamblin(key, self.when_ts, interaction.user.id))
                await asyncio.sleep(20)
                try:
                    await interaction.delete_original_response()
                except discord.HTTPException:
                    pass
                return
            await interaction.response.edit_message(content=f"{emoji} Starting **{name}**…", view=None)
            channel, err = await _start_gamblin(key, interaction.user.id)
            if err:
                await interaction.edit_original_response(content=err)
            else:
                await interaction.edit_original_response(content=f"{emoji} **{name}** is starting in {channel.mention}!")
            await asyncio.sleep(10)
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        return callback

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)
        await asyncio.sleep(3)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


# ---- hidden Manager-only command: ?gamblin [game | stop] ---------------------------------
async def _handle_gamblin_command(message: discord.Message) -> None:
    if not MANAGER_ROLE_ID:
        return
    if message.guild is None or not isinstance(message.author, discord.Member):
        return
    if MANAGER_ROLE_ID not in {role.id for role in message.author.roles}:
        return  # not authorised -- stay silent, like ?send / ?rolemenu / ?updateDB
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    async def reply(text: str) -> None:
        await message.channel.send(text, delete_after=30, allowed_mentions=discord.AllowedMentions.none())

    words = message.content.strip().lower().split()
    arg = words[1] if len(words) > 1 else ""

    if arg in ("stop", "end", "cancel"):
        if GAMBLIN is None:
            await reply("🎲 Nothing is running.")
        else:
            GAMBLIN.task.cancel()
            await reply(f"{GAMBLIN.EMOJI} Ending {GAMBLIN.NAME}… unresolved bets won't be charged.")
        return

    if arg == "schedule":
        await _handle_schedule(message, words[2:], reply)
        return

    if arg:
        kind = _GAMBLIN_ALIASES.get(arg)
        if kind is None:
            await reply("Games: `slots`, `bj`, `duck`, `roulette` — or just `?gamblin` to pick with buttons. `?gamblin stop` ends any game.")
            return
        channel, err = await _start_gamblin(kind, message.author.id)
        await reply(err or f"{GAMBLIN_GAMES[kind][0]} {GAMBLIN_GAMES[kind][1]} is starting in {channel.mention}!")
        return

    _, err = await _gamblin_preflight()
    if err:
        await reply(err)
        return
    view = GamblinPicker(message.author.id)
    view.message = await message.channel.send("🎲 **Which Gamblin game?** (pick one — I'll ping everyone and start)", view=view)



# ---------------------------------------------------------------------------
# 🎁 /daily  ·  💸 /pay  ·  📊 /gamblinstats
# ---------------------------------------------------------------------------
DAILY_BASE = _env_int("DAILY_BASE", 15)                  # day-1 reward
DAILY_STREAK_BONUS = _env_int("DAILY_STREAK_BONUS", 5)   # extra per streak day...
DAILY_STREAK_CAP = _env_int("DAILY_STREAK_CAP", 7)       # ...up to this many days
DAILY_WEEK_BONUS = _env_int("DAILY_WEEK_BONUS", 50)      # extra on every 7th day in a row
PAY_DAILY_CAP = _env_int("PAY_DAILY_CAP", 200)           # most points one person can /pay per day
try:                                                      # a fee (in %) on every /pay that is BURNED; 0 turns it off
    PAY_FEE_PERCENT = min(50, max(0, int(os.getenv("PAY_FEE_PERCENT", "5").strip() or 5)))
except ValueError:
    PAY_FEE_PERCENT = 5


def pay_fee(amount: int) -> int:
    """The fee on a /pay, rounded UP, at least 1 when the fee is on. The sender pays amount + fee; the
    receiver gets the full amount; the fee just disappears (it's burned, which shrinks the economy a little)."""
    return 0 if PAY_FEE_PERCENT == 0 else max(1, -(-amount * PAY_FEE_PERCENT // 100))


def _daily_reward(streak: int) -> int:
    reward = DAILY_BASE + DAILY_STREAK_BONUS * (min(streak, DAILY_STREAK_CAP) - 1)
    if streak % 7 == 0:
        reward += DAILY_WEEK_BONUS
    return reward


def _day_before(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


@bot.tree.command(name="daily", description=f"Claim your daily {PTS_NAME} — keep your streak going for bigger rewards!")
async def daily_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return
    uid = interaction.user.id
    blocked = frozen_block(uid)
    if blocked:
        await interaction.response.send_message(blocked, ephemeral=True)
        return

    day, secs = _day_info()
    last, streak = POINTS.daily_state(uid)
    if last >= day:                                           # instant memory check, no awaits before the claim
        await interaction.response.send_message(
            f"🎁 You already claimed today! Streak 🔥 **{streak}**. Come back in {_format_wait(secs)} "
            f"for **+{pfmt(_daily_reward(streak + 1))}**.",
            ephemeral=True,
        )
        return
    new_streak = streak + 1 if last == _day_before(day) else 1
    reward = _daily_reward(new_streak)
    POINTS.claim_daily(uid, day, new_streak, reward)

    lost = streak > 1 and new_streak == 1
    days_to_week = 7 - (new_streak % 7)
    tomorrow = (f"**+{pfmt(_daily_reward(new_streak + 1))}**"
                + (f" · a **+{DAILY_WEEK_BONUS} bonus** in {days_to_week} day{'s' if days_to_week != 1 else ''}!" if days_to_week else ""))
    embed = _render_daily(style_of(uid, "daily"), interaction.user.mention, reward, new_streak, lost, streak, tomorrow,
                          f"Balance: {pamt(POINTS.points(uid))} · Miss a day and the streak resets")
    await interaction.response.send_message(embed=embed)


def _pay_error(sender_id: int, recipient: discord.abc.User, amount: int) -> str | None:
    if not POINTS.ready:
        return _POINTS_UNAVAILABLE
    blocked = gamblin_block_text("`/pay`")
    if blocked:
        return blocked
    for uid in (sender_id, recipient.id):
        if uid in POINTS.frozen:
            return (f"🧊 Your {PTS_NAME} are frozen, so you can't send {PTS_NAME} right now." if uid == sender_id
                    else f"🧊 That person's {PTS_NAME} are frozen, so they can't receive {PTS_NAME} right now.")
    if recipient.id == sender_id:
        return "You can't pay yourself 🐾"
    if recipient.bot:
        return f"Bots don't need {PTS_NAME} 🐾"
    if amount < 1:
        return f"Send at least {pamt(1)}."
    day, secs = _day_info()
    sent = POINTS.sent_today(sender_id, day)
    if sent + amount > PAY_DAILY_CAP:
        left = max(0, PAY_DAILY_CAP - sent)
        return (f"💸 Daily limit: you can send {pcompact(PAY_DAILY_CAP, True)} a day and you've sent **{sent}**. "
                f"**{left}** left (resets in {_format_wait(secs)}).")
    fee = pay_fee(amount)
    if POINTS.points(sender_id) < amount + fee:
        if fee:
            return (f"You need {pfmt(amount + fee)} to send {pamt(amount)} (that includes a {pamt(fee)} fee) "
                    f"and you have {pfmt(POINTS.points(sender_id))}.")
        return f"You only have {pfmt(POINTS.points(sender_id))}."
    return None


class PayConfirmView(discord.ui.View):
    """Private 'send it?' step so a typo can't send points to the wrong person."""

    def __init__(self, sender: discord.abc.User, recipient: discord.Member, amount: int) -> None:
        super().__init__(timeout=30)
        self.sender, self.recipient, self.amount = sender, recipient, amount
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.sender.id:
            await interaction.response.send_message("This isn't your payment 🐾", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Send", emoji="💸", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            await interaction.response.send_message("Already handled!", ephemeral=True)
            return
        self.used = True
        self.stop()
        err = _pay_error(self.sender.id, self.recipient, self.amount)      # re-check: things may have changed
        if err:
            await interaction.response.edit_message(content=err, view=None)
            return
        day = _day_info()[0]
        fee = pay_fee(self.amount)
        POINTS.record_pay(self.sender.id, self.recipient.id, self.amount, day, fee)
        await interaction.response.edit_message(
            content=f"✅ Sent {pfmt(self.amount)} to {self.recipient.display_name}."
                    + (f" (fee: {pamt(fee)})" if fee else ""), view=None
        )
        try:
            await interaction.followup.send(
                f"💸 {self.sender.mention} sent {pcompact(self.amount, True)} to {self.recipient.mention}!",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            logger.warning("Couldn't announce a /pay (the points were still sent).")
        await log_event(f"💸 `/pay` <@{self.sender.id}> → <@{self.recipient.id}> **{self.amount:,}** "
                        f"(+{fee:,} fee burned, {POINTS.sent_today(self.sender.id, day)}/{PAY_DAILY_CAP} sent today)")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing sent. 🐾", view=None)


@bot.tree.command(name="pay", description=f"Send some of your {PTS_NAME} to another member (daily limit applies).")
@app_commands.describe(user=f"Who gets the {PTS_NAME}", amount=f"How many {PTS_NAME} to send")
async def pay_slash(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    err = _pay_error(interaction.user.id, user, amount)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    day = _day_info()[0]
    left = PAY_DAILY_CAP - POINTS.sent_today(interaction.user.id, day)
    view = PayConfirmView(interaction.user, user, amount)
    fee = pay_fee(amount)
    cost = (f"A **{PAY_FEE_PERCENT}%** fee ({pamt(fee)}) is burned, so you pay **{pamt(amount + fee)}** and they get the full amount.\n"
            if fee else "")
    await interaction.response.send_message(
        f"💸 Send {pcompact(amount, True)} to **{user.display_name}**?\n{cost}"
        f"You have {POINTS.points(interaction.user.id):,} · you can still send {left:,} today (limit {PAY_DAILY_CAP}).",
        view=view, ephemeral=True,
    )


@bot.tree.command(name="gamblinstats", description="See your Gamblin record and the hall of fame.")
@app_commands.describe(user="Whose stats to show (defaults to you)")
async def gamblinstats_slash(interaction: discord.Interaction, user: discord.Member | None = None):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return
    target = user or interaction.user
    st = POINTS.stats.get(target.id)
    embed = discord.Embed(title=f"🎲 {target.display_name}'s Gamblin record", color=discord.Color.gold())
    if not st:
        embed.description = "No Gamblin games yet — join the next one when a manager starts it!"
    else:
        fav = max(st["gm"].items(), key=lambda kv: kv[1])[0] if st.get("gm") else "—"
        embed.add_field(name="Games played", value=f"{st['s']:,}")
        embed.add_field(name="Total profit", value=f"{st['net']:+,}")
        embed.add_field(name=f"{PTS_CAP} wagered", value=f"{st['wg']:,}")
        embed.add_field(name="Biggest single win", value=f"+{st['bw']:,}")
        embed.add_field(name="Best game", value=f"+{st['bs']:,}")
        embed.add_field(name="Favourite", value=fav)

    def holder(field: str) -> str:
        best = max(POINTS.stats.items(), key=lambda kv: kv[1].get(field, 0), default=None)
        if best is None or best[1].get(field, 0) <= 0:
            return "—"
        return f"<@{best[0]}> · **{best[1][field]:,}**" if field != "bw" and field != "bs" else f"<@{best[0]}> · **+{best[1][field]:,}**"

    embed.add_field(
        name="🏛️ Hall of fame",
        value=(
            f"🏅 Biggest single win: {holder('bw')}\n"
            f"💎 Best single game: {holder('bs')}\n"
            f"🎲 Most games played: {holder('s')}\n"
            f"📈 Highest total profit: {holder('net')}"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


# ---------------------------------------------------------------------------
# 💾 Backup / restore, 📢 announce, 🗓️ scheduled Gamblin
# ---------------------------------------------------------------------------
def _backup_file(title_day: str | None = None) -> discord.File:
    blob = json.dumps(POINTS.backup_data(), separators=(",", ":")).encode()
    return discord.File(io.BytesIO(blob), filename=f"milo-backup-{title_day or _day_info()[0]}.json")


async def post_backup(title: str) -> bool:
    """Post a backup file to the log channel. Returns False if there's nowhere to post."""
    if not LOG_CHANNEL_ID:
        return False

    async def send() -> None:
        channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        await channel.send(content=title, file=_backup_file(), allowed_mentions=discord.AllowedMentions.none())
    await _retry_discord(send, "the nightly backup")
    return True


_UID_RE = re.compile(r"^\d{1,20}$")
_DAILY_VAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}:\d{1,5}$")


def _validate_backup(raw: bytes) -> tuple[dict | None, str | None]:
    """Parse and sanity-check a backup file. Returns (clean data, None) or (None, why not)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "That file isn't valid JSON."
    if not isinstance(data, dict) or data.get("version") != 1:
        return None, "That doesn't look like a Milo backup (wrong or missing version)."
    clean: dict = {"version": 1, "created": int(data.get("created", 0)) if str(data.get("created", 0)).isdigit() else 0}

    def ints(name: str, lo: int, hi: int):
        block = data.get(name, {})
        if not isinstance(block, dict) or len(block) > 50_000:
            raise ValueError(f"'{name}' is malformed or too large")
        out = {}
        for k, v in block.items():
            if not _UID_RE.match(str(k)) or isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"'{name}' has a bad entry ({str(k)[:20]})")
            out[str(k)] = v
        return out

    try:
        clean["balance"] = ints("balance", 0, 10 ** 9)
        clean["earned"] = ints("earned", 0, 10 ** 10)
        clean["jackpot"] = ints("jackpot", 0, 10 ** 6)
        daily = data.get("daily", {})
        if not isinstance(daily, dict) or len(daily) > 50_000 or any(
            not _UID_RE.match(str(k)) or not isinstance(v, str) or not _DAILY_VAL_RE.match(v) for k, v in daily.items()
        ):
            raise ValueError("'daily' is malformed")
        clean["daily"] = {str(k): v for k, v in daily.items()}
        stats = data.get("stats", {})
        if not isinstance(stats, dict) or len(stats) > 50_000:
            raise ValueError("'stats' is malformed")
        clean_stats = {}
        for k, st in stats.items():
            if not _UID_RE.match(str(k)) or not isinstance(st, dict):
                raise ValueError("'stats' has a bad entry")
            entry = {f: int(st.get(f, 0)) for f in ("s", "net", "bw", "bs", "wg")}
            gm = st.get("gm", {})
            entry["gm"] = {str(g)[:30]: int(n) for g, n in gm.items()} if isinstance(gm, dict) else {}
            clean_stats[str(k)] = entry
        clean["stats"] = clean_stats
        frozen = data.get("frozen", [])
        if not isinstance(frozen, list) or len(frozen) > 10_000 or any(not _UID_RE.match(str(x)) for x in frozen):
            raise ValueError("'frozen' is malformed")
        clean["frozen"] = [int(x) for x in frozen]
        shop = data.get("shop", {})
        if not isinstance(shop, dict) or len(shop) > 50_000:
            raise ValueError("'shop' is malformed")
        clean["shop"] = {}
        for k, st in shop.items():
            if not _UID_RE.match(str(k)):
                raise ValueError("'shop' has a bad entry")
            cleaned = _clean_shop_state(st)
            if cleaned:
                clean["shop"][str(k)] = cleaned
    except (ValueError, TypeError) as exc:
        return None, f"The backup is damaged: {exc}."
    return clean, None


class RestoreView(discord.ui.View):
    """Confirm / Cancel for ?restore. Only the manager who typed it can press."""

    def __init__(self, owner_id: int, data: dict) -> None:
        super().__init__(timeout=60)
        self.owner_id, self.data = owner_id, data
        self.message: discord.Message | None = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the manager who typed this can press it 🐾", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, restore it", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        if GAMBLIN is not None:
            await interaction.response.edit_message(content="🎰 A Gamblin game started in the meantime — try again when it's over.", view=None)
            return
        if not POINTS.ready:
            await interaction.response.edit_message(content=f"❌ {PTS_CAP} aren't loaded yet — try again in a minute.", view=None)
            return
        counts = POINTS.restore_data(self.data)
        await interaction.response.edit_message(content="✅ Restored in memory. Saving to the database…", view=None)
        ok, _, detail = await POINTS.flush("restore")
        if POINTS.store is None:
            result = "⚠️ No database is configured, so this only lasts until Milo restarts."
        elif ok:
            result = "💾 Saved. Safe to redeploy."
        else:
            result = f"⚠️ NOT saved yet ({detail}). Run `?updateDB` before redeploying."
        summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "nothing needed changing"
        await interaction.edit_original_response(content=f"✅ **Backup restored** — {summary}. {result}")
        await log_event(f"♻️ `?restore` by <@{interaction.user.id}> — {summary}. {result}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing changed. 🐾", view=None)

    async def on_timeout(self) -> None:
        if self.message is not None and not self.done:
            try:
                await self.message.edit(content="Timed out — nothing changed.", view=None)
            except discord.HTTPException:
                pass


_ANNOUNCE_RE = re.compile(r"^\?announce\s+(?:(--ping)\s+)?(?:<#(\d+)>\s+)?(?:(--ping)\s+)?(.+)$", re.I | re.S)


async def _do_announce(message: discord.Message, reply) -> None:
    m = _ANNOUNCE_RE.match(message.content.strip())
    if not m:
        await reply("Usage: `?announce Title | your message` · optional: `#channel` and `--ping` (pings @everyone), e.g. `?announce --ping #general Movie night | 8pm in voice!`")
        return
    ping = bool(m.group(1) or m.group(3))
    channel = message.channel
    if m.group(2):
        channel = message.guild.get_channel(int(m.group(2)))
    if not isinstance(channel, discord.TextChannel):
        await reply("❌ I can't post in that channel.")
        return
    body = m.group(4).strip()
    title, _, rest = body.partition("|")
    if rest.strip():
        title, body = title.strip()[:250], rest.strip()
    else:
        title = "📢 Announcement"
    if len(body) > 4000:
        await reply("❌ That's too long for one announcement (4000 characters max).")
        return
    embed = discord.Embed(title=title, description=body, color=discord.Color.blurple())
    embed.set_footer(text=f"Announced by {message.author.display_name}")
    try:
        await channel.send(
            content="@everyone" if ping else None, embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=ping),
        )
    except discord.HTTPException:
        await reply("❌ I couldn't post there — check my permissions in that channel.")
        return
    if channel.id != message.channel.id:
        await reply(f"📢 Posted in {channel.mention}.")
    await log_event(f"📢 `?announce` by <@{message.author.id}> in {channel.mention}: **{title}**")


def _parse_when(text: str, now: datetime | None = None) -> tuple[int | None, str | None]:
    """'in 30m', 'in 1h30m', '8pm', '8:30pm', '20:00' -> (unix timestamp, None) or (None, why not).
    Clock times are in the reset timezone (LIMIT_RESET_TZ) and mean the NEXT time that clock shows."""
    text = text.strip().lower()
    now = (now or datetime.now(_LIMIT_TZ)).astimezone(_LIMIT_TZ)
    hint = "Try `in 30m`, `in 2h`, `8pm`, `8:30pm` or `20:00`."
    rel = re.fullmatch(r"in\s+((?:\d+\s*(?:h|hr|hrs|hours?|m|min|mins|minutes?)\s*)+)", text)
    if rel:
        total = sum(int(n) * (3600 if u == "h" else 60) for n, u in re.findall(r"(\d+)\s*([hm])", rel.group(1)))
        target = now + timedelta(seconds=total)
    else:
        clock = re.fullmatch(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
        if not clock or not (clock.group(2) or clock.group(3)):
            return None, f"I couldn't read that time. {hint}"
        hour, minute, ampm = int(clock.group(1)), int(clock.group(2) or 0), clock.group(3)
        if ampm:
            if not 1 <= hour <= 12:
                return None, f"That hour doesn't work with am/pm. {hint}"
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        if hour > 23 or minute > 59:
            return None, f"That isn't a real time. {hint}"
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now + timedelta(seconds=60):
            target += timedelta(days=1)
    delta = (target - now).total_seconds()
    if delta < 60:
        return None, "Schedule at least a minute ahead."
    if delta > 24 * 3600:
        return None, "I can only schedule up to 24 hours ahead."
    return int(target.timestamp()), None


async def _gamblin_channel() -> discord.TextChannel | None:
    if not GAMBLIN_CHANNEL_ID:
        return None
    channel = bot.get_channel(GAMBLIN_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(GAMBLIN_CHANNEL_ID)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def _schedule_gamblin(kind: str, ts: int, manager_id: int) -> str:
    """Add a scheduled game and announce it. Returns the reply text for the manager."""
    emoji, name, _ = GAMBLIN_GAMES[kind]
    if not POINTS.ready:
        return f"❌ {PTS_CAP} haven't loaded from the database yet. Try again in a minute."
    channel = await _gamblin_channel()
    if channel is None:
        return "❌ I can't find the Gamblin channel — check `GAMBLIN_CHANNEL_ID` and my permissions there."
    if len(POINTS.schedule) >= 5:
        return "❌ There are already 5 games scheduled. Use `?gamblin schedule cancel` to clear them."
    POINTS.set_schedule(sorted(POINTS.schedule + [{"ts": ts, "kind": kind, "by": manager_id}], key=lambda x: x["ts"]))
    embed = discord.Embed(
        title=f"{emoji} {name} Gamblin is coming!",
        description=f"Starts <t:{ts}:F> (<t:{ts}:R>) in this channel.\nBring your {PTS_NAME} — everything is buttons!",
        color=discord.Color.gold(),
    )
    try:
        await channel.send(content="@everyone", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
    except discord.HTTPException:
        logger.warning("Couldn't post the schedule announcement", exc_info=True)
    await log_event(f"🗓️ {emoji} {name} scheduled for <t:{ts}:F> by <@{manager_id}>.")
    return f"🗓️ {emoji} **{name}** scheduled for <t:{ts}:F> (<t:{ts}:R>) and announced in {channel.mention}."


async def _handle_schedule(message: discord.Message, rest: list[str], reply) -> None:
    if not rest:
        await reply(f"Usage: `?gamblin schedule duck 8pm` · `?gamblin schedule 8pm` (pick the game with buttons) · "
                    f"`?gamblin schedule list` · `?gamblin schedule cancel`. Times are in {LIMIT_RESET_TZ}.")
        return
    if rest[0] in ("list", "ls"):
        if not POINTS.schedule:
            await reply("🗓️ Nothing is scheduled.")
        else:
            lines = [f"{GAMBLIN_GAMES[x['kind']][0]} {GAMBLIN_GAMES[x['kind']][1]} — <t:{x['ts']}:F> (<t:{x['ts']}:R>)"
                     for x in POINTS.schedule if x.get("kind") in GAMBLIN_GAMES]
            await reply("🗓️ **Scheduled:**\n" + "\n".join(lines))
        return
    if rest[0] in ("cancel", "clear"):
        n = len(POINTS.schedule)
        if n:
            POINTS.set_schedule([])
            await log_event(f"🗓️ Schedule cleared ({n} game{'s' if n != 1 else ''}) by <@{message.author.id}>.")
        await reply(f"🗓️ Cleared {n} scheduled game{'s' if n != 1 else ''}.")
        return
    kind = _GAMBLIN_ALIASES.get(rest[0])
    ts, err = _parse_when(" ".join(rest[1:] if kind else rest))
    if err:
        await reply(f"⚠️ {err}")
        return
    if kind:
        await reply(await _schedule_gamblin(kind, ts, message.author.id))
        return
    view = GamblinPicker(message.author.id, when_ts=ts)
    view.message = await message.channel.send(
        f"🗓️ **Which game at <t:{ts}:t>?** (pick one — I'll announce it and start it on time)", view=view
    )


async def _run_due_schedule() -> None:
    if not POINTS.schedule:
        return
    now = time.time()
    remaining, changed = [], False
    for item in sorted(POINTS.schedule, key=lambda x: x["ts"]):
        if item["ts"] > now:
            remaining.append(item)
            continue
        kind = item.get("kind")
        if kind not in GAMBLIN_GAMES:
            changed = True
        elif GAMBLIN is None:
            channel, err = await _start_gamblin(kind, int(item.get("by", 0)))
            changed = True
            await log_event(f"🗓️ Scheduled {GAMBLIN_GAMES[kind][1]} " + (f"couldn't start: {err}" if err else "is starting now."))
        elif now - item["ts"] > 600:
            changed = True
            await log_event(f"🗓️ Scheduled {GAMBLIN_GAMES[kind][1]} was skipped — another game was still running 10 minutes later.")
        else:
            remaining.append(item)                         # another game is on: wait for it to finish
    if changed:
        POINTS.set_schedule(remaining)


async def _gamblin_scheduler() -> None:
    """Background task: starts scheduled games when their time comes (checked every 20 seconds)."""
    await bot.wait_until_ready()
    while not POINTS.ready:
        await asyncio.sleep(5)
    while True:
        await asyncio.sleep(20)
        try:
            await _run_due_schedule()
        except Exception:
            logger.exception("Gamblin scheduler tick failed")



# ---------------------------------------------------------------------------
# 🌅 Midnight announcement -- "everything has reset!" in your #general
#
# At 12:00 AM (LIMIT_RESET_TZ, default IST -- the same moment the daily limits
# reset) Milo posts one embed with an @everyone ping. Off unless
# RESET_ANNOUNCE_CHANNEL_ID is set. Memory only: costs 0 Upstash commands.
# ---------------------------------------------------------------------------
_raw_reset_channel = os.getenv("RESET_ANNOUNCE_CHANNEL_ID", "").strip()
RESET_ANNOUNCE_CHANNEL_ID = int(_raw_reset_channel) if _raw_reset_channel.isdigit() else None


def _reset_announce_embed() -> discord.Embed:
    ask = "🤖 `/ask` — your daily limits are fresh again"
    if THINK_TIERS:
        ask += ": " + " · ".join(f"{t.emoji} {t.label} ×{t.user_daily}" for t in THINK_TIERS)
    lines = [
        f"🎁 `/daily` — claim your free {PTS_NAME} and keep your streak going 🔥",
        f"🎰 `/slots` — your **{SLOTS_FREE_SPINS} free spins** are back (bonus packs too)",
        "🎟️ `/lottery` — a fresh draw is open: pick 3 numbers and chase the jackpot",
        ask,
        f"💸 `/pay` — your {pamt(PAY_DAILY_CAP)} sending limit is back",
    ]
    embed = discord.Embed(title="🌅 New day — everything has reset!", description="\n".join(lines), color=discord.Color.gold())
    winners = POINTS.jackpot_winners(_day_before(_day_info()[0]))
    if winners:
        names = " ".join(f"<@{u}>" + (f" ×{n}" if n > 1 else "") for u, n in sorted(winners.items(), key=lambda kv: (-kv[1], kv[0]))[:25])
        embed.add_field(name="💥 Yesterday's 777 winners", value=f"{names}\nCongratulations! 🎉", inline=False)
    embed.set_footer(text=f"Limits reset every day at 12:00 AM ({LIMIT_RESET_TZ})")
    return embed


async def _post_reset_announcement() -> None:
    async def send() -> None:
        channel = bot.get_channel(RESET_ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(RESET_ANNOUNCE_CHANNEL_ID)
        await channel.send(
            content="@everyone", embed=_reset_announce_embed(),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
    if await _retry_discord(send, "the midnight announcement"):
        await log_event("📣 Midnight reset announcement posted.")
    else:
        await log_event("⚠️ I couldn't post the midnight reset announcement — check my permissions in that channel "
                        "(or Discord was blocking this host; see the Render logs).")


async def _daily_reset_announcer() -> None:
    """Background task: posts the moment the day changes. If Milo was offline at
    midnight (a redeploy right then) that day's announcement is simply skipped."""
    await bot.wait_until_ready()
    last = _day_info()[0]
    while True:
        day, secs = _day_info()
        if day != last:
            last = day
            await _post_reset_announcement()
            continue
        await asyncio.sleep(max(0.5, min(secs, 30)))      # wakes right at midnight, not up to a minute late


# ---------------------------------------------------------------------------
# 🎟️ DAILY LOTTERY -- /lottery
#
# Pick 3 numbers from 1-15. Tickets for "today" are drawn at the daily reset (12:00 AM,
# LIMIT_RESET_TZ). Matches: 3 = the (rollover) JACKPOT, split between winners;
# 2 = LOTTERY_PRIZE_2; 1 = your ticket back. Tickets + the jackpot are saved to the
# database; if Milo is offline at midnight the draw happens when it comes back.
# Lottery prizes move balances only (they don't count for the leaderboard).
# ---------------------------------------------------------------------------
def _lottery_nums(nums) -> str:
    return " · ".join(f"{n:02d}" for n in sorted(nums))


def _lottery_chance(k: int) -> float:
    """Chance a single ticket matches exactly k of the 3 drawn numbers."""
    return math.comb(LOTTERY_PICKS, k) * math.comb(LOTTERY_RANGE - LOTTERY_PICKS, LOTTERY_PICKS - k) / math.comb(LOTTERY_RANGE, LOTTERY_PICKS)


def _lottery_embed(uid: int) -> discord.Embed:
    day = _day_info()[0]
    mine = POINTS.tickets.get(day, {}).get(uid, {})
    e = discord.Embed(
        title="🎟️ Daily Lottery",
        description=(
            f"Pick **{LOTTERY_PICKS} numbers from 1–{LOTTERY_RANGE}**. The draw is every night at 12:00 AM ({LIMIT_RESET_TZ}).\n"
            f"A ticket costs {pfmt(LOTTERY_COST)} · up to **{LOTTERY_MAX_TICKETS}** per person per draw."
        ),
        color=discord.Color.gold(),
    )
    e.add_field(name=f"💰 Jackpot ({LOTTERY_PICKS} matches)", inline=False,
                value=f"**{pfmt(POINTS.lottery['carry'])}** — split between winners. Nobody wins? It keeps growing!")
    e.add_field(name="🏅 Other prizes", inline=False,
                value=f"2 matches → **{pfmt(LOTTERY_PRIZE_2)}**\n1 match → your ticket back ({pfmt(LOTTERY_COST)})")
    p1, p2, p3 = (_lottery_chance(k) for k in (1, 2, 3))
    e.add_field(name="📊 Odds per ticket", inline=False,
                value=f"Win something: **{(p1 + p2 + p3) * 100:.0f}%** · 2+ matches: **{(p2 + p3) * 100:.1f}%** · jackpot: **1 in {round(1 / p3):,}**")
    lines = [f"`{_lottery_nums(p)}`" for _, p in sorted(mine.items())]
    e.add_field(name=f"🎫 Your tickets tonight ({len(mine)}/{LOTTERY_MAX_TICKETS})", inline=False,
                value="\n".join(lines) or "None yet — buy one below!")
    e.add_field(name="🧾 Sold tonight", value=f"{POINTS.tickets_sold(day):,} tickets")
    last = POINTS.lottery.get("last")
    if last:
        winners = " ".join(f"<@{u}>" for u in last.get("top", [])) or "nobody"
        e.add_field(name=f"🎱 Last draw ({last['day']})", inline=False,
                    value=f"Numbers: **{_lottery_nums(last['nums'])}** · {last['tickets']:,} tickets\nJackpot: {winners if last['w3'] else 'no winner — it rolled over'} · 2 matches: {last['w2']} · 1 match: {last['w1']}")
    e.set_footer(text=f"Tickets cost {pamt(LOTTERY_COST)} · the jackpot restarts at {pamt(LOTTERY_SEED)} after a win")
    return e


def _lottery_buy_error(uid: int) -> str | None:
    if not POINTS.ready:
        return _POINTS_UNAVAILABLE
    blocked = frozen_block(uid) or gamblin_block_text("`/lottery`")
    if blocked:
        return blocked
    if POINTS.ticket_count(uid, _day_info()[0]) >= LOTTERY_MAX_TICKETS:
        return f"🎟️ You already have {LOTTERY_MAX_TICKETS} tickets for tonight's draw — good luck!"
    if POINTS.points(uid) < LOTTERY_COST:
        return f"🎟️ A ticket costs {pfmt(LOTTERY_COST)} and you have {pamt(POINTS.points(uid))}."
    return None


def _lottery_buy(uid: int, picks) -> None:
    """Instant memory work (no awaits): takes the cost, records the ticket, grows the jackpot."""
    POINTS.buy_ticket(uid, _day_info()[0], tuple(sorted(picks)), LOTTERY_COST, LOTTERY_CUT)


class LotteryMenuView(discord.ui.View):
    """The main /lottery panel: buy a ticket by picking numbers, or take a random one."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own with `/lottery` 🐾", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pick my numbers", emoji="🎟️", style=discord.ButtonStyle.success)
    async def pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        err = _lottery_buy_error(self.owner_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.edit_message(content=f"🎟️ **Pick {LOTTERY_PICKS} numbers** (1–{LOTTERY_RANGE}):", embed=None, view=LotteryPickView(self.owner_id))

    @discord.ui.button(label="Quick pick", emoji="🎲", style=discord.ButtonStyle.primary)
    async def quick(self, interaction: discord.Interaction, button: discord.ui.Button):
        err = _lottery_buy_error(self.owner_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        picks = sorted(random.sample(range(1, LOTTERY_RANGE + 1), LOTTERY_PICKS))
        _lottery_buy(self.owner_id, picks)
        await interaction.response.edit_message(
            content=f"🎲 Quick pick bought: **{_lottery_nums(picks)}** (−{pfmt(LOTTERY_COST)}). Good luck! 🍀",
            embed=_lottery_embed(self.owner_id), view=LotteryMenuView(self.owner_id),
        )


class LotteryPickView(discord.ui.View):
    """15 number buttons: tap exactly 3, then Buy. (Memory only; nothing is charged until Buy.)"""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.picks: set[int] = set()
        self.num_buttons: dict[int, discord.ui.Button] = {}
        for n in range(1, LOTTERY_RANGE + 1):
            b = discord.ui.Button(label=str(n), style=discord.ButtonStyle.secondary, row=(n - 1) // 5)
            b.callback = self._toggle(n)
            self.num_buttons[n] = b
            self.add_item(b)
        self.rand = discord.ui.Button(label="Random", emoji="🎲", style=discord.ButtonStyle.primary, row=3)
        self.rand.callback = self._random
        self.clear = discord.ui.Button(label="Clear", style=discord.ButtonStyle.secondary, row=3)
        self.clear.callback = self._clear
        self.buy = discord.ui.Button(label=f"Buy ({pamt(LOTTERY_COST)})", emoji="✅", style=discord.ButtonStyle.success, row=3, disabled=True)
        self.buy.callback = self._buy
        for b in (self.rand, self.clear, self.buy):
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own with `/lottery` 🐾", ephemeral=True)
            return False
        return True

    def _text(self) -> str:
        chosen = _lottery_nums(self.picks) if self.picks else "—"
        return f"🎟️ **Pick {LOTTERY_PICKS} numbers** (1–{LOTTERY_RANGE}): **{chosen}** ({len(self.picks)}/{LOTTERY_PICKS})"

    def _refresh(self) -> None:
        for n, b in self.num_buttons.items():
            b.style = discord.ButtonStyle.success if n in self.picks else discord.ButtonStyle.secondary
        self.buy.disabled = len(self.picks) != LOTTERY_PICKS

    def _toggle(self, n: int):
        async def callback(interaction: discord.Interaction) -> None:
            if n in self.picks:
                self.picks.discard(n)
            elif len(self.picks) < LOTTERY_PICKS:
                self.picks.add(n)
            else:
                await interaction.response.send_message(f"You already picked {LOTTERY_PICKS} — tap one to unselect it first.", ephemeral=True)
                return
            self._refresh()
            await interaction.response.edit_message(content=self._text(), view=self)
        return callback

    async def _random(self, interaction: discord.Interaction) -> None:
        self.picks = set(random.sample(range(1, LOTTERY_RANGE + 1), LOTTERY_PICKS))
        self._refresh()
        await interaction.response.edit_message(content=self._text(), view=self)

    async def _clear(self, interaction: discord.Interaction) -> None:
        self.picks = set()
        self._refresh()
        await interaction.response.edit_message(content=self._text(), view=self)

    async def _buy(self, interaction: discord.Interaction) -> None:
        err = _lottery_buy_error(self.owner_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if len(self.picks) != LOTTERY_PICKS:
            await interaction.response.send_message(f"Pick exactly {LOTTERY_PICKS} numbers first.", ephemeral=True)
            return
        picks = sorted(self.picks)
        _lottery_buy(self.owner_id, picks)
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ Ticket bought: **{_lottery_nums(picks)}** (−{pfmt(LOTTERY_COST)}). Good luck! 🍀",
            embed=_lottery_embed(self.owner_id), view=LotteryMenuView(self.owner_id),
        )


@bot.tree.command(name="lottery", description="Daily lottery — pick 3 numbers, the draw is at midnight!")
async def lottery_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return
    await interaction.response.send_message(
        embed=_lottery_embed(interaction.user.id), view=LotteryMenuView(interaction.user.id), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _lottery_draw(day: str) -> dict | None:
    """Draw the numbers for one day's tickets and pay everyone. Instant memory work.
    Returns the result, or None if there was nothing to draw (or it was already drawn)."""
    state = POINTS.lottery
    if state["drawn"] >= day:
        return None
    tickets = POINTS.tickets.get(day, {})
    total = sum(len(v) for v in tickets.values())
    if total == 0:
        POINTS.finish_lottery(day, state["carry"], None)
        return None
    nums = sorted(random.sample(range(1, LOTTERY_RANGE + 1), LOTTERY_PICKS))
    winning = set(nums)
    prizes: dict[int, int] = {}
    jackpot_tickets: list[int] = []
    n2 = n1 = 0
    for uid, mine in tickets.items():
        for picks in mine.values():
            matched = len(winning & set(picks))
            if matched == LOTTERY_PICKS:
                jackpot_tickets.append(uid)
            elif matched == 2:
                prizes[uid] = prizes.get(uid, 0) + LOTTERY_PRIZE_2
                n2 += 1
            elif matched == 1:
                prizes[uid] = prizes.get(uid, 0) + LOTTERY_COST
                n1 += 1
    carry, paid = state["carry"], 0
    if jackpot_tickets:
        share = carry // len(jackpot_tickets)
        for uid in jackpot_tickets:
            prizes[uid] = prizes.get(uid, 0) + share
        paid = share * len(jackpot_tickets)
        carry = LOTTERY_SEED + (carry - paid)                   # restart (any leftover coins stay in)
    for uid, amount in prizes.items():
        POINTS.add_balance(uid, amount)
    result = {"day": day, "nums": nums, "tickets": total, "w3": len(jackpot_tickets), "w2": n2, "w1": n1,
              "jackpot": paid, "carry": carry, "top": sorted(set(jackpot_tickets))[:10]}
    POINTS.finish_lottery(day, carry, result)
    POINTS.tickets.pop(day, None)
    return result


def _lottery_result_embed(r: dict) -> discord.Embed:
    e = discord.Embed(title=f"🎟️ Lottery draw — {r['day']}", color=discord.Color.gold())
    lines = [f"🎱 **{_lottery_nums(r['nums'])}**", f"{r['tickets']:,} ticket{'s' if r['tickets'] != 1 else ''} {'were' if r['tickets'] != 1 else 'was'} played.", ""]
    if r["w3"]:
        lines.append(f"🏆 **JACKPOT!** {' '.join(f'<@{u}>' for u in r['top'])} won **{pfmt(r['jackpot'])}**"
                     + (f" ({r['w3']} winning tickets, shared)" if r["w3"] > 1 else "") + " 🎉")
    else:
        lines.append(f"No jackpot winner — it rolls over to **{pfmt(r['carry'])}**!")
    lines.append(f"🥈 2 matches: **{r['w2']}** ticket{'s' if r['w2'] != 1 else ''} × {pfmt(LOTTERY_PRIZE_2)}")
    lines.append(f"🥉 1 match: **{r['w1']}** ticket{'s' if r['w1'] != 1 else ''} refunded")
    lines.append("\nNew tickets are on sale now — `/lottery`!")
    e.description = "\n".join(lines)
    return e


async def _post_lottery_result(r: dict) -> None:
    embed = _lottery_result_embed(r)
    channel_id = LOTTERY_CHANNEL_ID or RESET_ANNOUNCE_CHANNEL_ID
    if channel_id:
        async def send() -> None:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await _retry_discord(send, "the lottery result post")
    await log_event(f"🎟️ Lottery {r['day']}: numbers {_lottery_nums(r['nums'])}, {r['tickets']} tickets, "
                    f"{r['w3']} jackpot / {r['w2']} two-match / {r['w1']} one-match winners, jackpot now {pamt(r['carry'])}.")


async def _lottery_settle(day: str) -> None:
    result = _lottery_draw(day)
    if result is None:
        return
    await POINTS.flush("lottery draw")          # prizes + the new jackpot are saved right away
    await _post_lottery_result(result)


async def _lottery_loop() -> None:
    """Background task: draws at the daily reset, and catches up on any day that was
    missed because Milo was offline at midnight."""
    await bot.wait_until_ready()
    while not POINTS.ready:
        await asyncio.sleep(5)
    today = _day_info()[0]
    for old_day in sorted(d for d in POINTS.tickets if d < today):
        await _lottery_settle(old_day)
    last = today
    while True:
        day, secs = _day_info()
        if day != last:
            ended, last = last, day
            await _lottery_settle(ended)
            continue
        await asyncio.sleep(max(0.5, min(secs, 30)))



# ---------------------------------------------------------------------------
# 🛍️ /shop -- cosmetic DESIGNS for Milo's messages (no Discord roles, so no 250-role limit)
#
# Four independent categories, each with 3 designs + the free "Classic":
#   slots  -> how /slots looks when you spin once
#   quick  -> how the long result looks when you spin all at once
#   daily  -> how your /daily card looks
#   win    -> the special card Milo posts when you have the BIGGEST win of a Gamblin game
# Designs are purely cosmetic (never odds or payouts). Everything can be previewed for free first.
# What you own + what you've equipped is saved to the database. SHOP_PRICE_PERCENT scales all prices.
# ---------------------------------------------------------------------------
SHOP_PRICE_PERCENT = _env_int("SHOP_PRICE_PERCENT", 100)

SHOP_CATEGORIES = {
    "slots": ("🎰", "/slots — single spin", "How your result looks when you spin once."),
    "quick": ("⚡", "/slots — spin all", "How the long result looks when you spin everything at once."),
    "daily": ("🎁", "/daily", "How your daily reward card looks."),
    "win": ("🏆", "Gamblin win card", "The card Milo posts when you have the biggest win of a Gamblin game."),
}
SHOP_ITEMS: dict[str, dict] = {
    "slots_neon": {"cat": "slots", "name": "Neon Arcade", "price": 10_000, "blurb": "Electric colours and a glowing machine."},
    "slots_royal": {"cat": "slots", "name": "Royal Casino", "price": 20_000, "blurb": "Gold, crowns and a boxed-in machine."},
    "slots_cosmic": {"cat": "slots", "name": "Cosmic Milo", "price": 35_000, "blurb": "Stars, sparkles and Milo himself."},
    "quick_receipt": {"cat": "quick", "name": "Receipt Roll", "price": 12_000, "blurb": "Your spins printed like a shop receipt."},
    "quick_score": {"cat": "quick", "name": "Scoreboard", "price": 22_000, "blurb": "Green and black tiles, easy to scan."},
    "quick_galaxy": {"cat": "quick", "name": "Galaxy Grid", "price": 40_000, "blurb": "A compact grid, wins sparkle."},
    "daily_sunrise": {"cat": "daily", "name": "Sunrise Card", "price": 10_000, "blurb": "A warm morning card with a streak bar."},
    "daily_treasure": {"cat": "daily", "name": "Treasure Chest", "price": 18_000, "blurb": "Open the chest, tick off the week."},
    "daily_gift": {"cat": "daily", "name": "Milo's Gift Box", "price": 30_000, "blurb": "Confetti, a bow and Milo."},
    "win_champion": {"cat": "win", "name": "Champion Banner", "price": 15_000, "blurb": "A golden banner for the winner."},
    "win_fireworks": {"cat": "win", "name": "Fireworks", "price": 30_000, "blurb": "Light up the whole channel."},
    "win_legend": {"cat": "win", "name": "Hall of Legends", "price": 50_000, "blurb": "Your stats and records, in gold."},
}


def shop_price(item_id: str) -> int:
    return max(1, SHOP_ITEMS[item_id]["price"] * SHOP_PRICE_PERCENT // 100)


def _clean_shop_state(raw) -> dict | None:
    """Keep only things we know about (so an old or odd saved value can't break anything)."""
    if not isinstance(raw, dict):
        return None
    own = sorted({i for i in raw.get("own", []) if isinstance(i, str) and i in SHOP_ITEMS}) if isinstance(raw.get("own"), list) else []
    eq_raw = raw.get("eq", {}) if isinstance(raw.get("eq"), dict) else {}
    eq = {c: i for c, i in eq_raw.items() if i in own and SHOP_ITEMS[i]["cat"] == c}
    return {"own": own, "eq": eq} if own else None


def style_of(uid: int, cat: str) -> str:
    """The design id this person has equipped for a category ('classic' if none or not owned)."""
    st = POINTS.shop.get(uid)
    if not st:
        return "classic"
    item = st.get("eq", {}).get(cat)
    return item if item in SHOP_ITEMS and item in st.get("own", []) and SHOP_ITEMS[item]["cat"] == cat else "classic"


def _rgb(r: int, g: int, b: int) -> discord.Color:
    return discord.Color.from_rgb(r, g, b)


# ---- /slots (single spin) ----------------------------------------------------------------
def _render_spin(style: str, reels: list[str], headline: str, spin_line: str, footer: str,
                 color: discord.Color, won: bool) -> discord.Embed:
    r0, r1, r2 = reels
    if style == "slots_neon":
        e = discord.Embed(title="🎰 ✦ NEON ARCADE ✦ 🎰",
                          description=f"```\n┃ {r0} ┃ {r1} ┃ {r2} ┃\n```\n{headline}\n{spin_line}",
                          color=_rgb(0, 240, 255) if won else _rgb(255, 43, 214))
    elif style == "slots_royal":
        e = discord.Embed(title="👑 ROYAL SLOTS 👑",
                          description=f"```\n╔════╦════╦════╗\n║ {r0} ║ {r1} ║ {r2} ║\n╚════╩════╩════╝\n```\n{headline}\n{spin_line}",
                          color=_rgb(255, 200, 0) if won else _rgb(140, 20, 40))
    elif style == "slots_cosmic":
        e = discord.Embed(title="🌌 COSMIC MILO SLOTS 🌌",
                          description=f"✧･ﾟ: *✧･ﾟ:*  {MILO_EMOJI}  **[ {r0} | {r1} | {r2} ]**  {MILO_EMOJI}  *:･ﾟ✧*:･ﾟ✧\n{headline}\n{spin_line}",
                          color=_rgb(150, 80, 255) if won else _rgb(60, 40, 120))
    else:
        e = discord.Embed(title="🎰 Slots", description=f"**[ {' | '.join(reels)} ]**\n{headline}\n{spin_line}", color=color)
    e.set_footer(text=footer)
    return e


# ---- /slots (spin all) -------------------------------------------------------------------
def _spin_mark(sp: dict) -> str:
    """Pity wins are always called out, in every design."""
    if sp["pity"] == "soft":
        return f" 🍀 pity win (Milo felt bad after {SLOTS_PITY_AT - 1} misses)"
    if sp["pity"] == "jackpot":
        return f" 🎯 {SLOTS_JACKPOT_PITY_AT}-spin guarantee"
    return ""


def _render_batch(style: str, spins: list[dict], result: str, footer: str) -> discord.Embed:
    total = sum(sp["payout"] for sp in spins)
    wins = [sp for sp in spins if sp["payout"]]
    n = len(spins)
    big = any(sp["payout"] >= SLOT_PAYOUTS[SLOT_JACKPOT] for sp in spins)
    if style == "quick_receipt":
        rows = [f"#{sp['n']:02d}  {' '.join(sp['reels'])}" + (f"  +{sp['payout']}" if sp["payout"] else "") + (" *" if sp["pity"] else "")
                for sp in spins]
        body = "═══ MILO SLOTS ═══\n" + "\n".join(rows) + f"\n──────────────────\nSPINS {n}   WINS {len(wins)}\nTOTAL +{total}\n═══════════════════"
        if any(sp["pity"] for sp in spins):
            body += "\n* = pity win"
        e = discord.Embed(title="🧾 Milo Slots receipt", description=f"```\n{body}\n```"[:4000], color=_rgb(235, 232, 210))
    elif style == "quick_score":
        rows = [f"{'🟩' if sp['payout'] else '⬛'} `{sp['n']:>2}` {' '.join(sp['reels'])}"
                + (f" **+{sp['payout']}**" if sp["payout"] else "") + _spin_mark(sp) for sp in spins]
        e = discord.Embed(title=f"📊 Scoreboard — {n} spins", description="\n".join(rows)[:4000], color=_rgb(0, 200, 150))
    elif style == "quick_galaxy":
        cells = [(f"✨[{''.join(sp['reels'])}]✨" if sp["payout"] else f"[{''.join(sp['reels'])}]") + ("🍀" if sp["pity"] == "soft" else "🎯" if sp["pity"] == "jackpot" else "")
                 for sp in spins]
        rows = [f"`{i + 1:02d}-{min(i + 3, n):02d}` " + "  ".join(cells[i:i + 3]) for i in range(0, n, 3)]
        e = discord.Embed(title=f"🌌 Galaxy spin — {n} spins", description="\n".join(rows)[:4000], color=_rgb(120, 60, 220))
    else:
        rows = [f"**{sp['n']}.** {' '.join(sp['reels'])}" + (f"  ✅ **+{sp['payout']}**" if sp["payout"] else "") + _spin_mark(sp) for sp in spins]
        e = discord.Embed(title=f"🎰 Slots — {n} spins at once", description="\n".join(rows)[:4000],
                          color=discord.Color.gold() if big else (discord.Color.green() if wins else discord.Color.dark_grey()))
    e.add_field(name="Result", value=result, inline=False)
    e.set_footer(text=footer)
    return e


# ---- /daily -------------------------------------------------------------------------------
def _render_daily(style: str, mention: str, reward: int, streak: int, lost: bool, old_streak: int,
                  tomorrow: str, footer: str) -> discord.Embed:
    day_in_week = ((streak - 1) % 7) + 1
    streak_line = f"🔥 Streak: **{streak}** day{'s' if streak != 1 else ''}" + (f" *(your {old_streak}-day streak ended — fresh start!)*" if lost else "")
    if style == "daily_sunrise":
        bar = "🟧" * day_in_week + "⬜" * (7 - day_in_week) + f"  day {day_in_week}/7"
        e = discord.Embed(title="🌅 Good morning!", description=f"{mention} claimed **+{pfmt(reward)}**\n{bar}\n{streak_line}", color=_rgb(255, 150, 60))
    elif style == "daily_treasure":
        cal = "".join("✅" if i < day_in_week else ("🎁" if i == 6 else "⬜") for i in range(7))
        e = discord.Embed(title="🪙 Daily Treasure", description=f"🧰 {mention} opened the chest: **+{pfmt(reward)}**\n{cal}\n{streak_line}", color=_rgb(255, 200, 0))
    elif style == "daily_gift":
        e = discord.Embed(title="🎀 Milo's Gift Box 🎁",
                          description=f"🎊🎉🎊🎉🎊\n{MILO_EMOJI} {mention} unwrapped **+{pfmt(reward)}**!\n{streak_line}\n🎊🎉🎊🎉🎊", color=_rgb(255, 120, 190))
    else:
        e = discord.Embed(title="🎁 Daily reward!", description=f"{mention} claimed **+{pfmt(reward)}**\n{streak_line}", color=discord.Color.green())
    e.add_field(name="Tomorrow", value=tomorrow, inline=False)
    e.set_footer(text=footer)
    return e


# ---- Gamblin win card ------------------------------------------------------------------------
def _render_win_card(style: str, uid: int, game: str, net: int, best_win: int, record: bool, games: int) -> discord.Embed:
    who = f"<@{uid}>"
    if style == "win_champion":
        e = discord.Embed(title="🏆━━━ CHAMPION ━━━🏆", description=f"{who}\n**{game}**\n💰 **+{pfmt(net)}**", color=_rgb(255, 200, 0))
    elif style == "win_fireworks":
        e = discord.Embed(title="🎆 THE TABLE IS ON FIRE 🎆",
                          description=f"🎇✨ {who} ✨🎇\nlit up **{game}** with\n💥 **+{pfmt(net)}** 💥", color=_rgb(255, 90, 60))
    elif style == "win_legend":
        lines = [f"{who} conquered **{game}**", f"💎 Profit: **+{pfmt(net)}**", f"🔥 Best single win: **+{best_win:,}**", f"🎲 Games played: **{games:,}**"]
        if record:
            lines.append("🏅 **NEW PERSONAL RECORD!**")
        e = discord.Embed(title="👑 LEGENDARY WIN 👑", description="\n".join(lines), color=_rgb(212, 175, 55))
    else:
        e = discord.Embed(title=f"🏆 {game} — biggest win", description=f"{who} won the most this game: **+{pfmt(net)}**", color=discord.Color.gold())
    return e


# ---- previews (free, sample data) -------------------------------------------------------------
def _shop_preview(uid: int, style: str) -> discord.Embed:
    """What this design looks like, using sample data. Nothing is spent or changed."""
    cat = SHOP_ITEMS[style]["cat"] if style in SHOP_ITEMS else None
    note = "👀 Demo — sample data, nothing was spent"
    if cat == "slots" or style == "classic_slots":
        reels = ["7️⃣"] * 3
        return _render_spin(style, reels, f"💥 **JACKPOT!!!** Triple 7️⃣ — **+{pfmt(100)}!**", "🆓 Free spin", note, discord.Color.gold(), True)
    if cat == "quick" or style == "classic_quick":
        spins = [{"n": i + 1, "reels": r, "payout": p, "pity": pt, "paid": False} for i, (r, p, pt) in enumerate([
            (["🍒", "🍋", "🍇"], 0, ""), (["⭐", "⭐", "🍉"], 0, ""), (["🍇", "🍇", "🍇"], 12, ""), (["🍋", "🍒", "🍋"], 0, ""),
            (["🍉", "🍋", "🍒"], 0, ""), (["⭐", "⭐", "⭐"], 25, ""), (["🍒", "🍒", "🍒"], 4, "soft"), (["🍇", "🍋", "🍉"], 0, ""), (["🍒", "⭐", "🍇"], 0, "")])]
        result = f"**3** wins · total **{pfmt(41)}** · best: spin 6 (+25)"
        return _render_batch(style, spins, result, note)
    if cat == "daily" or style == "classic_daily":
        return _render_daily(style, "@you", 35, 5, False, 0, f"**+{pfmt(40)}** · a **+{DAILY_WEEK_BONUS} bonus** in 2 days!", note)
    return _render_win_card(style, uid, "Roulette", 2400, 1800, True, 12)


_CLASSIC_PREVIEW = {"slots": "classic_slots", "quick": "classic_quick", "daily": "classic_daily", "win": "classic_win"}


def _status_text(uid: int, item_id: str) -> str:
    if item_id == "classic":
        return "free"
    if style_of(uid, SHOP_ITEMS[item_id]["cat"]) == item_id:
        return "✅ equipped"
    return "🔓 owned" if POINTS.owns(uid, item_id) else f"🔒 {pamt(shop_price(item_id))}"


def _shop_home_embed(uid: int) -> discord.Embed:
    e = discord.Embed(
        title="🛍️ Milo Shop",
        description=(f"Change how Milo's messages look. Pick a category, **preview any design for free**, then buy it with your {PTS_NAME}.\n"
                     f"Your balance: **{pfmt(POINTS.points(uid))}**"),
        color=discord.Color.gold(),
    )
    owned = POINTS.shop_state(uid)["own"]
    for cat, (emoji, label, blurb) in SHOP_CATEGORIES.items():
        eq = style_of(uid, cat)
        name = "Classic" if eq == "classic" else SHOP_ITEMS[eq]["name"]
        mine = sum(1 for i in owned if SHOP_ITEMS[i]["cat"] == cat)
        e.add_field(name=f"{emoji} {label}", inline=False, value=f"{blurb}\nEquipped: **{name}** · you own {mine}/3")
    e.set_footer(text="Designs are cosmetic only — they never change odds or payouts.")
    return e


def _shop_cat_embed(uid: int, cat: str, sel: str | None) -> discord.Embed:
    emoji, label, blurb = SHOP_CATEGORIES[cat]
    e = discord.Embed(title=f"{emoji} {label}", description=f"{blurb}\nYour balance: **{pfmt(POINTS.points(uid))}**", color=discord.Color.gold())
    items = ["classic"] + [i for i, v in SHOP_ITEMS.items() if v["cat"] == cat]
    for i in items:
        name = "Classic" if i == "classic" else SHOP_ITEMS[i]["name"]
        desc = "The normal Milo look." if i == "classic" else SHOP_ITEMS[i]["blurb"]
        eq = style_of(uid, cat)
        status = "✅ equipped" if (i == eq) else _status_text(uid, i)
        e.add_field(name=f"{'▶ ' if i == sel else ''}{name} — {status}", value=desc, inline=False)
    e.set_footer(text="Pick a design below, press Preview to see it, then Buy / Equip.")
    return e


def _shop_buy_error(uid: int, item_id: str) -> str | None:
    if not POINTS.ready:
        return _POINTS_UNAVAILABLE
    blocked = frozen_block(uid) or gamblin_block_text("`/shop`")
    if blocked:
        return blocked
    if POINTS.owns(uid, item_id):
        return "You already own that one! Equip it from the shop."
    price = shop_price(item_id)
    if POINTS.points(uid) < price:
        return f"🛍️ **{SHOP_ITEMS[item_id]['name']}** costs {pfmt(price)} and you have {pfmt(POINTS.points(uid))}. Keep grinding! 💪"
    return None


class ShopHomeView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        for cat, (emoji, label, _) in SHOP_CATEGORIES.items():
            b = discord.ui.Button(label=label[:40], emoji=emoji, style=discord.ButtonStyle.primary)
            b.callback = self._open(cat)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own with `/shop` 🐾", ephemeral=True)
            return False
        return True

    def _open(self, cat: str):
        async def callback(interaction: discord.Interaction) -> None:
            view = ShopCategoryView(self.owner_id, cat)
            await interaction.response.edit_message(embed=_shop_cat_embed(self.owner_id, cat, view.sel), view=view)
        return callback


class ShopCategoryView(discord.ui.View):
    def __init__(self, owner_id: int, cat: str, sel: str | None = None) -> None:
        super().__init__(timeout=300)
        self.owner_id, self.cat, self.sel = owner_id, cat, sel
        self.select = discord.ui.Select(placeholder="Pick a design…", options=self._options())
        self.select.callback = self._picked
        self.add_item(self.select)
        self.preview_btn = discord.ui.Button(label="Preview", emoji="👀", style=discord.ButtonStyle.secondary)
        self.preview_btn.callback = self._preview
        self.action_btn = discord.ui.Button(label="Pick a design", style=discord.ButtonStyle.success, disabled=True)
        self.action_btn.callback = self._action
        self.back_btn = discord.ui.Button(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary)
        self.back_btn.callback = self._back
        for b in (self.preview_btn, self.action_btn, self.back_btn):
            self.add_item(b)
        self._sync()

    def _options(self) -> list[discord.SelectOption]:
        opts = [discord.SelectOption(label="Classic", value="classic", description="Free — the normal Milo look", default=self.sel == "classic")]
        for i, v in SHOP_ITEMS.items():
            if v["cat"] == self.cat:
                opts.append(discord.SelectOption(label=v["name"], value=i, default=self.sel == i,
                                                 description=_status_text(self.owner_id, i)[:100]))
        return opts

    def _sync(self) -> None:
        for o in self.select.options:
            o.default = o.value == self.sel
        b, uid = self.action_btn, self.owner_id
        if self.sel is None:
            b.label, b.disabled, b.style = "Pick a design", True, discord.ButtonStyle.secondary
        elif style_of(uid, self.cat) == (self.sel if self.sel != "classic" else "classic"):
            b.label, b.disabled, b.style = "Equipped ✓", True, discord.ButtonStyle.secondary
        elif self.sel == "classic" or POINTS.owns(uid, self.sel):
            b.label, b.disabled, b.style = "Equip", False, discord.ButtonStyle.success
        else:
            b.label, b.disabled, b.style = f"Buy · {pamt(shop_price(self.sel))}", False, discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own with `/shop` 🐾", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction, content: str | None = None) -> None:
        self.select.options = self._options()
        self._sync()
        await interaction.response.edit_message(content=content, embed=_shop_cat_embed(self.owner_id, self.cat, self.sel), view=self)

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.sel = self.select.values[0]
        await self._refresh(interaction)

    async def _preview(self, interaction: discord.Interaction) -> None:
        if self.sel is None:
            await interaction.response.send_message("Pick a design from the dropdown first.", ephemeral=True)
            return
        style = _CLASSIC_PREVIEW[self.cat] if self.sel == "classic" else self.sel
        await interaction.response.send_message(f"👀 **{'Classic' if self.sel == 'classic' else SHOP_ITEMS[self.sel]['name']}** — this is how it looks:",
                                                embed=_shop_preview(self.owner_id, style), ephemeral=True)

    async def _action(self, interaction: discord.Interaction) -> None:
        uid = self.owner_id
        if self.sel is None:
            return
        if self.sel == "classic":
            POINTS.equip(uid, self.cat, None)
            await self._refresh(interaction, "✅ Back to the Classic look.")
        elif POINTS.owns(uid, self.sel):
            POINTS.equip(uid, self.cat, self.sel)
            await self._refresh(interaction, f"✅ **{SHOP_ITEMS[self.sel]['name']}** equipped.")
        else:
            err = _shop_buy_error(uid, self.sel)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            price = shop_price(self.sel)
            await interaction.response.edit_message(
                content=(f"🛍️ Buy **{SHOP_ITEMS[self.sel]['name']}** for **{pfmt(price)}**?\n"
                         f"You have {pfmt(POINTS.points(uid))}. It's yours forever and gets equipped right away."),
                embed=None, view=ShopConfirmView(uid, self.cat, self.sel))

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=_shop_home_embed(self.owner_id), view=ShopHomeView(self.owner_id))


class ShopConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, cat: str, item_id: str) -> None:
        super().__init__(timeout=60)
        self.owner_id, self.cat, self.item_id, self.used = owner_id, cat, item_id, False
        self.confirm_btn.label = f"Buy ({pamt(shop_price(item_id))})"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own with `/shop` 🐾", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Buy", emoji="🛒", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            await interaction.response.send_message("Already handled!", ephemeral=True)
            return
        self.used = True
        self.stop()
        err = _shop_buy_error(self.owner_id, self.item_id)           # re-check: things may have changed
        if err:
            back = ShopCategoryView(self.owner_id, self.cat, self.item_id)
            await interaction.response.edit_message(content=err, embed=_shop_cat_embed(self.owner_id, self.cat, self.item_id), view=back)
            return
        price = shop_price(self.item_id)
        POINTS.buy_item(self.owner_id, self.item_id, price)
        name = SHOP_ITEMS[self.item_id]["name"]
        back = ShopCategoryView(self.owner_id, self.cat, self.item_id)
        await interaction.response.edit_message(content=f"🎉 You bought **{name}** for {pfmt(price)} and it's equipped!",
                                                embed=_shop_cat_embed(self.owner_id, self.cat, self.item_id), view=back)
        await log_event(f"🛍️ `/shop` <@{self.owner_id}> bought **{name}** for {price:,}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        self.stop()
        back = ShopCategoryView(self.owner_id, self.cat, self.item_id)
        await interaction.response.edit_message(content="Cancelled — nothing bought. 🐾", embed=_shop_cat_embed(self.owner_id, self.cat, self.item_id), view=back)


@bot.tree.command(name="shop", description=f"Buy designs for Milo's messages with your {PTS_NAME} (preview first!).")
async def shop_slash(interaction: discord.Interaction):
    if interaction.guild is not None and not is_allowed_guild(interaction.guild.id):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    if not POINTS.ready:
        await interaction.response.send_message(_POINTS_UNAVAILABLE, ephemeral=True)
        return
    await interaction.response.send_message(embed=_shop_home_embed(interaction.user.id), view=ShopHomeView(interaction.user.id), ephemeral=True)



# ---------------------------------------------------------------------------
# 🛠️ Manager toolbox -- hidden ?commands (Manager role only, silent to others)
#
#   ?give @user +300 pts     add points   (or -40 pts to take some away)
#   ?set @user 500           set someone's balance to an exact number
#   ?undo                    reverse the last ?give / ?set (this session)
#   ?check @user             a person's balance, earned, rank, spins today
#   ?balances                everyone's current points (shown here, then vanishes)
#   ?resetspins @user        give someone their free + bonus spins back for today
#   ?snapshot                post everyone's points to the log channel right now
#   ?mhelp                   this list
#
# Every change is also written to the log channel (LOG_CHANNEL_ID, optional),
# which also gets a "points at reset" snapshot every night at the daily reset.
# These change balances only -- never the all-time "earned" leaderboard.
# ---------------------------------------------------------------------------
_raw_log_channel = os.getenv("LOG_CHANNEL_ID", "").strip()
LOG_CHANNEL_ID = int(_raw_log_channel) if _raw_log_channel.isdigit() else None
MGR_MAX_AMOUNT = 1_000_000
_MGR_TARGET = r"(?:<@!?(\d+)>|(\d{15,20}))"
_GIVE_RE = re.compile(rf"^\?give\s+{_MGR_TARGET}\s+([+-]?[\d,]+)\s*(?:pts?|points?)?\s*(.*)$", re.I | re.S)
_SET_RE = re.compile(rf"^\?set\s+{_MGR_TARGET}\s+([\d,]+)\s*(?:pts?|points?)?\s*(.*)$", re.I | re.S)
_ONLY_TARGET_RE = re.compile(rf"^\?\w+\s+{_MGR_TARGET}\s*$", re.I)
_MGR_WORDS = {"?give", "?set", "?undo", "?check", "?balances", "?resetspins", "?snapshot", "?mhelp",
              "?resetleaderboard", "?resetleaderbord", "?freeze", "?unfreeze", "?frozen", "?backup",
              "?restore", "?announce", "?resetpreview", "?giveitem", "?takeitem"}
_mgr_history: list[dict] = []           # recent balance changes, newest last (memory only)


async def log_event(text: str | None = None, embed: discord.Embed | None = None, file: discord.File | None = None) -> None:
    """Write to the log channel (if LOG_CHANNEL_ID is set). Never raises."""
    if text:
        logger.info("LOG: %s", text.replace("\n", " | "))
    if not LOG_CHANNEL_ID or discord_blocked():
        return
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        await channel.send(content=text, embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.warning("Couldn't write to the log channel (check LOG_CHANNEL_ID and Milo's permissions).", exc_info=True)


def _snapshot_embeds(title: str) -> list[discord.Embed]:
    """Everyone's current points, richest first, split into as many embeds as needed."""
    rows = sorted(((u, b) for u, b in POINTS.balance.items() if b != 0), key=lambda t: (-t[1], t[0]))
    total = sum(b for _, b in rows)
    footer = f"{len(rows)} people with {PTS_NAME} · {pamt(total)} in total"
    if not rows:
        return [discord.Embed(title=title, description=f"Nobody has any {PTS_NAME} yet.", color=discord.Color.dark_teal())]
    chunks: list[list[str]] = [[]]
    size = 0
    for i, (uid, bal) in enumerate(rows, 1):
        line = f"`#{i}` <@{uid}> — **{bal:,}** (earned {POINTS.total_earned(uid):,})"
        if size + len(line) > 3500:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1
    embeds = []
    for n, lines in enumerate(chunks, 1):
        e = discord.Embed(
            title=title if n == 1 else f"{title} (cont. {n})",
            description="\n".join(lines), color=discord.Color.dark_teal(),
        )
        if n == len(chunks):
            e.set_footer(text=footer)
        embeds.append(e)
    return embeds


async def post_snapshot(title: str) -> None:
    for e in _snapshot_embeds(title):
        await log_event(embed=e)


async def _log_startup_and_daily_snapshots() -> None:
    """Background task: say hello in the log channel, then post everyone's
    points every night at the daily reset (LIMIT_RESET_TZ, default IST)."""
    await bot.wait_until_ready()
    while not POINTS.ready:
        await asyncio.sleep(5)
    await log_event(f"🟢 **Milo is online** — {PTS_NAME} loaded for {len(POINTS.balance)} people.")
    while True:
        day, secs = _day_info()
        await asyncio.sleep(secs + 2)
        try:
            await post_snapshot(f"🌙 Daily reset — everyone's {PTS_NAME} at the end of {day}")
            await post_backup(f"💾 Nightly backup — {day} (keep this file safe; `?restore` loads it back)")
        except Exception:
            logger.exception("Daily snapshot failed")


def _mgr_rank_text(uid: int) -> str:
    rank = POINTS.rank_of(uid)
    return f"#{rank}" if rank else f"not on it ({POINTS.total_earned(uid):,}/{LEADERBOARD_MIN_POINTS:,})"


class ResetLeaderboardView(discord.ui.View):
    """Confirm / Cancel for ?resetleaderboard. Only the manager who typed it can press."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=45)
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the manager who typed this can press it 🐾", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, reset it", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        if GAMBLIN is not None:
            await interaction.response.edit_message(content="🎰 A Gamblin game started in the meantime — try again when it's over.", view=None)
            return
        if not POINTS.ready:
            await interaction.response.edit_message(content=f"❌ {PTS_CAP} aren't loaded yet — try again in a minute.", view=None)
            return
        before = sorted(POINTS.earned.items(), key=lambda t: (-t[1], t[0]))[:10]
        changed = POINTS.reset_earned_to_balance()
        await interaction.response.edit_message(content=f"✅ Leaderboard reset for {changed} people. Saving to the database…", view=None)
        ok, _, detail = await POINTS.flush("leaderboard reset")
        if POINTS.store is None:
            result = "⚠️ No database is configured, so this only lasts until Milo restarts."
        elif ok:
            result = "💾 Saved. Safe to redeploy."
        else:
            result = f"⚠️ NOT saved yet ({detail}). Run `?updateDB` before redeploying."
        board = len(POINTS.qualified())
        await interaction.edit_original_response(
            content=f"✅ **Leaderboard reset** — everyone's all-time earned is now their current {PTS_NAME} ({changed} people changed, {board} on the board). {result}"
        )
        old = "\n".join(f"{i}. <@{u}> {v:,}" for i, (u, v) in enumerate(before, 1)) or "(empty)"
        await log_event(
            f"🧹 `?resetleaderboard` by <@{interaction.user.id}> — {changed} people changed, {board} on the board now. {result}\n"
            f"**Top earned before the reset:**\n{old}"
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing changed. 🐾", view=None)

    async def on_timeout(self) -> None:
        if self.message is not None and not self.done:
            try:
                await self.message.edit(content="Timed out — nothing changed.", view=None)
            except discord.HTTPException:
                pass


async def _handle_manager_command(message: discord.Message, word: str) -> bool:
    """Returns True if the message was a manager command from a real Manager.
    Anyone else gets silence (and the message is left alone)."""
    if not MANAGER_ROLE_ID or message.guild is None or not isinstance(message.author, discord.Member):
        return False
    if MANAGER_ROLE_ID not in {role.id for role in message.author.roles}:
        return False
    attachment_bytes = None                    # ?restore needs the file read BEFORE we delete the message
    if word == "?restore" and message.attachments and message.attachments[0].size <= 3_000_000:
        try:
            attachment_bytes = await message.attachments[0].read()
        except discord.HTTPException:
            attachment_bytes = None
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    async def reply(text: str, keep: int = 60) -> None:
        await message.channel.send(text, delete_after=keep, allowed_mentions=discord.AllowedMentions.none())

    text = message.content.strip()
    mgr = message.author

    async def find_member(uid_text: str) -> discord.Member | None:
        uid = int(uid_text)
        member = message.guild.get_member(uid)
        if member is None:
            try:
                member = await message.guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        if member is None or member.bot:
            await reply("❌ I can't find that person in this server (bots don't count).")
            return None
        return member

    def guard() -> str | None:
        if not POINTS.ready:
            return f"❌ {PTS_CAP} haven't loaded from the database yet — try again in a minute."
        if GAMBLIN is not None:
            return "🎰 Gamblin is running — balance changes would clash with held bets. Wait for it to end or use `?gamblin stop`."
        return None

    async def change(member: discord.Member, *, delta: int | None, set_to: int | None, note: str, kind: str) -> None:
        blocked = guard()
        if blocked:
            await reply(blocked)
            return
        uid = member.id
        before = POINTS.points(uid)
        d = (set_to - before) if set_to is not None else delta
        clamped = False
        if before + d < 0:
            d, clamped = -before, True          # balances never go below 0
        if d == 0:
            await reply(f"ℹ️ No change — {member.display_name} already has {pfmt(before)}.")
            return
        POINTS.adjust_balance(uid, d)
        after = POINTS.points(uid)
        _mgr_history.append({"uid": uid, "applied": d, "before": before, "after": after, "undone": False, "kind": kind})
        del _mgr_history[:-25]
        msg = f"✅ **{member.display_name}**: {before:,} → **{after:,}** ({d:+,})"
        if clamped:
            msg += " — they didn't have that many, so it stopped at 0"
        await reply(msg + "\nSaved within a few minutes (or run `?updateDB`).")
        extra = f" — {note}" if note else ""
        await log_event(f"🧾 `{kind}` by <@{mgr.id}>: <@{uid}> {before:,} → **{after:,}** ({d:+,}){extra}")

    # ---- ?give ----
    if word == "?give":
        m = _GIVE_RE.match(text)
        amount = int(m.group(3).replace(",", "")) if m and m.group(3).replace(",", "").lstrip("+-").isdigit() else None
        if not m or amount is None:
            await reply("Usage: `?give @user +300` · `?give @user -40` (add a note after it if you like; the unit is optional)")
            return
        if abs(amount) > MGR_MAX_AMOUNT:
            await reply(f"❌ That's a lot — the limit per command is {MGR_MAX_AMOUNT:,}.")
            return
        member = await find_member(m.group(1) or m.group(2))
        if member:
            await change(member, delta=amount, set_to=None, note=m.group(4).strip(), kind="?give")
        return

    # ---- ?set ----
    if word == "?set":
        m = _SET_RE.match(text)
        if not m or not m.group(3).replace(",", "").isdigit():
            await reply("Usage: `?set @user 500`")
            return
        value = int(m.group(3).replace(",", ""))
        if value > MGR_MAX_AMOUNT * 10:
            await reply("❌ That number is too big.")
            return
        member = await find_member(m.group(1) or m.group(2))
        if member:
            await change(member, delta=None, set_to=value, note=m.group(4).strip(), kind="?set")
        return

    # ---- ?undo ----
    if word == "?undo":
        blocked = guard()
        if blocked:
            await reply(blocked)
            return
        entry = next((h for h in reversed(_mgr_history) if not h["undone"]), None)
        if entry is None:
            await reply("ℹ️ Nothing to undo (only changes since Milo last started are remembered).")
            return
        uid = entry["uid"]
        before = POINTS.points(uid)
        d = -entry["applied"]
        if before + d < 0:
            d = -before
        entry["undone"] = True
        if d:
            POINTS.adjust_balance(uid, d)
        after = POINTS.points(uid)
        await reply(f"↩️ Undid {entry['kind']} for <@{uid}>: {before:,} → **{after:,}** ({d:+,})")
        await log_event(f"↩️ `?undo` by <@{mgr.id}>: <@{uid}> {before:,} → **{after:,}** ({d:+,})")
        return

    # ---- ?check / ?resetspins (need a target) ----
    if word in ("?check", "?resetspins"):
        m = _ONLY_TARGET_RE.match(text)
        if not m:
            await reply(f"Usage: `{word} @user`")
            return
        member = await find_member(m.group(1) or m.group(2))
        if member is None:
            return
        uid = member.id
        day, secs = _day_info()
        if word == "?check":
            free, bonus = POINTS.used(uid, day)
            unsaved = "yes" if uid in POINTS.unsaved_users() else "no"
            await reply(
                f"🔎 **{member.display_name}**\n"
                f"Balance **{POINTS.points(uid):,}** · earned {POINTS.total_earned(uid):,} · leaderboard {_mgr_rank_text(uid)}\n"
                f"Spins today: {free}/{SLOTS_FREE_SPINS} free, {bonus}/{SLOTS_PAID_SPINS} bonus · unsaved changes: {unsaved}\n"
                f"Daily streak {POINTS.daily_state(uid)[1]} (last claim {POINTS.daily_state(uid)[0] or 'never'}) · "
                f"777 pity {POINTS.jp_count(uid)}/{SLOTS_JACKPOT_PITY_AT} · sent today {POINTS.sent_today(uid, day)}/{PAY_DAILY_CAP} · "
                f"frozen: {'YES 🧊' if uid in POINTS.frozen else 'no'}",
                keep=90,
            )
            return
        if not POINTS.ready:
            await reply(f"❌ {PTS_CAP} haven't loaded from the database yet — try again in a minute.")
            return
        key = (day, uid)
        cur = POINTS.spins.get(key, [0, 0])
        used = list(cur)
        if not any(used):
            await reply(f"ℹ️ {member.display_name} hasn't used any spins today.")
            return
        for idx in (0, 1):
            if used[idx]:
                POINTS.spins[key][idx] = 0
                POINTS._p_spins.setdefault(key, [0, 0])[idx] -= used[idx]
        POINTS.pity.pop(uid, None)
        POINTS.last_spin.pop(uid, None)
        await reply(f"🔄 Reset today's spins for **{member.display_name}** ({used[0]} free + {used[1]} bonus given back).")
        await log_event(f"🔄 `?resetspins` by <@{mgr.id}>: <@{uid}> ({used[0]} free + {used[1]} bonus spins restored)")
        return

    # ---- ?balances ----
    if word == "?balances":
        if not POINTS.ready:
            await reply(f"❌ {PTS_CAP} haven't loaded from the database yet — try again in a minute.")
            return
        embeds = _snapshot_embeds(f"💰 Everyone's {PTS_NAME} right now")
        await message.channel.send(
            embed=embeds[0], delete_after=120, allowed_mentions=discord.AllowedMentions.none()
        )
        if len(embeds) > 1:
            await reply(f"…plus {len(embeds) - 1} more page(s) — use `?snapshot` to post them all to the log channel.")
        return

    # ---- ?snapshot ----
    if word == "?snapshot":
        if not POINTS.ready:
            await reply(f"❌ {PTS_CAP} haven't loaded from the database yet — try again in a minute.")
            return
        if not LOG_CHANNEL_ID:
            await reply("⚠️ Set `LOG_CHANNEL_ID` first so I know where to post it.")
            return
        await post_snapshot(f"📸 Snapshot requested by {mgr.display_name}")
        await reply("📸 Posted to the log channel.")
        return

    # ---- ?resetleaderboard ----
    if word in ("?resetleaderboard", "?resetleaderbord"):
        blocked = guard()
        if blocked:
            await reply(blocked)
            return
        changed = sum(1 for u in set(POINTS.earned) | set(POINTS.balance)
                      if max(0, POINTS.balance.get(u, 0)) != POINTS.earned.get(u, 0))
        will_qualify = sum(1 for b in POINTS.balance.values() if b >= LEADERBOARD_MIN_POINTS)
        top = POINTS.qualified()[:5]
        top_text = "\n".join(f"{i}. <@{u}> {v:,}" for i, (u, v) in enumerate(top, 1)) or "(nobody yet)"
        view = ResetLeaderboardView(mgr.id)
        view.message = await message.channel.send(
            "⚠️ **Reset the all-time leaderboard?**\n"
            f"Everyone's **total earned** will be set to their **current {PTS_NAME}**. Balances don't change.\n"
            f"• {changed} people will change\n"
            f"• After the reset, **{will_qualify}** people have {LEADERBOARD_MIN_POINTS:,}+ {PTS_NAME}, "
            "so only they will show on the board (the rest return as they earn)\n"
            f"• Top earners now:\n{top_text}\n"
            "This can't be undone (the old top 10 is saved in the log channel).",
            view=view, allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # ---- ?freeze / ?unfreeze / ?frozen ----
    if word in ("?freeze", "?unfreeze"):
        m = re.match(rf"^\?(?:un)?freeze\s+{_MGR_TARGET}\s*(.*)$", text, re.I | re.S)
        if not m:
            await reply(f"Usage: `{word} @user`" + (" `optional reason`" if word == "?freeze" else ""))
            return
        if not POINTS.ready:
            await reply(f"❌ {PTS_CAP} haven't loaded from the database yet — try again in a minute.")
            return
        member = await find_member(m.group(1) or m.group(2))
        if member is None:
            return
        flag = word == "?freeze"
        reason = m.group(3).strip()
        if not POINTS.set_frozen(member.id, flag):
            await reply(f"ℹ️ {member.display_name} is already {'frozen' if flag else 'not frozen'}.")
            return
        await reply(f"🧊 **{member.display_name}** is now frozen (no earning, gambling or trading)." if flag
                    else f"☀️ **{member.display_name}** is unfrozen.")
        await log_event(f"{'🧊' if flag else '☀️'} `{word}` by <@{mgr.id}>: <@{member.id}>" + (f" — {reason}" if reason else ""))
        return
    if word == "?frozen":
        lines = [f"<@{u}>" for u in sorted(POINTS.frozen)]
        await reply("🧊 **Frozen:** " + (", ".join(lines) if lines else "nobody"), keep=60)
        return

    # ---- ?backup / ?restore ----
    if word == "?backup":
        if not POINTS.ready:
            await reply(f"❌ {PTS_CAP} haven't loaded from the database yet — try again in a minute.")
        elif not LOG_CHANNEL_ID:
            await reply("⚠️ Set `LOG_CHANNEL_ID` first — backups are posted to the log channel so they stay private.")
        else:
            await post_backup(f"💾 Backup requested by <@{mgr.id}>")
            await reply("💾 Backup posted to the log channel.")
        return
    if word == "?restore":
        blocked = guard()
        if blocked:
            await reply(blocked)
            return
        if attachment_bytes is None:
            await reply("Attach the backup `.json` file (under 3 MB) to your `?restore` message.")
            return
        data, err = _validate_backup(attachment_bytes)
        if err:
            await reply(f"❌ {err}")
            return
        view = RestoreView(mgr.id, data)
        view.message = await message.channel.send(
            "⚠️ **Restore this backup?**\n"
            + (f"Made <t:{data['created']}:F>\n" if data["created"] else "")
            + f"• {len(data['balance'])} balances ({pfmt(sum(data['balance'].values()))} in the file vs {sum(POINTS.balance.values()):,} now)\n"
            f"• {len(data['earned'])} leaderboard totals, {len(data['jackpot'])} jackpot counters, {len(data['daily'])} daily streaks, "
            f"{len(data['stats'])} Gamblin records, {len(data['frozen'])} frozen, {len(data['shop'])} shop inventories\n"
            "Only people **in the file** are changed (everyone else is left alone); the frozen list is set to match the file exactly.\n"
            "This overwrites current numbers and can't be undone — run `?backup` first if you're unsure.",
            view=view, allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # ---- ?giveitem / ?takeitem (shop designs) ----
    if word in ("?giveitem", "?takeitem"):
        m = re.match(rf"^\?(?:give|take)item\s+{_MGR_TARGET}\s+(\w+)\s*$", text, re.I)
        if not m or m.group(3).lower() not in SHOP_ITEMS:
            await reply(f"Usage: `{word} @user item_id`\nItems: " + ", ".join(f"`{i}`" for i in SHOP_ITEMS), keep=90)
            return
        if not POINTS.ready:
            await reply("❌ Points haven't loaded from the database yet — try again in a minute.")
            return
        member = await find_member(m.group(1) or m.group(2))
        if member is None:
            return
        item = m.group(3).lower()
        if word == "?giveitem":
            POINTS.grant_item(member.id, item, equip=False)
            await reply(f"🎁 Gave **{SHOP_ITEMS[item]['name']}** to {member.display_name}.")
        elif POINTS.revoke_item(member.id, item):
            await reply(f"↩️ Took **{SHOP_ITEMS[item]['name']}** from {member.display_name}.")
        else:
            await reply(f"ℹ️ {member.display_name} doesn't own that design.")
            return
        await log_event(f"🛍️ `{word}` by <@{mgr.id}>: <@{member.id}> — {SHOP_ITEMS[item]['name']} (`{item}`)")
        return

    # ---- ?resetpreview: see the midnight message without pinging anyone ----
    if word == "?resetpreview":
        await message.channel.send(
            "👀 **Preview** of the midnight announcement (the real one pings @everyone"
            + ("" if RESET_ANNOUNCE_CHANNEL_ID else " — but `RESET_ANNOUNCE_CHANNEL_ID` isn't set, so it's currently OFF")
            + "):",
            embed=_reset_announce_embed(), delete_after=60, allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # ---- ?announce ----
    if word == "?announce":
        await _do_announce(message, reply)
        return

    # ---- ?mhelp ----
    if word == "?mhelp":
        e = discord.Embed(title="🛠️ Manager commands", description="Only people with the Manager role can use these.", color=discord.Color.dark_teal())
        e.add_field(name=f"💰 {PTS_CAP}", inline=False, value=(
            f"`?give @user +300` / `-40` — add or remove {PTS_NAME} (optional note after it)\n"
            "`?set @user 500` — set an exact balance · `?undo` — reverse the last ?give / ?set\n"
            "`?check @user` — balance, earned, rank, spins, streak, pity, frozen?\n"
            f"`?balances` — everyone's {PTS_NAME} · `?resetspins @user` — restore today's spins\n"
            f"`?resetleaderboard` — set the all-time leaderboard to everyone's current {PTS_NAME} (asks to confirm)\n"
            "`?freeze @user [reason]` / `?unfreeze @user` / `?frozen` — stop (or allow) earning, gambling and trading\n"
            "`?giveitem @user item_id` / `?takeitem @user item_id` — give or remove a shop design (type it with no item to see the ids)"))
        e.add_field(name="🎲 Games & events", inline=False, value=(
            "`?gamblin` — pick a game (slots / blackjack / duck race / roulette), ping everyone and start it\n"
            "`?gamblin duck` (or `slots`, `bj`, `roulette`) — start straight away · `?gamblin stop` — end any game\n"
            "`?gamblin schedule duck 8pm` — announce + auto-start later (`in 30m`, `8:30pm`, `20:00`)\n"
            "`?gamblin schedule 8pm` — same, but pick the game with buttons\n"
            "`?gamblin schedule list` / `cancel` — see or clear what's scheduled\n"
            "`?resetpreview` — preview the 12:00 AM \"everything has reset\" announcement (no ping)"))
        e.add_field(name="📢 Chat & safety", inline=False, value=(
            "`?announce Title | message` — fancy embed (add `#channel` and/or `--ping` for @everyone)\n"
            "`?send` — speak as Milo · `?rolemenu` — post the self-role menu\n"
            "`?backup` — post a backup file to the log channel (also automatic every night)\n"
            "`?restore` — attach a backup file to the message, then confirm\n"
            f"`?snapshot` — post everyone's {PTS_NAME} to the log channel · `?updateDB` — save to Upstash now"))
        e.set_footer(text="Balance changes never touch the all-time leaderboard (except ?resetleaderboard).")
        await message.channel.send(embed=e, delete_after=180)
        return
    return True



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

    if message.content.strip().lower() == "?updatedb":
        await _handle_updatedb_command(message)
        return

    _first_word = message.content.strip().lower().split(None, 1)[0] if message.content.strip() else ""
    if _first_word == "?gamblin":
        await _handle_gamblin_command(message)
        return
    if _first_word in _MGR_WORDS:
        if await _handle_manager_command(message, _first_word) is not False:
            return  # a Manager used it. Everyone else falls through, untouched.

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
        elif custom_id.startswith(THINK_BUTTON_PREFIX):
            await _handle_think_click(interaction, custom_id)
        elif custom_id.startswith(GAMBLIN_PREFIX):
            await handle_gamblin_click(interaction, custom_id)
        elif custom_id.startswith(BJ_PREFIX):
            await handle_bj_click(interaction, custom_id)
        elif custom_id.startswith(DK_PREFIX):
            await handle_dk_click(interaction, custom_id)
        elif custom_id.startswith(RL_PREFIX):
            await handle_rl_click(interaction, custom_id)
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
_background_tasks: set[asyncio.Task] = set()


async def main() -> None:
    logger.info("Starting Milo...")
    await start_http_server()

    # Render stops a service with SIGTERM and kills it ~30 seconds later. By
    # default Python would just die on SIGTERM; instead, close() so unsaved
    # points are written to Upstash first.
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        logger.info("Shutdown requested -- saving points, then closing.")
        task = loop.create_task(bot.close())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass  # e.g. Windows: Ctrl+C raises KeyboardInterrupt instead, which also closes cleanly

    logger.info("Connecting to Discord...")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down Milo (KeyboardInterrupt).")
