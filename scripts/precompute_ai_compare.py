"""預先計算 AI 照片比對（Demo 用）：對資料庫現有資料，兩個方向都做，結果寫進 ai_comparisons。

用法（在專案根目錄、啟用 venv 後執行）：
  python scripts/precompute_ai_compare.py --dry-run                 # 只印出會呼叫幾次、涵蓋哪些配對
  python scripts/precompute_ai_compare.py                           # 全部（兩個方向）
  python scripts/precompute_ai_compare.py --only-lost L01 L02       # 只做這些公告當觸發方（飼主端方向）
  python scripts/precompute_ai_compare.py --only-sighting S01 12    # 只做這些通報當觸發方（協尋者端方向）

--only-* 可填資料庫 id，或 demo 資料的 key（L01／S01；資料庫沒有 key 欄位，
實際是比對 email／contact_email 的前綴，例如 l01@demo.local、s01@demo.local）。

規則：
- 每筆通報取前 5 筆候選公告、每筆公告取前 5 筆候選通報（通過外觀門檻、依 rank_key 排序，與網站一致），
  合併去重（同一組配對只算一次）、排除 ai_comparisons 已有結果的配對，再依觸發方分組，每組一次 Azure 呼叫。
- 冪等：重跑不會重複呼叫已經有結果的配對。
- 不會觸發任何 LINE 推播或其他通知（只讀資料、只寫 ai_comparisons）。
- 失敗只印出並繼續下一組；每次呼叫之間停 0.3 秒。
- 需要 DATABASE_URL 與 AZURE_OPENAI_*（環境變數或 .env）。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# 匯入 app 會執行 init_db()（建表，已存在就跳過）；不會送出任何通知
import ai_compare
from app import (
    build_candidates,
    ai_pick_lost_notifications,
    compute_notifications,
    get_db_connection,
    sighting_to_match_obj,
)

DELAY_SECONDS = 0.3


def resolve_ids(conn, table, email_col, values):
  """把 id 或 demo key（L01／S01）轉成資料庫 id；找不到的印出警告。"""
  ids = []
  for v in values:
    if v.isdigit():
      row = conn.execute(f"SELECT id FROM {table} WHERE id = %s", (int(v),)).fetchone()
    else:
      row = conn.execute(
          f"SELECT id FROM {table} WHERE LOWER({email_col}) = %s", (v.lower() + "@demo.local",)
      ).fetchone()
    if row:
      ids.append(row["id"])
    else:
      print(f"⚠️  找不到 {table} 的 {v}，略過")
  return ids


def build_groups(conn, only_lost, only_sighting):
  """回傳 [(trigger_type, trigger_id, trigger_photo, [(候選 id, 候選照片)])]，已去重、已排除有結果的配對。"""
  do_sighting = only_sighting is not None or only_lost is None
  do_lost = only_lost is not None or only_sighting is None

  lost_rows = conn.execute(
      """
      SELECT id, case_code, pet_name, species, size, fur_length, accessory, accessory_color,
             fur_color, special_mark, special_mark_note, lat_precise, lng_precise, lost_time, photo_1
      FROM lost_reports WHERE status = '尋找中'
      """
  ).fetchall()
  existing = {
      (r["lost_id"], r["sighting_id"])
      for r in conn.execute("SELECT lost_id, sighting_id FROM ai_comparisons").fetchall()
  }
  claimed_pairs = set(existing)  # 已有結果或已被前面的組分配的配對 (lost_id, sighting_id)
  groups = []

  if do_sighting:
    where, params = "status != '飼主已確認'", ()
    if only_sighting is not None:
      where += " AND id = ANY(%s)"
      params = (only_sighting,)
    sightings = conn.execute(
        f"""
        SELECT id, species, size, fur_length, accessory, accessory_color, fur_color,
               special_mark, special_mark_note, lat, lng, sighting_time, photo_1
        FROM sightings WHERE {where} ORDER BY id
        """,
        params,
    ).fetchall()
    for s in sightings:
      top = build_candidates(lost_rows, sighting_to_match_obj(s))[:ai_compare.MAX_CANDIDATES]
      cands = []
      for c in top:
        pair = (c["lost_id"], s["id"])
        if pair not in claimed_pairs:
          claimed_pairs.add(pair)
          cands.append((c["lost_id"], c["photo_1"]))
      if cands:
        groups.append(("sighting", s["id"], s["photo_1"], cands))

  if do_lost:
    lost_ids = only_lost if only_lost is not None else [r["id"] for r in lost_rows]
    photos = {r["id"]: r["photo_1"] for r in lost_rows}
    for lid in lost_ids:
      if lid not in photos:  # 不是「尋找中」的公告不做
        continue
      top = ai_pick_lost_notifications(compute_notifications(conn, lid))
      cands = []
      for n in top:
        pair = (lid, n["sighting_id"])
        if pair not in claimed_pairs:
          claimed_pairs.add(pair)
          cands.append((n["sighting_id"], n["photo_1"]))
      if cands:
        groups.append(("lost", lid, photos[lid], cands))
  return groups


def main():
  ap = argparse.ArgumentParser(description="預先計算 AI 照片比對")
  ap.add_argument("--dry-run", action="store_true", help="只印出會做什麼，不呼叫 Azure、不寫入")
  ap.add_argument("--only-lost", nargs="+", metavar="KEY_OR_ID")
  ap.add_argument("--only-sighting", nargs="+", metavar="KEY_OR_ID")
  args = ap.parse_args()

  with get_db_connection() as conn:
    only_lost = resolve_ids(conn, "lost_reports", "email", args.only_lost) if args.only_lost else None
    only_sighting = (
        resolve_ids(conn, "sightings", "contact_email", args.only_sighting)
        if args.only_sighting else None
    )
    groups = build_groups(conn, only_lost, only_sighting)

  pairs = sum(len(g[3]) for g in groups)
  print(f"共 {len(groups)} 次 Azure 呼叫，涵蓋 {pairs} 組配對（已排除已有結果的配對）")
  for ttype, tid, _photo, cands in groups:
    label = "通報→公告" if ttype == "sighting" else "公告→通報"
    print(f"  {label} #{tid}: 候選 {[c[0] for c in cands]}")
  if args.dry_run or not groups:
    return

  if not os.environ.get("AZURE_OPENAI_API_KEY"):
    sys.exit("缺少 AZURE_OPENAI_* 環境變數")

  ok = fail = written = 0
  for i, (ttype, tid, photo, cands) in enumerate(groups, 1):
    try:
      with get_db_connection() as conn:
        n = ai_compare.compare_and_store(conn, ttype, tid, photo, cands)
      written += n
      ok += 1
      print(f"[{i}/{len(groups)}] {ttype} #{tid}：寫入 {n} 筆")
    except Exception as e:
      fail += 1
      print(f"[{i}/{len(groups)}] {ttype} #{tid} 失敗：{type(e).__name__}: {str(e)[:150]}")
    time.sleep(DELAY_SECONDS)
  print(f"完成：成功 {ok} 組、失敗 {fail} 組、共寫入 {written} 筆")


if __name__ == "__main__":
  main()
