import gc
import logging
import os
import re
import asyncio
import discord
from discord import ui
from aiohttp import web
import aiohttp

# 啟動 Python 強制垃圾回收機制
gc.enable()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OrderBot")

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "8080"))  # Render 會自動注入 PORT 變數

TARGET_GUILD_ID = 1458114486442524904
TARGET_FORUM_ID = 1496743164713766952
APPROVER_ID = 1137258253949079683  
RECURRING_TAG_ID = 1525702398205759488  

TAG_CONFIG = {
    1496744588751274057: (
        "Pls check the latest delivery time at"
        " https://discord.com/channels/1458114486442524904/1501825678822084618"
        " first.\n\n<@&1505934626202452079>，有人要買飛機啦"
    ),
    1496883730068013247: "<@936410788242001970>，有人要賣飛機啦",
    1496883628523917464: "<@&1513789778225659904>, do you guys want to help?",
}

EXTRA_INSTRUCTIONS = (
    "\n\n- 如果您願意協助銷售/代銷，請按下` 接手 ` If you are willing to assist with"
    " sales/reselling, please press ` Take Over `.\n- 如果訂單已完成，請按下` 已完成"
    " ` If the order is completed, please press ` Completed `.\n- 如果要轉成定期定額，請按下`"
    " 轉成定期定額 Change to Recurring Investing `"
)

def clean_thread_name(name: str) -> str:
    return re.sub(r"^\[(Complete|Weekly|Monthly|Cancelled)\]\s*", "", name, flags=re.IGNORECASE)

# =================================================================
# 🛒 UI 視圖元件 (保持不變)
# =================================================================
class ChangeFrequencySelect(ui.Select):
    def __init__(self, target_message: discord.Message):
        options = [
            discord.SelectOption(label="Weekly", description="每週定期定額 (Weekly Recurring)"),
            discord.SelectOption(label="Monthly", description="每月定期定額 (Monthly Recurring)")
        ]
        super().__init__(placeholder="請選擇新的定期定額週期...", options=options)
        self.target_message = target_message

    async def callback(self, interaction: discord.Interaction):
        new_freq = self.values[0]
        thread = interaction.channel
        new_text = f"This is a {new_freq} Recurring Investing order."
        await self.target_message.edit(content=new_text)
        if isinstance(thread, discord.Thread):
            base_name = clean_thread_name(thread.name)
            try: await thread.edit(name=f"[{new_freq}] {base_name}")
            except Exception as e: logger.error(f"Error updating thread title: {e}")
        await interaction.response.send_message(f"✅ 已將定期定額週期變更為 **{new_freq}**！", ephemeral=True)

class ChangeFrequencyView(ui.View):
    def __init__(self, target_message: discord.Message):
        super().__init__(timeout=60)
        self.add_item(ChangeFrequencySelect(target_message))

class RequestFrequencySelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Weekly", description="申請每週定期定額"),
            discord.SelectOption(label="Monthly", description="申請每月定期定額")
        ]
        super().__init__(placeholder="請選擇申請的定期定額週期...", options=options)

    async def callback(self, interaction: discord.Interaction):
        freq = self.values[0]
        thread = interaction.channel
        content = f"<@{APPROVER_ID}>，使用者 {interaction.user.mention} 申請將此訂單轉為 **{freq}** 定期定額 (Recurring Investing)，請審核："
        await thread.send(content=content, view=RecurringApprovalView(freq=freq))
        await interaction.response.send_message(f"✅ 已發送 **{freq}** 定期定額申請，等待審核中。", ephemeral=True)

class RequestFrequencyView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(RequestFrequencySelect())

class ActiveRecurringView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="取消 Cancelled", style=discord.ButtonStyle.danger, custom_id="order_system:recurring_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild_id != TARGET_GUILD_ID: return await interaction.response.send_message("❌ 非目標伺服器無法操作。", ephemeral=True)
        for child in self.children:
            if isinstance(child, ui.Button): child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("❌ 此定期定額訂單已取消並鎖定貼文。")
        if isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
            base_name = clean_thread_name(thread.name)
            try: await thread.edit(name=f"[Cancelled] {base_name}", locked=True, archived=True)
            except Exception as e: logger.error(f"Error cancelling thread: {e}")

    @ui.button(label="更改週期 Change Frequency", style=discord.ButtonStyle.primary, custom_id="order_system:recurring_change")
    async def change_freq_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild_id != TARGET_GUILD_ID: return await interaction.response.send_message("❌ 非目標伺服器無法操作。", ephemeral=True)
        view = ChangeFrequencyView(target_message=interaction.message)
        await interaction.response.send_message("請選擇欲變更的定期定額週期：", view=view, ephemeral=True)

class RecurringApprovalView(ui.View):
    def __init__(self, freq: str = "Monthly"):
        super().__init__(timeout=None)
        self.freq = freq

    @ui.button(label="同意 Approve", style=discord.ButtonStyle.success, custom_id="order_system:approve")
    async def approve_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != APPROVER_ID: return await interaction.response.send_message(f"❌ 只有 <@{APPROVER_ID}> 可以進行審核！", ephemeral=True)
        freq = "Weekly" if "Weekly" in interaction.message.content else "Monthly"
        thread = interaction.channel
        new_content = f"This is a {freq} Recurring Investing order."
        await interaction.response.edit_message(content=new_content, view=ActiveRecurringView())
        if isinstance(thread, discord.Thread):
            base_name = clean_thread_name(thread.name)
            new_title = f"[{freq}] {base_name}"
            try: await thread.edit(name=new_title, applied_tags=[discord.Object(id=RECURRING_TAG_ID)])
            except Exception as e: logger.error(f"Error updating thread title/tag on approval: {e}")

    @ui.button(label="不同意 Reject", style=discord.ButtonStyle.danger, custom_id="order_system:reject")
    async def reject_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != APPROVER_ID: return await interaction.response.send_message(f"❌ 只有 <@{APPROVER_ID}> 可以進行審核！", ephemeral=True)
        for child in self.children:
            if isinstance(child, ui.Button): child.disabled = True
        await interaction.response.edit_message(content="❌ **定期定額申請已被拒絕 (Recurring Investing Request Rejected)**", view=self)
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            async for msg in thread.history(limit=15, oldest_first=True):
                if msg.author.id == interaction.client.user.id and msg.components:
                    try:
                        view = OrderActionView()
                        current_content = msg.content
                        for child in view.children:
                          if isinstance(child, ui.Button) and child.custom_id == "order_system:recurring_req":
                            child.disabled = True
                            child.style = discord.ButtonStyle.secondary
                          elif isinstance(child, ui.Button) and child.custom_id == "order_system:takeover" and "👉 **接手人**" in current_content:
                            child.disabled = True
                            child.style = discord.ButtonStyle.secondary
                        await msg.edit(view=view)
                        break
                    except Exception as e: logger.error(f"Error disabling recurring button: {e}")

class OrderActionView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="接手 Take Over", style=discord.ButtonStyle.primary, custom_id="order_system:takeover")
    async def takeover_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild_id != TARGET_GUILD_ID: return await interaction.response.send_message("❌ 此功能僅限指定伺服器使用。", ephemeral=True)
        if "【🟢 狀態：已完成結案】" in interaction.message.content: return await interaction.response.send_message("❌ 此訂單已完成結案，無法再接手。", ephemeral=True)
        if "👉 **接手人**" in interaction.message.content: return await interaction.response.send_message("❌ 此訂單已被接手，無法重複接手。", ephemeral=True)
        
        user_mention = interaction.user.mention
        current_content = interaction.message.content
        new_content = current_content + f"\n\n👉 **接手人**: {user_mention}"
        button.disabled = True
        button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(content=new_content, view=self)
        await interaction.followup.send(f"✅ {user_mention} 已接手此訂單！", ephemeral=False)

    @ui.button(label="已完成 Completed", style=discord.ButtonStyle.success, custom_id="order_system:complete")
    async def complete_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild_id != TARGET_GUILD_ID: return await interaction.response.send_message("❌ 此功能僅限指定伺服器使用。", ephemeral=True)
        if "【🟢 狀態：已完成結案】" in interaction.message.content: return await interaction.response.send_message("❌ 此訂單已經完成過了。", ephemeral=True)
        
        current_content = interaction.message.content
        new_content = current_content.replace("訊息已接收", "【🟢 狀態：已完成結案】") if "訊息已接收" in current_content else current_content + "\n\n【🟢 狀態：已完成結案】"
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
                if child.custom_id == "order_system:complete": child.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(content=new_content, view=self)
        await interaction.followup.send("✅ 訂單已完成，貼文已鎖定並關閉。 This order has been completed.")
        if isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
            base_name = clean_thread_name(thread.name)
            try: await thread.edit(name=f"[Complete] {base_name}", locked=True, archived=True)
            except Exception as e: logger.error(f"Error completing thread: {e}")

    @ui.button(label="轉成定期定額 Change to Recurring Investing", style=discord.ButtonStyle.secondary, custom_id="order_system:recurring_req")
    async def recurring_request_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild_id != TARGET_GUILD_ID: return await interaction.response.send_message("❌ 此功能僅限指定伺服器使用。", ephemeral=True)
        await interaction.response.send_message("請選擇欲申請的定期定額週期：", view=RequestFrequencyView(), ephemeral=True)

# =================================================================
# 🤖 專為 Render 優化的純 Client 核心
# =================================================================
class OrderClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none()
        )

client = OrderClient()

@client.event
async def on_ready():
    gc.collect()
    logger.info(f"🟢 Discord Bot 已成功上線: {client.user.name}")

@client.event
async def on_thread_create(thread: discord.Thread):
    if thread.guild.id != TARGET_GUILD_ID or thread.parent_id != TARGET_FORUM_ID: return
    applied_tag_ids = [tag.id for tag in thread.applied_tags]
    msg_to_send = ""
    for tag_id in applied_tag_ids:
        if tag_id in TAG_CONFIG:
            msg_to_send = TAG_CONFIG[tag_id]
            break
    if msg_to_send:
        full_content = msg_to_send + EXTRA_INSTRUCTIONS
        await thread.send(content=full_content, view=OrderActionView())

# =================================================================
# 🛠️ [自我診斷] 與優先啟動邏輯
# =================================================================
async def handle_ping(request):
    return web.Response(text="Bot is alive and healthy!")

async def start_web_server():
    logger.info("🔍 [自我診斷] 正在檢查 Render 環境變數...")
    logger.info(f"🔍 [自我診斷] 當前目標 PORT 設為: {PORT}")
    
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    try:
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"🟢 [自我診斷] 成功綁定到 0.0.0.0:{PORT} ！Render 掃描器現在可以抓到此服務。")
        
        # 本地迴路自我測試
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{PORT}/") as resp:
                text = await resp.text()
                logger.info(f"🟢 [自我診斷] 本地網頁響應測試成功，收到回應: '{text}'")
    except Exception as e:
        logger.error(f"❌ [自我診斷] 網頁伺服器啟動失敗: {e}")
        raise e

async def main():
    # 1. 🥇 第一優先：啟動網頁伺服器（秒開 Port 應付 Render 檢查與防斷線）
    await start_web_server()
    
    # 2. 🥈 第二優先：註冊持久化 UI
    client.add_view(OrderActionView())
    client.add_view(RecurringApprovalView())
    client.add_view(ActiveRecurringView())
    logger.info("✅ 持久化 UI 元件註冊完成")
    
    # 3. 🥉 第三優先：登入 Discord
    logger.info("🚀 正在建立與 Discord 伺服器的連線...")
    async with client:
        await client.start(TOKEN)

if __name__ == "__main__":
    if TOKEN:
        asyncio.run(main())
