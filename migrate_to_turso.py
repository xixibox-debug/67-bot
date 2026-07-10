"""
一次性搬家腳本：把 Railway 上現有的本地 SQLite 資料，複製一份到 Turso 當備份。
不會刪除、不會動到本地檔案，執行完畢後 main.py 完全不受影響，可以重複執行（會用 INSERT OR REPLACE 覆蓋同筆資料，不會重複）。

用法（在 Railway 的網頁版 Shell 裡執行，不用本機指令）：
    python migrate_to_turso.py
"""
import os
import sqlite3
import sys

try:
    import libsql
except ImportError:
    print("❌ 找不到 libsql 套件。請先在 requirements.txt 加上 'libsql'，重新部署後再執行這支腳本。")
    sys.exit(1)

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    print("❌ 找不到 TURSO_DATABASE_URL 或 TURSO_AUTH_TOKEN，請先到 Railway 的 Variables 分頁設定好這兩個環境變數。")
    sys.exit(1)

if not os.path.exists(DB_PATH):
    print(f"❌ 找不到本地資料庫檔案：{DB_PATH}")
    sys.exit(1)

print(f"📂 來源（本地 SQLite）：{DB_PATH}")
print(f"☁️  目標（Turso）：{TURSO_DATABASE_URL}")
print("-" * 50)

local_conn = sqlite3.connect(DB_PATH)
local_cursor = local_conn.cursor()

turso_conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
turso_cursor = turso_conn.cursor()

# 取得本地資料庫裡所有的資料表跟建表語句
local_cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
tables = local_cursor.fetchall()

if not tables:
    print("⚠️ 本地資料庫裡沒有任何資料表，沒有東西可以搬。")
    sys.exit(0)

total_rows = 0

for table_name, create_sql in tables:
    print(f"\n🔄 處理資料表：{table_name}")

    # 1. 在 Turso 上建立同樣結構的表（如果還沒有的話）
    try:
        turso_cursor.execute(create_sql)
        turso_conn.commit()
    except Exception as e:
        # 表可能已經存在，忽略這類錯誤，其他錯誤照樣印出來看
        if "already exists" not in str(e).lower():
            print(f"   ⚠️ 建表時出現訊息：{e}")

    # 2. 抓出這張表所有欄位名稱，準備組 INSERT 語句
    local_cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [col[1] for col in local_cursor.fetchall()]
    col_list = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    insert_sql = f"INSERT OR REPLACE INTO {table_name} ({col_list}) VALUES ({placeholders})"

    # 3. 讀出本地所有資料，一筆一筆寫進 Turso
    local_cursor.execute(f"SELECT {col_list} FROM {table_name}")
    rows = local_cursor.fetchall()

    for row in rows:
        turso_cursor.execute(insert_sql, row)

    turso_conn.commit()
    print(f"   ✅ 搬了 {len(rows)} 筆資料")
    total_rows += len(rows)

print("-" * 50)
print(f"🎉 全部完成！共搬了 {len(tables)} 張表、{total_rows} 筆資料到 Turso。")
print("本地檔案完全沒被動過，main.py 現在還是照原本的方式在跑，Turso 那邊只是多了一份備份。")

local_conn.close()
turso_conn.close()
