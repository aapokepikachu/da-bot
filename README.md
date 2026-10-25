# Milo

A simple, single-server, **cat-themed** Discord bot built with **Python 3.12+** and **discord.py 2.x**.

Features:
- 46 slash commands (shown in Discord's `/` picker) across group management, cat-themed fun, games, points & slots, party/social, AI, lookups, and utility — see Section 9 for the full list
- A points economy: win points in `/slots` (25 free spins a day, then bonus spins for points), check them with `/points`, climb `/leaderboard` — kept in memory and saved to Upstash in the background (Section 15)
- A persistent **✔️ Interested** button on `/join` messages — survives bot restarts, **no database required**
- A cat-themed welcome message when new members join
- Self-assign roles (button-based, no database), posted via the hidden `?rolemenu` command
- Hidden text commands — `?send` (Manager role **or** Milo role), `?rolemenu`, `?updateDB`, `?gamblin` (slots, blackjack, duck race, roulette) and the Manager toolbox (`?give`, `?set`, `?undo`, `?check`, `?balances`, `?resetspins`, `?resetleaderboard`, `?snapshot`, `?mhelp`). All Manager role only, silent to everyone else; see Sections 16 and 17. — the **only** `?`-prefixed commands in the whole bot. None shows up in Discord's `/` picker and none is listed in `/help`; they're documented here in the README only.
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

## 6. If a /join game role isn't notifying anyone

The bot's code already sends the role mention with `allowed_mentions=discord.AllowedMentions(roles=True, ...)`, so it isn't being suppressed on the code side. If the role still shows up as a mention but doesn't actually ping/notify members, it's almost always one of these **server-side settings**, not a code problem:

1. **The role isn't mentionable.** Go to **Server Settings → Roles → (the role you set for that game in `JOIN_GAMES`)**, and toggle **"Allow anyone to @mention this role"** ON.
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

## 8. The hidden `?send`, `?rolemenu` and `?updateDB` commands

The `?`-prefixed commands are all **hidden, plain-text, Manager-only** (except `?send`, which the Milo role can use too). Everything else is a slash command. The full list, with what each does, is in Sections 16–22; type `?mhelp` in Discord any time for a short version. They are never shown in `/help`, and anyone without the right role just gets silence.

| Command | What it does |
|---|---|
| `?send <message>` | Posts `<message>` in the current channel, exactly as Milo, then deletes the original command message. Example: a Manager types `?send Server maintenance tonight at 9 PM!` in `#announcements`, and Milo deletes their message and posts it in its place. |
| `?rolemenu` | Posts the self-assign roles embed + buttons in the current channel (see Section 7), then deletes the original command message. |
| `?updateDB` | **Saves everyone's points to Upstash right now** and replies `✅ Saved N people's points… Safe to redeploy`, or `❌ NOT saved… Don't redeploy yet` if Upstash can't be reached. Run it before every redeploy (Section 15). Manager only. |

If neither role is configured, `?send` is completely disabled; if `MANAGER_ROLE_ID` isn't set, `?rolemenu` is completely disabled. If someone without a permitted role tries either one, nothing happens at all — no reply, no error.

| Role | `?send` | `?rolemenu` | `?updateDB` |
|---|---|---|---|
| Manager (`MANAGER_ROLE_ID`) | ✅ | ✅ | ✅ |
| Milo (`MILO_ROLE_ID`) | ✅ | ❌ | ❌ |
| Everyone else | ❌ | ❌ | ❌ |

---

## 9. Command reference

### Group & server

| Command | Description |
|---|---|
| `/join game:<name>` | Posts a looking-for-players message with a green ✔️ Interested button and pings that game's role. The games, their roles and their wording come from the `JOIN_GAMES` env var (Section 18). Out of the box it's **VBL** and **Minecraft**, exactly as before. Add `message:<text>` for a custom note. |
| `/join game:<choice> message:your text` | Same as above, with an optional custom message appended. Rate-limited to once per 30s per user. |
| `/serverinfo` | Shows info about the current server. |
| `/userinfo user:<member>` | Shows info about a member (defaults to yourself). |
| `/ping` | Latency check with several readings: **gateway** (Milo's live connection to Discord), **Discord API** called as Milo, **Discord API** over plain public HTTP, **message round trip**, **command delivery** (how long Discord took to hand Milo your command), and **Milo's own web server**. Shows an **average** of the Discord-facing pings plus Milo's **uptime**. 🟢 under 150 ms · 🟡 under 350 ms · 🔴 slower · ⚪ unavailable. |

### Cat Corner

| Command | Description |
|---|---|
| `/aboutme` | Milo's intro: who it is, what it does, and a clickable "AAPoke" link to its creator. |
| `/hi` | One of 25 random cat-themed greetings. |
| `/purr` | A random cat-ism (pun or ASCII cat). |
| `/catfact` | A random cat fact, via [catfact.ninja](https://catfact.ninja). |
| `/meow` | A random cat photo, via [cataas.com](https://cataas.com). |
| `/catbreed` | A random cat breed, its temperament, origin, and life span, via [TheCatAPI](https://thecatapi.com). **Requires `CAT_API_KEY`** (free signup) — see Section 10. |

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
| `/yesno` | A random yes/no answer with a reaction gif, via [yesno.wtf](https://yesno.wtf). |
| `/react reaction:<search> user:<member>` | Any otakugifs.xyz reaction (hug, slap, pat, wave, handhold, cry, dance, punch, wink, and ~60 more) — start typing in the `reaction` field to search and pick one. |
| `/throw user:<member>` | Throw a random absurdly-specific item at someone (or a random nearby person if left blank) — a Roomba fighting a cat, a Nokia 3310, a printer jam of biblical proportions, etc. Mirrors YAGPDB's own `/fun throw`: 5% chance of a triple throw, 10% chance of a double, otherwise a single. |
| `/grouptrivia` | A live group round: everyone in the channel can answer the same question. What each person picked stays secret, but their name appears in a visibly growing "answered so far" list the moment they lock one in. After 60 seconds, the correct answer and everyone who got it right are revealed. No database — all state lives in memory for that 60-second window only. |

### Slots & points

| Command | Description |
|---|---|
| `/slots` | Spin a 3-reel slot machine for points. **25 free spins a day**, then up to **20 bonus spins** in packs: **5 points buys a pack of 10 spins**. Match all three to win: 7️⃣ 100 · ⭐ 25 · 🍇 12 · 🍉 8 · 🍋 6 · 🍒 4. Every **200th spin since your last 777 is a guaranteed 777**, and a small win is guaranteed after 19 misses. Buying a pack asks you to confirm first. 5-second cooldown. Can be limited to certain channels with `SLOTS_CHANNEL_IDS`. |
| `/daily` | Claim free points once a day. Day 1 gives 15 and each consecutive day adds 5 (up to day 7), and **every 7th day in a row adds a +50 bonus**. Miss a day and the streak restarts. Works out to roughly 37 points a day for a perfect streak. |
| `/pay @user amount` | Send some of your points to another member. You confirm first, and there's a **daily limit of 200 points sent** per person (`PAY_DAILY_CAP`). Not allowed to yourself, to bots, to or from frozen people, or while a Gamblin game runs. |
| `/gamblinstats [user]` | Your Gamblin record (games, total profit, biggest win, best game, favourite) plus the **hall of fame**: biggest single win, best single game, most games, highest total profit. |
| `/points user:<member>` | Your balance, total points earned, leaderboard rank (or progress toward it), and spins left today. Private to you. Add a member to peek at theirs (spins hidden). |
| `/leaderboard` | Two boards: the top 10 by **total points earned** (all-time; you need 1,000 earned via `LEADERBOARD_MIN_POINTS` to appear, until then it shows who's closest) and, below it, the top 5 **richest right now** by current balance (no minimum). Both come from memory — 0 Upstash commands. |

### Party & social

| Command | Description |
|---|---|
| `/emojify text:<text>` | Converts letters to 🇦🇧🇨-style regional-indicator emoji and digits to keycap emoji. Discord markup tokens (custom emoji, mentions, timestamps — anything in `<...>`) pass through untouched instead of getting shredded. |
| `/mock text:<text>` | sPoNgEbOb CaSe converter. |
| `/fortune` | A random fortune-cookie-style message. |
| `/compliment user:<member>` | A random compliment, aimed at someone (defaults to yourself). |
| `/icebreaker` | A random conversation-starter question. |
| `/tableflip` | `(╯°□°)╯︵ ┻━┻` |
| `/unflip` | `┬─┬ ノ( ゜-゜ノ)` |

### Lookups (free public APIs)

| Command | Description |
|---|---|
| `/quote` | A random inspirational quote, via [zenquotes.io](https://zenquotes.io). |
| `/weather location:<place>` | Current temperature & wind speed. Add a country after a comma to be precise — `Bengaluru, India`. Tries [Open-Meteo](https://open-meteo.com) first, then [MET Norway](https://api.met.no) if Open-Meteo is rate-limiting Milo's shared IP; results are cached for 10 minutes. The footer says which one answered. |
| `/translate text:<text> to:<lang>` | Translates text using a language code (`es`, `fr`, `hi`, ...). Tries Google Translate, then MyMemory, then LibreTranslate, in order, so one being rate-limited doesn't take the whole command down. |
| `/joke` | A random dad joke, via [icanhazdadjoke.com](https://icanhazdadjoke.com). |
| `/advice` | A random piece of life advice, via [api.adviceslip.com](https://api.adviceslip.com). |
| `/comic` | A random xkcd comic, via xkcd's own official API. |
| `/meme` | A random meme pulled from Reddit, via [meme-api.com](https://meme-api.com). |

### AI

| Command | Description |
|---|---|
| `/ask question:<text>` | Ask Milo a quick question (up to 300 characters), answered by `openai/gpt-oss-20b` via [Groq](https://console.groq.com) in Milo's cat-themed voice. **Requires `GROQ_API_KEY`** (free, no credit card). Can be limited to certain channels (`ASK_CHANNEL_IDS`), has a 15-second per-person cooldown, and shows an "AI can be wrong" footer. Under each answer, optional **🧠 Think harder** / **🔮 Second opinion** buttons re-ask the question with a smarter model — only the asker can press them, for 10 minutes, with daily limits (see Section 14). Milo knows his own birthday and, if asked, that he's male. |

### Utility

| Command | Description |
|---|---|
| `/avatar user:<member>` | Shows a member's full-size avatar (defaults to yourself). |
| `/firstmessage channel:<#channel>` | Jump-link to the first message ever posted in a channel (defaults to the current one). |
| `/color hex_code:<code>` | Shows a swatch and the Discord embed-color value for a hex code. |

### Everything else

| Command | Description |
|---|---|
| `/help` | Shows the full command list (minus all the hidden `?` commands). |
| `?send <message>` | **Hidden.** Manager role or Milo role. Posts `<message>` as Milo. Not in `/help`, not a slash command. |
| `?rolemenu` | **Hidden.** Manager-role-only. Posts the self-assign roles menu. Not in `/help`, not a slash command. |
| `?updateDB` | **Hidden.** Manager-role-only. Saves all points to Upstash now and confirms. Not in `/help`, not a slash command. |
| `?gamblin` / `?gamblin stop` | **Hidden.** Manager-role-only. Shows a button picker (Slots / Blackjack / Duck Race / Roulette), pings everyone and starts the one you choose in `GAMBLIN_CHANNEL_ID`. `?gamblin stop` ends whichever game is running (Section 16). |
| `?resetleaderboard` | **Hidden.** Manager-role-only. Sets the all-time leaderboard to everyone's current points, after a confirm button (Section 17). |

---

## 10. Environment variables (final list)

| Variable | Required? | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | **Required** | Your bot's token. Never commit this. |
| `CREATOR_USER_ID` | Recommended | Your Discord user ID, used to build the `/aboutme` link. |
| `ALLOWED_GUILD_ID` | Recommended | Restricts the bot to one server and makes slash commands sync instantly. |
| `MILO_EMOJI` | Optional | The server's `:Milo:` emoji code. Defaults to `<:Milo:1554818642804482078>`. |
| `ASK_CHANNEL_IDS` | Optional | Channel ID(s) where `/ask` is allowed, comma-separated (e.g. `111111111111111111,222222222222222222`). Anywhere else, `/ask` politely points people to the allowed channel(s) and doesn't use any AI budget. Threads inside an allowed channel count as allowed. Blank = `/ask` works in every channel. To get an ID: Discord Settings → Advanced → Developer Mode on, then right-click the channel → Copy Channel ID. |
| `SLOTS_CHANNEL_IDS` | Optional | Channel ID(s) where `/slots` is allowed, comma-separated. Anywhere else `/slots` points people to the right channel and spends nothing. Threads inside an allowed channel count. Blank = every channel. |
| `SLOTS_FREE_SPINS` / `SLOTS_PAID_SPINS` | Optional | Free spins per person per day (default `25`) and most bonus spins per day (default `20`, rounded down to whole packs). |
| `SLOTS_PACK_COST` / `SLOTS_PACK_SPINS` | Optional | A bonus pack: points it costs (default `5`) and spins it gives (default `10`). **Replaces the old `SLOTS_SPIN_COST`, which is now ignored.** |
| `SLOTS_JACKPOT_PITY_AT` | Optional | The spin number (since your last 777) that is guaranteed to be a 777. Default `200`. |
| `DAILY_BASE` / `DAILY_STREAK_BONUS` / `DAILY_STREAK_CAP` / `DAILY_WEEK_BONUS` | Optional | `/daily`: day-1 reward (default `15`), extra per streak day (`5`), streak days that count (`7`), and the bonus on every 7th day (`50`). |
| `PAY_DAILY_CAP` | Optional | Most points one person can `/pay` per day. Default `200`. |
| `LOG_CHANNEL_ID` | Optional | A private staff channel where Milo logs manager actions, Gamblin results, save problems, online/offline notices, and everyone's points at the nightly reset. See Section 17. |
| `JOIN_GAMES` | Optional | The games `/join` offers: `ROLE_ID\|Name\|Emoji\|Call to action,...` (emoji and call-to-action optional). Unset = VBL + Minecraft as before. See Section 18. |
| `GAMBLIN_DUCK_ROUNDS` / `GAMBLIN_DUCK_BET_SECONDS` | Optional | Duck Race: races (default `3`) and betting window (`30`). |
| `GAMBLIN_RL_ROUNDS` / `GAMBLIN_RL_BET_SECONDS` | Optional | Roulette: spins (default `4`) and betting window (`40`). |
| `GAMBLIN_BJ_ROUNDS` / `GAMBLIN_BJ_BET_SECONDS` / `GAMBLIN_BJ_TURN_SECONDS` | Optional | Blackjack: rounds (default `5`), betting window (`30`), and time to play your hand (`40`). Uses the same channel, bet limits and table size as Gamblin. |
| `GAMBLIN_CHANNEL_ID` | Needed for Gamblin | The channel Gamblin runs in. Without it `?gamblin` just tells the manager to set it. See Section 16. |
| `GAMBLIN_LOBBY_SECONDS` / `GAMBLIN_ROUNDS` | Optional | Join window (default `90`) and rounds per event (default `3`). |
| `GAMBLIN_BET_SECONDS` / `GAMBLIN_REEL_SECONDS` | Optional | Betting window before reel 1 (default `40`) and before reels 2 and 3 (default `25`). |
| `GAMBLIN_MIN_BET` / `GAMBLIN_MAX_BET` | Optional | Smallest and largest single bet. Defaults `5` and `100`. |
| `GAMBLIN_MAX_PLAYERS` / `GAMBLIN_MAX_MULT` | Optional | Table size (default `25`) and the cap on 3-of-a-kind multipliers (default `500`). |
| `LEADERBOARD_MIN_POINTS` | Optional | Total points a person must have *earned* to appear on `/leaderboard`. Default `1000`. |
| `POINTS_SAVE_MINUTES` | Optional | How often changed points are saved to Upstash. Default `5`. |
| `GROQ_THINK_MODEL` | Optional | Turns on the 🧠 **Think harder** button using this Groq model. Recommended: `openai/gpt-oss-120b`. Same `GROQ_API_KEY` — no second key. Blank = no button. |
| `GROQ_SECOND_MODEL` | Optional | Turns on the 🔮 **Second opinion** button using a different model. Recommended: `qwen/qwen3.8-27b` (a *preview* model — can be withdrawn at short notice). Blank = no button. |
| `THINK_USER_DAILY` / `THINK_GLOBAL_DAILY` | Optional | Tries per person / for the whole server per day on 🧠. Defaults `3` / `40` (sized for ~10 people). |
| `SECOND_USER_DAILY` / `SECOND_GLOBAL_DAILY` | Optional | Same, for 🔮. Defaults `2` / `25`. |
| `LIMIT_RESET_TZ` | Optional | Timezone (IANA name) whose midnight resets the daily limits. Default `Asia/Kolkata`. |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Recommended | A free [Upstash](https://upstash.com) Redis database. It stores **the points** and the 🧠/🔮 daily limits, so both survive restarts. Without it, points work but live in memory only and are **wiped on every restart/redeploy** (Milo logs a loud warning). The token is a secret — treat it like `DISCORD_TOKEN`. |
| `GROQ_MODEL` | Optional | Which Groq model `/ask` uses. Defaults to `openai/gpt-oss-20b`. Groq retires models over time — if `/ask` suddenly starts failing with a 404 (`model_not_found` in the Render logs), set this to a current model from [console.groq.com/docs/models](https://console.groq.com/docs/models) instead of editing code. |
| `GROQ_API_KEY` | Optional | Free key from [console.groq.com](https://console.groq.com), needed for `/ask`. Command is disabled with a clear message if blank. Groq is unrelated to xAI/Grok or SpaceX despite the similar-sounding name. |
| `CAT_API_KEY` | Optional | Free key from [thecatapi.com/signup](https://thecatapi.com/signup), needed for `/catbreed`. Command is disabled with a clear message if blank. |
| `WELCOME_CHANNEL_ID` | Optional | Channel where new-member welcome messages post. Welcome feature is skipped if blank. |
| `MANAGER_ROLE_ID` | Optional | The Manager role. Can use `?send` **and** `?rolemenu`. `?rolemenu` is disabled if blank. |
| `MILO_ROLE_ID` | Optional | A second, more limited role (e.g. one you name "Milo"). Can use `?send` **only** — not `?rolemenu`. Leave blank if only Managers should use `?send`. |
| `SELF_ROLES` | Optional | Self-assignable roles for `?rolemenu`, as `ROLE_ID\|Label\|Emoji,...`. `?rolemenu` has nothing to post if blank. |
| `PORT` | Managed by Render | HTTP port for the health server. Don't set this yourself on Render. |

See `.env.example` for a ready-to-copy version of all of these with comments.

---

## 11. Testing checklist

- [ ] `/aboutme` replies with an embed: Milo's intro, a bullet list of what it does, and a clickable "AAPoke" link.
- [ ] `/join game:<a game>` posts the message with the correct role mention (and members of that role actually get notified) plus a green ✔️ Interested button.
- [ ] `/join game:VBL message:Need 2 more players` appends the custom message on its own line.
- [ ] `/join game:<another game>` posts that game's message with the correct role mention and notification.
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
- [ ] `?send Hello everyone` from a member with **only** the Milo role (`MILO_ROLE_ID`) does the same.
- [ ] `?rolemenu` from a member with **only** the Milo role does nothing at all (it's Manager-only).
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
- [ ] `/react reaction:hug user:<someone>` posts a reaction gif (or falls back to text-only if the gif API is briefly down).
- [ ] `/react` — typing a few letters in the `reaction` field (e.g. `cr`) shows matching suggestions (e.g. `cry`, `celebrate`); picking one and a user posts that reaction's gif.
- [ ] `/quote`, `/weather location:London`, and `/translate text:Hello to:es` each return live results.
- [ ] With `GROQ_THINK_MODEL` set, an `/ask` answer shows a 🧠 **Think harder** button. Pressing it as the asker posts a longer answer as a reply (footer shows the model and `1/3 used today`), and the button disappears from the original.
- [ ] Someone **else** pressing the button gets a private "only <@asker> can use this" message. Pressing a button on an answer older than 10 minutes gets a private "too old" message. Pressing twice on the same answer gets "already got its…".
- [ ] After your daily tries are spent, the button says so privately and tells you how long until it resets.
- [ ] Redeploy Milo, then press a button on a *recent* answer: it still works (the buttons carry their own data). If you've set Upstash, your used-up daily tries are still used up; without Upstash they reset.
- [ ] `/firstmessage` jump-links to the oldest message in the current channel.
- [ ] `/color hex_code:ff6600` shows a swatch; an invalid code like `/color hex_code:zzz` gives a friendly error instead of crashing.
- [ ] `/riddle` posts a question; clicking "Reveal Answer" shows the answer only to you (ephemeral).
- [ ] `/slots` spins and gives a jackpot, near-miss, or no-match result.
- [ ] `/emojify text:hi 5` shows individual boxed letters (🇭 🇮) followed by a keycap 5️⃣ — **not** a country flag. If you see an actual flag, the zero-width-space fix didn't make it into your deployed copy.
- [ ] `/emojify text:hi <:Milo:1554818642804482078>` (or any custom emoji/mention your server has) leaves the `<...>` part completely untouched instead of shredding its letters and digits into more emoji.
- [ ] `/translate text:Hello to:fr` returns an actual French translation, not the "couldn't translate" fallback message. Check Render's logs for an `Translated via <provider>` line to see which of the three actually succeeded.
- [ ] `/yesno` returns a yes/no/maybe answer with an image.
- [ ] With `GROQ_API_KEY` set, `/ask question:Why is the sky blue?` returns a short answer in an embed. Without it set, `/ask` gives a clear "needs a free Groq API key" message.
- [ ] `/ask question:Are you Milo?` answers in a cat-like voice and confirms it's Milo. `/ask question:When is your birthday?` gives the date from `MILO_BIRTHDAY`; `/ask question:Are you a boy or a girl?` answers male.
- [ ] `/aboutme` shows the birthday line (27 September 2026).
- [ ] With `ASK_CHANNEL_IDS` set, `/ask` in an allowed channel (and in a thread inside it) works; in any other channel it replies privately telling you where to go.
- [ ] Using `/ask` twice within 15 seconds gives a "ask again in Ns" message the second time; a different person isn't affected.
- [ ] `/ping` shows all six readings, an average, and an uptime; nothing says `unavailable` on a healthy deploy (if one does, that specific path is the one having trouble).
- [ ] If `/ask` ever fails, check the Render logs: a line like `Groq returned status 404: ...model_not_found...` now says exactly why.
- [ ] With `CAT_API_KEY` set, `/catbreed` returns a random breed's name, temperament, origin, life span, and photo. Without it set, `/catbreed` gives a clear "needs an API key" message instead of a generic failure.
- [ ] `/weather location:Sao Paulo` and `/weather location:Bengaluru, India` work; the title shows the full match (e.g. `Bengaluru, Karnataka, India`). If Open-Meteo is rate-limiting, the footer reads `Source: MET Norway` instead of failing. Asking the same place twice within 10 minutes is instant.
- [ ] `/mock text:hello world` returns `HeLlO WoRlD`-style alternating case.
- [ ] `/fortune`, `/compliment`, `/icebreaker`, `/tableflip`, `/unflip` each respond instantly (no network call, so no way for these to be slow or fail).
- [ ] `/joke`, `/advice`, `/comic`, `/meme` each return live results.
- [ ] `/throw` and `/throw user:<someone>` both work; roughly 1 in 20 throws should be a "TRIPLE THROW", 1 in 10 a "DOUBLE THROW".
- [ ] `/grouptrivia`: have 2+ people click different answer buttons. Confirm everyone who answered shows up by name in the embed, but nobody can see what anyone else picked. After 60 seconds, confirm the correct answer reveals and only the people who picked right get mentioned as winners.
- [ ] `/slots` shows the reels, the result, your balance, and spins left. A win adds points; a loss doesn't take any (free spins are free). Spinning twice within 5 seconds gives a private "reels are still spinning".
- [ ] After 25 free spins, the next `/slots` is a **bonus spin** that costs 5 points; with fewer than 5 points it explains why it can't. After 10 bonus spins it says you're done until midnight.
- [ ] With `SLOTS_CHANNEL_IDS` set, `/slots` in another channel replies privately with the right channel and spends nothing.
- [ ] `/points` shows balance, total earned, leaderboard progress, spins left. `/leaderboard` says nobody's on it until someone has earned 1,000.
- [ ] `?updateDB` from a Manager replies `✅ Saved … Safe to redeploy`; from anyone else, nothing happens at all. Check Upstash's Data Browser: keys starting `milo:points:` should appear.
- [ ] **Redeploy test:** win a few points, run `?updateDB`, redeploy, then `/points` — the same balance and spins-left come back. Render's log should say `Points loaded from Upstash: N people`.
- [ ] Visiting `https://your-render-url.onrender.com/` shows `Milo is running.`

---

## 12. Limitations / assumptions

- Discord only renders `[text](url)` markdown as a clickable link **inside embeds**, not in plain message content, so `/aboutme` sends a small embed to make "AAPoke" clickable.
- `/poll` supports up to 5 options because Discord slash commands need fixed, named parameters rather than an open-ended list — `option3`–`option5` are optional.
- `?send` accepts either `MANAGER_ROLE_ID` or `MILO_ROLE_ID`; `?rolemenu` accepts only `MANAGER_ROLE_ID`. A member holding both roles can use both commands.
- `?rolemenu` can offer at most 25 roles (Discord's hard limit on buttons in one message); if `SELF_ROLES` has more, the extras are dropped with a log warning.
- `?send` requires at least one of `MANAGER_ROLE_ID` / `MILO_ROLE_ID` to be set, plus the Message Content intent to be enabled — without those, it silently does nothing.
- Clicking your own **Interested** button is blocked with a friendly ephemeral message — remove that check in `bot.py` if you'd rather allow it.
- Custom `/join` messages are capped at 300 characters; Discord's own per-message limit is 2000.
- `/catfact`, `/meow`, `/quote`, `/weather`, `/translate`, `/yesno`, and `/react` all call free, keyless public APIs (catfact.ninja, cataas.com, zenquotes.io, Open-Meteo, Google Translate's web endpoint, yesno.wtf, otakugifs.xyz). None of them need an API key or env var, but they're third-party services outside your control — if one is briefly down, slow, or rate-limits you, the command replies with a friendly fallback message instead of crashing (and the reaction commands still post as plain text without the gif).
- **How `/ask`'s rate limit works.** `/ask` uses `openai/gpt-oss-20b` on Groq's free tier, which allows **30 requests/minute, 1,000 requests/day, 8,000 tokens/minute, and 200,000 tokens/day** — and you're held to whichever limit you hit *first*. These belong to your **Groq account, not to individual Discord users**: everyone on the server shares one pool. A "token" is roughly a small piece of a word, and every question spends some on Milo's personality prompt (~170), your question, the model's brief hidden "thinking", and the answer — roughly 350–550 in total. That means the **token limits bite long before the request limits do**: expect about **400–550 questions a day** (not 1,000) and about **10–15 a minute** across the whole server. (These are estimates, not guarantees.) When a limit is hit, Groq replies 429 and `/ask` answers with a friendly in-character message instead of crashing, telling a per-minute limit ("give me a minute") apart from a daily one ("I've used up my brainpower for today"). To protect the shared pool, `/ask` also has a 15-second per-person cooldown (a failed question doesn't count against it) and can be restricted to specific channels with `ASK_CHANNEL_IDS`. Groq's free-tier numbers change over time — see console.groq.com/docs/rate-limits.
- **Why `/ask` uses `GROQ_MODEL` instead of a hard-coded name.** The first version of `/ask` used `llama-3.1-8b-instant`, which Groq shut down for regular accounts on August 16, 2026 (its model page now lists it as Enterprise-only) — every request then failed with a 404. The model name is now an overridable env var (default `openai/gpt-oss-20b`) so the next retirement is a one-line change in Render, not a code fix. `gpt-oss` models are *reasoning* models whose hidden thinking tokens count against the response budget, so `/ask` requests low reasoning effort and a 500-token budget; if the budget ever runs out, Groq returns an empty answer, which `/ask` detects and logs (`finish_reason=length`) rather than posting a blank embed.
- `/ask` caps each question at 300 characters and each answer at roughly 300 tokens (and hard-truncates anything over 1000 characters) so one reply can never exceed Discord's embed field limits. It has no memory — every question is answered independently, with no conversation history carried over.
- `/catbreed` is the one exception that **does** need a key: TheCatAPI started requiring `x-api-key` on its breeds endpoint in late September 2026 (previously it worked unauthenticated). A free account at thecatapi.com/signup gives you one. If TheCatAPI changes its policy again in the future, this is the place in `bot.py` to look (`CAT_API_KEY` and the `catbreed_slash` function).
- Every `_fetch_json` call (used by every API-backed command) automatically retries once on a timeout before giving up, since free public APIs occasionally drop a single request without actually being down. Other failures (404, 429, connection refused) fail immediately without retrying.
- **Why `/weather` sometimes said "can't get" (HTTP 429).** Open-Meteo's free API limits by *IP address*, and on shared hosting like Render every app on the same IP shares one quota — so it can answer 429 ("too many requests") even though neither Milo nor Open-Meteo is broken. The same root cause as `/translate`'s old failures. Milo now falls back to MET Norway (Norway's national weather service; free, no key, asks clients to identify themselves — Milo's User-Agent includes your Discord profile link) and caches each place for 10 minutes. Geocoding (turning "Bengaluru" into coordinates) also goes through Open-Meteo and is cached for 24 hours; if *that* is rate-limited Milo says the map service is busy rather than pretending the place doesn't exist. Typos matter: `Banglore` fuzzy-matched somewhere in Pakistan once — use `Bengaluru`.
- `/weather` properly percent-encodes its input (via aiohttp's `params`) before building the request URL. An earlier version inserted raw user text directly into the URL string, so a city name with a space or accented letter could hang the request instead of cleanly failing.
- `/react` covers every otakugifs.xyz category via autocomplete in one command rather than a dedicated slash command per reaction — Discord caps a fixed dropdown at 25 choices, and there are nearly 70 categories, so this was simpler to maintain than keeping separate `/hug`, `/slap`, `/pat`, `/fistbump`, `/nudge` commands around too.
- `/define` was removed entirely: it depended on dictionaryapi.dev, which had a confirmed, chronic multi-week outage pattern (independently verified via third-party uptime monitoring) rather than an occasional blip. No reliable free, keyless alternative was found — every "no signup" option that turned up was either the same broken service under a different URL, or an unverified hobby project. A paid-free-tier option like Wordnik (free account, no card) would fix this properly if you want dictionary lookups back someday.
- `/riddle`'s question bank, like `/trivia`'s, is a fixed local list in `bot.py` (`RIDDLES`) — add more by editing it directly.
- `/emojify` only converts letters a–z and digits 0–9; punctuation and other characters are left as-is. Long input is capped at 80 characters since very long emoji strings render poorly in Discord. Each letter emoji is followed by an invisible zero-width space — without it, two adjacent regional-indicator letters (e.g. "H" + "I") get auto-merged into a country flag by Discord's renderer instead of showing as two separate boxed letters.
- `/emojify` also leaves any Discord markup token (`<...>`) completely untouched — custom emoji (`<:name:id>`), user/role/channel mentions, and timestamps. An earlier version shredded these: a custom emoji's name and numeric ID would get converted into boxed letters and keycap digits like everything else, breaking the code so Discord could no longer recognize it.
- `/joke`, `/advice`, `/comic`, and `/meme` were deliberately picked for having long, stable track records (icanhazdadjoke.com, adviceslip.com, xkcd's own official API, meme-api.com) rather than smaller/newer services, after `/define`'s removal over a chronically unreliable upstream. None of them need an API key.
- `/numberfact` was removed for the same reason as `/define`: `numbersapi.com` is confirmed dead (TLS certificate mismatch on HTTPS, and independent automated link-checkers from unrelated projects confirming a hard 404 on the bare domain as recently as 13 days before this was written). Not fixable by changing the request format -- the root domain itself is down.
- `/grouptrivia`'s per-round state (who answered, what they picked) lives only in memory for the duration of that one 60-second round, scoped to that specific message's View object. It is **not** shared between rounds and does not survive a bot restart mid-round -- if Milo restarts while a group trivia round is in progress, that round's buttons stop working and it never reveals a winner. This matches the same tradeoff `/trivia` and `/riddle` already make for their own short-lived views.
- `/fortune`, `/compliment`, `/icebreaker`, `/mock`, `/tableflip`, and `/unflip` are pure local logic with no network calls at all, so none of them can ever fail the way an external API can.
- `/translate` tries three free, keyless providers in order — Google Translate's web endpoint, then MyMemory, then LibreTranslate — falling through to the next one if a provider is rate-limited, down, or errors. This matters specifically because of Render's shared hosting IPs: lots of different apps on Render (and other free platforms) hit the same handful of free translate APIs, so any one of them can end up rate-limiting the whole IP range regardless of how little *this* bot calls it. None of the three need an API key or env var. If all three ever fail at once, the command says so plainly rather than showing a generic error — check Render's logs (`logger.warning` lines mentioning "translate providers failed") to see which ones were tried.
- None of the three translate providers are officially documented, supported APIs with an SLA — they're free community/unofficial endpoints that could change or disappear without notice. If translation stops working entirely someday, that's the most likely reason, and the fix is swapping in a real provider (e.g. DeepL's free tier, which gives each account its own quota instead of a shared one) in `bot.py`.
- `/trivia`'s question bank is a fixed local list in `bot.py` — add more by editing the `TRIVIA_QUESTIONS` list.
- `/firstmessage` needs the **Read Message History** permission; without it, it replies with a clear "I don't have permission" message rather than failing silently.
- The `/join` cooldown (30s per user) and `?send`'s message-delete step both need no persistence — they're either in-memory or one-shot actions.
- On Render's Free plan, expect spin-down after ~15 minutes of inactivity and a slower "cold start" reconnect afterward — a Render platform limitation, not a bug in the bot.

---

## 13. `/ask` in plain English (copy this into a rules/info channel if you like)

**Where does Milo's AI brain come from?** A free service called Groq. The key point: the server shares **one** allowance — it isn't per person. Think of it like a single prepaid phone card that everyone in the server uses.

**Two things can run out:**

1. **The "too many at once" limit.** If lots of people ask in the same minute, Milo says he needs a minute. Nothing is broken — wait about a minute and ask again.
2. **The daily limit.** The card only holds so much per day (roughly 400–550 questions for the whole server). When it's empty, Milo says he's out of brainpower, and it refills later (usually the next day). Short questions use less of the card than long ones.

**Why the rules?**
- **15 seconds between your own questions** so one person can't use up everyone's share.
- **`/ask` only works in the chosen channel(s)** (set by the server owner) so the allowance isn't burned in spam channels.
- **Milo can be wrong.** He's an AI with a cat's personality — double-check anything important.

**If Milo ever says "my brain's napping":** the AI service had a hiccup. Just try again in a bit.

---

## 14. The thinking buttons (🧠 Think harder / 🔮 Second opinion)

Under a normal `/ask` answer, optional buttons re-ask **the same question** with a smarter model. They're switched on by environment variables — **the same `GROQ_API_KEY` works for every model**, you only name the extra models.

### Which Groq models are free right now

Checked against Groq's docs (October 2026). Every free chat model gets the same allowance, and **each model has its own separate allowance** — using one never uses up another's:

| Model | Role in Milo | Free allowance (per model) |
|---|---|---|
| `openai/gpt-oss-20b` | **Fast** — plain `/ask` (`GROQ_MODEL`) | 30 requests/min · 1,000/day · 8,000 tokens/min · 200,000 tokens/day |
| `openai/gpt-oss-120b` | **Smart** — 🧠 (`GROQ_THINK_MODEL`) | same |
| `qwen/qwen3.8-27b` *(preview)* | **Second opinion** — 🔮 (`GROQ_SECOND_MODEL`) | same |

Not usable on a free account: `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` and `minimaxai/minimax-m2.7` are listed as Enterprise-only; `openai/gpt-oss-safeguard-20b`, the Prompt Guard models, Whisper and Orpheus are safety/audio/speech models, not chat models. Groq's docs say the exact numbers for *your* account are on [console.groq.com/settings/limits](https://console.groq.com/settings/limits) — check there if anything looks off. Qwen3.8-27B isn't necessarily "smarter" than the 120B, which is why its button says *Second opinion*.

### Default limits, sized for ~10 people

A think uses roughly 2,000–3,500 tokens (hidden reasoning + the answer), against 200,000 tokens/day per model.

| Button | Per person / day | Whole server / day | Cooldown | Worst-case daily token use |
|---|---|---|---|---|
| 🧠 Think harder | 3 | 40 | 60 s | 10 people × 3 = 30 thinks ≈ 60–105K (of 200K) |
| 🔮 Second opinion | 2 | 25 | 60 s | 10 people × 2 = 20 thinks ≈ 40–70K (of 200K) |

The server-wide cap is a safety net, not the normal limit: with 10 people it only matters if they all max out *and* the server grows. If it grows, raise `THINK_GLOBAL_DAILY`, but keep (cap × ~3,500) under 200,000. Only **one think runs at a time per button**, which also keeps Groq's 8,000-tokens-per-minute cap from being tripped.

### Why people can't skip the limits

| Attempt | What happens |
|---|---|
| Someone else presses your button | Private "only @you can use this" message. Nothing is counted. |
| Pressing a button on an old answer | Buttons last **10 minutes**; after that: private "too old" message, and the buttons are removed. The timestamp rides inside the button itself, so it also works across restarts. |
| Pressing twice on the same answer | "Already got its…" — one press per answer per button. |
| Running `/ask` over and over to get more buttons | Limits are per **person per day**, not per answer. A new answer doesn't add tries. |
| Spamming the button | 60-second cooldown, and one think at a time. |
| A button edited/forged to claim someone else's answer | Milo cross-checks it against Discord's own record of who ran the `/ask`. |
| Using up everyone's budget | The server-wide daily cap stops it; everyone else keeps their own tries. |

Every refusal has its own message (not yours / too old / already used / your daily limit with a countdown / server limit / cooldown / Milo busy / Groq's per-minute limit / Groq's daily limit / model unavailable / limit tracker down). **A think that fails for any reason is refunded** — errors never cost a try.

What these rules can't stop: a person using a second Discord account. That's what the server-wide cap is for.

### Making the daily limit permanent (Upstash)

> The same Upstash database also stores Milo's **points** (Section 15), so setting it up once covers both.

By default the counters live in Milo's memory, so **a restart or redeploy hands everyone fresh tries** (Milo logs a warning about this at startup). For a limit that actually survives restarts:

1. Make a free account at [upstash.com](https://upstash.com) and create a **Redis** database (free plan, pick the region nearest your Render region).
2. On the database page, open the **REST API** section and copy the URL and token.
3. In Render → Environment add `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`, then redeploy.
4. The startup log should now say *"Think limits are stored in Upstash Redis (survive restarts)"*.

Upstash's free plan is far more than this needs (a button press costs a handful of Redis commands). If Upstash is ever unreachable, the thinking buttons pause with a friendly message (they **fail closed** rather than letting everyone through) and normal `/ask` keeps working.

> **Note:** the Upstash code follows Upstash's documented REST format and is tested against a simulated server, but it couldn't be tried against a real Upstash database when it was written — so watch the first startup log and try one button press after adding the variables.

### `/ask` limits in plain English (for members)

> There's a **fast brain** for normal questions, and two **deeper brains** behind the 🧠 and 🔮 buttons. Each brain has its own daily allowance that the whole server shares. You get a few deeper tries per day (they come back at midnight IST), only you can press the buttons on your answer, and they stop working after 10 minutes. If Milo says he's busy or out of brainpower, nothing is lost — try again later.

---

## 15. Points, `/slots`, and how saving works

### The rules

| | |
|---|---|
| Free spins | **25 per person per day** (`SLOTS_FREE_SPINS`) |
| Bonus spins | up to **20 more per day** in **packs**: **5 points buys 10 spins** (`SLOTS_PACK_COST`, `SLOTS_PACK_SPINS`, `SLOTS_PAID_SPINS`). Only the first spin of each pack costs anything and asks you to confirm; the next 9 are already paid for. Unused spins in a pack expire at the daily reset. |
| Cooldown | 5 seconds between your spins |
| Wins (all 3 reels match; each is 1-in-216) | 7️⃣ **100** · ⭐ **25** · 🍇 **12** · 🍉 **8** · 🍋 **6** · 🍒 **4** |
| Pity (small) | the **20th** spin since your last win of any kind is a guaranteed 🍒 or 🍋 triple (low tier only). Memory only, resets on redeploy. |
| Pity (jackpot) | the **200th** spin since your last 7️⃣7️⃣7️⃣ **is** a 7️⃣7️⃣7️⃣, whatever else you won in between. Free and bonus spins both count. **Saved to the database**, so redeploys don't reset it. A 777 you win naturally resets the count. `/points` shows your progress. |
| Reset | midnight in `LIMIT_RESET_TZ` (default IST) |
| Leaderboard | ranked by **total points earned** (spending never lowers it); you need `LEADERBOARD_MIN_POINTS` (default 1,000) to appear |

Points are **virtual only** — they can't be bought or cashed out. Keep it that way: once real money or valuable prizes are involved, a slot machine becomes a gambling feature, which means legal and Discord-policy trouble.

### The numbers (simulated with the real code)

- **Income per spin averages about 1.18 points** (simulated over 1.5 million spins), so free spins give roughly **30 points per person per day**. Adding the 200-spin guarantee would have raised that by about a quarter, so the ⭐🍇🍉🍋🍒 payouts were trimmed to cancel it out: the daily income is the same as before, with the jackpot now landing on average every **133 spins** instead of every 216.
- **A bonus pack is now a good deal, on purpose:** 10 spins cost 5 and return about 11.8 on average, roughly **+7 points per pack, +14 a day** if someone buys both. The old price (5 per single spin) returned only about 24% and nobody would buy it. If you'd rather make packs break-even, set `SLOTS_PACK_COST=12`; for a sink, raise it more.
- **Rule of thumb:** a 10-spin pack is worth about 12 points, so any price under 12 is profit for the player and any price over 12 burns points.
- **The leaderboard is quicker now:** slots winnings (about 30 a day free, plus about 24 from two packs) plus `/daily` (about 37) is roughly 90 points earned a day, so the first person reaches 1,000 in about **11 days** (`LEADERBOARD_MIN_POINTS` changes the target).

### How saving works: memory first, saved in the background

Everything — spins, balances, rank — is read and changed in Milo's **memory**, so commands are instant and cost **zero** Upstash commands. Spamming `/slots`, `/points` or `/leaderboard` cannot touch your database budget.

Changed points are written to Upstash:

1. **every 5 minutes** (`POINTS_SAVE_MINUTES`), only for people who changed,
2. **when Render shuts Milo down** (it sends a stop signal, then waits ~30 seconds before killing it — a save takes about a second), and
3. **on demand with `?updateDB`**.

Two details make this safe:

- **Deltas, not totals.** Milo saves "+40 since last save", never "balance = 140". Render runs the old and new Milo side by side for about a minute during a deploy; saving totals could let one overwrite the other with a stale number, but adding deltas can't.
- **Load before opening.** On startup Milo loads everyone's points, today's spins used, and the leaderboard from Upstash *before* `/slots` works (until then it says "my points book isn't open" — it never starts everyone at zero and then overwrites the real data). If the load fails it retries every minute. It does one more re-sync about two minutes after boot, to catch anything the old copy saved during the overlap.

| Thing | Where it lives |
|---|---|
| Balances, total earned, today's spins used | Memory, saved to Upstash (every 5 min / shutdown / `?updateDB`) |
| 🧠/🔮 daily limits | Upstash immediately (strict limits shouldn't wait for a timer) |
| `/daily` streaks, 777-pity counters, Gamblin stats, frozen list, scheduled games | Memory, saved to Upstash with the same 5-minute save |
| What each person has `/pay`'d today | Memory, saved to Upstash (so the daily limit survives restarts) |
| Pity counter, 5-second cooldown | Memory only — fine to lose |
| `/points` and `/leaderboard` | Computed from memory (0 Upstash commands) |

### Redeploying safely

1. Type **`?updateDB`** (Manager only).
2. Wait for **`✅ … Safe to redeploy`**. If you get **`❌ NOT saved`**, don't redeploy — try again in a minute.
3. Redeploy. Milo saves again on shutdown automatically, so even if you forget step 1 you only risk the last few minutes. After it boots, Render's log should say `Points loaded from Upstash: N people`.

### Does it fit the free limits? (measured, 10 members)

Measured by running the actual save and limit code through simulated days. Upstash's free plan allows **500,000 commands a month** (about 16,700 a day); reads and writes both count, and batching doesn't reduce the count.

| Day type | Upstash commands/day | /month | Share of 500K |
|---|---|---|---|
| Normal — 2 play sessions each, ~35 spins, 2 think presses | 170 | 5,100 | 1.0% |
| Busy — play spread over 12 time-slots, all 5 think presses each | 386 | 11,580 | 2.3% |
| Extreme — every member spinning non-stop all day, all think presses | 564 | 16,920 | 3.4% |

A successful 🧠/🔮 press costs about 4.4 commands. Each active member costs roughly 16 commands a day, so the free plan only runs out at around **1,000 daily-active members**. Render usage is unaffected: the save is a tiny background task in the same process, so it uses no extra instance hours.

**Added later (measured the same way, 10 members):** `/daily`, `/pay`, the saved 777 counter, Gamblin stats and the schedule add roughly **60–100 commands a day** on top of the table above (a quiet day measured 77 in total, a busy Gamblin day 222, including 16 for the two startup loads). Everything together stays under about **4%** of the free plan.

### What can still go wrong (honestly)

- **A hard crash with no warning** loses at most the last save interval (default 5 minutes) of play. Render's normal redeploy/restart sends a stop signal first, which triggers a save.
- **A save whose reply gets lost on the way back** may be retried and credited twice. Rare, and at most one save's worth.
- **During a deploy**, both Milo copies answer for about a minute. Totals stay correct (deltas), but someone could squeeze in a couple of extra spins in that window.
- **If Upstash is down:** spins keep working from memory and saves retry every 5 minutes. If it's still down when Milo *restarts*, points stay paused until it returns.
- **No backups.** `?updateDB` is a save, not a backup, and the free plan shouldn't be assumed to keep backups.
- **Alt accounts** can't be stopped — each account gets its own daily spins. The daily cap bounds it.
- **Not tried against a real Upstash database** when written — the commands follow Upstash's documented REST format and were tested against a simulated server. After adding the variables, check the first startup log, run `?updateDB`, and look for `milo:points:*` keys in Upstash's Data Browser.

### `/slots` in plain English (for members)

> You get **25 free spins a day** (they reset at midnight IST). Match all three symbols to win points — 7️⃣ pays the most. After your free spins you can buy up to 10 bonus spins for 5 points each. If you go a long time without winning, Milo gives you a small guaranteed win. Check `/points` for your balance and `/leaderboard` for the top 10 — earn 1,000 points in total to get on it. Points are just for fun.


## 16. Gamblin (button-only betting event)

A Manager types **`?gamblin`** (from anywhere). Milo shows a short-lived button picker: **🎰 Slots · 🃏 Blackjack · 🦆 Duck Race · 🎡 Roulette**. Only the manager who typed it can press. Pick one and Milo pings @everyone and starts it in `GAMBLIN_CHANNEL_ID`. **`?gamblin stop`** ends whichever game is running. Shortcuts skip the picker: `?gamblin slots`, `?gamblin bj`, `?gamblin duck`, `?gamblin roulette`. All of it is silent to everyone without the Manager role. (`?gamblinbj` no longer exists; it's now just `?gamblin bj`.)

**Scheduling:** `?gamblin schedule duck 8pm` announces the game with an @everyone ping and **starts it by itself** at that time. `?gamblin schedule 8pm` lets you pick the game with buttons. Times: `in 30m`, `in 1h30m`, `8pm`, `8:30pm`, `20:00` (clock times are in `LIMIT_RESET_TZ`, and mean the *next* time that clock shows; up to 24 hours ahead, up to 5 games). `?gamblin schedule list` shows what's coming and `?gamblin schedule cancel` clears it. The schedule is **saved to the database**, so a redeploy won't lose it. If another game is still running at the start time, the scheduled one waits and is dropped after 10 minutes (and logged).

All four games share the same rules of the house: button-only betting in private panels, stakes held until each result lands, `/slots` + `/ask` + `/grouptrivia` + autosave paused while a game runs (back automatically when it ends or is stopped), one game at a time, and one batched save to Upstash at the end. Sections 16 (Slots), 19 (Blackjack), 20 (Duck Race) and 21 (Roulette) describe each game.

**Leaderboard (all four games):** a player's *positive* session profit is added to their all-time "total earned" when the game ends (even if it's stopped early). Losses never reduce it, and a losing session adds nothing. It's counted per session so winning and losing back and forth can't inflate the board.

### Channel setup (so nobody can type)
In the Gamblin channel: deny **Send Messages** (and Create Threads) for `@everyone`, keep **View Channel**, **Read Message History** and **Use Application Commands**. Give **Milo** Send Messages, Embed Links, Manage Messages and **Mention Everyone** (needed for the `@everyone` ping). Buttons and the custom-bet popup still work for members; they just can't chat.

### How an event runs
1. **Lobby.** Milo posts `@everyone`, three empty reels `[ ❔ | ❔ | ❔ ]`, a **Join** button and a countdown. Each join gets a "`@name` is participating!" line. You need at least `GAMBLIN_MIN_BET` points to join. When the timer ends the lobby message is deleted.
2. **Rounds** (`GAMBLIN_ROUNDS`, default 3). One shared machine per round. Reel 1, 2 and 3 stop one at a time, with a betting window before each:
   - **🎰 Jackpot Spin** — one bet on the whole spin, placed *before reel 1*. Pick: a specific triple (3× 🍒 …), **Any triple**, or **Pair or better**.
   - **🎚️ Reel Rush** — bet on each reel separately: pick the symbol that reel will land on. Skip any reel you like.
   - You choose one mode per round. Once you lock a bet it can't be changed, and Jackpot Spin players simply wait while the reels stop.
3. **Betting is private.** Buttons open a panel only you can see: pick → stake (5% / 10% / 25% / All-in-or-Max / ✏️ Custom popup) → confirm → 🔒 Lock. Bets are `GAMBLIN_MIN_BET`–`GAMBLIN_MAX_BET`.
4. **Results** are posted after each reel (and the whole spin after reel 3), then standings at the end.

### What Milo pauses while it runs
`/slots`, `/ask` (+ the think buttons), `/grouptrivia` and the 5-minute autosave. `?updateDB` tells the manager to wait (or use `?gamblin stop`). Everything resumes automatically when it ends.

### The odds (computed from the real reel weights)
The Gamblin reels are weighted: 🍒 24% · 🍋 21% · 🍉 18% · 🍇 16% · ⭐ 12% · 7️⃣ 9%. Every multiplier is `0.92 ÷ chance`, so over a long time the house keeps about 8%. Reel Rush pays x3.8 (🍒) up to x10.2 (7️⃣). Jackpot Spin pays e.g. Pair or better x1.9, Any triple x25.9, 3× 🍒 x66.5, 3× ⭐ and 3× 7️⃣ capped at x500 (`GAMBLIN_MAX_MULT`). **The cap makes the two rarest triples worse value than the rest** — that's on purpose, so one lucky bet can't wreck the economy.

### Points and saving
- A stake is only *held* until its reel stops (it still shows in your balance, but you can't bet it twice). Then your real balance changes in memory.
- Balances move as each bet lands. Your positive session profit also counts towards the all-time leaderboard (see the Leaderboard note in the intro of this section); everything shows on **Richest right now**.
- When the event ends, Milo does **one batched save** to Upstash: about **1 command per player whose balance changed** (10 players ≈ 10 commands, a few % of 1 day's normal use). Nothing is written during the event.
- If Milo crashes mid-event, Upstash still holds the pre-event balances, so at worst the event's results are lost, never half-applied. On a normal shutdown (SIGTERM) Milo stops Gamblin, never charges unresolved bets, and saves what already landed.

### Limits (honest)
- A session lives only in memory: a restart ends it (buttons then say "that Gamblin session is over").
- Only one Gamblin at a time. Late joiners wait for the next one.
- Points are virtual only — never cash or prizes.


## 17. Manager toolbox & log channel

All of these are **hidden** `?` commands: Manager role only, silent to everyone else, and Milo deletes your command message so chat stays clean. They never appear in `/help`. (`?send` is unchanged.) Type `?mhelp` any time for the list.

| Command | What it does |
|---|---|
| `?give @user +300 pts` | Adds points. `?give @user -40 pts` removes them (it stops at 0, never negative). Add a note after it if you like: `?give @Archo -40 pts typo fix` — the note goes in the log. A user ID works instead of a mention. Limit 1,000,000 per command. |
| `?set @user 500` | Sets someone's balance to an exact number. |
| `?undo` | Reverses the most recent `?give` / `?set`. Run it again to step back further. Remembers the last 25 changes since Milo started. |
| `?check @user` | Balance, total earned, leaderboard rank, spins used today, and whether they have unsaved changes. |
| `?balances` | Everyone's current points, richest first. Shows in the channel and disappears after 2 minutes. |
| `?resetleaderboard` | Sets everyone's all-time **total earned** to their **current points** (balances don't change). Milo shows what will happen (how many people change, who will be on the board afterwards, the current top 5) with **Yes, reset it / Cancel** buttons that only you can press. After you confirm it saves to the database immediately and writes the old top 10 to the log channel. It can't be undone, and it's blocked while a Gamblin game is running. `?resetleaderbord` (typo) works too. |
| `?freeze @user [reason]` / `?unfreeze @user` / `?frozen` | A frozen person can't earn, gamble or trade: `/slots`, `/daily`, `/pay` (sending or receiving) and joining a Gamblin game are all refused with a friendly note. Saved to the database. Use it for abuse while you sort things out; `?frozen` lists everyone frozen. |
| `?announce Title \| message` | Posts a tidy embed as Milo. Add `--ping` to ping @everyone and/or a `#channel` to post elsewhere: `?announce --ping #general Movie night \| 8pm in voice!`. Without a `\|` the whole text is the message. Your command is deleted. (`?send` is still there for plain Milo messages.) |
| `?backup` | Posts a backup file (balances, earned, 777 counters, streaks, Gamblin stats, frozen list) to the **log channel**. Milo also posts one **every night** next to the points snapshot. Needs `LOG_CHANNEL_ID`. |
| `?restore` | Attach a backup file to your `?restore` message. Milo checks the file, shows what's in it and asks you to confirm. It then sets those numbers, saves immediately and logs it. Only people *in the file* are changed; the frozen list is set to match the file. Blocked while a Gamblin game runs. Run `?backup` first if you're unsure. |
| `?resetspins @user` | Gives someone today's free + bonus spins back (and clears their cooldown/pity). For when something glitched. |
| `?snapshot` | Posts everyone's points to the log channel right now. |
| `?updateDB` · `?gamblin` · `?rolemenu` · `?send` | Existing commands, unchanged. |

**What these change:** balances only. `?give` / `?set` never touch the all-time "total earned", so the top-earners leaderboard stays fair; they do show on **Richest right now**. Changes are saved to Upstash with the normal 5-minute autosave (or instantly with `?updateDB`). They're blocked while Gamblin is running so held bets can't be disturbed.

### The log channel (`LOG_CHANNEL_ID`, optional)
Make a private staff channel, give Milo **Send Messages + Embed Links** there, and set `LOG_CHANNEL_ID`. If it's empty, logging is simply off. Milo posts there:
- 🧾 every `?give` / `?set` / `?undo` (who did it, who got it, before → after, your note), plus `?resetspins` and `?updateDB`
- 🌙 **every night at the daily reset** (`LIMIT_RESET_TZ`, default IST): everyone's points, richest first, with earned totals and the grand total (split across several messages if there are many people)
- 🎰 Gamblin started / finished, with each player's profit or loss
- 🟢 / 🔴 Milo online (after points load) and shutting down
- ⚠️ if saving to Upstash fails twice in a row (and ✅ when it works again)

Cost: the log channel uses **0 Upstash commands** — it's all from memory.


## 18. Choosing the /join games (`JOIN_GAMES`)

`/join` used to be hard-wired to VBL and Minecraft. Now it reads the list from one env var, the same style as `SELF_ROLES`:

```
JOIN_GAMES=ROLE_ID|Name|Emoji|Call to action,ROLE_ID|Name|Emoji|Call to action
```

- **ROLE_ID** and **Name** are required. The name is what members see in the `/join` dropdown (and in `/help`).
- **Emoji** (optional) appears before the ping, e.g. `🏐`. Custom emoji like `<:gg:123456789012345678>` work.
- **Call to action** (optional) is the last line, default `Join now!`. Don't put commas in it.
- Up to 25 games. Example: `JOIN_GAMES=1532265584069185597|VBL|🏐|Join the lobby now!,1536962762952146944|Minecraft||Join the Server now!,123456789012345678|Valorant|🎯|Queue up!`
- **Leave it unset and nothing changes**: you still get VBL and Minecraft with the exact same messages.

Only the list changed. The ping rules are the same as before (roles and users can be pinged, never `@everyone`), the 30-second cooldown is the same, and the ✔️ Interested button works the same way. After changing `JOIN_GAMES`, redeploy so Discord's dropdown refreshes. Each role still needs "Allow anyone to @mention this role" on (Section 6).


## 19. Blackjack Gamblin 🃏

Start it with **`?gamblin`** and pick **🃏 Blackjack** (or type `?gamblin bj`). `?gamblin stop` ends it. It runs in `GAMBLIN_CHANNEL_ID`, button-only, with the same channel setup as Section 16, and pauses exactly what every Gamblin game pauses. Only one game runs at a time.

### How a game runs
1. **Lobby:** `@everyone` ping, Join button, countdown (same as Gamblin).
2. **Rounds** (`GAMBLIN_BJ_ROUNDS`, default 5), each against the dealer:
   - **Betting window** (30s): press **💰 Place bet**, pick a stake privately (5% / 10% / 25% / All-in or Max / ✏️ Custom popup), confirm, 🔒 lock. Late joiners can sit down any round.
   - **Deal:** everyone's cards are public on the table message; the dealer shows one card.
   - **Your move** (40s): press **🃏 My hand** for a private panel with **Hit**, **Stand** and **Double** (first two cards only). Idle players auto-stand. The round moves on as soon as everyone has finished.
   - **Dealer** reveals and draws to 17, then every bet is settled and shown on the table.
3. After the last round (or if nobody bets for two rounds in a row) you get the final standings.

### Rules and payouts
6 decks, fresh shoe every round. Dealer **stands on all 17s**. Win pays **1:1**, **blackjack pays 3:2** (rounded down), push returns your stake, bust loses. If the dealer has blackjack the round ends immediately (you can't double into it; your own blackjack pushes). **No split, insurance or surrender.** Bets use the same `GAMBLIN_MIN_BET` / `GAMBLIN_MAX_BET` as Gamblin. Doubling can go over the max bet (it just doubles your stake).

### Leaderboard
Your **positive session profit is added to your all-time "total earned"** when the game ends (even if it's stopped early). Same rule as every Gamblin game (see Section 16).

### Points and saving
Same as Gamblin: stakes are held (not deducted) until the hand settles, nothing is written to Upstash during the game, and one batched save happens at the end (about 1 command per player whose balance or earned changed). A crash mid-game means Upstash still has the pre-game numbers. A redeploy stops the table cleanly and never charges unsettled hands.

### Limits
Sessions live in memory (a restart ends it). Points are virtual only.


## 20. Duck Race 🦆

Pick **🦆 Duck Race** in `?gamblin`. **3 races** by default (`GAMBLIN_DUCK_ROUNDS`).

**Each race:**
1. **Betting (30s):** Milo posts the line-up of six ducks 🟥 Quackers · 🟦 Sir Waddles · 🟩 Puddles · 🟨 Drake · 🟪 Beaky · 🟧 Feathers with each one's payout and win chance. The odds are **shuffled between ducks every race**, so yesterday's favourite isn't always today's. Press **💰 Place bet**, pick a duck, pick a stake (5% / 10% / 25% / All-in or Max / ✏️ Custom popup), confirm, 🔒 lock. **One bet per race.** Newcomers can join any race.
2. **The race:** the message animates with ducks waddling across the track to the 🏁 (about 15 seconds).
3. **Results:** podium, then every bet with its win or loss.

**The odds:** win chances are 28% / 22% / 18% / 14% / 10% / 8% (shuffled across the ducks). Payout is about `0.92 ÷ chance`, shown in the line-up (roughly x3.2 up to x11.5). The race is drawn so each duck's chance of winning is *exactly* the stated chance (I tested it with 60,000 simulated races). If nobody bets for two races in a row, the track closes early.

**Settings:** `GAMBLIN_DUCK_ROUNDS`, `GAMBLIN_DUCK_BET_SECONDS`, plus the shared `GAMBLIN_MIN_BET` / `GAMBLIN_MAX_BET` / `GAMBLIN_MAX_PLAYERS`.


## 21. Roulette 🎡

Pick **🎡 Roulette** in `?gamblin`. **4 spins** by default (`GAMBLIN_RL_ROUNDS`). It's a real **European wheel**: numbers 0–36, one green 0.

**Each spin:**
1. **Betting (40s):** press **💰 Place bets**. You can place **up to 5 bets per spin**, one after another (the panel has an **➕ Add another bet** button).
2. **Bet types and payouts:**

| Bet | Wins on | Pays |
|---|---|---|
| 🔴 Red / ⚫ Black | red / black numbers | **x2** |
| Odd / Even | odd / even numbers | **x2** |
| 1–18 / 19–36 | low / high | **x2** |
| 1st / 2nd / 3rd dozen | 1–12 / 13–24 / 25–36 | **x3** |
| 🎯 Number | one exact number 0–36 (you type it in a popup) | **x36** |

3. **Spin:** the wheel animates, the ball lands, and every bet is settled and shown. The last 10 numbers are shown on the table.

**The odds are the real ones**, not tuned: every bet returns 36/37 = **97.3%** in the long run (the house edge is the green 0, and 0 loses all red/black/odd/even/low/high/dozen bets). I checked all 37 numbers for every bet type.

**Settings:** `GAMBLIN_RL_ROUNDS`, `GAMBLIN_RL_BET_SECONDS`, plus the shared bet limits. The most you can have staked in one spin is limited by your free points (each bet is capped by `GAMBLIN_MAX_BET`, up to 5 bets).


## 22. Daily rewards, paying friends, stats, and safety tools

### `/daily`
Once a day (resets at midnight in `LIMIT_RESET_TZ`). Reward = `DAILY_BASE` + `DAILY_STREAK_BONUS` for each extra streak day up to `DAILY_STREAK_CAP`, and a `DAILY_WEEK_BONUS` on every 7th day in a row. With the defaults the first week pays 15, 20, 25, 30, 35, 40, **95**, then 45 a day with another +50 every 7th day. Skip a day and the streak starts over at 1 (Milo tells you when that happens). Daily points count as **earned**, so they count towards the leaderboard. The streak is saved to the database.

### `/pay @user amount`
Private confirm step first, then a public "X sent N points to Y" message that pings the receiver. Each person can send up to `PAY_DAILY_CAP` (default 200) a day; the amount sent today is saved, so restarting Milo doesn't reset it. Moving points between people never changes anyone's "total earned". Every payment is written to the log channel. It's refused for frozen people, bots, yourself, and while a Gamblin game is running (balances are held during games).

### `/gamblinstats`
Gamblin results are tallied when a game ends and saved: games played, total profit, points wagered, biggest single win, best single game, and favourite game. The hall of fame shows the record holders. People with no games yet just see a note.

### The safety net, in order of how often you'll need it
1. **Mistake with `?give`/`?set`?** `?undo`.
2. **Someone abusing something?** `?freeze @user`.
3. **A bad deploy or a data mix-up?** `?restore` with last night's backup file from the log channel. (Upstash's free plan has no backups of its own, so this file is your copy.)
4. **Want a copy right now?** `?backup`.

Keep the log channel private: backup files contain member IDs and point totals.
