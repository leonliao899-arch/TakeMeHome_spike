"""Demo 種子資料匯入：把 "demo_seed/" 的 lost_reports.csv、sightings.csv 寫入資料庫。

用法（專案根目錄、啟用 venv 後執行）：
  python seed_demo.py --dry-run     # 只檢查 CSV、列出會做什麼，不寫入
  python seed_demo.py               # 匯入（會先刪掉舊的 demo 資料再重灌，可重複執行）
  python seed_demo.py --no-township # 不呼叫 Nominatim 反查鄉鎮（較快，篩選的鄉鎮欄位會是空的）

連到 .env 裡 DATABASE_URL 指向的資料庫。demo 資料的辨識方式：
  走失公告 email、目擊通報 contact_email 以 @demo.local 結尾。
照片：CSV 的 photo_files（例如 1.jpg;2.jpg）從 "demo_seed/photos/" 讀取，
  有設定 CLOUDINARY_URL 就上傳 Cloudinary，否則複製到 static/uploads；找不到檔案就留空。
"""

import argparse
import csv
from datetime import datetime, timedelta
import os
import random
import shutil
import sys
import time

from dotenv import load_dotenv

load_dotenv()

import cloudinary.uploader

# 匯入 app 時會自動執行 init_db()
from app import (
    CLOUDINARY_FOLDER, OWNER_TOKEN_PREFIX, TRACK_TOKEN_PREFIX, UPLOAD_FOLDER,
    USE_CLOUDINARY, calculate_public_coords, get_db_connection, lookup_township,
    new_token, normalize_time,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = os.path.join(BASE_DIR, "demo_seed")
PHOTO_DIR = os.path.join(SEED_DIR, "photos")
DEMO_EMAIL_LIKE = "%@demo.local"


def created_utc(taipei_text):
  """上架時間（created_at，UTC）設成事件時間，讓清單的「今天／昨天／10/12」各不相同"""
  return datetime.strptime(taipei_text, "%Y-%m-%d %H:%M") - timedelta(hours=8)


def read_csv(name):
  with open(os.path.join(SEED_DIR, name), encoding="utf-8-sig", newline="") as f:
    return list(csv.DictReader(f))


def photo_urls(kind, key):
  """回傳最多 5 個照片網址（不足補空字串）。照片取 photos/<kind>/<key>/ 底下所有圖檔（依檔名排序）"""
  folder = os.path.join(PHOTO_DIR, kind, key)
  names = []
  if os.path.isdir(folder):
    names = sorted(n for n in os.listdir(folder)
                   if os.path.splitext(n)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".avif"))[:5]
  urls = []
  for i, name in enumerate(names, 1):
    path = os.path.join(folder, name)
    ext = os.path.splitext(name)[1].lower()
    public_id = f"demo_{key}_{i}"
    if USE_CLOUDINARY:
      res = cloudinary.uploader.upload(
          path, folder=CLOUDINARY_FOLDER, public_id=public_id,
          overwrite=True, resource_type="image",
      )
      urls.append(res["secure_url"])
    else:
      os.makedirs(UPLOAD_FOLDER, exist_ok=True)
      new_name = f"{public_id}{ext}"
      shutil.copyfile(path, os.path.join(UPLOAD_FOLDER, new_name))
      urls.append(f"/static/uploads/{new_name}")
  return urls + [""] * (5 - len(urls))


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--no-township", action="store_true")
  args = parser.parse_args()

  lost_rows = read_csv("lost_reports.csv")
  sighting_rows = read_csv("sightings.csv")
  print(f"讀到走失公告 {len(lost_rows)} 筆、目擊通報 {len(sighting_rows)} 筆")
  has_photos = os.path.isdir(PHOTO_DIR)
  print("照片資料夾：" + (PHOTO_DIR if has_photos else "找不到 demo_seed/photos/，照片欄位會留空"))
  if args.dry_run:
    print("--dry-run：不寫入。")
    return

  conn = get_db_connection()
  cur = conn.cursor()
  cur.execute("SELECT id FROM lost_reports WHERE email LIKE %s", (DEMO_EMAIL_LIKE,))
  old_lost = [r["id"] for r in cur.fetchall()]
  cur.execute("SELECT id FROM sightings WHERE contact_email LIKE %s", (DEMO_EMAIL_LIKE,))
  old_sight = [r["id"] for r in cur.fetchall()]
  if old_lost or old_sight:
    print(f"清掉舊的 demo 資料：公告 {len(old_lost)}、通報 {len(old_sight)}")
    cur.execute(
        "DELETE FROM sighting_claims WHERE lost_id = ANY(%s) OR sighting_id = ANY(%s)",
        (old_lost, old_sight))
    cur.execute(
        "DELETE FROM sighting_rejections WHERE lost_id = ANY(%s) OR sighting_id = ANY(%s)",
        (old_lost, old_sight))
    cur.execute("DELETE FROM lost_reports WHERE id = ANY(%s)", (old_lost,))
    cur.execute("DELETE FROM sightings WHERE id = ANY(%s)", (old_sight,))

  used_codes = set()
  geocoded = []  # (table, id, lat, lng)
  for r in lost_rows:
    lat, lng = float(r["lat"]), float(r["lng"])
    radius = int(r["privacy_radius"] or 50)
    lat_pub, lng_pub = calculate_public_coords(lat, lng, radius)
    code = f"LOST-{random.randint(1000, 9999)}"
    while code in used_codes:
      code = f"LOST-{random.randint(1000, 9999)}"
    used_codes.add(code)
    photos = photo_urls("lost", r["key"])
    cur.execute(
        """
        INSERT INTO lost_reports (
            case_code, pet_name, species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, special_mark_note, notes,
            photo_1, photo_2, photo_3, photo_4, photo_5,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status, owner_token, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'尋找中',%s,%s)
        RETURNING id
        """,
        (code, r["pet_name"], r["species"], r["size"], r["fur_length"], r["accessory"],
         r["accessory_color"], r["fur_color"], r["special_mark"], r["special_mark_note"],
         r["notes"], *photos, lat, lng, lat_pub, lng_pub, radius,
         normalize_time(r["lost_time"]), r["email"], new_token(OWNER_TOKEN_PREFIX),
         created_utc(normalize_time(r["lost_time"]))),
    )
    geocoded.append(("lost_reports", cur.fetchone()["id"], lat_pub, lng_pub))

  for r in sighting_rows:
    lat, lng = float(r["lat"]), float(r["lng"])
    photos = photo_urls("sightings", r["key"])
    cur.execute(
        """
        INSERT INTO sightings (
            species, size, fur_length, accessory, accessory_color, fur_color,
            special_mark, special_mark_note,
            photo_1, photo_2, photo_3, photo_4, photo_5,
            lat, lng, description, sighting_time, status, score, tracking_token, contact_email, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'待確認',0,%s,%s,%s)
        RETURNING id
        """,
        (r["species"], r["size"], r["fur_length"], r["accessory"], r["accessory_color"],
         r["fur_color"], r["special_mark"], r["special_mark_note"], *photos, lat, lng,
         r["description"], normalize_time(r["sighting_time"]),
         new_token(TRACK_TOKEN_PREFIX), r["contact_email"],
         created_utc(normalize_time(r["sighting_time"]))),
    )
    geocoded.append(("sightings", cur.fetchone()["id"], lat, lng))

  conn.commit()
  print(f"已寫入：公告 {len(lost_rows)}、通報 {len(sighting_rows)}")

  if not args.no_township:
    print("反查鄉鎮（Nominatim 每秒限 1 次，約需 1 分鐘）…")
    for table, row_id, lat, lng in geocoded:
      township = lookup_township(lat, lng)
      if township:
        cur.execute(f"UPDATE {table} SET township = %s WHERE id = %s", (township, row_id))
        conn.commit()
      time.sleep(1.1)
  conn.close()
  print("完成。")


if __name__ == "__main__":
  sys.exit(main())
