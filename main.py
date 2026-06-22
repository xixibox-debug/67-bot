import discord
from discord import app_commands
from discord.ext import commands, tasks
import random
import datetime
import os
import sqlite3
import io
import logging
import re
from PIL import Image

# =================================================================
# ⚙️ 1. GLOBAL BOT CONFIGURATIONS
# =================================================================
WATCHING_STATUSES = [
    "67",
    "/help",    
    "Six Seven",
    "24/7 Auto Mute"
]

os.makedirs("data", exist_ok=True)
DB_PATH = "data/bot.db"

logging.basicConfig(level=logging.INFO)

# =================================================================
# 🗄️ 2. DATABASE INITIALIZATION (SQLite3)
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
    
    # 自動 Mute 禁字防護表（欄位格式保持 TEXT 相容性）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            guild_id TEXT, 
            banned_word TEXT, 
            duration_str TEXT, 
            PRIMARY KEY (guild_id, banned_word)
        )
    """)
    
    conn.commit()
    conn.close()
    print("✨ [Database] All functional database tables checked and initialized.")

init_db()

# =================================================================
# 🤖 3. BOT CORE CLASS DEFINITION
# =================================================================
class SixSevenBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.status_index = 0
        self.invites = {}
        self.last_announced_minute = ""  # 用於精準防漂移報時鎖

    async def setup_hook(self):
        """當機器人啟動時，負責掛載背景任務與同步斜線指令"""
        self.rotate_status.start()
        self.check_time_announcements.start()
        await self.tree.sync()
        print("🤖 [System] Bot core and slash command tree synchronized successfully.")

    # --- 🔄 LOOP 1: STATUS ROTATION (EVERY 5 SECONDS) ---
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

    # --- ⏰ LOOP 2: 精準防漏報時監聽器 (每 30 秒高頻精準對時) ---
    @tasks.loop(seconds=30)
    async def check_time_announcements(self):
        # 使用台灣時間時區 (UTC+8)
        tz_tw = datetime.timezone(datetime.timedelta(hours=8))
        now_tw = datetime.datetime.now(tz_tw).strftime("%H:%M")
        
        # 如果這一分鐘已經成功報時過，直接跳過防重複觸發
        if now_tw == self.last_announced_minute:
            return
            
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message FROM announcements WHERE time = ?", (now_tw,))
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            self.last_announced_minute = now_tw  # 記錄當前已發送的分鐘
            for channel_id, message in rows:
                channel = self.get_channel(int(channel_id))
                if channel:
                    try:
                        await channel.send(message)
                    except Exception as e:
                        print(f"❌ Announcement delivery failed (Channel ID: {channel_id}): {e}")

    @check_time_announcements.before_loop
    async def before_check_time(self):
        await self.wait_until_ready()

bot = SixSevenBot()

# =================================================================
# 📡 4. INVITE CACHE EVENTS
# =================================================================
@bot.event
async def on_ready():
    print(f"🟢 [Online] Bot successfully logged in as {bot.user.name} ({bot.user.id})")
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
    try:
        bot.invites[guild.id] = await guild.invites()
    except:
        pass

@bot.event
async def on_guild_remove(guild):
    bot.invites.pop(guild.id, None)

# =================================================================
# 🛠️ 4.5 TIME PARSER HELPER FOR MUTE COMMAND
# =================================================================
def parse_mute_duration(duration_str: str):
    """解析時間字串，如 1m, 3m, 1h, 1d 等，最高限制 14d"""
    match = re.match(r"^(\d+)([mhd])$", duration_str.strip().lower())
    if not match:
        return None, "❌ 時間格式錯誤！請使用基本格式如 `1m` (分), `3h` (小時), `1d` (天)。"
    
    amount = int(match.group(1))
    unit = match.group(2)
    
    if unit == 'm':
        delta = datetime.timedelta(minutes=amount)
    elif unit == 'h':
        delta = datetime.timedelta(hours=amount)
    elif unit == 'd':
        delta = datetime.timedelta(days=amount)
    else:
        return None, "❌ 未知的時間單位。"
        
    if delta > datetime.timedelta(days=14):
        return None, "❌ 時間限制超標！最多只能禁言至 `14d` (14天)。"
        
    return delta, None

# =================================================================
# 🖥️ 5. INTERACTIVE UI COMPONENTS (MODALS & VIEWS)
# =================================================================

class ManualMsgModal(discord.ui.Modal, title="Send Plain Text Message"):
    msg_input = discord.ui.TextInput(
        label="Enter the message content to broadcast", 
        style=discord.TextStyle.paragraph,
        placeholder="Type your text here...",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.channel.send(content=self.msg_input.value)
        await interaction.response.send_message("✅ Message successfully sent as plain text.", ephemeral=True)


class WelcomeModal(discord.ui.Modal):
    """/setwelcome 專用彈出式視窗：儲存後直接在悄悄話下方塞入完全模擬的測試卡片"""
    def __init__(self, channel: discord.TextChannel):
        super().__init__(title="Setup Server Welcome Message")
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

        guild = interaction.guild
        member = interaction.user  
        mock_inviter = "67_Tester" 

        avatar_color = discord.Color.blue()
        try:
            avatar_bytes = await member.display_avatar.read()
            img = Image.open(io.BytesIO(avatar_bytes)).resize((1, 1))
            rgb = img.getpixel((0, 0))
            if isinstance(rgb, tuple):
                avatar_color = discord.Color.from_rgb(rgb[0], rgb[1], rgb[2])
            else:
                avatar_color = discord.Color.from_rgb(rgb, rgb, rgb)
        except:
            pass

        def parse_template(template_str):
            if not template_str:
                return ""
            return (template_str
                    .replace("{member.count}", str(guild.member_count))
                    .replace("{inviter.name}", mock_inviter)
                    .replace("{user.name}", member.name)
                    .replace("{user.mention}", member.mention)
                    .replace("{server.name}", guild.name))

        description_text = parse_template(self.msg_input.value)
        title_text = parse_template("Hey, welcome to {server.name}!!!")
        footer_text = parse_template("{server.name} | 67")

        embed = discord.Embed(title=title_text, description=description_text, color=avatar_color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=footer_text)

        confirmation_content = (
            f"✅ **Welcome settings configured successfully!** Messages bound to {self.channel.mention}.\n"
            f"--- \n"
            f"👁️ **【歡迎卡片效果即時測試預覽】** (此為悄悄話，僅有你能看見測試畫面)：\n"
            f"{member.mention}"
        )

        await interaction.response.send_message(
            content=confirmation_content,
            embed=embed,
            ephemeral=True
        )


class LevelUpModal(discord.ui.Modal):
    def __init__(self, channel: discord.TextChannel):
        super().__init__(title="Customize Level Up Message")
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
    def __init__(self, options_list):
        options = []
        for item in options_list:
            db_id, t_time, msg = item
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


class RemovePanelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="歡迎訊息面板 (Welcome Panel)",
                value="welcome",
                description="取消綁定歡迎頻道，並刪除客製化卡片模板資料",
                emoji="👋"
            ),
            discord.SelectOption(
                label="等級提升面板 (Level Up Panel)",
                value="levelup",
                description="取消綁定升級頻道，並刪除成員升級通知語句",
                emoji="🎉"
            )
        ]
        super().__init__(placeholder="請選擇想要取消設定（關閉）的系統面板...", options=options)

    async def callback(self, interaction: discord.Interaction):
        panel_type = self.values[0]
        guild_id_str = str(interaction.guild_id)
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        if panel_type == "welcome":
            cursor.execute("DELETE FROM welcome WHERE guild_id = ?", (guild_id_str,))
            panel_name = "歡迎訊息面板"
        elif panel_type == "levelup":
            cursor.execute("DELETE FROM levelup WHERE guild_id = ?", (guild_id_str,))
            panel_name = "等級提升面板"
            
        count = cursor.rowcount
        conn.commit()
        conn.close()
        
        if count > 0:
            await interaction.response.send_message(f"🗑️ 已成功重設並刪除 **{panel_name}** 的所有頻道與模板設定！", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 移除失敗：此伺服器目前本來就沒有設定 **{panel_name}**。", ephemeral=True)


class RemovePanelView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(RemovePanelSelect())

# =================================================================
# 🎛️ 6. SLASH COMMANDS
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
            "`/removepanel` - Select panel modules (Welcome, Level Up) via dropdown menu to disable them.\n"
            "`/addtime [time] [message]` - Schedule an automated announcement (Taiwan Time UTC+8, e.g., 16:30).\n"
            "`/removetime` - Display all scheduled announcements to quickly select and delete them.\n"
            "`/automute [word] [duration]` - Monitor a phrase, timeouts user with custom embed card without deleting message.\n"
            "`/removeautomute [word]` - Unblock a specified phrase from the automated anti-spam defense list.\n"
            "`/mute [user] [time] [reason]` - Timeout a user and log with custom design card.\n"
            "`/unmute [user]` - Instantly unmute a user and remove timeout restriction.\n"
            "`/kick [user] [reason]` - Kick a user from the server and log with custom design card."
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
    await interaction.response.send_modal(ManualMsgModal())


@bot.tree.command(name="setwelcome", description="Configure the welcome message channel and template")
@app_commands.describe(channel="Choose the channel where welcome embed cards will be sent")
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_modal(WelcomeModal(channel))


@bot.tree.command(name="removepanel", description="Open dropdown menu to unset and clear server functional panels")
@app_commands.checks.has_permissions(manage_guild=True)
async def removepanel(interaction: discord.Interaction):
    await interaction.response.send_message(
        content="請從下方的下拉選單中，選擇你想要**取消設定（關閉並清空資料）**的面板功能：", 
        view=RemovePanelView(), 
        ephemeral=True
    )


@bot.tree.command(name="mute", description="Timeout a server member with a beautiful green embed report")
@app_commands.describe(user="The member to mute", time="Duration format (e.g., 1m, 10m, 2h, 1d, 14d)", reason="Reason for mute (Optional)")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, time: str, reason: str = "None"):
    delta, error_msg = parse_mute_duration(time)
    if error_msg:
        await interaction.response.send_message(content=error_msg, ephemeral=True)
        return
        
    try:
        await user.timeout(delta, reason=reason)
        
        embed = discord.Embed(
            title=f"✅ {user.name} has been muted.", 
            color=0x2ecc71,  
            description=f"Time: {time}\nReason: {reason}"
        )
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 權限不足！機器人的權限必須比該被禁言的成員職位更高。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 發生未知錯誤: {e}", ephemeral=True)


@bot.tree.command(name="unmute", description="Instantly remove timeout restriction from a member")
@app_commands.describe(user="The member to unmute")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    try:
        await user.timeout(None, reason="Unmuted by Administrator")
        
        embed = discord.Embed(
            title=f"✅ {user.name} has been unmuted.",
            color=0x2ecc71
        )
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 權限不足！無法解除該成員的禁言狀態。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 發生未知錯誤: {e}", ephemeral=True)


# --- 🛡️ NEW: /kick 踢出成員指令 (外觀比照 /mute 綠色面板) ---
@bot.tree.command(name="kick", description="Kick a server member with a beautiful green embed report")
@app_commands.describe(user="The member to kick", reason="Reason for kick (Optional)")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "None"):
    try:
        # 執行踢出
        await user.kick(reason=reason)
        
        # 建立與 /mute 風格一致的嵌入面板
        embed = discord.Embed(
            title=f"✅ {user.name} has been kicked.", 
            color=0x2ecc71,  
            description=f"Reason: {reason}"
        )
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 權限不足！機器人的最高身分組職位必須比該被踢出成員更高，且需要踢出成員權限。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 發生未知錯誤: {e}", ephemeral=True)


@bot.tree.command(name="addtime", description="新增自動報時排程（精準支援台灣時間 UTC+8）")
@app_commands.describe(time="請輸入24小時制時間，例如 08:00 或 16:30", message="報時的訊息內容")
async def addtime(interaction: discord.Interaction, time: str, message: str):
    if ":" not in time or len(time) != 5:
        await interaction.response.send_message("❌ 時間格式錯誤！請輸入5字元格式，例如 `08:00` 或 `23:15`。", ephemeral=True)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)", 
        (time, message, str(interaction.channel_id))
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 自動報時排程設定成功！機器人將於每日台灣時間 `{time}` 在此頻道發送報時訊息。", ephemeral=True)


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
    
    next_level_chars = lvl * 150
    progress = chars % 150
    embed.add_field(name="📈 Level Up Progress", value=f"`{next_level_chars - chars}` words remaining until next level ({progress}/150)", inline=False)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="automute", description="設定敏感詞防護（不刪除訊息，違規者自動禁言並觸發特製字卡）")
@app_commands.describe(message="要封鎖的敏感詞彙", time="禁言時間格式（例如：1m, 30m, 2h, 1d）")
async def automute(interaction: discord.Interaction, message: str, time: str):
    if message == "67":
        await interaction.response.send_message("❌ 安全例外：無法將核心魔術數字 '67' 設定為敏感詞！", ephemeral=True)
        return
        
    delta, error_msg = parse_mute_duration(time)
    if error_msg:
        await interaction.response.send_message(content=error_msg, ephemeral=True)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO mutes (guild_id, banned_word, duration_str) VALUES (?, ?, ?)",
        (str(interaction.guild_id), message, time)
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🔒 敏感詞防護上線！已監控字詞 `{message}`。違規者將被禁言 `{time}`，原訊息將被保留並發送 HAHAHA 嘲諷卡片。", ephemeral=True)


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
async def setlevelup(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_modal(LevelUpModal(channel))

# =================================================================
# ⚡ 7. CORE SYSTEM EVENT LISTENERS
# =================================================================

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    inviter_name = "Unknown"
    
    try:
        old_invites = bot.invites.get(guild.id, [])
        new_invites = await guild.invites()
        bot.invites[guild.id] = new_invites
        
        for old_inv in old_invites:
            for new_inv in new_invites:
                if old_inv.code == new_inv.code and new_inv.uses > old_inv.uses:
                    inviter_name = new_inv.inviter.name
                    break
    except Exception as invite_err:
        print(f"⚠️ [Event] Invite tracking calculation error: {invite_err}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message FROM welcome WHERE guild_id = ?", (str(guild.id),))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        channel = bot.get_channel(int(row[0]))
        if channel:
            avatar_color = discord.Color.blue()
            try:
                avatar_bytes = await member.display_avatar.read()
                img = Image.open(io.BytesIO(avatar_bytes)).resize((1, 1))
                rgb = img.getpixel((0, 0))
                if isinstance(rgb, tuple):
                    avatar_color = discord.Color.from_rgb(rgb[0], rgb[1], rgb[2])
                else:
                    avatar_color = discord.Color.from_rgb(rgb, rgb, rgb)
            except Exception as color_err:
                print(f"⚠️ [Event] Color extraction failed: {color_err}")

            def parse_template(template_str):
                if not template_str:
                    return ""
                return (template_str
                        .replace("{member.count}", str(guild.member_count))
                        .replace("{inviter.name}", inviter_name)
                        .replace("{user.name}", member.name)
                        .replace("{user.mention}", member.mention)
                        .replace("{server.name}", guild.name))

            description_text = parse_template(row[1])
            title_text = parse_template("Hey, welcome to {server.name}!!!")
            footer_text = parse_template("{server.name} | 67")

            embed = discord.Embed(title=title_text, description=description_text, color=avatar_color)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=footer_text)

            await channel.send(content=f"{member.mention}", embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild_id_str = str(message.guild.id)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 讀取敏感詞與對應的時間格式字串
    cursor.execute("SELECT banned_word, duration_str FROM mutes WHERE guild_id = ?", (guild_id_str,))
    banned_words = cursor.fetchall()
    
    for word, duration_str in banned_words:
        if word in message.content:
            # 解析動態時間格式
            delta, _ = parse_mute_duration(duration_str)
            if not delta:
                delta = datetime.timedelta(minutes=10) # 格式萬一損毀的備用安全機制
                
            try:
                # 執行禁言懲罰
                await message.author.timeout(delta, reason=f"Triggered server filtered banned word: {word}")
                
                # 完美還原 Image 5 嘲諷卡片：不刪除原訊息，直接原地發送
                embed = discord.Embed(
                    title="HAHAHA 😂",
                    description=f"{message.author.name} has been muted for {duration_str} due to he/she sent the message \"{message.content}\", you can try and be the next!",
                    color=0xe74c3c  # 紅色系邊框
                )
                embed.set_footer(text=f"{message.guild.name} | 67")
                
                await message.channel.send(embed=embed)
                conn.close()
                return  # 攔截成功，中斷後續邏輯避免重複觸發
            except discord.Forbidden:
                print(f"⚠️ [Security] Failed to timeout member due to lack of sufficient bot permissions.")
            except Exception as e:
                print(f"⚠️ [Security] Banned word interception routine error: {e}")

    # --- 經驗值與升級模組 ---
    user_id_str = str(message.author.id)
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (user_id_str,))
    row = cursor.fetchone()
    
    current_chars, current_level = row if row else (0, 1)
    level_up_triggered = False
    
    if message.content.strip() == "67":
        current_level += 1
        level_up_triggered = True
    else:
        current_chars += len(message.content)
        calculated_level = (current_chars // 150) + 1
        if calculated_level > current_level:
            current_level = calculated_level
            level_up_triggered = True

    cursor.execute(
        "INSERT OR REPLACE INTO levels (user_id, chars, level) VALUES (?, ?, ?)",
        (user_id_str, current_chars, current_level)
    )
    conn.commit()

    if level_up_triggered:
        cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (guild_id_str,))
        levelup_row = cursor.fetchone()
        if levelup_row:
            notify_channel = bot.get_channel(int(levelup_row[0]))
            if notify_channel:
                rendered_msg = levelup_row[1].replace("{user.mention}", message.author.mention)\
                                             .replace("{user.name}", message.author.name)\
                                             .replace("{user.level}", str(current_level))
                try:
                    await notify_channel.send(content=rendered_msg)
                except Exception as send_err:
                    print(f"⚠️ [Event] Level up broadcast message sending failed: {send_err}")

    conn.close()
    await bot.process_commands(message)

# =================================================================
# 🚀 8. RUN THE BOT
# =================================================================
bot.run(os.getenv("DISCORD_TOKEN"))
