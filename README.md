# 67 Bot

## [Invite to your server](https://discord.com/oauth2/authorize?client_id=1509040704406556682)

**67 Bot** is a multi-purpose Discord bot focused on moderation, community engagement, economy, voice/music, optional AI replies, and per-guild customization.

Built with **Python 3** and **discord.py** (slash commands, Components V2 where used, persistent SQLite data).

---

## Features

### Core & moderation
- Slash-based moderation: mute / unmute / kick / ban / unban / warn (DM)
- Role add/remove helpers
- **Auto Mute** (blocked words + timeouts via Discord AutoMod integration)
- Welcome / goodbye messaging
- Manual bot messages (`/manualmsg`) with optional AI-style watermark

### Levels & “67”
- XP / level system with configurable role rewards
- Tracks how often members say **67**
- `/level`, `/setlevel`, `/random67`

### Streaks
- Daily message streaks with optional role / nickname rewards
- Leaderboard + personal view (`/streaks`)
- Admin override (`/setstreaks`)

### Economy
- Daily claim, work, pay, rob
- Balance + leaderboard (`/ecobalance`)
- Admin balance tools (`/setbalance`)

### Voice & music
- Stay in a voice channel (`/afkvoice`)
- YouTube playback (`/play`, queue, skip, pause, resume, stop)
- Bonus: `/nggyu` (single rickroll)

### Utility
- **Server stats** voice channels (members / bots / bans / mutes) via `/setserverstats`
- **Enlarge emoji / sticker** with Components V2 containers (`/enlargeemoji`, `/enlargesticker`)
- Optional **Auto Reaction**: case-insensitive text triggers → emoji reactions (unicode or `<:name:id>`)
- **Time Message**: scheduled channel posts with timezone-aware setup

### 67+AI
- Mention- or channel-based AI replies (DMs without `@`)
- Multi-provider fallback chain (e.g. Kimi → Gemini → Groq)
- Optional web context via Tavily when configured
- Agent tools for moderators (mute, kick, roles, economy, etc.), always gated by the **invoker’s** permissions
- Per-guild toggles: enable/disable AI, “only on selected channel”

### Paid / premium gates (owner whitelist)
- **67+profile**: per-server bot nickname, bio, avatar, banner
- Other feature keys (e.g. silent-related) managed with `/addpaidserver` & `/removepaidserver`

### Settings hub
- `/settings` panel for Welcome, Levels, Streaks, Counting, Auto Mute, Time Message, Profile, 67+AI, Auto Reaction, and more
