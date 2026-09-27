# Da Bot

A simple, single-server Discord bot built with **Python 3.12+** and **discord.py 2.x**.

Features:
- Prefix commands: `?creator`, `?join`, `?help`
- A persistent **✅ Interested** button on `?join` messages — survives bot restarts, **no database required**
- Runs as a **Render Free Web Service** (Discord Gateway + a tiny HTTP health server in one process)
- Secrets stay in environment variables, never in code

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

You should see log lines like `Logged in as Da Bot#1234`, `Connected to 1 guild(s).`, and `HTTP health server listening on 0.0.0.0:10000`.

---

## 3. Discord Developer Portal setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. Open the **Bot** tab → click **Add Bot**.
3. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** — required because this bot reads `?` prefix commands out of message text. Discord treats this as a privileged intent, so it must be turned on here *and* in the code (already done in `bot.py`).
4. Click **Reset Token** (or **Copy**) to get your bot token. Put it in `.env` as `DISCORD_TOKEN`. **Never share this token or commit it to GitHub.**
5. Go to **OAuth2 → URL Generator**:
   - Scopes: check **bot**.
   - Bot Permissions: at minimum **Send Messages**, **Read Message History**, **Use Slash/Application Commands** (used internally for buttons), **Embed Links**.
6. Copy the generated URL, open it in your browser, and invite the bot to your one server.

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
9. Watch the **Logs** tab for `Logged in as ...` and `Da Bot is ready.`
10. Check that the bot shows as **online** in your Discord server's member list.

### Free tier realities (please read)

- Render Free Web Services can **spin down after ~15 minutes** of no incoming HTTP/WebSocket traffic, and take roughly a minute to spin back up.
- Render can restart the service at any time.
- Free services get **750 instance hours per calendar month**.
- The filesystem is **ephemeral** — nothing written to disk survives a restart, redeploy, or spin-down. This is exactly why the bot has **no database and no local persistent files**; the join-author's ID is recovered from the button's `custom_id` instead.
- This bot is **not guaranteed to be online 24/7** on the Free plan, and it does not use any hacky "keep-alive" tricks to fake that.

---

## 5. How the button persistence works (no database)

When someone runs `?join`, the bot attaches an **✅ Interested** button whose `custom_id` is `join_interested:<AUTHOR_USER_ID>` (well under Discord's 100-character limit). A global `on_interaction` listener reads that ID back out whenever *anyone* clicks the button — including after the bot has restarted — and mentions both the clicker and the original author. No state is ever stored outside the message itself.

---

## 6. Command reference

| Command | Description |
|---|---|
| `?creator` | Replies "aapoke made me" with `aapoke` as a clickable link to the creator's profile. |
| `?join game:vbl` | Posts a VBL looking-for-players message with an Interested button. |
| `?join game:minecraft` | Posts a Minecraft looking-for-players message with an Interested button. |
| `?join game:vbl message:your text` | Same as above, with an optional custom message appended. |
| `?help` | Shows the command list. |

Game matching is case-insensitive (`vbl`, `VBL`, `VbL` all work) and whitespace is trimmed.

---

## 7. Testing checklist

- [ ] `?creator` replies with a clickable "aapoke" link.
- [ ] `?join game:vbl` posts the VBL message with the correct role mention and an Interested button.
- [ ] `?join game:vbl message:Need 2 more players` appends the custom message on its own line.
- [ ] `?join game:minecraft` posts the Minecraft message with the correct role mention.
- [ ] `?join game:minecraft message:Starting at 8 PM` appends the custom message.
- [ ] `?join` (no arguments) replies with a usage message, no crash.
- [ ] `?join game:valorant` replies that only VBL and Minecraft are supported, no crash.
- [ ] Clicking **✅ Interested** posts `<@clicker> is joining <@author>` using real mentions.
- [ ] Restart the bot process, then click **Interested** on an *old* message — it still works correctly.
- [ ] Clicking Interested on your own `?join` message gives a friendly "can't join your own request" reply.
- [ ] `?help` shows all commands with no internal details, tokens, or env var names.
- [ ] Running a command from a different server (if `ALLOWED_GUILD_ID` is set) is silently ignored.
- [ ] Visiting `https://your-render-url.onrender.com/` shows `Da Bot is running.`
- [ ] Visiting `https://your-render-url.onrender.com/health` returns `OK` with HTTP 200.

---

## 8. Limitations / assumptions

- Discord only renders `[text](url)` markdown as a clickable link **inside embeds**, not in plain message content, so `?creator` sends a small embed to make "aapoke" clickable as requested.
- The wrong-server guard (`ALLOWED_GUILD_ID`) is configured to **silently ignore** commands from other servers; interaction (button) attempts from another server get a short ephemeral "not configured for this server" reply instead, since that only the clicking user sees it.
- Clicking your own **Interested** button is blocked with a friendly ephemeral message — this wasn't explicitly requested but avoids a slightly odd `<@you> is joining <@you>` message; remove that check in `bot.py` if you'd rather allow it.
- `AUTO_RESPONSES` in `bot.py` is an empty dictionary by design — it's there so you can add future automatic replies (e.g. `"hello": "Hello!"`) without restructuring the bot, but no extra responses were added since none were requested.
- Custom `?join` messages are capped at 300 characters as a simple safety limit; Discord's own per-message limit is 2000 characters.
- On Render's Free plan, expect spin-down after ~15 minutes of inactivity and a slower "cold start" reconnect afterward — this is a Render platform limitation, not a bug in the bot.
