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
    "/settings",    
    "Six Seven",
    "24/7 Auto Mute"
]

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

logging.basicConfig(level=logging.INFO)

# =================================================================
# 🗄️ 2. DATABASE INITIALIZATION & AUTO-MIGRATION
# =================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 建立核心資料表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome (
            guild_id TEXT PRIMARY KEY, 
            channel_id TEXT, 
            welcome_title TEXT, 
            welcome_desc TEXT, 
            goodbye_title TEXT, 
            goodbye_desc TEXT
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS levelup (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, message TEXT, channel_id TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS levels (user_id TEXT PRIMARY KEY, chars INTEGER, level INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS mutes (guild_id TEXT, banned_word TEXT, duration_str TEXT, PRIMARY KEY (guild_id, banned_word))")
    
    # 🛡️ 自動相容性升級：如果使用者有舊版資料庫，自動補齊 4 個新欄位
    try:
        cursor.execute("ALTER TABLE welcome ADD COLUMN welcome_title TEXT")
        cursor.execute("ALTER TABLE welcome ADD COLUMN welcome_desc TEXT")
        cursor.execute("ALTER TABLE welcome ADD COLUMN goodbye_title TEXT")
        cursor.execute("ALTER TABLE welcome ADD COLUMN goodbye_desc TEXT")
    except sqlite3.OperationalError:
        pass # 欄位早已存在，忽略即可
        
    conn.commit()
    conn.close()

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
        self.last_announced_minute = "" 

    async def setup_hook(self):
        self.rotate_status.start()
        self.check_time_announcements.start()
        await self.tree.sync()
        print("🤖 [System] Bot core and slash command tree synchronized successfully.")

    @tasks.loop(seconds=5)
    async def rotate_status(self):
        if not WATCHING_STATUSES: return
        if self.status_index >= len(WATCHING_STATUSES): self.status_index = 0
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=WATCHING_STATUSES[self.status_index]))
        self.status_index += 1

    @rotate_status.before_loop
    async def before_rotate(self): await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def check_time_announcements(self):
        tz_tw = datetime.timezone(datetime.timedelta(hours=8))
        now_tw = datetime.datetime.now(tz_tw).strftime("%H:%M")
        if now_tw == self.last_announced_minute: return
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message FROM announcements WHERE time = ?", (now_tw,))
        rows = cursor.fetchall()
        conn.close()
        if rows:
            self.last_announced_minute = now_tw  
            for channel_id, message in rows:
                channel = self.get_channel(int(channel_id))
                if channel:
                    try: await channel.send(message)
                    except: pass

    @check_time_announcements.before_loop
    async def before_check_time(self): await self.wait_until_ready()

bot = SixSevenBot()

# =================================================================
# 📡 4. INVITE CACHE EVENTS & UTILS
# =================================================================
@bot.event
async def on_ready():
    print(f"🟢 [Online] Bot successfully logged in as {bot.user.name}")
    for guild in bot.guilds:
        try: bot.invites[guild.id] = await guild.invites()
        except: pass

@bot.event
async def on_guild_join(guild):
    try: bot.invites[guild.id] = await guild.invites()
    except: pass

@bot.event
async def on_guild_remove(guild): bot.invites.pop(guild.id, None)

def parse_mute_duration(duration_str: str):
    match = re.match(r"^(\d+)([mhd])$", duration_str.strip().lower())
    if not match: return None, "❌ 時間格式錯誤！請使用基本格式如 `1m` (分), `3h` (小時), `1d` (天)。"
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == 'm': delta = datetime.timedelta(minutes=amount)
    elif unit == 'h': delta = datetime.timedelta(hours=amount)
    elif unit == 'd': delta = datetime.timedelta(days=amount)
    else: return None, "❌ 未知的時間單位。"
    if delta > datetime.timedelta(days=14): return None, "❌ 時間限制超標！最多只能禁言至 `14d` (14天)。"
    return delta, None

# 🌟 新增：萬能變數解析轉換器（精確對應圖片中的所有語法支持）
def parse_welcome_goodbye_template(template_str: str, guild: discord.Guild, member: discord.Member, inviter_name: str = "Unknown"):
    if not template_str: return ""
    res = template_str
    # 伺服器變數替換
    res = res.replace("{guild.name}", guild.name).replace("{server.name}", guild.name)
    res = res.replace("{guild.membercount}", str(guild.member_count)).replace("{guild.members}", str(guild.member_count)).replace("{member.count}", str(guild.member_count))
    # 使用者變數替換
    res = res.replace("{user.name}", member.name).replace("{user.username}", member.name).replace("{user.mention}", member.mention)
    # 邀請人變數替換
    res = res.replace("{inviter.name}", inviter_name).replace("{inviter}", inviter_name)
    return res

# =================================================================
# 🖥️ 5. INTERACTIVE UI COMPONENTS (MODALS & VIEWS)
# =================================================================

class ManualMsgModal(discord.ui.Modal, title="發送純文字訊息"):
    msg_input = discord.ui.TextInput(label="請輸入要廣播的訊息內容", style=discord.TextStyle.paragraph, required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.channel.send(content=self.msg_input.value)
        await interaction.response.send_message("✅ 訊息已成功發送。", ephemeral=True)

# 🌟 核心改動：精確復刻圖片表單的 4 大文字輸入區塊（Set Welcome Message）
class WelcomeGoodbyeModal(discord.ui.Modal):
    def __init__(self, channel: discord.TextChannel):
        super().__init__(title="Set Welcome Message")
        self.channel = channel

    welcome_title = discord.ui.TextInput(
        label="Enter Embed Title of Welcome message",
        default="Hey, welcome to {guild.name}!!!",
        placeholder="可以使用 {user.name} {guild.name}...",
        required=True
    )
    welcome_desc = discord.ui.TextInput(
        label="Enter Embed Description of Welcome message",
        style=discord.TextStyle.paragraph,
        default="You are the {member.count} member here!\nInviter: {inviter.name}",
        placeholder="可以使用 {guild.membercount} {inviter}...",
        required=True
    )
    goodbye_title = discord.ui.TextInput(
        label="Enter Embed Title of Goodbye message",
        default="{user.name} has leave the server",
        placeholder="離開標題",
        required=True
    )
    goodbye_desc = discord.ui.TextInput(
        label="Enter Embed Description of Goodbye message",
        style=discord.TextStyle.paragraph,
        default="Whyyyyyy u leave us?????",
        placeholder="離開說明內文",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO welcome (guild_id, channel_id, welcome_title, welcome_desc, goodbye_title, goodbye_desc) 
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(interaction.guild_id), 
            str(self.channel.id), 
            self.welcome_title.value, 
            self.welcome_desc.value, 
            self.goodbye_title.value, 
            self.goodbye_desc.value
        ))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ **Welcome/Goodbye 面板配置儲存成功！**\n已成功將通知功能綁定至頻道：{self.channel.mention}", ephemeral=True)

class LevelUpModal(discord.ui.Modal):
    def __init__(self, channel: discord.TextChannel):
        super().__init__(title="設定自訂升級通知")
        self.channel = channel
    msg_input = discord.ui.TextInput(label="通知內文 (支援 {user.mention} 與 {user.level})", style=discord.TextStyle.paragraph, default="🎉 Congratulations {user.mention}, you leveled up to **Lv. {user.level}**!", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO levelup (guild_id, channel_id, message) VALUES (?, ?, ?)", (str(interaction.guild_id), str(self.channel.id), self.msg_input.value))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ 等級提升通知已成功綁定至頻道：{self.channel.mention}！", ephemeral=True)

class RemoveTimeSelect(discord.ui.Select):
    def __init__(self, options_list):
        options = []
        for item in options_list:
            db_id, t_time, msg = item
            short_msg = msg if len(msg) <= 20 else f"{msg[:17]}..."
            options.append(discord.SelectOption(label=f"[{t_time}] {short_msg}", value=str(db_id)))
        super().__init__(placeholder="請選擇要移除的自動報時排程...", options=options)
    async def callback(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM announcements WHERE id = ?", (self.values[0],))
        conn.commit()
        conn.close()
        await interaction.response.send_message("🗑️ 該自動報時排程已完全從資料庫中移除。", ephemeral=True)

# =================================================================
# 🎛️ 5.5 INTEGRATED SETTINGS CONTROLLER
# =================================================================

class ChannelSelectMenu(discord.ui.ChannelSelect):
    def __init__(self, panel_type: str):
        self.panel_type = panel_type
        super().__init__(placeholder="請選擇目標文字頻道...", channel_types=[discord.ChannelType.text])
    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        if self.panel_type == "welcome":
            # 觸發全新打造的4欄位整合型設計畫面
            await interaction.response.send_modal(WelcomeGoodbyeModal(channel))
        elif self.panel_type == "levelup":
            await interaction.response.send_modal(LevelUpModal(channel))

class ChannelSelectView(discord.ui.View):
    def __init__(self, panel_type: str):
        super().__init__(timeout=60)
        self.add_item(ChannelSelectMenu(panel_type))

class PanelSubView(discord.ui.View):
    def __init__(self, panel_type: str):
        super().__init__(timeout=60)
        self.panel_type = panel_type
    @discord.ui.button(label="⚙️ 設定/修改頻道與字卡", style=discord.ButtonStyle.primary)
    async def setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("請選擇您要發送通知的目標頻道：", view=ChannelSelectView(self.panel_type), ephemeral=True)
    @discord.ui.button(label="🗑️ 關閉功能並重設資料", style=discord.ButtonStyle.danger)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        table = "welcome" if self.panel_type == "welcome" else "levelup"
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {table} WHERE guild_id = ?", (str(interaction.guild_id),))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        msg = "👋 歡迎與告別面板" if self.panel_type == "welcome" else "🎉 等級面板"
        if count > 0: await interaction.response.send_message(f"🗑️ 已成功移除並關閉 **{msg}** 功能！", ephemeral=True)
        else: await interaction.response.send_message(f"❌ 該功能本來就沒有設定過任何資料。", ephemeral=True)

class AutoMuteAddModal(discord.ui.Modal, title="🔒 新增敏感詞防護"):
    word = discord.ui.TextInput(label="要封鎖的字詞", placeholder="例如: 髒話", required=True)
    duration = discord.ui.TextInput(label="禁言時間 (格式如: 1m, 30m, 2h, 1d)", default="10m", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        if self.word.value == "67":
            await interaction.response.send_message("❌ 安全例外：無法將核心魔術數字 '67' 設定為敏感詞！", ephemeral=True)
            return
        delta, error_msg = parse_mute_duration(self.duration.value)
        if error_msg:
            await interaction.response.send_message(content=error_msg, ephemeral=True)
            return
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO mutes (guild_id, banned_word, duration_str) VALUES (?, ?, ?)", (str(interaction.guild_id), self.word.value, self.duration.value))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"🔒 敏感詞防護上線！已監控 `{self.word.value}`，違規者將自動關小黑屋 `{self.duration.value}`。", ephemeral=True)

class AutoMuteRemoveModal(discord.ui.Modal, title="🔓 移除敏感詞防護"):
    word = discord.ui.TextInput(label="請輸入要解鎖的字詞", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mutes WHERE guild_id = ? AND banned_word = ?", (str(interaction.guild_id), self.word.value))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        if count > 0: await interaction.response.send_message(f"✅ 已成功將字詞 `{self.word.value}` 從敏感詞清單移除！", ephemeral=True)
        else: await interaction.response.send_message(f"❌ 清單中找不到該字詞。", ephemeral=True)

class AutoMuteSubView(discord.ui.View):
    def __init__(self): super().__init__(timeout=60)
    @discord.ui.button(label="➕ 新增阻斷字詞", style=discord.ButtonStyle.success)
    async def add_word(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoMuteAddModal())
    @discord.ui.button(label="➖ 移除阻斷字詞", style=discord.ButtonStyle.danger)
    async def remove_word(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoMuteRemoveModal())

class TimeAnnouncementAddModal(discord.ui.Modal, title="⏰ 新增定時廣播"):
    time_str = discord.ui.TextInput(label="24小時制時間 (例如: 08:00 或 16:30)", placeholder="HH:MM", max_length=5, required=True)
    message = discord.ui.TextInput(label="廣播的訊息內容", style=discord.TextStyle.paragraph, required=True)
    async def on_submit(self, interaction: discord.Interaction):
        if ":" not in self.time_str.value or len(self.time_str.value) != 5:
            await interaction.response.send_message("❌ 時間格式錯誤！請務必填寫如 `08:00` 的5字元格式。", ephemeral=True)
            return
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)", (self.time_str.value, self.message.value, str(interaction.channel_id)))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ 排程設定成功！每日台灣時間 `{self.time_str.value}` 將會在此頻道自動發送廣播。", ephemeral=True)

class TimeMessageSubView(discord.ui.View):
    def __init__(self): super().__init__(timeout=60)
    @discord.ui.button(label="➕ 新增定時廣播排程", style=discord.ButtonStyle.success)
    async def add_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeAnnouncementAddModal())
    @discord.ui.button(label="🗑️ 查看與移除現有排程", style=discord.ButtonStyle.danger)
    async def view_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, time, message FROM announcements")
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            await interaction.response.send_message("❌ 目前資料庫中沒有任何有效的報時排程。", ephemeral=True)
            return
        view = discord.ui.View()
        view.add_item(RemoveTimeSelect(rows))
        await interaction.response.send_message("請選擇您想要刪除的定時排程：", view=view, ephemeral=True)

class SettingsHubView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Welcome/Goodbye Panel", style=discord.ButtonStyle.secondary, emoji="👋")
    async def press_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🛠| **歡迎與告別面板管理選單：**", view=PanelSubView("welcome"), ephemeral=True)
    @discord.ui.button(label="Level System", style=discord.ButtonStyle.secondary, emoji="🎉")
    async def press_level(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🛠| **等級經驗值系統管理選單：**", view=PanelSubView("levelup"), ephemeral=True)
    @discord.ui.button(label="Auto Mute", style=discord.ButtonStyle.secondary, emoji="🔒")
    async def press_automute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🛠| **自動防護敏感詞管理選單：**", view=AutoMuteSubView(), ephemeral=True)
    @discord.ui.button(label="Time Message", style=discord.ButtonStyle.secondary, emoji="⏰")
    async def press_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🛠| **定時推播與廣播排程管理選單：**", view=TimeMessageSubView(), ephemeral=True)

# =================================================================
# 🎛️ 6. SLASH COMMANDS
# =================================================================

@bot.tree.command(name="settings", description="開啟 67-Bot 伺服器模組全能整合式控制面板")
@app_commands.checks.has_permissions(manage_guild=True)
async def settings_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Settings",
        color=0xdfe600, 
        description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message"
    )
    await interaction.response.send_message(embed=embed, view=SettingsHubView())

@bot.tree.command(name="help", description="Display the full list of bot commands and documentation")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Bot Maintenance Manual", color=discord.Color.gold())
    embed.add_field(name="⚙️ 全能控制台 (建議使用)", value="`/settings` - 一鍵開啟視覺化管理面板，內置按鈕調整所有功能模組", inline=False)
    embed.add_field(name="🛡️ 傳統管理指令", value="`/mute`, `/unmute`, `/kick`, `/manualmsg`", inline=False)
    embed.add_field(name="🎭 互動日常", value="`/random67`, `/level`", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="manualmsg", description="Send a plain text message as the bot")
async def manualmsg(interaction: discord.Interaction): await interaction.response.send_modal(ManualMsgModal())

@bot.tree.command(name="mute", description="Timeout a server member")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, time: str, reason: str = "None"):
    delta, error_msg = parse_mute_duration(time)
    if error_msg: return await interaction.response.send_message(content=error_msg, ephemeral=True)
    try:
        await user.timeout(delta, reason=reason)
        await interaction.response.send_message(embed=discord.Embed(title=f"✅ {user.name} has been muted.", color=0x2ecc71, description=f"Time: {time}\nReason: {reason}"))
    except: await interaction.response.send_message("❌ 權限不足。", ephemeral=True)

@bot.tree.command(name="unmute", description="Instantly remove timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    try:
        await user.timeout(None)
        await interaction.response.send_message(embed=discord.Embed(title=f"✅ {user.name} has been unmuted.", color=0x2ecc71))
    except: await interaction.response.send_message("❌ 失敗。", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a server member")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "None"):
    try:
        await user.kick(reason=reason)
        await interaction.response.send_message(embed=discord.Embed(title=f"✅ {user.name} has been kicked.", color=0x2ecc71, description=f"Reason: {reason}"))
    except: await interaction.response.send_message("❌ 失敗。", ephemeral=True)

@bot.tree.command(name="random67", description="Send a random magical message")
async def random67(interaction: discord.Interaction):
    jokes = ["🤖 67 is a number full of infinite magic!", "🔮 Current luck factor is exactly 67 points!", "✨ The Six Seven spirit is spreading..."]
    await interaction.response.send_message(random.choice(jokes))

@bot.tree.command(name="level", description="Check member level stats")
async def level(interaction: discord.Interaction, user: discord.Member = None):
    target_user = user or interaction.user
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (str(target_user.id),))
    row = cursor.fetchone()
    conn.close()
    chars, lvl = row if row else (0, 1)
    embed = discord.Embed(title=f"📊 Report for {target_user.display_name}", color=discord.Color.green())
    embed.add_field(name="✨ Level", value=f"`Lv. {lvl}`", inline=True)
    embed.add_field(name="✍️ Words", value=f"`{chars}`", inline=True)
    await interaction.response.send_message(embed=embed)

# =================================================================
# ⚡ 7. CORE SYSTEM EVENT LISTENERS
# =================================================================

# 👋 系統監聽 A：成員加入（發送精美自訂 Embed 歡迎卡）
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
    except: pass
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, welcome_title, welcome_desc FROM welcome WHERE guild_id = ?", (str(guild.id),))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0]:
        channel = bot.get_channel(int(row[0]))
        if channel:
            title_tpl = row[1] if row[1] else "Hey, welcome to {server.name}!!!"
            desc_tpl = row[2] if row[2] else "You are the {member.count} member here!\nInviter: {inviter.name}"
            
            parsed_title = parse_welcome_goodbye_template(title_tpl, guild, member, inviter_name)
            parsed_desc = parse_welcome_goodbye_template(desc_tpl, guild, member, inviter_name)
            
            # 建立與圖片 1000008233.png 一致的精美左側亮藍邊嵌入字卡
            embed = discord.Embed(title=parsed_title, description=parsed_desc, color=0x54a7dd)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"{guild.name} | 67")
            await channel.send(content=f"{member.mention}", embed=embed)

# 🏃‍♂️ 系統監聽 B：成員退出（發送精美自訂 Embed 告別卡）
@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, goodbye_title, goodbye_desc FROM welcome WHERE guild_id = ?", (str(guild.id),))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0]:
        channel = bot.get_channel(int(row[0]))
        if channel:
            title_tpl = row[1] if row[1] else "{user.name} has leave the server"
            desc_tpl = row[2] if row[2] else "Whyyyyyy u leave us?????"
            
            parsed_title = parse_welcome_goodbye_template(title_tpl, guild, member)
            parsed_desc = parse_welcome_goodbye_template(desc_tpl, guild, member)
            
            # 退出改採淡紅色系
            embed = discord.Embed(title=parsed_title, description=parsed_desc, color=0xe74c3c)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"{guild.name} | 67")
            await channel.send(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild: return

    # 🎰 24/7 全天候 67 隨機大標題回覆系統
    cleaned_content = re.sub(r'<@!?\d+>|<@&\d+>|<#\d+>|<a?:.+?:\d+>', '', message.content)
    if "67" in cleaned_content or "6️⃣7️⃣" in cleaned_content:
        lucky_responses = [f"# {message.author.mention}, 67!!!!!", f"# {message.author.mention}, six seven!!!!!"]
        try: await message.reply(content=random.choice(lucky_responses))
        except: pass

    guild_id_str = str(message.guild.id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT banned_word, duration_str FROM mutes WHERE guild_id = ?", (guild_id_str,))
    banned_words = cursor.fetchall()
    
    for word, duration_str in banned_words:
        if word in message.content:
            delta, _ = parse_mute_duration(duration_str)
            if not delta: delta = datetime.timedelta(minutes=10)
            try:
                await message.author.timeout(delta, reason=f"Banned word: {word}")
                embed = discord.Embed(title="HAHAHA 😂", description=f"{message.author.name} has been muted for {duration_str} due to he/she sent the message \"{message.content}\", you can try and be the next!", color=0xe74c3c)
                embed.set_footer(text=f"{message.guild.name} | 67")
                await message.channel.send(embed=embed)
                conn.close()
                return
            except: pass

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

    cursor.execute("INSERT OR REPLACE INTO levels (user_id, chars, level) VALUES (?, ?, ?)", (user_id_str, current_chars, current_level))
    conn.commit()

    if level_up_triggered:
        cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (guild_id_str,))
        levelup_row = cursor.fetchone()
        if levelup_row:
            notify_channel = bot.get_channel(int(levelup_row[0]))
            if notify_channel:
                rendered_msg = levelup_row[1].replace("{user.mention}", message.author.mention).replace("{user.name}", message.author.name).replace("{user.level}", str(current_level))
                try: await notify_channel.send(content=rendered_msg)
                except: pass

    conn.close()
    await bot.process_commands(message)

# =================================================================
# 🚀 8. RUN THE BOT
# =================================================================
bot.run(os.getenv("DISCORD_TOKEN"))
