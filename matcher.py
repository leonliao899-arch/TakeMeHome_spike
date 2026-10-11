import math
import re
from datetime import datetime


# ==========================================
# 推送門檻與排序（寧可多推，不要漏掉）
# ==========================================
# 協尋者常常只看錯一個欄位（例如夜間把虎斑看成黑色，就少 25 分），
# 真正的寶貝分數就會從 75 掉到 50。飼主看到通知一定會自己用照片判斷，
# 多推一筆的代價很小，漏推的代價很大，所以推送門檻從 60 降到 40：
# 只看錯一個欄位的線索會落在 40～59 分，仍然推給飼主；低於 40 代表至少兩個欄位不符。
NOTIFY_THRESHOLD = 40   # 外觀分數 >= 40 就推送（反向推薦清單、飼主通知欄）
STRONG_THRESHOLD = 60   # 外觀分數 >= 60 為主要候選（🟢🟡），排序時一律排在前面


def rank_key(appearance_score, spatiotemporal_score):
  """排序依據：先看外觀等級（60 分以上優先），同一級內再依時空分數由高到低。

  避免「剛好在附近、但外觀只有 40 分的路過黑狗」排在「稍遠、外觀 75 分的真正寶貝」前面。
  用法：清單.sort(key=lambda x: rank_key(x["appearance_score"], x["spatiotemporal_score"]), reverse=True)
  """
  return (1 if appearance_score >= STRONG_THRESHOLD else 0, spatiotemporal_score)


# ==========================================
# 特殊記號文字比對：關鍵字詞表（方案 A，不使用 AI）
# ==========================================
# 「其他」的手寫描述（例如「右耳有缺角」）沒辦法用字串完全相等比對，
# 所以先把文字拆成「部位」與「特徵」兩種標籤，再比對兩邊的標籤是否重疊。
# 詞表集中放在這裡，要補同義詞時只改這兩張表。
# 注意：刻意不分左右（協尋者遠看常分不清左右耳、左右腳）。

# 部位：標籤 → 文字裡可能出現的關鍵字
MARK_PARTS = {
    "耳朵": ["耳"],
    "尾巴": ["尾"],
    "腳": ["腳", "腿", "爪", "掌", "足", "肢", "襪"],
    "眼睛": ["眼", "瞳"],
    "臉": ["臉", "嘴", "鼻", "額", "頭", "下巴", "鬍"],
    "胸口": ["胸"],
    "脖子": ["脖", "頸"],
    "背": ["背"],
    "身體": ["身上", "身體", "肚", "腹", "腰", "屁股", "臀", "側"],
}

# 特徵：標籤 → 文字裡可能出現的關鍵字
MARK_TYPES = {
    "缺損": ["缺", "斷", "短", "剪", "破", "少一", "少了", "沒了", "截"],
    "白色": ["白"],
    "斑塊": ["斑", "塊", "點", "花紋"],
    "疤痕": ["疤", "傷", "痕", "禿", "掉毛"],
    "行動異常": ["跛", "瘸", "拐", "一拐"],
    "形狀異常": ["捲", "折", "垂", "彎", "歪"],
    "眼睛異常": ["異色", "瞎", "獨眼", "白內障", "混濁"],
}

# 下拉選項也轉成同一套標籤，讓「下拉選項」與「手寫描述」可以互相比對
# 例如飼主選下拉「缺耳」、協尋者手寫「右耳有缺角」→ 都是（耳朵, 缺損）
DROPDOWN_MARK_TAGS = {
    "白襪子": ({"腳"}, {"白色"}),
    "胸前白斑": ({"胸口"}, {"白色", "斑塊"}),
    "眼周色塊": ({"眼睛"}, {"斑塊"}),
    "斷尾": ({"尾巴"}, {"缺損"}),
    "缺耳": ({"耳朵"}, {"缺損"}),
    "跛腳": ({"腳"}, {"行動異常"}),
}


def _find_tags(text, table):
  """找出文字中出現的所有標籤"""
  return {tag for tag, words in table.items() if any(w in text for w in words)}


def mark_tags(mark, note=""):
  """把一方的特殊記號轉成標籤。

  回傳 (部位集合, 部位-特徵配對集合)：
    - 下拉選項：查 DROPDOWN_MARK_TAGS
    - 「其他」：把描述依標點與「和、跟」切成小段，每一段各自找部位與特徵，
      這樣「右耳缺角、尾巴有白毛」會得到（耳朵, 缺損）與（尾巴, 白色），
      不會誤配成（耳朵, 白色）
  """
  if mark in DROPDOWN_MARK_TAGS:
    parts, types = DROPDOWN_MARK_TAGS[mark]
    return set(parts), {(p, t) for p in parts for t in types}

  if mark != "其他" or not note:
    return set(), set()

  all_parts, pairs = set(), set()
  for segment in re.split(r"[，,、。；;／/\s]+|和|跟|還有|以及", note):
    if not segment:
      continue
    parts = _find_tags(segment, MARK_PARTS)
    types = _find_tags(segment, MARK_TYPES)
    all_parts |= parts
    pairs |= {(p, t) for p in parts for t in types}
  return all_parts, pairs


def compare_special_marks(l_mark, l_note, s_mark, s_note):
  """比對雙方的特殊記號，回傳 (分數, 說明)。滿分 35 分。

  原有規則（下拉選項之間）不變；只要有一方選「其他」，就改用標籤比對：
    部位與特徵都相同 → 35（等同完全吻合）
    部位相同、特徵不同或沒寫特徵 → 20（部分吻合）
    有一方的描述解析不出部位 → 10（無法比對，比照「看不清楚」給中立分）
    兩邊都解析得出部位，但部位不同 → 0
  """
  # 1. 看不清楚（中立容錯）
  if l_mark == "看不清楚" or s_mark == "看不清楚":
    if {l_mark, s_mark} == {"沒有", "看不清楚"}:
      # 一方說「沒有記號」、另一方「看不清楚」：看不清楚代表沒看到任何矛盾，
      # 不該比回答「沒有」更吃虧，比照「雙方皆無」給 20 分
      return 20, "一方沒有記號、另一方看不清楚 (比照雙方皆無 +20)"
    return 10, "其中一方看不清楚 (給中立分 10 分)"

  # 2. 雙方皆無
  if l_mark == "沒有" and s_mark == "沒有":
    return 20, "雙方皆無特殊記號 (+20)"

  # 3. 一方說沒有、另一方有記號
  if l_mark == "沒有" or s_mark == "沒有":
    other_mark, other_note = (s_mark, s_note) if l_mark == "沒有" else (l_mark, l_note)
    if other_mark == "其他":
      # 「其他」的描述裡找不到任何「部位＋特徵」（例如「尾巴很長」「很可愛」），
      # 代表寫的是一般外觀而不是特殊記號，跟「沒有記號」並不矛盾，比照雙方皆無
      _, pairs = mark_tags(other_mark, other_note)
      if not pairs:
        return 20, "「其他」的描述沒有明確的特殊特徵，與「沒有記號」不矛盾 (比照雙方皆無 +20)"
    return 0, f"特殊記號不一致 ({l_mark} vs {s_mark}) (+0)"

  # 4. 雙方都是下拉選項（沒有「其他」）：維持原本的完全相等比對
  if l_mark != "其他" and s_mark != "其他":
    if l_mark == s_mark:
      return 35, f"特殊記號完全吻合 ({l_mark}) (+35)"
    return 0, f"特殊記號不一致 ({l_mark} vs {s_mark}) (+0)"

  # 5. 至少一方是「其他」：用部位／特徵標籤比對
  l_parts, l_pairs = mark_tags(l_mark, l_note)
  s_parts, s_pairs = mark_tags(s_mark, s_note)

  if not l_parts or not s_parts:
    return 10, "特殊記號描述無法比對 (給中立分 10 分，請飼主看描述判斷)"

  same_pairs = l_pairs & s_pairs
  if same_pairs:
    part, kind = sorted(same_pairs)[0]
    return 35, f"特殊記號描述吻合 ({part}・{kind}) (+35)"

  same_parts = l_parts & s_parts
  if same_parts:
    return 20, f"特殊記號部位相同 ({'、'.join(sorted(same_parts))})，特徵不同或未描述 (+20)"

  return 0, (
      f"特殊記號部位不同 ({'、'.join(sorted(l_parts))} vs"
      f" {'、'.join(sorted(s_parts))}) (+0)"
  )


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

  # 2. 體型不再硬性排除（v2.1 修正）：
  #    照片沒有比例尺，體型是協尋者最容易看錯的欄位，不該握有「直接剔除」的權力。
  #    體型不計分、也不排除，只在詳情頁並排顯示，交給飼主看照片判斷。

  # ==========================================
  # 【第二關：核心外觀加權計分 (滿分 100 分)】
  # ==========================================
  app_score = 0
  details = {}

  # 1. 特殊記號 (滿分 35 分)：規則集中在 compare_special_marks()
  mark_score, mark_note = compare_special_marks(
      lost.get("special_mark", "沒有"),
      lost.get("special_mark_note") or "",
      sighting.get("special_mark", "沒有"),
      sighting.get("special_mark_note") or "",
  )

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
  elif app_score >= STRONG_THRESHOLD:
    result["match_level"] = "🟡 中度潛在"
  else:
    # 40～59 分仍會推送（排在後面）；低於 40 分不推送，只顯示在地圖上
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

  # 3. 基準公告：黑皮（沒有配件、沒有記號的黑色米克斯，用於驗證情境 8）
  plain_dog_lost = dict(
      dog_lost, pet_name="黑皮", accessory="沒有", accessory_color="無",
      special_mark="沒有",
  )

  # 4. 基準公告：阿灰（特殊記號選「其他」並手寫描述，用於驗證情境 9～12）
  other_mark_lost = dict(
      dog_lost, pet_name="阿灰", special_mark="其他",
      special_mark_note="右耳有缺角、尾巴尖端是白色",
  )

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
          "title": "情境 7：體型相差兩級不再排除（小 vs 大，照常計分）",
          "lost": cat_lost,  # 貓(小) vs 通報(大)
          "sighting": {
              "species": "貓",
              "size": "大",
              "fur_length": "短毛",
              "lat": 25.0260,
              "lng": 121.5430,
              "sighting_time": "2026-10-01 21:00",
          },
          "expected_app": "🔴 低度關聯 (35分：記號皆無 20 + 毛長 15，未填毛色 → 低於 40 不推送)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 8：飼主沒有記號 vs 協尋者看不清楚（比照雙方皆無 20 分 +"
              " 配件 0 + 毛色 25 + 毛長 15）"
          ),
          "lost": plain_dog_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "看不清楚",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🟡 中度潛在 (60分，修正前只有 50 分會被擋下)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 9：文字比對【部位＋特徵吻合】飼主「右耳有缺角…」vs"
              " 協尋者下拉「缺耳」"
          ),
          "lost": other_mark_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "缺耳",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🟡 中度潛在 (75分：記號 35 + 配件 0 + 毛色 25 + 毛長 15)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 10：文字比對【只有部位相同】飼主「尾巴尖端是白色」vs"
              " 協尋者手寫「尾巴很短」"
          ),
          "lost": dict(other_mark_lost, special_mark_note="尾巴尖端是白色"),
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "其他",
              "special_mark_note": "尾巴很短",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🟡 中度潛在 (60分：記號 20 + 毛色 25 + 毛長 15)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 11：文字比對【部位不同】飼主「右耳有缺角…」vs 協尋者手寫"
              "「背上有一塊疤」"
          ),
          "lost": other_mark_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "其他",
              "special_mark_note": "背上有一塊疤",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🔴 低度關聯 (40分：記號 0 + 毛色 25 + 毛長 15 → 剛好達推送門檻)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 12：文字比對【描述解析不出部位】協尋者手寫「很特別的樣子」"
          ),
          "lost": other_mark_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "其他",
              "special_mark_note": "很特別的樣子",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🔴 低度關聯 (50分：記號中立 10 + 毛色 25 + 毛長 15 → 推送)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 13：飼主沒有記號 vs 協尋者「其他：尾巴很長」（描述沒有特殊特徵，"
              "不算矛盾）"
          ),
          "lost": plain_dog_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "其他",
              "special_mark_note": "尾巴很長",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🟡 中度潛在 (60分：記號 20 + 配件 0 + 毛色 25 + 毛長 15)",
          "expected_st": "+15 分",
      },
      {
          "title": (
              "情境 14：飼主沒有記號 vs 協尋者「其他：背上有疤」（描述出明確特徵，"
              "與「沒有」矛盾）"
          ),
          "lost": plain_dog_lost,
          "sighting": {
              "species": "狗",
              "size": "中",
              "fur_length": "短毛",
              "accessory": "沒有",
              "accessory_color": "無",
              "fur_color": "黑/深棕",
              "special_mark": "其他",
              "special_mark_note": "背上有疤",
              "lat": 25.0295,
              "lng": 121.5360,
              "sighting_time": "2026-10-01 23:20",
          },
          "expected_app": "🔴 低度關聯 (40分：記號 0 + 毛色 25 + 毛長 15 → 剛好達推送門檻)",
          "expected_st": "+15 分",
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
      pushed = "推送" if res["appearance_score"] >= NOTIFY_THRESHOLD else "不推送"
      print(
          f"    👉 判定：{res['match_level']} | 核心外觀分數:"
          f" {res['appearance_score']} 分（{pushed}）"
      )
      print(
          f"       球面距離: {res['distance_km']} km | 實際時空分數:"
          f" {res['spatiotemporal_score']} 分"
      )
      if idx >= 8:
        print(f"       特殊記號: {res['score_details']['特殊記號'][1]}")
