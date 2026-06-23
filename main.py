import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import datetime
import logging
import os
import random
import re
import sqlite3
from typing import Optional

# =================================================================
# ⚙️ 1. GLOBAL BOT CONFIGURATIONS (全局設定)
# =================================================================
WATCHING_STATUSES = [
    "67",
    "/settings",
    "Six Seven",
    "24/7 Auto Mute"
]

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SixSevenBot")

# =================================================================
# 🗄️ 2. DATABASE INITIALIZATION (資料庫初始化)
# =================================================================
def init_db():
    """初始化 SQLite 所有功能資料表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 歡迎與告別設定表 (包含 4 個自訂欄位)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, 
            w_title TEXT, w_desc TEXT, g_title TEXT, g_desc TEXT
        )
    """)
    # 等級通知設定表
    cursor.execute("CREATE TABLE IF NOT EXISTS levelup (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    # 定時報時排程表
    cursor.execute("CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, message TEXT, channel_id TEXT)")
    # 使用者等級數據表
    cursor.execute("CREATE TABLE IF NOT EXISTS levels (user_id TEXT PRIMARY KEY, chars INTEGER, level INTEGER)")
    # 自動 Mute 敏感詞庫
    cursor.execute("CREATE TABLE IF NOT EXISTS mutes (guild_id TEXT, banned_word TEXT, duration_str TEXT, PRIMARY KEY (guild_id, banned_word))")
    conn.commit()
    conn.close()

init_db()

# =================================================================
# 🔄 3. CORE UTILITIES (核心工具函式與自訂變數解析)
# =================================================================
def parse_placeholders(text: str, member: discord.Member, guild: discord.Guild, inviter: discord.Member = None, extra: dict = None) -> str:
    """變數解析器：將字串中的 {user.name} 等替換為 Discord 真實資料，並支援自訂變數擴充"""
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
    """解析禁言時間格式 (支援 1m, 3m, 1h, 2d 等)"""
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
class SixSevenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.status_index = 0
        self.invites = {}
        self.last_announced_minute = ""

    async def setup_hook(self):
        """啟動後的掛載與同步任務"""
        self.rotate_status.start()
        self.check_time_announcements.start()
        await self.tree.sync()

    @tasks.loop(seconds=5)
    async def rotate_status(self):
        """循環切換機器人狀態"""
        self.status_index = (self.status_index + 1) % len(WATCHING_STATUSES)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=WATCHING_STATUSES[self.status_index]))

    @tasks.loop(seconds=30)
    async def check_time_announcements(self):
        """背景定時報時掃描器"""
        tz = datetime.timezone(datetime.timedelta(hours=8))
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
                channel = self.get_channel(int(cid))
                if channel: await channel.send(msg)

bot = SixSevenBot()

# =================================================================
# 🖥️ 5. INTERACTIVE UI (MODALS & VIEWS - 全英文設定介面)
# =================================================================

class WelcomeGoodbyeModal(ui.Modal, title="Set Welcome Message"):
    """符合截圖 2 設計的歡迎與告別字卡設定視窗"""
    w_title = ui.TextInput(label="Enter Embed Title of Welcome message", placeholder="Hey, welcome to {guild.name}!!!", required=False)
    w_desc = ui.TextInput(label="Enter Embed Description of Welcome message *", placeholder="You are the {member.count} member here!\nInviter: {inviter.name}", required=True, style=discord.TextStyle.long)
    g_title = ui.TextInput(label="Enter Embed Title of Goodbye message", placeholder="{user.name} has leave the server", required=False)
    g_desc = ui.TextInput(label="Enter Embed Description of Goodbye message *", placeholder="Whyyyyyy u leave us?????", required=True, style=discord.TextStyle.long)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO welcome VALUES (?, ?, ?, ?, ?, ?)", 
                       (str(interaction.guild_id), str(interaction.channel_id), self.w_title.value, self.w_desc.value, self.g_title.value, self.g_desc.value))
        conn.commit(); conn.close()

        w_t = parse_placeholders(self.w_title.value or self.w_title.placeholder, interaction.user, interaction.guild)
        w_d = parse_placeholders(self.w_desc.value, interaction.user, interaction.guild)
        
        embed = discord.Embed(title=w_t, description=w_d, color=0x54a7dd)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(content="✅ **Settings Saved!** Preview:", embed=embed, ephemeral=True)

class LevelMessageModal(ui.Modal, title="Set Level Up Message"):
    level_msg = ui.TextInput(label="Enter Level Up Message *", placeholder="Congrats {user.mention}! Level {level}!", required=True, style=discord.TextStyle.long)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id FROM levelup WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone(); cid = row[0] if row else None
        cursor.execute("INSERT OR REPLACE INTO levelup VALUES (?, ?, ?)", (str(interaction.guild_id), cid, self.level_msg.value))
        conn.commit(); conn.close()
        
        preview = parse_placeholders(self.level_msg.value, interaction.user, interaction.guild, extra={"level": "5"})
        await interaction.response.send_message(f"✅ **Message Saved!** Preview: {preview}", ephemeral=True)

class LevelSettingsView(ui.View):
    def __init__(self): super().__init__(timeout=180)
    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select Level Up Channel 📢")
    async def select_level_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        cid = select.values[0].id
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT message FROM levelup WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone(); msg = row[0] if row else "Level up to {level}!"
        cursor.execute("INSERT OR REPLACE INTO levelup VALUES (?, ?, ?)", (str(interaction.guild_id), str(cid), msg))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"✅ Level channel set to {select.values[0].mention}", ephemeral=True)

    @ui.button(label="Modify Level Message", style=discord.ButtonStyle.success)
    async def mod_text(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LevelMessageModal())

class AutoMuteModal(ui.Modal, title="Add Banned Word"):
    word = ui.TextInput(label="Enter Banned Word", required=True)
    time = ui.TextInput(label="Mute Duration (e.g., 10m, 1h)", default="10m", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        if self.word.value == "67": return await interaction.response.send_message("Cannot block '67'!", ephemeral=True)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO mutes VALUES (?, ?, ?)", (str(interaction.guild_id), self.word.value, self.time.value))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"🔒 Banned word `{self.word.value}` added.", ephemeral=True)

class AnnouncementModal(ui.Modal, title="Add Time Message"):
    t_time = ui.TextInput(label="Time (HH:MM)", placeholder="08:00", max_length=5, required=True)
    msg = ui.TextInput(label="Message Content", style=discord.TextStyle.paragraph, required=True)
    async def on_submit(self, interaction: discord.Interaction):
        if ":" not in self.t_time.value: return await interaction.response.send_message("Invalid time!", ephemeral=True)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)", (self.t_time.value, self.msg.value, str(interaction.channel_id)))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"⏰ Announcement set at {self.t_time.value}", ephemeral=True)

class SettingsView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="Welcome/Goodbye Panel", style=discord.ButtonStyle.secondary, emoji="👋")
    async def btn_w(self, interaction: discord.Interaction, btn: ui.Button):
        await interaction.response.send_modal(WelcomeGoodbyeModal())
    @ui.button(label="Level System", style=discord.ButtonStyle.secondary, emoji="🎉")
    async def btn_l(self, interaction: discord.Interaction, btn: ui.Button):
        await interaction.response.send_message("📈 **Level System Configuration**", view=LevelSettingsView(), ephemeral=True)
    @ui.button(label="Auto Mute", style=discord.ButtonStyle.secondary, emoji="🔒")
    async def btn_a(self, interaction: discord.Interaction, btn: ui.Button):
        await interaction.response.send_modal(AutoMuteModal())
    @ui.button(label="Time Message", style=discord.ButtonStyle.secondary, emoji="⏰")
    async def btn_t(self, interaction: discord.Interaction, btn: ui.Button):
        await interaction.response.send_modal(AnnouncementModal())

# =================================================================
# 🚀 6. SLASH COMMANDS (全 9 個指令 - 嚴格按照規定順序與參數命名)
# =================================================================

# --- 1. /help ---
@bot.tree.command(name="help", description="Show help menu")
async def help_cmd(interaction: discord.Interaction):
    """幫助選單指令"""
    embed = discord.Embed(title="Bot Help Menu", color=discord.Color.gold())
    embed.add_field(name="Admin Commands", value="`/settings`, `/mute`, `/unmute`, `/kick`, `/setlevel`, `/manualmsg`")
    embed.add_field(name="User Commands", value="`/level`, `/random67`, `/help`")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 2. /settings ---
@bot.tree.command(name="settings", description="Open bot configuration hub")
@app_commands.checks.has_permissions(manage_guild=True)
async def settings(interaction: discord.Interaction):
    """主控面板指令 - 渲染截圖 1 的黃條 Embed 樣式"""
    embed = discord.Embed(
        title="Settings", 
        color=0xdfe600, 
        description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message"
    )
    await interaction.response.send_message(embed=embed, view=SettingsView())

# --- 3. /manualmsg ---
@bot.tree.command(name="manualmsg", description="Send manual text message as bot")
async def manualmsg(interaction: discord.Interaction, text: str):
    """手動發送訊息指令"""
    await interaction.channel.send(text)
    await interaction.response.send_message("✅ Message sent.", ephemeral=True)

# --- 4. /mute ---
@bot.tree.command(name="mute", description="Timeout a server member")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, time: str, reason: Optional[str] = "None"):
    """手動禁言指令 - 渲染截圖 4 上方的綠條 Embed 樣式"""
    delta, err = parse_mute_duration(time)
    if err: return await interaction.response.send_message(err, ephemeral=True)
    await user.timeout(delta, reason=reason)
    
    embed = discord.Embed(
        title=parse_placeholders("✅ {user.name} has been muted.", user, interaction.guild), 
        color=0x2ecc71,
        description=parse_placeholders("Time: {mute time}\nReason: {reason}", user, interaction.guild, extra={"mute time": time, "reason": reason})
    )
    embed.set_footer(text=parse_placeholders("{server.name} | 67", user, interaction.guild))
    await interaction.response.send_message(embed=embed)

# --- 5. /unmute ---
@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    """手動解除禁言指令 - 渲染截圖 4 下方的綠條 Embed 樣式"""
    await user.timeout(None)
    
    embed = discord.Embed(
        title=parse_placeholders("✅ {user.name} has been unmuted.", user, interaction.guild), 
        color=0x2ecc71
    )
    embed.set_footer(text=parse_placeholders("{server.name} | 67", user, interaction.guild))
    await interaction.response.send_message(embed=embed)

# --- 6. /kick ---
@bot.tree.command(name="kick", description="Kick a member from server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = "None"):
    """手動踢出指令 - 同步採用內嵌 Embed 高級排版樣式"""
    await user.kick(reason=reason)
    
    embed = discord.Embed(
        title=parse_placeholders("✅ {user.name} has been kicked.", user, interaction.guild), 
        color=0xe74c3c,
        description=parse_placeholders("Reason: {reason}", user, interaction.guild, extra={"reason": reason})
    )
    embed.set_footer(text=parse_placeholders("{server.name} | 67", user, interaction.guild))
    await interaction.response.send_message(embed=embed)

# --- 7. /setlevel ---
@bot.tree.command(name="setlevel", description="Manually set a member's level")
@app_commands.checks.has_permissions(administrator=True)
async def setlevel(interaction: discord.Interaction, user: discord.Member, level: int):
    """強制設定等級指令"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO levels (user_id, chars, level) VALUES (?, ?, ?)", (str(user.id), (level-1)*150, level))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Set {user.name} to Level {level}", ephemeral=True)

# --- 8. /level ---
@bot.tree.command(name="level", description="Check current activity stats")
async def level(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    """查詢等級指令"""
    target = user or interaction.user
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (str(target.id),))
    row = cursor.fetchone(); conn.close()
    chars, lvl = row if row else (0, 1)
    embed = discord.Embed(title=f"Activity for {target.name}", color=0x2ecc71)
    embed.add_field(name="Level", value=f"Lv. {lvl}")
    embed.add_field(name="Words", value=f"{chars}")
    await interaction.response.send_message(embed=embed)

# --- 9. /random67 ---
@bot.tree.command(name="random67", description="Get lucky 67 message")
async def random67(interaction: discord.Interaction):
    """娛樂隨機 67 指令"""
    jokes = ["67 is magic!", "Luck factor: 67", "Spirit of Six Seven!"]
    await interaction.response.send_message(random.choice(jokes))

# =================================================================
# ⚡ 7. SYSTEM EVENTS (核心監聽事件與安全過濾機制)
# =================================================================

@bot.event
async def on_ready():
    """啟動成功事件"""
    print(f"Logged in as {bot.user}")
    for guild in bot.guilds:
        try: bot.invites[guild.id] = await guild.invites()
        except: pass

@bot.event
async def on_member_join(member: discord.Member):
    """成員加入事件"""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT channel_id, w_title, w_desc FROM welcome WHERE guild_id = ?", (str(member.guild.id),))
    row = cursor.fetchone(); conn.close()
    if row and row[0]:
        channel = bot.get_channel(int(row[0]))
        if channel:
            title = parse_placeholders(row[1] or "Welcome!", member, member.guild)
            desc = parse_placeholders(row[2], member, member.guild)
            embed = discord.Embed(title=title, description=desc, color=0x54a7dd)
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(content=member.mention, embed=embed)

@bot.event
async def on_message(message: discord.Message):
    """訊息過濾與經驗值計算事件"""
    if message.author.bot or not message.guild: return

    # 🎰 67 大標題偵測 (自動過濾掉 ID 標籤防止誤觸)
    cleaned = re.sub(r'<@&?\d+>|<#\d+>|<@!\d+>', '', message.content)
    if "67" in cleaned or "6️⃣7️⃣" in cleaned:
        await message.reply("# 67!!!!!")

    # 自動 Mute 敏感詞庫執行 - 渲染截圖 3 的紅色 "HAHAHA 😂" 警告條樣式
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT banned_word, duration_str FROM mutes WHERE guild_id = ?", (str(message.guild.id),))
    banned_list = cursor.fetchall()
    for word, dur in banned_list:
        if word in message.content:
            try:
                await message.delete()
                delta, _ = parse_mute_duration(dur)
                await message.author.timeout(delta or datetime.timedelta(minutes=10), reason="Auto Mute Triggered")
                
                embed = discord.Embed(
                    title="HAHAHA 😂", 
                    color=0xff0000, 
                    description=parse_placeholders(
                        "{user.name} has been muted for {mute time} due to he/she sent the message \"{message}\", you can try and be the next!", 
                        message.author, message.guild, extra={"mute time": dur, "message": message.content}
                    )
                )
                embed.set_footer(text=parse_placeholders("{server.name} | 67", message.author, message.guild))
                await message.channel.send(embed=embed)
                return
            except: pass

    # 經驗值結算邏輯
    uid = str(message.author.id)
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (uid,))
    row = cursor.fetchone()
    chars, lvl = row if row else (0, 1)
    chars += len(message.content)
    new_lvl = (chars // 150) + 1
    cursor.execute("INSERT OR REPLACE INTO levels VALUES (?, ?, ?)", (uid, chars, new_lvl))
    conn.commit()

    if new_lvl > lvl:
        cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (str(message.guild.id),))
        lrow = cursor.fetchone()
        if lrow and lrow[0]:
            chan = bot.get_channel(int(lrow[0]))
            if chan:
                txt = parse_placeholders(lrow[1], message.author, message.guild, extra={"level": new_lvl})
                await chan.send(txt)
    conn.close()

# =================================================================
# 🔑 8. RUN BOT (啟動端)
# =================================================================
bot.run(os.getenv("DISCORD_TOKEN"))
