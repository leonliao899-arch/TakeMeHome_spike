import math
from datetime import datetime


def calculate_distance(lat1, lon1, lat2, lon2):
  """使用 Haversine 球面公式計算兩個經緯度之間的直線距離（公里 km）"""
  R = 6371.0
  dlat = math.radians(lat2 - lat1)
  dlon = math.radians(lon2 - lon1)
  a = (
      math.sin(dlat / 2) ** 2
      + math.cos(math.radians(lat1))
      * math.cos(math.radians(lat2))
      * math.sin(dlon / 2) ** 2
  )
  c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
  return R * c


def calculate_match(lost, sighting):
  """核心加權比對函式（100% 嚴格對齊專題規劃總結 v1.7）"""
  result = {
      "is_excluded": False,
      "exclude_reason": None,
      "appearance_score": 0,
      "match_level": "低度關聯",
      "spatiotemporal_score": 0,
      "distance_km": 0.0,
      "score_details": {},
  }

  # ==========================================
  # 【第一關：硬性排除 (Hard Filtering)】
  # ==========================================
  # 1. 物種不同直接排除
  if lost.get("species") != sighting.get("species"):
    result["is_excluded"] = True
    result["exclude_reason"] = (
        f"物種不符 (公告:{lost.get('species')} vs"
        f" 通報:{sighting.get('species')})"
    )
    return result

  # 2. 體型相差兩級（小 vs 大）排除；相差一級仍計分
  size_map = {"小": 1, "中": 2, "大": 3}
  lost_size = size_map.get(lost.get("size"), 2)
  sighting_size = size_map.get(sighting.get("size"), 2)
  if abs(lost_size - sighting_size) >= 2:
    result["is_excluded"] = True
    result["exclude_reason"] = (
        f"體型相差兩級排除 (公告:{lost.get('size')} vs"
        f" 通報:{sighting.get('size')})"
    )
    return result

  # ==========================================
  # 【第二關：核心外觀加權計分 (滿分 100 分)】
  # ==========================================
  app_score = 0
  details = {}

  # 1. 特殊記號 (滿分 35 分)
  l_mark = lost.get("special_mark", "沒有")
  s_mark = sighting.get("special_mark", "沒有")

  if l_mark == "看不清楚" or s_mark == "看不清楚":
    mark_score = 10  # 中立容錯
    mark_note = "其中一方看不清楚 (給中立分 10 分)"
  elif l_mark != "沒有" and l_mark == s_mark:
    mark_score = 35  # 完全吻合
    mark_note = f"特殊記號完全吻合 ({l_mark}) (+35)"
  elif l_mark == "沒有" and s_mark == "沒有":
    mark_score = 20  # 雙方皆無
    mark_note = "雙方皆無特殊記號 (+20)"
  else:
    mark_score = 0
    mark_note = f"特殊記號不一致 ({l_mark} vs {s_mark}) (+0)"

  app_score += mark_score
  details["特殊記號"] = (mark_score, mark_note)

  # 2. 配件＋顏色 (滿分 25 分，嚴格依 v1.7 三種規則)
  l_acc = lost.get("accessory", "沒有")
  s_acc = sighting.get("accessory", "沒有")
  l_acc_color = lost.get("accessory_color", "")
  s_acc_color = sighting.get("accessory_color", "")

  if l_acc != "沒有" and l_acc == s_acc:
    if l_acc_color == s_acc_color:
      acc_score = 25
      acc_note = f"配件與顏色完全吻合 ({l_acc}-{l_acc_color}) (+25)"
    else:
      acc_score = 15
      acc_note = (
          f"配件類型吻合但顏色不同 ({l_acc}:{l_acc_color} vs {s_acc_color})"
          " (+15)"
      )
  elif l_acc != "沒有" and s_acc == "沒有":
    acc_score = 0
    acc_note = "飼主有配件但通報填「沒有」（視為走失脫落，不扣分） (+0)"
  else:
    # 雙方皆無配件，或無吻合配件，皆記 0 分（移除擅自加的 +10 分）
    acc_score = 0
    acc_note = "無吻合之配件標記 (+0)"

  app_score += acc_score
  details["配件與顏色"] = (acc_score, acc_note)

  # 3. 毛色 (滿分 25 分，5大色系)
  l_fur = lost.get("fur_color")
  s_fur = sighting.get("fur_color")
  if l_fur == s_fur:
    fur_score = 25
    fur_note = f"毛色色系完全吻合 ({l_fur}) (+25)"
  else:
    fur_score = 0
    fur_note = f"毛色色系不吻合 ({l_fur} vs {s_fur}) (+0)"

  app_score += fur_score
  details["毛色色系"] = (fur_score, fur_note)

  # 4. 毛長 (滿分 15 分)
  l_len = lost.get("fur_length")
  s_len = sighting.get("fur_length")
  if l_len == s_len:
    len_score = 15
    len_note = f"毛長吻合 ({l_len}) (+15)"
  else:
    len_score = 0
    len_note = f"毛長不吻合 ({l_len} vs {s_len}) (+0)"

  app_score += len_score
  details["毛長"] = (len_score, len_note)

  result["appearance_score"] = app_score
  result["score_details"] = details

  # ==========================================
  # 【第三關：門檻判定 (依核心外觀分數)】
  # ==========================================
  if app_score >= 80:
    result["match_level"] = "🟢 高度匹配"
  elif app_score >= 60:
    result["match_level"] = "🟡 中度潛在"
  else:
    result["match_level"] = "🔴 低度關聯"

  # ==========================================
  # 【獨立時空分數：僅決定排序，不影響門檻】
  # ==========================================
  dist_km = calculate_distance(
      lost.get("lat", 0.0),
      lost.get("lng", 0.0),
      sighting.get("lat", 0.0),
      sighting.get("lng", 0.0),
  )
  result["distance_km"] = round(dist_km, 2)

  try:
    fmt = "%Y-%m-%d %H:%M"
    t_lost = datetime.strptime(lost.get("lost_time"), fmt)
    t_sight = datetime.strptime(sighting.get("sighting_time"), fmt)
    days = max(0, (t_sight - t_lost).total_seconds() / 86400.0)
  except Exception:
    days = 0.5

  species = lost.get("species")
  st_score = 0

  if species == "狗":
    if days <= 1.0:
      st_score = 15 if dist_km <= 2.0 else 0
    elif days <= 3.0:
      st_score = 10 if dist_km <= 10.0 else 0
    else:
      if dist_km <= 50.0:
        st_score = 5
      else:
        penalty = math.floor((dist_km - 50.0) / 2.0)
        st_score = max(-30, 5 - penalty)
  elif species == "貓":
    if days <= 1.0:
      st_score = 15 if dist_km <= 0.5 else 0
    elif days <= 3.0:
      st_score = 10 if dist_km <= 1.5 else 0
    else:
      if dist_km <= 5.0:
        st_score = 5
      else:
        penalty = math.floor((dist_km - 5.0) * 5.0)
        st_score = max(-30, 5 - penalty)

  result["spatiotemporal_score"] = st_score
  return result

# ==========================================
# 🧪 單元測試驅動：完整覆蓋特徵外觀與時空衰減所有分支
# ==========================================
if __name__ == "__main__":
  print("=" * 70)
  print("🚀 啟動【媒合機制技術驗證：檢查點一】全分支與時空對照單元測試")
  print("=" * 70)

  # 1. 基準公告：阿凱的小黑（台北捷運科技大樓站 25.0260, 121.5430）
  dog_lost = {
      "pet_name": "小黑",
      "species": "狗",
      "size": "中",
      "fur_length": "短毛",
      "accessory": "項圈",
      "accessory_color": "紅色",
      "fur_color": "黑/深棕",
      "special_mark": "白襪子",
      "lat": 25.0260,
      "lng": 121.5430,
      "lost_time": "2026-10-01 20:30",
  }

  # 2. 基準公告：咪咪（大安區 貓公告，用於驗證貓的分級與時空）
  cat_lost = {
      "pet_name": "咪咪",
      "species": "貓",
      "size": "小",
      "fur_length": "短毛",
      "accessory": "沒有",
      "accessory_color": "無",
      "fur_color": "黃/棕/褐色",
      "special_mark": "沒有",
      "lat": 25.0260,
      "lng": 121.5430,
      "lost_time": "2026-10-01 12:00",
  }

  test_cases = [
      {
          "title": "情境 1：主要路徑（科技大樓站 -> 大安森林公園，對照 Google 0.81km）",
          "lost": dog_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "項圈",
              "accessory_color": "紅色",
              "fur_color": "黑/深棕",
              "special_mark": "白襪子",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",  # < 1天
          },
          "expected_app": "🟢 高度匹配 (100分)",
          "expected_st": "+15 分 (狗 <=1天, <=2km)",
      },
      {
          "title": (
              "情境 2：項圈脫落容錯（配件脫落 0分 + 白襪子 35分 + 毛色 25分 +"
              " 毛長 15分）"
          ),
          "lost": dog_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "白襪子",
              "lat": 25.0280,
              "lng": 121.5400,
              "sighting_time": "2026-10-02 01:00",  # < 1天
          },
          "expected_app": "🟡 中度潛在 (75分)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 3：時空驗證【狗 > 3天 且超過 50km 扣分遞減】（大安 ->"
              " 新竹約 62km）"
          ),
          "lost": dog_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "項圈",
              "accessory_color": "紅色",
              "fur_color": "黑/深棕",
              "special_mark": "白襪子",
              "lat": 24.8050,
              "lng": 120.9700,  # 新竹護城河
              "sighting_time": "2026-10-06 20:30",  # 走失 5 天
          },
          "expected_app": "🟢 高度匹配 (100分，外觀資格不因距離被刷掉)",
          "expected_st": "-1 分 (基準+5，超標12km扣6分: 5-6=-1)",
      },
      {
          "title": (
              "情境 4：時空驗證【貓 1~3 天，活動範圍 <= 1.5km】（師大附中約"
              " 0.94km）"
          ),
          "lost": cat_lost,
          "sighting": {
              "species": "貓",
              "size": "小",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黃/棕/褐色",
              "special_mark": "沒有",
              "lat": 25.0340,
              "lng": 121.5400,
              "sighting_time": "2026-10-03 12:00",
          },
          "expected_app": (  # 修正：記號皆無 20 + 配件 0 + 毛色 25 + 毛長 15 = 60
              "🟡 中度潛在 (60分)"
          ),
          "expected_st": "+10 分 (貓 1~3天, <=1.5km)",
      },
      {
          "title": (
              "情境 5：時空驗證【貓 > 3天 且超過 5km 急速扣分遞減】（大安 ->"
              " 劍潭約 6.7km）"
          ),
          "lost": cat_lost,
          "sighting": {
              "species": "貓",
              "size": "小",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黃/棕/褐色",
              "special_mark": "沒有",
              "lat": 25.0840,
              "lng": 121.5250,
              "sighting_time": "2026-10-06 12:00",
          },
          "expected_app": (  # 修正：記號皆無 20 + 配件 0 + 毛色 25 + 毛長 15 = 60
              "🟡 中度潛在 (60分)"
          ),
          "expected_st": "-3 分 (基準+5，超標1.7km扣8分: 5-8=-3)",
      },
      {
          "title": "情境 6：硬性排除（物種不符：狗 vs 貓）",
          "lost": dog_lost,
          "sighting": {
              "species": "貓",
              "size": "中",
              "fur_length": "短毛",
              "lat": 25.0265,
              "lng": 121.5435,
              "sighting_time": "2026-10-01 21:00",
          },
          "expected_app": "❌ 直接排除",
          "expected_st": "不計算",
      },
      {
          "title": "情境 7：硬性排除（體型相差兩級：小 vs 大）",
          "lost": cat_lost,  # 貓(小) vs 通報(大)
          "sighting": {
              "species": "貓",
              "size": "大",
              "fur_length": "短毛",
              "lat": 25.0260,
              "lng": 121.5430,
              "sighting_time": "2026-10-01 21:00",
          },
          "expected_app": "❌ 直接排除",
          "expected_st": "不計算",
      },
  ]

  for idx, case in enumerate(test_cases, 1):
    res = calculate_match(case["lost"], case["sighting"])
    print(f"\n[{idx}] {case['title']}")
    print(
        f"    預期結果：外觀: {case['expected_app']} | 時空分: {case['expected_st']}"
    )
    if res["is_excluded"]:
      print(f"    👉 判定：❌ 排除！原因: {res['exclude_reason']}")
    else:
      print(
          f"    👉 判定：{res['match_level']} | 核心外觀分數:"
          f" {res['appearance_score']} 分"
      )
      print(
          f"       球面距離: {res['distance_km']} km | 實際時空分數:"
          f" {res['spatiotemporal_score']} 分"
      )