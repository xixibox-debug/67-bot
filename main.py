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
ai_cooldowns = {}

# 🎯 語音時數追蹤：(guild_id, user_id) -> 進入監聽頻道的時間戳
voice_sessions = {}
MAX_VOICE_WATCH = 20  # 全機器人同時間最多監聽 5 個語音頻道
voice_keepalive_tasks = {}  # 🎯 guild_id -> asyncio.Task，避免語音連線因為完全沒有音訊流量被 Discord 判定閒置斷線


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
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, 
            w_title TEXT, w_desc TEXT, g_title TEXT, g_desc TEXT
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS levelup (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    ensure_column(cursor, "levelup", "reply_mode", "INTEGER DEFAULT 0")  # 🎯 0=發到頻道, 1=在該訊息下回覆
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
    # 💰 付費解鎖清單（例如「67+Slient」解除自動回覆 67 的限制），只能靠開發者手動新增
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paid_features (
            guild_id TEXT,
            feature TEXT,
            PRIMARY KEY (guild_id, feature)
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
    conn.commit()
    conn.close()

init_db()

# =================================================================
# 🔄 3. CORE UTILITIES (核心工具函式與變數解析)
# =================================================================
def get_xp_needed(level: int, is_admin: bool = False) -> int:
    if is_admin:
        return 150  # 管理員專屬：每一等都固定只要 150 XP
    return 5 * (level ** 2) + 50 * level + 100

async def check_level_roles(member: discord.Member, level: int):
    """檢查並發放該等級對應的身分組獎勵"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT role_id FROM level_roles WHERE guild_id = ? AND level = ?", (str(member.guild.id), level))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        role = member.guild.get_role(int(row[0]))
        if role and role not in member.roles:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                logger.error(f"nah, I can't give **{role.name}** to **{member.name}**")


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


def is_autoreply_enabled(guild_id) -> bool:
    """67 自動回覆，跟其他功能相反，預設是開啟的"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT enabled FROM feature_toggles WHERE guild_id = ? AND feature = ?", (str(guild_id), "autoreply67"))
    row = cursor.fetchone(); conn.close()
    return (row[0] == 1) if row else True  # 🎯 預設開啟


def is_paid_guild(guild_id, feature: str) -> bool:
    """檢查這個伺服器是不是已經手動被加進某個付費功能的白名單"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM paid_features WHERE guild_id = ? AND feature = ?", (str(guild_id), feature))
    row = cursor.fetchone(); conn.close()
    return row is not None

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
    """把暱稱結尾的「 [符號]天數」格式去掉，回傳乾淨的原始名稱"""
    pattern = r"\s*\[" + re.escape(emoji) + r"\]\d+$"
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

    # Discord 暱稱上限 32 字，超過的話從原本名稱那段截短，確保後面的 [符號]天數 一定完整保留
    if len(new_nick) > 32:
        overflow = len(new_nick) - 32
        base_name = base_name[:max(0, len(base_name) - overflow)]
        new_nick = f"{base_name} [{emoji}]{cur_streak}"

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
    match = re.match(r"^(\d+)([mhd])$", duration_str.strip().lower())
    if not match: return None, "Invalid format! Use 1m, 3m, 1h, or 2d."
    amount, unit = int(match.group(1)), match.group(2)
    if unit == 'm': delta = datetime.timedelta(minutes=amount)
    elif unit == 'h': delta = datetime.timedelta(hours=amount)
    else: delta = datetime.timedelta(days=amount)
    if delta > datetime.timedelta(days=14): return None, "Max duration is 14 days."
    return delta, None

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
        super().__init__(timeout=300)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "welcome") if guild_id else False
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if not enabled:
            # 🎯 關閉時只留 Back + Toggle 兩顆
            self.remove_item(self.set_channel)
            self.remove_item(self.edit_msg)
            self.remove_item(self.reset_panel)
        elif guild_id:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT channel_id FROM welcome WHERE guild_id = ?", (str(guild_id),))
            row = cursor.fetchone()
            conn.close()
            if row and row[0] and str(row[0]).isdigit():
                self.set_channel.default_values = [discord.Object(id=int(row[0]))]

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
            embed.description = (
                f"Status: **on**\n"
                f"Notification channel: {ch_text}\n\n"
                f"**Welcome Embed title:** {w_t or 'Not set'}\n"
                f"**Welcome Embed content:**\n{w_d or 'Not set'}\n\n"
                f"**Goodbye Embed title:** {g_t or 'Not set'}\n"
                f"**Goodbye Embed content:**\n{g_d or 'Not set'}"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nAuto Mute\nTime Message")
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


class LevelSettingsView(ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=180)
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
                f"**Level up message:**\n{msg}"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nAuto Mute\nTime Message")
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
        super().__init__(timeout=60)
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
        self.view.update_select_menu()
        await interaction.response.edit_message(embed=self.view.build_embed(interaction.guild), view=self.view)
        await interaction.followup.send(f"🔒 Auto mute `{self.word.value}` added.", ephemeral=True)


class BannedWordDeleteSelect(ui.Select):
    def __init__(self): super().__init__(placeholder="🗑️ Select a word to CANCEL / REMOVE rule", min_values=1, max_values=1, options=[discord.SelectOption(label="Placeholder", value="none")], row=1)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return await interaction.response.defer()
        word = self.values[0]
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM mutes WHERE guild_id = ? AND banned_word = ?", (str(interaction.guild_id), word))
        conn.commit(); conn.close()
        self.view.update_select_menu()
        await interaction.response.edit_message(embed=self.view.build_embed(interaction.guild), view=self.view)
        await interaction.followup.send(f"✅ Removed Auto mute for: `{word}`", ephemeral=True)


class AutoMuteConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "automute")
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if enabled:
            self.select_menu = BannedWordDeleteSelect()
            self.add_item(self.select_menu)
            self.update_select_menu()
        else:
            self.remove_item(self.add_word)

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
            embed.description = f"Status: **on**\n\n**Banned word:**\n{words_text}"
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nAuto Mute\nTime Message")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(self.guild_id, "automute")
        set_feature_enabled(self.guild_id, "automute", not cur)
        new_view = AutoMuteConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="➕ Add Banned Word", style=discord.ButtonStyle.success, row=0)
    async def add_word(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(AutoMuteModal(self))

class AnnouncementModal(ui.Modal, title="Add Time Message"):
    t_time = ui.TextInput(label="Time (HH:MM)", placeholder="08:00", max_length=5, required=True)
    msg = ui.TextInput(label="Message Content", style=discord.TextStyle.paragraph, required=True)
    def __init__(self, view: 'TimeMessageConfigView'):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        if ":" not in self.t_time.value: return await interaction.response.send_message("Invalid time format!", ephemeral=True)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)", (self.t_time.value, self.msg.value, str(interaction.channel_id)))
        conn.commit(); conn.close()
        self.view.update_select_menu()
        await interaction.response.edit_message(embed=self.view.build_embed(interaction.guild), view=self.view)
        await interaction.followup.send(f"⏰ Auto message time set at {self.t_time.value}", ephemeral=True)


class TimeMessageDeleteSelect(ui.Select):
    def __init__(self): super().__init__(placeholder="🗑️ Select an announcement schedule to CANCEL", min_values=1, max_values=1, options=[discord.SelectOption(label="Placeholder", value="none")], row=1)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return await interaction.response.defer()
        rid = self.values[0]
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM announcements WHERE id = ?", (rid,))
        conn.commit(); conn.close()
        self.view.update_select_menu()
        await interaction.response.edit_message(embed=self.view.build_embed(interaction.guild), view=self.view)
        await interaction.followup.send("✅ This Auto message has been cancelled.", ephemeral=True)


class TimeMessageConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        enabled = is_feature_enabled(guild_id, "timemsg")
        self.toggle_enabled.label = "✅ Status: On" if enabled else "❌ Status: Off"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger

        if enabled:
            self.select_menu = TimeMessageDeleteSelect()
            self.add_item(self.select_menu)
            self.update_select_menu()
        else:
            self.remove_item(self.add_time)

    def update_select_menu(self):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT id, time, message, channel_id FROM announcements")
        all_rows = cursor.fetchall(); conn.close()
        valid_options = []
        for rid, t_time, msg, cid in all_rows:
            channel = bot.get_channel(int(cid))
            if channel and channel.guild.id == self.guild_id:
                short_msg = msg[:20] + "..." if len(msg) > 20 else msg
                valid_options.append(discord.SelectOption(label=f"[{t_time}] {short_msg}", value=str(rid)))
        if valid_options:
            self.select_menu.options = valid_options[:25]
            self.select_menu.disabled = False
        else:
            self.select_menu.options = [discord.SelectOption(label="No scheduled announcements", value="none")]
            self.select_menu.disabled = True

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = is_feature_enabled(self.guild_id, "timemsg")
        embed = discord.Embed(title="⏰ Auto Time Message Settings", color=0x3498db if enabled else 0x2b2d31)
        if not enabled:
            embed.description = "Status: **off**"
        else:
            conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
            cursor.execute("SELECT time, message, channel_id FROM announcements")
            all_rows = cursor.fetchall(); conn.close()
            lines = []
            for t_time, msg, cid in all_rows:
                channel = bot.get_channel(int(cid))
                if channel and channel.guild.id == self.guild_id:
                    short_msg = msg[:30] + "..." if len(msg) > 30 else msg
                    lines.append(f"{t_time} GMT {short_msg}")
            sched_text = "\n".join(lines) if lines else "Not set"
            embed.description = f"Status: **on**\n\n**Now schedule:**\n{sched_text}"
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nAuto Mute\nTime Message")
        embed.set_footer(text=f"{interaction.guild.name}｜67")
        await interaction.response.edit_message(embed=embed, view=SettingsView(interaction.guild_id))

    @ui.button(label="❌ Status: Off", style=discord.ButtonStyle.danger, row=0)
    async def toggle_enabled(self, interaction: discord.Interaction, button: ui.Button):
        cur = is_feature_enabled(self.guild_id, "timemsg")
        set_feature_enabled(self.guild_id, "timemsg", not cur)
        new_view = TimeMessageConfigView(interaction.guild_id)
        await interaction.response.edit_message(embed=new_view.build_embed(interaction.guild), view=new_view)

    @ui.button(label="⏰ Add Time Message", style=discord.ButtonStyle.success, row=0)
    async def add_time(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(AnnouncementModal(self))

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
        super().__init__(timeout=120)
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
        super().__init__(timeout=120)
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
        super().__init__(timeout=180)
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
                f"Show emoji {nick_emoji} if streaks more than **{nick_threshold}** days\n"
                f"Notification channel: {ch_text}"
            )
        embed.set_footer(text=f"{guild.name}｜67")
        return embed

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.gray, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nStreaks\nAuto Mute\nTime Message")
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
        view = AutoMuteConfigView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
    @ui.button(label="Time Message", style=discord.ButtonStyle.secondary, emoji="⏰")
    async def btn_t(self, interaction: discord.Interaction, btn: ui.Button):
        view = TimeMessageConfigView(interaction.guild_id)
        embed = view.build_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="✅ Auto Reply: On", style=discord.ButtonStyle.success, emoji="🔁")
    async def btn_autoreply(self, interaction: discord.Interaction, btn: ui.Button):
        currently_on = is_autoreply_enabled(interaction.guild_id)

        if currently_on:
            # 🎯 要關閉之前，先檢查這個伺服器有沒有在付費白名單裡
            if not is_paid_guild(interaction.guild_id, "67silent"):
                return await interaction.response.send_message("Seems u haven't buy 67+Slient", ephemeral=True)
            set_feature_enabled(interaction.guild_id, "autoreply67", False)
        else:
            set_feature_enabled(interaction.guild_id, "autoreply67", True)

        new_state = is_autoreply_enabled(interaction.guild_id)
        btn.label = "✅ Auto Reply: On" if new_state else "❌ Auto Reply: Off"
        btn.style = discord.ButtonStyle.success if new_state else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

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

@bot.tree.command(name="settings", description="Open bot configuration hub")
@app_commands.checks.has_permissions(manage_guild=True)
async def settings(interaction: discord.Interaction):
    embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message")
    embed.set_footer(text=f"{interaction.guild.name}｜67")
    await interaction.response.send_message(embed=embed, view=SettingsView(interaction.guild_id))

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
    await check_level_roles(user, level)
    
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
        super().__init__(timeout=60)
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
            xp_needed = get_xp_needed(lvl, is_admin)

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
    xp_needed = get_xp_needed(lvl, is_admin)
    
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
# =================================================================
# 💰 ECONOMY SYSTEM COMMANDS & VIEWS (對應圖 {696E6907-11CB-4468-B5BB-9C2678E2F7F2}.png)
# =================================================================

class EcoBalanceView(ui.View):
    def __init__(self, target: discord.User, guild: discord.Guild):
        super().__init__(timeout=60)
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

@bot.tree.command(name="addrole", description="Manually add a role to a user")
@app_commands.checks.has_permissions(administrator=True)
async def addrole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if role in user.roles:
        return await interaction.response.send_message(f"❌ {user.mention} already owned {role.name}.", ephemeral=True)
    try:
        await user.add_roles(role)
        await interaction.response.send_message(f"✅ Give role {role.mention} to {user.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Called the moderator to give me a higher permission.", ephemeral=True)

@bot.tree.command(name="removerole", description="Manually remove a role from a user")
@app_commands.checks.has_permissions(administrator=True)
async def removerole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if role not in user.roles:
        return await interaction.response.send_message(f"❌ {user.mention} don't have **{role.name}**. ", ephemeral=True)
    try:
        await user.remove_roles(role)
        await interaction.response.send_message(f"✅ Remove {user.mention}'s **{role.mention} **.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Called the moderator to give me a higher permission.", ephemeral=True)

@bot.tree.command(name="setstreaks", description="Flooding or reducing the number of Streaks days.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="Users who are subject to spam or have their Streaks days reduced.", streaks="Set Streaks days. (>=0)")
async def setstreaks(interaction: discord.Interaction, user: discord.Member, streaks: int):
    if streaks < 0:
        return await interaction.response.send_message("❌ Ur math teather is crying.", ephemeral=True)
    
    gid = str(interaction.guild_id)
    uid = str(user.id)
    
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
            SET current_streak = ?, longest_streak = ? 
            WHERE guild_id = ? AND user_id = ?
        """, (streaks, new_longest, gid, uid))
    else:
        # 若無歷史資料，則直接新增一筆
        cursor.execute("""
            INSERT INTO streaks_data (guild_id, user_id, current_streak, longest_streak) 
            VALUES (?, ?, ?, ?)
        """, (gid, uid, streaks, streaks))
        
    conn.commit()
    conn.close()
    
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

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for guild in bot.guilds:
        try: bot.invites[guild.id] = await guild.invites()
        except: pass

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
    except Exception as e: logger.error(f"[on_member_join 崩潰]: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot: return
    if not is_feature_enabled(member.guild.id, "welcome"): return
    try:
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(member.guild.id),))
        row = cursor.fetchone(); conn.close()
        if row and row[0]:
            try:
                channel_id = int(row[0])
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            except: return
            if channel:
                title = parse_placeholders(row[1] or "Goodbye!", member, member.guild)
                desc = parse_placeholders(row[2], member, member.guild)
                embed_color = discord.Color(0xe74c3c)
                embed = discord.Embed(title=title, description=desc, color=embed_color)
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"{member.guild.name}｜67")
                await channel.send(embed=embed)
    except Exception as e: logger.error(f"[on_member_remove 崩潰]: {e}")



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


@bot.event  # 🎯 已將 @client.event 修改為 @bot.event
async def on_message_delete(message):
    """當使用者刪除（收回）訊息時，檢查是否有正在執行的 AI 任務，有則立即強制取消"""
    if 'active_ai_tasks' in globals() and message.id in globals()['active_ai_tasks']:
        task, user_id = globals()['active_ai_tasks'][message.id]
        if not task.done():
            task.cancel()
            logger.info(f"⚡ 已成功發送取消訊號至訊息 ID {message.id} 的 AI 任務。")

@bot.event
async def on_message(message: discord.Message):
    # 排除機器人自己的訊息
    if message.author.bot:
        return

    # 🎯 標記監聽器（Groq API 完美非同步版，支援單純標記與回覆標記）
    # 這段刻意放在「伺服器限定」判斷之前，讓私訊（個人安裝情境）也能 @ 機器人問問題
    if bot.user.mentioned_in(message) and not message.mention_everyone:
        # 🧹 拔除訊息中的機器人標籤與前後空格
        clean_content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

        # 狀況 A：如果後面「沒有加任何文字」 -> 觸發原本的極度厭世英文回覆
        if not clean_content:
            annoyed_phrases = [
                "Why are you even pinging me? Go away.",
                "Don't @ me for no reason. I'm exhausted.",
                "What do you want now? Stop messing with me.",
                "Pinged me for what? Just let me exist in peace.",
                "Unless the server is literally burning down, don't @ me."
            ]
            await message.reply(random.choice(annoyed_phrases))
            return

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
                # 🧠 核心邏輯：動態爬軌跡，最多回溯 10 則、限時 10 分鐘的連貫回覆
                # -----------------------------------------------------------
                conversation_history = []
                current_ref = message.reference
                history_count = 0
                
                # discord.py 的 message.created_at 是時區感知的 UTC 時間
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                
                logger.info("🔍 開始追溯單獨連貫的回覆鏈...")
                while current_ref and current_ref.message_id and history_count < 10:
                    try:
                        ref_msg = await message.channel.fetch_message(current_ref.message_id)
                        
                        # ⏳ 檢查時間限制：如果該則訊息距離現在超過 10 分鐘(600秒)，則立即斬斷記憶
                        if (now_utc - ref_msg.created_at).total_seconds() > 600:
                            logger.info(f"⏱️ 訊息 {ref_msg.id} 已超過 10 分鐘，停止向上回溯。")
                            break
                        
                        # 解析並清理內容，依照身份貼上標籤
                        if ref_msg.author.id == bot.user.id:
                            role = "assistant"
                            # 拔除舊回應底部的 67 免責聲明，避免干擾 AI
                            content = ref_msg.content.split("\n\n-# **67+AI")[0].split("\n\n-# 67+AI")[0].split("\n\n67+AI")[0].strip()
                        else:
                            role = "user"
                            content = ref_msg.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
                        
                        # 💡 關鍵：使用 insert(0, ...) 確保越古老的訊息排在陣列越前面，符合聊天紀錄順序
                        conversation_history.insert(0, {"role": role, "content": content})
                        history_count += 1
                        
                        # 繼續向上尋找該訊息是否有「更上一層的回覆目標」
                        current_ref = ref_msg.reference
                        
                    except Exception as chain_err:
                        logger.warning(f"⚠️ 無法獲取回覆鏈中某個節點的訊息 (可能被刪除): {chain_err}")
                        break # 連貫中斷，直接跳出
                
                logger.info(f"✨ 成功載入 {history_count} 則連貫上下文記憶！")

                # 🌐 優化 Tavily 搜尋關鍵字：如果有歷史故事，結合「故事起點(最早的提問)」與「最新提問」送去搜尋
                if conversation_history:
                    search_query = f"{conversation_history[0]['content']} {clean_content}"
                else:
                    search_query = clean_content
                
                # 呼叫 Tavily 進行非同步網路搜尋
                search_context = await tavily_search(search_query)

                # 🧱 組合 System、故事歷史與本次提問
                ai_messages = [
                    {
                        "role": "system", 
                        "content": (
                            "You are an AI model in a Discord bot called '67'. You like to say 67 (but don't say it too often) and respond just like Meta AI. "
                            "Drop the corporate PR tone, be direct, slightly witty. "
                            "Use ENGLISH to response. but if the user use chinese, u should use TRADITIONAL CHINESE to response. DONT use Simplified chinese. Max 800 characters.\n\n"
                            f"【請優先參考以下網路即時資訊回答】：\n{search_context}"
                        )
                    }
                ]
                ai_messages.extend(conversation_history)
                ai_messages.append({"role": "user", "content": clean_content})

                ai_reply = None
                
                # ===========================================================
                # 🛡️ ⚔️ 雙陣營火線防禦機制 (Gemini 直連 -> Groq 備援)
                # ===========================================================
                
# ───【第一防線：直連 Google Gemini API 輪詢機制】───
                used_provider = None  # 💡 用於追蹤是哪一個模型成功回應
                
                if os.getenv("GEMINI_API_KEY") and not ai_reply:
                    gemini_models = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash", "gemini-2.5-flash", "gmeini-3.5-flash-lite", "gemini-3.1-flash-lite"]
                    for model_name in gemini_models:
                        try:
                            logger.info(f"🤖 優先請求直連 Gemini API ({model_name})...")
                            gemini_response = await gemini_client.chat.completions.create(
                                model=model_name, 
                                messages=ai_messages,
                                temperature=0.7
                            )
                            ai_reply = gemini_response.choices[0].message.content
                            if ai_reply:
                                logger.info(f"✨ [第一防線] 直連 Gemini ({model_name}) 成功救援故事！")
                                # 💡 根據成功回應的模型決定標記
                                if model_name in ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash", "gemini-2.5-flash"]:
                                    used_provider = "gemini_loop"
                                elif model_name in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]:
                                    used_provider = "gemini_lite"
                                break  # 成功取得回應，跳出 Gemini 輪詢
                        except Exception as gemini_err:
                            logger.warning(f"⚠️ [第一防線] Gemini ({model_name}) 直連失敗: {gemini_err}，準備切換下一順位...")

                # ───【第二防線：Groq API 終極備援】───
                if not ai_reply:
                    try:
                        logger.info("🤖 [2/2] 前方失敗！觸發最終底線，請求 Groq API (llama-3.3-70b-versatile)...")
                        groq_response = await groq_client.chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=ai_messages,
                            max_tokens=600,
                            temperature=0.7
                        )
                        ai_reply = groq_response.choices[0].message.content
                        if ai_reply:
                            logger.info("✨ [第二防線] Groq 終極防線救援成功！")
                            used_provider = "groq"
                    except Exception as groq_err:
                        logger.error(f"❌ [第二防線] Groq 也失敗了: {groq_err}")
                
                # ───【🚨 終極檢查：全線癱瘓防範】───
                if not ai_reply:
                    logger.error("❌ [核心崩潰] Gemini 與 Groq API 管道於本次故事請求中全數癱瘓。")
                    await message.reply("❌ 67+AI suck. Try again later.")
                    return

                # 安全字數截斷（僅針對 Groq 進行截斷，Gemini 回覆不用砍字數）
                if used_provider not in ["gemini_loop", "gemini_lite"] and len(ai_reply) > 700:
                    ai_reply = ai_reply[:697] + "..."
                    
                # 🎯 根據成功的來源追加對應的新版格式浮水印
                if used_provider == "gemini_loop":
                    watermark = "\n\n-# **67+AI (2.7 loop)**｜67+AI suck and frequently makes mistakes; please verify it yourself."
                elif used_provider == "gemini_lite":
                    watermark = "\n\n-# **67+AI (2.5a)**｜67+AI suck and frequently makes mistakes; please verify it yourself."
                elif used_provider == "groq":
                    watermark = "\n\n-# **67+AI (1)**｜67+AI suck and frequently makes mistakes; please verify it yourself."
                else:
                    watermark = "\n\n-# 67+AI suck and frequently makes mistakes; please verify it yourself."

                ai_reply = f"{ai_reply}{watermark}"
                await message.reply(ai_reply)
                return  # 結束事件，不觸發後續 XP 增加系統

        except Exception as e:
            logger.error(f"❌ AI 處理過程發生錯誤: {e}")
            
        finally:
            # 確保無論成功或發生異常，都會將任務從全域追蹤清單中移除
            if 'active_ai_tasks' in globals() and message.id in globals()['active_ai_tasks']:
                globals()['active_ai_tasks'].pop(message.id, None)

    # 🎯 以下都是伺服器限定功能（自動禁言、67 統計、等級、Streaks），私訊沒有 guild，到此為止
    if not message.guild:
        return

# =================================================================
    # 🔒 1. 自動禁言黑名單檢查（修復：刪除前發送通知、被禁言的人看得見時間）
    # =================================================================
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    gid = str(message.guild.id)
    uid = str(message.author.id)

    if is_feature_enabled(gid, "automute"):
        cursor.execute("SELECT banned_word, duration_str FROM mutes WHERE guild_id = ?", (gid,))
        banned_list = cursor.fetchall()
        for word, dur in banned_list:
            if word in message.content:
                try:
                    # 🎯 修正點：在刪除原訊息前，先「私訊」給違規用戶，確保他絕對看得到自己被關多久、為什麼被關
                    try:
                        await message.author.send(
                            f"⚠️ **Auto mute**\n"
                            f"U sent `\"{word}\"` in **{message.guild.name}**\n"
                            f"And u have been **Timeout** for** {dur}** by system.\n"
                            f"ur original message: \n ```{message.content}```"
                        )
                    except discord.Forbidden:
                        pass  # 對方若關閉陌生人私訊則略過，不讓程式崩潰

                    # await message.delete()
                    delta, _ = parse_mute_duration(dur)
                    await message.author.timeout(delta or datetime.timedelta(minutes=10), reason="Auto Mute Triggered")
                    
                    # 🎯 修正點：公開頻道警示也改用 mention 標記，讓他事後看得到
                    embed = discord.Embed(
                        title="HAHAHA 🤣", 
                        color=0xff0000, 
                        description=f'{message.author.mention} has been muted for **{dur}** due to sending a blocked word, you can try and be the next!'
                    )
                    embed.set_footer(text=f"{message.guild.name}｜67")
                    await message.reply(embed=embed, mention_author=False)
                    conn.close()
                    return 
                except Exception as e:
                    logger.error(f"[Auto Mute 錯誤]: {e}")
                    pass

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
        
        while new_xp >= get_xp_needed(new_lvl, is_admin):
            new_xp -= get_xp_needed(new_lvl, is_admin)
            new_lvl += 1

        cursor.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level, count_67) VALUES (?, ?, ?, ?, ?)", (gid, uid, new_xp, new_lvl, count_67))
        conn.commit()

        if new_lvl > lvl:
            for l in range(lvl + 1, new_lvl + 1):
                await check_level_roles(message.author, l)
                
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

class StreaksBoardView(ui.View):
    def __init__(self, target: discord.User, guild: discord.Guild):
        super().__init__(timeout=60)
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
    
# =================================================================
# 🔑 8. RUN BOT
# =================================================================
bot.run(os.getenv("DISCORD_TOKEN"))
