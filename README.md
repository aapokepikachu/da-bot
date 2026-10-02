# Milo

A simple, single-server, **cat-themed** Discord bot built with **Python 3.12+** and **discord.py 2.x**.

Features:
- 35 slash commands (shown in Discord's `/` picker) across group management, cat-themed fun, games, lookups, and utility — see Section 9 for the full list
- A persistent **✔️ Interested** button on `/join` messages — survives bot restarts, **no database required**
- A cat-themed welcome message when new members join
- Self-assign roles (button-based, no database), posted via the hidden `?rolemenu` command
- Two hidden, Manager-role-only text commands — `?send` and `?rolemenu` — the **only** `?`-prefixed commands in the whole bot. Neither shows up in Discord's `/` picker and neither is listed in `/help`; they're documented here in the README only.
- Several commands call free, keyless public APIs (cat facts/photos, quotes, dictionary, weather, translation, reaction gifs) — every call has a graceful fallback message if the API is briefly down or slow
- Runs as a **Render Free Web Service** (Discord Gateway + a tiny HTTP health server in one process)
- Secrets stay in environment variables, never in code
- No database — everything is either generated live, pulled from a free public API, or derived from Discord interaction/event data

---

## 1. File overview

```
da-bot/
├── bot.py            # All bot logic
├── requirements.txt   # Python dependencies
├── .env.example       # Placeholder environment variables (safe to commit)
├── .gitignore         # Keeps .env and local junk out of git
└── README.md          # This file
```

---

## 2. Local setup

### 2.1 Install Python
Install **Python 3.12 or newer** from [python.org](https://www.python.org/downloads/). Confirm with:

```bash
python3 --version
```

### 2.2 Create a virtual environment

```bash
cd da-bot
python3 -m venv .venv
source .venv/bin/activate     # On Windows: .venv\Scripts\activate
```

### 2.3 Install dependencies

```bash
pip install -r requirements.txt
```

### 2.4 Create your `.env` file

```bash
cp .env.example .env
```

Then fill in real values — see **Section 10** below for what every variable does.

### 2.5 Run the bot

```bash
python bot.py
```

You should see log lines like `Logged in as Milo#1234`, `Connected to 1 guild(s).`, and `HTTP health server listening on 0.0.0.0:10000`.

---

## 3. Discord Developer Portal setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application** (or open your existing Milo application).
2. Open the **Bot** tab → click **Add Bot** (skip if already added).
3. Under **Privileged Gateway Intents**, enable **both**:
   - **Message Content Intent** — required so the hidden `?send` command can read its message text.
   - **Server Members Intent** — required so `on_member_join` fires reliably for the welcome message.

   (Slash commands and buttons themselves don't need either — these two are only needed because of `?send` and the welcome feature.)
4. Click **Reset Token** (or **Copy**) to get your bot token. Put it in `.env` / Render as `DISCORD_TOKEN`. **Never share this token or commit it to GitHub.**
5. Go to **OAuth2 → URL Generator**:
   - Scopes: check **bot** and **applications.commands**.
   - Bot Permissions: at minimum **Send Messages**, **Embed Links**, **Add Reactions** (for `/poll`), **Manage Messages** (so `?send` can delete the triggering message), **Manage Roles** (for `?rolemenu` to grant/remove self-assign roles), **Read Message History** (for `/firstmessage`).
   - If you invited Milo before adding **Manage Roles**, regenerate the URL with it checked and re-invite — re-inviting with added permissions doesn't remove the bot or reset anything, it just grants the new permission.
6. Copy the generated URL, open it in your browser, and invite the bot to your one server.
7. After the bot logs in for the first time, it registers its slash commands automatically. If `ALLOWED_GUILD_ID` is set, they appear in that server almost instantly; without it, a global sync can take up to an hour.

---

## 4. Render setup (Free Web Service)

1. Push this project to a **GitHub repository**.
2. Go to [Render](https://render.com/) and create a **New Web Service**.
3. **Connect your GitHub repository**.
4. Select the **Free** instance plan.
5. **Build command:** `pip install -r requirements.txt`
6. **Start command:** `python bot.py`
7. Add every variable from **Section 10** in Render's Environment tab. Render provides `PORT` automatically.
8. Click **Deploy**, then watch **Logs** for `Logged in as ...` and `Milo is ready.`
9. Check that the bot shows as **online** in your Discord server's member list.

### Free tier realities (please read)

- Render Free Web Services can **spin down after ~15 minutes** of no incoming HTTP/WebSocket traffic, and take roughly a minute to spin back up.
- Render can restart the service at any time; Free services get **750 instance hours per calendar month**.
- The filesystem is **ephemeral** — nothing written to disk survives a restart, redeploy, or spin-down. This is exactly why the bot has **no database and no local persistent files**.
- This bot is **not guaranteed to be online 24/7** on the Free plan, and it does not use any hacky "keep-alive" tricks to fake that.

---

## 5. How the button persistence works (no database)

When someone runs `/join`, the bot attaches an **✔️ Interested** button (green, Discord's "success" style) whose `custom_id` is `join_interested:<AUTHOR_USER_ID>` (well under Discord's 100-character limit). A global `on_interaction` listener reads that ID back out whenever *anyone* clicks the button — including after the bot has restarted — and mentions both the clicker and the original author. No state is ever stored outside the message itself.

---

## 6. If the VBL/Minecraft role isn't notifying anyone

The bot's code already sends the role mention with `allowed_mentions=discord.AllowedMentions(roles=True, ...)`, so it isn't being suppressed on the code side. If the role still shows up as a mention but doesn't actually ping/notify members, it's almost always one of these **server-side settings**, not a code problem:

1. **The role isn't mentionable.** Go to **Server Settings → Roles → (the VBL or Minecraft role)**, and toggle **"Allow anyone to @mention this role"** ON.
2. **Or, give Milo's own role the permission to bypass that.** Go to **Server Settings → Roles → Milo's role**, and enable **"Mention @everyone, @here, and All Roles"**.

Either fixes it — you don't need both. Option 2 is usually cleaner since it doesn't open the role up to being pinged by everyone else too.

---

## 7. Self-assign roles with the hidden `?rolemenu` command

This works exactly like the Interested button — no database. Each role button's `custom_id` encodes the role ID directly (e.g. `self_role:123456789012345678`), so clicking it keeps working forever, even after a restart, because the ID lives in the button itself rather than in memory.

`?rolemenu` is a **plain text command, not a slash command** — just like `?send` (see Section 8), it never appears in Discord's `/` picker and is deliberately left out of `/help`. It's documented here in the README only.

### How to set it up

1. **Decide which roles you want self-assignable**, and get each role's ID (Discord Settings → Advanced → turn on Developer Mode, then right-click a role in Server Settings → Roles → "Copy Role ID").
2. **Set `SELF_ROLES`** in your `.env` / Render environment, using the format:
   ```
   SELF_ROLES=ROLE_ID|Label|Emoji,ROLE_ID|Label|Emoji,...
   ```
   Emoji is optional per entry. Example:
   ```
   SELF_ROLES=111111111111111111|Gamer|🎮,222222222222222222|Artist|🎨,333333333333333333|Night Owl|🦉
   ```
3. **Give Milo the Manage Roles permission**, and in **Server Settings → Roles**, drag Milo's own role **above** every role listed in `SELF_ROLES` — Discord won't let a bot grant or remove a role ranked higher than its own, no matter what permission it has.
4. Restart the bot so it picks up the new `SELF_ROLES` value.

### How to use it in Discord

1. Go to whichever channel you want the role-picker to live in (commonly `#roles`).
2. A member with the **Manager role** (`MANAGER_ROLE_ID`) types `?rolemenu` in that channel and sends it, exactly like typing any normal message. Milo deletes that trigger message and posts an embed listing every configured role, each with its own button.
3. Anyone in the server can now click a button to **add** that role to themselves, and click it again to **remove** it — it toggles, and only they can see the ephemeral "Gave you..." / "Removed..." confirmation.
4. The message stays there permanently. You don't need to send `?rolemenu` again after a bot restart — the buttons keep working as-is. Only send it again if you want to post a *fresh* menu (e.g. after changing `SELF_ROLES`).
5. Anyone who isn't a Manager typing `?rolemenu` gets completely ignored — no reply, no error, no hint the command exists.

---

## 8. The hidden `?send` and `?rolemenu` commands

These are the **only two `?`-prefixed commands in the bot** — everything else is a slash command. Both are plain text commands (not slash commands), so neither ever appears in Discord's `/` picker, and neither is listed in `/help`. Both share the same `MANAGER_ROLE_ID` gate, so you only set that role up once.

| Command | What it does |
|---|---|
| `?send <message>` | Posts `<message>` in the current channel, exactly as Milo, then deletes the original command message. Example: a Manager types `?send Server maintenance tonight at 9 PM!` in `#announcements`, and Milo deletes their message and posts it in its place. |
| `?rolemenu` | Posts the self-assign roles embed + buttons in the current channel (see Section 7), then deletes the original command message. |

For both: if `MANAGER_ROLE_ID` isn't set, the command is completely disabled. If a non-Manager tries either one, nothing happens at all.

---

## 9. Command reference

### Group & server

| Command | Description |
|---|---|
| `/join game:VBL` | Posts a VBL looking-for-players message with an Interested button, and notifies the VBL role. |
| `/join game:Minecraft` | Same, for Minecraft. |
| `/join game:<choice> message:your text` | Same as above, with an optional custom message appended. Rate-limited to once per 30s per user. |
| `/serverinfo` | Shows info about the current server. |
| `/userinfo user:<member>` | Shows info about a member (defaults to yourself). |
| `/ping` | Replies with Milo's current Discord Gateway latency. |

### Cat Corner

| Command | Description |
|---|---|
| `/aboutme` | Milo's intro: who it is, what it does, and a clickable "AAPoke" link to its creator. |
| `/hi` | One of 25 random cat-themed greetings. |
| `/purr` | A random cat-ism (pun or ASCII cat). |
| `/catfact` | A random cat fact, via [catfact.ninja](https://catfact.ninja). |
| `/meow` | A random cat photo, via [cataas.com](https://cataas.com). |
| `/catbreed` | A random cat breed, its temperament, origin, and life span, via [TheCatAPI](https://thecatapi.com). |

### Fun & games

| Command | Description |
|---|---|
| `/8ball question:<text>` | Ask the magic 8-ball a question. |
| `/poll question option1 option2 ...` | Posts a poll (2–5 options) with number-emoji reactions for voting. |
| `/coinflip` | Flips a coin. |
| `/roll sides:<n> count:<n>` | Rolls dice (defaults: 1 die, 6 sides). |
| `/rps choice:rock` | Rock-paper-scissors against Milo, instant result. |
| `/trivia` | A random trivia question with answer buttons (60s to answer, nothing persisted). |
| `/riddle` | A random riddle; the answer is hidden behind a "Reveal Answer" button (ephemeral, only you see it). |
| `/slots` | Spins a 3-reel emoji slot machine — jackpot on all 3 matching, a near-miss on 2. |
| `/yesno` | A random yes/no answer with a reaction gif, via [yesno.wtf](https://yesno.wtf). |
| `/emojify text:<text>` | Converts letters to 🇦🇧🇨-style regional-indicator emoji and digits to keycap emoji. |
| `/hug user:<member>` | Hug someone, with a reaction gif via [otakugifs.xyz](https://otakugifs.xyz). |
| `/slap user:<member>` | Same, but a (playful) slap. |
| `/pat user:<member>` | Same, but a headpat. |
| `/wave user:<member>` | Wave at someone. |
| `/fistbump user:<member>` | Fist bump someone (otakugifs category: `brofist`). |
| `/nudge user:<member>` | Nudge someone (otakugifs category: `poke`). |
| `/handshake user:<member>` | Shake hands with someone. **Unverified** — see limitations below. |

### Lookups (free public APIs)

| Command | Description |
|---|---|
| `/quote` | A random inspirational quote, via [zenquotes.io](https://zenquotes.io). |
| `/define word:<text>` | Dictionary lookup, via [dictionaryapi.dev](https://dictionaryapi.dev). |
| `/weather location:<place>` | Current temperature & wind speed, via [Open-Meteo](https://open-meteo.com) (no API key needed). |
| `/translate text:<text> to:<lang>` | Translates text using a language code (`es`, `fr`, `hi`, ...), via [MyMemory](https://mymemory.translated.net). |

### Utility

| Command | Description |
|---|---|
| `/avatar user:<member>` | Shows a member's full-size avatar (defaults to yourself). |
| `/firstmessage channel:<#channel>` | Jump-link to the first message ever posted in a channel (defaults to the current one). |
| `/color hex_code:<code>` | Shows a swatch and the Discord embed-color value for a hex code. |

### Everything else

| Command | Description |
|---|---|
| `/help` | Shows the full command list (minus the hidden `?send` and `?rolemenu`). |
| `?send <message>` | **Hidden.** Manager-role-only. Posts `<message>` as Milo. Not in `/help`, not a slash command. |
| `?rolemenu` | **Hidden.** Manager-role-only. Posts the self-assign roles menu. Not in `/help`, not a slash command. |

---

## 10. Environment variables (final list)

| Variable | Required? | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | **Required** | Your bot's token. Never commit this. |
| `CREATOR_USER_ID` | Recommended | Your Discord user ID, used to build the `/aboutme` link. |
| `ALLOWED_GUILD_ID` | Recommended | Restricts the bot to one server and makes slash commands sync instantly. |
| `MILO_EMOJI` | Optional | The server's `:Milo:` emoji code. Defaults to `<:Milo:1554818642804482078>`. |
| `WELCOME_CHANNEL_ID` | Optional | Channel where new-member welcome messages post. Welcome feature is skipped if blank. |
| `MANAGER_ROLE_ID` | Optional | The one role allowed to use `?send` **and** `?rolemenu`. Both are disabled if blank. |
| `SELF_ROLES` | Optional | Self-assignable roles for `?rolemenu`, as `ROLE_ID\|Label\|Emoji,...`. `?rolemenu` has nothing to post if blank. |
| `PORT` | Managed by Render | HTTP port for the health server. Don't set this yourself on Render. |

See `.env.example` for a ready-to-copy version of all of these with comments.

---

## 11. Testing checklist

- [ ] `/aboutme` replies with an embed: Milo's intro, a bullet list of what it does, and a clickable "AAPoke" link.
- [ ] `/join game:VBL` posts the VBL message with the correct role mention (and members of that role actually get notified) plus a green ✔️ Interested button.
- [ ] `/join game:VBL message:Need 2 more players` appends the custom message on its own line.
- [ ] `/join game:Minecraft` posts the Minecraft message with the correct role mention and notification.
- [ ] Using `/join` twice within 30 seconds gives a "please wait" reply the second time.
- [ ] `/hi` gives a cat-themed reply with the Milo emoji.
- [ ] `/8ball question:Will it rain tomorrow?` gives a random classic 8-ball answer.
- [ ] `/poll question:Pizza? option1:Yes option2:No` posts an embed with 1️⃣ and 2️⃣ reactions already added.
- [ ] `/coinflip` and `/roll` reply with random results.
- [ ] `/serverinfo` and `/userinfo` show accurate live data.
- [ ] `/ping` replies with a latency in milliseconds.
- [ ] Clicking **✔️ Interested** posts `<@clicker> is joining <@author>` using real mentions.
- [ ] Restart the bot process, then click **Interested** on an *old* message — it still works correctly.
- [ ] A new member joining posts a cat-themed welcome message in `WELCOME_CHANNEL_ID`.
- [ ] `?send Hello everyone` from a Manager-role member deletes their message and posts "Hello everyone" as Milo.
- [ ] `?send Hello everyone` from a non-Manager does nothing at all (no reply, no error).
- [ ] `/help` lists every command **except** `?send`.
- [ ] Typing `?send` doesn't show up anywhere in Discord's `/` command picker.
- [ ] `?rolemenu` from a Manager deletes their message and posts the role-picker embed with one button per `SELF_ROLES` entry.
- [ ] `?rolemenu` from a non-Manager does nothing at all (no reply, no error).
- [ ] Typing `?rolemenu` or `?send` doesn't show up anywhere in Discord's `/` command picker, and neither appears in `/help`.
- [ ] Clicking a role button gives you that role and an ephemeral "Gave you..." confirmation; clicking it again removes the role.
- [ ] Restart the bot, then click a role button on an *old* `?rolemenu` message — it still works.
- [ ] `/catfact` and `/meow` return a fact and a cat photo respectively.
- [ ] `/purr` gives a random cat-themed one-liner.
- [ ] `/avatar` shows your own avatar; `/avatar user:<someone>` shows theirs.
- [ ] `/rps choice:rock` gives an instant win/lose/tie result.
- [ ] `/trivia` posts a question with 4 answer buttons; clicking one gives an ephemeral correct/incorrect reply.
- [ ] `/hug`, `/slap`, `/pat`, `/wave`, `/fistbump`, `/nudge` each post a reaction gif (or fall back to text-only if the gif API is briefly down).
- [ ] `/handshake` — **check this one specifically**; if `handshake` isn't a real category on otakugifs.xyz, it'll post text-only every time instead of occasionally. See limitations below for how to fix it if so.
- [ ] `/quote`, `/define word:cat`, `/weather location:London`, and `/translate text:Hello to:es` each return live results.
- [ ] `/firstmessage` jump-links to the oldest message in the current channel.
- [ ] `/color hex_code:ff6600` shows a swatch; an invalid code like `/color hex_code:zzz` gives a friendly error instead of crashing.
- [ ] `/riddle` posts a question; clicking "Reveal Answer" shows the answer only to you (ephemeral).
- [ ] `/slots` spins and gives a jackpot, near-miss, or no-match result.
- [ ] `/emojify text:hi 5` gives back `🇭🇮 5️⃣`.
- [ ] `/yesno` returns a yes/no/maybe answer with an image.
- [ ] `/catbreed` returns a random breed's name, temperament, origin, life span, and photo.
- [ ] Visiting `https://your-render-url.onrender.com/` shows `Milo is running.`

---

## 12. Limitations / assumptions

- Discord only renders `[text](url)` markdown as a clickable link **inside embeds**, not in plain message content, so `/aboutme` sends a small embed to make "AAPoke" clickable.
- `/poll` supports up to 5 options because Discord slash commands need fixed, named parameters rather than an open-ended list — `option3`–`option5` are optional.
- `?rolemenu` and `?send` share the same `MANAGER_ROLE_ID` gate — set it once, both features use it.
- `?rolemenu` can offer at most 25 roles (Discord's hard limit on buttons in one message); if `SELF_ROLES` has more, the extras are dropped with a log warning.
- `?send` requires both `MANAGER_ROLE_ID` to be set and the Message Content intent to be enabled — without either, it silently does nothing.
- Clicking your own **Interested** button is blocked with a friendly ephemeral message — remove that check in `bot.py` if you'd rather allow it.
- Custom `/join` messages are capped at 300 characters; Discord's own per-message limit is 2000.
- `/catfact`, `/meow`, `/catbreed`, `/quote`, `/define`, `/weather`, `/translate`, `/yesno`, and `/hug`/`/slap`/`/pat`/`/wave`/`/fistbump`/`/nudge`/`/handshake` all call free, keyless public APIs (catfact.ninja, cataas.com, TheCatAPI, zenquotes.io, dictionaryapi.dev, Open-Meteo, MyMemory, yesno.wtf, otakugifs.xyz). None of them need an API key or env var, but they're third-party services outside your control — if one is briefly down, slow, or rate-limits you, the command replies with a friendly fallback message instead of crashing (and the reaction commands still post as plain text without the gif).
- `/handshake` is genuinely unverified — I couldn't confirm from here whether `handshake` is a real category on otakugifs.xyz (its full category list wasn't something I could check live). If `/handshake` only ever posts text with no gif, open `bot.py`, find `handshake_slash`, and swap `"handshake"` for a real category name (its API docs list all valid ones) — one-line fix, same pattern as how `fistbump` → `brofist` and `nudge` → `poke` were already mapped.
- `/riddle`'s question bank, like `/trivia`'s, is a fixed local list in `bot.py` (`RIDDLES`) — add more by editing it directly.
- `/emojify` only converts letters a–z and digits 0–9; punctuation and other characters are left as-is. Long input is capped at 80 characters since very long emoji strings render poorly in Discord.
- `/trivia`'s question bank is a fixed local list in `bot.py` — add more by editing the `TRIVIA_QUESTIONS` list.
- `/firstmessage` needs the **Read Message History** permission; without it, it replies with a clear "I don't have permission" message rather than failing silently.
- The `/join` cooldown (30s per user) and `?send`'s message-delete step both need no persistence — they're either in-memory or one-shot actions.
- On Render's Free plan, expect spin-down after ~15 minutes of inactivity and a slower "cold start" reconnect afterward — a Render platform limitation, not a bug in the bot.
