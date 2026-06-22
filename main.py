import discord
from discord import app_commands
from discord.ext import commands, tasks
import random
import datetime
import os
import sqlite3

# =================================================================
# 📝 【手動自訂狀態列欄位】
# 你可以在這個陣列裡自由增加、刪除或修改機器人要輪播的「正在觀看」訊息。
# 每一條訊息請用雙引號包起來，並用逗號隔開。
# =================================================================
WATCHING_STATUSES = [
    "67",                  # 目前指定的初始狀態
    "/help",    # 你可以把這幾行刪掉，或自己加更多進去！
    "Six Seven",
    "Auto Mute"
]
# =================================================================

# 確保資料庫資料夾存在（用於 Railway 雲端硬碟掛載）
os.makedirs("data", exist_ok=True)
DB_PATH = "data/bot.db"

# --- 資料庫初始化 ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS welcome (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS levelup (guild_id TEXT PRIMARY KEY, channel_id TEXT, message TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, message TEXT, channel_id TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS levels (user_id TEXT PRIMARY KEY, chars INTEGER, level INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS mutes (guild_id TEXT, banned_word TEXT, duration_mins INTEGER, PRIMARY KEY (guild_id, banned_word))")
    conn.commit()
    conn.close()

init_db()

# --- 機器人基礎建構 ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.status_index = 0  # 用於紀錄目前輪播到第幾個狀態

    async def setup_hook(self):
        self.check_time_announcements.start()  # 啟動自動報時檢查
        self.rotate_status.start()             # 啟動 5 秒狀態輪播
        await self.tree.sync()
        print("🤖 正式版機器人已就緒，狀態輪播與斜線指令同步成功！")

    # ⏳ 5秒狀態輪播背景任務
    @tasks.loop(seconds=5)
    async def rotate_status(self):
        if not WATCHING_STATUSES:
            return

        # 防呆：如果手動刪除陣列導致索引溢出，立刻歸零
        if self.status_index >= len(WATCHING_STATUSES):
            self.status_index = 0

        # 取出當前要顯示的字串
        current_status = WATCHING_STATUSES[self.status_index]

        # 變更機器人狀態為「正在觀看 XXX」
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=current_status
            )
        )

        # 索引指向下一個狀態
        self.status_index += 1

    @rotate_status.before_loop
    async def before_rotate(self):
        await self.wait_until_ready()

    # ⏰ 24/7 背景報時巡邏（每 60 秒檢查一次）
    @tasks.loop(seconds=60)
    async def check_time_announcements(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message FROM announcements WHERE time = ?", (now_utc,))
        rows = cursor.fetchall()
        conn.close()

        for channel_id, message in rows:
            channel = self.get_channel(int(channel_id))
            if channel:
                embed = discord.Embed(title="⏰ 自動報時通知 (UTC+0)", description=message, color=discord.Color.blue())
                await channel.send(embed=embed)

    @check_time_announcements.before_loop
    async def before_check(self):
        await self.wait_until_ready()

bot = MyBot()

# --- UI 互動組件 (Modals & Views) ---

class ManualMsgModal(discord.ui.Modal, title="手動發送自訂訊息"):
    msg_input = discord.ui.TextInput(label="請輸入訊息內容", style=discord.TextStyle.paragraph)
    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📢 系統公告", description=self.msg_input.value, color=discord.Color.blue())
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("✅ 訊息已成功發送！", ephemeral=True)

class WelcomeModal(discord.ui.Modal, title="設定歡迎訊息內容"):
    def __init__(self, channel: discord.abc.GuildChannel):
        super().__init__()
        self.channel = channel
    msg_input = discord.ui.TextInput(label="歡迎訊息文字", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO welcome (guild_id, channel_id, message) VALUES (?, ?, ?)",
                       (str(interaction.guild_id), str(self.channel.id), self.msg_input.value))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ 歡迎頻道已設定為：{self.channel.mention}", ephemeral=True)

class LevelUpModal(discord.ui.Modal, title="自訂升級通知訊息"):
    def __init__(self, channel: discord.abc.GuildChannel):
        super().__init__()
        self.channel = channel
    msg_input = discord.ui.TextInput(label="輸入升級訊息內容", style=discord.TextStyle.paragraph, default="🎉 恭喜 {user.mention} 升級到了 **Lv. {user.level}**！")

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO levelup (guild_id, channel_id, message) VALUES (?, ?, ?)",
                       (str(interaction.guild_id), str(self.channel.id), self.msg_input.value))
        conn.commit()
        conn.close()
        
        test_rendered = self.msg_input.value.replace("{user.mention}", interaction.user.mention).replace("{user.name}", interaction.user.name).replace("{user.level}", "99")
        await interaction.response.send_message("✅ 升級通知設定成功！下方為預覽畫面：", ephemeral=True)
        await interaction.followup.send(content=f"👁️ **【測試預覽】**\n{test_rendered}", ephemeral=True)

class RemoveTimeSelect(discord.ui.Select):
    def __init__(self, options_list):
        options = [discord.SelectOption(label=f"[{item[1]}] {item[2][:20]}", value=str(item[0])) for item in options_list]
        super().__init__(placeholder="請選擇要刪除的報時指令...", options=options)

    async def callback(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM announcements WHERE id = ?", (self.values[0],))
        conn.commit()
        conn.close()
        await interaction.response.send_message("🗑️ 已成功刪除該報時項目。", ephemeral=True)

class RemoveTimeView(discord.ui.View):
    def __init__(self, options_list):
        super().__init__()
        self.add_item(RemoveTimeSelect(options_list))


# --- 斜線指令實作 ---

@bot.tree.command(name="help", description="顯示使用說明及指令")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 機器人指令清單 (正式版)", color=discord.Color.gold())
    cmds = ["/help - 顯示說明", "/setwelcome - 設定歡迎頻道", "/addtime - 新增報時(UTC+0)", "/removetime - 刪除報時", "/manualmsg - 代發訊息", "/random67 - 隨機67話題", "/level - 查等級", "/automute - 設禁字Mute", "/removeautomute - 解除禁字", "/setlevelup - 設升級通知"]
    embed.description = "\n".join(cmds)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setwelcome", description="設定歡迎訊息發送的頻道與內容")
async def setwelcome(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    await interaction.response.send_modal(WelcomeModal(channel))

@bot.tree.command(name="addtime", description="手動新增一條自動報時（+0時區）")
async def addtime(interaction: discord.Interaction, time: str, message: str):
    if ":" not in time or len(time) != 5:
        await interaction.response.send_message("❌ 格式錯誤，請輸入如 `08:00`", ephemeral=True)
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO announcements (time, message, channel_id) VALUES (?, ?, ?)", (time, message, str(interaction.channel_id)))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 報時已排定於 UTC {time} 發送。", ephemeral=True)

@bot.tree.command(name="removetime", description="刪除一條自動報時")
async def removetime(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, time, message FROM announcements")
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("❌ 目前沒有任何報時項目。", ephemeral=True)
        return
    await interaction.response.send_message("請選擇要刪除的項目：", view=RemoveTimeView(rows), ephemeral=True)

@bot.tree.command(name="manualmsg", description="手動發送一則自訂訊息")
async def manualmsg(interaction: discord.Interaction):
    await interaction.response.send_modal(ManualMsgModal())

@bot.tree.command(name="random67", description="隨機發送跟67有關的訊息")
async def random67(interaction: discord.Interaction):
    jokes = ["🤖 67 是個充滿魔力的數字。", "🔮 偵測到此頻道的幸運指數為 67 分！"]
    await interaction.response.send_message(random.choice(jokes))

@bot.tree.command(name="level", description="顯示使用者等級")
async def level(interaction: discord.Interaction, user: discord.Member = None):
    target_user = user or interaction.user
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (str(target_user.id),))
    row = cursor.fetchone()
    conn.close()
    
    chars, lvl = row if row else (0, 1)
    embed = discord.Embed(title=f"📊 {target_user.display_name} 的等級資訊", color=discord.Color.green())
    embed.add_field(name="目前等級", value=f"Lv. {lvl}", inline=True)
    embed.add_field(name="累積字數", value=f"{chars} 字", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="automute", description="自動偵測字詞並 mute（訊息不能是67）")
async def automute(interaction: discord.Interaction, message: str, time: int):
    if message == "67":
        await interaction.response.send_message("❌ 錯誤：不能將 '67' 設為禁字！", ephemeral=True)
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO mutes (guild_id, banned_word, duration_mins) VALUES (?, ?, ?)",
                   (str(interaction.guild_id), message, time))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🔒 禁字 `{message}` 防護已啟動，觸發將 Mute {time} 分鐘。", ephemeral=True)

@bot.tree.command(name="removeautomute", description="取消指定訊息自動 Mute")
async def removeautomute(interaction: discord.Interaction, message: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mutes WHERE guild_id = ? AND banned_word = ?", (str(interaction.guild_id), message))
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    if updated > 0:
        await interaction.response.send_message(f"✅ 已解除 `{message}` 的自動 Mute 防護。", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ 找不到該禁字設定。", ephemeral=True)

@bot.tree.command(name="setlevelup", description="設定成員升等訊息發送細節")
async def setlevelup(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    await interaction.response.send_modal(LevelUpModal(channel))


# --- 事件監聽 ---

@bot.event
async def on_member_join(member: discord.Member):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message FROM welcome WHERE guild_id = ?", (str(member.guild.id),))
    row = cursor.fetchone()
    conn.close()
    if row:
        channel = bot.get_channel(int(row[0]))
        if channel:
            text = row[1].replace("{user.mention}", member.mention).replace("{user.name}", member.name)
            await channel.send(text)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # A. 禁字攔截
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT banned_word, duration_mins FROM mutes WHERE guild_id = ?", (str(message.guild.id),))
    banned_words = cursor.fetchall()
    
    for word, mins in banned_words:
        if word in message.content:
            try:
                await message.author.timeout(datetime.timedelta(minutes=mins), reason="觸發禁字")
                await message.delete()
                await message.channel.send(f"🚫 {message.author.mention} 發表了禁字，已被自動隔離 {mins} 分鐘！")
                conn.close()
                return
            except discord.Forbidden:
                pass

    # B. 經驗值更新
    u_id = str(message.author.id)
    cursor.execute("SELECT chars, level FROM levels WHERE user_id = ?", (u_id,))
    row = cursor.fetchone()
    chars, lvl = row if row else (0, 1)
    
    level_up = False
    if message.content.strip() == "67":
        lvl += 1
        level_up = True
    else:
        chars += len(message.content)
        calc_lvl = (chars // 150) + 1
        if calc_lvl > lvl:
            lvl = calc_lvl
            level_up = True

    cursor.execute("INSERT OR REPLACE INTO levels (user_id, chars, level) VALUES (?, ?, ?)", (u_id, chars, lvl))
    conn.commit()

    # C. 升級通知（已修改：未經設定不發送任何訊息）
    if level_up:
        cursor.execute("SELECT channel_id, message FROM levelup WHERE guild_id = ?", (str(message.guild.id),))
        l_row = cursor.fetchone()
        if l_row:
            ch = bot.get_channel(int(l_row[0]))
            if ch:
                tx = l_row[1].replace("{user.mention}", message.author.mention).replace("{user.name}", message.author.name).replace("{user.level}", str(lvl))
                await ch.send(tx)

    conn.close()
    await bot.process_commands(message)

# 啟動
bot.run(os.getenv("DISCORD_TOKEN"))
