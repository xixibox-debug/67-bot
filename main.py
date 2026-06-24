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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, 
            w_title TEXT, w_desc TEXT, g_title TEXT, g_desc TEXT
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS levelup (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, message TEXT, channel_id TEXT)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS levels (
            user_id TEXT PRIMARY KEY, 
            xp INTEGER DEFAULT 0, 
            level INTEGER DEFAULT 1, 
            count_67 INTEGER DEFAULT 0
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS mutes (guild_id TEXT, banned_word TEXT, duration_str TEXT, PRIMARY KEY (guild_id, banned_word))")
    conn.commit()
    conn.close()

init_db()

# =================================================================
# 🔄 3. CORE UTILITIES (核心工具函式與變數解析)
# =================================================================
def get_xp_needed(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100

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
class SixSevenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.status_index = 0
        self.invites = {}
        self.last_announced_minute = ""

    async def setup_hook(self):
        self.rotate_status.start()
        self.check_time_announcements.start()
        await self.tree.sync()

    @tasks.loop(seconds=5)
    async def rotate_status(self):
        self.status_index = (self.status_index + 1) % len(WATCHING_STATUSES)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=WATCHING_STATUSES[self.status_index]))

    @tasks.loop(seconds=30)
    async def check_time_announcements(self):
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
                channel = self.get_channel(int(cid)) or await self.fetch_channel(int(cid))
                if channel: await channel.send(msg)

bot = SixSevenBot()

# =================================================================
# 🖥️ 5. INTERACTIVE UI (MODALS & VIEWS)
# =================================================================

class ManualMsgModal(ui.Modal, title="Send Manual Message"):
    text = ui.TextInput(label="Message Content", style=discord.TextStyle.paragraph, required=True, placeholder="Type your text here...")
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.channel.send(self.text.value)
        await interaction.response.send_message("✅ Raw text message sent successfully.", ephemeral=True)


# 🛠️ 修正點：縮短 Label 長度至 45 字元內，防範 Discord API 噴出 400 錯誤（對應圖 5）
class WelcomeGoodbyeModal(ui.Modal, title="Set Welcome Message"):
    def __init__(self, cid: str = None, w_t: str = None, w_d: str = None, g_t: str = None, g_d: str = None):
        super().__init__()
        self.channel = ui.TextInput(label="Select Channel *", default=cid if cid else "Channel 1", required=True)
        self.w_title = ui.TextInput(label="Welcome Embed Title", default=w_t if w_t else "Hey, welcome to {guild.name}!!!", required=False)
        self.w_desc = ui.TextInput(label="Welcome Embed Description *", default=w_d if w_d else "You are the {member.count} member here!\nInviter: {inviter.name}", required=True, style=discord.TextStyle.long)
        self.g_title = ui.TextInput(label="Goodbye Embed Title", default=g_t if g_t else "{user.name} has leave the server", required=False)
        self.g_desc = ui.TextInput(label="Goodbye Embed Description *", default=g_d if g_d else "Whyyyyyy u leave us?????", required=True, style=discord.TextStyle.long)

        self.add_item(self.channel)
        self.add_item(self.w_title)
        self.add_item(self.w_desc)
        self.add_item(self.g_title)
        self.add_item(self.g_desc)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO welcome VALUES (?, ?, ?, ?, ?, ?)", (str(interaction.guild_id), self.channel.value, self.w_title.value, self.w_desc.value, self.g_title.value, self.g_desc.value))
        conn.commit(); conn.close()
        w_t = parse_placeholders(self.w_title.value, interaction.user, interaction.guild)
        w_d = parse_placeholders(self.w_desc.value, interaction.user, interaction.guild)
        embed = discord.Embed(title=w_t, description=w_d, color=interaction.user.color)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.send_message(content="✅ **Embed Text Content Saved!** Preview:", embed=embed, ephemeral=True)


class WelcomeConfigView(ui.View):
    def __init__(self): super().__init__(timeout=300)
    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="🎯 Select Welcome Alert Channel")
    async def set_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        cid = select.values[0].id
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT w_title, w_desc, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone()
        if row: cursor.execute("UPDATE welcome SET channel_id = ? WHERE guild_id = ?", (str(cid), str(interaction.guild_id)))
        else: cursor.execute("INSERT INTO welcome VALUES (?, ?, '', '', '', '')", (str(interaction.guild_id), str(cid)))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"🎯 Target log channel set to {select.values[0].mention}", ephemeral=True)

    @ui.button(label="📝 Edit Cards (Modal)", style=discord.ButtonStyle.primary)
    async def edit_msg(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, w_title, w_desc, g_title, g_desc FROM welcome WHERE guild_id = ?", (str(interaction.guild_id),))
        row = cursor.fetchone(); conn.close()
        if row: await interaction.response.send_modal(WelcomeGoodbyeModal(row[0], row[1], row[2], row[3], row[4]))
        else: await interaction.response.send_modal(WelcomeGoodbyeModal())

    @ui.button(label="❌ Disable / Reset Panel", style=discord.ButtonStyle.danger)
    async def reset_panel(self, interaction: discord.Interaction, button: ui.Button):
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM welcome WHERE guild_id = ?", (str(interaction.guild_id),))
        conn.commit(); conn.close()
        await interaction.response.send_message("🗑️ Welcome/Goodbye feature disabled.", ephemeral=True)

    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message")
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=SettingsView())


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
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send(f"🔒 Banned word `{self.word.value}` added.", ephemeral=True)


class BannedWordDeleteSelect(ui.Select):
    def __init__(self): super().__init__(placeholder="🗑️ Select a word to CANCEL / REMOVE rule", min_values=1, max_values=1, options=[discord.SelectOption(label="Placeholder", value="none")], row=1)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return await interaction.response.defer()
        word = self.values[0]
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM mutes WHERE guild_id = ? AND banned_word = ?", (str(interaction.guild_id), word))
        conn.commit(); conn.close()
        self.view.update_select_menu()
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send(f"✅ Removed rule for: `{word}`", ephemeral=True)


class AutoMuteConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.select_menu = BannedWordDeleteSelect()
        self.add_item(self.select_menu)
        self.update_select_menu()

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

    @ui.button(label="➕ Add Banned Word", style=discord.ButtonStyle.success, row=0)
    async def add_word(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(AutoMuteModal(self))
    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message")
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=SettingsView())


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
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send(f"⏰ Announcement scheduler set at {self.t_time.value}", ephemeral=True)


class TimeMessageDeleteSelect(ui.Select):
    def __init__(self): super().__init__(placeholder="🗑️ Select an announcement schedule to CANCEL", min_values=1, max_values=1, options=[discord.SelectOption(label="Placeholder", value="none")], row=1)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return await interaction.response.defer()
        rid = self.values[0]
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("DELETE FROM announcements WHERE id = ?", (rid,))
        conn.commit(); conn.close()
        self.view.update_select_menu()
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send("✅ Scheduled announcement has been cancelled.", ephemeral=True)


class TimeMessageConfigView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.select_menu = TimeMessageDeleteSelect()
        self.add_item(self.select_menu)
        self.update_select_menu()

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

    @ui.button(label="⏰ Add Time Message", style=discord.ButtonStyle.success, row=0)
    async def add_time(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(AnnouncementModal(self))
    @ui.button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message")
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=SettingsView())


class SettingsView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="Welcome/Goodbye Panel", style=discord.ButtonStyle.secondary, emoji="👋")
    async def btn_w(self, interaction: discord.Interaction, btn: ui.Button):
        embed = discord.Embed(title="👋 Welcome & Goodbye Settings", color=0x54a7dd, description="請先在下方下拉選單選擇發送頻道，再點擊按鈕編輯自訂卡片內容。")
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=WelcomeConfigView())
        
    @ui.button(label="Level System", style=discord.ButtonStyle.secondary, emoji="🎉")
    async def btn_l(self, interaction: discord.Interaction, btn: ui.Button):
        await interaction.response.send_message("📈 **Level System Configuration**", view=LevelSettingsView(), ephemeral=True)
        
    @ui.button(label="Auto Mute", style=discord.ButtonStyle.secondary, emoji="🔒")
    async def btn_a(self, interaction: discord.Interaction, btn: ui.Button):
        embed = discord.Embed(title="🔒 Auto Mute Filter Config Hub", color=0xff0000, description="Create text filtering parameters or remove existing configurations below.")
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=AutoMuteConfigView(interaction.guild_id))
        
    @ui.button(label="Time Message", style=discord.ButtonStyle.secondary, emoji="⏰")
    async def btn_t(self, interaction: discord.Interaction, btn: ui.Button):
        embed = discord.Embed(title="⏰ Time Message Alerts Hub", color=0x3498db, description="Schedule timed standard text warnings or clear past records below.")
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=TimeMessageConfigView(interaction.guild_id))

# =================================================================
# 🚀 6. SLASH COMMANDS
# =================================================================

@bot.tree.command(name="help", description="Show help menu")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Help Menu", color=discord.Color.gold())
    embed.add_field(name="Admin Commands", value="`/settings`, `/mute`, `/unmute`, `/kick`, `/setlevel`, `/manualmsg`")
    embed.add_field(name="User Commands", value="`/level`, `/random67`, `/help`")
    embed.set_footer(text=f"{interaction.guild.name} | 67" if interaction.guild else "67")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="settings", description="Open bot configuration hub")
@app_commands.checks.has_permissions(manage_guild=True)
async def settings(interaction: discord.Interaction):
    embed = discord.Embed(title="Settings", color=0xdfe600, description="Welcome/Goodbye Panel\nLevel System\nAuto Mute\nTime Message")
    embed.set_footer(text=f"{interaction.guild.name} | 67")
    await interaction.response.send_message(embed=embed, view=SettingsView())

@bot.tree.command(name="manualmsg", description="Send manual text message as bot")
async def manualmsg(interaction: discord.Interaction): await interaction.response.send_modal(ManualMsgModal())

# 🛠️ 修正點：對應圖 2 之 Mute 嵌入卡片（綠色邊框 + 變數渲染）
@bot.tree.command(name="mute", description="Timeout a server member")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, time: str, reason: Optional[str] = "None"):
    delta, err = parse_mute_duration(time)
    if err: return await interaction.response.send_message(err, ephemeral=True)
    await user.timeout(delta, reason=reason)
    embed = discord.Embed(
        title=parse_placeholders("✅ {user.name} has been muted.", user, interaction.guild), 
        color=0x2ecc71, 
        description=f"Time: {time}\nReason: {reason}"
    )
    embed.set_footer(text=f"{interaction.guild.name} | 67" if interaction.guild else "67")
    await interaction.response.send_message(embed=embed)

# 🛠️ 修正點：對應圖 2 之 Unmute 嵌入卡片（綠色邊框 + 變數渲染）
@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    await user.timeout(None)
    embed = discord.Embed(
        title=parse_placeholders("✅ {user.name} has been unmuted.", user, interaction.guild), 
        color=0x2ecc71
    )
    embed.set_footer(text=f"{interaction.guild.name} | 67" if interaction.guild else "67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="kick", description="Kick a member from server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = "None"):
    if user.id == interaction.guild.owner_id: return await interaction.response.send_message("❌ 無法對伺服器擁有者執行踢出處分！", ephemeral=True)
    if user.id == bot.user.id: return await interaction.response.send_message("❌ 你不能叫我踢出我自己！", ephemeral=True)
    try:
        await user.kick(reason=reason)
        embed = discord.Embed(title=parse_placeholders("✅ {user.name} has been kicked.", user, interaction.guild), color=0xe74c3c, description=parse_placeholders("Reason: {reason}", user, interaction.guild, extra={"reason": reason}))
        embed.set_footer(text=f"{interaction.guild.name} | 67")
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ **踢出失敗！** 機器人的身分組階級不夠高，或缺少「踢出成員」權限。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 發生未知錯誤：{e}", ephemeral=True)

@kick.error
async def kick_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions): await interaction.response.send_message("❌ 你沒有「踢出成員」的權限！", ephemeral=True)


@bot.tree.command(name="setlevel", description="Manually set a member's level (supports upgrades & downgrades)")
@app_commands.checks.has_permissions(administrator=True)
async def setlevel(interaction: discord.Interaction, user: discord.Member, level: int):
    if level < 1: return await interaction.response.send_message("❌ 等級不能小於 1！", ephemeral=True)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT level, count_67 FROM levels WHERE user_id = ?", (str(user.id),))
    row = cursor.fetchone()
    old_lvl, current_67 = row if row else (1, 0)
    
    cursor.execute("INSERT OR REPLACE INTO levels (user_id, xp, level, count_67) VALUES (?, ?, ?, ?)", (str(user.id), 0, level, current_67))
    conn.commit()
    await interaction.response.send_message(f"✅ 已將 {user.name} 的等級調整為 Lv. {level}", ephemeral=True)
    
    cursor.execute("SELECT channel_id FROM levelup WHERE guild_id = ?", (str(interaction.guild.id),))
    lrow = cursor.fetchone()
    conn.close()
    
    if lrow and lrow[0]:
        try:
            chan = bot.get_channel(int(lrow[0])) or await bot.fetch_channel(int(lrow[0]))
            if chan:
                is_upgrade = level > old_lvl
                title_text = "📈 **管理員手動灌水通知**" if is_upgrade else "📉 **管理員手動降級通知**"
                embed_color = 0x2ecc71 if is_upgrade else 0xe74c3c
                
                embed = discord.Embed(
                    title=title_text,
                    description=f"管理員調整了 {user.mention} 的活動等級狀態：\n\n• 原本等級：`Lv. {old_lvl}`\n• 目前等級：`Lv. {level}`\n• 剩餘進度：`0 / {get_xp_needed(level)} XP`",
                    color=embed_color
                )
                embed.set_footer(text=f"{interaction.guild.name} | 67")
                await chan.send(embed=embed)
        except Exception as e:
            logger.error(f"[setlevel 公告發送出錯]: {e}")


# 🛠️ 修正點：完全遵循圖 6 藍圖重製的 /level 面板，無任何自創欄位或隱藏修改
@bot.tree.command(name="level", description="Check current activity stats and 67 counts")
async def level(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT xp, level, count_67 FROM levels WHERE user_id = ?", (str(target.id),))
    row = cursor.fetchone()
    
    xp, lvl, count_67 = row if row else (0, 1, 0)
    xp_needed = get_xp_needed(lvl)
    
    # 📈 高效率全服即時排名計算
    cursor.execute("SELECT COUNT(*) FROM levels WHERE level > ? OR (level = ? AND xp > ?)", (lvl, lvl, xp))
    level_rank = cursor.fetchone()[0] + 1
    
    cursor.execute("SELECT COUNT(*) FROM levels WHERE count_67 > ?", (count_67,))
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
    
    embed.set_footer(text=f"{interaction.guild.name} | 67" if interaction.guild else "67")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="random67", description="Get lucky 67 message")
async def random67(interaction: discord.Interaction):
    jokes = ["67 is magic!", "Luck factor: 67", "Spirit of Six Seven!"]
    await interaction.response.send_message(random.choice(jokes))

# =================================================================
# ⚡ 7. SYSTEM EVENTS
# =================================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for guild in bot.guilds:
        try: bot.invites[guild.id] = await guild.invites()
        except: pass

@bot.event
async def on_member_join(member: discord.Member):
    if member.bot: return
    try:
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT channel_id, w_title, w_desc FROM welcome WHERE guild_id = ?", (str(member.guild.id),))
        row = cursor.fetchone(); conn.close()
        if row and row[0]:
            try:
                channel_id = int(row[0])
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            except: return
            if channel:
                title = parse_placeholders(row[1] or "Welcome!", member, member.guild)
                desc = parse_placeholders(row[2], member, member.guild)
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
                embed.set_footer(text=f"{member.guild.name} | 67")
                await channel.send(content=member.mention, embed=embed)
    except Exception as e: logger.error(f"[on_member_join 崩潰]: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot: return
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
                embed.set_footer(text=f"{member.guild.name} | 67")
                await channel.send(embed=embed)
    except Exception as e: logger.error(f"[on_member_remove 崩潰]: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild: return

    cleaned = re.sub(r'<@!?\d+>|<@&\d+>|<#\d+>|<a?:[a-zA-Z0-9_]+:\d+>|<t:\d+(?::[a-zA-Z])?>', '', message.content)
    occurrences = cleaned.count("67") + cleaned.count("6️⃣7️⃣")
    
    if occurrences > 0:
        await message.reply(f"# {message.author.mention} 67!!!!!")

    # 🔒 檢查自動禁言黑名單（對應圖 1 之觸發警告卡片，紅色邊框 + 變數渲染）
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
                    description=f'{message.author.name} has been muted for {dur} due to he/she sent the message "{message.content}", you can try and be the next!'
                )
                embed.set_footer(text=f"{message.guild.name} | 67")
                await message.channel.send(embed=embed)
                conn.close()
                return
            except: pass

    # 📈 經驗值與計數更新
    uid = str(message.author.id)
    cursor.execute("SELECT xp, level, count_67 FROM levels WHERE user_id = ?", (uid,))
    row = cursor.fetchone()
    xp, lvl, count_67 = row if row else (0, 1, 0)
    
    if occurrences > 0:
        count_67 += occurrences
        xp += (occurrences * 20) + random.randint(10, 25)
    else:
        xp += random.randint(10, 25)
        
    new_lvl = lvl
    while xp >= get_xp_needed(new_lvl):
        xp -= get_xp_needed(new_lvl)
        new_lvl += 1
        
    cursor.execute("INSERT OR REPLACE INTO levels (user_id, xp, level, count_67) VALUES (?, ?, ?, ?)", (uid, xp, new_lvl, count_67))
    conn.commit()

    if new_lvl > lvl:
        cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (str(message.guild.id),))
        lrow = cursor.fetchone()
        if lrow and lrow[0]:
            try:
                chan = bot.get_channel(int(lrow[0])) or await bot.fetch_channel(int(lrow[0]))
                if chan:
                    txt = parse_placeholders(lrow[1], message.author, message.guild, extra={"level": new_lvl})
                    await chan.send(txt)
            except: pass
            
    conn.close()

# =================================================================
# 🔑 8. RUN BOT
# =================================================================
bot.run(os.getenv("DISCORD_TOKEN"))
