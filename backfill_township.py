"""補上舊資料的行政區（township）。

只處理 township 還是空的走失公告與目擊通報，每筆之間停 1.1 秒（Nominatim 限制每秒 1 次）。
查不到的資料會留空，可以重跑。用法（在專案根目錄，會讀 .env 裡的 DATABASE_URL）：

    python backfill_township.py
"""
import time

from app import get_db_connection, lookup_township

# 走失公告只用公開模糊座標，跟發布時的 save_township_async 一致
TABLE_COORDS = {
    "lost_reports": "lat_public AS lat, lng_public AS lng",
    "sightings": "lat, lng",
}


def main():
  for table, coords in TABLE_COORDS.items():
    with get_db_connection() as conn:
      rows = conn.execute(
          f"SELECT id, {coords} FROM {table} WHERE township IS NULL ORDER BY id"
      ).fetchall()

    print(f"{table}: 待補 {len(rows)} 筆")
    for row in rows:
      township = None
      for _ in range(3):   # 逾時等暫時性失敗，多試幾次
        township = lookup_township(row["lat"], row["lng"])
        if township:
          break
        time.sleep(1.1)
      if township:
        with get_db_connection() as conn:
          conn.execute(f"UPDATE {table} SET township = %s WHERE id = %s", (township, row["id"]))
      print(f"  #{row['id']} -> {township or '查不到'}")
      time.sleep(1.1)


if __name__ == "__main__":
  main()
