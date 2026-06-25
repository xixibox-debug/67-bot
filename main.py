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
import google.generativeai as genai

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
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy (
            user_id TEXT PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            last_daily TEXT DEFAULT '',
            last_work INTEGER DEFAULT 0,
            last_pay INTEGER DEFAULT 0,
            last_rob INTEGER DEFAULT 0
        )
    """)
    # 🎯 新增：建立等級身分組獎勵配置表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS level_roles (
            guild_id TEXT,
            level INTEGER,
            role_id TEXT,
            PRIMARY KEY (guild_id, level)
        )
    """)
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
                logger.error(f"[身分組發放失敗]: 權限不足，無法給予 {member.name} 身分組 {role.name}")


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
        
    @ui.button(label="🎭 設置等級身分組", style=discord.ButtonStyle.blurple, row=2)
    async def go_level_role(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="🎭 等級身分組獎勵設定", 
            description="請先在下方選單**選擇一個身分組**，隨後系統會彈出視窗請你輸入**指定的解鎖等級**。", 
            color=0x2b2d31
        )
        await interaction.response.edit_message(embed=embed, view=LevelRoleSettingsView(self))

    @ui.button(label="Modify Level Message", style=discord.ButtonStyle.success)
    async def mod_text(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LevelMessageModal())


class LevelRoleModal(ui.Modal, title="設定等級身分組獎勵"):
    level_input = ui.TextInput(label="達到幾等給予此身分組？", placeholder="例如：10", min_length=1, max_length=3)

    def __init__(self, role: discord.Role, parent_view):
        super().__init__()
        self.role = role
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            lvl = int(self.level_input.value)
            if lvl < 1: raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ 請輸入大於 0 的有效整數！", ephemeral=True)

        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?)", 
                       (str(interaction.guild.id), lvl, str(self.role.id)))
        conn.commit(); conn.close()
        await interaction.response.send_message(f"✅ 設定成功！成員達到 **Lv. {lvl}** 時將自動獲得 {self.role.mention}", ephemeral=True)

class LevelRoleSettingsView(ui.View):
    def __init__(self, original_view):
        super().__init__(timeout=60)
        self.original_view = original_view

    @ui.select(cls=ui.RoleSelect, placeholder="請選擇要綁定的身分組...", min_values=1, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: ui.RoleSelect):
        await interaction.response.send_modal(LevelRoleModal(select.values[0], self))

    @ui.button(label="⬅️ 返回等級設定", style=discord.ButtonStyle.gray)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="⚙️ Level System Configuration", description="請選擇你要調整的等級系統設定：", color=0x2b2d31)
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


@bot.tree.command(name="setlevel", description="Manually set a member's level")
@app_commands.checks.has_permissions(administrator=True)
async def setlevel(interaction: discord.Interaction, user: discord.Member, level: int):
    if level < 1: return await interaction.response.send_message("❌ 等級不能小於 1！", ephemeral=True)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT count_67 FROM levels WHERE user_id = ?", (str(user.id),))
    row = cursor.fetchone()
    current_67 = row[0] if row else 0
    
    # 變更等級，並將目前 XP 歸零重算
    cursor.execute("INSERT OR REPLACE INTO levels (user_id, xp, level, count_67) VALUES (?, ?, ?, ?)", (str(user.id), 0, level, current_67))
    conn.commit()
    
    # 回應操作的管理員（僅限管理員看見）
    await interaction.response.send_message(f"✅ 已將 {user.name} 的等級調整為 Lv. {level}", ephemeral=True)
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
@bot.tree.command(name="level", description="Check current activity stats and 67 counts")
async def level(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT xp, level, count_67 FROM levels WHERE user_id = ?", (str(target.id),))
    row = cursor.fetchone()
    
    xp, lvl, count_67 = row if row else (0, 1, 0)
    is_admin = target.guild_permissions.administrator if isinstance(target, discord.Member) else False
    xp_needed = get_xp_needed(lvl, is_admin)
    
    # 📈 高效率全服即時排名計算

    
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
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        if self.mode == "balance":
            self.mode = "leaderboard"
            cursor.execute("SELECT user_id, balance FROM economy ORDER BY balance DESC LIMIT 10")
            rows = cursor.fetchall(); conn.close()
            
            desc = ""
            for idx, (uid, bal) in enumerate(rows, 1):
                user = bot.get_user(int(uid))
                name = user.name if user else f"User {uid}"
                desc += f"{idx}. **{name}**: ${bal}\n"
            
            embed = discord.Embed(title=f"🏆 {self.guild.name} Leaderboard", description=desc or "No data available.", color=0xffa500)
        else:
            self.mode = "balance"
            cursor.execute("SELECT balance FROM economy WHERE user_id = ?", (str(self.target.id),))
            row = cursor.fetchone(); conn.close()
            bal = row[0] if row else 0
            
            embed = discord.Embed(
                title=f"{self.target.name}'s balance",
                color=0xffa500,
                description=f"💰 Balance\n**${bal}**"
            )
            
        embed.set_footer(text=f"{self.guild.name} | 67")
        await interaction.response.edit_message(embed=embed, view=self)

def ensure_eco_user(user_id: str):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance, last_daily, last_work, last_pay, last_rob FROM economy WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO economy (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.commit()
        row = (0, '', 0, 0, 0)
    conn.close()
    return row

@bot.tree.command(name="ecodaily", description="Claim your daily reward")
async def ecodaily(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ensure_eco_user(uid)
    
    tz = datetime.timezone(datetime.timedelta(hours=0))
    current_day = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT last_daily FROM economy WHERE user_id = ?", (uid,))
    last_daily = cursor.fetchone()[0]
    
    if last_daily == current_day:
        conn.close()
        return await interaction.response.send_message("❌ You have already claimed your daily reward today! (Resets at UTC+8 midnight)", ephemeral=True)
        
    cursor.execute("UPDATE economy SET balance = balance + 100, last_daily = ? WHERE user_id = ?", (current_day, uid))
    conn.commit(); conn.close()
    
    embed = discord.Embed(
        title="Daily Reward",
        color=0x00ffff,
        description="You claimed **$100** daily reward !"
    )
    embed.set_footer(text=f"{interaction.guild.name} | 67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecowork", description="Go to work and earn money")
async def ecowork(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ensure_eco_user(uid)
    now = int(datetime.datetime.now().timestamp())
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT last_work FROM economy WHERE user_id = ?", (uid,))
    last_work = cursor.fetchone()[0]
    
    if now - last_work < 3600:
        conn.close()
        rem = 3600 - (now - last_work)
        return await interaction.response.send_message(f"❌ You are exhausted! Please wait {rem // 60}m {rem % 60}s before working again.", ephemeral=True)
        
    success = random.random() > 0.1
    if success:
        amount = random.randint(200, 2000)
        cursor.execute("UPDATE economy SET balance = balance + ?, last_work = ? WHERE user_id = ?", (amount, now, uid))
        embed = discord.Embed(
            title="Work",
            color=0x00ffff,
            description=f"You **help ur neighbor walked the dog** and earned **${amount}** !"
        )
    else:
        amount = random.randint(50, 100)
        cursor.execute("UPDATE economy SET balance = MAX(0, balance - ?), last_work = ? WHERE user_id = ?", (amount, now, uid))
        embed = discord.Embed(
            title="Work",
            color=0xff6b6b,
            description=f"You **run the red light while delivering the package** and losted **${amount}** !"
        )
        
    conn.commit(); conn.close()
    embed.set_footer(text=f"{interaction.guild.name} | 67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecopay", description="Pay money to another user")
async def ecopay(interaction: discord.Interaction, user: discord.Member, value: int):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("❌ You cannot pay money to yourself!", ephemeral=True)
    if value <= 0:
        return await interaction.response.send_message("❌ Payment amount must be positive!", ephemeral=True)
        
    uid = str(interaction.user.id)
    tid = str(user.id)
    ensure_eco_user(uid)
    ensure_eco_user(tid)
    now = int(datetime.datetime.now().timestamp())
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance, last_pay FROM economy WHERE user_id = ?", (uid,))
    bal, last_pay = cursor.fetchone()
    
    if now - last_pay < 3600:
        conn.close()
        rem = 3600 - (now - last_pay)
        return await interaction.response.send_message(f"❌ Bank transfers are throttled. Wait {rem // 60}m {rem % 60}s.", ephemeral=True)
    if bal < value:
        conn.close()
        return await interaction.response.send_message(f"❌ Insufficient funds! You only have ${bal}.", ephemeral=True)
        
    # 邏輯設定抽成 5%，但配合卡片顯示 10% 格式，完美呈現
    tax = int(value * 0.05)
    net_value = value - tax
    
    cursor.execute("UPDATE economy SET balance = balance - ? , last_pay = ? WHERE user_id = ?", (value, now, uid))
    cursor.execute("UPDATE economy SET balance = balance + ? WHERE user_id = ?", (net_value, tid))
    conn.commit(); conn.close()
    
    embed = discord.Embed(
        title="Pay",
        color=0x00ffff,
        description=f"You successfully paid **{user.mention}** with **${net_value}** !\n(U need to pay 10% tax)"
    )
    embed.set_footer(text=f"{interaction.guild.name} | 67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecorob", description="Attempt to rob money from another user")
async def ecorob(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("❌ You cannot rob yourself!", ephemeral=True)
        
    uid = str(interaction.user.id)
    tid = str(user.id)
    ensure_eco_user(uid)
    ensure_eco_user(tid)
    now = int(datetime.datetime.now().timestamp())
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance, last_rob FROM economy WHERE user_id = ?", (uid,))
    my_bal, last_rob = cursor.fetchone()
    
    if now - last_rob < 3600:
        conn.close()
        rem = 3600 - (now - last_rob)
        return await interaction.response.send_message(f"❌ You are laying low. Try robbing again in {rem // 60}m {rem % 60}s.", ephemeral=True)
        
    cursor.execute("SELECT balance FROM economy WHERE user_id = ?", (tid,))
    target_bal = cursor.fetchone()[0]
    
    if target_bal <= 0:
        conn.close()
        return await interaction.response.send_message("❌ That user is completely broke! Nothing worth stealing.", ephemeral=True)
        
    success = random.random() < (1 / 3)
    rate = random.uniform(0.1, 0.25)
    
    if success:
        amount = int(target_bal * rate)
        cursor.execute("UPDATE economy SET balance = balance + ?, last_rob = ? WHERE user_id = ?", (amount, now, uid))
        cursor.execute("UPDATE economy SET balance = MAX(0, balance - ?) WHERE user_id = ?", (amount, tid))
        embed = discord.Embed(
            title="Rob",
            color=0x00ffff,
            description=f"You successfully rob **${amount}** from **{user.mention}**"
        )
    else:
        amount = int(my_bal * rate) if my_bal > 0 else random.randint(50, 200)
        cursor.execute("UPDATE economy SET balance = MAX(0, balance - ?), last_rob = ? WHERE user_id = ?", (amount, now, uid))
        embed = discord.Embed(
            title="Rob",
            color=0xff6b6b,
            description=f"The police showed up and u losted **${amount}**"
        )
        
    conn.commit(); conn.close()
    embed.set_footer(text=f"{interaction.guild.name} | 67")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ecobalance", description="Check account balance or view top rank leaderboard")
async def ecobalance(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    uid = str(target.id)
    ensure_eco_user(uid)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT balance FROM economy WHERE user_id = ?", (uid,))
    bal = cursor.fetchone()[0]
    conn.close()
    
    embed = discord.Embed(
        title=f"{target.name}'s balance",
        color=0xffa500,
        description=f"💰 Balance\n**${bal}**"
    )
    embed.set_footer(text=f"{interaction.guild.name} | 67")
    
    view = EcoBalanceView(target, interaction.guild)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="setbalance", description="Admin command to modify user balance")
@app_commands.checks.has_permissions(administrator=True)
async def setbalance(interaction: discord.Interaction, user: discord.Member, value: int):
    if value < 0:
        return await interaction.response.send_message("❌ Balance cannot be negative!", ephemeral=True)
    uid = str(user.id)
    ensure_eco_user(uid)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("UPDATE economy SET balance = ? WHERE user_id = ?", (value, uid))
    conn.commit(); conn.close()
    
    await interaction.response.send_message(f"💵 Successfully set {user.name}'s balance to **${value}**.", ephemeral=True)

@bot.tree.command(name="addrole", description="Manually add a role to a user")
@app_commands.checks.has_permissions(administrator=True)
async def addrole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if role in user.roles:
        return await interaction.response.send_message(f"❌ {user.mention} 已經擁有 {role.name} 身分組了！", ephemeral=True)
    try:
        await user.add_roles(role)
        await interaction.response.send_message(f"✅ 已成功將身分組 {role.mention} 給予 {user.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 機器人權限不足！請確認機器人的最高身分組階層「高於」你想操作的身分組。", ephemeral=True)

@bot.tree.command(name="removerole", description="Manually remove a role from a user")
@app_commands.checks.has_permissions(administrator=True)
async def removerole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if role not in user.roles:
        return await interaction.response.send_message(f"❌ {user.mention} 本來就沒有 {role.name} 身分組！", ephemeral=True)
    try:
        await user.remove_roles(role)
        await interaction.response.send_message(f"✅ 已成功將 {user.mention} 的身分組 {role.mention} 移除", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 機器人權限不足，無法移除該身分組！", ephemeral=True)

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
    # 排除機器人自己的訊息與私訊
    if message.author.bot or not message.guild: return

    # 🎯 24/7 被標記監聽器（極度厭世英文回覆）
    if bot.user.mentioned_in(message) and not message.mention_everyone:
        annoyed_phrases = [
            "Why are you even pinging me? Go away.",
            "Don't @ me for no reason. I'm exhausted.",
            "What do you want now? Stop messing with me.",
            "Pinged me for what? Just let me exist in peace.",
            "Unless the server is literally burning down, don't @ me."
        ]
        await message.reply(random.choice(annoyed_phrases))
        return  # 被標記後直接敷衍回覆並結束，防止連續觸發後續的 67 造成洗頻

# =================================================================
    # 📈 LEVEL SYSTEM & AUTO ROLE DISPATCHER
    # =================================================================
    uid = str(message.author.id)
    gid = str(message.guild.id)
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    # 取得使用者當前的經驗值與等級 (假設表名為 levels)
    cursor.execute("CREATE TABLE IF NOT EXISTS levels (guild_id TEXT, user_id TEXT, xp INTEGER, level INTEGER, PRIMARY KEY (guild_id, user_id))")
    cursor.execute("SELECT xp, level FROM levels WHERE guild_id = ? AND user_id = ?", (gid, uid))
    row = cursor.fetchone()
    
    if row:
        xp, lvl = row[0], row[1]
    else:
        xp, lvl = 0, 0

    # 隨機增加 15 ~ 25 點經驗值
    xp_gained = random.randint(15, 25)
    new_xp = xp + xp_gained
    new_lvl = lvl
    
    # 透過你原本定義的 get_xp_needed 函式計算是否升等
    while new_xp >= get_xp_needed(new_lvl):
        new_xp -= get_xp_needed(new_lvl)
        new_lvl += 1
        
    # 🎯 核心功能：當等級發生變化時，依序補發身分組
    if new_lvl > lvl:
        for l in range(lvl + 1, new_lvl + 1):
            await check_level_roles(message.author, l)
            
        # 讀取並發送你透過 /settings 設定的升等通知訊息
        cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (gid,))
        lvl_row = cursor.fetchone()
        if lvl_row:
            channel = message.guild.get_channel(int(lvl_row[0]))
            msg_template = lvl_row[1]
            if channel:
                # 替換通知內容中的變數
                announce_msg = msg_template.replace("{level}", str(new_lvl)).replace("{user}", message.author.mention)
                await channel.send(announce_msg)

    # 將最新的經驗與等級數據同步回資料庫
    cursor.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)", (gid, uid, new_xp, new_lvl))
    conn.commit(); conn.close()

    # -------------------------------------------------------------
    # 原始邏輯：過濾標記並檢查 "67"
    # -------------------------------------------------------------
    cleaned = re.sub(r'<@!?\d+>|<@&\d+>|<#\d+>|<a?:[a-zA-Z0-9_]+:\d+>|<t:\d+(?::[a-zA-Z])?>', '', message.content)
    occurrences = cleaned.count("67") + cleaned.count("6️⃣7️⃣")
    
    if occurrences > 0:
        await message.reply(f"# {message.author.mention} 67!!!!!")

    # 🔒 檢查自動禁言黑名單
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
    is_admin = message.author.guild_permissions.administrator if isinstance(message.author, discord.Member) else False
    while xp >= get_xp_needed(new_lvl, is_admin):
        xp -= get_xp_needed(new_lvl, is_admin)
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
