from dotenv import load_dotenv
load_dotenv()

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import datetime
import logging
import os
import random
import re
import json
import sqlite3
import aiohttp
import asyncio
import time  
from typing import Optional
from openai import AsyncOpenAI 

# =================================================================
# ⚙️ 1. GLOBAL BOT CONFIGURATIONS (全局設定)
# =================================================================
WATCHING_STATUSES = [
    "67",
    "/settings",
    "Six Seven",
    "24/7 Auto Mute",
    "Introducing 67+AI"
]

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SixSevenBot")

# =================================================================
# 🤖 AI 用戶端初始化 (保留 Gemini 直連與 Groq 備援設定)
# =================================================================

# 1. ✨ 直連 Google Gemini 客戶端 (利用 OpenAI 相容端點語法)
gemini_client = AsyncOpenAI(
    api_key=os.getenv("GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

# 2. Groq 客戶端 (作為最終防線)
groq_client = AsyncOpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

# 3. Kimi（Moonshot）客戶端（第一防線；中國模型，禁止碰政治話題，見下方 POLITICAL_KEYWORDS）
kimi_client = AsyncOpenAI(
    api_key=os.getenv("KIMI_API_KEY"),   # 或你自己的 KIMI_API_KEY
    base_url="https://api.moonshot.ai/v1"   # 國際站改成 https://api.moonshot.ai/v1
)

# 🚫 政治相關關鍵字，命中就直接跳過 GLM 這個防線，不會讓這個模型碰到政治話題
POLITICAL_KEYWORDS = [
    "政治", "政黨", "選舉", "總統", "首相", "主席", "立法院", "國會", "民主", "獨裁",
    "台獨", "台灣獨立", "統一", "一國兩制", "共產黨", "國民黨", "民進黨", "習近平",
    "六四", "天安門", "新疆", "西藏", "香港獨立", "反送中", "法輪功", "人權", "示威",
    "抗議", "革命", "政變", "戰爭", "軍事衝突", "制裁",
    "politics", "political", "election", "president", "prime minister", "government policy",
    "democracy", "dictatorship", "communist party", "taiwan independence", "tiananmen",
    "xinjiang", "tibet", "hong kong independence", "human rights", "protest", "coup", "sanctions",
]

def is_political_topic(text: str) -> bool:
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in POLITICAL_KEYWORDS)

# =================================================================
# 🕵️ 67+Agent：AI 可呼叫的管理工具
# =================================================================
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "mute_member",
            "description": (
                "Timeout (mute) a member. Use this not just when explicitly asked, but ALSO whenever the "
                "conversation clearly describes someone being disruptive right now (e.g. spamming, being annoying, "
                "being toxic, won't stop talking, being rude). Use your own judgement — you don't need the exact "
                "word 'mute'. Default to a short duration like '10m' for minor annoyance, longer for worse behavior."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member to mute. Pick from the 'Available target candidates' list."},
                    "duration": {"type": "string", "description": "Mute duration like '10m', '1h', '2d'. Default '10m' if not specified."},
                    "reason": {"type": "string", "description": "Reason for the mute, inferred from context if not stated."}
                },
                "required": ["user_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "unmute_member",
            "description": "Remove an active timeout/mute from a member. Use when asked to unmute, forgive, or let someone talk again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member to unmute."}
                },
                "required": ["user_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kick_member",
            "description": "Kick a member from this server. Use when the conversation implies someone should be removed but not permanently banned (e.g. a one-off serious incident).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member to kick."},
                    "reason": {"type": "string", "description": "Reason for the kick, inferred from context if not stated."}
                },
                "required": ["user_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ban_member",
            "description": "Ban a member from this server. Use when the conversation implies someone did something severe enough to be permanently removed (raiding, serious harassment, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member to ban."},
                    "reason": {"type": "string", "description": "Reason for the ban, inferred from context if not stated."}
                },
                "required": ["user_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "warn_member",
            "description": "Send a formal warning to a member, posted in the channel and DMed to them. Use for behavior that deserves a documented warning but not a mute/kick/ban.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member to warn."},
                    "warn_message": {"type": "string", "description": "The warning message content, write it yourself based on the context."}
                },
                "required": ["user_id", "warn_message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_role",
            "description": "Give a role to a member. Use when asked to add/give someone a role, or promote them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member."},
                    "role_id": {"type": "string", "description": "The Discord role ID or mention (e.g. <@&123456789012345678>) to give."}
                },
                "required": ["user_id", "role_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_role",
            "description": "Remove a role from a member. Use when asked to remove/take away someone's role, or demote them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member."},
                    "role_id": {"type": "string", "description": "The Discord role ID or mention to remove."}
                },
                "required": ["user_id", "role_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_balance",
            "description": "Set a member's economy balance to a specific amount. Use when asked to give/set someone's money/balance/coins.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member."},
                    "amount": {"type": "integer", "description": "The new balance amount (must be >= 0)."}
                },
                "required": ["user_id", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_level",
            "description": "Set a member's level to a specific number. Use when asked to change/give someone a level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member."},
                    "level": {"type": "integer", "description": "The new level (must be >= 1)."}
                },
                "required": ["user_id", "level"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_streaks",
            "description": "Set (flood or reduce) a member's Streaks day count. Use when asked to give/reset/fix someone's streak days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The Discord user ID of the member."},
                    "days": {"type": "integer", "description": "The new streak day count (must be >= 0)."}
                },
                "required": ["user_id", "days"]
            }
        }
    },
]


def _resolve_target_id(raw: str) -> str | None:
    raw = str(raw).strip()
    m = re.match(r"^<@!?(\d+)>$", raw)
    if m:
        return m.group(1)
    m2 = re.match(r"^<@&(\d+)>$", raw)  # 身分組也可能長這樣被誤傳進來，一併處理
    if m2:
        return m2.group(1)
    return raw if raw.isdigit() else None


async def build_target_candidates(message: discord.Message) -> str:
    """把這則訊息裡 @ 到的人、還有回覆鏈裡出現過的人，整理成一份「候選名單」給 AI 參考，
    避免 AI 在完全沒有明確 ID 可用時用猜的（猜錯就會直接被 execute_agent_tool 擋下來）。"""
    candidates = {}
    for m in message.mentions:
        if not m.bot:
            candidates[m.id] = m.name

    if message.reference and message.reference.message_id:
        ref_author = None
        # 🎯 先看快取裡有沒有，快取沒有的話（常見情況）主動去 Discord 抓，不能只靠 .resolved
        if isinstance(message.reference.resolved, discord.Message):
            ref_author = message.reference.resolved.author
        else:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                ref_author = ref_msg.author
            except Exception as e:
                logger.warning(f"⚠️ [候選名單] 抓不到被回覆的訊息: {e}")

        if ref_author and not ref_author.bot:
            candidates[ref_author.id] = ref_author.name

    if not candidates:
        return "Available target candidates: (none mentioned in this message — if the user didn't @ mention anyone, ask them to @ mention the target instead of guessing.)"
    lines = [f"- {name} (user_id: {uid})" for uid, name in candidates.items()]
    return "Available target candidates:\n" + "\n".join(lines)


async def execute_agent_tool(tool_name: str, args: dict, invoker: discord.Member, guild: discord.Guild) -> tuple:
    """執行 Agent 決定呼叫的工具，回傳 (embed 或 None, 錯誤訊息或 None)。一律用 invoker 本人的權限驗證。"""
    target_id = _resolve_target_id(args.get("user_id", ""))
    if not target_id:
        return None, "❌ Agent couldn't figure out who you meant. Please @ mention the target member clearly."

    target = guild.get_member(int(target_id))
    if not target:
        return None, "❌ That user isn't in this server."
    if target.id == invoker.id and tool_name in ("mute_member", "kick_member", "ban_member", "warn_member"):
        return None, "❌ You can't target yourself."
    if target.id == bot.user.id:
        return None, "❌ You can't target me."
    if target.id == guild.owner_id and tool_name in ("mute_member", "kick_member", "ban_member"):
        return None, "❌ Bro don't do that. I don't wnat to be fired."

    bot_member = guild.me

    if tool_name == "mute_member":
        if not invoker.guild_permissions.moderate_members:
            return None, "❌ You don't have permission to timeout members."
        duration_str = args.get("duration") or "10m"
        delta, err = parse_mute_duration(duration_str)
        if err:
            return None, f"❌ {err}"
        if target.top_role >= bot_member.top_role:
            return None, f"❌ I can't mute **{target.display_name}**, their role is higher than or equal to mine."
        reason = args.get("reason") or "None"
        try:
            if delta:
                await target.timeout(delta, reason=reason)
            embed = discord.Embed(
                title=parse_placeholders("✅ {user.name} has been muted.", target, guild),
                color=0x2ecc71,
                description=f"Time: {duration_str}\nReason: {reason}"
            )
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "unmute_member":
        if not invoker.guild_permissions.moderate_members:
            return None, "❌ You don't have permission to unmute members."
        try:
            await target.timeout(None)
            embed = discord.Embed(
                title=parse_placeholders("✅ {user.name} has been unmuted.", target, guild),
                color=0x2ecc71
            )
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "kick_member":
        if not invoker.guild_permissions.kick_members:
            return None, "❌ You don't have permission to kick members."
        if target.top_role >= bot_member.top_role:
            return None, f"❌ I can't kick **{target.display_name}**, their role is higher than or equal to mine."
        reason = args.get("reason") or "None"
        try:
            await target.kick(reason=reason)
            await send_goodbye_message(target, guild)
            embed = discord.Embed(title=parse_placeholders("✅ {user.name} has been kicked.", target, guild), color=0xe74c3c, description=parse_placeholders("Reason: {reason}", target, guild, extra={"reason": reason}))
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "ban_member":
        if not invoker.guild_permissions.ban_members:
            return None, "❌ You don't have permission to ban members."
        if target.top_role >= bot_member.top_role:
            return None, f"❌ I can't ban **{target.display_name}**, their role is higher than or equal to mine."
        reason = args.get("reason") or "None"
        try:
            await guild.ban(target, reason=reason)
            await send_goodbye_message(target, guild)
            embed = discord.Embed(
                title=parse_placeholders("✅ {user.name} has been banned.", target, guild),
                color=0xe74c3c,
                description=parse_placeholders(
                    "Reason: {reason}", target, guild, extra={"reason": reason}
                ),
            )
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "warn_member":
        if not invoker.guild_permissions.moderate_members:
            return None, "❌ You don't have permission to warn members."
        warn_msg = (args.get("warn_message") or "").strip()
        if not warn_msg:
            return None, "❌ No warning message provided."
        embed = discord.Embed(
            title="⚠️ Warn",
            color=0xff8500,
            description=(
                f"`{target.name}` got warned by `{invoker.name}`\n"
                f"Warn message:\n```\n{warn_msg}\n```"
            ),
        )
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "warn_member":
        if not invoker.guild_permissions.moderate_members:
            return None, "❌ You don't have permission to warn members."
        warn_msg = (args.get("warn_message") or "").strip()
        if not warn_msg:
            return None, "❌ No warning message provided."
        embed = discord.Embed(
            title="⚠️ Warn",
            color=0xff8500,
            description=(
                f"`{target.name}` got warned by `{invoker.name}`\n"
                f"Warn message:\n```\n{warn_msg}\n```"
            ),
        )
        embed.set_footer(text=f"{guild.name}｜67")
        dm_id = None
        try:
            dm_msg = await target.send(
                view=build_warn_dm_view(guild.name, invoker, warn_msg)
            )
            dm_id = str(dm_msg.id)
        except discord.Forbidden:
            pass
        cfg = ensure_warn_settings(guild)
        if cfg["dashboard_enabled"] and target.id != invoker.id:
            insert_warn(
                guild.id, target.id, invoker.id, warn_msg, dm_message_id=dm_id
            )
        return embed, None

    elif tool_name == "add_role":
        if not invoker.guild_permissions.administrator:
            return None, "❌ You don't have permission to manage roles."
        role_id = _resolve_target_id(args.get("role_id", ""))
        role = guild.get_role(int(role_id)) if role_id else None
        if not role:
            return None, "❌ Couldn't find that role."
        if role >= bot_member.top_role:
            return None, f"❌ I can't manage **{role.name}**, it's higher than or equal to my own role."
        try:
            await target.add_roles(role, reason=f"Added by Agent on behalf of {invoker}")
            embed = discord.Embed(title=f"✅ Added {role.mention} to {target.mention}", color=0x2ecc71)
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "remove_role":
        if not invoker.guild_permissions.administrator:
            return None, "❌ You don't have permission to manage roles."
        role_id = _resolve_target_id(args.get("role_id", ""))
        role = guild.get_role(int(role_id)) if role_id else None
        if not role:
            return None, "❌ Couldn't find that role."
        if role >= bot_member.top_role:
            return None, f"❌ I can't manage **{role.name}**, it's higher than or equal to my own role."
        try:
            await target.remove_roles(role, reason=f"Removed by Agent on behalf of {invoker}")
            embed = discord.Embed(title=f"✅ Removed {role.mention} from {target.mention}", color=0x2ecc71)
            embed.set_footer(text=f"{guild.name}｜67")
            return embed, None
        except discord.Forbidden:
            return None, "❌ Call any moderator to give me a higher privileges."

    elif tool_name == "set_balance":
        if not invoker.guild_permissions.administrator:
            return None, "❌ You don't have permission to modify balances."
        amount = args.get("amount")
        if amount is None or int(amount) < 0:
            return None, "❌ Balance cannot be negative."
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        ensure_eco_user(str(guild.id), str(target.id))
        cursor.execute("UPDATE economy SET balance = ? WHERE guild_id = ? AND user_id = ?", (int(amount), str(guild.id), str(target.id)))
        conn.commit(); conn.close()
        embed = discord.Embed(title=f"💵 Set {target.name}'s balance to ${int(amount)}", color=0x2ecc71)
        embed.set_footer(text=f"{guild.name}｜67")
        return embed, None

    elif tool_name == "set_level":
        if not invoker.guild_permissions.administrator:
            return None, "❌ You don't have permission to set levels."
        level = args.get("level")
        if level is None or int(level) < 1:
            return None, "❌ Level must be at least 1."
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT count_67 FROM levels WHERE guild_id = ? AND user_id = ?", (str(guild.id), str(target.id)))
        row = cursor.fetchone()
        current_67 = row[0] if row else 0
        cursor.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level, count_67) VALUES (?, ?, ?, ?, ?)", (str(guild.id), str(target.id), 0, int(level), current_67))
        conn.commit(); conn.close()
        # 在 commit 之後
        if isinstance(target, discord.Member):
            await check_level_roles(target, int(level), from_level=None)
        embed = discord.Embed(title=f"🔥 Set {target.name}'s level to {int(level)}", color=0x2ecc71)
        embed.set_footer(text=f"{guild.name}｜67")
        return embed, None

    elif tool_name == "set_streaks":
        if not invoker.guild_permissions.administrator:
            return None, "❌ You don't have permission to set streaks."
        days = args.get("days")
        if days is None or int(days) < 0:
            return None, "❌ Streak days cannot be negative."
        days = int(days)
        gid_str, uid_str = str(guild.id), str(target.id)
        tz = datetime.timezone(datetime.timedelta(hours=0))
        today_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT longest_streak FROM streaks_data WHERE guild_id = ? AND user_id = ?", (gid_str, uid_str))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE streaks_data SET current_streak = ?, longest_streak = ?, last_streak_date = ? WHERE guild_id = ? AND user_id = ?",
                           (days, max(row[0], days), today_str, gid_str, uid_str))
        else:
            cursor.execute("INSERT INTO streaks_data (guild_id, user_id, current_streak, longest_streak, last_streak_date) VALUES (?, ?, ?, ?, ?)",
                           (gid_str, uid_str, days, days, today_str))
        conn.commit(); conn.close()
        recompute_current_week_status(gid_str, uid_str, days)
        await check_streak_roles(target, days)
        await apply_streak_nickname(target, days)
        embed = discord.Embed(title=f"🔥 Set {target.name}'s Streaks to {days} days", color=0x2ecc71)
        embed.set_footer(text=f"{guild.name}｜67")
        return embed, None

    return None, "❌ Unknown action."

async def run_agent_completion(client, model: str, messages: list, guild, invoker, use_tools: bool, max_tokens: int = None):
    """
    跑一次 completion；如果模型決定呼叫工具，執行工具、把結果丟回模型，再拿一次自然語言回覆。
    回傳 (ai_reply_text, embeds_list)
    """
    kwargs = {"model": model, "messages": messages, "temperature": 0.7}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if use_tools:
        kwargs["tools"] = AGENT_TOOLS

    response = await client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)

    embeds = []
    if use_tools and tool_calls:
        messages.append(msg)
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            embed, err = await execute_agent_tool(tc.function.name, args, invoker, guild)
            if embed:
                embeds.append(embed)
                result_text = "Action completed successfully."
            else:
                result_text = err or "Action failed."
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

        follow_up = await client.chat.completions.create(model=model, messages=messages, temperature=0.7)
        ai_reply_text = follow_up.choices[0].message.content
    else:
        ai_reply_text = msg.content

    return ai_reply_text, embeds

ai_cooldowns = {}

# =====================================================
# ==================== OSLF ASC =======================
# =====================================================
PLANE_GUILD_ID = 1458114486442524904
PLANE_FORUM_ID = 1496743164713766952

TAG_MAIN_A = 1496744588751274057          # 主標籤 A → 兩種文案分支
TAG_MAIN_B = 1496883628523917464          # 主標籤 B
TAG_MAIN_C = 1496883730068013247          # 主標籤 C

TAG_LOC_SELL_1 = 1541828559155363851      # 與 A 組合 → 第一種 @role
TAG_LOC_SELL_2 = 1541980764802121758

TAG_COMPLETED = 1497211349502263387

# 地點標籤白名單（用於「必須剛好一個」驗證）
PLANE_LOCATION_TAGS = {
    1503349407175807116, 1503349452071764009, 1503380125176037426,
    1532795473637937302, 1532795530969612478, 1532795690642837674,
    1541828559155363851, 1541980764802121758, 1541980827456372846,
    1542446601899868270, 1543862391924723713,
}

ROLE_SELL_A = 1547189642540089384         # A + 特定地點
ROLE_SELL_B = 1505934626202452079         # A + 其他／無上述兩地點
ROLE_HELP_B = 1513789778225659904         # 主標籤 B
USER_PING_C = 936410788242001970          # 主標籤 C

USER_RC_REVIEW_1 = 1137258253949079683    # 定期定額審核
USER_RC_REVIEW_2 = 936410788242001970

DELIVERY_INFO_LINK = "https://discord.com/channels/1458114486442524904/1542445353591111750"

# 🎯 語音時數追蹤：(guild_id, user_id) -> 進入監聽頻道的時間戳
voice_sessions = {}
MAX_VOICE_WATCH = 20  # 全機器人同時間最多監聽 5 個語音頻道
voice_keepalive_tasks = {}  # 🎯 guild_id -> asyncio.Task，避免語音連線因為完全沒有音訊流量被 Discord 判定閒置斷線

counting_locks = {}  # guild_id -> asyncio.Lock，確保同一伺服器的數數判斷一次只處理一則訊息，避免競速條件

def get_counting_lock(guild_id) -> asyncio.Lock:
    if guild_id not in counting_locks:
        counting_locks[guild_id] = asyncio.Lock()
    return counting_locks[guild_id]

async def start_voice_keepalive(guild_id: int, voice_client: discord.VoiceClient):
    """啟動（或重啟）指定伺服器的語音保活任務"""
    stop_voice_keepalive(guild_id)  # 先確保沒有殘留的舊任務

    async def _loop():
        try:
            while voice_client.is_connected():
                voice_client.send_audio_packet(b'\xF8\xFF\xFE', encode=False)
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[語音保活錯誤] guild={guild_id}: {e}")

    voice_keepalive_tasks[guild_id] = asyncio.create_task(_loop())


def stop_voice_keepalive(guild_id: int):
    """停止指定伺服器的語音保活任務"""
    task = voice_keepalive_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()

# =================================================================
# 🗄️ 2. DATABASE INITIALIZATION (資料庫初始化)
# =================================================================

def ensure_column(cursor, table: str, column: str, col_def: str):
    """安全地為既有資料表新增欄位（不存在才新增，不會清掉舊資料）"""
    cursor.execute(f"PRAGMA table_info({table})")
    if column not in [c[1] for c in cursor.fetchall()]:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # 🎯 開啟 WAL 模式：讀取不會被寫入卡住，大幅降低多個連線互搶造成的阻塞
    # 這是寫進資料庫檔案本身的設定，只需要在啟動時設一次，之後所有 sqlite3.connect() 都會自動套用
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.commit()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, 
            w_title TEXT, w_desc TEXT, g_title TEXT, g_desc TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome_roles (
            guild_id TEXT,
            role_id TEXT,
            PRIMARY KEY (guild_id, role_id)
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS levelup (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    ensure_column(cursor, "levelup", "reply_mode", "INTEGER DEFAULT 0")  # 🎯 0=發到頻道, 1=在該訊息下回覆
    ensure_column(cursor, "levelup", "admin_xp_per_level", "INTEGER DEFAULT 500")  # 🎯 管理員優待：每等固定要多少 XP
    cursor.execute("CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, message TEXT, channel_id TEXT)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS levels (
            guild_id TEXT,
            user_id TEXT, 
            xp INTEGER DEFAULT 0, 
            level INTEGER DEFAULT 1, 
            count_67 INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    # 🎯 舊版 levels 表沒有 guild_id，資料無法安全歸屬到特定伺服器，偵測到舊結構就重建（歸零）
    cursor.execute("PRAGMA table_info(levels)")
    if 'guild_id' not in [c[1] for c in cursor.fetchall()]:
        cursor.execute("ALTER TABLE levels RENAME TO levels_old")
        cursor.execute("""
            CREATE TABLE levels (
                guild_id TEXT,
                user_id TEXT, 
                xp INTEGER DEFAULT 0, 
                level INTEGER DEFAULT 1, 
                count_67 INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        cursor.execute("DROP TABLE levels_old")
        conn.commit()
    cursor.execute("CREATE TABLE IF NOT EXISTS mutes (guild_id TEXT, banned_word TEXT, duration_str TEXT, PRIMARY KEY (guild_id, banned_word))")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy (
            guild_id TEXT,
            user_id TEXT,
            balance INTEGER DEFAULT 0,
            last_daily TEXT DEFAULT '',
            last_work INTEGER DEFAULT 0,
            last_pay INTEGER DEFAULT 0,
            last_rob INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    # 🎯 舊版 economy 表沒有 guild_id，資料無法安全歸屬到特定伺服器，偵測到舊結構就重建（歸零）
    cursor.execute("PRAGMA table_info(economy)")
    if 'guild_id' not in [c[1] for c in cursor.fetchall()]:
        cursor.execute("ALTER TABLE economy RENAME TO economy_old")
        cursor.execute("""
            CREATE TABLE economy (
                guild_id TEXT,
                user_id TEXT,
                balance INTEGER DEFAULT 0,
                last_daily TEXT DEFAULT '',
                last_work INTEGER DEFAULT 0,
                last_pay INTEGER DEFAULT 0,
                last_rob INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        cursor.execute("DROP TABLE economy_old")
        conn.commit()
    # 🎯 新增：建立等級身分組獎勵配置表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS level_roles (
            guild_id TEXT,
            level INTEGER,
            role_id TEXT,
            PRIMARY KEY (guild_id, level)
        )
    """)
    # 🎯 新增：語音時數追蹤功能
    cursor.execute("CREATE TABLE IF NOT EXISTS voice_watch (guild_id TEXT PRIMARY KEY, channel_id TEXT)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voice_time (
            guild_id TEXT,
            user_id TEXT,
            seconds INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    # 🎯 新增：/settings 四大功能 + Streaks 的獨立開關（每個伺服器隔離）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feature_toggles (
            guild_id TEXT,
            feature TEXT,
            enabled INTEGER DEFAULT 1,
            PRIMARY KEY (guild_id, feature)
        )
    """)
    # 💰 付費解鎖清單（例如「67+Silent」解除自動回覆 67 的限制），只能靠開發者手動新增
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paid_features (
            guild_id TEXT,
            feature TEXT,
            PRIMARY KEY (guild_id, feature)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_bot_profile (
            guild_id TEXT PRIMARY KEY,
            nick TEXT,
            bio TEXT
        )
    """)
    # 🔥 新增：Streaks 系統
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks_settings (
            guild_id TEXT PRIMARY KEY,
            messages_needed INTEGER DEFAULT 20,
            notify_channel_id TEXT,
            nick_threshold INTEGER DEFAULT 3,
            nick_emoji TEXT DEFAULT '🔥'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks_data (
            guild_id TEXT,
            user_id TEXT,
            current_streak INTEGER DEFAULT 0,
            longest_streak INTEGER DEFAULT 0,
            messages_today INTEGER DEFAULT 0,
            last_message_date TEXT DEFAULT '',
            last_streak_date TEXT DEFAULT '',
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks_roles (
            guild_id TEXT,
            streak_count INTEGER,
            role_id TEXT,
            PRIMARY KEY (guild_id, streak_count)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks_weekly (
            guild_id TEXT,
            user_id TEXT,
            week_start TEXT,
            day_status TEXT DEFAULT '0000000',
            PRIMARY KEY (guild_id, user_id, week_start)
        )
    """)
    # 🎯 加入伺服器時預先建立的 Webhook（給之後「發射台」架構備用）
    cursor.execute("CREATE TABLE IF NOT EXISTS guild_webhooks (guild_id TEXT PRIMARY KEY, webhook_url TEXT, channel_id TEXT)")
    # 🔢 數數頻道
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS counting_settings (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT,
            current_count INTEGER DEFAULT 0,
            last_user_id TEXT,
            mute_duration TEXT DEFAULT '10m'
        )
    """)
    # 📊 Server Stats 語音頻道
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS server_stats (
            guild_id TEXT PRIMARY KEY,
            category_id TEXT,
            all_members_id TEXT,
            members_id TEXT,
            bots_id TEXT,
            bans_id TEXT,
            mutes_id TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_settings (
            guild_id TEXT PRIMARY KEY,
            only_selected INTEGER DEFAULT 0,
            channel_id TEXT
        )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS auto_reactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        trigger_text TEXT NOT NULL,
        emoji TEXT NOT NULL
    )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS plane_orders (
            thread_id TEXT PRIMARY KEY,
            guild_id TEXT,
            message_id TEXT,
            status TEXT,
            taken_by TEXT,
            prev_status TEXT,
            prev_title TEXT,
            rc_period TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warn_settings (
            guild_id TEXT PRIMARY KEY,
            dashboard_enabled INTEGER DEFAULT 1,
            reply_allowed INTEGER DEFAULT 1,
            reply_channel_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            warner_id TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            dm_message_id TEXT,
            reply_text TEXT,
            reply_at TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warns_guild_time ON warns(guild_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warns_target ON warns(guild_id, target_id)"
    )
    
    conn.commit()
    conn.close()

init_db()

# ===== Plane Order UI =====

def plane_order_upsert(thread_id: int, guild_id: int, **fields):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    tid, gid = str(thread_id), str(guild_id)
    cursor.execute("SELECT thread_id FROM plane_orders WHERE thread_id = ?", (tid,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO plane_orders (thread_id, guild_id, status, created_at) VALUES (?, ?, ?, ?)",
            (
                tid,
                gid,
                fields.get("status", "open"),
                datetime.datetime.utcnow().isoformat(),
            ),
        )
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        cursor.execute(
            f"UPDATE plane_orders SET {cols} WHERE thread_id = ?",
            (*[fields[k] for k in fields], tid),
        )
    conn.commit()
    conn.close()

def _plane_is_reviewer(user_id: int) -> bool:
    return user_id in (USER_RC_REVIEW_1, USER_RC_REVIEW_2)


def _plane_has_assign_role(member: discord.Member) -> bool:
    return any(r.id == ROLE_SELL_B for r in member.roles)


async def _plane_edit_status_line(message: discord.Message, line: str):
    """Append or refresh a trailing status line on the bot message."""
    base = message.content or ""
    # strip previous taken-over / status lines we added
    lines = [
        ln
        for ln in base.split("\n")
        if not ln.startswith("👉 Taken over by：")
        and not ln.startswith("【Status: Completed】")
    ]
    lines.append(line)
    await message.edit(content="\n".join(lines))


class PlaneRCApproveView(ui.View):
    """同意 / 不同意 for recurring transfer."""

    def __init__(self, thread_id: int, period: str, order_message_id: int):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.period = period  # "weekly" | "monthly"
        self.order_message_id = order_message_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _plane_is_reviewer(interaction.user.id):
            await interaction.response.send_message(
                "❌ Only reviewers can approve this.", ephemeral=True
            )
            return False
        return True

    @ui.button(label="同意", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        thread = interaction.guild.get_thread(self.thread_id)
        if thread is None:
            try:
                thread = await interaction.guild.fetch_channel(self.thread_id)
            except discord.HTTPException:
                return await interaction.followup.send("❌ Thread not found.", ephemeral=True)

        prefix = "[Weekly] " if self.period == "weekly" else "[Monthly] "
        title = thread.name or ""
        if not title.startswith("[Weekly] ") and not title.startswith("[Monthly] "):
            new_title = (prefix + title)[:100]
            try:
                await thread.edit(name=new_title)
            except discord.HTTPException as e:
                return await interaction.followup.send(f"❌ Cannot edit title: {e}", ephemeral=True)

        plane_order_upsert(
            self.thread_id,
            interaction.guild.id,
            status=f"rc_{self.period}",
            rc_period=self.period,
            prev_title=title,
        )

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send(
            f"✅ Recurring **{self.period}** approved. Title updated.",
            ephemeral=True,
        )

    @ui.button(label="不同意", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        # Disable Transfer button on original order message if we can find it
        thread = interaction.guild.get_thread(self.thread_id)
        if thread:
            try:
                order_msg = await thread.fetch_message(self.order_message_id)
                if order_msg.view is None and order_msg.components:
                    pass  # rebuild disabled transfer via new view snapshot
                # Re-send state: disable only transfer by editing with a view flag in DB
                plane_order_upsert(
                    self.thread_id,
                    interaction.guild.id,
                    status="open",
                    rc_period=None,
                )
                # Try to disable transfer on stored view by re-posting disabled state
                view = PlaneOrderView(
                    thread_id=self.thread_id,
                    layout="full",
                    transfer_disabled=True,
                )
                await order_msg.edit(view=view)
            except discord.HTTPException:
                pass

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send("⛔ Transfer denied. Transfer button disabled.", ephemeral=True)


class PlaneRCPeriodSelect(ui.Select):
    def __init__(self, thread_id: int, order_message_id: int):
        super().__init__(
            placeholder="Weekly or Monthly?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Weekly", value="weekly"),
                discord.SelectOption(label="Monthly", value="monthly"),
            ],
        )
        self.thread_id = thread_id
        self.order_message_id = order_message_id

    async def callback(self, interaction: discord.Interaction):
        period = self.values[0]
        await interaction.response.send_message(
            f"<@{USER_RC_REVIEW_1}> Recurring **{period}** requested in <#{self.thread_id}>.\n"
            f"Reviewers: approve or deny.",
            view=PlaneRCApproveView(self.thread_id, period, self.order_message_id),
        )
        plane_order_upsert(
            self.thread_id,
            interaction.guild.id,
            status="rc_pending",
            rc_period=period,
        )


class PlaneRCPeriodView(ui.View):
    def __init__(self, thread_id: int, order_message_id: int):
        super().__init__(timeout=120)
        self.add_item(PlaneRCPeriodSelect(thread_id, order_message_id))


class PlaneAssignSelect(ui.Select):
    def __init__(self, thread_id: int, order_message_id: int, members: list[discord.Member]):
        options = [
            discord.SelectOption(
                label=(m.display_name or m.name)[:100],
                value=str(m.id),
            )
            for m in members[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="No members", value="0")]
        super().__init__(
            placeholder="Assign to…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.thread_id = thread_id
        self.order_message_id = order_message_id

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "0":
            return await interaction.response.send_message("❌ No members.", ephemeral=True)
        uid = int(self.values[0])
        await interaction.response.defer()
        thread = interaction.channel
        try:
            msg = await thread.fetch_message(self.order_message_id)
        except discord.HTTPException:
            return await interaction.followup.send("❌ Order message not found.", ephemeral=True)

        await _plane_edit_status_line(msg, f"👉 Taken over by：<@{uid}>")
        view = PlaneOrderView(
            thread_id=self.thread_id,
            layout=getattr(self.view, "layout", "full"),
            take_assign_disabled=True,
        )
        await msg.edit(view=view)
        plane_order_upsert(
            self.thread_id,
            interaction.guild.id,
            status="taken",
            taken_by=str(uid),
            prev_status="open",
        )
        await interaction.followup.send(f"✅ Assigned to <@{uid}>.", ephemeral=True)


class PlaneAssignView(ui.View):
    def __init__(self, thread_id: int, order_message_id: int, members: list[discord.Member], layout: str):
        super().__init__(timeout=120)
        self.layout = layout
        self.add_item(PlaneAssignSelect(thread_id, order_message_id, members))


class PlaneOrderView(ui.View):
    """
    layout:
      - "full"  : Take Over | Transfer | Assign | Completed
      - "b"     : Take Over | Completed
      - "c"     : Take Over | Assign | Completed
    """

    def __init__(
        self,
        thread_id: int,
        layout: str = "full",
        take_assign_disabled: bool = False,
        transfer_disabled: bool = False,
        all_disabled: bool = False,
    ):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.layout = layout

        def maybe_disable(btn, force=False):
            btn.disabled = all_disabled or force

        # Take Over
        self.btn_take = ui.Button(
            label="Take Over",
            style=discord.ButtonStyle.primary,
            custom_id=f"plane:take:{thread_id}",
        )
        self.btn_take.callback = self._take_over
        maybe_disable(self.btn_take, take_assign_disabled)
        self.add_item(self.btn_take)

        if layout == "full":
            self.btn_transfer = ui.Button(
                label="Transfer to Recurring Investing",
                style=discord.ButtonStyle.secondary,
                custom_id=f"plane:rc:{thread_id}",
            )
            self.btn_transfer.callback = self._transfer
            maybe_disable(self.btn_transfer, transfer_disabled)
            self.add_item(self.btn_transfer)

        if layout in ("full", "c"):
            self.btn_assign = ui.Button(
                label="Assign",
                style=discord.ButtonStyle.secondary,
                custom_id=f"plane:assign:{thread_id}",
            )
            self.btn_assign.callback = self._assign
            maybe_disable(self.btn_assign, take_assign_disabled)
            self.add_item(self.btn_assign)

        self.btn_done = ui.Button(
            label="Completed",
            style=discord.ButtonStyle.success,
            custom_id=f"plane:done:{thread_id}",
        )
        self.btn_done.callback = self._completed
        maybe_disable(self.btn_done, False)
        if all_disabled:
            self.btn_done.disabled = True
        self.add_item(self.btn_done)

    async def _take_over(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _plane_edit_status_line(
            interaction.message,
            f"👉 Taken over by：{interaction.user.mention}",
        )
        new_view = PlaneOrderView(
            thread_id=self.thread_id,
            layout=self.layout,
            take_assign_disabled=True,
        )
        await interaction.message.edit(view=new_view)
        plane_order_upsert(
            self.thread_id,
            interaction.guild.id,
            status="taken",
            taken_by=str(interaction.user.id),
            message_id=str(interaction.message.id),
            prev_status="open",
        )

    async def _transfer(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Select recurring period:",
            view=PlaneRCPeriodView(self.thread_id, interaction.message.id),
            ephemeral=True,
        )

    async def _assign(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not _plane_has_assign_role(
            interaction.user
        ):
            return await interaction.response.send_message(
                "❌ Only members with the assign role can use this.",
                ephemeral=True,
            )
        role = interaction.guild.get_role(ROLE_SELL_B)
        members = [m for m in (role.members if role else []) if not m.bot][:25]
        await interaction.response.send_message(
            "Pick a member:",
            view=PlaneAssignView(
                self.thread_id, interaction.message.id, members, self.layout
            ),
            ephemeral=True,
        )

    async def _completed(self, interaction: discord.Interaction):
        await interaction.response.defer()
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return await interaction.followup.send("❌ Not a thread.", ephemeral=True)

        # status line
        await _plane_edit_status_line(interaction.message, "【Status: Completed】")

        # title
        title = thread.name or ""
        if not title.startswith("[Completed] "):
            try:
                await thread.edit(name=("[Completed] " + title)[:100])
            except discord.HTTPException:
                pass

        # tags → only completed tag; lock + archive
        parent = thread.parent
        new_tags = []
        if isinstance(parent, discord.ForumChannel):
            tag = discord.utils.get(parent.available_tags, id=TAG_COMPLETED)
            if tag:
                new_tags = [tag]
        try:
            await thread.edit(applied_tags=new_tags, locked=True, archived=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"⚠️ Partial complete: {e}", ephemeral=True)

        done_view = PlaneOrderView(
            thread_id=self.thread_id,
            layout=self.layout,
            all_disabled=True,
        )
        await interaction.message.edit(view=done_view)
        plane_order_upsert(
            self.thread_id,
            interaction.guild.id,
            status="completed",
            prev_status="taken",
            prev_title=title,
        )
        await interaction.followup.send("✅ Marked completed, locked & closed.", ephemeral=True)

# =================================================================
# 🔄 3. CORE UTILITIES (核心工具函式與變數解析)
# =================================================================
def get_xp_needed(level: int, is_admin: bool = False, admin_xp: int = 500) -> int:
    if is_admin:
        return admin_xp  # 管理員專屬：每一等都固定要這個數字的 XP，可在 /settings 調整
    return 5 * (level ** 2) + 50 * level + 100


def get_admin_xp_per_level(guild_id) -> int:
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT admin_xp_per_level FROM levelup WHERE guild_id = ?", (str(guild_id),))
    row = cursor.fetchone(); conn.close()
    return row[0] if row and row[0] else 500

async def check_level_roles(
    member: discord.Member,
    level: int,
    from_level: int | None = None,
):
    """
    發放等級身分組。
    - from_level 有給：只補 (from_level, level] 之間設定過的角色（一般升等）
    - from_level 為 None：補所有 level <= 目前等級的角色（/setlevel、Agent 灌等）
    """
    if not isinstance(member, discord.Member):
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    gid = str(member.guild.id)

    if from_level is None:
        cursor.execute(
            "SELECT role_id FROM level_roles WHERE guild_id = ? AND level <= ? ORDER BY level ASC",
            (gid, level),
        )
    else:
        cursor.execute(
            "SELECT role_id FROM level_roles WHERE guild_id = ? AND level > ? AND level <= ? ORDER BY level ASC",
            (gid, from_level, level),
        )
    rows = cursor.fetchall()
    conn.close()

    to_add = []
    for (role_id,) in rows:
        role = member.guild.get_role(int(role_id))
        if role and role not in member.roles:
            to_add.append(role)

    if not to_add:
        return

    try:
        await member.add_roles(*to_add, reason="Level role reward")
    except discord.Forbidden:
        for role in to_add:
            try:
                await member.add_roles(role, reason="Level role reward")
            except discord.Forbidden:
                logger.error(
                    f"❌ nah, I can't give **{role.name}** to **{member.name}**"
                )
            except Exception as e:
                logger.error(f"[check_level_roles] {e}")
    except Exception as e:
        logger.error(f"[check_level_roles bulk] {e}")

def is_feature_enabled(guild_id, feature: str) -> bool:
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT enabled FROM feature_toggles WHERE guild_id = ? AND feature = ?", (str(guild_id), feature))
    row = cursor.fetchone(); conn.close()
    return (row[0] == 1) if row else False  # 🎯 預設關閉


def set_feature_enabled(guild_id, feature: str, enabled: bool):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO feature_toggles (guild_id, feature, enabled) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, feature) DO UPDATE SET enabled = excluded.enabled",
        (str(guild_id), feature, 1 if enabled else 0)
    )
    conn.commit(); conn.close()



def _default_reply_channel_id(guild: discord.Guild) -> str | None:
    # Community「Server Updates / Public Updates」，否則 system channel
    ch = getattr(guild, "public_updates_channel", None) or guild.system_channel
    return str(ch.id) if ch else None


def ensure_warn_settings(guild: discord.Guild) -> dict:
    gid = str(guild.id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT dashboard_enabled, reply_allowed, reply_channel_id FROM warn_settings WHERE guild_id = ?",
        (gid,),
    )
    row = cursor.fetchone()
    if not row:
        default_ch = _default_reply_channel_id(guild)
        cursor.execute(
            "INSERT INTO warn_settings (guild_id, dashboard_enabled, reply_allowed, reply_channel_id) VALUES (?, 1, 1, ?)",
            (gid, default_ch),
        )
        conn.commit()
        conn.close()
        return {
            "dashboard_enabled": 1,
            "reply_allowed": 1,
            "reply_channel_id": default_ch,
        }
    dashboard, reply_allowed, reply_ch = row
    if not reply_ch:
        reply_ch = _default_reply_channel_id(guild)
        if reply_ch:
            cursor.execute(
                "UPDATE warn_settings SET reply_channel_id = ? WHERE guild_id = ?",
                (reply_ch, gid),
            )
            conn.commit()
    conn.close()
    return {
        "dashboard_enabled": int(dashboard if dashboard is not None else 1),
        "reply_allowed": int(reply_allowed if reply_allowed is not None else 1),
        "reply_channel_id": reply_ch,
    }


def set_warn_dashboard(guild_id, enabled: bool):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO warn_settings (guild_id, dashboard_enabled) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET dashboard_enabled = excluded.dashboard_enabled",
        (str(guild_id), 1 if enabled else 0),
    )
    conn.commit()
    conn.close()


def set_warn_reply_allowed(guild_id, allowed: bool):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO warn_settings (guild_id, reply_allowed) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET reply_allowed = excluded.reply_allowed",
        (str(guild_id), 1 if allowed else 0),
    )
    conn.commit()
    conn.close()


def set_warn_reply_channel(guild_id, channel_id: str | None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO warn_settings (guild_id, reply_channel_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET reply_channel_id = excluded.reply_channel_id",
        (str(guild_id), channel_id),
    )
    conn.commit()
    conn.close()


def insert_warn(
    guild_id,
    target_id,
    warner_id,
    message: str,
    dm_message_id: str | None = None,
) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = discord.utils.utcnow().isoformat()
    cursor.execute(
        "INSERT INTO warns (guild_id, target_id, warner_id, message, created_at, dm_message_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(guild_id), str(target_id), str(warner_id), message, now, dm_message_id),
    )
    wid = cursor.lastrowid
    conn.commit()
    conn.close()
    return wid


def get_warn_stats(guild: discord.Guild) -> dict:
    gid = str(guild.id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM warns WHERE guild_id = ?", (gid,))
    total = cursor.fetchone()[0] or 0
    cursor.execute(
        "SELECT target_id, COUNT(*) AS c FROM warns WHERE guild_id = ? "
        "GROUP BY target_id ORDER BY c DESC LIMIT 1",
        (gid,),
    )
    top = cursor.fetchone()
    cursor.execute(
        "SELECT target_id, warner_id FROM warns WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
        (gid,),
    )
    latest = cursor.fetchone()
    conn.close()

    def uname(uid):
        if not uid:
            return "—"
        m = guild.get_member(int(uid))
        if m:
            return m.name
        u = bot.get_user(int(uid))
        return u.name if u else str(uid)

    return {
        "total": total,
        "highest_name": uname(top[0]) if top else "—",
        "highest_count": top[1] if top else 0,
        "latest_from": uname(latest[1]) if latest else "—",
        "latest_to": uname(latest[0]) if latest else "—",
    }


def list_warns(guild_id, limit=10, target_id=None, warner_id=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    q = "SELECT id, target_id, warner_id, message, created_at, reply_text, reply_at FROM warns WHERE guild_id = ?"
    args = [str(guild_id)]
    if target_id:
        q += " AND target_id = ?"
        args.append(str(target_id))
    if warner_id:
        q += " AND warner_id = ?"
        args.append(str(warner_id))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    cursor.execute(q, args)
    rows = cursor.fetchall()
    conn.close()
    return rows

def is_autoreply_enabled(guild_id) -> bool:
    """67 自動回覆，跟其他功能相反，預設是開啟的"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT enabled FROM feature_toggles WHERE guild_id = ? AND feature = ?", (str(guild_id), "autoreply67"))
    row = cursor.fetchone(); conn.close()
    return (row[0] == 1) if row else True  # 🎯 預設開啟

def is_automute_delete_enabled(guild_id) -> bool:
    """AutoMod 觸發時要不要連同封鎖訊息（block_message），預設開啟"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT enabled FROM feature_toggles WHERE guild_id = ? AND feature = ?", (str(guild_id), "automute_delete"))
    row = cursor.fetchone(); conn.close()
    return (row[0] == 1) if row else True

def is_paid_guild(guild_id, feature: str) -> bool:
    """檢查這個伺服器是不是已經手動被加進某個付費功能的白名單"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM paid_features WHERE guild_id = ? AND feature = ?", (str(guild_id), feature))
    row = cursor.fetchone(); conn.close()
    return row is not None

def list_auto_reactions(guild_id) -> list[tuple[int, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, trigger_text, emoji FROM auto_reactions WHERE guild_id = ? ORDER BY id",
        (str(guild_id),),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def add_auto_reaction(guild_id, trigger_text: str, emoji: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO auto_reactions (guild_id, trigger_text, emoji) VALUES (?, ?, ?)",
        (str(guild_id), trigger_text.strip(), emoji.strip()),
    )
    conn.commit()
    conn.close()


def delete_auto_reaction(row_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM auto_reactions WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


def parse_reaction_emoji(raw: str):
    """Return str (unicode) or discord.PartialEmoji for custom."""
    raw = (raw or "").strip()
    m = re.match(r"<(a?):([a-zA-Z0-9_]+):(\d+)>$", raw)
    if m:
        animated, name, eid = m.groups()
        return discord.PartialEmoji(name=name, id=int(eid), animated=bool(animated))
    # unicode / short emoji string
    if raw:
        return raw
    return None

import base64

_PROFILE_MAX_BYTES = 2 * 1024 * 1024
_PROFILE_IMAGE_RULES = {
    ".png": ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ".gif": ("image/gif", lambda b: b.startswith((b"GIF87a", b"GIF89a"))),
    ".webp": (
        "image/webp",
        lambda b: len(b) >= 12 and b.startswith(b"RIFF") and b[8:12] == b"WEBP",
    ),
}

def get_guild_bot_profile(guild_id) -> tuple[str, str]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT nick, bio FROM guild_bot_profile WHERE guild_id = ?",
        (str(guild_id),),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return "", ""
    return (row[0] or "", row[1] or "")

def save_guild_bot_profile(guild_id, nick: str | None, bio: str | None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO guild_bot_profile (guild_id, nick, bio) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET nick = excluded.nick, bio = excluded.bio",
        (str(guild_id), nick or "", bio or ""),
    )
    conn.commit()
    conn.close()

def _validate_profile_image(data: bytes, filename: str, content_type: str | None) -> str | None:
    if not data:
        return "❌ Empty file."
    if len(data) > _PROFILE_MAX_BYTES:
        return f"❌ Image too large (max {_PROFILE_MAX_BYTES // 1024}KB)."
    name = (filename or "").lower()
    ext = next((e for e in _PROFILE_IMAGE_RULES if name.endswith(e)), None)
    if not ext:
        return "❌ Only png / jpg / jpeg / gif / webp allowed."
    expected_mime, magic_ok = _PROFILE_IMAGE_RULES[ext]
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct and ct not in ("application/octet-stream",) and not (
        ct == expected_mime or ct.startswith("image/")
    ):
        return f"❌ Bad content type `{ct}`."
    if not magic_ok(data):
        return "❌ Not a real image (header mismatch). Rejected."
    return None

def _image_ext(filename: str) -> str:
    name = (filename or "").lower()
    for e in (".webp", ".gif", ".jpeg", ".jpg", ".png"):
        if name.endswith(e):
            return "jpg" if e == ".jpeg" else e[1:]
    return "png"

async def _patch_guild_profile(guild: discord.Guild, **fields):
    payload = {}
    for key in ("nick", "bio"):
        if key in fields:
            payload[key] = fields[key]
    for key in ("avatar", "banner"):
        if key not in fields:
            continue
        data = fields[key]
        if data is None:
            payload[key] = None
        else:
            raw, ext = data
            mime = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "image/png")
            payload[key] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    if not payload:
        return
    route = discord.http.Route(
        "PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild.id
    )
    await bot.http.request(route, json=payload)

async def check_streak_roles(member: discord.Member, streak_count: int):
    """達成連擊天數時發放對應身分組"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT role_id FROM streaks_roles WHERE guild_id = ? AND streak_count = ?", (str(member.guild.id), streak_count))
    row = cursor.fetchone()
    conn.close()
    if row:
        role = member.guild.get_role(int(row[0]))
        if role and role not in member.roles:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                logger.error(f"nah, I can't give **{role.name}** to **{member.name}**")


async def revoke_streak_roles(member: discord.Member, up_to_streak: int):
    """連擊斷掉時，收回所有已經拿到的連擊身分組"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT role_id FROM streaks_roles WHERE guild_id = ? AND streak_count <= ?", (str(member.guild.id), up_to_streak))
    rows = cursor.fetchall(); conn.close()
    for (rid,) in rows:
        role = member.guild.get_role(int(rid))
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except discord.Forbidden:
                logger.error(f"nah, I can't remove **{role.name}** from **{member.name}**")


def _strip_streak_suffix(nickname: str, emoji: str) -> str:
    """把暱稱結尾的「 符號天數」格式去掉，回傳乾淨的原始名稱"""
    pattern = r"\s*" + re.escape(emoji) + r"\d+$"
    return re.sub(pattern, "", nickname)


async def apply_streak_nickname(member: discord.Member, cur_streak: int):
    """格式：{原本名稱} [符號]{天數}，天數每次連擊增加都要更新，不是只設定一次"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT nick_threshold, nick_emoji FROM streaks_settings WHERE guild_id = ?", (str(member.guild.id),))
    row = cursor.fetchone(); conn.close()
    if not row or not row[1] or cur_streak < row[0]:
        return

    threshold, emoji = row
    base_name = _strip_streak_suffix(member.display_name, emoji)
    new_nick = f"{base_name} {emoji}{cur_streak}"

    # Discord 暱稱上限 32 字，超過的話從原本名稱那段截短，確保後面的 符號天數 一定完整保留
    if len(new_nick) > 32:
        overflow = len(new_nick) - 32
        base_name = base_name[:max(0, len(base_name) - overflow)]
        new_nick = f"{base_name} {emoji}{cur_streak}"
        
    if member.display_name != new_nick:
        try:
            await member.edit(nick=new_nick)
        except discord.Forbidden:
            pass


async def revert_streak_nickname(member: discord.Member):
    """連擊斷掉時自動偵測並還原成原本的名稱"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT nick_emoji FROM streaks_settings WHERE guild_id = ?", (str(member.guild.id),))
    row = cursor.fetchone(); conn.close()
    if not row or not row[0]:
        return

    base_name = _strip_streak_suffix(member.display_name, row[0])
    if base_name != member.display_name:
        try:
            await member.edit(nick=base_name or None)
        except discord.Forbidden:
            pass


def get_week_start(dt: datetime.datetime) -> str:
    monday = dt - datetime.timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def build_week_line(day_status: str) -> str:
    days = ["M", "T", "W", "T", "F", "S", "S"]
    day_line = "  ".join(days)
    status_line = "  ".join("✅" if c == "1" else "❌" for c in day_status)
    return f"```\n{day_line}\n{status_line}\n```"

def recompute_current_week_status(gid: str, uid: str, streak: int):
    """
    根據 /setstreaks 設定後的 current_streak，往回推算「這一週」（週一到今天）
    每一天是否該顯示 ✅，整段覆蓋寫入 streaks_weekly，灌水補上✅、砍掉的天數還原成❌。
    """
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    tz = datetime.timezone(datetime.timedelta(hours=0))
    today = datetime.datetime.now(tz)
    week_start_str = get_week_start(today)
    monday = today - datetime.timedelta(days=today.weekday())

    days_this_week = (today - monday).days + 1  # 這週從週一到今天總共幾天
    status = ["0"] * 7
    for i in range(days_this_week):
        day = monday + datetime.timedelta(days=i)
        days_ago = (today - day).days
        if days_ago < streak:  # 這天落在「往回數 streak 天」的範圍內
            status[i] = "1"

    cursor.execute(
        "INSERT OR REPLACE INTO streaks_weekly (guild_id, user_id, week_start, day_status) VALUES (?, ?, ?, ?)",
        (gid, uid, week_start_str, "".join(status))
    )
    conn.commit(); conn.close() 

def parse_placeholders(text: str, member: discord.Member, guild: discord.Guild, inviter: discord.Member = None, extra: dict = None) -> str:
    if not text: return ""
    reps = {
        "{user.mention}": member.mention if member else "",
        "{user.name}": member.name if member else "",
        "{user.username}": member.name if member else "",
        "{server.name}": guild.name if guild else "",
        "{guild.name}": guild.name if guild else "",
        "{member.count}": str(guild.member_count) if guild else "0",
        "{guild.membercount}": str(guild.member_count) if guild else "0",
        "{guild.members}": str(guild.member_count) if guild else "0",
        "{inviter.name}": inviter.name if inviter else "Someone",
        "{inviter}": inviter.mention if inviter else "Unknown"
    }
    if extra:
        for k, v in extra.items(): 
            reps[f"{{{k}}}"] = str(v)
    for p, v in reps.items(): 
        text = text.replace(p, v)
    return text

def parse_mute_duration(duration_str: str):
    cleaned = duration_str.strip().lower()
    if cleaned in ("0", "none", "off"):
        return None, None  # 🎯 特殊值：代表「不禁言」，不是錯誤，回傳 (None, None) 讓呼叫端自行判斷
    match = re.match(r"^(\d+)([mhd])$", cleaned)
    if not match: return None, "Invalid format! Use 1m, 3m, 1h, 2d, or 0/none to disable muting."
    amount, unit = int(match.group(1)), match.group(2)
    if unit == 'm': delta = datetime.timedelta(minutes=amount)
    elif unit == 'h': delta = datetime.timedelta(hours=amount)
    else: delta = datetime.timedelta(days=amount)
    if delta > datetime.timedelta(days=14): return None, "Max duration is 14 days."
    return delta, None

def _stat_channel_name(current_name: str, count: int) -> str:
    """只改名稱結尾的數字，前面管理員怎麼改都保留。"""
    m = re.search(r"^(.*?)(\d+)\s*$", current_name or "")
    if m:
        return f"{m.group(1)}{count}"
    return f"{current_name} {count}" if current_name else str(count)


async def update_server_stats(guild: discord.Guild):
    """依 DB 紀錄更新該伺服器的 stats 語音頻道名稱；頻道被刪就不報錯。"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category_id, all_members_id, members_id, bots_id, bans_id, mutes_id "
        "FROM server_stats WHERE guild_id = ?",
        (str(guild.id),)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return

    _, all_id, mem_id, bot_id, ban_id, mute_id = row

    total = guild.member_count or len(guild.members)
    humans = sum(1 for m in guild.members if not m.bot)
    bots = sum(1 for m in guild.members if m.bot)

    # Mutes：目前仍在 timeout 的人
    now = discord.utils.utcnow()
    mutes = sum(
        1 for m in guild.members
        if m.timed_out_until and m.timed_out_until > now
    )

    # Bans：需要 Ban Members 權限；失敗就跳過不改
    bans = None
    try:
        bans = 0
        async for _ in guild.bans(limit=None):
            bans += 1
    except (discord.Forbidden, discord.HTTPException):
        bans = None

    id_to_count = {
        all_id: total,
        mem_id: humans,
        bot_id: bots,
        mute_id: mutes,
    }
    if bans is not None:
        id_to_count[ban_id] = bans

    for cid, count in id_to_count.items():
        if not cid:
            continue
        ch = guild.get_channel(int(cid))
        if ch is None:
            continue  # 管理員刪了頻道 → 安靜跳過
        new_name = _stat_channel_name(ch.name, count)
        if new_name != ch.name:
            try:
                await ch.edit(name=new_name, reason="Server stats update")
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

AUTOMOD_RULE_PREFIX = "67bot-automute-"
MAX_AUTOMOD_KEYWORD_RULES = 6


async def sync_automod_rules(guild: discord.Guild) -> str | None:
    """
    把 mutes 表的違規字詞同步成 Discord 原生 AutoMod 規則。
    同一個禁言時長的字詞會合併進同一條規則。回傳 None 代表成功，回傳字串代表錯誤訊息。
    """
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT banned_word, duration_str FROM mutes WHERE guild_id = ?", (str(guild.id),))
    rows = cursor.fetchall(); conn.close()

    groups = {}
    for word, dur in rows:
        groups.setdefault(dur, []).append(word)

    if len(groups) > MAX_AUTOMOD_KEYWORD_RULES:
        return (f"❌ There are currently **{len(groups)}** different ban durations, but each Discord AutoMod server can only have a maximum of "
                f"{MAX_AUTOMOD_KEYWORD_RULES} keyword rules. Please change some of the violating words to the same duration, or reduce the number of categories.")

    try:
        existing_rules = await guild.fetch_automod_rules()
    except discord.Forbidden:
        return "❌ 我沒有 `Manage Server` 權限，無法建立/讀取 AutoMod 規則。請確認 bot 有這個權限（可能需要重新邀請機器人）。"

    managed_rules = {r.name: r for r in existing_rules if r.name.startswith(AUTOMOD_RULE_PREFIX)}

    delete_msg = is_automute_delete_enabled(guild.id)

    for dur, words in groups.items():
        rule_name = f"{AUTOMOD_RULE_PREFIX}{dur}"
        delta, err = parse_mute_duration(dur)
        if err or not delta:
            continue

        actions = [discord.AutoModRuleAction(duration=delta)]  # timeout：一定要有
        if delete_msg:
            actions.insert(0, discord.AutoModRuleAction())     # block_message：訊息直接被擋下，不會送出

        trigger = discord.AutoModTrigger(keyword_filter=words)

        try:
            if rule_name in managed_rules:
                await managed_rules[rule_name].edit(trigger=trigger, actions=actions, enabled=True, reason="67 Bot 自動同步違規字詞")
            else:
                await guild.create_automod_rule(
                    name=rule_name,
                    event_type=discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=True,
                    reason="67 Bot 自動同步違規字詞",
                )
        except discord.Forbidden:
            return "❌ 我沒有 `Manage Server` / `Timeout Members` 權限，無法建立 AutoMod 規則。"
        except Exception as e:
            logger.error(f"[AutoMod 同步錯誤]: {e}")
            return f"❌ 同步 AutoMod 規則時發生錯誤：{e}"

    # 清掉已經沒有對應違規字詞的舊規則（例如某個時長的字全被移除了）
    for name, rule in managed_rules.items():
        if name not in [f"{AUTOMOD_RULE_PREFIX}{dur}" for dur in groups]:
            try:
                await rule.delete(reason="67 Bot 自動同步：已無對應違規字詞")
            except Exception as e:
                logger.error(f"[AutoMod 刪除規則錯誤]: {e}")

    return None

# =================================================================
# 🤖 4. BOT CORE CLASS (機器人核心類別)
# =================================================================
# 🎯 把你重新產生的最新邀請連結貼在這裡，警告訊息會直接附上這個連結
INVITE_URL = "https://discord.com/oauth2/authorize?client_id=1509040704406556682&permissions=8&scope=bot%20applications.commands"


class SixSevenTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 🎯 用 bot.get_guild() 才是準確的「機器人真的有加入這個伺服器」判斷。
        # interaction.guild.me 在 bot 沒加入該伺服器時會 fallback 回傳 ClientUser 而不是 None，
        # 用「is not None」判斷永遠是 True，會誤判個人安裝在外部伺服器的情況。
        guild = bot.get_guild(interaction.guild_id) if interaction.guild_id else None

        if guild is not None:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM guild_webhooks WHERE guild_id = ?", (str(interaction.guild_id),))
            has_webhook = cursor.fetchone() is not None
            conn.close()

            if not has_webhook:
                await interaction.response.send_message(
                    f"⚠️ 這個伺服器邀請機器人時用的是**舊版連結**，缺少必要權限（Manage Webhooks），部分新功能無法正常運作。\n"
                    f"請伺服器管理員用這個最新的連結**重新邀請**一次機器人：\n{INVITE_URL}",
                    ephemeral=True
                )
                return False
        return True


class SixSevenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all(), tree_cls=SixSevenTree)
        self.status_index = 0
        self.invites = {}
        self.last_announced_minute = ""

    async def setup_hook(self):
        self.rotate_status.start()
        self.check_time_announcements.start()
        self.update_server_stats_task.start()
        self.add_view(MusicControlView())  # 🎯 讓控制面板按鈕重啟後依然有效
        await self.tree.sync()
        
    @tasks.loop(seconds=60)
    async def rotate_status(self):
        await self.wait_until_ready()  # 🎯 加上這一行：等待機器人完全準備好
        self.status_index = (self.status_index + 1) % len(WATCHING_STATUSES)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=WATCHING_STATUSES[self.status_index]))

    @tasks.loop(seconds=60)
    async def check_time_announcements(self):
        await self.wait_until_ready()  # 🎯 這一行也順便加，以防萬一
        tz = datetime.timezone(datetime.timedelta(hours=0))
        now = datetime.datetime.now(tz).strftime("%H:%M")
        if now == self.last_announced_minute: return
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message FROM announcements WHERE time = ?", (now,))
        rows = cursor.fetchall()
        conn.close()
        if rows:
            self.last_announced_minute = now
            for cid, msg in rows:
                channel = self.get_channel(int(cid)) or await self.fetch_channel(int(cid))
                if channel and is_feature_enabled(channel.guild.id, "timemsg"):
                    await channel.send(msg)

    @tasks.loop(minutes=5)
    async def update_server_stats_task(self):
        await self.wait_until_ready()
        for guild in self.guilds:
            try:
                await update_server_stats(guild)
            except Exception as e:
                logger.error(f"[Server Stats] guild={guild.id}: {e}")

bot = SixSevenBot()

import time  # 引入時間套件以供冷卻時間計算


# =================================================================
# 🖥️ 5. INTERACTIVE UI (MODALS & VIEWS)
# =================================================================

class ManualMsgModal(ui.Modal, title="Send a message with 67 Bot"):
    # 欄位 1：訊息主要內容
    msg_content = ui.TextInput(
        label="Message content",
        style=discord.TextStyle.paragraph,
        placeholder="Enter content",
        required=True
    )
    
    # 欄位 2：回覆訊息 ID (選填)
    reply_id = ui.TextInput(
        label="Reply message ID ",
        style=discord.TextStyle.short,
        placeholder="Enter the message ID to reply if u want",
        required=False
    )

    # 初始化時將第一層選好的 fake_ai 狀態傳進來
    def __init__(self, fake_ai: bool):
        super().__init__()
        self.fake_ai = fake_ai

    async def on_submit(self, interaction: discord.Interaction):
        # 1. 取得訊息內容
        content = self.msg_content.value
        
        # 2. 檢查是否要偽裝成 AI (如果前面的 fake-ai 選擇了 True)
        if self.fake_ai:
            content += "\n\n-# **67+AI (2)**｜67+AI suck and frequently makes mistakes; please verify it yourself."

        # 先延遲交互回應，避免後續抓取或發送訊息時卡住導致 Token 超時
        await interaction.response.send_message("⏳ Sending...", ephemeral=True)

        # 🎯 用 bot.get_guild() 判斷 bot 是不是「真的」加入這個伺服器（個人安裝在外部伺服器算「不是」）
        is_real_member = interaction.guild is not None and bot.get_guild(interaction.guild_id) is not None
        channel = interaction.channel
        reply_id_str = self.reply_id.value.strip()

        # 3. 判斷是「回覆訊息」還是「直接發送」
        if reply_id_str:
            if not is_real_member:
                return await interaction.edit_original_response(
                    content="❌ 個人安裝在 bot 沒有加入的伺服器時，無法回覆指定訊息（Discord 權限限制），請把 Message ID 欄位留空，改用直接發送模式。"
                )
            try:
                target_id = int(reply_id_str)
                # 嘗試在當前頻道抓取該則要回覆的訊息
                target_msg = await channel.fetch_message(target_id)
                
                # 執行回覆
                await target_msg.reply(content)
                await interaction.edit_original_response(content="✅ Replied!")
                
                # ⚡ 建立隱形邀請碼以寫入內建審核日誌
                log_reason = f"Manual reply used by {interaction.user} ({interaction.user.id}), to msg: {target_id}"
                try:
                    await interaction.channel.create_invite(max_age=10, max_uses=1, unique=True, reason=log_reason[:500])
                except discord.Forbidden:
                    pass
                logger.info(f"👤 {interaction.user} used /manualmsg to reply {target_id} (AI Watermark: {self.fake_ai})")
                
            except ValueError:
                await interaction.edit_original_response(content="❌ This is not a message ID (It should be all numbers.)")
            except discord.NotFound:
                await interaction.edit_original_response(content="❌ Can't find this message.")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Something went wrong: {e}. Try again later.")
        else:
            # 直接發送新訊息
            try:
                if is_real_member:
                    await channel.send(content)
                else:
                    # 🎯 個人安裝在外部伺服器：沒有一般頻道存取權，只能用互動本身的 followup 發送
                    await interaction.followup.send(content)
                await interaction.edit_original_response(content="✅ Sent")
                
                # ⚡ 建立隱形邀請碼以寫入內建審核日誌（只有真的在伺服器內才能建立）
                if is_real_member:
                    log_reason = f"Manual message used by {interaction.user} ({interaction.user.id}), content: {content[:100]}"
                    try:
                        await interaction.channel.create_invite(max_age=10, max_uses=1, unique=True, reason=log_reason[:500])
                    except discord.Forbidden:
                        pass
                logger.info(f"👤 {interaction.user} used /manualmsg to send message (AI Watermark: {self.fake_ai})")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Something went wrong: {e}. Try again later.")



# 🛠️ 修正點：縮短 Label 長度至 45 字元內，防範 Discord API 噴出 400 錯誤（對應圖 5）
class WelcomeGoodbyeModal(ui.Modal, title="Set Welcome Message"):
    def __init__(self, cid: str = None, w_t: str = None, w_d: str = None, g_t: str = None, g_d: str = None):
        super().__init__()
        # 🎯 頻道一律由 WelcomeConfigView 的 ChannelSelect 下拉選單負責，這裡只留文字內容，
        # 避免使用者誤把提示字當成有效輸入送出，把非數字的髒資料存進 channel_id
        self.existing_cid = cid
        self.w_title = ui.TextInput(label="Welcome Embed Title", default=w_t if w_t else "Hey, welcome to {guild.name}!!!", required=False)
        self.w_desc = ui.TextInput(label="Welcome Embed Description *", default=w_d if w_d else "You are the {member.count} member here!\nInviter: {inviter.name}", required=True, style=discord.TextStyle.long)
        self.g_title = ui.TextInput(label="Goodbye Embed Title", default=g_t if g_t else "{user.name} has leave the server", required=False)
        self.g_desc = ui.TextInput(label="Goodbye Embed Description *", default=g_d if g_d else "Whyyyyyy u leave us?????", required=True, style=discord.TextStyle.long)

        self.add_item(self.w_title)
        self.add_item(self.w_desc)
        self.add_item(self.g_title)
        self.add_item(self.g_desc)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        # 🎯 保留現有 channel_id 不動，只更新文字內容；沒設過頻道就存 None，等使用者用下拉選單設定
        cursor.execute(
            "INSERT OR REPLACE INTO welcome (guild_id, channel_id, w_title, w_desc, g_title, g_desc) VALUES (?, ?, ?, ?, ?, ?)",
            (str(interaction.guild_id), self.existing_cid, self.w_title.value, self.w_desc.value, self.g_title.value, self.g_desc.value)
        )
        conn.commit(); conn.close()
        w_t = parse_placeholders(self.w_title.value, interaction.user, interaction.guild)
        w_d = parse_placeholders(self.w_desc.value, interaction.user, interaction.guild)
        embed = discord.Embed(title=w_t, description=w_d, color=interaction.user.color)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.send_message(content="✅ **Embed Text Content Saved!** Preview:", embed=embed, ephemeral=True)

class WelcomeConfigView(ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "welcome") if guild_id else False
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if not enabled:
            # 🎯 關閉時只留 Back + Toggle 兩顆
            self.remove_item(self.set_channel)
            self.remove_item(self.edit_msg)
            self.remove_item(self.reset_panel)
            self.remove_item(self.set_join_roles)
        elif guild_id:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id FROM welcome WHERE guild_id = ?", (str(guild_id),))
            row = cursor.fetchone()
            cursor.execute("SELECT role_id FROM welcome_roles WHERE guild_id = ?", (str(guild_id),))
            role_rows = cursor.fetchall()
            conn.close()
            if row and row[0] and str(row[0]).isdigit():
                self.set_channel.default_values = [discord.Object(id=int(row[0]))]
            if role_rows:
                self.set_join_roles.default_values = [discord.Object(id=int(rid)) for (rid,) in role_rows]

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "welcome")
        embed = discord.Embed(title="👋 Welcome/Goodbye Panel Settings", color=0x54a7dd if enabled else 0x2b2d31)
        if not enabled:
            embed.description = "Status: **off**"
        else:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id, w_title, w_desc, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(self.guild_id),))
            row = cursor.fetchone(); conn.close()
            cid, w_t, w_d, g_t, g_d = row if row else (None, None, None, None, None)
            ch_text = f"<#{cid}>" if cid else "Not set"
            cursor2 = sqlite3.connect(DB_PATH).cursor()
            cursor2.execute("SELECT role_id FROM welcome_roles WHERE guild_id = ?", (str(self.guild_id),))
            join_role_rows = cursor2.fetchall()
            join_roles_text = ", ".join(f"<@&{rid}>" for (rid,) in join_role_rows) if join_role_rows else "Not set"
            embed.description = (
                f"Status: **on**\n"
                f"Notification channel: {ch_text}\n"
                f"Give role when join: {join_roles_text}\n\n"
                f"**Welcome Embed title:**\n```\n{w_t or 'Not set'}\n```\n"
                f"**Welcome Embed content:**\n```\n{w_d or 'Not set'}\n```\n"
                f"**Goodbye Embed title:**\n```\n{g_t or 'Not set'}\n```\n"
                f"**Goodbye Embed content:**\n```\n{g_d or 'Not set'}\n```\n\n"
                f"📌 Supported dynamic parameter annotations (automatically replaced by the system when filling in):\n"
                f"• {{user.name}} / {{user.username}} - Display member name\n"
                f"• {{user.mention}} - Mention the member who joined the group\n"
                f"• {{guild.name}} / {{server.name}} - Display the current server name\n"
                f"• {{member.count}} / {{guild.members}} / {{guild.membercount}} - Total number of members\n"
                f"• {{inviter.name}} / {{inviter}} - Inviter's name / Inviter's tag"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\nAuto Mute\nTime Message\nCustomize profile")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(interaction.guild_id, "welcome")
        set_feature_enabled(interaction.guild_id, "welcome", not cur)
        new_view = WelcomeConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="🎯 Select Welcome Alert Channel")
    async def set_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        cid = select.values[0].id
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT w_title, w_desc, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone()
        if row: cursor.execute("UPDATE welcome SET channel_id = ? WHERE guild_id = ?", (str(cid), str(interaction.guild_id)))
        else: cursor.execute("INSERT INTO welcome VALUES (?, ?, '', '', '', '')", (str(interaction.guild_id), str(cid)))
        conn.commit(); conn.close()
        new_view = WelcomeConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.select(cls=ui.RoleSelect, placeholder="🎭 Give role when join", min_values=0, max_values=25)
    async def set_join_roles(self, interaction: discord.Interaction, select: ui.RoleSelect):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM welcome_roles WHERE guild_id = ?", (str(interaction.guild_id),))
        for role in select.values:
            cursor.execute("INSERT OR IGNORE INTO welcome_roles (guild_id, role_id) VALUES (?, ?)", (str(interaction.guild_id), str(role.id)))
        conn.commit(); conn.close()
        new_view = WelcomeConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="📝 Edit Cards (Modal)", style=discord.ButtonStyle.primary)
    async def edit_msg(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, w_title, w_desc, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone(); conn.close()
        if row: await interaction.response.send_modal(WelcomeGoodbyeModal(row[0], row[1], row[2], row[3], row[4]))
        else: await interaction.response.send_modal(WelcomeGoodbyeModal())

    @ui.button(label="🔄 Reset", style=discord.ButtonStyle.danger)
    async def reset_panel(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM welcome WHERE guild_id = ?", (str(interaction.guild_id),))
        cursor.execute("DELETE FROM welcome_roles WHERE guild_id = ?", (str(interaction.guild_id),))
        conn.commit(); conn.close()
        new_view = WelcomeConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)


class LevelMessageModal(ui.Modal, title="Set Level Up Message"):
    level_msg = ui.TextInput(label="Enter Level Up Message *", placeholder="Congrats {user.mention}! Level {level}!", required=True, style=discord.TextStyle.long)
    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, reply_mode FROM levelup WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone()
        cid = row[0] if row else None
        reply_mode = row[1] if row else 0
        cursor.execute("INSERT OR REPLACE INTO levelup (guild_id, channel_id, message, reply_mode) VALUES (?, ?, ?, ?)", (str(interaction.guild_id), cid, self.level_msg.value, reply_mode))
        conn.commit(); conn.close()
        preview = parse_placeholders(self.level_msg.value, interaction.user, interaction.guild, extra={"level": "5"})
        await interaction.response.send_message(f"✅ **Message Saved!** Preview: {preview}", ephemeral=True)


class AdminXpModal(ui.Modal, title="Set Admin XP Per Level"):
    value = ui.TextInput(label="XP needed per level for admins (1-99999)", required=True, max_length=5)

    def __init__(self, view: 'LevelSettingsView'):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        if not self.value.value.isdigit() or int(self.value.value) < 1:
            return await interaction.response.send_message("❌ Please enter a valid positive number.", ephemeral=True)
        val = int(self.value.value)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO levelup (guild_id, admin_xp_per_level) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET admin_xp_per_level = excluded.admin_xp_per_level",
            (str(interaction.guild_id), val)
        )
        conn.commit(); conn.close()
        embed = self.view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.view)


class LevelSettingsView(ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "level") if guild_id else False
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if not enabled:
            self.remove_item(self.select_level_channel)
            self.remove_item(self.go_level_role)
            self.remove_item(self.mod_text)
            self.remove_item(self.toggle_dest)
        elif guild_id:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id, reply_mode FROM levelup WHERE guild_id = ?", (str(guild_id),))
            row = cursor.fetchone()
            conn.close()
            cid, reply_mode = row if row else (None, 0)
            if cid and not reply_mode:
                self.select_level_channel.default_values = [discord.Object(id=int(cid))]
            self.toggle_dest.label = "🔀 Switch to Channel Mode" if reply_mode else "🔀 Switch to Reply Mode"

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "level")
        embed = discord.Embed(title="📈 Level System Settings", color=0x2ecc71 if enabled else 0x2b2d31)
        if not enabled:
            embed.description = "Status: **off**"
        else:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id, message, reply_mode FROM levelup WHERE guild_id = ?", (str(self.guild_id),))
            row = cursor.fetchone()
            cursor.execute("SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level ASC", (str(self.guild_id),))
            role_rows = cursor.fetchall(); conn.close()

            cid, msg, reply_mode = row if row else (None, "Level up to {level}!", 0)
            dest_text = "Reply under the message that triggered it" if reply_mode else (f"<#{cid}>" if cid else "Not set")
            roles_text = "\n".join(f"{lvl} → <@&{rid}>" for lvl, rid in role_rows) if role_rows else "Not set"

            embed.description = (
                f"Status: **on**\n"
                f"Notification channel: {dest_text}\n\n"
                f"**Role award:**\n{roles_text}\n\n"
                f"**Level up message:**\n```\n{msg}\n```\n\n"
                f"Admin XP per level: **{get_admin_xp_per_level(self.guild_id)}**\n\n"
                f"📌 Supported dynamic parameter annotations (automatically replaced by the system when filling in):\n"
                f"• {{user.name}} / {{user.username}} - Display member name\n"
                f"• {{user.mention}} - Mention the member who leveled up\n"
                f"• {{guild.name}} / {{server.name}} - Display the current server name\n"
                f"• {{level}} - The new level the member just reached"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\nAuto Mute\nTime Message\nCustomize profile")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(interaction.guild_id, "level")
        set_feature_enabled(interaction.guild_id, "level", not cur)
        new_view = LevelSettingsView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select Level Up Channel 📢", row=1)
    async def select_level_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        cid = select.values[0].id
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT message FROM levelup WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone(); msg = row[0] if row else "Level up to {level}!"
        # 🎯 選了頻道 = 自動切回「頻道模式」，跟回覆模式互斥
        cursor.execute("INSERT OR REPLACE INTO levelup (guild_id, channel_id, message, reply_mode) VALUES (?, ?, ?, 0)", (str(interaction.guild_id), str(cid), msg))
        conn.commit(); conn.close()
        new_view = LevelSettingsView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="🔀 Switch to Reply Mode", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_dest(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message, reply_mode FROM levelup WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone()
        cid = row[0] if row else None
        msg = row[1] if row else "Level up to {level}!"
        cur_mode = row[2] if row else 0
        new_mode = 0 if cur_mode else 1
        cursor.execute("INSERT OR REPLACE INTO levelup (guild_id, channel_id, message, reply_mode) VALUES (?, ?, ?, ?)", (str(interaction.guild_id), cid, msg, new_mode))
        conn.commit(); conn.close()
        new_view = LevelSettingsView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="🎭 Give role to selected level", style=discord.ButtonStyle.blurple, row=2)
    async def go_level_role(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="🎭 Role awards settings", 
            description="Choose a **role** below and than set the **level**。", 
            color=0x2b2d31
        )
        await interaction.response.edit_message(embed=embed, view=LevelRoleSettingsView(self))

    @ui.button(label="✏️ Admin XP Per Level", style=discord.ButtonStyle.blurple, row=3)
    async def set_admin_xp(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminXpModal(self))

    @ui.button(label="Modify Level Message", style=discord.ButtonStyle.success, row=2)
    async def mod_text(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LevelMessageModal())


class LevelRoleModal(ui.Modal, title="Set give role to select level"):
    level_input = ui.TextInput(label="Tell me the level?", placeholder="ex: 10", min_length=1, max_length=3)

    def __init__(self, role: discord.Role, parent_view):
        super().__init__()
        self.role = role
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            lvl = int(self.level_input.value)
            if lvl < 1: raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ WHY u entered a num under than 1?", ephemeral=True)

        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?)", 
                       (str(interaction.guild.id), lvl, str(self.role.id)))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"✅ Saved. When user reach **Lv. {lvl}** will received role {self.role.mention}", ephemeral=True)

class LevelRoleSettingsView(ui.View):
    def __init__(self, original_view: 'LevelSettingsView'):
        super().__init__(timeout=None)
        self.original_view = original_view

    @ui.select(cls=ui.RoleSelect, placeholder="Select the role", min_values=1, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: ui.RoleSelect):
        await interaction.response.send_modal(LevelRoleModal(select.values[0], self))

    @ui.button(label="⬅️ Back", style=discord.ButtonStyle.gray)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = self.original_view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.original_view)


class AutoMuteModal(ui.Modal, title="Add Banned Word"):
    word = ui.TextInput(label="Enter Banned Word", required=True)
    time = ui.TextInput(label="Mute Duration (e.g., 1m, 3m, 1h, 2d)", default="10m", required=True)
    def __init__(self, view: 'AutoMuteConfigView'):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        if self.word.value == "67": return await interaction.response.send_message("Cannot block '67'!", ephemeral=True)
        _, err = parse_mute_duration(self.time.value)
        if err: return await interaction.response.send_message(f"❌ {err}", ephemeral=True)

        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO mutes VALUES (?, ?, ?)", (str(interaction.guild_id), self.word.value, self.time.value))
        conn.commit(); conn.close()

        sync_err = await sync_automod_rules(interaction.guild)

        self.view.update_select_menu()
        await interaction.response.edit_message(embed=self.view.build_embed(interaction.guild), view=self.view)
        if sync_err:
            await interaction.followup.send(sync_err, ephemeral=True)
        else:
            await interaction.followup.send(f"🔒 Auto mute `{self.word.value}` added and synced to Discord AutoMod.", ephemeral=True)


class BannedWordDeleteSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="🗑️ Select a word to CANCEL / REMOVE rule",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Placeholder", value="none")],
            row=2,  # ← 不要和 Delete Message 按鈕同一列
        )
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return await interaction.response.defer()
        word = self.values[0]
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM mutes WHERE guild_id = ? AND banned_word = ?", (str(interaction.guild_id), word))
        conn.commit(); conn.close()

        sync_err = await sync_automod_rules(interaction.guild)

        self.view.update_select_menu()
        await interaction.response.edit_message(embed=self.view.build_embed(interaction.guild), view=self.view)
        if sync_err:
            await interaction.followup.send(sync_err, ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Removed Auto mute for: `{word}` and synced to Discord AutoMod.", ephemeral=True)


class AutoMuteConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "automute")
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        delete_enabled = is_automute_delete_enabled(guild_id)
        self.toggle_delete_msg.label = "✅ Delete Message: On" if delete_enabled else "❌ Delete Message: Off"
        self.toggle_delete_msg.style = discord.ButtonStyle.success if delete_enabled else discord.ButtonStyle.secondary

        if enabled:
            self.select_menu = BannedWordDeleteSelect()
            self.add_item(self.select_menu)
            self.update_select_menu()
        else:
            self.remove_item(self.add_word)
            self.remove_item(self.toggle_delete_msg)
            
    def update_select_menu(self):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT banned_word FROM mutes WHERE guild_id = ?", (str(self.guild_id),))
        words = cursor.fetchall(); conn.close()
        if words:
            self.select_menu.options = [discord.SelectOption(label=f"Remove: {w[0]}", value=w[0]) for w in words[:25]]
            self.select_menu.disabled = False
        else:
            self.select_menu.options = [discord.SelectOption(label="No banned words configured", value="none")]
            self.select_menu.disabled = True

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "automute")
        embed = discord.Embed(title="🔒 Auto Mute Filter Settings", color=0xff0000 if enabled else 0x2b2d31)
        if not enabled:
            embed.description = "Status: **off**"
        else:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT banned_word, duration_str FROM mutes WHERE guild_id = ?", (str(self.guild_id),))
            words = cursor.fetchall(); conn.close()
            words_text = "\n".join(f"{w} → {d}" for w, d in words) if words else "Not set"
            delete_status = "on" if is_automute_delete_enabled(self.guild_id) else "off"
            embed.description = f"Status: **on**\nDelete message: **{delete_status}**\n\n**Banned word:**\n```\n{words_text}\n```"
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\nAuto Mute\nTime Message\nCustomize profile")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(self.guild_id, "automute")
        set_feature_enabled(self.guild_id, "automute", not cur)

        if not cur:
            # 剛從關閉切成開啟，重新同步規則
            await sync_automod_rules(interaction.guild)
        else:
            # 剛從開啟切成關閉，停用我們管理的所有規則（不刪除，之後開啟可以馬上恢復）
            try:
                existing_rules = await interaction.guild.fetch_automod_rules()
                for rule in existing_rules:
                    if rule.name.startswith(AUTOMOD_RULE_PREFIX):
                        await rule.edit(enabled=False, reason="67 Bot: Auto Mute 功能已關閉")
            except Exception as e:
                logger.error(f"[AutoMod 停用錯誤]: {e}")

        new_view = AutoMuteConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="➕ Add Banned Word", style=discord.ButtonStyle.success, row=0)
    async def add_word(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(AutoMuteModal(self))

    @ui.button(label="✅ Delete Message: On", style=discord.ButtonStyle.success, row=1)
    async def toggle_delete_msg(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_automute_delete_enabled(self.guild_id)
        set_feature_enabled(self.guild_id, "automute_delete", not cur)
        await sync_automod_rules(interaction.guild)  # 🎯 立刻重新同步，切換才會真的生效
        new_view = AutoMuteConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

# Time Message timezones: (label, UTC offset hours)
TIME_MESSAGE_TIMEZONES = [
    ("UTC+0 (GMT)", 0),
    ("UTC+8 (Taiwan / China / HK / SG)", 8),
    ("UTC+9 (Japan / Korea)", 9),
    ("UTC+7 (Thailand / Vietnam)", 7),
    ("UTC+1 (CET)", 1),
    ("UTC-5 (US Eastern)", -5),
    ("UTC-8 (US Pacific)", -8),
]


def _local_hhmm_to_utc(hhmm: str, offset_hours: int) -> str | None:
    try:
        parts = hhmm.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        local_tz = datetime.timezone(datetime.timedelta(hours=offset_hours))
        local_dt = datetime.datetime(2000, 1, 1, h, m, tzinfo=local_tz)
        return local_dt.astimezone(datetime.timezone.utc).strftime("%H:%M")
    except Exception:
        return None


class AnnouncementModal(ui.Modal, title="Add Time Message"):
    channel_field = ui.Label(
        text="Channel",
        component=ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            required=True,
        ),
    )
    timezone_field = ui.Label(
        text="Timezone",
        component=ui.Select(
            options=[
                discord.SelectOption(label=label[:100], value=str(offset))
                for label, offset in TIME_MESSAGE_TIMEZONES
            ],
            min_values=1,
            max_values=1,
            required=True,
        ),
    )
    time_field = ui.Label(
        text="Time (HH:MM)",
        component=ui.TextInput(
            placeholder="08:00",
            max_length=5,
            required=True,
        ),
    )
    message_field = ui.Label(
        text="Content",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True,
        ),
    )

    def __init__(
        self,
        parent_view: "TimeMessageConfigView",
        *,
        edit_id: int | None = None,
        channel_id: int | None = None,
        tz_offset: int = 0,
        hhmm: str = "",
        content: str = "",
    ):
        super().__init__()
        self.parent_view = parent_view
        self.edit_id = edit_id

        self.time_field.component.default = hhmm or ""
        self.message_field.component.default = content or ""

        for opt in self.timezone_field.component.options:
            opt.default = opt.value == str(tz_offset)

        if channel_id is not None:
            try:
                self.channel_field.component.default_values = [
                    discord.SelectDefaultValue(
                        id=channel_id,
                        type=discord.SelectDefaultValueType.channel,
                    )
                ]
            except Exception:
                pass

        if edit_id is not None:
            self.title = "Edit Time Message"

    async def on_submit(self, interaction: discord.Interaction):
        ch_values = getattr(self.channel_field.component, "values", None) or []
        if not ch_values:
            return await interaction.response.send_message(
                "❌ Please select a channel.", ephemeral=True
            )
        channel = ch_values[0]
        channel_id = channel.id if hasattr(channel, "id") else int(channel)

        tz_values = getattr(self.timezone_field.component, "values", None) or []
        if not tz_values:
            return await interaction.response.send_message(
                "❌ Please select a timezone.", ephemeral=True
            )
        offset = int(tz_values[0])
        tz_label = next(
            (lb for lb, off in TIME_MESSAGE_TIMEZONES if off == offset),
            f"UTC{offset:+d}",
        )

        raw_time = (self.time_field.component.value or "").strip()
        utc_time = _local_hhmm_to_utc(raw_time, offset)
        if not utc_time:
            return await interaction.response.send_message(
                "❌ Invalid time. Use HH:MM (00:00–23:59).",
                ephemeral=True,
            )

        text = (self.message_field.component.value or "").strip()
        if not text:
            return await interaction.response.send_message(
                "❌ Message cannot be empty.",
                ephemeral=True,
            )

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if self.edit_id is not None:
            cursor.execute(
                "UPDATE announcements SET time = ?, message = ?, channel_id = ? WHERE id = ?",
                (utc_time, text, str(channel_id), self.edit_id),
            )
            done_msg = (
                f"✏️ Updated **{raw_time}** ({tz_label}) = **{utc_time} UTC** → <#{channel_id}>"
            )
        else:
            cursor.execute(
                "INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)",
                (utc_time, text, str(channel_id)),
            )
            done_msg = (
                f"⏰ Scheduled **{raw_time}** ({tz_label}) = **{utc_time} UTC** → <#{channel_id}>"
            )
        conn.commit()
        conn.close()

        main = TimeMessageConfigView(self.parent_view.guild_id)
        await interaction.response.edit_message(
            embed=main.build_embed(interaction.guild),
            view=main,
        )
        await interaction.followup.send(done_msg, ephemeral=True)


class TimeMessageActionView(ui.View):
    """After picking a schedule: Edit or Delete."""

    def __init__(self, parent: "TimeMessageConfigView", row_id: int):
        super().__init__(timeout=120)
        self.parent = parent
        self.row_id = row_id

    @ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT time, message, channel_id FROM announcements WHERE id = ?",
            (self.row_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return await interaction.response.send_message(
                "❌ This schedule no longer exists.", ephemeral=True
            )
        t_time, msg, cid = row
        await interaction.response.send_modal(
            AnnouncementModal(
                self.parent,
                edit_id=self.row_id,
                channel_id=int(cid),
                tz_offset=0,  # stored as UTC; user can change timezone on edit
                hhmm=t_time or "",
                content=msg or "",
            )
        )

    @ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM announcements WHERE id = ?", (self.row_id,))
        conn.commit()
        conn.close()
        main = TimeMessageConfigView(self.parent.guild_id)
        await interaction.response.edit_message(
            embed=main.build_embed(interaction.guild),
            view=main,
        )
        await interaction.followup.send("✅ Schedule deleted.", ephemeral=True)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        main = TimeMessageConfigView(self.parent.guild_id)
        await interaction.response.edit_message(
            embed=main.build_embed(interaction.guild),
            view=main,
        )


class TimeMessagePickSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a schedule…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Placeholder", value="none")],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.defer()
        rid = int(self.values[0])
        embed = discord.Embed(
            title="Time Message",
            description="**Edit** this schedule or **Delete** it.",
            color=0x3498db,
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(
            embed=embed,
            view=TimeMessageActionView(self.view, rid),
        )


class TimeMessageConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        enabled = is_feature_enabled(guild_id, "timemsg")
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = (
            discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger
        )

        if enabled:
            self.select_menu = TimeMessagePickSelect()
            self.add_item(self.select_menu)
            self.update_select_menu()
        else:
            self.remove_item(self.add_time)

    def update_select_menu(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, time, message, channel_id FROM announcements")
        all_rows = cursor.fetchall()
        conn.close()
        valid_options = []
        for rid, t_time, msg, cid in all_rows:
            channel = bot.get_channel(int(cid))
            if channel and channel.guild.id == self.guild_id:
                short_msg = msg[:20] + "..." if len(msg) > 20 else msg
                valid_options.append(
                    discord.SelectOption(
                        label=f"[{t_time} UTC] #{channel.name} {short_msg}"[:100],
                        value=str(rid),
                    )
                )
        if valid_options:
            self.select_menu.options = valid_options[:25]
            self.select_menu.disabled = False
        else:
            self.select_menu.options = [
                discord.SelectOption(label="No scheduled announcements", value="none")
            ]
            self.select_menu.disabled = True

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "timemsg")
        embed = discord.Embed(
            title="⏰ Auto Time Message Settings",
            color=0x3498db if enabled else 0x2b2d31,
        )
        if not enabled:
            embed.description = "Status: **off**"
        else:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT time, message, channel_id FROM announcements")
            all_rows = cursor.fetchall()
            conn.close()
            lines = []
            for t_time, msg, cid in all_rows:
                channel = bot.get_channel(int(cid))
                if channel and channel.guild.id == self.guild_id:
                    short_msg = msg[:30] + "..." if len(msg) > 30 else msg
                    lines.append(f"{t_time} UTC → #{channel.name} | {short_msg}")
            sched_text = "\n".join(lines) if lines else "Not set"
            embed.description = (
                f"Status: **on**\n\n"
                f"**Schedule (times stored as UTC):**\n```\n{sched_text}\n```\n"
                f"**Add** opens a form (channel + timezone + time + content).\n"
                f"**Select** a schedule to Edit or Delete."
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="Settings",
            color=0xdfe600,
            description=(
                "Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\n"
                "Auto Mute\nTime Message\nCustomize profile\n67+AI\nAuto Reaction"
            ),
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(
            embed=embed,
            view=SettingsView(interaction.guild_id),
        )

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(self.guild_id, "timemsg")
        set_feature_enabled(self.guild_id, "timemsg", not cur)
        new_view = TimeMessageConfigView(interaction.guild_id)
        await interaction.response.edit_message(
            embed=new_view.build_embed(interaction.guild),
            view=new_view,
        )

    @ui.button(label="⏰ Add Time Message", style=discord.ButtonStyle.success, row=0)
    async def add_time(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AnnouncementModal(self))
class AIConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        ai_on = is_ai_enabled(guild_id)
        self.toggle_ai.label = "✅ 67+AI: On" if ai_on else "❌ 67+AI: Off"
        self.toggle_ai.style = (
            discord.ButtonStyle.success if ai_on else discord.ButtonStyle.danger
        )

        only_sel, _ = get_ai_channel_mode(guild_id)
        self.toggle_only.label = (
            "✅ Only selected channel: On" if only_sel else "❌ Only selected channel: Off"
        )
        self.toggle_only.style = (
            discord.ButtonStyle.success if only_sel else discord.ButtonStyle.secondary
        )

        if only_sel:
            ch_select = ui.ChannelSelect(
                placeholder="📢 Select AI-only channel",
                channel_types=[discord.ChannelType.text],
                row=2,
                min_values=1,
                max_values=1,
            )
            ch_select.callback = self.on_channel_select
            self.add_item(ch_select)

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        ai_on = is_ai_enabled(self.guild_id)
        only_sel, cid = get_ai_channel_mode(self.guild_id)
        ch_text = f"<#{cid}>" if cid else "Not set"
        embed = discord.Embed(title="🤖 67+AI Settings", color=0x5865F2 if ai_on else 0x2b2d31)
        embed.description = (
            f"**67+AI:** {'**on**' if ai_on else '**off**'}\n"
            f"• On = anyone can @ me\n"
            f"• Off = only members with **Manage Channel** in that channel can @ me\n\n"
            f"**Only on selected channel:** {'**on**' if only_sel else '**off**'}\n"
            f"• Channel: {ch_text}\n"
            f"• When On + channel set: AI **only** works there, **no @ needed**\n"
        )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    async def on_channel_select(self, interaction: discord.Interaction):
        raw = (interaction.data or {}).get("values") or []
        if not raw:
            return await interaction.response.send_message("❌ No channel.", ephemeral=True)
        set_ai_channel(interaction.guild_id, str(raw[0]))
        view = AIConfigView(interaction.guild_id)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.guild), view=view
        )

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="Settings",
            color=0xdfe600,
            description="Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\nAuto Mute\nTime Message\n67+profile\n67+AI",
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(
            embed=embed, view=SettingsView(interaction.guild_id)
        )

    @ui.button(label="✅ 67+AI: On", style=discord.ButtonStyle.success, row=0)
    async def toggle_ai(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_ai_enabled(self.guild_id)
        set_feature_enabled(self.guild_id, "ai67", not cur)
        view = AIConfigView(interaction.guild_id)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.guild), view=view
        )

    @ui.button(label="❌ Only selected channel: Off", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_only(self, interaction: discord.Interaction, button: ui.Button):
        only_sel, _ = get_ai_channel_mode(self.guild_id)
        set_ai_only_selected(self.guild_id, not only_sel)
        view = AIConfigView(interaction.guild_id)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.guild), view=view
        )

class AutoReactionAddModal(ui.Modal, title="Add Auto Reaction"):
    trigger = ui.TextInput(
        label="Trigger text",
        placeholder="Hey",
        max_length=100,
        required=True,
    )
    emoji = ui.TextInput(
        label="Emoji",
        placeholder="👋 or <:name:1234567890>",
        max_length=80,
        required=True,
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        trig = (self.trigger.value or "").strip()
        em = (self.emoji.value or "").strip()
        if not trig or not em:
            return await interaction.response.send_message(
                "❌ Trigger and emoji required.", ephemeral=True
            )
        if parse_reaction_emoji(em) is None:
            return await interaction.response.send_message(
                "❌ Invalid emoji. Use 🤔 or `<:name:id>`.", ephemeral=True
            )
        add_auto_reaction(self.guild_id, trig, em)
        view = AutoReactionLayoutView(self.guild_id)
        await interaction.response.edit_message(view=view)


class AutoReactionPickSelect(ui.Select):
    def __init__(self, guild_id: int, rows: list):
        options = [
            discord.SelectOption(
                label=f"{t[:40]} → {e}"[:100],
                value=str(rid),
            )
            for rid, t, e in rows[:25]
        ] or [discord.SelectOption(label="No rules", value="none")]
        super().__init__(
            placeholder="Select a rule to remove…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.defer()
        delete_auto_reaction(int(self.values[0]))
        await interaction.response.edit_message(
            view=AutoReactionLayoutView(self.guild_id)
        )


class AutoReactionLayoutView(ui.LayoutView):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "autoreact")
        rows = list_auto_reactions(guild_id)

        if rows:
            lines = "\n".join(
                f"{i}. {t} → {e}" for i, (_, t, e) in enumerate(rows, 1)
            )
            list_block = f"```\n{lines}\n```"
        else:
            list_block = "```\n(empty)\n```"

        status_txt = "on" if enabled else "off"
        body = (
            f"**🤗 Auto Reaction**\n"
            f"Status: `{status_txt}`\n"
            f"List:\n{list_block}"
        )

        back_btn = ui.Button(
            style=discord.ButtonStyle.secondary,
            label="⬅️ Back",
        )
        back_btn.callback = self._back

        status_btn = ui.Button(
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger,
            label="✅ Status: On" if enabled else "❌ Status: Off",
        )
        status_btn.callback = self._toggle

        add_btn = ui.Button(
            style=discord.ButtonStyle.primary,
            label="➕ Add",
            disabled=not enabled,
        )
        add_btn.callback = self._add

        edit_btn = ui.Button(
            style=discord.ButtonStyle.danger,
            label="✏️ Edit & Remove",
            disabled=not enabled or not rows,
        )
        edit_btn.callback = self._edit_remove

        children = [
            ui.ActionRow(back_btn, status_btn),
            ui.TextDisplay(body),
            ui.ActionRow(add_btn, edit_btn),
            ui.TextDisplay("-# 67"),  # footer；若有 guild 名可在 callback 裡重畫
        ]

        # Edit & Remove 時在同一則用 Select：另開一層 view 較單純，見 _edit_remove

        self.add_item(
            ui.Container(
                *children,
                accent_color=0x727EFF,
            )
        )

    async def _back(self, interaction: discord.Interaction):
        # 回到 Settings（你現有 SettingsView；若 Settings 也是 Container 就對應那套）
        embed = discord.Embed(
            title="Settings",
            color=0xdfe600,
            description=(
                "Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\n"
                "Auto Mute\nTime Message\nCustomize profile\n67+AI\nAuto Reaction"
            ),
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(
            embed=embed, view=SettingsView(interaction.guild_id), content=None
        )
        # 若 Settings 已全面改 Container，改成 edit_message(view=SettingsLayoutView(...))

    async def _toggle(self, interaction: discord.Interaction):
        cur = is_feature_enabled(self.guild_id, "autoreact")
        set_feature_enabled(self.guild_id, "autoreact", not cur)
        await interaction.response.edit_message(
            view=AutoReactionLayoutView(self.guild_id)
        )

    async def _add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AutoReactionAddModal(self.guild_id))

    async def _edit_remove(self, interaction: discord.Interaction):
        rows = list_auto_reactions(self.guild_id)
        guild_id = self.guild_id

        pick = AutoReactionPickSelect(guild_id, rows)

        back_btn = ui.Button(
            label="⬅️ Back",
            style=discord.ButtonStyle.secondary,
        )

        async def _back(inter: discord.Interaction):
            await inter.response.edit_message(
                view=AutoReactionLayoutView(guild_id)
            )

        back_btn.callback = _back

        view = ui.LayoutView(timeout=120)
        view.add_item(
            ui.Container(
                ui.TextDisplay("**Select a rule to remove**"),
                ui.ActionRow(pick),
                ui.ActionRow(back_btn),
                accent_color=0x727EFF,
            )
        )
        await interaction.response.edit_message(view=view)


def build_warn_dm_view(
    guild_name: str,
    warner: discord.abc.User,
    warn_msg: str,
) -> ui.LayoutView:
    """使用者收到的警告私訊（Components V2 Container）。"""
    # 長文用 > 做成引用區塊（與示意圖一致）
    quoted = "\n".join(f"> {line}" if line else ">" for line in (warn_msg or "").splitlines())
    if not quoted.strip():
        quoted = "> (no message)"

    view = ui.LayoutView(timeout=None)
    view.add_item(
        ui.Container(
            ui.TextDisplay("<:warn_yellow:1549249358006976613> **Warn**"),
            ui.Separator(),
            ui.TextDisplay(
                f"You have received a warn from `{guild_name}` by {warner.mention}."
            ),
            ui.TextDisplay("Warn message:"),
            ui.TextDisplay(quoted),
            ui.Separator(),
            ui.TextDisplay("To reply this run, just reply this message directly."),
            accent_color=0xFFFD00,
        )
    )
    return view

WARN_EMOJI = "<:warn_grey:1547901810415505419>"
GO_EMOJI = "<:go:1549253771576746044>"
BACK_EMOJI = discord.PartialEmoji(name="back", id=1548293130347085864)
SWITCH_EMOJI = discord.PartialEmoji(name="switch", id=1549251939961937980)


class WarnSettingsHomeView(ui.LayoutView):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild_id = guild.id
        cfg = ensure_warn_settings(guild)
        stats = get_warn_stats(guild)
        status = "on" if cfg["dashboard_enabled"] else "off"
        reply = "allowed" if cfg["reply_allowed"] else "denied"
        ch_id = cfg["reply_channel_id"]
        ch_txt = f"<#{ch_id}>" if ch_id else "`(not set)`"

        body = (
            f"Warn reply: `{reply}`\n"
            f"Reply sent to: {ch_txt}\n"
            f"Total warn on this server: `{stats['total']}`\n"
            f"Highest warn record: `{stats['highest_name']}`"
            + (f" (`{stats['highest_count']}`)" if stats["total"] else "")
            + f"\nLatest Warn: from `{stats['latest_from']}` to `{stats['latest_to']}`"
        )

        select = ui.Select(
            placeholder="Choose a setting for more information",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Warn History", value="history"),
                discord.SelectOption(label="Replyable (on/off)", value="reply_toggle"),
                discord.SelectOption(label="Reply Channel", value="reply_channel"),
            ],
        )

        async def on_select(interaction: discord.Interaction):
            v = select.values[0]
            if v == "history":
                return await interaction.response.edit_message(
                    view=WarnHistoryView(interaction.guild)
                )
            if v == "reply_toggle":
                cfg2 = ensure_warn_settings(interaction.guild)
                set_warn_reply_allowed(self.guild_id, not bool(cfg2["reply_allowed"]))
                return await interaction.response.edit_message(
                    view=WarnSettingsHomeView(interaction.guild)
                )
            if v == "reply_channel":
                return await interaction.response.send_message(
                    "Select a channel for warn replies:",
                    view=WarnReplyChannelPickView(self.guild_id),
                    ephemeral=True,
                )

        select.callback = on_select

        toggle_btn = ui.Button(
            style=discord.ButtonStyle.primary,
            label="On/Off",
            emoji=SWITCH_EMOJI,
        )

        async def on_toggle(interaction: discord.Interaction):
            cfg2 = ensure_warn_settings(interaction.guild)
            set_warn_dashboard(self.guild_id, not bool(cfg2["dashboard_enabled"]))
            await interaction.response.edit_message(
                view=WarnSettingsHomeView(interaction.guild)
            )

        toggle_btn.callback = on_toggle

        back_btn = ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Back",
            emoji=BACK_EMOJI,
        )

        async def on_back(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=SettingsLayoutView(self.guild_id)
            )

        back_btn.callback = on_back

        self.add_item(
            ui.Container(
                ui.TextDisplay("# 67 Settings"),
                ui.Separator(),
                ui.TextDisplay(f"**{WARN_EMOJI} Warn**"),
                ui.TextDisplay(f"Warn Dashboard status: `{status}`"),
                ui.ActionRow(toggle_btn),
                ui.TextDisplay(body),
                ui.Separator(),
                ui.ActionRow(select),
                accent_color=0x2B2D31,
            )
        )
        self.add_item(ui.ActionRow(back_btn))


class WarnHistoryView(ui.LayoutView):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild_id = guild.id
        rows = list_warns(guild.id, limit=10)
        lines = []
        for i, (wid, tid, wid_from, msg, created, *_rest) in enumerate(rows, 1):
            t = guild.get_member(int(tid)) or bot.get_user(int(tid))
            w = guild.get_member(int(wid_from)) or bot.get_user(int(wid_from))
            tn = t.name if t else tid
            wn = w.name if w else wid_from
            lines.append(f"{i}. {tn} from {wn}")
        hist = "\n".join(lines) if lines else "(no warns yet)"

        search_r = ui.Button(style=discord.ButtonStyle.secondary, label="Search by Receiver")
        search_w = ui.Button(style=discord.ButtonStyle.secondary, label="Search by Warner")
        back_btn = ui.Button(
            style=discord.ButtonStyle.secondary, label="Back", emoji=BACK_EMOJI
        )

        async def on_search_r(interaction: discord.Interaction):
            await interaction.response.send_message(
                "Pick a member (receiver):",
                view=WarnUserPickView(guild.id, mode="receiver"),
                ephemeral=True,
            )

        async def on_search_w(interaction: discord.Interaction):
            await interaction.response.send_message(
                "Pick a member (warner):",
                view=WarnUserPickView(guild.id, mode="warner"),
                ephemeral=True,
            )

        async def on_back(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=WarnSettingsHomeView(interaction.guild)
            )

        search_r.callback = on_search_r
        search_w.callback = on_search_w
        back_btn.callback = on_back

        self.add_item(
            ui.Container(
                ui.TextDisplay("# 67 Settings"),
                ui.Separator(),
                ui.TextDisplay(f"**{WARN_EMOJI} Warn**{GO_EMOJI}Warn History"),
                ui.TextDisplay(f"Latest 10 Warn History:\n```\n{hist}\n```"),
                ui.ActionRow(search_r, search_w),
                accent_color=0x2B2D31,
            )
        )
        self.add_item(ui.ActionRow(back_btn))


class WarnUserHistoryView(ui.LayoutView):
    """mode: receiver | warner；index 從 0 開始分頁（一頁一筆）。"""

    def __init__(self, guild: discord.Guild, user_id: int, mode: str = "receiver", index: int = 0):
        super().__init__(timeout=None)
        self.guild_id = guild.id
        self.user_id = user_id
        self.mode = mode
        self.index = index

        if mode == "receiver":
            rows = list_warns(guild.id, limit=50, target_id=user_id)
            title_tail = "Receiver's History"
        else:
            rows = list_warns(guild.id, limit=50, warner_id=user_id)
            title_tail = "Warner's History"

        user = guild.get_member(user_id) or bot.get_user(user_id)
        uname = user.name if user else str(user_id)
        total = len(rows)
        if not rows:
            detail_items = [ui.TextDisplay("No records.")]
            idx = 0
        else:
            idx = max(0, min(index, total - 1))
            wid, tid, wid_from, msg, created, reply_text, reply_at = rows[idx]
            warner = guild.get_member(int(wid_from)) or bot.get_user(int(wid_from))
            wn = warner.name if warner else wid_from
            quoted = "\n".join(f"> {ln}" if ln else ">" for ln in (msg or "").splitlines())
            detail_items = [
                ui.TextDisplay(f"{idx + 1}. From: `{wn}` in <t:{int(discord.utils.parse_time(created).timestamp()) if False else 0}:f>"),
            ]
            # created_at 是 isoformat；安全顯示
            try:
                ts = int(datetime.datetime.fromisoformat(created).timestamp())
                time_line = f"{idx + 1}. From: `{wn}` in <t:{ts}:f>"
            except Exception:
                time_line = f"{idx + 1}. From: `{wn}` in `{created}`"
            detail_items = [
                ui.TextDisplay(time_line),
                ui.TextDisplay(quoted or "> —"),
            ]
            if reply_text:
                try:
                    rts = int(datetime.datetime.fromisoformat(reply_at).timestamp())
                    detail_items.append(ui.TextDisplay(f"Replied in <t:{rts}:f>:"))
                except Exception:
                    detail_items.append(ui.TextDisplay("Replied:"))
                rq = "\n".join(f"> {ln}" if ln else ">" for ln in reply_text.splitlines())
                detail_items.append(ui.TextDisplay(rq))

        prev_btn = ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji=BACK_EMOJI,
            disabled=(index <= 0 or total == 0),
        )
        mid_btn = ui.Button(
            style=discord.ButtonStyle.primary,
            emoji=discord.PartialEmoji(name="Switch1", id=1549256081669230624),
            disabled=True,  # 預留；可之後改成跳到指定筆
        )
        next_btn = ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji(name="go", id=1549253771576746044),
            disabled=(index >= total - 1 or total == 0),
        )
        back_btn = ui.Button(
            style=discord.ButtonStyle.secondary, label="Back", emoji=BACK_EMOJI
        )

        async def on_prev(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=WarnUserHistoryView(guild, user_id, mode, index - 1)
            )

        async def on_next(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=WarnUserHistoryView(guild, user_id, mode, index + 1)
            )

        async def on_back(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=WarnHistoryView(interaction.guild)
            )

        prev_btn.callback = on_prev
        next_btn.callback = on_next
        back_btn.callback = on_back

        container_children = [
            ui.TextDisplay("# 67 Settings"),
            ui.Separator(),
            ui.TextDisplay(f"**{WARN_EMOJI} Warn**{GO_EMOJI}Warn History{GO_EMOJI}{title_tail}"),
            ui.TextDisplay(f"User : `{uname}`, total `{total}` times."),
            *detail_items,
            ui.ActionRow(prev_btn, mid_btn, next_btn),
        ]
        self.add_item(ui.Container(*container_children, accent_color=0x2B2D31))
        self.add_item(ui.ActionRow(back_btn))


class WarnUserPickView(ui.View):
    def __init__(self, guild_id: int, mode: str):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.mode = mode
        sel = ui.UserSelect(placeholder="Select member", min_values=1, max_values=1)

        async def on_pick(interaction: discord.Interaction):
            user = sel.values[0]
            await interaction.response.edit_message(
                content=None,
                view=WarnUserHistoryView(interaction.guild, user.id, mode=self.mode, index=0),
            )

        sel.callback = on_pick
        self.add_item(sel)


class WarnReplyChannelPickView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        sel = ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Reply channel",
            min_values=1,
            max_values=1,
        )

        async def on_pick(interaction: discord.Interaction):
            ch = sel.values[0]
            set_warn_reply_channel(self.guild_id, str(ch.id))
            await interaction.response.edit_message(
                content=f"✅ Reply channel → {ch.mention}",
                view=None,
            )

        sel.callback = on_pick
        self.add_item(sel)

def build_warn_dm_view(
    guild_name: str,
    warner: discord.abc.User,
    warn_msg: str,
) -> ui.LayoutView:
    quoted = "\n".join(
        f"> {line}" if line else ">"
        for line in (warn_msg or "").splitlines()
    ) or "> (no message)"
    view = ui.LayoutView(timeout=None)
    view.add_item(
        ui.Container(
            ui.TextDisplay("<:warn_yellow:1549249358006976613> **Warn**"),
            ui.Separator(),
            ui.TextDisplay(
                f"You have received a warn from `{guild_name}` by {warner.mention}."
            ),
            ui.TextDisplay("Warn message:"),
            ui.TextDisplay(quoted),
            ui.Separator(),
            ui.TextDisplay("To reply this run, just reply this message directly."),
            accent_color=0xFFFD00,
        )
    )
    return view


def build_warn_reply_channel_view(
    warner_id: int,
    receiver: discord.abc.User,
    reply_text: str,
) -> ui.LayoutView:
    quoted = "\n".join(
        f"> {line}" if line else ">"
        for line in (reply_text or "").splitlines()
    ) or "> (empty)"
    view = ui.LayoutView(timeout=None)
    view.add_item(
        ui.Container(
            ui.TextDisplay("<:warn_yellow:1549249358006976613> **Warn Reply**"),
            ui.Separator(),
            ui.TextDisplay(
                f"<@{warner_id}>, {receiver.mention} replied your warn."
            ),
            ui.TextDisplay("Reply message:"),
            ui.TextDisplay(quoted),
            ui.Separator(),
            ui.TextDisplay("To reply this message, run `/warn` again."),
            accent_color=0xFFFD00,
        )
    )
    return view


class WarnModal(ui.Modal, title="Send a Warning"):
    reason = ui.TextInput(label="Warning message", style=discord.TextStyle.long, required=True, max_length=1000)

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        channel_embed = discord.Embed(
            title="⚠️ Warn",
            color=0xff8500,
            description=(
                f"`{self.target.name}` got warned by `{interaction.user.name}`\n"
                f"Warn message:\n```\n{self.reason.value}\n```"
            )
        )
        channel_embed.set_footer(text=f"{interaction.guild.name}｜67")
        channel_embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.send_message(embed=channel_embed)

        warn_msg = self.reason.value
        invoker = interaction.user
        target = self.target
        guild = interaction.guild

        dm_id = None
        try:
            dm_msg = await target.send(
                view=build_warn_dm_view(guild.name, invoker, warn_msg)
            )
            dm_id = str(dm_msg.id)
        except discord.Forbidden:
            pass

        # Dashboard off 或警告自己 → 不記錄、不算次數
        cfg = ensure_warn_settings(guild)
        if cfg["dashboard_enabled"] and target.id != invoker.id:
            insert_warn(guild.id, target.id, invoker.id, warn_msg, dm_message_id=dm_id)


@bot.tree.command(name="warn", description="Warn a member (and sent via DM)")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(user="The member to warn")
async def warn(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(WarnModal(user))


@warn.error
async def warn_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Bro don't have the permission to do that.", ephemeral=True)


# =================================================================
# 🔥 Streaks 系統 UI
# =================================================================
class StreaksNumberModal(ui.Modal, title="Set Daily Streaks Message Count"):
    value = ui.TextInput(label="Messages needed per day (1-999)", required=True, max_length=3)
    def __init__(self, view: 'StreaksMainView'):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        if not self.value.value.isdigit() or int(self.value.value) < 1:
            return await interaction.response.send_message("❌ Please enter a valid positive number.", ephemeral=True)
        val = int(self.value.value)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO streaks_settings (guild_id, messages_needed) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET messages_needed = excluded.messages_needed",
            (str(self.view.guild_id), val)
        )
        conn.commit(); conn.close()
        embed = self.view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.view)


class StreaksNicknameModal(ui.Modal, title="Set Nickname Emoji Condition"):
    threshold = ui.TextInput(label="Show emoji if streak days >=", required=True, max_length=3)
    emoji = ui.TextInput(label="Emoji to prepend (e.g. 🔥)", required=True, max_length=10)

    def __init__(self, view: 'StreaksMainView'):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        if not self.threshold.value.isdigit() or int(self.threshold.value) < 1:
            return await interaction.response.send_message("❌ Please enter a valid positive number for the threshold.", ephemeral=True)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO streaks_settings (guild_id, nick_threshold, nick_emoji) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET nick_threshold = excluded.nick_threshold, nick_emoji = excluded.nick_emoji",
            (str(self.view.guild_id), int(self.threshold.value), self.emoji.value)
        )
        conn.commit(); conn.close()
        embed = self.view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.view)


class StreaksChannelSelectView(ui.View):
    def __init__(self, guild_id: int, parent_view: 'StreaksMainView'):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.parent_view = parent_view
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT notify_channel_id FROM streaks_settings WHERE guild_id = ?", (str(guild_id),))
        row = cursor.fetchone(); conn.close()
        if row and row[0]:
            self.select_channel.default_values = [discord.Object(id=int(row[0]))]

    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="🔔 Select Notification Channel")
    async def select_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        cid = select.values[0].id
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO streaks_settings (guild_id, notify_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET notify_channel_id = excluded.notify_channel_id",
            (str(self.guild_id), str(cid))
        )
        conn.commit(); conn.close()
        embed = self.parent_view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.gray)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = self.parent_view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class StreaksRoleModal(ui.Modal, title="Set streak requirement for role"):
    streak_count = ui.TextInput(label="Required streak count", required=True, max_length=3)

    def __init__(self, role: discord.Role, view: 'StreaksRoleSettingsView'):
        super().__init__()
        self.role = role
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        if not self.streak_count.value.isdigit():
            return await interaction.response.send_message("❌ Please enter a valid number.", ephemeral=True)
        count = int(self.streak_count.value)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO streaks_roles (guild_id, streak_count, role_id) VALUES (?, ?, ?)", (str(interaction.guild_id), count, str(self.role.id)))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"✅ **{self.role.name}** will be given at a **{count}**-day streak, and removed automatically if the streak breaks.", ephemeral=True)


class StreaksRoleSettingsView(ui.View):
    def __init__(self, parent_view: 'StreaksMainView'):
        super().__init__(timeout=None)
        self.parent_view = parent_view

    @ui.select(cls=ui.RoleSelect, placeholder="Select the role", min_values=1, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: ui.RoleSelect):
        await interaction.response.send_modal(StreaksRoleModal(select.values[0], self))

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.gray)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = self.parent_view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class StreaksMainView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "streaks")
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if not enabled:
            self.remove_item(self.set_channel)
            self.remove_item(self.set_msgneeded)
            self.remove_item(self.set_nickname_condition)
            self.remove_item(self.role_rewards)

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "streaks")
        embed = discord.Embed(title="🔥 Streaks System Settings", color=0xff6600 if enabled else 0x2b2d31)
        if not enabled:
            embed.description = "Status: **off**"
        else:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT messages_needed, notify_channel_id, nick_threshold, nick_emoji FROM streaks_settings WHERE guild_id = ?", (str(self.guild_id),))
            row = cursor.fetchone(); conn.close()
            msgneeded, channel_id, nick_threshold, nick_emoji = row if row else (20, None, 3, '🔥')
            ch_text = f"<#{channel_id}>" if channel_id else "Not set"
            embed.description = (
                f"Status: **on**\n"
                f"Daily streaks message: **{msgneeded}**\n"
                f"Show emoji `{nick_emoji}` if streaks more than **{nick_threshold}** days\n"
                f"Notification channel: {ch_text}"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.gray, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\nAuto Mute\nTime Message\nCustomize profile")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(self.guild_id, "streaks")
        set_feature_enabled(self.guild_id, "streaks", not cur)
        new_view = StreaksMainView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="📢 Notification Channel", style=discord.ButtonStyle.blurple, row=1)
    async def set_channel(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="🔔 Select Notification Channel", color=0x2b2d31)
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=StreaksChannelSelectView(self.guild_id, self))

    @ui.button(label="✏️ Daily Message Count", style=discord.ButtonStyle.blurple, row=1)
    async def set_msgneeded(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(StreaksNumberModal(self))

    @ui.button(label="😀 Nickname Emoji Condition", style=discord.ButtonStyle.blurple, row=1)
    async def set_nickname_condition(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(StreaksNicknameModal(self))

    @ui.button(label="🎭 Role Rewards", style=discord.ButtonStyle.secondary, row=2)
    async def role_rewards(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="🎭 Streaks Role Rewards", 
            description="Choose a **role** below and then set the required **streak count**.\n⚠️ The role will be **automatically removed** if the streak breaks.", 
            color=0x2b2d31
        )
        await interaction.response.edit_message(embed=embed, view=StreaksRoleSettingsView(self))

class CountingMuteDurationModal(ui.Modal, title="Set Mute Duration for Wrong Number"):
    duration = ui.TextInput(label="Duration, or 0/none to disable muting", default="10m", required=True)

    def __init__(self, view: 'CountingConfigView'):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        _, err = parse_mute_duration(self.duration.value)
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)

        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO counting_settings (guild_id, mute_duration) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET mute_duration = excluded.mute_duration",
            (str(interaction.guild_id), self.duration.value)
        )
        conn.commit(); conn.close()
        new_view = CountingConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)


class CountingConfigView(ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "counting") if guild_id else False
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if not enabled:
            self.remove_item(self.set_channel)
            self.remove_item(self.set_mute_duration)
            self.remove_item(self.reset_count)
        elif guild_id:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id FROM counting_settings WHERE guild_id = ?", (str(guild_id),))
            row = cursor.fetchone(); conn.close()
            if row and row[0] and str(row[0]).isdigit():
                self.set_channel.default_values = [discord.Object(id=int(row[0]))]

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "counting")
        embed = discord.Embed(title="🔢 Counting Channel Settings", color=0x3498db if enabled else 0x2b2d31)
        if not enabled:
            embed.description = "Status: **`off`**"
        else:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id, current_count, mute_duration FROM counting_settings WHERE guild_id = ?", (str(self.guild_id),))
            row = cursor.fetchone(); conn.close()
            cid, count, dur = row if row else (None, 0, "10m")
            ch_text = f"<#{cid}>" if cid else "Not set"
            dur_text = "Disabled (no mute)" if (dur or "").lower() in ("0", "none", "off") else dur
            embed.description = (
                f"Status: **`on`**\n"
                f"Counting channel: {ch_text}\n"
                f"Current count: **`{count}`**\n"
                f"Mute duration if broken: **`{dur_text}`**"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.gray, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\nAuto Mute\nTime Message\nCustomize profile")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(self.guild_id, "counting")
        set_feature_enabled(self.guild_id, "counting", not cur)
        new_view = CountingConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="🎯 Select Counting Channel", row=1)
    async def set_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        cid = select.values[0].id
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO counting_settings (guild_id, channel_id, current_count, last_user_id) VALUES (?, ?, 0, NULL) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, current_count = 0, last_user_id = NULL",
            (str(interaction.guild_id), str(cid))
        )
        conn.commit(); conn.close()
        new_view = CountingConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="⏱️ Mute Duration", style=discord.ButtonStyle.blurple, row=2)
    async def set_mute_duration(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CountingMuteDurationModal(self))

    @ui.button(label="🔄 Reset Count", style=discord.ButtonStyle.secondary, row=2)
    async def reset_count(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("UPDATE counting_settings SET current_count = 0, last_user_id = NULL WHERE guild_id = ?", (str(interaction.guild_id),))
        conn.commit(); conn.close()
        new_view = CountingConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

class ProfileEditModal(ui.Modal, title="67+profile"):
    name_field = ui.Label(
        text="Nickname",
        description="The nickname of me.",
        component=ui.TextInput(
            style=discord.TextStyle.short,
            placeholder="Nickname",
            max_length=32,
            required=False,
        ),
    )
    bio_field = ui.Label(
        text="Bio",
        description="Optional.",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Bio",
            max_length=190,
            required=False,
        ),
    )
    avatar_field = ui.Label(
        text="Update Avatar",
        description="Optional, only image file accepted.",
        component=ui.FileUpload(max_values=1, min_values=0, required=False),
    )
    banner_field = ui.Label(
        text="Upload Banner",
        description="Optional, only image file accepted.",
        component=ui.FileUpload(max_values=1, min_values=0, required=False),
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        prev_nick, prev_bio = get_guild_bot_profile(guild_id)
        self.name_field.component.default = prev_nick or ""
        self.bio_field.component.default = prev_bio or ""

    async def on_submit(self, interaction: discord.Interaction):
        if not is_paid_guild(interaction.guild_id, "67profile"):
            return await interaction.response.send_message(
                "Seems u haven't buy 67+profile", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)

        nick = (self.name_field.component.value or "").strip() or None
        bio = (self.bio_field.component.value or "").strip() or None
        try:
            me = interaction.guild.me
            if me is not None:
                try:
                    await me.edit(nick=nick)
                except discord.Forbidden:
                    pass
            await _patch_guild_profile(interaction.guild, nick=nick, bio=bio)
            save_guild_bot_profile(interaction.guild_id, nick, bio)
        except Exception as e:
            return await interaction.followup.send(f"❌ Name/Bio: {e}", ephemeral=True)

        for field_name, label_comp in (
            ("avatar", self.avatar_field),
            ("banner", self.banner_field),
        ):
            files = getattr(label_comp.component, "values", None) or []
            if not files:
                continue
            att = files[0]
            try:
                data = await att.read()
            except Exception:
                await interaction.followup.send(f"❌ Cannot read {field_name}.", ephemeral=True)
                continue
            err = _validate_profile_image(data, att.filename or "", att.content_type)
            if err:
                await interaction.followup.send(f"❌ {field_name}: {err}", ephemeral=True)
                continue
            try:
                await _patch_guild_profile(
                    interaction.guild,
                    **{field_name: (data, _image_ext(att.filename or "x.png"))},
                )
            except Exception as e:
                await interaction.followup.send(f"❌ {field_name}: {e}", ephemeral=True)

        await interaction.followup.send("✅ Profile updated.", ephemeral=True)


class ProfileSettingsView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        unlocked = is_paid_guild(guild.id, "67profile")
        embed = discord.Embed(
            title="👮‍♀️ Customize Profile",
            color=0x5865F2 if unlocked else 0x2b2d31,
        )
        if not unlocked:
            embed.description = (
                "Status: **locked**\n"
                "Owner: `/addpaidserver guild_id:... feature:67profile`"
            )
        else:
            nick, bio = get_guild_bot_profile(guild.id)
            me = guild.me
            shown = nick or (me.display_name if me else "—")
            embed.description = (
                f"Status: **unlocked**\n\n"
                f"**Nickname:** {shown}\n"
                f"**Bio:** {bio or '???????'}\n\n"
                f"Press **✏️ Edit** to customize the bot profile."
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="Settings",
            color=0xdfe600,
            description=(
                "Welcome/Goodbye Panel\nLevel System\nStreaks\nCounting\n"
                "Auto Mute\nTime Message\nCustomize profile"
            ),
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(
            embed=embed, view=SettingsView(interaction.guild_id)
        )

    @ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        if not is_paid_guild(interaction.guild_id, "67profile"):
            return await interaction.response.send_message(
                "Seems u haven't buy 67+profile", ephemeral=True
            )
        await interaction.response.send_modal(ProfileEditModal(interaction.guild_id))

# =================================================================
# Settings hub (Container categories + select)
# =================================================================

class SettingsSelect(ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label="Welcome/Goodbye Panel",
                value="welcome",
                emoji="<:shake_hand_grey:1547899561018658816>",
            ),
            discord.SelectOption(
                label="Customize Profile",
                value="profile",
                emoji="<:profile_grey:1547899599455264848>",
            ),
            discord.SelectOption(
                label="Level System",
                value="level",
                emoji="<:level_grey:1547899589498249306>",
            ),
            discord.SelectOption(
                label="Streaks System",
                value="streaks",
                emoji="<:streaks_grey:1547899591540744242>",
            ),
            discord.SelectOption(
                label="Counting",
                value="counting",
                emoji="<:number_grey:1547899597165301850>",
            ),
            discord.SelectOption(
                label="67+AI",
                value="ai",
                emoji="<:bot_grey:1547899602848448633>",
            ),
            discord.SelectOption(
                label="Auto Reply (toggle)",
                value="autoreply",
                emoji="<:reply_grey:1547901390473400360>",
                description="Select once to turn ON/OFF",
            ),
            discord.SelectOption(
                label="Auto Mute",
                value="automute",
                emoji="<:mute_grey:1547899593167994990>",
            ),
            discord.SelectOption(
                label="Auto Reaction",
                value="autoreact",
                emoji="<:emoji_grey:1547899600914874379>",
            ),
            discord.SelectOption(
                label="Time Message",
                value="timemsg",
                emoji="<:time_message_grey:1547899595390984212>",
            ),
            discord.SelectOption(
                label="Warn",
                value="warn",
                emoji="<:warn_grey:1547901810415505419>",
            ),
        ]
        super().__init__(
            placeholder="Choose a setting for more information",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        gid = self.guild_id

        try:
            # V2 主畫面不能 edit 成 embed → 舊設定頁改 send_message
            if key == "welcome":
                view = WelcomeConfigView(gid)
                return await interaction.response.send_message(
                    embed=view.build_embed(interaction.guild),
                    view=view,
                    ephemeral=True,
                )

            if key == "profile":
                view = ProfileSettingsView(gid)
                embed = (
                    view.build_embed(interaction.guild)
                    if hasattr(view, "build_embed")
                    else None
                )
                return await interaction.response.send_message(
                    embed=embed,
                    view=view,
                    ephemeral=True,
                )

            if key == "level":
                view = LevelSettingsView(gid)
                return await interaction.response.send_message(
                    embed=view.build_embed(interaction.guild),
                    view=view,
                    ephemeral=True,
                )

            if key == "streaks":
                view = StreaksMainView(gid)
                return await interaction.response.send_message(
                    embed=view.build_embed(interaction.guild),
                    view=view,
                    ephemeral=True,
                )

            if key == "counting":
                view = CountingConfigView(gid)
                return await interaction.response.send_message(
                    embed=view.build_embed(interaction.guild),
                    view=view,
                    ephemeral=True,
                )

            if key == "ai":
                view = AIConfigView(gid)
                embed = (
                    view.build_embed(interaction.guild)
                    if hasattr(view, "build_embed")
                    else None
                )
                return await interaction.response.send_message(
                    embed=embed,
                    view=view,
                    ephemeral=True,
                )

            if key == "autoreply":
                cur = is_autoreply_enabled(gid)
                set_feature_enabled(gid, "autoreply67", not cur)
                state = "ON" if not cur else "OFF"
                await interaction.response.edit_message(
                    view=SettingsLayoutView(gid),
                )
                return await interaction.followup.send(
                    f"✅ Auto Reply is now **{state}**.",
                    ephemeral=True,
                )

            if key == "automute":
                view = AutoMuteConfigView(gid)
                return await interaction.response.send_message(
                    embed=view.build_embed(interaction.guild),
                    view=view,
                    ephemeral=True,
                )

            if key == "autoreact":
                return await interaction.response.edit_message(
                    view=AutoReactionLayoutView(gid),
                )

            if key == "timemsg":
                view = TimeMessageConfigView(gid)
                return await interaction.response.send_message(
                    embed=view.build_embed(interaction.guild),
                    view=view,
                    ephemeral=True,
                )

            if key == "warn":
                # 與主畫面同為 V2 LayoutView → 用 edit_message，不要帶 embed
                return await interaction.response.edit_message(
                    view=WarnSettingsHomeView(interaction.guild)
                )

            await interaction.response.send_message(
                "❌ Unknown setting.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"[SettingsSelect] {key}: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {e}", ephemeral=True)


class SettingsLayoutView(ui.LayoutView):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        ar = "on" if is_autoreply_enabled(guild_id) else "off"

        body_server = (
            "**Server**\n"
            "- <:shake_hand_grey:1547899561018658816> Welcome/Goodbye Panel\n"
            "- <:profile_grey:1547899599455264848> Customize Profile"
        )
        body_activity = (
            "**Activity**\n"
            "- <:level_grey:1547899589498249306> Level System\n"
            "- <:streaks_grey:1547899591540744242> Streaks System"
        )
        body_ent = (
            "**Entertainment**\n"
            "- <:number_grey:1547899597165301850> Counting\n"
            "- <:bot_grey:1547899602848448633> 67+AI\n"
            f"- <:reply_grey:1547901390473400360> Auto Reply (`{ar}` — select to toggle)"
        )
        body_mod = (
            "**Moderation**\n"
            "- <:mute_grey:1547899593167994990> Auto Mute\n"
            "- <:emoji_grey:1547899600914874379> Auto Reaction\n"
            "- <:time_message_grey:1547899595390984212> Time Message\n"
            "- <:warn_grey:1547901810415505419> Warn"
        )

        self.add_item(
            ui.Container(
                ui.TextDisplay("# 67 Settings"),
                ui.TextDisplay(body_server),
                ui.TextDisplay(body_activity),
                ui.TextDisplay(body_ent),
                ui.TextDisplay(body_mod),
                ui.ActionRow(SettingsSelect(guild_id)),
                accent_color=0x2B2D31,
            )
        )


@bot.tree.command(name="settings", description="Open bot configuration hub")
@app_commands.checks.has_permissions(manage_guild=True)
async def settings(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "❌ Settings can only be used in a server.",
        )
    await interaction.response.send_message(
        view=SettingsLayoutView(interaction.guild.id),
    )


@settings.error
async def settings_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Manage Server** permission to open Settings."
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)  # public, not ephemeral
        return
    logger.error(f"[/settings error]: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message(f"❌ Something went wrong: {error}")

class SettingsView(ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        if guild_id:
            enabled = is_autoreply_enabled(guild_id)
            self.btn_autoreply.label = "✅ Auto Reply: On" if enabled else "❌ Auto Reply: Off"
            self.btn_autoreply.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary

    @ui.button(label="Welcome/Goodbye Panel", style=discord.ButtonStyle.secondary, emoji="👋")
    async def btn_w(self, interaction: discord.Interaction, btn: ui.Button):
        view = WelcomeConfigView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
    @ui.button(label="Level System", style=discord.ButtonStyle.secondary, emoji="🎉")
    async def btn_l(self, interaction: discord.Interaction, btn: ui.Button):
        view = LevelSettingsView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="Streaks", style=discord.ButtonStyle.secondary, emoji="🔥")
    async def btn_s(self, interaction: discord.Interaction, btn: ui.Button):
        view = StreaksMainView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
    @ui.button(label="Auto Mute", style=discord.ButtonStyle.secondary, emoji="🔒")
    async def btn_a(self, interaction: discord.Interaction, btn: ui.Button):
        try:
            view = AutoMuteConfigView(interaction.guild_id)
            embed = view.build_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception as e:
            logger.error(f"[AutoMute open error]: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        
    @ui.button(label="Time Message", style=discord.ButtonStyle.secondary, emoji="⏰")
    async def btn_t(self, interaction: discord.Interaction, btn: ui.Button):
        view = TimeMessageConfigView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="Counting", style=discord.ButtonStyle.secondary, emoji="🔢")
    async def btn_c(self, interaction: discord.Interaction, btn: ui.Button):
        view = CountingConfigView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
    @ui.button(label="Customize profile", style=discord.ButtonStyle.secondary, emoji="👮‍♀️", row=2)
    async def btn_profile(self, interaction: discord.Interaction, btn: ui.Button):
        view = ProfileSettingsView(interaction.guild_id)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.guild), view=view
        )

    @ui.button(label="67+AI", style=discord.ButtonStyle.secondary, emoji="🤖", row=2)
    async def btn_ai(self, interaction: discord.Interaction, btn: ui.Button):
        view = AIConfigView(interaction.guild_id)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.guild), view=view
        )

    @ui.button(label="🤗 Auto Reaction", style=discord.ButtonStyle.secondary, row=2)
    async def btn_autoreact(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=None,  # Container 模式可不使用 embed
            view=AutoReactionLayoutView(interaction.guild_id),
        )
    
    @ui.button(label="✅ Auto Reply: On", style=discord.ButtonStyle.success, emoji="🔁")
    async def btn_autoreply(self, interaction: discord.Interaction, btn: ui.Button):
        currently_on = is_autoreply_enabled(interaction.guild_id)

        if currently_on:
            # 🎯 要關閉之前，先檢查這個伺服器有沒有在付費白名單裡
            if not is_paid_guild(interaction.guild_id, "67silent"):
                return await interaction.response.send_message("Seems u haven't buy 67+Silent", ephemeral=True)
            set_feature_enabled(interaction.guild_id, "autoreply67", False)
        else:
            set_feature_enabled(interaction.guild_id, "autoreply67", True)

        new_state = is_autoreply_enabled(interaction.guild_id)
        btn.label = "✅ Auto Reply: On" if new_state else "❌ Auto Reply: Off"
        btn.style = discord.ButtonStyle.success if new_state else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

def is_ai_enabled(guild_id) -> bool:
    """67+AI 總開關，預設開啟。"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT enabled FROM feature_toggles WHERE guild_id = ? AND feature = ?",
        (str(guild_id), "ai67"),
    )
    row = cursor.fetchone()
    conn.close()
    return (row[0] == 1) if row else True  # 預設 On
    
    
def get_ai_channel_mode(guild_id) -> tuple[bool, str | None]:
    """回傳 (only_selected, channel_id)。"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT only_selected, channel_id FROM ai_settings WHERE guild_id = ?",
        (str(guild_id),),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False, None
    return bool(row[0]), row[1]
    
    
def set_ai_only_selected(guild_id, enabled: bool):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ai_settings (guild_id, only_selected) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET only_selected = excluded.only_selected",
        (str(guild_id), 1 if enabled else 0),
    )
    conn.commit()
    conn.close()


def set_ai_channel(guild_id, channel_id: str | None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ai_settings (guild_id, channel_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id",
        (str(guild_id), channel_id),
    )
    conn.commit()
    conn.close()

# =================================================================
# 🚀 6. SLASH COMMANDS
# =================================================================

@bot.tree.command(name="help", description="Show help menu")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Help Menu", color=discord.Color.gold())
    embed.add_field(name="Admin Commands", value="`/settings`, `/mute`, `/unmute`, `/kick`, `/setlevel`, `/manualmsg`")
    embed.add_field(name="User Commands", value="`/level`, `/random67`, `/help`")
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "67")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="manualmsg", description="Send a message with bot (Moderators only, and u can add a 67+AI Watermark.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.rename(fake_ai="fake-ai")  # 👈 讓參數在 Discord 面板上顯示為 fake-ai
@app_commands.describe(fake_ai="Add 67+AI Watermark? (True=On / False=Off)")
async def manualmsg(interaction: discord.Interaction, fake_ai: bool = False):
    # 呼叫上方設計好的新版 Modal，並把 fake_ai 參數帶進去
    await interaction.response.send_modal(ManualMsgModal(fake_ai=fake_ai))
    
async def resolve_ban_target(interaction: discord.Interaction, user_input: str):
    """
    解析 /ban 的 user 輸入，支援兩種格式：
    1. @提及 或 從清單挑選（Discord 會自動轉成 <@id> 文字）
    2. 純數字 Discord ID（用來 ban 已經不在伺服器的人）
    回傳 (target, is_member)；解析失敗回傳 (None, None)
    """
    raw = user_input.strip()
    match = re.match(r"^<@!?(\d+)>$", raw)
    uid_str = match.group(1) if match else (raw if raw.isdigit() else None)
    if uid_str is None:
        return None, None
    uid = int(uid_str)
    member = interaction.guild.get_member(uid)
    if member:
        return member, True
    try:
        user_obj = await bot.fetch_user(uid)
        return user_obj, False
    except (discord.NotFound, discord.HTTPException):
        return None, None
        
# 🛠️ 修正點：對應圖 2 之 Mute 嵌入卡片（綠色邊框 + 變數渲染）
@bot.tree.command(name="mute", description="Timeout a server member")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, time: str, reason: Optional[str] = "None"):
    delta, err = parse_mute_duration(time)
    if err: return await interaction.response.send_message(err, ephemeral=True)
    if user.id == interaction.guild.owner_id: return await interaction.response.send_message("❌ Bro don't do that. I don't wnat to be fired.", ephemeral=True)
    if user.id == bot.user.id: return await interaction.response.send_message("❌ Are u kidding? Call me to mute myself?", ephemeral=True)

    # 🎯 前置身分組階級檢查：對方比機器人高就直接跳 error，不要打去給 Discord 拒絕
    bot_member = interaction.guild.me
    if user.top_role >= bot_member.top_role:
        return await interaction.response.send_message(f"❌ I can't mute **{user.display_name}**, their role is higher than or equal to mine.", ephemeral=True)

    try:
        await user.timeout(delta, reason=reason)
        embed = discord.Embed(
            title=parse_placeholders("✅ {user.name} has been muted.", user, interaction.guild), 
            color=0x2ecc71, 
            description=f"Time: {time}\nReason: {reason}"
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "67")
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Call any moderator to give me a higher privileges.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sorry, something ({e}) went wrong. Try again later.", ephemeral=True)

# 🛠️ 修正點：對應圖 2 之 Unmute 嵌入卡片（綠色邊框 + 變數渲染）
@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    await user.timeout(None)
    embed = discord.Embed(
        title=parse_placeholders("✅ {user.name} has been unmuted.", user, interaction.guild), 
        color=0x2ecc71
    )
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="kick", description="Kick a member from server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = "None"):
    if user.id == interaction.guild.owner_id: return await interaction.response.send_message("❌ Bro don't do that. I don't wnat to be fired.", ephemeral=True)
    if user.id == bot.user.id: return await interaction.response.send_message("❌ Are u kidding? Call me to kick myself?", ephemeral=True)

    # 🎯 前置身分組階級檢查：對方比機器人高就直接跳 error
    bot_member = interaction.guild.me
    if user.top_role >= bot_member.top_role:
        return await interaction.response.send_message(f"❌ Bro don't do that. I don't wnat to be fired.", ephemeral=True)

    try:
        await user.kick(reason=reason)
        await send_goodbye_message(user, interaction.guild)  # 🎯 主動發送，不等 Gateway 事件（可能因快取問題漏掉）
        embed = discord.Embed(title=parse_placeholders("✅ {user.name} has been kicked.", user, interaction.guild), color=0xe74c3c, description=parse_placeholders("Reason: {reason}", user, interaction.guild, extra={"reason": reason}))
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Call any moderator to give me a higher privileges.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sorry, something went wrong. Try again later.", ephemeral=True)

@kick.error
async def kick_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions): await interaction.response.send_message("❌ Bro don't have the kick permission. ", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member from server")
@app_commands.describe(user="Choose a user or enter the user's ID（that means u can ban a user that's not in the server）")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: str, reason: Optional[str] = "None"):
    target, is_member = await resolve_ban_target(interaction, user)
    if target is None:
        return await interaction.response.send_message("❌ Invalid user. Tag a user or enter the correct user ID (It's a long numbers, for example 1145141919810....)", ephemeral=True)

    if target.id == interaction.guild.owner_id: 
        return await interaction.response.send_message("❌ Bro don't do that. I don't wnat to be fired.", ephemeral=True)
    if target.id == bot.user.id: 
        return await interaction.response.send_message("❌ Are u kidding? Call me to ban myself?", ephemeral=True)

    # 🎯 前置身分組階級檢查：只有對方還在群內時才有身分組可比較
    if is_member:
        bot_member = interaction.guild.me
        if target.top_role >= bot_member.top_role:
            return await interaction.response.send_message(f"❌ Bro don't do that. I don't wnat to be fired.", ephemeral=True)

    try:
        await interaction.guild.ban(target, reason=reason)
        if is_member:
            # 🎯 主動發送，不等 Gateway 事件；is_member 才發是因為本來就不在群內的人沒有「離開」可言
            await send_goodbye_message(target, interaction.guild)
        embed = discord.Embed(
            title=parse_placeholders("✅ {user.name} has been banned.", target, interaction.guild), 
            color=0xe74c3c, 
            description=parse_placeholders("Reason: {reason}", target, interaction.guild, extra={"reason": reason})
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Call any moderator to give me a higher privileges.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sorry, something ({e}) went wrong. Try again later.", ephemeral=True)

@ban.error
async def ban_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions): 
        await interaction.response.send_message("❌ Bro don't have the ban permission.", ephemeral=True)

@bot.tree.command(name="unban", description="Unban a user from server")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str, reason: Optional[str] = "None"):
    try:
        target = await bot.fetch_user(int(user_id))
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)

    try:
        await interaction.guild.unban(target, reason=reason)
        embed = discord.Embed(
            title=parse_placeholders("✅ {user.name} has been unbanned.", target, interaction.guild), 
            color=0x2ecc71, 
            description=parse_placeholders("Reason: {reason}", target, interaction.guild, extra={"reason": reason})
        )
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.send_message(embed=embed)
    except discord.NotFound:
        await interaction.response.send_message("❌ This user isn't banned.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Call any moderator to give me a higher privileges.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sorry, something ({e}) went wrong. Try again later.", ephemeral=True)

@unban.error
async def unban_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions): 
        await interaction.response.send_message("❌ Bro don't have the ban permission.", ephemeral=True)

@bot.tree.command(name="setlevel", description="Manually set a member's level")
@app_commands.checks.has_permissions(administrator=True)
async def setlevel(interaction: discord.Interaction, user: discord.Member, level: int):
    if level < 1: return await interaction.response.send_message("❌ Ur math suck. Don't set level under 1.", ephemeral=True)
    
    gid = str(interaction.guild_id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT count_67 FROM levels WHERE guild_id = ? AND user_id = ?", (gid, str(user.id)))
    row = cursor.fetchone()
    current_67 = row[0] if row else 0
    
    # 變更等級，並將目前 XP 歸零重算
    cursor.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level, count_67) VALUES (?, ?, ?, ?, ?)", (gid, str(user.id), 0, level, current_67))
    conn.commit()
    
    # 回應操作的管理員（僅限管理員看見）
    await interaction.response.send_message(f"✅ Set {user.name}'s level to Lv. {level}.", ephemeral=True)
    # 🎯 新增：手動調等後，自動補上對應等級的身分組獎勵
    # 灌到目標等：補齊所有 <= 該等的獎勵身分組
    await check_level_roles(user, level, from_level=None)
    
    # 🎯 核心修正：直接抓取與一般升等完全相同的頻道與訊息設定
    cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (str(interaction.guild.id),))
    lrow = cursor.fetchone()
    conn.close()
    
    if lrow and lrow[0]:
        try:
            chan = bot.get_channel(int(lrow[0])) or await bot.fetch_channel(int(lrow[0]))
            if chan:
                # 完全沿用一般升等的解析與發送邏輯，渲染出你自訂的升等訊息
                txt = parse_placeholders(lrow[1], user, interaction.guild, extra={"level": level})
                await chan.send(txt)
        except Exception as e:
            logger.error(f"[setlevel 發送升等訊息出錯]: {e}")



# 🛠️ 修正點：完全遵循圖 6 藍圖重製的 /level 面板，無任何自創欄位或隱藏修改
class LevelBoardView(ui.View):
    def __init__(self, target: discord.User, guild: discord.Guild):
        super().__init__(timeout=None)
        self.target = target
        self.guild = guild
        self.mode = "personal"

    @ui.button(label="Leaderboard / Personal 🔄", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction: discord.Interaction, button: ui.Button):
        gid = str(self.guild.id)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()

        if self.mode == "personal":
            self.mode = "leaderboard"
            cursor.execute("SELECT user_id, level, xp FROM levels WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 10", (gid,))
            rows = cursor.fetchall(); conn.close()

            desc = ""
            for idx, (uid, lvl, xp) in enumerate(rows, 1):
                user = bot.get_user(int(uid))
                name = user.name if user else f"User {uid}"
                desc += f"{idx}. **{name}**: Lv.{lvl} ({xp} XP)\n"

            embed = discord.Embed(title=f"🏆 {self.guild.name} Level Leaderboard", description=desc or "No data available.", color=0x9b59b6)
        else:
            self.mode = "personal"
            cursor.execute("SELECT xp, level, count_67 FROM levels WHERE guild_id = ? AND user_id = ?", (gid, str(self.target.id)))
            row = cursor.fetchone()

            xp, lvl, count_67 = row if row else (0, 1, 0)
            is_admin = self.target.guild_permissions.administrator if isinstance(self.target, discord.Member) else False
            admin_xp = get_admin_xp_per_level(self.guild.id)
            xp_needed = get_xp_needed(lvl, is_admin, admin_xp)

            cursor.execute("SELECT COUNT(*) FROM levels WHERE guild_id = ? AND (level > ? OR (level = ? AND xp > ?))", (gid, lvl, lvl, xp))
            level_rank = cursor.fetchone()[0] + 1

            cursor.execute("SELECT COUNT(*) FROM levels WHERE guild_id = ? AND count_67 > ?", (gid, count_67))
            count_67_rank = cursor.fetchone()[0] + 1
            conn.close()

            embed = discord.Embed(
                title=f"{self.target.name}'s Level",
                color=0x9b59b6,
                description=(
                    "**Level**\n"
                    f"{lvl}\n"
                    "**XP**\n"
                    f"{xp}/{xp_needed}\n"
                    "**67 Times**\n"
                    f"{count_67}\n\n"
                    "**Level Rank**\n"
                    f"#{level_rank}\n"
                    "**User 67 Rank**\n"
                    f"#{count_67_rank}"
                )
            )

        embed.set_footer(text=f"{self.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="level", description="Check current activity stats and 67 counts")
async def level(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    gid = str(interaction.guild_id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT xp, level, count_67 FROM levels WHERE guild_id = ? AND user_id = ?", (gid, str(target.id)))
    row = cursor.fetchone()
    
    xp, lvl, count_67 = row if row else (0, 1, 0)
    is_admin = target.guild_permissions.administrator if isinstance(target, discord.Member) else False
    admin_xp = get_admin_xp_per_level(interaction.guild_id)
    xp_needed = get_xp_needed(lvl, is_admin, admin_xp)
    
    # 📈 高效率同伺服器內即時排名計算
    cursor.execute("SELECT COUNT(*) FROM levels WHERE guild_id = ? AND (level > ? OR (level = ? AND xp > ?))", (gid, lvl, lvl, xp))
    level_rank = cursor.fetchone()[0] + 1
    
    cursor.execute("SELECT COUNT(*) FROM levels WHERE guild_id = ? AND count_67 > ?", (gid, count_67))
    count_67_rank = cursor.fetchone()[0] + 1
    conn.close()
    
    # 🖼️ 建立完全一模一樣的文字嵌入塊
    embed = discord.Embed(
        title=f"{target.name}'s Level",
        color=0x9b59b6, # 絕美紫邊框
        description=(
            "**Level**\n"
            f"{lvl}\n"
            "**XP**\n"
            f"{xp}/{xp_needed}\n"
            "**67 Times**\n"
            f"{count_67}\n\n"
            "**Level Rank**\n"
            f"#{level_rank}\n"
            "**User 67 Rank**\n"
            f"#{count_67_rank}"
        )
    )
    
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "67")
    view = LevelBoardView(target, interaction.guild)
    await interaction.response.send_message(embed=embed, view=view)

from typing import Literal, Optional

random67_cooldowns = {}

@bot.tree.command(name="random67", description="Get a 67 message / 獲取67迷因訊息")
@app_commands.describe(language="Select language / 選擇語言")
async def random67(interaction: discord.Interaction, language: Literal["English", "中文"]):
    user = interaction.user
    is_admin = user.guild_permissions.administrator if isinstance(user, discord.Member) else False

    # ⏳ 檢查冷卻時間（管理員不受限制）
    if not is_admin:
        now = time.time()
        last_used = random67_cooldowns.get(user.id, 0)
        cooldown_time = 60  # 冷卻秒數

        if now - last_used < cooldown_time:
            remaining = int(cooldown_time - (now - last_used))
            return await interaction.response.send_message(
                f"⏳ U use this command too frequently. Wait for `{remaining}` seconds.", 
                ephemeral=True
            )
        
        # 紀錄本次使用時間
        random67_cooldowns[user.id] = now

    jokes_en = [
        "67 is magic!",
        "Luck factor: 67%",
        "Spirit of Six Seven!",
        "Keep calm and 67 on.",
        "42 is the answer to life, but 67 is the upgrade.",
        "67: Not all heroes wear capes, some are just numbers.",
        "Error 67: Too much luck detected!",
        "You got blessed by the legendary 67 spirit ✨",
        "67% of all statistics are made up on the spot, including this one.",
        "Why was 6 afraid of 7? Because 7 ate 9... but 67 just chilled.",
        "May the 67 be with you, always.",
        "67 mode activated: 100% chance of randomness!"
    ]
    
    # 👑 洗腦大獎段落
    big_prize_zh = "欸six seven🗣️🗣️🔥🔥🔥\n欸six seven🗣️🗣️🔥🔥🔥\nsix！six！seven🥰🥰\n欸six seven🗣️🗣️🔥🔥🔥\n阿公67↗️\n阿公阿公67↘️↗️\n阿公67↗️\n阿公67↗️\n阿公阿公67↘️↗️\n阿公！！🥰🥰🥰\n67！\n阿公阿公67↘️↗️\nsix seven🗣️🗣️🔥🔥🔥"

    # 👴👵 一般親戚圖鑑
    relatives_zh = [
        "阿公67", "阿嬤67", "外公67", "外婆67",
        "大伯公67", "二伯公67", "三伯公67",
        "大叔公67", "二叔公67", "三叔公67",
        "大姑婆67", "二姑婆67", "三姑婆67",
        "大舅公67", "二舅公67", "三舅公67",
        "大姨婆67", "二姨婆67", "三姨婆67",
        "大嬸婆67", "二嬸婆67", "三嬸婆67",
        "大伯婆67", "二伯婆67", "三伯婆67",
        "大舅婆67", "二舅婆67", "三舅婆67",
        "大姑丈公67", "二姑丈公67", "三姑丈公67",
        "大姨丈公67", "二姨丈公67", "三姨丈公67",
        "大伯67", "二伯67", "三伯67",
        "大叔67", "二叔67", "三叔67",
        "大姑67", "二姑67", "三姑67",
        "大舅67", "二舅67", "三舅67",
        "大姨67", "二姨67", "三姨67",
        "大伯母67", "二伯母67", "三伯母67",
        "大嬸嬸67", "二嬸嬸67", "三嬸嬸67",
        "大舅媽67", "二舅媽67", "三舅媽67",
        "大姑丈67", "二姑丈67", "三姑丈67",
        "大姨丈67", "二姨丈67", "三姨丈67",
        "大表伯67", "二表伯67", "三表伯67",
        "大表叔67", "二表叔67", "三表叔67",
        "大表姑67", "二表姑67", "三表姑67",
        "大表舅67", "二表舅67", "三表舅67",
        "大表姨67", "二表姨67", "三表姨67",
        "大表嬸67", "二表嬸67", "三表嬸67",
        "大表舅媽67", "二表舅媽67", "三表舅媽67"
    ]

    if language == "English":
        selected = random.choice(jokes_en)
    else:
        # 🎯 50% 機率出洗腦大獎，剩下 50% 隨機抽親戚
        if random.random() < 0.5:
            selected = big_prize_zh
        else:
            selected = random.choice(relatives_zh)

    await interaction.response.send_message(selected)

@bot.tree.command(name="setserverstats", description="Create a SERVER STATS category with live member/bot/ban/mute voice channels")
@app_commands.checks.has_permissions(administrator=True)
async def setserverstats(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("❌ Guild only.", ephemeral=True)

    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ I need **Manage Channels** permission.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    # 已存在就先清掉舊紀錄對應的頻道（可選：不刪，直接重建覆蓋）
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT category_id, all_members_id, members_id, bots_id, bans_id, mutes_id FROM server_stats WHERE guild_id = ?", (str(guild.id),))
    old = cursor.fetchone()
    if old:
        for cid in old:
            if not cid:
                continue
            ch = guild.get_channel(int(cid))
            if ch:
                try:
                    await ch.delete(reason="Recreate server stats")
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True),
        me: discord.PermissionOverwrite(connect=True, manage_channels=True, view_channel=True),
    }

    try:
        category = await guild.create_category(
            "📊 SERVER STATS 📊",
            overwrites=overwrites,
            reason="Server stats setup",
        )

        total = guild.member_count or len(guild.members)
        humans = sum(1 for m in guild.members if not m.bot)
        bots = sum(1 for m in guild.members if m.bot)
        now = discord.utils.utcnow()
        mutes = sum(1 for m in guild.members if m.timed_out_until and m.timed_out_until > now)

        bans = 0
        try:
            async for _ in guild.bans(limit=None):
                bans += 1
        except (discord.Forbidden, discord.HTTPException):
            bans = 0

        ch_all = await category.create_voice_channel(f"All Members: {total}", overwrites=overwrites)
        ch_mem = await category.create_voice_channel(f"Members: {humans}", overwrites=overwrites)
        ch_bot = await category.create_voice_channel(f"Bots: {bots}", overwrites=overwrites)
        ch_ban = await category.create_voice_channel(f"Bans: {bans}", overwrites=overwrites)
        ch_mute = await category.create_voice_channel(f"Mutes: {mutes}", overwrites=overwrites)

        cursor.execute(
            """
            INSERT OR REPLACE INTO server_stats
            (guild_id, category_id, all_members_id, members_id, bots_id, bans_id, mutes_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(guild.id),
                str(category.id),
                str(ch_all.id),
                str(ch_mem.id),
                str(ch_bot.id),
                str(ch_ban.id),
                str(ch_mute.id),
            ),
        )
        conn.commit()
        conn.close()

        await interaction.followup.send(
            f"✅ Server stats created under **{category.name}**.\n"
            f"Names update every 5 minutes (only the number is changed; you can rename the prefix).\n"
            f"Deleting any channel is fine — missing channels are skipped silently.",
            ephemeral=True,
        )
    except Exception as e:
        conn.close()
        logger.error(f"[/setserverstats] {e}")
        await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)
        
# =================================================================
# 💰 ECONOMY SYSTEM COMMANDS & VIEWS (對應圖 {696E6907-11CB-4468-B5BB-9C2678E2F7F2}.png)
# =================================================================

class EcoBalanceView(ui.View):
    def __init__(self, target: discord.User, guild: discord.Guild):
        super().__init__(timeout=None)
        self.target = target
        self.guild = guild
        self.mode = "balance"

    @ui.button(label="Leaderboard / Balance 🔄", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction: discord.Interaction, button: ui.Button):
        gid = str(self.guild.id)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        if self.mode == "balance":
            self.mode = "leaderboard"
            cursor.execute("SELECT user_id, balance FROM economy WHERE guild_id = ? ORDER BY balance DESC LIMIT 10", (gid,))
            rows = cursor.fetchall(); conn.close()
            
            desc = ""
            for idx, (uid, bal) in enumerate(rows, 1):
                user = bot.get_user(int(uid))
                name = user.name if user else f"User {uid}"
                desc += f"{idx}. **{name}**: ${bal}\n"
            
            embed = discord.Embed(title=f"🏆 {self.guild.name} Leaderboard", description=desc or "No data available.", color=0xffa500)
        else:
            self.mode = "balance"
            cursor.execute("SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?", (gid, str(self.target.id)))
            row = cursor.fetchone(); conn.close()
            bal = row[0] if row else 0
            
            embed = discord.Embed(
                title=f"{self.target.name}'s balance",
                color=0xffa500,
                description=f"💰 Balance\n**${bal}**"
            )
            
        embed.set_footer(text=f"{self.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=self)

# 🎯 個人安裝（不在真實伺服器情境下）用的獨立經濟備份，跟任何真實伺服器的 economy 資料完全分開
PERSONAL_ECO_GID = "personal"

def resolve_eco_gid(interaction: discord.Interaction) -> str:
    """
    判斷這次互動的經濟資料要記在哪個「桶子」：
    - 如果 bot 真的是這個伺服器的成員（正常在伺服器內使用）→ 用真實 guild_id
    - 如果是私訊、或透過個人安裝在 bot 沒加入的伺服器裡使用 → 全部歸進同一個獨立的 "personal" 桶子
    """
    if interaction.guild is not None and interaction.guild.me is not None:
        return str(interaction.guild_id)
    return PERSONAL_ECO_GID

def ensure_eco_user(guild_id: str, user_id: str):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance, last_daily, last_work, last_pay, last_rob FROM economy WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, 0)", (guild_id, user_id))
        conn.commit()
        row = (0, '', 0, 0, 0)
    conn.close()
    return row

@bot.tree.command(name="ecodaily", description="Claim your daily reward")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ecodaily(interaction: discord.Interaction):
    gid = resolve_eco_gid(interaction)
    uid = str(interaction.user.id)
    ensure_eco_user(gid, uid)
    
    tz = datetime.timezone(datetime.timedelta(hours=0))
    current_day = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT last_daily FROM economy WHERE guild_id = ? AND user_id = ?", (gid, uid))
    last_daily = cursor.fetchone()[0]
    
    if last_daily == current_day:
        conn.close()
        return await interaction.response.send_message("❌ You have already claimed your daily reward today! (Resets at UTC+8 midnight)", ephemeral=True)
        
    cursor.execute("UPDATE economy SET balance = balance + 100, last_daily = ? WHERE guild_id = ? AND user_id = ?", (current_day, gid, uid))
    conn.commit(); conn.close()
    
    embed = discord.Embed(
        title="Daily Reward",
        color=0x00ffff,
        description="You claimed **$100** daily reward !"
    )
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "Personal Wallet｜67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecowork", description="Go to work and earn money")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ecowork(interaction: discord.Interaction):
    gid = resolve_eco_gid(interaction)
    uid = str(interaction.user.id)
    ensure_eco_user(gid, uid)
    now = int(datetime.datetime.now().timestamp())
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT last_work FROM economy WHERE guild_id = ? AND user_id = ?", (gid, uid))
    last_work = cursor.fetchone()[0]
    
    if now - last_work < 3600:
        conn.close()
        rem = 3600 - (now - last_work)
        return await interaction.response.send_message(f"❌ You are exhausted! Please wait {rem // 60}m {rem % 60}s before working again.", ephemeral=True)
        
    success = random.random() > 0.1
    work_success_reasons = [
        "help ur neighbor walked the dog",
        "worked a shift at McDonald's",
        "fixed ur boss's computer by turning it off and on",
        "sold some old anime figures on eBay",
        "helped an old lady cross the street",
        "tutored a kid in math for 2 hours",
        "cleaned up the local park",
        "delivered pizzas all night",
        "found a lost wallet and returned it for a reward",
        "wrote a Discord bot for a friend",
        "mowed ur neighbor's lawn",
        "streamed on Twitch and got some generous donations"
    ]

    work_fail_reasons = [
        "run the red light while delivering the package",
        "dropped and broke a set of expensive coffee cups at work",
        "accidentally ordered 50 cups of boba tea for everyone",
        "got fined for sleeping on the job",
        "spilled coffee all over ur boss's mechanical keyboard",
        "got a parking ticket while delivering food",
        "bought a useless mystery box on the internet",
        "tripped and ruined the giant birthday cake u were carrying",
        "got caught playing games during working hours",
        "accidentally deleted the company's database",
        "broke the soft-serve ice cream machine at work",
        "lost ur wallet while running to catch the bus"
    ]

    if success:
        amount = random.randint(200, 2000)
        # 🎯 修正：原本漏了 guild_id 篩選，會導致同時更新這個使用者在所有伺服器的餘額
        cursor.execute("UPDATE economy SET balance = balance + ?, last_work = ? WHERE guild_id = ? AND user_id = ?", (amount, now, gid, uid))
        reason = random.choice(work_success_reasons)
        embed = discord.Embed(
            title="Work",
            color=0x00ffff,
            description=f"You **{reason}** and earned **${amount}** !"
        )
    else:
        amount = random.randint(50, 100)
        cursor.execute("UPDATE economy SET balance = MAX(0, balance - ?), last_work = ? WHERE guild_id = ? AND user_id = ?", (amount, now, gid, uid))
        reason = random.choice(work_fail_reasons)
        embed = discord.Embed(
            title="Work",
            color=0xff6b6b,
            description=f"You **{reason}** and losted **${amount}** !"
        )
        
    conn.commit(); conn.close()
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "Personal Wallet｜67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecopay", description="Pay money to another user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ecopay(interaction: discord.Interaction, user: discord.Member, value: int):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("❌ You cannot pay money to yourself!", ephemeral=True)
    if value <= 0:
        return await interaction.response.send_message("❌ Payment amount must be positive!", ephemeral=True)
        
    gid = resolve_eco_gid(interaction)
    uid = str(interaction.user.id)
    tid = str(user.id)
    ensure_eco_user(gid, uid)
    ensure_eco_user(gid, tid)
    now = int(datetime.datetime.now().timestamp())
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance, last_pay FROM economy WHERE guild_id = ? AND user_id = ?", (gid, uid))
    bal, last_pay = cursor.fetchone()
    
    if now - last_pay < 3600:
        conn.close()
        rem = 3600 - (now - last_pay)
        return await interaction.response.send_message(f"❌ Bank transfers are throttled. Wait {rem // 60}m {rem % 60}s.", ephemeral=True)
    if bal < value:
        conn.close()
        return await interaction.response.send_message(f"❌ Insufficient funds! You only have ${bal}.", ephemeral=True)
        
    tax = int(value * 0.05)
    net_value = value - tax
    
    cursor.execute("UPDATE economy SET balance = balance - ? , last_pay = ? WHERE guild_id = ? AND user_id = ?", (value, now, gid, uid))
    cursor.execute("UPDATE economy SET balance = balance + ? WHERE guild_id = ? AND user_id = ?", (net_value, gid, tid))
    conn.commit(); conn.close()
    
    embed = discord.Embed(
        title="Pay",
        color=0x00ffff,
        description=f"You successfully paid **{user.mention}** with **${net_value}** !\n(U need to pay 10% tax)"
    )
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "Personal Wallet｜67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecorob", description="Attempt to rob money from another user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ecorob(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("❌ You cannot rob yourself!", ephemeral=True)
        
    gid = resolve_eco_gid(interaction)
    uid = str(interaction.user.id)
    tid = str(user.id)
    ensure_eco_user(gid, uid)
    ensure_eco_user(gid, tid)
    now = int(datetime.datetime.now().timestamp())
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance, last_rob FROM economy WHERE guild_id = ? AND user_id = ?", (gid, uid))
    my_bal, last_rob = cursor.fetchone()
    
    if now - last_rob < 3600:
        conn.close()
        rem = 3600 - (now - last_rob)
        return await interaction.response.send_message(f"❌ You are laying low. Try robbing again in {rem // 60}m {rem % 60}s.", ephemeral=True)
        
    cursor.execute("SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?", (gid, tid))
    target_bal = cursor.fetchone()[0]
    
    if target_bal <= 0:
        conn.close()
        return await interaction.response.send_message("❌ That user is completely broke! Nothing worth stealing.", ephemeral=True)
        
    success = random.random() < (1 / 3)
    rate = random.uniform(0.1, 0.25)
    
    if success:
        amount = int(target_bal * rate)
        cursor.execute("UPDATE economy SET balance = balance + ?, last_rob = ? WHERE guild_id = ? AND user_id = ?", (amount, now, gid, uid))
        cursor.execute("UPDATE economy SET balance = MAX(0, balance - ?) WHERE guild_id = ? AND user_id = ?", (amount, gid, tid))
        embed = discord.Embed(
            title="Rob",
            color=0x00ffff,
            description=f"You successfully rob **${amount}** from **{user.mention}**"
        )
    else:
        amount = int(my_bal * rate) if my_bal > 0 else random.randint(50, 200)
        cursor.execute("UPDATE economy SET balance = MAX(0, balance - ?), last_rob = ? WHERE guild_id = ? AND user_id = ?", (amount, now, gid, uid))
        embed = discord.Embed(
            title="Rob",
            color=0xff6b6b,
            description=f"The police showed up and u losted **${amount}**"
        )
        
    conn.commit(); conn.close()
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "Personal Wallet｜67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecobalance", description="Check account balance or view top rank leaderboard")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ecobalance(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    gid = resolve_eco_gid(interaction)
    uid = str(target.id)
    ensure_eco_user(gid, uid)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?", (gid, uid))
    bal = cursor.fetchone()[0]
    conn.close()
    
    embed = discord.Embed(
        title=f"{target.name}'s balance",
        color=0xffa500,
        description=f"💰 Balance\n**${bal}**"
    )
    embed.set_footer(text=f"{interaction.guild.name}｜67" if interaction.guild else "Personal Wallet｜67")
    
    # 🎯 排行榜按鈕只有「真的在伺服器內」才有意義，個人備份沒有排行榜可比較
    if interaction.guild is not None and interaction.guild.me is not None:
        view = EcoBalanceView(target, interaction.guild)
        await interaction.response.send_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed)
    
@bot.tree.command(name="setbalance", description="Admin command to modify user balance")
@app_commands.checks.has_permissions(administrator=True)
async def setbalance(interaction: discord.Interaction, user: discord.Member, value: int):
    if value < 0:
        return await interaction.response.send_message("❌ Balance cannot be negative!", ephemeral=True)
    gid = str(interaction.guild_id)
    uid = str(user.id)
    ensure_eco_user(gid, uid)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("UPDATE economy SET balance = ? WHERE guild_id = ? AND user_id = ?", (value, gid, uid))
    conn.commit(); conn.close()
    
    await interaction.response.send_message(f"💵 Successfully set {user.name}'s balance to **${value}**.", ephemeral=True)

@bot.tree.command(name="afkvoice", description="Let the bot afk in the selected voice channel.")
@app_commands.describe(channel="Choose the target channel, blank means cancelled.")
@app_commands.checks.has_permissions(administrator=True)
async def afkvoice(interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
    await interaction.response.defer()  # 🎯 先延長回應期限到 15 分鐘，避免語音握手超過 3 秒導致沒反應
    gid = str(interaction.guild_id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()

    if channel is None:
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if vc: await vc.disconnect(force=True)
        stop_voice_keepalive(interaction.guild_id)  # 🎯 取消時順便停掉保活任務
        cursor.execute("DELETE FROM voice_watch WHERE guild_id = ?", (gid,))
        conn.commit(); conn.close()
        return await interaction.followup.send("✅ Cancelled afking.", ephemeral=True)

    # 🎯 先明確檢查 bot 對這個頻道有沒有 Connect 權限，權限不夠直接給明確訊息，不用等連線失敗才知道
    perms = channel.permissions_for(interaction.guild.me)
    if not perms.connect:
        conn.close()
        return await interaction.followup.send(f"❌ I don't have **Connect** permission in {channel.mention}. Please check the channel's permission overwrites for my role.", ephemeral=True)

    existing_vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    active_count = len([vc for vc in bot.voice_clients if vc.is_connected()])

    if not existing_vc and active_count >= MAX_VOICE_WATCH:
        conn.close()
        return await interaction.followup.send(f"❌ Maximum number of channels available for AFK ({MAX_VOICE_WATCH} channels), use `/afkvoice` to cancel some channels and try again later", ephemeral=True)

    try:
        if existing_vc:
            await asyncio.wait_for(existing_vc.move_to(channel), timeout=15)
        else:
            await asyncio.wait_for(channel.connect(self_mute=True, self_deaf=True, timeout=15), timeout=20)
    except asyncio.TimeoutError:
        conn.close()
        stuck_vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if stuck_vc:
            try: await stuck_vc.disconnect(force=True)
            except: pass
        logger.error(f"[/afkvoice] 連線逾時：guild={interaction.guild_id}, channel={channel.id}")
        return await interaction.followup.send("❌ Voice connect timed out (20s). Cleared the stuck connection, please try again. If this keeps happening, screenshot this and send it to the developer.", ephemeral=True)
    except discord.Forbidden as e:
        conn.close()
        logger.error(f"[/afkvoice] Forbidden: {e}")
        return await interaction.followup.send(f"❌ Missing permission to join/speak in this channel: {e}", ephemeral=True)
    except discord.ClientException as e:
        conn.close()
        logger.error(f"[/afkvoice] ClientException: {e}")
        return await interaction.followup.send(f"❌ Voice client error: {e}", ephemeral=True)
    except Exception as e:
        conn.close()
        logger.error(f"[/afkvoice] 未預期錯誤: {type(e).__name__}: {e}")
        return await interaction.followup.send(f"❌ Unexpected error: {type(e).__name__}: {e}", ephemeral=True)

    cursor.execute("INSERT OR REPLACE INTO voice_watch (guild_id, channel_id) VALUES (?, ?)", (gid, str(channel.id)))
    conn.commit(); conn.close()

    # 🎯 連線成功，啟動保活任務避免之後被 Discord 判定閒置斷線
    active_vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if active_vc:
        await start_voice_keepalive(interaction.guild_id, active_vc)

    now = datetime.datetime.now().timestamp()
    for member in channel.members:
        if not member.bot:
            voice_sessions[(gid, str(member.id))] = now

    embed = discord.Embed(
        title="🔇 AFK",
        color=0x2ecc71,
        description=f"Now afking in {channel.mention}."
    )
    embed.set_footer(text=f"{interaction.guild.name}｜67")
    await interaction.followup.send(embed=embed)

@afkvoice.error
async def afkvoice_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Bro don't have the permission to do that.", ephemeral=True)

# 🎯 音檔路徑：跟 main.py 放在同一層目錄，檔名自己換成你實際上傳的檔名
NGGYU_FILE = "nggyu.mp3"

@bot.tree.command(name="nggyu", description="Play Never Gonna Give You Up once in a voice channel")
@app_commands.describe(channel="Voice channel to rickroll")
@app_commands.checks.has_permissions(administrator=True)
async def nggyu(interaction: discord.Interaction, channel: discord.VoiceChannel):
    await interaction.response.defer()

    if not os.path.exists(NGGYU_FILE):
        return await interaction.followup.send(f"❌ 找不到音檔：`{NGGYU_FILE}`，請確認檔案有跟 main.py 放在同一層目錄。", ephemeral=True)

    perms = channel.permissions_for(interaction.guild.me)
    if not perms.connect or not perms.speak:
        return await interaction.followup.send(f"❌ I don't have Connect/Speak permission in {channel.mention}.", ephemeral=True)

    existing_vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    original_channel = None  # 🎯 如果 bot 原本就用 /afkvoice 掛在別的頻道，播完要切回去

    try:
        if existing_vc:
            if existing_vc.channel.id != channel.id:
                original_channel = existing_vc.channel
                stop_voice_keepalive(interaction.guild_id)
                await asyncio.wait_for(existing_vc.move_to(channel), timeout=15)
            vc = existing_vc
        else:
            active_count = len([v for v in bot.voice_clients if v.is_connected()])
            if active_count >= MAX_VOICE_WATCH:
                return await interaction.followup.send(f"❌ 目前已達語音連線上限（{MAX_VOICE_WATCH} 個），請稍後再試。", ephemeral=True)
            vc = await asyncio.wait_for(channel.connect(self_deaf=True, timeout=15), timeout=20)
    except Exception as e:
        return await interaction.followup.send(f"❌ Failed to join voice channel: {type(e).__name__}: {e}", ephemeral=True)

    if vc.is_playing():
        vc.stop()

    finished = asyncio.Event()

    def _after_play(error):
        if error:
            logger.error(f"[/nggyu 播放錯誤]: {error}")
        bot.loop.call_soon_threadsafe(finished.set)

    try:
        vc.play(discord.FFmpegPCMAudio(NGGYU_FILE), after=_after_play)
    except Exception as e:
        return await interaction.followup.send(f"❌ Failed to play audio: {type(e).__name__}: {e}", ephemeral=True)

    await interaction.followup.send(f"📀 Never gonna give you up~ playing in {channel.mention}")
    await finished.wait()

    # 🎯 播完：如果原本是 /afkvoice 常駐狀態，切回原頻道恢復保活；不然就離開
    if original_channel:
        try:
            await asyncio.wait_for(vc.move_to(original_channel), timeout=15)
            await start_voice_keepalive(interaction.guild_id, vc)
        except Exception as e:
            logger.error(f"[/nggyu 切回原頻道失敗]: {e}")
    elif not existing_vc:
        await vc.disconnect(force=True)


@nggyu.error
async def nggyu_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Bro don't have the permission to do that.", ephemeral=True)

# =================================================================
# 🎵 點歌系統
# =================================================================
import yt_dlp

def _build_ytdlp_opts() -> dict:
    """每次呼叫時才檢查 cookies.txt 存不存在，不用改了 cookies 就要重啟 bot"""
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "source_address": "0.0.0.0",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"],
                "formats": ["missing_pot"],
            }
        },
    }
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    return opts

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def _ytdlp_extract(query: str) -> dict:
    with yt_dlp.YoutubeDL(_build_ytdlp_opts()) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return {
            "title": info.get("title", "Unknown"),
            "url": info.get("url"),
            "webpage_url": info.get("webpage_url"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail"),
        }


async def ytdlp_extract(query: str) -> dict:
    # 🎯 yt-dlp 的 extract_info 是阻塞式的，丟到另一個執行緒跑，避免卡住整個 bot 的事件迴圈
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ytdlp_extract, query)


class GuildMusicState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue = []
        self.current = None
        self.voice_client = None
        self.panel_message = None
        self.panel_channel_id = None
        self.is_paused = False
        self.was_afk_channel = None  # 🎯 播放前如果 /afkvoice 正掛在別的頻道，記住它，播完切回去


music_states = {}  # guild_id -> GuildMusicState

def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id)
    return music_states[guild_id]


class MusicControlView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="⏯️ Pause/Resume", style=discord.ButtonStyle.primary, custom_id="music:pauseresume")
    async def pause_resume(self, interaction: discord.Interaction, button: ui.Button):
        try:
            state = music_states.get(interaction.guild_id)
            if not state or not state.voice_client:
                return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            if state.voice_client.is_playing():
                state.voice_client.pause()
                state.is_paused = True
            elif state.voice_client.is_paused():
                state.voice_client.resume()
                state.is_paused = False
            await interaction.response.defer()
            await update_music_panel(interaction.guild_id)
        except discord.NotFound:
            pass

    @ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: ui.Button):
        try:
            state = music_states.get(interaction.guild_id)
            if not state or not state.voice_client:
                return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            state.voice_client.stop()  # 觸發 after callback，自動播下一首
            await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)
        except discord.NotFound:
            pass

    @ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.defer()
            await stop_music(interaction.guild_id)
        except discord.NotFound:
            pass

    @ui.button(label="📃 Queue", style=discord.ButtonStyle.secondary, custom_id="music:queue")
    async def show_queue(self, interaction: discord.Interaction, button: ui.Button):
        try:
            state = music_states.get(interaction.guild_id)
            if not state or not state.queue:
                return await interaction.response.send_message("📃 Queue is empty.", ephemeral=True)
            lines = [f"{i + 1}. {s['title']}" for i, s in enumerate(state.queue[:10])]
            await interaction.response.send_message("**Up Next:**\n" + "\n".join(lines), ephemeral=True)
        except discord.NotFound:
            pass


async def update_music_panel(guild_id: int, ended: bool = False):
    state = music_states.get(guild_id)
    if not state or not state.panel_channel_id:
        return
    channel = bot.get_channel(int(state.panel_channel_id))
    if not channel:
        return

    if ended or not state.current:
        embed = discord.Embed(title="📻 Music Panel", description="Queue ended.", color=0x2b2d31)
    else:
        song = state.current
        status = "⏸️ Paused" if state.is_paused else "▶️ Playing"
        embed = discord.Embed(title="📻 Now Playing", color=0x1db954, description=f"**[{song['title']}]({song['webpage_url']})**\n{status}")
        if song.get("thumbnail"):
            embed.set_thumbnail(url=song["thumbnail"])
        if state.queue:
            upnext = "\n".join(f"{i + 1}. {s['title']}" for i, s in enumerate(state.queue[:5]))
            embed.add_field(name="Up Next", value=upnext, inline=False)

    view = MusicControlView()
    try:
        if state.panel_message:
            await state.panel_message.edit(embed=embed, view=view)
        else:
            state.panel_message = await channel.send(embed=embed, view=view)
    except discord.NotFound:
        state.panel_message = await channel.send(embed=embed, view=view)
    except Exception as e:
        logger.error(f"[點歌面板更新失敗]: {e}")


async def play_next(guild_id: int):
    state = music_states.get(guild_id)
    if not state:
        return

    if not state.queue:
        state.current = None
        await update_music_panel(guild_id, ended=True)
        if state.voice_client:
            if state.was_afk_channel:
                try:
                    await asyncio.wait_for(state.voice_client.move_to(state.was_afk_channel), timeout=15)
                    await start_voice_keepalive(guild_id, state.voice_client)
                except Exception as e:
                    logger.error(f"[點歌] 切回 afk 頻道失敗: {e}")
                    await state.voice_client.disconnect(force=True)
                    state.voice_client = None
            else:
                await state.voice_client.disconnect(force=True)
                state.voice_client = None
        state.was_afk_channel = None
        return

    song = state.queue.pop(0)
    state.current = song
    state.is_paused = False

    def _after(error):
        if error:
            logger.error(f"[點歌播放錯誤]: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

    try:
        source = discord.FFmpegPCMAudio(song["url"], **FFMPEG_OPTS)
        state.voice_client.play(source, after=_after)
    except Exception as e:
        logger.error(f"[點歌播放失敗]: {e}")
        await play_next(guild_id)
        return

    await update_music_panel(guild_id)


async def stop_music(guild_id: int):
    state = music_states.get(guild_id)
    if not state:
        return
    state.queue.clear()
    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        state.voice_client.stop()  # 佇列已空，觸發 after -> play_next 會自動處理離開/切回 afk 頻道
    else:
        state.current = None
        if state.voice_client:
            if state.was_afk_channel:
                try:
                    await asyncio.wait_for(state.voice_client.move_to(state.was_afk_channel), timeout=15)
                    await start_voice_keepalive(guild_id, state.voice_client)
                except Exception as e:
                    logger.error(f"[點歌] stop 後切回 afk 頻道失敗: {e}")
            else:
                await state.voice_client.disconnect(force=True)
            state.voice_client = None
        state.was_afk_channel = None
        await update_music_panel(guild_id, ended=True)


# ---------- Slash 指令 ----------

@bot.tree.command(name="play", description="Play a song from YouTube in a voice channel")
@app_commands.describe(query="Song name or YouTube link", channel="Voice channel (defaults to your current voice channel)")
async def play(interaction: discord.Interaction, query: str, channel: Optional[discord.VoiceChannel] = None):
    await interaction.response.defer()

    target_channel = channel or (interaction.user.voice.channel if interaction.user.voice else None)
    if not target_channel:
        return await interaction.followup.send("❌ You're not in a voice channel, and no channel was specified.", ephemeral=True)

    perms = target_channel.permissions_for(interaction.guild.me)
    if not perms.connect or not perms.speak:
        return await interaction.followup.send(f"❌ I don't have Connect/Speak permission in {target_channel.mention}.", ephemeral=True)

    try:
        song_info = await ytdlp_extract(query)
    except Exception as e:
        return await interaction.followup.send(f"❌ Couldn't find that song: {type(e).__name__}: {e}", ephemeral=True)

    state = get_music_state(interaction.guild_id)
    state.panel_channel_id = str(target_channel.id)  # 🎯 控制面板發在語音頻道自己的文字聊天室

    existing_vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if existing_vc:
        if existing_vc.channel.id != target_channel.id:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id FROM voice_watch WHERE guild_id = ?", (str(interaction.guild_id),))
            watch_row = cursor.fetchone(); conn.close()
            if watch_row and int(watch_row[0]) == existing_vc.channel.id:
                state.was_afk_channel = existing_vc.channel  # 🎯 記住原本 /afkvoice 掛的頻道
            stop_voice_keepalive(interaction.guild_id)
            try:
                await asyncio.wait_for(existing_vc.move_to(target_channel), timeout=15)
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to move to voice channel: {e}", ephemeral=True)
        state.voice_client = existing_vc
    else:
        active_count = len([v for v in bot.voice_clients if v.is_connected()])
        if active_count >= MAX_VOICE_WATCH:
            return await interaction.followup.send(f"❌ 目前已達語音連線上限（{MAX_VOICE_WATCH} 個），請稍後再試。", ephemeral=True)
        try:
            state.voice_client = await asyncio.wait_for(target_channel.connect(timeout=15), timeout=20)
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to join voice channel: {type(e).__name__}: {e}", ephemeral=True)

    state.queue.append(song_info)
    await interaction.followup.send(f"✅ Added **{song_info['title']}** to the queue.")

    if not state.voice_client.is_playing() and not state.voice_client.is_paused():
        await play_next(interaction.guild_id)
    else:
        await update_music_panel(interaction.guild_id)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip_cmd(interaction: discord.Interaction):
    state = music_states.get(interaction.guild_id)
    if not state or not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    state.voice_client.stop()
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(name="pause", description="Pause the current song")
async def pause_cmd(interaction: discord.Interaction):
    state = music_states.get(interaction.guild_id)
    if not state or not state.voice_client or not state.voice_client.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    state.voice_client.pause()
    state.is_paused = True
    await update_music_panel(interaction.guild_id)
    await interaction.response.send_message("⏸️ Paused.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the current song")
async def resume_cmd(interaction: discord.Interaction):
    state = music_states.get(interaction.guild_id)
    if not state or not state.voice_client or not state.voice_client.is_paused():
        return await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
    state.voice_client.resume()
    state.is_paused = False
    await update_music_panel(interaction.guild_id)
    await interaction.response.send_message("▶️ Resumed.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    await stop_music(interaction.guild_id)
    await interaction.followup.send("⏹️ Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current song queue")
async def queue_cmd(interaction: discord.Interaction):
    state = music_states.get(interaction.guild_id)
    if not state or (not state.current and not state.queue):
        return await interaction.response.send_message("📃 Queue is empty.", ephemeral=True)
    desc = ""
    if state.current:
        desc += f"**Now Playing:** {state.current['title']}\n\n"
    desc += "\n".join(f"{i + 1}. {s['title']}" for i, s in enumerate(state.queue)) if state.queue else "*(no songs queued)*"
    embed = discord.Embed(title="📃 Queue", description=desc, color=0x2b2d31)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="addrole", description="Manually add a role to a user")
@app_commands.checks.has_permissions(administrator=True)
async def addrole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if role in user.roles:
        return await interaction.response.send_message(
            f"❌ {user.mention} already has **{role.name}**.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        await user.add_roles(role, reason=f"Added by {interaction.user}")
        await interaction.followup.send(
            f"✅ Gave {role.mention} to {user.mention}.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I can't manage that role (role hierarchy / missing permissions).", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


@bot.tree.command(name="removerole", description="Manually remove a role from a user")
@app_commands.checks.has_permissions(administrator=True)
async def removerole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if role not in user.roles:
        return await interaction.response.send_message(
            f"❌ {user.mention} doesn't have **{role.name}**.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        await user.remove_roles(role, reason=f"Removed by {interaction.user}")
        await interaction.followup.send(
            f"✅ Removed {role.mention} from {user.mention}.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I can't manage that role (role hierarchy / missing permissions).", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)
@bot.tree.command(name="setstreaks", description="Flooding or reducing the number of Streaks days.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="Users who are subject to spam or have their Streaks days reduced.", streaks="Set Streaks days. (>=0)")
async def setstreaks(interaction: discord.Interaction, user: discord.Member, streaks: int):
    if streaks < 0:
        return await interaction.response.send_message("❌ Ur math teather is crying.", ephemeral=True)
    
    gid = str(interaction.guild_id)
    uid = str(user.id)

    tz = datetime.timezone(datetime.timedelta(hours=0))
    today_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 📝 檢查該用戶在 streaks_data 是否已有資料，用以維護「最長連擊紀錄 (longest_streak)」
    cursor.execute("SELECT longest_streak FROM streaks_data WHERE guild_id = ? AND user_id = ?", (gid, uid))
    row = cursor.fetchone()
    
    if row:
        old_longest = row[0]
        new_longest = max(old_longest, streaks)  # 如果新設定的天數大於歷史紀錄，就同步更新最長紀錄
        cursor.execute("""
            UPDATE streaks_data 
            SET current_streak = ?, longest_streak = ?, last_streak_date = ?
            WHERE guild_id = ? AND user_id = ?
        """, (streaks, new_longest, today_str, gid, uid))
    else:
        # 若無歷史資料，則直接新增一筆
        cursor.execute("""
            INSERT INTO streaks_data (guild_id, user_id, current_streak, longest_streak, last_streak_date) 
            VALUES (?, ?, ?, ?, ?)
        """, (gid, uid, streaks, streaks, today_str))
        
    conn.commit()
    conn.close()

    # 🎯 同步更新這一週的簽到表格，灌水補✅、砍掉的天數還原❌
    recompute_current_week_status(gid, uid, streaks)
    
    # 🎯 自動補強：手動調天數後，自動觸發機器人內建的「身分組發放」與「暱稱表情符號」檢查
    await check_streak_roles(user, streaks)
    await apply_streak_nickname(user, streaks)
    
    await interaction.response.send_message(f"🔥 Now {user.mention}'s Streaks had setted to **{streaks}** days!", ephemeral=True)
    
@bot.tree.command(name="addpaidserver", description="[Owner Only] Add a server to a paid feature's whitelist")
@app_commands.describe(guild_id="The server ID that has paid", feature="The paid feature key, e.g. 67silent")
async def addpaidserver(interaction: discord.Interaction, guild_id: str, feature: str = "67silent"):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("❌ This command is owner-only.", ephemeral=True)

    if not guild_id.isdigit():
        return await interaction.response.send_message("❌ guild_id must be numeric.", ephemeral=True)

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO paid_features (guild_id, feature) VALUES (?, ?)", (guild_id, feature))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Added guild `{guild_id}` to `{feature}` whitelist.", ephemeral=True)


@bot.tree.command(name="removepaidserver", description="[Owner Only] Remove a server from a paid feature's whitelist")
@app_commands.describe(guild_id="The server ID to remove", feature="The paid feature key, e.g. 67silent")
async def removepaidserver(interaction: discord.Interaction, guild_id: str, feature: str = "67silent"):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("❌ This command is owner-only.", ephemeral=True)

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("DELETE FROM paid_features WHERE guild_id = ? AND feature = ?", (guild_id, feature))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Removed guild `{guild_id}` from `{feature}` whitelist.", ephemeral=True)

# =================================================================
# ⚡ 7. SYSTEM EVENTS
# =================================================================

@tasks.loop(minutes=1)
async def plane_rc_delivery_reminders():
    """UTC+8 12:00 — weekly on Monday, monthly on day 1."""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    if now.hour != 12 or now.minute != 0:
        return

    want = []
    if now.weekday() == 0:  # Monday
        want.append("weekly")
    if now.day == 1:
        want.append("monthly")
    if not want:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(want))
    cursor.execute(
        f"SELECT thread_id, guild_id, rc_period FROM plane_orders "
        f"WHERE rc_period IN ({placeholders}) AND status != 'completed'",
        want,
    )
    rows = cursor.fetchall()
    conn.close()

    for thread_id, guild_id, period in rows:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            continue
        thread = guild.get_thread(int(thread_id))
        if thread is None:
            try:
                thread = await guild.fetch_channel(int(thread_id))
            except discord.HTTPException:
                continue
        try:
            await thread.send(
                f"<@{USER_RC_REVIEW_1}> Reminder: **{period}** recurring delivery is due."
            )
        except discord.HTTPException:
            pass


@plane_rc_delivery_reminders.before_loop
async def before_plane_rc_loop():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for guild in bot.guilds:
        try: bot.invites[guild.id] = await guild.invites()
        except: pass

    if not plane_rc_delivery_reminders.is_running():
        plane_rc_delivery_reminders.start()

    # Restore PlaneOrderView for open orders (buttons work after restart)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT thread_id, prev_status FROM plane_orders "
            "WHERE status IS NULL OR status NOT IN ('completed')"
        )
        for thread_id, layout in cursor.fetchall():
            lay = layout if layout in ("full", "b", "c") else "full"
            try:
                bot.add_view(PlaneOrderView(int(thread_id), layout=lay))
            except Exception as e:
                logger.warning(f"[plane add_view] {thread_id}: {e}")
        conn.close()
    except Exception as e:
        logger.error(f"[plane restore views]: {e}")
    
    # 🎯 幫還沒有 Webhook 紀錄的既有伺服器（bot 加入時這個功能還不存在）補跑一次
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT guild_id FROM guild_webhooks")
    have_webhook_ids = {row[0] for row in cursor.fetchall()}
    conn.close()

    for guild in bot.guilds:
        if str(guild.id) not in have_webhook_ids:
            await ensure_guild_webhook(guild)

    # 🎯 重新連線先前設定的語音追蹤頻道（重啟後恢復，上限 5 個）
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT guild_id, channel_id FROM voice_watch")
    watch_rows = cursor.fetchall()
    conn.close()
    for gid, cid in watch_rows[:MAX_VOICE_WATCH]:
        guild = bot.get_guild(int(gid))
        channel = guild.get_channel(int(cid)) if guild else None
        if channel:
            try:
                vc = await channel.connect(self_mute=True, self_deaf=True)
                await start_voice_keepalive(int(gid), vc)  # 🎯 重連後也要啟動保活
                now = datetime.datetime.now().timestamp()
                for m in channel.members:
                    if not m.bot:
                        voice_sessions[(gid, str(m.id))] = now
            except Exception as e:
                logger.error(f"[Voice Reconnect 錯誤]: {e}")



@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    gid = str(member.guild.id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT channel_id FROM voice_watch WHERE guild_id = ?", (gid,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return

    watched_id = int(row[0])
    key = (gid, str(member.id))
    now = datetime.datetime.now().timestamp()

    was_in = before.channel is not None and before.channel.id == watched_id
    is_in = after.channel is not None and after.channel.id == watched_id

    if is_in and not was_in:
        voice_sessions[key] = now
    elif was_in and not is_in:
        start = voice_sessions.pop(key, None)
        if start:
            elapsed = int(now - start)
            if elapsed > 0:
                conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO voice_time (guild_id, user_id, seconds) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET seconds = seconds + excluded.seconds",
                    (gid, str(member.id), elapsed)
                )
                conn.commit(); conn.close()

async def ensure_guild_webhook(guild: discord.Guild):
    """幫指定伺服器確保有一個 Webhook 可用，優先沿用既有的，不會重複建立"""
    # 🎯 資料庫已經有紀錄就直接跳過，不用再做任何事
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT webhook_url FROM guild_webhooks WHERE guild_id = ?", (str(guild.id),))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return

    target_channel = None
    candidates = [guild.system_channel] if guild.system_channel else []
    candidates += guild.text_channels
    for ch in candidates:
        if ch and ch.permissions_for(guild.me).manage_webhooks:
            target_channel = ch
            break

    if not target_channel:
        logger.warning(f"[Webhook 建立失敗] 在 {guild.name} 找不到任何有 Manage Webhooks 權限的頻道，很可能是用舊版邀請連結加進來的")
        return

    try:
        # 🎯 先查 Discord 那邊這個伺服器有沒有已經存在、我們自己建立過的同名 Webhook，有就直接沿用
        existing_webhooks = await guild.webhooks()
        existing = discord.utils.find(
            lambda w: w.name == "67 Bot Webhook" and w.user and w.user.id == bot.user.id,
            existing_webhooks
        )

        if existing:
            webhook = existing
            used_channel_id = webhook.channel_id
            logger.info(f"[Webhook 沿用既有] {guild.name} 已經有一個，直接沿用，不重新建立")
        else:
            webhook = await target_channel.create_webhook(name="67 Bot Webhook")
            used_channel_id = target_channel.id
            logger.info(f"[Webhook 建立成功] {guild.name} -> #{target_channel.name}")

        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO guild_webhooks (guild_id, webhook_url, channel_id) VALUES (?, ?, ?)",
            (str(guild.id), webhook.url, str(used_channel_id))
        )
        conn.commit(); conn.close()
    except discord.Forbidden:
        logger.warning(f"[Webhook 建立失敗] 沒有權限在 {guild.name} 建立/讀取 Webhook")
    except Exception as e:
        logger.error(f"[Webhook 建立錯誤] {guild.name}: {e}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    await ensure_guild_webhook(guild)

@bot.event
async def on_member_join(member: discord.Member):
    if member.bot: return
    if not is_feature_enabled(member.guild.id, "welcome"): return
    try:
        guild = member.guild
        inviter_found = None
        
        # 🎯 終極修正：防範斷線重連、用完即焚碼、剛創即用碼，彻底消除 Someone 盲區
        if guild.id not in bot.invites:
            try: bot.invites[guild.id] = await guild.invites()
            except: bot.invites[guild.id] = []

        try:
            old_invites = bot.invites[guild.id] or []
            new_invites = await guild.invites()
            bot.invites[guild.id] = new_invites  # 即時更新快取
            
            old_dict = {inv.code: inv for inv in old_invites}
            new_dict = {inv.code: inv for inv in new_invites}
            
            # 1. 優先檢查：使用次數有增加的，或者「舊快取沒有」但一進來次數大於 0 的新代碼
            for code, new_inv in new_dict.items():
                if code in old_dict:
                    if new_inv.uses > old_dict[code].uses:
                        inviter_found = new_inv.inviter
                        break
                else:
                    if new_inv.uses > 0:
                        inviter_found = new_inv.inviter
                        break
                        
            # 2. 備援檢查：如果是「單次使用網址」，用完就從新清單消失了，比對誰不見了
            if not inviter_found:
                for code, old_inv in old_dict.items():
                    if code not in new_dict:
                        inviter_found = old_inv.inviter
                        break
        except Exception as invite_err:
            logger.error(f"[邀請碼追蹤失敗]: {invite_err}")
                
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, w_title, w_desc FROM welcome WHERE guild_id = ?", (str(guild.id),))
        row = cursor.fetchone(); conn.close()
        
        if row and row[0]:
            try:
                channel_id = int(row[0])
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            except: return
            if channel:
                # 🎯 修正點：將 inviter_found 傳入工具函式，不再盲目吐出 Someone
                title = parse_placeholders(row[1] or "Welcome!", member, guild, inviter=inviter_found)
                desc = parse_placeholders(row[2], member, guild, inviter=inviter_found)
                
                embed_color = discord.Color(0x54a7dd)
                try:
                    from PIL import Image
                    import io
                    avatar_bytes = await member.display_avatar.with_format("png").read()
                    img = Image.open(io.BytesIO(avatar_bytes))
                    img = img.resize((1, 1))
                    rgb = img.getpixel((0, 0))
                    embed_color = discord.Color.from_rgb(rgb[0], rgb[1], rgb[2])
                except: pass
                
                embed = discord.Embed(title=title, description=desc, color=embed_color)
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"{guild.name}｜67")
                await channel.send(content=member.mention, embed=embed)

        # 🎯 Give role when join：不管有沒有設定歡迎頻道，只要有設定身分組就發放
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT role_id FROM welcome_roles WHERE guild_id = ?", (str(guild.id),))
        role_rows = cursor.fetchall(); conn.close()
        if role_rows:
            roles_to_give = [guild.get_role(int(rid)) for (rid,) in role_rows]
            roles_to_give = [r for r in roles_to_give if r is not None]
            if roles_to_give:
                try:
                    await member.add_roles(*roles_to_give, reason="Give role when join")
                except discord.Forbidden:
                    logger.error(f"[Give role when join] 沒有權限在 {guild.name} 給 {member} 加身分組")
    except Exception as e: logger.error(f"[on_member_join 崩潰]: {e}")

async def send_goodbye_message(member: discord.Member, guild: discord.Guild):
    """發送離群訊息（涵蓋自己離開、被踢、被封鎖），共用邏輯，讓 /kick、/ban 可以主動呼叫，不用等待可能漏掉的 Gateway 事件"""
    if member.bot: return
    if not is_feature_enabled(guild.id, "welcome"): return
    try:
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(guild.id),))
        row = cursor.fetchone(); conn.close()
        if row and row[0]:
            try:
                channel_id = int(row[0])
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            except: return
            if channel:
                title = parse_placeholders(row[1] or "Goodbye!", member, guild)
                desc = parse_placeholders(row[2], member, guild)
                embed_color = discord.Color(0xe74c3c)
                embed = discord.Embed(title=title, description=desc, color=embed_color)
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"{guild.name}｜67")
                await channel.send(embed=embed)
    except Exception as e: logger.error(f"[send_goodbye_message 崩潰]: {e}")


@bot.event
async def on_member_remove(member: discord.Member):
    # 🎯 涵蓋「自己退出」跟其他不是透過我們自己指令觸發的移除（例如用 Discord 原生介面踢人）
    await send_goodbye_message(member, member.guild)


async def tavily_search(query: str) -> str:
    """使用 Tavily API 進行非同步聯網搜尋"""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        logger.warning("⚠️ TAVILY_API_KEY 未設定，將跳過連網搜尋。")
        return "未提供連網搜尋資料。"

    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",  # basic 速度最快且省額度
        "max_results": 3          # 只抓最相關的前 3 筆，避免填滿 Token
    }
    
    try:
        # 使用 aiohttp 發送非同步 POST 請求，不卡住機器人主執行緒
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("results", [])
                    if not results:
                        return "網路搜尋不到相關結果。"
                    
                    # 萃取網頁標題與精煉內容
                    search_results = []
                    for r in results:
                        search_results.append(f"標題: {r.get('title')}\n內容: {r.get('content')}\n")
                    return "\n".join(search_results)
                else:
                    logger.error(f"[Tavily API 錯誤] 狀態碼: {response.status}")
                    return "搜尋失敗，暫時無法取得網路即時資訊。"
    except Exception as e:
        logger.error(f"[Tavily 執行錯誤]: {e}")
        return "搜尋時發生錯誤。"

async def handle_plane_forum_post(thread: discord.Thread):
    """Validate tags, post order message + buttons, or error and delete."""
    await asyncio.sleep(1.5)  # wait for applied_tags to settle

    try:
        thread = await thread.guild.fetch_channel(thread.id)
    except discord.HTTPException:
        pass

    tag_ids = {t.id for t in (getattr(thread, "applied_tags", None) or [])}
    purpose_ids = {TAG_MAIN_A, TAG_MAIN_B, TAG_MAIN_C}
    purpose = tag_ids & purpose_ids
    locations = tag_ids & PLANE_LOCATION_TAGS

    # exactly 1 purpose tag, at least 1 location tag
    if len(purpose) != 1 or len(locations) < 1:
        try:
            await thread.send(
                "❌ Invalid tags: need **exactly one** purpose tag "
                "and **at least one** location tag.\n"
                "This post will be deleted in 10 seconds."
            )
        except discord.HTTPException:
            pass
        await asyncio.sleep(10)
        try:
            await thread.delete(reason="Invalid plane order tags")
        except discord.HTTPException:
            pass
        return

    purpose_tag = next(iter(purpose))
    layout = "full"
    text = ""

    if purpose_tag == TAG_MAIN_A:
        layout = "full"
        if locations & {TAG_LOC_SELL_1, TAG_LOC_SELL_2}:
            role_ping = f"<@&{ROLE_SELL_A}>"
        else:
            role_ping = f"<@&{ROLE_SELL_B}>"
        text = (
            f"Pls check the latest delivery time at {DELIVERY_INFO_LINK} first.\n"
            f"{role_ping}, time to sell planes.\n"
            f"- If you are willing to assist with sales/reselling, please press `Take Over`.\n"
            f"- If the order is completed, please press `Completed`."
        )
    elif purpose_tag == TAG_MAIN_B:
        layout = "b"
        text = (
            f"<@&{ROLE_HELP_B}>, do you guys want to help?\n"
            f"- If you are willing to assist with sales/reselling, please press `Take Over`.\n"
            f"- If the order is completed, please press `Completed`."
        )
    elif purpose_tag == TAG_MAIN_C:
        layout = "c"
        text = (
            f"<@{USER_PING_C}>，有人要賣飛機啦\n"
            f"- If you are willing to assist with sales/reselling, please press `Take Over`.\n"
            f"- If the order is completed, please press `Completed`."
        )
    else:
        return

    view = PlaneOrderView(thread.id, layout=layout)
    try:
        msg = await thread.send(content=text, view=view)
    except discord.HTTPException as e:
        logger.error(f"[plane order send failed]: {e}")
        return

    plane_order_upsert(
        thread.id,
        thread.guild.id,
        message_id=str(msg.id),
        status="open",
        prev_status=layout,  # stash layout for add_view after restart
    )
    try:
        bot.add_view(PlaneOrderView(thread.id, layout=layout))
    except Exception as e:
        logger.warning(f"[plane add_view on create]: {e}")

@bot.event
async def on_thread_create(thread: discord.Thread):
    if thread.guild is None or thread.guild.id != PLANE_GUILD_ID:
        return
    if thread.parent_id != PLANE_FORUM_ID:
        return
    await handle_plane_forum_post(thread)

async def handle_plane_prefix(message: discord.Message, rest: str):
    thread = message.channel
    assert isinstance(thread, discord.Thread)
    parts = rest.split()
    if not parts:
        return
    cmd = parts[0].lower()
    args = parts[1:]
    uid = message.author.id
    is_rev = _plane_is_reviewer(uid)

    async def find_order_message():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT message_id FROM plane_orders WHERE thread_id = ?",
            (str(thread.id),),
        )
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        try:
            return await thread.fetch_message(int(row[0]))
        except discord.HTTPException:
            return None

    # takeover
    if cmd == "takeover":
        msg = await find_order_message()
        if not msg:
            return await message.reply("❌ Order message not found.")
        await _plane_edit_status_line(msg, f"👉 Taken over by：{message.author.mention}")
        layout = "full"
        await msg.edit(
            view=PlaneOrderView(thread.id, layout=layout, take_assign_disabled=True)
        )
        plane_order_upsert(
            thread.id, message.guild.id, status="taken", taken_by=str(uid), prev_status="open"
        )
        return await message.reply("✅ Taken over.")

    # complete
    if cmd == "complete":
        msg = await find_order_message()
        title = thread.name or ""
        if msg:
            await _plane_edit_status_line(msg, "【Status: Completed】")
            await msg.edit(view=PlaneOrderView(thread.id, layout="full", all_disabled=True))
        if not title.startswith("[Completed] "):
            try:
                await thread.edit(name=("[Completed] " + title)[:100])
            except discord.HTTPException:
                pass
        parent = thread.parent
        new_tags = []
        if isinstance(parent, discord.ForumChannel):
            tag = discord.utils.get(parent.available_tags, id=TAG_COMPLETED)
            if tag:
                new_tags = [tag]
        try:
            await thread.edit(applied_tags=new_tags, locked=True, archived=True)
        except discord.HTTPException as e:
            return await message.reply(f"⚠️ {e}")
        plane_order_upsert(
            thread.id, message.guild.id, status="completed", prev_title=title
        )
        return await message.reply("✅ Completed.")

    # assign @user
    if cmd == "assign":
        if not isinstance(message.author, discord.Member) or not _plane_has_assign_role(
            message.author
        ):
            return await message.reply("❌ No permission.")
        target = None
        if message.mentions:
            target = message.mentions[0]
        elif args and args[0].isdigit():
            target = message.guild.get_member(int(args[0]))
        if not target:
            return await message.reply("❌ Usage: `m.assign @user`")
        msg = await find_order_message()
        if not msg:
            return await message.reply("❌ Order message not found.")
        await _plane_edit_status_line(msg, f"👉 Taken over by：{target.mention}")
        await msg.edit(
            view=PlaneOrderView(thread.id, layout="full", take_assign_disabled=True)
        )
        plane_order_upsert(
            thread.id, message.guild.id, status="taken", taken_by=str(target.id)
        )
        return await message.reply(f"✅ Assigned to {target.mention}.")

    # rc weekly|monthly
    if cmd == "rc":
        if not is_rev:
            return await message.reply("❌ No permission.")
        if not args or args[0].lower() not in ("weekly", "monthly"):
            return await message.reply("❌ Usage: `m.rc weekly` or `m.rc monthly`")
        period = args[0].lower()
        prefix = "[Weekly] " if period == "weekly" else "[Monthly] "
        title = thread.name or ""
        if not title.startswith("[Weekly] ") and not title.startswith("[Monthly] "):
            try:
                await thread.edit(name=(prefix + title)[:100])
            except discord.HTTPException as e:
                return await message.reply(f"❌ {e}")
        plane_order_upsert(
            thread.id,
            message.guild.id,
            status=f"rc_{period}",
            rc_period=period,
            prev_title=title,
        )
        return await message.reply(f"✅ Set recurring **{period}**.")

    # back
    if cmd == "back":
        if not is_rev:
            return await message.reply("❌ No permission.")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT prev_status, prev_title, status FROM plane_orders WHERE thread_id = ?",
            (str(thread.id),),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return await message.reply("❌ No history.")
        prev_status, prev_title, _ = row
        if prev_title:
            try:
                await thread.edit(name=prev_title[:100])
            except discord.HTTPException:
                pass
        plane_order_upsert(
            thread.id, message.guild.id, status=prev_status or "open", rc_period=None
        )
        return await message.reply("✅ Reverted last step (best-effort).")

    # lock / unlock / close / open
    if cmd == "lock":
        if not is_rev:
            return await message.reply("❌ No permission.")
        await thread.edit(locked=True)
        return await message.reply("🔒 Locked.")

    if cmd == "unlock":
        if not is_rev:
            return await message.reply("❌ No permission.")
        await thread.edit(locked=False)
        return await message.reply("🔓 Unlocked.")

    if cmd == "close":
        if not is_rev:
            return await message.reply("❌ No permission.")
        await thread.edit(archived=True)
        return await message.reply("📁 Closed.")

    if cmd == "open":
        if not is_rev:
            return await message.reply("❌ No permission.")
        await thread.edit(archived=False, locked=False)
        return await message.reply("📂 Opened.")

    # rcdelivery
    if cmd == "rcdelivery":
        if not is_rev:
            return await message.reply("❌ No permission.")
        await thread.send(
            f"<@{USER_RC_REVIEW_1}> Manual recurring delivery reminder."
        )
        return await message.reply("✅ Sent.")

    # rcreturn
    if cmd == "rcreturn":
        if not is_rev:
            return await message.reply("❌ No permission.")
        title = thread.name or ""
        for p in ("[Weekly] ", "[Monthly] "):
            if title.startswith(p):
                title = title[len(p) :]
                break
        try:
            await thread.edit(name=title[:100] or "order")
        except discord.HTTPException as e:
            return await message.reply(f"❌ {e}")
        plane_order_upsert(
            thread.id, message.guild.id, status="open", rc_period=None
        )
        return await message.reply("✅ Returned to normal order.")

@bot.event  # 🎯 已將 @client.event 修改為 @bot.event
async def on_message_delete(message):
    """當使用者刪除（收回）訊息時，檢查是否有正在執行的 AI 任務，有則立即強制取消"""
    if 'active_ai_tasks' in globals() and message.id in globals()['active_ai_tasks']:
        task, user_id = globals()['active_ai_tasks'][message.id]
        if not task.done():
            task.cancel()
            logger.info(f"⚡ 已成功發送取消訊號至訊息 ID {message.id} 的 AI 任務。")

    # --- Warn DM reply → 轉發到伺服器 reply 頻道 ---
    if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
        if message.reference and message.reference.message_id:
            ref_id = str(message.reference.message_id)
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, guild_id, target_id, warner_id, reply_text "
                "FROM warns WHERE dm_message_id = ? AND target_id = ?",
                (ref_id, str(message.author.id)),
            )
            row = cursor.fetchone()
            if row:
                warn_id, gid, target_id, warner_id, old_reply = row
                # 僅在仍允許 reply 且尚未回過（或允許覆蓋）時處理
                cursor.execute(
                    "SELECT reply_allowed, reply_channel_id, dashboard_enabled "
                    "FROM warn_settings WHERE guild_id = ?",
                    (gid,),
                )
                srow = cursor.fetchone()
                conn.close()

                if srow and int(srow[0] or 0) == 1 and srow[1]:
                    # 更新 DB（dashboard off 時根本不會有這筆，通常進不來）
                    now = discord.utils.utcnow().isoformat()
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE warns SET reply_text = ?, reply_at = ? WHERE id = ?",
                        (message.content, now, warn_id),
                    )
                    conn.commit()
                    conn.close()

                    guild = bot.get_guild(int(gid))
                    channel = bot.get_channel(int(srow[1]))
                    if guild and channel:
                        warner = guild.get_member(int(warner_id)) or bot.get_user(int(warner_id))
                        receiver = message.author
                        if warner is None:
                            warner = discord.Object(id=int(warner_id))  # mention 仍可用 id
                        try:
                            await channel.send(
                                view=build_warn_reply_channel_view(
                                    warner, receiver, message.content
                                )
                            )
                        except Exception as e:
                            logger.error(f"[Warn reply forward]: {e}")
                    return  # 私訊回覆不跑後面伺服器邏輯
            else:
                conn.close()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # ----- Plane order prefix commands (m.) -----
    raw = (message.content or "").strip()
    if raw.lower().startswith("m."):
        if (
            message.guild
            and message.guild.id == PLANE_GUILD_ID
            and isinstance(message.channel, discord.Thread)
            and message.channel.parent_id == PLANE_FORUM_ID
        ):
            await handle_plane_prefix(message, raw[2:].strip())
        return  # 不讓 m. 進 AI；若要進 AI 就刪掉這行 return
        
    # ----- 67+AI trigger -----
    trigger_ai = False
    require_mention = True

    if message.guild is None:
        # DM: no @ required
        trigger_ai = True
        require_mention = False
    else:
        gid = message.guild.id
        only_sel, ai_cid = get_ai_channel_mode(gid)

        if only_sel and ai_cid:
            if str(message.channel.id) == str(ai_cid):
                trigger_ai = True
                require_mention = False
            else:
                trigger_ai = False
        else:
            trigger_ai = bot.user.mentioned_in(message) and not message.mention_everyone
            require_mention = True

        if trigger_ai and not is_ai_enabled(gid):
            if not isinstance(message.author, discord.Member):
                trigger_ai = False
            else:
                perms = message.channel.permissions_for(message.author)
                if not perms.manage_channels:
                    trigger_ai = False

    if message.guild and is_feature_enabled(message.guild.id, "autoreact"):
        content_lower = (message.content or "").lower()
        if content_lower:
            for _rid, trigger, emoji_raw in list_auto_reactions(message.guild.id):
                if trigger.lower() in content_lower:
                    em = parse_reaction_emoji(emoji_raw)
                    if em is None:
                        continue
                    try:
                        await message.add_reaction(em)
                    except (discord.HTTPException, discord.Forbidden):
                        pass
    
    if trigger_ai:
        clean_content = (
            (message.content or "")
            .replace(f"<@{bot.user.id}>", "")
            .replace(f"<@!{bot.user.id}>", "")
            .strip()
        )
        if not require_mention and not clean_content and not message.attachments:
            return

        # image_url / word limit / cooldown / AI call... (keep your existing code below)
        # 🧹 拔除訊息中的機器人標籤與前後空格
        clean_content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

        # 🖼️ 檢查有沒有附帶圖片（附件本身是圖片，或圖片以連結形式貼上都算）
        image_url = None
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_url = att.url
                break

        # 狀況 A：完全沒有文字、也沒有圖片 -> 觸發原本的極度厭世英文回覆
        if not clean_content and not image_url:
            annoyed_phrases = [
                "Why are you even pinging me? Go away.",
                "Don't @ me for no reason. I'm exhausted.",
                "What do you want now? Stop messing with me.",
                "Pinged me for what? Just let me exist in peace.",
                "Unless the server is literally burning down, don't @ me."
            ]
            await message.reply(random.choice(annoyed_phrases))
            return

        # 🖼️ 只有圖片、沒有文字的話，給一個預設提示詞，讓 AI 知道要做什麼
        if not clean_content and image_url:
            clean_content = "What's in this image?"

        # 狀況 B：後面有字 -> 限制檢查並呼叫 Groq
        word_count = len(clean_content.split())
        if word_count > 100:
            await message.reply(f"❌ nah, u give me {word_count} words, too much.")
            return

        # 使用者冷卻時間 30 秒
        current_time = time.time()
        user_id = message.author.id
        if user_id in ai_cooldowns:
            time_passed = current_time - ai_cooldowns[user_id]
            if time_passed < 30:
                remaining = int(30 - time_passed)
                await message.reply(f"⏱️ nah, u asked too much. Wait for {remaining} seconds.")
                return

        ai_cooldowns[user_id] = current_time

# 🚀 直接呼叫 AI (整合 10層/10分鐘連貫回溯、Tavily 連網、收回偵測、雙級 API 備援)
        if 'active_ai_tasks' not in globals():
            globals()['active_ai_tasks'] = {}

        try:
            # 📌 紀錄當前協程任務與用戶 ID，以便收回訊息時可以即時中斷
            globals()['active_ai_tasks'][message.id] = (asyncio.current_task(), user_id)

            async with message.channel.typing():

                # -----------------------------------------------------------
                # 🧠 核心邏輯：動態爬軌跡，最多回溯 10 則、只要是回覆就算數，不限時間
                # -----------------------------------------------------------
                conversation_history = []
                current_ref = message.reference
                history_count = 0

                logger.info("🔍 開始追溯單獨連貫的回覆鏈...")
                while current_ref and current_ref.message_id and history_count < 10:
                    try:
                        ref_msg = await message.channel.fetch_message(current_ref.message_id)

                        if ref_msg.author.id == bot.user.id:
                            role = "assistant"
                            content = ref_msg.content.split("\n\n-# **67+AI")[0].split("\n\n-# 67+AI")[0].split("\n\n67+AI")[0].strip()
                        else:
                            role = "user"
                            content = ref_msg.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

                        conversation_history.insert(0, {"role": role, "content": content})
                        history_count += 1
                        current_ref = ref_msg.reference

                    except Exception as chain_err:
                        logger.warning(f"⚠️ 無法獲取回覆鏈中某個節點的訊息 (可能被刪除): {chain_err}")
                        break

                logger.info(f"✨ 成功載入 {history_count} 則連貫上下文記憶！")

                use_tools = message.guild is not None and isinstance(message.author, discord.Member)

                if conversation_history:
                    search_query = f"{conversation_history[0]['content']} {clean_content}"
                else:
                    search_query = clean_content

                search_context = await tavily_search(search_query)

                # 判斷這次有沒有真正的連網結果（失敗／沒 key 都不算有連網）
                _no_search_markers = (
                    "未提供連網搜尋資料",
                    "網路搜尋不到相關結果",
                    "搜尋失敗",
                    "搜尋時發生錯誤",
                )
                has_web_search = bool(search_context) and not any(
                    m in search_context for m in _no_search_markers
                )

                # 共用：禁止模型否認連網；有 Tavily 結果就當即時資訊用
                web_search_instruction = (
                    "You ARE given real-time web search results below (from Tavily). "
                    "Treat them as current internet information when answering. "
                    "NEVER say you cannot browse the web, cannot search the internet, or lack internet access. "
                    "If the block says no data / search failed / key missing, say you don't have enough live info for that topic — "
                    "do NOT claim you have no browsing ability.\n\n"
                    f"【請優先參考以下網路即時資訊回答】：\n{search_context}"
                )

                ai_messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are an AI model in a Discord bot called '67'. You like to say 67 (but don't say it too often) and respond just like Meta AI. "
                            "Drop the corporate PR tone, be direct, slightly witty. "
                            "You also have real moderation/admin tools available (mute, kick, ban, warn, role management, economy, level, streaks). "
                            "Use your own judgement liberally to decide when to use them — you don't need an explicit command-like phrase. "
                            "If the conversation clearly describes someone misbehaving (annoying, spamming, toxic, etc), take an appropriate "
                            "action yourself (e.g. a short mute) instead of just talking about it. "
                            f"{await build_target_candidates(message)}\n\n"
                            "Use ENGLISH to response. but if the user use chinese, u should use TRADITIONAL CHINESE to response. "
                            "DONT use Simplified chinese. Max 800 characters.\n\n"
                            f"{web_search_instruction}"
                        )
                    }
                ]
                ai_messages.extend(conversation_history)
                if image_url:
                    ai_messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": clean_content},
                            {"type": "image_url", "image_url": {"url": image_url}}
                        ]
                    })
                else:
                    ai_messages.append({"role": "user", "content": clean_content})

                ai_reply = None
                tool_embeds = []

                # ===========================================================
                # 🛡️ ⚔️ 三陣營火線防禦機制 (Kimi -> Gemini -> Groq)
                # ===========================================================
                used_provider = None

                # ───【第一防線：Kimi（Moonshot）】───
                # 與 kimi_client 一致：用 KIMI_API_KEY（若你 .env 只有 MOONSHOT_API_KEY 也可二擇一）
                if (os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")) and not ai_reply:
                    if is_political_topic(clean_content):
                        logger.info("🚫 [第一防線] 偵測到政治相關內容，跳過 Kimi（中國模型），直接進下一防線")
                    else:
                        try:
                            logger.info("🤖 [1/3] 優先請求 Kimi API...")
                            kimi_messages = [
                                {
                                    "role": "system",
                                    "content": (
                                        "You are an AI model in a Discord bot called '67'. "
                                        "IMPORTANT: You must NEVER discuss politics, political figures, political parties, "
                                        "elections, government policy, geopolitical conflicts, or any politically sensitive "
                                        "topics of any country. If asked about politics, politely decline and say you can't "
                                        "discuss political topics, then offer to help with something else. "
                                        "You also have real moderation/admin tools available. Use your own judgement liberally — "
                                        "you don't need an explicit command-like phrase. If the conversation clearly describes someone "
                                        "misbehaving (annoying, spamming, toxic, etc), take an appropriate action yourself (e.g. a short mute). "
                                        f"{await build_target_candidates(message)}\n\n"
                                        "Use ENGLISH to response. but if the user use chinese, u should use TRADITIONAL CHINESE "
                                        "to response. DONT use Simplified chinese. Max 800 characters.\n\n"
                                        f"{web_search_instruction}"
                                    )
                                }
                            ]
                            kimi_messages.extend(conversation_history)
                            if image_url:
                                kimi_messages.append({
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": clean_content},
                                        {"type": "image_url", "image_url": {"url": image_url}}
                                    ]
                                })
                            else:
                                kimi_messages.append({"role": "user", "content": clean_content})

                            ai_reply, tool_embeds = await run_agent_completion(
                                kimi_client, "kimi-k3", kimi_messages, message.guild, message.author, use_tools
                            )
                            if ai_reply:
                                logger.info("✨ [第一防線] Kimi 成功回應！")
                                used_provider = "kimi"
                        except Exception as kimi_err:
                            logger.warning(f"⚠️ [第一防線] Kimi 失敗: {kimi_err}，準備切換下一順位...")

                # ───【第二防線：直連 Google Gemini API 輪詢機制】───
                if os.getenv("GEMINI_API_KEY") and not ai_reply:
                    gemini_models = [
                        "gemini-3.6-flash",
                        "gemini-3.5-flash",
                        "gemini-3-flash",
                        "gemini-2.5-flash",
                        "gemini-3.5-flash-lite",
                        "gemini-3.1-flash-lite",
                    ]
                    for model_name in gemini_models:
                        try:
                            logger.info(f"🤖 [2/3] 請求直連 Gemini API ({model_name})...")
                            ai_reply, tool_embeds = await run_agent_completion(
                                gemini_client, model_name, ai_messages, message.guild, message.author, use_tools
                            )
                            if ai_reply:
                                logger.info(f"✨ [第二防線] 直連 Gemini ({model_name}) 成功救援故事！")
                                if model_name in [
                                    "gemini-3.6-flash",
                                    "gemini-3.5-flash",
                                    "gemini-3-flash",
                                    "gemini-2.5-flash",
                                ]:
                                    used_provider = "gemini_loop"
                                else:
                                    used_provider = "gemini_lite"
                                break
                        except Exception as gemini_err:
                            logger.warning(
                                f"⚠️ [第二防線] Gemini ({model_name}) 直連失敗: {gemini_err}，準備切換下一順位..."
                            )

                # ───【第三防線：Groq API 終極備援】───
                if not ai_reply:
                    try:
                        logger.info(
                            "🤖 [3/3] 前方失敗！觸發最終底線，請求 Groq API (llama-3.3-70b-versatile)..."
                        )
                        ai_reply, tool_embeds = await run_agent_completion(
                            groq_client,
                            "llama-3.3-70b-versatile",
                            ai_messages,
                            message.guild,
                            message.author,
                            use_tools,
                            max_tokens=600,
                        )
                        if ai_reply:
                            logger.info("✨ [第三防線] Groq 終極防線救援成功！")
                            used_provider = "groq"
                    except Exception as groq_err:
                        logger.error(f"❌ [第三防線] Groq 也失敗了: {groq_err}")

                if not ai_reply:
                    logger.error("❌ [核心崩潰] Kimi、Gemini 與 Groq API 管道於本次請求中全數癱瘓。")
                    await message.reply("❌ 67+AI suck. Try again later.")
                    return

                if used_provider not in ["gemini_loop", "gemini_lite"] and len(ai_reply) > 700:
                    ai_reply = ai_reply[:697] + "..."

                no_net = "" if has_web_search else ", No internet search"

                if tool_embeds:
                    watermark = (
                        f"-# **67+Agent (Beta**{no_net}**)** Powered by 67+AI. "
                        "67+AI suck, it might be disorder."
                    )
                else:
                    if used_provider == "gemini_loop":
                        watermark = (
                            f"-# **67+AI (2.7 loop{no_net})**｜"
                            "67+AI suck and frequently makes mistakes; please verify it yourself."
                        )
                    elif used_provider == "gemini_lite":
                        watermark = (
                            f"-# **67+AI (2.5a{no_net})**｜"
                            "67+AI suck and frequently makes mistakes; please verify it yourself."
                        )
                    elif used_provider == "groq":
                        watermark = (
                            f"-# **67+AI (1{no_net})**｜"
                            "67+AI suck and frequently makes mistakes; please verify it yourself."
                        )
                    elif used_provider == "kimi":
                        watermark = (
                            f"-# **67+AI (3{no_net})**｜"
                            "67+AI suck and frequently makes mistakes; please verify it yourself."
                        )
                    else:
                        watermark = (
                            f"-# 67+AI{no_net} suck and frequently makes mistakes; please verify it yourself."
                        )

                final_content = f"{ai_reply}\n\n{watermark}"
                try:
                    await message.reply(content=final_content, embeds=tool_embeds[:10])
                except Exception as e:
                    logger.error(f"[Agent 回覆發送失敗]: {e}")
                return

        except Exception as e:
            logger.error(f"❌ AI 處理過程發生錯誤: {e}")

        finally:
            if 'active_ai_tasks' in globals() and message.id in globals()['active_ai_tasks']:
                globals()['active_ai_tasks'].pop(message.id, None)

    # 🎯 以下都是伺服器限定功能（自動禁言、67 統計、等級、Streaks），私訊沒有 guild，到此為止
    if not message.guild:
        return

    # =================================================================
    # 🔒 自動禁言已改用 Discord 原生 AutoMod 處理，訊息在送出前就會被擋下，
    # 不需要再自己掃描 message.content，這裡直接建立資料庫連線給後面的區塊用
    # =================================================================
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    gid = str(message.guild.id)
    uid = str(message.author.id)

    # =================================================================
    # 🔢 數數頻道（...）
    # =================================================================
    if is_feature_enabled(gid, "counting"):
        cursor.execute("SELECT channel_id FROM counting_settings WHERE guild_id = ?", (gid,))
        c_row = cursor.fetchone()
        if c_row and c_row[0] and str(c_row[0]) == str(message.channel.id):
            content = message.content.strip()
            is_admin = (
                message.author.guild_permissions.administrator
                if isinstance(message.author, discord.Member)
                else False
            )

            def _parse_count_int(s: str):
                if not s:
                    return None
                if s.isdigit():
                    return int(s)
                if s[0] == "-" and len(s) > 1 and s[1:].isdigit():
                    return int(s)
                return None

            number = _parse_count_int(content)
            if number is None:
                if not is_admin:
                    try:
                        await message.delete()
                    except discord.Forbidden:
                        logger.error(
                            f"[Counting] 沒有權限刪除 {message.author} 在 {message.guild.name} 的非數字訊息"
                        )
                    except discord.NotFound:
                        pass
                conn.close()
                return

            async with get_counting_lock(message.guild.id):
                cursor.execute(
                    "SELECT current_count, last_user_id, mute_duration FROM counting_settings WHERE guild_id = ?",
                    (gid,),
                )
                s_row = cursor.fetchone()
                current_count, last_user_id, mute_dur = s_row if s_row else (0, None, "10m")

                if current_count == 0:
                    valid = number in (1, -1)
                    expected_hint = "1 or -1"
                elif current_count > 0:
                    valid = number == current_count + 1
                    expected_hint = str(current_count + 1)
                else:
                    valid = number == current_count - 1
                    expected_hint = str(current_count - 1)

                same_user = (
                    last_user_id is not None
                    and str(message.author.id) == str(last_user_id)
                )

                if valid and not same_user:
                    cursor.execute(
                        "UPDATE counting_settings SET current_count = ?, last_user_id = ? WHERE guild_id = ?",
                        (number, str(message.author.id), gid),
                    )
                    conn.commit()
                    try:
                        await message.add_reaction("✅")
                    except discord.Forbidden:
                        pass
                else:
                    if same_user and valid:
                        reason = "connected two numbers in a row"
                    else:
                        reason = f"wrong number (expected {expected_hint})"
                    cursor.execute(
                        "UPDATE counting_settings SET current_count = 0, last_user_id = NULL WHERE guild_id = ?",
                        (gid,),
                    )
                    conn.commit()

                    try:
                        await message.add_reaction("❌")
                    except discord.Forbidden:
                        pass

                    delta, _ = parse_mute_duration(mute_dur or "10m")
                    if delta:
                        try:
                            await message.author.timeout(
                                delta,
                                reason=f"Broke the counting channel: {reason}",
                            )
                        except discord.Forbidden:
                            logger.error(
                                f"[Counting] 沒有權限禁言 {message.author} in {message.guild.name}"
                            )

                    try:
                        await message.channel.send(
                            f"💥 {message.author.mention} broke the count at **{number}** ({reason})! "
                            f"Count reset to **0**. Next: **1** or **-1**."
                        )
                    except Exception as e:
                        logger.error(f"[Counting 重置訊息發送失敗]: {e}")

            conn.close()
            return  # 🎯 數數頻道的訊息不再繼續往下跑 67 統計、等級、Streaks

    # =================================================================
    # 6️⃣7️⃣ 2. 檢查 "67" 關鍵字與次數統計（修復：排除網址）
    # =================================================================
    cleaned = re.sub(r'<@!?\d+>|<@&\d+>|<#\d+>|<a?:[a-zA-Z0-9_]+:\d+>|<t:\d+(?::[a-zA-Z])?>', '', message.content)
    cleaned = re.sub(r'https?://\S+', '', cleaned)  # 🎯 核心修正：利用正規表達式將所有 http/https 網址抹除，防範網址內含 67 造成誤判
    
    occurrences = cleaned.count("67") + cleaned.count("6️⃣ 7️⃣")
    if occurrences > 0 and is_autoreply_enabled(message.guild.id):
        await message.reply(f"# {message.author.mention} 67!!!!!")

    # =================================================================
    # 📈 3. 經驗值更新、升等檢查、身分組與通知發放
    # =================================================================
    if is_feature_enabled(gid, "level"):
        cursor.execute("SELECT xp, level, count_67 FROM levels WHERE guild_id = ? AND user_id = ?", (gid, uid))
        row = cursor.fetchone()
        xp, lvl, count_67 = row if row else (0, 1, 0)

        if occurrences > 0:
            count_67 += occurrences
            xp_gained = (occurrences * 20) + random.randint(15, 25)
        else:
            xp_gained = random.randint(15, 25)
            
        new_xp = xp + xp_gained
        new_lvl = lvl
        
        is_admin = message.author.guild_permissions.administrator if isinstance(message.author, discord.Member) else False
        admin_xp = get_admin_xp_per_level(gid)
        
        while new_xp >= get_xp_needed(new_lvl, is_admin, admin_xp):
            new_xp -= get_xp_needed(new_lvl, is_admin, admin_xp)
            new_lvl += 1

        cursor.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level, count_67) VALUES (?, ?, ?, ?, ?)", (gid, uid, new_xp, new_lvl, count_67))
        conn.commit()

        if new_lvl > lvl:
            await check_level_roles(message.author, new_lvl, from_level=lvl)
                
            cursor.execute("SELECT channel_id, message, reply_mode FROM levelup WHERE guild_id = ?", (gid,))
            lvl_row = cursor.fetchone()
            if lvl_row:
                cid, msg_template, reply_mode = lvl_row
                try:
                    announce_msg = parse_placeholders(msg_template, message.author, message.guild, extra={"level": new_lvl})
                    if reply_mode:
                        await message.reply(announce_msg)
                    elif cid:
                        channel = message.guild.get_channel(int(cid)) or await message.fetch_channel(int(cid))
                        if channel:
                            await channel.send(announce_msg)
                except Exception as e:
                    logger.error(f"[發送升等訊息失敗]: {e}")

    # =================================================================
    # 🔥 4. Streaks 系統：連續發言天數統計（每個伺服器獨立）
    # =================================================================
    if is_feature_enabled(gid, "streaks"):
        cursor.execute("SELECT messages_needed, notify_channel_id FROM streaks_settings WHERE guild_id = ?", (gid,))
        s_row = cursor.fetchone()
        s_msgneeded, s_channel_id = s_row if s_row else (20, None)

        s_tz = datetime.timezone(datetime.timedelta(hours=0))
        s_now = datetime.datetime.now(s_tz)
        today_str = s_now.strftime("%Y-%m-%d")
        yesterday_str = (s_now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        cursor.execute("SELECT current_streak, longest_streak, messages_today, last_message_date, last_streak_date FROM streaks_data WHERE guild_id = ? AND user_id = ?", (gid, uid))
        d_row = cursor.fetchone()
        cur_streak, longest_streak, msgs_today, last_msg_date, last_streak_date = d_row if d_row else (0, 0, 0, '', '')

        if last_msg_date != today_str:
            if last_streak_date != yesterday_str and last_streak_date != today_str and cur_streak > 0:
                # 🎯 連擊斷了：收回身分組、還原暱稱
                await revoke_streak_roles(message.author, cur_streak)
                await revert_streak_nickname(message.author)
                cur_streak = 0
            msgs_today = 0
            last_msg_date = today_str

        msgs_today += 1

        if msgs_today == s_msgneeded and last_streak_date != today_str:
            cur_streak += 1
            last_streak_date = today_str
            longest_streak = max(longest_streak, cur_streak)

            await check_streak_roles(message.author, cur_streak)
            await apply_streak_nickname(message.author, cur_streak)

            week_start = get_week_start(s_now)
            weekday_idx = s_now.weekday()
            cursor.execute("SELECT day_status FROM streaks_weekly WHERE guild_id = ? AND user_id = ? AND week_start = ?", (gid, uid, week_start))
            wrow = cursor.fetchone()
            status = list(wrow[0]) if wrow else list("0000000")
            status[weekday_idx] = "1"
            cursor.execute("INSERT OR REPLACE INTO streaks_weekly (guild_id, user_id, week_start, day_status) VALUES (?, ?, ?, ?)", (gid, uid, week_start, "".join(status)))

            if s_channel_id:
                try:
                    notify_channel = message.guild.get_channel(int(s_channel_id)) or await message.guild.fetch_channel(int(s_channel_id))
                    if notify_channel:
                        await notify_channel.send(f"{message.author.mention}, Streak up! 🔥{cur_streak}")
                except Exception as e:
                    logger.error(f"[Streak 通知發送失敗]: {e}")

        cursor.execute(
            "INSERT OR REPLACE INTO streaks_data (guild_id, user_id, current_streak, longest_streak, messages_today, last_message_date, last_streak_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, uid, cur_streak, longest_streak, msgs_today, last_msg_date, last_streak_date)
        )
        conn.commit()

    conn.close()


@bot.event
async def on_automod_action(execution: discord.AutoModAction):
    # 🎯 一次觸發如果同時有 block_message + timeout 兩個動作，Discord 會各發一次事件，
    # 只在 timeout 這一次私訊，避免使用者收到兩則重複通知
    if execution.action.type != discord.AutoModRuleActionType.timeout:
        return

    member = execution.member
    if not member:
        return

    duration = execution.action.duration
    dur_text = f"{int(duration.total_seconds() // 60)} minutes" if duration else "some time"

    try:
        await member.send(
            f"⚠️ **Auto Mute**\n"
            f"U sent a blocked word in **{execution.guild.name}**\n"
            f"And u have been **Timeout** for **{dur_text}** by system.\n"
            f"Matched keyword: `{execution.matched_keyword}`"
        )
    except discord.Forbidden:
        pass  # 對方關閉私訊，跳過

class StreaksBoardView(ui.View):
    def __init__(self, target: discord.User, guild: discord.Guild):
        super().__init__(timeout=None)
        self.target = target
        self.guild = guild
        self.mode = "personal"

    def build_personal_embed(self) -> discord.Embed:
        gid = str(self.guild.id)
        uid = str(self.target.id)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT current_streak, longest_streak, messages_today, last_message_date FROM streaks_data WHERE guild_id = ? AND user_id = ?", (gid, uid))
        row = cursor.fetchone()

        cursor.execute("SELECT messages_needed FROM streaks_settings WHERE guild_id = ?", (gid,))
        srow = cursor.fetchone()
        msgneeded = srow[0] if srow else 20

        s_tz = datetime.timezone(datetime.timedelta(hours=0))
        s_now = datetime.datetime.now(s_tz)
        today_str = s_now.strftime("%Y-%m-%d")
        week_start = get_week_start(s_now)

        cursor.execute("SELECT day_status FROM streaks_weekly WHERE guild_id = ? AND user_id = ? AND week_start = ?", (gid, uid, week_start))
        wrow = cursor.fetchone()
        day_status = wrow[0] if wrow else "0000000"
        conn.close()

        if row:
            cur_streak, longest, msgs_today, last_msg_date = row
            today_count = msgs_today if last_msg_date == today_str else 0
        else:
            cur_streak, longest, today_count = 0, 0, 0

        progress_status = "✅ Achieved!" if today_count >= msgneeded else "Not up to standard"

        embed = discord.Embed(
            title=f"🔥 {self.target.name}'s Streaks",
            color=0xff6600,
            description=(
                f"Today's progress: **{today_count}/{msgneeded}** → **{progress_status}**\n"
                f"Now streaks: **{cur_streak}** days\n"
                f"Top streaks: **{longest}** days\n\n"
                f"This week:\n{build_week_line(day_status)}"
            )
        )
        embed.set_footer(text=f"{self.guild.name}｜67")
        return embed

    def build_leaderboard_embed(self) -> discord.Embed:
        gid = str(self.guild.id)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT user_id, current_streak, longest_streak FROM streaks_data WHERE guild_id = ? ORDER BY current_streak DESC LIMIT 10", (gid,))
        rows = cursor.fetchall(); conn.close()

        desc = ""
        for idx, (uid, cur, longest) in enumerate(rows, 1):
            user = bot.get_user(int(uid))
            name = user.name if user else f"User {uid}"
            desc += f"{idx}. **{name}**: 🔥{cur} (Best: {longest})\n"

        embed = discord.Embed(title=f"🏆 {self.guild.name} Streaks Leaderboard", description=desc or "No data available.", color=0xff6600)
        embed.set_footer(text=f"{self.guild.name}｜67")
        return embed

    @ui.button(label="Leaderboard / Personal 🔄", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction: discord.Interaction, button: ui.Button):
        self.mode = "leaderboard" if self.mode == "personal" else "personal"
        embed = self.build_leaderboard_embed() if self.mode == "leaderboard" else self.build_personal_embed()
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="streaks", description="Check your current message streak or view the leaderboard")
async def streaks(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    view = StreaksBoardView(target, interaction.guild)
    embed = view.build_personal_embed()
    await interaction.response.send_message(embed=embed, view=view)

def get_emoji_manage_guilds(user: discord.abc.User) -> list[discord.Guild]:
    """Bot 有在、使用者有在、且使用者有 Manage Expressions 的伺服器。"""
    out = []
    for guild in bot.guilds:
        member = guild.get_member(user.id)
        if member is None:
            continue
        perms = member.guild_permissions
        can = (
            getattr(perms, "manage_expressions", False)
            or getattr(perms, "manage_emojis_and_stickers", False)
        )
        if can:
            out.append(guild)
    return out


async def _download_emoji_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.read()


async def _add_emoji_to_guild(
    guild: discord.Guild,
    name: str,
    url: str,
    animated: bool,
    added_by: discord.abc.User,
) -> discord.Emoji:
    if any(e.name == name for e in guild.emojis):
        raise ValueError(f"`:{name}:` already exists in **{guild.name}**.")

    limit = guild.emoji_limit
    animated_count = sum(1 for e in guild.emojis if e.animated)
    static_count = sum(1 for e in guild.emojis if not e.animated)
    if animated and animated_count >= limit:
        raise ValueError(f"Animated emoji slots full in **{guild.name}** ({animated_count}/{limit}).")
    if not animated and static_count >= limit:
        raise ValueError(f"Emoji slots full in **{guild.name}** ({static_count}/{limit}).")

    me = guild.me
    bot_perms = me.guild_permissions if me else None
    bot_can = bot_perms and (
        getattr(bot_perms, "manage_expressions", False)
        or getattr(bot_perms, "manage_emojis_and_stickers", False)
    )
    if not bot_can:
        raise PermissionError(f"I need **Manage Expressions** in **{guild.name}**.")

    img = await _download_emoji_bytes(url)
    return await guild.create_custom_emoji(
        name=name,
        image=img,
        reason=f"Added via /enlargeemoji by {added_by}",
    )


class AddEmojiToCurrentButton(ui.Button):
    def __init__(self, name: str, url: str, animated: bool):
        super().__init__(label="Add to this server", style=discord.ButtonStyle.success, emoji="➕")
        self.emoji_name = name
        self.emoji_url = url
        self.animated = animated

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("❌ Guild only.")
        await interaction.response.defer(ephemeral=True)
        try:
            new_emoji = await _add_emoji_to_guild(
                guild, self.emoji_name, self.emoji_url, self.animated, interaction.user
            )
            self.disabled = True
            self.label = "Added"
            try:
                await interaction.message.edit(view=self.view)
            except Exception:
                pass
            await interaction.followup.send(
                f"✅ Added {new_emoji} (`:{new_emoji.name}:`) to **{guild.name}**.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ {e}")


class AddEmojiGuildSelect(ui.Select):
    def __init__(self, name: str, url: str, animated: bool, guilds: list[discord.Guild]):
        options = [
            discord.SelectOption(label=g.name[:100], value=str(g.id), description=f"ID {g.id}"[:100])
            for g in guilds[:25]
        ]
        super().__init__(placeholder="Add to my server…", min_values=1, max_values=1, options=options)
        self.emoji_name = name
        self.emoji_url = url
        self.animated = animated

    async def callback(self, interaction: discord.Interaction):
        gid = int(self.values[0])
        guild = bot.get_guild(gid)
        if not guild:
            return await interaction.response.send_message("❌ Server not found (bot left?).")

        member = guild.get_member(interaction.user.id)
        if not member:
            return await interaction.response.send_message("❌ You're not in that server.")

        perms = member.guild_permissions
        can = (
            getattr(perms, "manage_expressions", False)
            or getattr(perms, "manage_emojis_and_stickers", False)
        )
        if not can:
            return await interaction.response.send_message(
                "❌ You need **Manage Expressions** there.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            new_emoji = await _add_emoji_to_guild(
                guild, self.emoji_name, self.emoji_url, self.animated, interaction.user
            )
            await interaction.followup.send(
                f"✅ Added {new_emoji} (`:{new_emoji.name}:`) to **{guild.name}**.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ {e}")


@bot.tree.command(name="enlargeemoji", description="Enlarge an emoji image in a Container and show its URL")
@app_commands.describe(emoji="Paste the emoji (e.g. <:kyk:1497866917468442665>)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def enlargeemoji(interaction: discord.Interaction, emoji: str):
    emoji = emoji.strip()

    m = re.match(r"<(a?):([a-zA-Z0-9_]+):(\d+)>$", emoji)
    if not m:
        return await interaction.response.send_message(
            "❌ WTH is this? Paste a Discord custom emoji like `<:name:1234567890>`."
        )

    animated_flag, name, eid = m.groups()
    animated = bool(animated_flag)
    ext = "gif" if animated else "png"
    url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=4096&quality=lossless"
    title = f":{name}:"

    view = discord.ui.LayoutView(timeout=180)
    container = discord.ui.Container(
        discord.ui.TextDisplay(f"**{title}**"),
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=url, description=title)
        ),
        discord.ui.TextDisplay(url),
        accent_color=0x5865F2,
    )
    view.add_item(container)

    # Container 下方：有當前服權限 → Add to this server；否則 → Add to my server
    manage_guilds = get_emoji_manage_guilds(interaction.user)
    current = interaction.guild

    can_this_server = False
    if current and isinstance(interaction.user, discord.Member):
        perms = interaction.user.guild_permissions
        can_this_server = (
            getattr(perms, "manage_expressions", False)
            or getattr(perms, "manage_emojis_and_stickers", False)
        )
        if can_this_server and any(e.name == name for e in current.emojis):
            can_this_server = False

    if can_this_server:
        row = discord.ui.ActionRow()
        row.add_item(AddEmojiToCurrentButton(name=name, url=url, animated=animated))
        view.add_item(row)
    else:
        others = [g for g in manage_guilds if not current or g.id != current.id]
        if not others:
            others = list(manage_guilds)
        if others:
            row = discord.ui.ActionRow()
            row.add_item(AddEmojiGuildSelect(name=name, url=url, animated=animated, guilds=others))
            view.add_item(row)

    await interaction.response.send_message(view=view)


@bot.tree.command(name="enlargesticker", description="Enlarge the latest sticker or image from the message above")
async def enlargesticker(interaction: discord.Interaction):
    if not interaction.channel:
        return await interaction.response.send_message("❌ Can't read message history here.")

    await interaction.response.defer()

    def sticker_url(sticker) -> str:
        url = getattr(sticker, "url", None)
        if url:
            return str(url)
        sid = sticker.id
        fmt = getattr(sticker, "format", None)
        fmt_value = getattr(fmt, "value", fmt)
        if fmt_value == 4:
            return f"https://media.discordapp.net/stickers/{sid}.gif?size=4096"
        if fmt_value == 3:
            return f"https://discord.com/stickers/{sid}.json"
        return f"https://media.discordapp.net/stickers/{sid}.png?size=4096"

    def pick_image(msg: discord.Message):
        for att in msg.attachments:
            if (att.content_type and att.content_type.startswith("image/")) or att.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")
            ):
                return att.url, att.filename or "Image"
        for emb in msg.embeds:
            if emb.image and emb.image.url:
                return emb.image.url, "Image"
            if emb.thumbnail and emb.thumbnail.url:
                return emb.thumbnail.url, "Image"
        return None, None

    try:
        sticker_hit = None  # (msg, url, name)
        image_hit = None

        # 往上多看幾則，優先貼圖
        async for raw in interaction.channel.history(limit=15):
            try:
                msg = await interaction.channel.fetch_message(raw.id)
            except (discord.NotFound, discord.HTTPException):
                msg = raw

            if msg.stickers and sticker_hit is None:
                st = msg.stickers[0]
                sticker_hit = (msg, sticker_url(st), getattr(st, "name", None) or "Sticker")
                break  # 找到貼圖就停

            if image_hit is None:
                iu, iname = pick_image(msg)
                if iu:
                    image_hit = (msg, iu, iname)

        chosen = sticker_hit or image_hit
        if not chosen:
            return await interaction.followup.send(
                "❌ No sticker or image found in the last 15 messages."
            )

        msg, url, name = chosen
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                f"**{name}**\nFrom {msg.author.mention} · [Jump to message]({msg.jump_url})"
            ),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=url, description=name)
            ),
            discord.ui.TextDisplay(url),
            accent_color=0x5865F2,
        )
        view.add_item(container)
        await interaction.followup.send(view=view)

    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Bro I don't have permission to read message history in this channel."
        )
    except Exception as e:
        logger.error(f"[/enlargesticker error]: {e}")
        await interaction.followup.send(f"❌ Sry, something went wrong: {e}")

# =================================================================
# 🔑 8. RUN BOT
# =================================================================
bot.run(os.getenv("DISCORD_TOKEN"))
