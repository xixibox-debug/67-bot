import discord
from discord import app_commands
from discord.ext import commands, tasks
import random
import datetime
import os
import sqlite3
import io
import logging
from PIL import Image

# =================================================================
# ⚙️ 1. 機器人全域固定設定項目
# =================================================================
# 狀態列完全鎖定，每 5 秒切換一次（已全數鎖定為英文）
WATCHING_STATUSES = [
    "67",
    "/help",    
    "Six Seven",
    "24/7 Auto Mute"
]

# 設定 SQLite 資料庫路徑（支援 Railway 的 data 持久化目錄）
os.makedirs("data", exist_ok=True)
DB_PATH = "data/bot.db"

# 設定基本的 Log 輸出，方便在雲端後台查看錯誤
logging.basicConfig(level=logging.INFO)

# =================================================================
# 🗄️ 2. 資料庫結構初始化 (SQLite3)
# =================================================================
def init_db():
    """初始化所有機器人所需要的資料庫表格，確保欄位完整不遺漏"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 歡迎訊息設定表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome (
            guild_id TEXT PRIMARY KEY, 
            channel_id TEXT, 
            message TEXT
        )
    """)
    
    # 升級通知設定表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS levelup (
            guild_id TEXT PRIMARY KEY, 
            channel_id TEXT, 
            message TEXT
        )
    """)
    
    # 24/7 自動報時排程表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            time TEXT, 
            message TEXT, 
            channel_id TEXT
        )
    """)
    
    # 成員經驗值與等級紀錄表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS levels (
            user_id TEXT PRIMARY KEY, 
            chars INTEGER, 
            level INTEGER
        )
    """)
    
    # 自動 Mute 禁字防護表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            guild_id TEXT, 
            banned_word TEXT, 
            duration_mins INTEGER, 
            PRIMARY KEY (guild_id, banned_word)
        )
    """)
    
    conn.commit()
    conn.close()
    print("✨ [Database] All functional database tables checked and initialized.")

# 執行資料庫建置
init_db()

# =================================================================
# 🤖 3. 機器人主核心類別建構
# =================================================================
class SixSevenBot(commands.Bot):
    def __init__(self):
        # 啟用全功能 Intents，確保看得到成員加入、訊息內容與邀請碼
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.status_index = 0
        self.invites = {} # 用於儲存各伺服器邀請碼狀態的記憶體快取

    async def setup_hook(self):
        """當機器人啟動時，負責掛載背景任務與同步斜線指令"""
        self.rotate_status.start()
        self.check_time_announcements.start()
        await self.tree.sync()
        print("🤖 [System] Bot core and slash command tree synchronized successfully.")

    # --- 🔄 背景任務一：固定狀態每 5 秒自動輪播 ---
    @tasks.loop(seconds=5)
    async def rotate_status(self):
        if not WATCHING_STATUSES:
            return
        
        if self.status_index >= len(WATCHING_STATUSES):
            self.status_index = 0
            
        current_status = WATCHING_STATUSES[self.status_index]
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=current_status
            )
        )
        self.status_index += 1

    @rotate_status.before_loop
    async def before_rotate(self):
        await self.wait_until_ready()

    # --- ⏰ 背景任務二：24/7 報時系統巡邏 (每分鐘檢查一次) ---
    @tasks.loop(seconds=60)
    async def check_time_announcements(self):
        # 取得目前的 UTC 零時區時間，格式為 HH:MM
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message FROM announcements WHERE time = ?", (now_utc,))
        rows = cursor.fetchall()
        conn.close()
        
        for channel_id, message in rows:
            channel = self.get_channel(int(channel_id))
            if channel:
                try:
                    # 報時直接發送純文字內容
                    await channel.send(message)
                except Exception as e:
                    print(f"❌ Announcement delivery failed (Channel ID: {channel_id}): {e}")

    @check_time_announcements.before_loop
    async def before_check_time(self):
        await self.wait_until_ready()

# 實例化機器人對象
bot = SixSevenBot()

# =================================================================
# 📡 4. 邀請碼快取追蹤事件處理
# =================================================================
@bot.event
async def on_ready():
    print(f"🟢 [Online] Bot successfully logged in as {bot.user.name} ({bot.user.id})")
    # 開機時抓取所有伺服器的現有邀請碼列表並存入快取
    for guild in bot.guilds:
        try:
            bot.invites[guild.id] = await guild.invites()
            print(f"📥 [Cache] Cached invite codes for server 【{guild.name}】.")
        except discord.Forbidden:
            print(f"⚠️ [Cache] Missing permissions to read invites for 【{guild.name}】.")
        except Exception as e:
            print(f"⚠️ [Cache] Error reading invites for 【{guild.name}】: {e}")

@bot.event
async def on_guild_join(guild):
    """當機器人被加入新伺服器，自動補抓邀請碼快取"""
    try:
        bot.invites[guild.id] = await guild.invites()
    except:
        pass

@bot.event
async def on_guild_remove(guild):
    """當機器人離開伺服器，清除對應快取避免洩漏記憶體"""
    bot.invites.pop(guild.id, None)

# =================================================================
# 🖥️ 5. 用於指令的 UI 互動視窗 (Modals) 與下拉選單 (Views)
# =================================================================

class ManualMsgModal(discord.ui.Modal, title="Send Plain Text Message"):
    """/manualmsg 專用彈出式視窗：完全不用嵌入、無前綴、純文字代理發言"""
    msg_input = discord.ui.TextInput(
        label="Enter the message content to broadcast", 
        style=discord.TextStyle.paragraph,
        placeholder="Type your text here...",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # 核心修改：直接在當前頻道發送純文字訊息，完全不透過 Embed 包裹
        await interaction.channel.send(content=self.msg_input.value)
        # 悄悄話回覆管理員確認訊息已發送
        await interaction.response.send_message("✅ Successfully sent the message.", ephemeral=True)


class WelcomeModal(discord.ui.Modal, title="Setup Server Welcome Message"):
    """/setwelcome 專用彈出式視窗"""
    def __init__(self, channel: discord.abc.GuildChannel):
        super().__init__()
        self.channel = channel
        
    msg_input = discord.ui.TextInput(
        label="Welcome Message Template (Use variables below)",
        style=discord.TextStyle.paragraph,
        default="You are the {member.count} member here!\nInviter: {inviter.name}",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO welcome (guild_id, channel_id, message) VALUES (?, ?, ?)",
            (str(interaction.guild_id), str(self.channel.id), self.msg_input.value)
        )
        conn.commit()
        conn.close()
        await interaction.response.send_message(
            f"✅ Welcome settings configured successfully! Messages will be sent to {self.channel.mention} with dynamic avatar color theme enabled.", 
            ephemeral=True
        )


class LevelUpModal(discord.ui.Modal, title="Customize Level Up Message"):
    """/setlevelup 專用彈出式視窗"""
    def __init__(self, channel: discord.abc.GuildChannel):
        super().__init__()
        self.channel = channel
        
    msg_input = discord.ui.TextInput(
        label="Notification content (Supports {user.mention} and {user.level})",
        style=discord.TextStyle.paragraph,
        default="🎉 Congratulations {user.mention}, you leveled up to **Lv. {user.level}**!",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO levelup (guild_id, channel_id, message) VALUES (?, ?, ?)",
            (str(interaction.guild_id), str(self.channel.id), self.msg_input.value)
        )
        conn.commit()
        conn.close()
        await interaction.response.send_message(
            f"✅ Level up notifications successfully bound to channel: {self.channel.mention}!", 
            ephemeral=True
        )


class RemoveTimeSelect(discord.ui.Select):
    """/removetime 專用的動態下拉選單組件"""
    def __init__(self, options_list):
        options = []
        for item in options_list:
            db_id, t_time, msg = item
            # 取前20個字當作預覽
            short_msg = msg if len(msg) <= 20 else f"{msg[:17]}..."
            options.append(discord.SelectOption(
                label=f"[{t_time}] {short_msg}", 
                value=str(db_id),
                description="Click to remove this schedule from the system"
            ))
        super().__init__(placeholder="Select an automated announcement schedule to remove...", options=options)

    async def callback(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM announcements WHERE id = ?", (self.values[0],))
        conn.commit()
        conn.close()
        await interaction.response.send_message("🗑️ The automated announcement schedule has been completely removed from the database.", ephemeral=True)


class RemoveTimeView(discord.ui.View):
    def __init__(self, options_list):
        super().__init__()
        self.add_item(RemoveTimeSelect(options_list))

# =================================================================
# 🎛️ 6. 全套系統斜線指令實作 (Slash Commands)
# =================================================================

@bot.tree.command(name="help", description="Display the full list of bot commands and documentation")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Bot Commands and Maintenance Manual (Full Unabridged Version)", 
        color=discord.Color.gold(),
        description="All commands have been converted into Slash Commands. Type `/` to call them."
    )
    embed.add_field(
        name="🛡️ Server Protection & Management Commands", 
        value=(
            "`/setwelcome [channel]` - Configure the welcome channel and dynamic color embed layout.\n"
            "`/setlevelup [channel]` - Set the target channel for member level up broadcast alerts.\n"
            "`/addtime [time] [message]` - Schedule an automated announcement (Use UTC+0 format, e.g., 08:00).\n"
            "`/removetime` - Display all scheduled announcements to quickly select and delete them.\n"
            "`/automute [word] [minutes]` - Monitor a phrase, automatically deletes message and triggers temporary timeout.\n"
            "`/removeautomute [word]` - Unblock a specified phrase from the automated anti-spam defense list."
        ), 
        inline=False
    )
    embed.add_field(
        name="🎭 Interaction & Daily Commands", 
        value=(
            "`/manualmsg` - Open modal for admins to send **plain text** messages anonymously through the bot.\n"
            "`/random67` - Send a randomly selected magical quote filled with the 67 luck index.\n"
            "`/level [member]` - Check your or a target member's chat word stats and active level tier."
        ), 
        inline=False
    )
    embed.set_footer(text="System guarding 24/7 silently | 67 Core")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="manualmsg", description="Send a plain text message as the bot (no embeds or prefixes)")
async def manualmsg(interaction: discord.Interaction):
    # 彈出 Modal 供使用者輸入
    await interaction.response.send_modal(ManualMsgModal())


@bot.tree.command(name="setwelcome", description="Configure the welcome message channel and template")
@app_commands.describe(channel="Choose the channel where welcome embed cards will be sent")
async def setwelcome(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    await interaction.response.send_modal(WelcomeModal(channel))


@bot.tree.command(name="addtime", description="Add an automated announcement (UTC+0 timezone format)")
@app_commands.describe(time="Enter a 24-hour format time like 12:30", message="The full content of the announcement")
async def addtime(interaction: discord.Interaction, time: str, message: str):
    if ":" not in time or len(time) != 5:
        await interaction.response.send_message("❌ Time format error! Please input a 5-character time like `08:00` or `23:15`.", ephemeral=True)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)", 
        (time, message, str(interaction.channel_id))
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Automated announcement scheduled successfully! The bot will automatically speak at `{time}` UTC daily in this channel.", ephemeral=True)


@bot.tree.command(name="removetime", description="List all active automated announcement schedules with a removal menu")
async def removetime(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, time, message FROM announcements")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await interaction.response.send_message("❌ No automated announcement schedules found in the database.", ephemeral=True)
        return
        
    await interaction.response.send_message("Please select the announcement schedule you want to delete from the dropdown menu below:", view=RemoveTimeView(rows), ephemeral=True)


@bot.tree.command(name="random67", description="Send a random magical message filled with 67 luck index")
async def random67(interaction: discord.Interaction):
    jokes = [
        "🤖 67 is a number full of infinite magic and surprises!", 
        "🔮 After advanced algorithmic simulations, this channel's current luck factor is exactly 67 points!",
        "✨ The Six Seven spirit is quietly spreading inside this server...",
        "🚀 Random inspection complete: Your 67 energy index for today is at 100%!"
    ]
    await interaction.response.send_message(random.choice(jokes))


@bot.tree.command(name="level", description="Check your or another member's level and total text stats")
@app_commands.describe(user="Select the member you want to check (leave blank for yourself)")
async def level(interaction: discord.Interaction, user: discord.Member = None):
    target_user = user or interaction.user
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (str(target_user.id),))
    row = cursor.fetchone()
    conn.close()
    
    chars, lvl = row if row else (0, 1)
    
    embed = discord.Embed(title=f"📊 Server Activity Report for {target_user.display_name}", color=discord.Color.green())
    if target_user.avatar:
        embed.set_thumbnail(url=target_user.avatar.url)
    embed.add_field(name="✨ Current Level", value=f"`Lv. {lvl}`", inline=True)
    embed.add_field(name="✍️ Total Words Chatted", value=f"`{chars}` words", inline=True)
    
    # 計算下一級所需的目標字數 (每 150 字升一級)
    next_level_chars = lvl * 150
    progress = chars % 150
    embed.add_field(name="📈 Level Up Progress", value=f"`{next_level_chars - chars}` words remaining until next level ({progress}/150)", inline=False)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="automute", description="Setup banned word filters (timeouts user and deletes message; cannot be 67)")
@app_commands.describe(message="The sensitive phrase to block", time="Timeout duration in minutes for violators")
async def automute(interaction: discord.Interaction, message: str, time: int):
    if message == "67":
        await interaction.response.send_message("❌ Security Exception: The magical core number '67' cannot be set as a banned word!", ephemeral=True)
        return
        
    if time <= 0:
        await interaction.response.send_message("❌ Duration error! Must be greater than 0 minutes.", ephemeral=True)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO mutes (guild_id, banned_word, duration_mins) VALUES (?, ?, ?)",
        (str(interaction.guild_id), message, time)
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🔒 Banned word defense online! Word `{message}` is now monitored. Violators will be timed out for `{time}` minutes and their messages cleared.", ephemeral=True)


@bot.tree.command(name="removeautomute", description="Remove a specified banned word from the monitoring filter")
@app_commands.describe(message="The word you want to unblock")
async def removeautomute(interaction: discord.Interaction, message: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mutes WHERE guild_id = ? AND banned_word = ?", (str(interaction.guild_id), message))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    
    if count > 0:
        await interaction.response.send_message(f"✅ Successfully removed word `{message}` from the banned word monitoring filter.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Removal failed: Word `{message}` was not found in the defense list.", ephemeral=True)


@bot.tree.command(name="setlevelup", description="Customize the level up notification message and broadcast channel")
@app_commands.describe(channel="Choose the specific channel for level up broadcasts")
async def setlevelup(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    await interaction.response.send_modal(LevelUpModal(channel))

# =================================================================
# ⚡ 7. 核心系統事件監聽處理 (Events)
# =================================================================

@bot.event
async def on_member_join(member: discord.Member):
    """新成員加入事件：動態追蹤邀請人、頭像智慧吸色、極致還原 UI 截圖排版"""
    guild = member.guild
    inviter_name = "Unknown"
    
    # 1. 🔍 比對快取計算出是誰邀請的
    try:
        old_invites = bot.invites.get(guild.id, [])
        new_invites = await guild.invites()
        bot.invites[guild.id] = new_invites # 即時同步新列表快取
        
        for old_inv in old_invites:
            for new_inv in new_invites:
                if old_inv.code == new_inv.code and new_inv.uses > old_inv.uses:
                    inviter_name = new_inv.inviter.name
                    break
    except Exception as invite_err:
        print(f"⚠️ [Event] Invite tracking calculation error: {invite_err}")

    # 2. 🗄️ 從資料庫提取歡迎訊息設定
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message FROM welcome WHERE guild_id = ?", (str(guild.id),))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        channel = bot.get_channel(int(row[0]))
        if channel:
            # 3. 🎨 核心吸色技術：下載成員頭像並壓縮至 1x1 提取主色
            avatar_color = discord.Color.blue() # 防錯預設藍色
            try:
                avatar_bytes = await member.display_avatar.read()
                img = Image.open(io.BytesIO(avatar_bytes)).resize((1, 1))
                rgb = img.getpixel((0, 0))
                if isinstance(rgb, tuple):
                    avatar_color = discord.Color.from_rgb(rgb[0], rgb[1], rgb[2])
                else:
                    avatar_color = discord.Color.from_rgb(rgb, rgb, rgb)
            except Exception as color_err:
                print(f"⚠️ [Event] Mem
