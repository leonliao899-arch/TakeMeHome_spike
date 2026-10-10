import sqlite3
from matcher import calculate_match

DB_NAME = "database.db"


def run_phase2_tests():
  print("=" * 70)
  print("🚀 啟動【階段二：資料庫查詢與反向比對】嚴謹驗證（動態分數核對）")
  print("=" * 70)

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()

  # 清理舊的測試標記資料
  cursor.execute(
      "DELETE FROM lost_reports WHERE case_code LIKE 'TEST-PHASE2-%'"
  )

  # [Case A] 已尋回（測試檢查點 5：絕對不能被 SQL 撈出）
  cursor.execute("""
        INSERT INTO lost_reports (
            case_code, pet_name, species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, notes, photo_1,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (
            'TEST-PHASE2-A', '已找到的狗', '狗', '中', '短毛', '項圈', '紅色',
            '黑/深棕', '白襪子', '', '',
            25.0260, 121.5430, 25.0260, 121.5430, 50,
            '2026-10-01 20:30', 'test@test.com', '已尋回'
        )
    """)

  # [Case B] 尋找中：無配件狗，核心分數 75 分，但就在目擊點旁 (時空分數 +15)
  # 地點：大安森林公園旁 25.0290, 121.5365 (< 2km)
  cursor.execute("""
        INSERT INTO lost_reports (
            case_code, pet_name, species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, notes, photo_1,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (
            'TEST-PHASE2-B', '近處無配件小黑', '狗', '中', '短毛', '沒有', '無',
            '黑/深棕', '白襪子', '', '',
            25.0290, 121.5365, 25.0290, 121.5365, 50,
            '2026-10-01 22:00', 'test@test.com', '尋找中'
        )
    """)

  # [Case C] 尋找中：帶紅項圈，特徵 100% 吻合 (核心滿分 100 分)，但遠在新竹 (時空分數 -1)
  # 地點：新竹 24.8050, 120.9700 (> 50km，走失 6 天)
  cursor.execute("""
        INSERT INTO lost_reports (
            case_code, pet_name, species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, notes, photo_1,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (
            'TEST-PHASE2-C', '遠在新竹的滿分小黑', '狗', '中', '短毛', '項圈', '紅色',
            '黑/深棕', '白襪子', '', '',
            24.8050, 120.9700, 24.8050, 120.9700, 50,
            '2026-09-25 12:00', 'test@test.com', '尋找中'
        )
    """)

  # [Case D] 尋找中：核心分數 < 60 (僅毛長吻合 15分)，應被資格判定剔除
  cursor.execute("""
        INSERT INTO lost_reports (
            case_code, pet_name, species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, notes, photo_1,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (
            'TEST-PHASE2-D', '路人白狗', '狗', '大', '短毛', '沒有', '無',
            '白/米白/淺黃', '沒有', '', '',
            25.0290, 121.5365, 25.0290, 121.5365, 50,
            '2026-10-01 22:00', 'test@test.com', '尋找中'
        )
    """)
  conn.commit()

  # 目擊通報：明確拍到「紅色項圈」
  current_sighting = {
      "species": "狗",
      "size": "中",
      "fur_length": "短毛",
      "accessory": "項圈",
      "accessory_color": "紅色",
      "fur_color": "黑/深棕",
      "special_mark": "白襪子",
      "lat": 25.0295,
      "lng": 121.5360,
      "sighting_time": "2026-10-01 23:20",
  }

  print(
      "\n協尋者目擊通報地點：(25.0295, 121.5360)，特徵：黑/深棕中型犬、白襪子、紅色項圈"
  )

  # 執行資料庫查詢
  cursor.execute("""
        SELECT id, case_code, pet_name, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark,
               lat_precise, lng_precise, lost_time, status
        FROM lost_reports
        WHERE status = '尋找中' AND case_code LIKE 'TEST-PHASE2-%'
    """)
  rows = cursor.fetchall()

  # -------------------------------------------------------------------------
  # 【檢查點 5】過濾條件驗證
  # -------------------------------------------------------------------------
  case_codes_fetched = [r[1] for r in rows]
  print("\n" + "-" * 60)
  print("📌 【檢查點 5】過濾條件驗證（只抓 status = '尋找中'）")
  print(f"資料庫撈出的候選清單: {case_codes_fetched}")
  if "TEST-PHASE2-A" not in case_codes_fetched:
    print(
        "👉 驗證通過：✅ 'TEST-PHASE2-A' (已尋回) 成功被 SQL 排除，未進入比對流程！"
    )
  else:
    print("👉 驗證失敗：❌ '已尋回' 案件未被排除！")

  # -------------------------------------------------------------------------
  # 【檢查點 6】資格判定驗證
  # -------------------------------------------------------------------------
  print("\n" + "-" * 60)
  print("📌 【檢查點 6】資格判定驗證（外觀分數 >= 60 分才保留）")
  candidates = []
  for row in rows:
    lost_obj = {
        "species": row[3],
        "size": row[4],
        "fur_length": row[5],
        "accessory": row[6],
        "accessory_color": row[7],
        "fur_color": row[8],
        "special_mark": row[9],
        "lat": row[10],
        "lng": row[11],
        "lost_time": row[12],
    }
    match_res = calculate_match(lost_obj, current_sighting)
    app_score = match_res["appearance_score"]
    st_score = match_res["spatiotemporal_score"]
    case_code = row[1]
    name = row[2]

    print(
        f"案件 {case_code} ({name}): 外觀分數 = {app_score} 分 | 時空分數 ="
        f" {st_score} 分"
    )

    if not match_res["is_excluded"] and app_score >= 60:
      candidates.append({
          "case_code": case_code,
          "pet_name": name,
          "appearance_score": app_score,
          "spatiotemporal_score": st_score,
      })

  eligible_codes = [c["case_code"] for c in candidates]
  print(f"\n外觀合格（>= 60分）名單: {eligible_codes}")
  if "TEST-PHASE2-D" not in eligible_codes and len(eligible_codes) == 2:
    print("👉 驗證通過：✅ 低分案件 (TEST-PHASE2-D, 15分) 成功被過濾剔除！")
  else:
    print("👉 驗證失敗：❌ 資格判定未正確過濾！")

  # -------------------------------------------------------------------------
  # 【檢查點 7】排序依據驗證（外觀低但時空高 vs 外觀高但時空低）
  # -------------------------------------------------------------------------
  print("\n" + "-" * 60)
  print("📌 【檢查點 7】排序依據驗證（外觀較低但時空極高 vs 外觀滿分但時空負分）")

  # 依時空分數由高到低排序
  candidates.sort(key=lambda x: x["spatiotemporal_score"], reverse=True)

  print("反向推薦清單最終排序結果：")
  for rank, c in enumerate(candidates, 1):
    print(
        f"  第 {rank} 名: {c['case_code']} ({c['pet_name']}) -> 時空分數:"
        f" {c['spatiotemporal_score']} 分 | 核心外觀分數:"
        f" {c['appearance_score']} 分"
    )

  c1 = candidates[0]
  c2 = candidates[1]

  # 斷言：第 1 名的外觀分數必須「小於」第 2 名，但時空分數必須「大於」第 2 名
  if (
      c1["spatiotemporal_score"] > c2["spatiotemporal_score"]
      and c1["appearance_score"] < c2["appearance_score"]
  ):
    print("\n👉 驗證通過：✅ 排序完全依『時空分數』由高至低排！")
    print(
        f"   證明：{c1['case_code']}（外觀 {c1['appearance_score']} 分、時空"
        f" {c1['spatiotemporal_score']:+} 分）成功排在"
        f" {c2['case_code']}（外觀 {c2['appearance_score']} 分、時空"
        f" {c2['spatiotemporal_score']:+} 分）的前面！"
    )
    print("   目標達成：證明『過濾條件』『資格判定』『排序依據』三者完全獨立、無混用！")
  else:
    print("\n👉 驗證失敗：❌ 排序邏輯未體現時空分數對外觀分數的反超！")

  # 清理測試資料
  cursor.execute(
      "DELETE FROM lost_reports WHERE case_code LIKE 'TEST-PHASE2-%'"
  )
  conn.commit()
  conn.close()
  print("\n" + "=" * 70)


if __name__ == "__main__":
  run_phase2_tests()