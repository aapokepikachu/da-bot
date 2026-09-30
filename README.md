# Milo

A simple, single-server Discord bot built with **Python 3.12+** and **discord.py 2.x**.

Features:
- Slash commands: `/creator`, `/join`, `/help` — Discord auto-suggests the `game` choice (VBL/Minecraft only) and the optional `message` field, so nobody has to type raw text like `game:vbl`
- A persistent **✅ Interested** button on `/join` messages — survives bot restarts, **no database required**
- Runs as a **Render Free Web Service** (Discord Gateway + a tiny HTTP health server in one process)
- Secrets stay in environment variables, never in code
- Does **not** require the privileged "Message Content" intent, since slash commands and buttons don't need it

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

Copy the example file and fill in real values:

```bash
cp .env.example .env
```

Edit `.env`:

```
DISCORD_TOKEN=your_bot_token_here
CREATOR_USER_ID=your_discord_user_id_here
ALLOWED_GUILD_ID=your_server_id_here
```

- **DISCORD_TOKEN** — from the Discord Developer Portal (see section 3).
- **CREATOR_USER_ID** — your numeric Discord user ID (enable Developer Mode in Discord, then right-click your profile → "Copy User ID").
- **ALLOWED_GUILD_ID** — your server's numeric ID (right-click the server icon → "Copy Server ID"). Leave blank to allow any server.

### 2.5 Run the bot

```bash
python bot.py
```

You should see log lines like `Logged in as Milo#1234`, `Connected to 1 guild(s).`, and `HTTP health server listening on 0.0.0.0:10000`.

---

## 3. Discord Developer Portal setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. Open the **Bot** tab → click **Add Bot**.
3. No privileged intents need to be enabled for this bot — slash commands and button clicks arrive as structured interaction data, not raw message text, so **Message Content Intent** is not required. (If you later add text-based `AUTO_RESPONSES` in `bot.py`, you'll need to flip `intents.message_content = True` in code *and* enable "Message Content Intent" here.)
4. Click **Reset Token** (or **Copy**) to get your bot token. Put it in `.env` as `DISCORD_TOKEN`. **Never share this token or commit it to GitHub.**
5. Go to **OAuth2 → URL Generator**:
   - Scopes: check **bot** and **applications.commands** (the second one is required for slash commands to register).
   - Bot Permissions: at minimum **Send Messages**, **Embed Links**.
6. Copy the generated URL, open it in your browser, and invite the bot to your one server.
7. After the bot logs in for the first time, it registers its slash commands automatically (see `setup_hook` in `bot.py`). If `ALLOWED_GUILD_ID` is set, the commands appear in that server almost instantly; without it, a global sync can take up to an hour to show up everywhere.

---

## 4. Render setup (Free Web Service)

1. Push this project to a **GitHub repository**.
2. Go to [Render](https://render.com/) and create a **New Web Service**.
3. **Connect your GitHub repository**.
4. Select the **Free** instance plan.
5. **Build command:**
   ```
   pip install -r requirements.txt
   ```
6. **Start command:**
   ```
   python bot.py
   ```
7. Add **Environment Variables** in Render's dashboard (Environment tab):
   - `DISCORD_TOKEN`
   - `CREATOR_USER_ID`
   - `ALLOWED_GUILD_ID` (optional)
   - Render provides `PORT` automatically — you don't need to set it.
8. Click **Deploy**.
9. Watch the **Logs** tab for `Logged in as ...` and `Milo is ready.`
10. Check that the bot shows as **online** in your Discord server's member list.

### Free tier realities (please read)

- Render Free Web Services can **spin down after ~15 minutes** of no incoming HTTP/WebSocket traffic, and take roughly a minute to spin back up.
- Render can restart the service at any time.
- Free services get **750 instance hours per calendar month**.
- The filesystem is **ephemeral** — nothing written to disk survives a restart, redeploy, or spin-down. This is exactly why the bot has **no database and no local persistent files**; the join-author's ID is recovered from the button's `custom_id` instead.
- This bot is **not guaranteed to be online 24/7** on the Free plan, and it does not use any hacky "keep-alive" tricks to fake that.

---

## 5. How the button persistence works (no database)

When someone runs `/join`, the bot attaches an **✔️ Interested** button (green, Discord's "success" style) whose `custom_id` is `join_interested:<AUTHOR_USER_ID>` (well under Discord's 100-character limit). A global `on_interaction` listener reads that ID back out whenever *anyone* clicks the button — including after the bot has restarted — and mentions both the clicker and the original author. No state is ever stored outside the message itself.

## 6. If the VBL/Minecraft role isn't notifying anyone

The bot's code already sends the role mention with `allowed_mentions=discord.AllowedMentions(roles=True, ...)`, so it isn't being suppressed on the code side. If the role still shows up as a mention but doesn't actually ping/notify members, it's almost always one of these **server-side settings**, not a code problem:

1. **The role isn't mentionable.** Go to **Server Settings → Roles → (the VBL or Minecraft role) → Display role members separately** section, and toggle **"Allow anyone @mention this role"** ON.
2. **Or, give Milo's own role the permission to bypass that.** Go to **Server Settings → Roles → Milo's role**, and enable **"Mention @everyone, @here, and All Roles"**. This lets the bot ping any role even if it isn't mentionable by regular members.

Either one fixes it — you don't need both. Option 2 is usually cleaner since it doesn't open the role up to being pinged by everyone else too.

---

## 7. Command reference

| Command | Description |
|---|---|
| `/creator` | Shows an intro for Milo (with the `:Milo:` emoji) and a clickable "AAPoke" link to the creator's profile. |
| `/join game:VBL` | Posts a VBL looking-for-players message with an Interested button, and notifies the VBL role. |
| `/join game:Minecraft` | Posts a Minecraft looking-for-players message with an Interested button, and notifies the Minecraft role. |
| `/join game:<choice> message:your text` | Same as above, with an optional custom message appended. |
| `/ping` | Replies with Milo's current Discord Gateway latency in milliseconds. |
| `/help` | Shows the command list. |

`game` is a dropdown with exactly two options (VBL, Minecraft) that Discord shows automatically — there's no way to type an invalid game. `message` is an optional free-text field.

---

## 8. Testing checklist

- [ ] `/creator` replies with an embed: "Hi, I'm Milo! 🐾", a group-management blurb, and a clickable "AAPoke" link.
- [ ] `/join game:VBL` posts the VBL message with the correct role mention (and members of that role actually get notified) plus a green ✔️ Interested button.
- [ ] `/join game:VBL message:Need 2 more players` appends the custom message on its own line.
- [ ] `/join game:Minecraft` posts the Minecraft message with the correct role mention and notification.
- [ ] `/join game:Minecraft message:Starting at 8 PM` appends the custom message.
- [ ] Typing `/join` shows Discord's built-in dropdown for `game` (only VBL/Minecraft) and an optional `message` field — no invalid game can be typed.
- [ ] `/ping` replies with a latency in milliseconds.
- [ ] Clicking **✔️ Interested** posts `<@clicker> is joining <@author>` using real mentions.
- [ ] Restart the bot process, then click **Interested** on an *old* message — it still works correctly.
- [ ] Clicking Interested on your own `/join` message gives a friendly "can't join your own request" reply.
- [ ] `/help` shows all commands with no internal details, tokens, or env var names.
- [ ] Running a command from a different server (if `ALLOWED_GUILD_ID` is set) is silently ignored.
- [ ] Visiting `https://your-render-url.onrender.com/` shows `Milo is running.`
- [ ] Visiting `https://your-render-url.onrender.com/health` returns `OK` with HTTP 200.

---

## 9. Limitations / assumptions

- Discord only renders `[text](url)` markdown as a clickable link **inside embeds**, not in plain message content, so `/creator` sends a small embed to make "AAPoke" clickable as requested.
- `MILO_EMOJI` defaults to a plain 🐾 emoji if you don't set it. To show your server's actual custom `:Milo:` emoji, set the env var to its raw code (see `.env.example` for how to grab it).
- The wrong-server guard (`ALLOWED_GUILD_ID`) is configured to **silently ignore** commands from other servers; interaction (button) attempts from another server get a short ephemeral "not configured for this server" reply instead, since that only the clicking user sees it.
- Clicking your own **Interested** button is blocked with a friendly ephemeral message — this wasn't explicitly requested but avoids a slightly odd `<@you> is joining <@you>` message; remove that check in `bot.py` if you'd rather allow it.
- `AUTO_RESPONSES` in `bot.py` is an empty dictionary by design — it's there so you can add future automatic replies (e.g. `"hello": "Hello!"`) without restructuring the bot, but no extra responses were added since none were requested.
- Custom `/join` messages are capped at 300 characters as a simple safety limit; Discord's own per-message limit is 2000 characters.
- On Render's Free plan, expect spin-down after ~15 minutes of inactivity and a slower "cold start" reconnect afterward — this is a Render platform limitation, not a bug in the bot.
