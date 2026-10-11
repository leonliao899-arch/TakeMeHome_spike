"""一次性遷移腳本：把 database.db（SQLite）與 static/uploads 的照片搬到 Neon + Cloudinary。

用法（在專案根目錄、啟用 venv 後執行）：
  python migrate_to_neon.py --dry-run   # 只檢查、列出會做什麼，不上傳也不寫入
  python migrate_to_neon.py             # 正式搬遷
  python migrate_to_neon.py --force     # 目標資料庫已經有資料時，先清空再重搬

會連到 .env 裡 DATABASE_URL 指向的資料庫（dev 或 production branch，看你填哪一串）。

做的事情：
1. 用 app.py 的 init_db() 在 Neon 建好資料表
2. 讀出 SQLite 的 lost_reports、sightings、sighting_rejections
3. 照片欄位是 /static/uploads/... 的，上傳到 Cloudinary 並換成網址
   （public_id 用原檔名，重跑時會覆蓋同一張，不會重複佔空間）
4. 保留原本的 id 寫入 Neon（/case/<id> 與追蹤連結才不會失效），最後重設自動編號
舊的 reports 表程式已經沒有在用，不搬。
"""

import argparse
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

import cloudinary
import cloudinary.uploader

# 匯入 app 時會自動執行 init_db()，在 Neon 建好資料表
from app import get_db_connection

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TABLES = ("lost_reports", "sightings", "sighting_rejections")
PHOTO_COLUMNS = ("photo_1", "photo_2", "photo_3", "photo_4", "photo_5")
LOCAL_PREFIX = "/static/uploads/"
CLOUDINARY_FOLDER = "takemehome"


def pg_columns(pg_conn, table):
  """Neon 上這張表有哪些欄位（只搬兩邊都有的欄位）"""
  rows = pg_conn.execute(
      "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
      (table,),
  ).fetchall()
  return {r["column_name"] for r in rows}


def make_photo_converter(dry_run):
  """回傳一個函式：把 /static/uploads/xxx.jpg 換成 Cloudinary 網址（同一張只傳一次）"""
  cache = {}
  stats = {"uploaded": 0, "missing": 0}

  def convert(path):
    if not path or not path.startswith(LOCAL_PREFIX):
      return path  # 空值或已經是網址，原樣保留
    if path in cache:
      return cache[path]

    filename = path[len(LOCAL_PREFIX):]
    local_file = os.path.join(BASE_DIR, "static", "uploads", filename)
    if not os.path.exists(local_file):
      print(f"  ⚠️ 找不到照片檔 {local_file}，這個欄位改成空值")
      stats["missing"] += 1
      cache[path] = ""
      return ""

    if dry_run:
      url = f"(dry-run) 會上傳 {filename}"
    else:
      result = cloudinary.uploader.upload(
          local_file,
          folder=CLOUDINARY_FOLDER,
          public_id=os.path.splitext(filename)[0],
          overwrite=True,
          resource_type="image",
      )
      url = result["secure_url"]
      print(f"  ☁️ {filename} → {url}")
    stats["uploaded"] += 1
    cache[path] = url
    return url

  return convert, stats


def main():
  parser = argparse.ArgumentParser(description="SQLite + 本機照片 → Neon + Cloudinary")
  parser.add_argument("--sqlite", default=os.path.join(BASE_DIR, "database.db"))
  parser.add_argument("--dry-run", action="store_true", help="只檢查，不上傳也不寫入")
  parser.add_argument("--force", action="store_true", help="目標已有資料時先清空")
  args = parser.parse_args()

  if not os.path.exists(args.sqlite):
    sys.exit(f"找不到 SQLite 檔案：{args.sqlite}")
  if not args.dry_run and not os.environ.get("CLOUDINARY_URL"):
    sys.exit("找不到 CLOUDINARY_URL，請先寫進 .env（只想檢查可以加 --dry-run）")
  if os.environ.get("CLOUDINARY_URL"):
    cloudinary.config(secure=True)

  src = sqlite3.connect(args.sqlite)
  src.row_factory = sqlite3.Row
  pg = get_db_connection()

  # 1. 目標資料庫必須是空的，避免覆蓋已經在線上產生的新資料
  existing = {t: pg.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}
  if any(existing.values()):
    print(f"目標資料庫已經有資料：{existing}")
    if not args.force:
      sys.exit("為了安全不會覆蓋。確定要清空重搬，請加上 --force")
    if not args.dry_run:
      pg.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY")
      print("已清空目標資料表")

  convert_photo, stats = make_photo_converter(args.dry_run)
  counts = {}

  # 2. 逐表搬資料（保留原本的 id）
  for table in TABLES:
    rows = src.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    target_cols = pg_columns(pg, table)
    print(f"\n▶ {table}：{len(rows)} 筆")

    for row in rows:
      record = {k: row[k] for k in row.keys() if k in target_cols}
      for col in PHOTO_COLUMNS:
        if col in record:
          record[col] = convert_photo(record[col])

      cols = list(record.keys())
      sql = (
          f"INSERT INTO {table} ({', '.join(cols)}) "
          f"VALUES ({', '.join(['%s'] * len(cols))})"
      )
      if not args.dry_run:
        pg.execute(sql, [record[c] for c in cols])
    counts[table] = len(rows)

  # 3. 重設自動編號：下一筆新資料從「目前最大 id + 1」開始，才不會跟搬過來的資料撞號
  if not args.dry_run:
    for table in TABLES:
      pg.execute(
          f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
          f" COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
      )
    pg.commit()

  # 4. 搬完後核對筆數
  print("\n========== 結果 ==========")
  for table in TABLES:
    if args.dry_run:
      print(f"{table}: 會搬 {counts[table]} 筆")
    else:
      n = pg.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
      mark = "✅" if n == counts[table] else "❌"
      print(f"{mark} {table}: SQLite {counts[table]} 筆 → Neon {n} 筆")
  print(f"照片：上傳 {stats['uploaded']} 張，找不到 {stats['missing']} 張")
  if args.dry_run:
    print("\n（dry-run：沒有上傳任何照片，也沒有寫入資料庫）")

  src.close()
  pg.close()


if __name__ == "__main__":
  main()
