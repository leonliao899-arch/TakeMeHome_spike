import math
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime
from flask import Flask, jsonify, render_template, request

# 讀取專案根目錄的 .env（放 Azure 金鑰用；沒裝 python-dotenv 也不影響執行）
try:
  from dotenv import load_dotenv

  load_dotenv()
except ImportError:
  pass

# 引入檢查點一驗證完成的比對核心模組
from matcher import calculate_match

# AI 照片辨識（Azure AI Foundry）：提供 /api/analyze-photo 預填外觀欄位
from vision import vision_bp

app = Flask(__name__)
app.register_blueprint(vision_bp)
DB_NAME = "database.db"

# 單次請求上限 32MB；超過時 Flask 會自動回 413
# 不設 10MB 的原因：飼主表單目前直接上傳手機原圖，AI 辨識一次最多送 3 張原圖，
# iPhone 照片轉成 JPEG 後單張就可能超過 5MB，設太小會讓上傳在中途被切斷。
# 協尋者表單已在前端壓縮到約 200KB，不受影響；單張 10MB 的限制由 vision.py 檢查。
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# matcher.py 計算天數時使用的時間格式，所有寫入資料庫的時間都要統一成這個格式
TIME_FORMAT = "%Y-%m-%d %H:%M"

# 早期版本的種子資料指向不存在的照片，啟動時一併清空，避免畫面出現破圖
MISSING_SEED_PHOTOS = (
    "/static/uploads/demo_dog.jpg",
    "/static/uploads/sighting_dog.jpg",
)


def normalize_time(raw):
  """把前端送來的時間統一成 'YYYY-MM-DD HH:MM'。

  手機的 <input type="datetime-local"> 送出的是 '2026-10-01T20:30'
  （中間是 T，部分瀏覽器還會帶秒數 ':00'），但 matcher.py 用
  '%Y-%m-%d %H:%M' 解析，格式不符時會默默改用 0.5 天計算，時空分數就失真了。
  所以寫入資料庫前先把 T 換成空格、去掉秒數，再實際解析一次確認格式正確。
  格式錯誤回傳 None，由呼叫端回 400 錯誤。
  """
  if not raw:
    return None
  text = str(raw).strip().replace("T", " ")[:16]
  try:
    datetime.strptime(text, TIME_FORMAT)
  except ValueError:
    return None
  return text


def get_db_connection():
  """建立資料庫連線，設定 row_factory 讓查詢結果能像字典一樣用欄位名稱存取"""
  conn = sqlite3.connect(DB_NAME)
  conn.row_factory = sqlite3.Row
  return conn


def ensure_columns(cursor, table, columns):
  """檢查資料表是否有指定欄位，缺少的就用 ALTER TABLE 補上。

  PRAGMA table_info 會列出資料表目前所有欄位，第 2 個值（索引 1）是欄位名稱。
  """
  cursor.execute(f"PRAGMA table_info({table})")
  existing = {row[1] for row in cursor.fetchall()}
  for name, col_type in columns.items():
    if name not in existing:
      cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def init_db():
  """建立資料表，並在首次啟動時自動載入測試資料"""
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 走失公告表：包含 pet_name 欄位
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS lost_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_code TEXT NOT NULL,
            pet_name TEXT,
            species TEXT NOT NULL,
            size TEXT NOT NULL,
            fur_length TEXT NOT NULL,
            accessory TEXT NOT NULL,
            accessory_color TEXT,
            fur_color TEXT NOT NULL,
            special_mark TEXT NOT NULL,
            notes TEXT,
            photo_1 TEXT,
            photo_2 TEXT,
            photo_3 TEXT,
            lat_precise REAL NOT NULL,
            lng_precise REAL NOT NULL,
            lat_public REAL NOT NULL,
            lng_public REAL NOT NULL,
            privacy_radius INTEGER DEFAULT 50,
            lost_time TEXT NOT NULL,
            email TEXT NOT NULL,
            status TEXT DEFAULT '尋找中',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

  # 2. 目擊通報表
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            species TEXT NOT NULL,
            size TEXT NOT NULL,
            fur_length TEXT NOT NULL,
            accessory TEXT NOT NULL,
            accessory_color TEXT,
            fur_color TEXT NOT NULL,
            special_mark TEXT NOT NULL,
            photo_1 TEXT,
            photo_2 TEXT,
            photo_3 TEXT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            description TEXT,
            sighting_time TEXT NOT NULL,
            matched_lost_id INTEGER,
            status TEXT DEFAULT '待確認',
            score INTEGER DEFAULT 0,
            tracking_token TEXT,
            contact_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

  # 2.5 補上舊資料庫缺少的欄位
  #     CREATE TABLE IF NOT EXISTS 只會在「表不存在」時建表；如果表是舊版程式建的，
  #     之後新增的欄位（例如 pet_name）不會自動補上，查詢時就會出現 no such column。
  #     所以逐一檢查欄位，缺少的用 ALTER TABLE 補上（補上的欄位舊資料會是空值）。
  ensure_columns(cursor, "lost_reports", {
      "pet_name": "TEXT",
      "notes": "TEXT",
      "photo_2": "TEXT",
      "photo_3": "TEXT",
      "privacy_radius": "INTEGER DEFAULT 50",
  })
  ensure_columns(cursor, "sightings", {
      "photo_2": "TEXT",
      "photo_3": "TEXT",
      "description": "TEXT",
      "matched_lost_id": "INTEGER",
      "score": "INTEGER DEFAULT 0",
      "tracking_token": "TEXT",
      "contact_email": "TEXT",
  })

  # 3. 測試種子資料（阿凱的小黑）；照片欄位留空，不引用不存在的檔案
  cursor.execute("SELECT COUNT(*) FROM lost_reports")
  if cursor.fetchone()[0] == 0:
    cursor.execute("""
            INSERT INTO lost_reports (
                case_code, pet_name, species, size, fur_length, accessory, accessory_color,
                fur_color, special_mark, notes, photo_1,
                lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
                lost_time, email, status
            ) VALUES (
                'LOST-0423', '小黑', '狗', '中', '短毛', '項圈', '紅色',
                '黑/深棕', '白襪子', '親人、貪吃', '',
                25.0260, 121.5430, 25.0263, 121.5433, 50,
                '2026-10-01 20:30', 'owner@example.com', '尋找中'
            )
        """)

  cursor.execute("SELECT COUNT(*) FROM sightings")
  if cursor.fetchone()[0] == 0:
    cursor.execute("""
            INSERT INTO sightings (
                species, size, fur_length, accessory, accessory_color,
                fur_color, special_mark, photo_1,
                lat, lng, description, sighting_time,
                status, score, tracking_token, contact_email
            ) VALUES (
                '狗', '中', '短毛', '沒有', '無',
                '黑/深棕', '看不清楚', '',
                25.0295, 121.5360, '躲在紅色車輛車底', '2026-10-01 23:20',
                '待確認', 0, 'token_xyz123', 'finder@example.com'
            )
        """)

  # 4. 修正既有資料（每次啟動都跑，已修正過的資料不會再被改到）
  #    (1) 舊資料的時間若含 T 或秒數，統一成 'YYYY-MM-DD HH:MM'
  cursor.execute("""
        UPDATE lost_reports SET lost_time = substr(replace(lost_time, 'T', ' '), 1, 16)
        WHERE lost_time LIKE '%T%' OR length(lost_time) > 16
    """)
  cursor.execute("""
        UPDATE sightings SET sighting_time = substr(replace(sighting_time, 'T', ' '), 1, 16)
        WHERE sighting_time LIKE '%T%' OR length(sighting_time) > 16
    """)
  #    (2) 清空指向不存在檔案的種子照片
  for table in ("lost_reports", "sightings"):
    cursor.execute(
        f"UPDATE {table} SET photo_1 = '' WHERE photo_1 IN (?, ?)",
        MISSING_SEED_PHOTOS,
    )

  conn.commit()
  conn.close()


# 全域執行初始化建表，符合 Gunicorn 與本地開發標準
init_db()


@app.errorhandler(413)
def file_too_large(_err):
  """上傳超過 MAX_CONTENT_LENGTH 時回傳前端看得懂的 JSON，而不是 HTML 錯誤頁"""
  return jsonify({"error": "照片太大，請改選較小的照片"}), 413


# ==========================================
# 頁面路由
# ==========================================
@app.route("/")
def index():
  """首頁路由：回傳地圖頁面"""
  return render_template("index.html")


@app.route("/lost-form")
def lost_form():
  """飼主走失公告表單頁面"""
  return render_template("lost_form.html")


@app.route("/sighting-form")
def sighting_form():
  """協尋者目擊通報表單頁面（手機優先）"""
  return render_template("sighting_form.html")


@app.route("/track/<token>")
def track_sighting(token):
  """協尋者追蹤頁：用送出成功時拿到的 token 查詢自己那筆通報的最新狀態。

  不需登入，連結本身就是身分；頁面只顯示通報本身與關聯公告的公開資訊，
  不顯示任何人的聯絡方式。
  """
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      """
        SELECT s.species, s.size, s.fur_color, s.description, s.sighting_time,
               s.status, s.photo_1, l.pet_name, l.case_code, l.status AS lost_status
        FROM sightings s
        LEFT JOIN lost_reports l ON l.id = s.matched_lost_id
        WHERE s.tracking_token = ?
    """,
      (token,),
  )
  row = cursor.fetchone()
  conn.close()

  if not row:
    return render_template("track.html", sighting=None), 404
  return render_template("track.html", sighting=dict(row))


# ==========================================
# API 路由
# ==========================================
@app.route("/api/map-data")
def get_map_data():
  """檢查點三：整合兩張表的公開地圖資料回傳 JSON"""
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 撈取走失公告（隱私保護：使用公開模糊座標 lat_public / lng_public，且不外洩 Email）
  cursor.execute("""
        SELECT id, case_code, pet_name, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark,
               lat_public AS lat, lng_public AS lng, lost_time, status
        FROM lost_reports
        WHERE status = '尋找中'
    """)
  lost_rows = cursor.fetchall()
  lost_reports = [dict(row) for row in lost_rows]

  # 2. 撈取目擊通報
  cursor.execute("""
        SELECT id, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark,
               lat, lng, description, sighting_time, status
        FROM sightings
    """)
  sighting_rows = cursor.fetchall()
  sightings = [dict(row) for row in sighting_rows]

  conn.close()
  return jsonify({"lost_reports": lost_reports, "sightings": sightings})


@app.route("/api/upload", methods=["POST"])
def upload_file():
  """通用照片上傳 API：儲存檔案至 static/uploads 並回傳路徑"""
  if "photo" not in request.files:
    return jsonify({"error": "沒有上傳檔案"}), 400

  file = request.files["photo"]
  if file.filename == "":
    return jsonify({"error": "未選擇任何檔案"}), 400

  ext = os.path.splitext(file.filename)[1].lower()
  if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
    return jsonify({"error": "僅支援 jpg、png、webp 格式圖片"}), 400

  new_filename = f"photo_{int(time.time() * 1000)}{ext}"
  save_path = os.path.join(UPLOAD_FOLDER, new_filename)
  file.save(save_path)

  public_path = f"/static/uploads/{new_filename}"
  return jsonify({"file_path": public_path})


def calculate_public_coords(lat, lng, radius_meters):
  """隱私模糊演算法：依據選擇的半徑 (30m 或 50m) 進行隨機角度與距離的固定偏移"""
  angle = random.uniform(0, 2 * math.pi)
  distance = random.uniform(radius_meters * 0.7, radius_meters)
  delta_lat = (distance * math.cos(angle)) / 111000.0
  delta_lng = (distance * math.sin(angle)) / (
      111000.0 * math.cos(math.radians(lat))
  )
  return round(lat + delta_lat, 6), round(lng + delta_lng, 6)


@app.route("/api/lost_reports", methods=["POST"])
def create_lost_report():
  """接收飼主發布走失公告，寫入 SQLite"""
  data = request.get_json()
  if not data:
    return jsonify({"error": "未收到任何資料"}), 400

  if not data.get("pet_name"):
    return jsonify({"error": "請填寫寵物名字"}), 400
  if not data.get("photo_1"):
    return jsonify({"error": "第 1 張全身照為必填項目"}), 400
  if not data.get("lat") or not data.get("lng"):
    return jsonify({"error": "請務必點擊按鈕帶入走失地點座標"}), 400

  # 時間統一成 matcher.py 看得懂的格式（把 datetime-local 的 T 換成空格）
  lost_time = normalize_time(data.get("lost_time"))
  if not lost_time:
    return jsonify({"error": "請填寫正確的走失時間"}), 400

  email = (data.get("email") or "").strip()
  email_pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
  if not email or not re.match(email_pattern, email):
    return jsonify({"error": "請填寫正確格式的 Email 信箱"}), 400

  try:
    lat_precise = float(data.get("lat"))
    lng_precise = float(data.get("lng"))
    privacy_radius = int(data.get("privacy_radius", 50))
  except ValueError:
    return jsonify({"error": "座標或模糊半徑格式不正確"}), 400

  lat_public, lng_public = calculate_public_coords(
      lat_precise, lng_precise, privacy_radius
  )
  case_code = f"LOST-{random.randint(1000, 9999)}"

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()

  cursor.execute(
      """
        INSERT INTO lost_reports (
            case_code, pet_name, species, size, fur_length,
            accessory, accessory_color, fur_color, special_mark, notes,
            photo_1, photo_2, photo_3,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '尋找中')
    """,
      (
          case_code,
          data.get("pet_name").strip(),
          data.get("species", "狗"),
          data.get("size", "中"),
          data.get("fur_length", "短毛"),
          data.get("accessory", "沒有"),
          data.get("accessory_color", "無"),
          data.get("fur_color", "黑/深棕"),
          data.get("special_mark", "沒有"),
          data.get("notes", ""),
          data.get("photo_1"),
          data.get("photo_2", ""),
          data.get("photo_3", ""),
          lat_precise,
          lng_precise,
          lat_public,
          lng_public,
          privacy_radius,
          lost_time,
          email,
      ),
  )

  new_id = cursor.lastrowid
  conn.commit()
  conn.close()

  return (
      jsonify({
          "message": "公告發布成功！",
          "id": new_id,
          "case_code": case_code,
          "lat_public": lat_public,
          "lng_public": lng_public,
      }),
      201,
  )


@app.route("/api/sightings", methods=["POST"])
def create_sighting():
  """【檢查點二】協尋者目擊通報 API：

  1. 先存檔保底寫入 sightings
  2. 自動產生專屬 tracking_token
  3. 反向撈取尋找中的走失公告進行加權比對
  4. 回傳外觀分數 >= 60 的候選清單（依時空分數由高至低排序）
  """
  data = request.get_json()
  if not data:
    return jsonify({"error": "未收到任何通報資料"}), 400

  # 1. 基礎必填驗證（前端已擋過一次，後端再擋一次，避免繞過前端直接打 API）
  if not data.get("lat") or not data.get("lng"):
    return jsonify({"error": "缺少目擊地點座標"}), 400
  if data.get("species") not in ("狗", "貓"):
    return jsonify({"error": "缺少物種資訊"}), 400
  if not data.get("photo_1"):
    return jsonify({"error": "請至少上傳 1 張目擊照片"}), 400

  try:
    lat = float(data.get("lat"))
    lng = float(data.get("lng"))
  except (TypeError, ValueError):
    return jsonify({"error": "座標格式不正確"}), 400

  # sighting_time 必填：空值會讓 matcher.py 用 0.5 天計算，在排序上占便宜
  sighting_time = normalize_time(data.get("sighting_time"))
  if not sighting_time:
    return jsonify({"error": "請填寫正確的目擊時間"}), 400

  # email 選填；有填才檢查格式
  contact_email = (data.get("contact_email") or "").strip()
  if contact_email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", contact_email):
    return jsonify({"error": "Email 格式不正確"}), 400

  tracking_token = f"token_{uuid.uuid4().hex[:8]}"

  # 2. 先存檔保底：寫入 sightings 資料表
  conn = get_db_connection()
  cursor = conn.cursor()

  cursor.execute(
      """
        INSERT INTO sightings (
            species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, photo_1, photo_2, photo_3,
            lat, lng, description, sighting_time,
            status, score, tracking_token, contact_email
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '待確認', 0, ?, ?)
    """,
      (
          data.get("species"),
          data.get("size", "中"),
          data.get("fur_length", "短毛"),
          data.get("accessory", "沒有"),
          data.get("accessory_color", "無"),
          data.get("fur_color", "黑/深棕"),
          data.get("special_mark", "沒有"),
          data.get("photo_1", ""),
          data.get("photo_2", ""),
          data.get("photo_3", ""),
          lat,
          lng,
          (data.get("description") or "").strip(),
          sighting_time,
          tracking_token,
          contact_email,
      ),
  )

  new_sighting_id = cursor.lastrowid
  conn.commit()

  # 3. 反向查詢推薦：撈出所有 status = '尋找中' 的走失公告進行算分
  cursor.execute("""
        SELECT id, case_code, pet_name, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark,
               lat_precise, lng_precise, lost_time, photo_1
        FROM lost_reports
        WHERE status = '尋找中'
    """)
  lost_candidates = cursor.fetchall()
  conn.close()

  candidates = []
  current_sighting = {
      "species": data.get("species"),
      "size": data.get("size", "中"),
      "fur_length": data.get("fur_length", "短毛"),
      "accessory": data.get("accessory", "沒有"),
      "accessory_color": data.get("accessory_color", "無"),
      "fur_color": data.get("fur_color", "黑/深棕"),
      "special_mark": data.get("special_mark", "沒有"),
      "lat": lat,
      "lng": lng,
      "sighting_time": sighting_time,
  }

  for row in lost_candidates:
    lost_obj = {
        "species": row["species"],
        "size": row["size"],
        "fur_length": row["fur_length"],
        "accessory": row["accessory"],
        "accessory_color": row["accessory_color"],
        "fur_color": row["fur_color"],
        "special_mark": row["special_mark"],
        "lat": row["lat_precise"],  # 使用後端精確座標比對
        "lng": row["lng_precise"],
        "lost_time": row["lost_time"],
    }

    match_res = calculate_match(lost_obj, current_sighting)

    # 門檻過濾：排除者剔除，外觀分數 >= 60 才進入反向推薦清單
    if not match_res["is_excluded"] and match_res["appearance_score"] >= 60:
      candidates.append({
          "lost_id": row["id"],
          "case_code": row["case_code"],
          "pet_name": row["pet_name"] or "未命名",
          "photo_1": row["photo_1"],
          "appearance_score": match_res["appearance_score"],
          "match_level": match_res["match_level"],
          "distance_km": match_res["distance_km"],
          "spatiotemporal_score": match_res["spatiotemporal_score"],
          "details": match_res["score_details"],
      })

  # 4. 清單內部依「時空分數」由高到低排序
  candidates.sort(key=lambda x: x["spatiotemporal_score"], reverse=True)

  return (
      jsonify({
          "message": "目擊通報已存檔！",
          "sighting_id": new_sighting_id,
          "tracking_token": tracking_token,
          "candidates_count": len(candidates),
          "candidates": candidates,
      }),
      201,
  )


#=========================================
@app.route("/api/sightings/<int:sighting_id>/match", methods=["POST"])
def match_sighting(sighting_id):
  """【檢查點三】協尋者反向指認 API（「就是這隻！」）：

  1. 接收協尋者選擇的 lost_id
  2. 將 sightings 表中的 matched_lost_id 綁定該公告
  3. 將 status 更新為 '已通知飼主'，並記錄分數
  """
  data = request.get_json() or {}
  lost_id = data.get("lost_id")

  if not lost_id:
    return jsonify({"error": "缺少要關聯的走失公告 ID (lost_id)"}), 400

  conn = get_db_connection()
  cursor = conn.cursor()

  # 確認該通報存在
  cursor.execute(
      "SELECT id, status FROM sightings WHERE id = ?", (sighting_id,)
  )
  sighting = cursor.fetchone()
  if not sighting:
    conn.close()
    return jsonify({"error": f"找不到通報 ID {sighting_id}"}), 404

  # 飼主已確認或已排除的通報不能再被改綁，避免覆蓋飼主的判斷
  if sighting["status"] in ("飼主已確認", "飼主已排除"):
    conn.close()
    return jsonify({"error": "這筆通報飼主已處理過，無法再指認"}), 409

  # 確認該走失公告存在
  cursor.execute(
      "SELECT id, case_code, pet_name, email FROM lost_reports WHERE id = ?",
      (lost_id,),
  )
  lost = cursor.fetchone()
  if not lost:
    conn.close()
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404

  score = data.get("score", 100)

  # 更新目擊通報狀態與關聯外鍵
  cursor.execute(
      """
        UPDATE sightings
        SET matched_lost_id = ?,
            status = '已通知飼主',
            score = ?
        WHERE id = ?
    """,
      (lost_id, score, sighting_id),
  )

  conn.commit()
  conn.close()

  return (
      jsonify({
          "message": "指認成功！已關聯公告並標記為「已通知飼主」",
          "sighting_id": sighting_id,
          "matched_lost_id": lost_id,
          "case_code": lost["case_code"],
          "pet_name": lost["pet_name"],
          "status": "已通知飼主",
      }),
      200,
  )


#==========================================


# ==========================================
# 【檢查點四】飼主通知查詢與確認結案 API
# ==========================================
@app.route("/api/lost_reports/<int:lost_id>/notifications", methods=["GET"])
def get_lost_notifications(lost_id):
  """查詢特定走失公告的通知清單（即時計算版）：

  1. 撈出這筆走失公告本身的特徵
  2. 撈出所有「還沒結案」的目擊通報（排除已被飼主確認/排除的）
  3. 逐筆套用 calculate_match，外觀分數 >= 60 才算有效通知
  4. 已被協尋者手動指認（matched_lost_id = 本案）的通報，無論分數都強制納入
  5. 依時空分數由高到低排序回傳

  （修正：原版只讀 sightings.score，但該欄位在自動比對時從未被寫入，
  導致 >=60 分的候選永遠不會出現在飼主的通知清單裡。改成跟
  create_sighting() 一樣即時計算，兩邊都不依賴預先存好的分數，
  也能接住「飼主比協尋者晚發公告」的情況。）
  """
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 先確認走失公告存在，並取出比對所需欄位
  cursor.execute(
      """
        SELECT species, size, fur_length, accessory, accessory_color,
               fur_color, special_mark, lat_precise, lng_precise, lost_time
        FROM lost_reports WHERE id = ?
    """,
      (lost_id,),
  )
  lost_row = cursor.fetchone()
  if not lost_row:
    conn.close()
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404

  lost_obj = {
      "species": lost_row["species"],
      "size": lost_row["size"],
      "fur_length": lost_row["fur_length"],
      "accessory": lost_row["accessory"],
      "accessory_color": lost_row["accessory_color"],
      "fur_color": lost_row["fur_color"],
      "special_mark": lost_row["special_mark"],
      "lat": lost_row["lat_precise"],
      "lng": lost_row["lng_precise"],
      "lost_time": lost_row["lost_time"],
  }

  # 2. 撈出所有還沒結案的目擊通報（已被飼主確認/排除的不再參與比對）
  cursor.execute(
      """
        SELECT id, species, size, fur_length, accessory, accessory_color,
               fur_color, special_mark, photo_1, lat, lng, description,
               sighting_time, status, matched_lost_id, tracking_token, contact_email
        FROM sightings
        WHERE status NOT IN ('飼主已確認', '飼主已排除')
    """
  )
  sighting_rows = cursor.fetchall()
  conn.close()

  notifications = []
  for row in sighting_rows:
    sighting_obj = {
        "species": row["species"],
        "size": row["size"],
        "fur_length": row["fur_length"],
        "accessory": row["accessory"],
        "accessory_color": row["accessory_color"],
        "fur_color": row["fur_color"],
        "special_mark": row["special_mark"],
        "lat": row["lat"],
        "lng": row["lng"],
        "sighting_time": row["sighting_time"],
    }

    match_res = calculate_match(lost_obj, sighting_obj)

    # 3. 門檛判斷：外觀分數 >= 60 才算通知；但已被協尋者手動指認本案的，強制納入
    already_picked = row["matched_lost_id"] == lost_id
    if already_picked or (
        not match_res["is_excluded"] and match_res["appearance_score"] >= 60
    ):
      notifications.append({
          "sighting_id": row["id"],
          "photo_1": row["photo_1"],
          "description": row["description"],
          "sighting_time": row["sighting_time"],
          "status": row["status"],
          "tracking_token": row["tracking_token"],
          "contact_email": row["contact_email"],
          "appearance_score": match_res["appearance_score"],
          "match_level": match_res["match_level"],
          "spatiotemporal_score": match_res["spatiotemporal_score"],
          "distance_km": match_res["distance_km"],
          "manually_confirmed": already_picked,
      })

  # 4. 依時空分數排序（手動指認的已經是鐵板,但排序時一樣用分數排,簡單一致）
  notifications.sort(key=lambda x: x["spatiotemporal_score"], reverse=True)

  return (
      jsonify({
          "lost_id": lost_id,
          "notification_count": len(notifications),
          "notifications": notifications,
      }),
      200,
  )


@app.route("/api/lost_reports/<int:lost_id>/confirm_found", methods=["POST"])
def confirm_found_pet(lost_id):
  """飼主點擊「這是我的寶貝！」完成確認閉環：

  1. lost_reports.status -> '已尋回'（系統停止參與後續比對）
  2. 該公告對應的通報 sightings.status -> '飼主已確認'
  3. 回傳通知協尋者的確認訊息
  """
  data = request.get_json() or {}
  sighting_id = data.get("sighting_id")

  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 將走失公告改為「已尋回」
  cursor.execute(
      "UPDATE lost_reports SET status = '已尋回' WHERE id = ?", (lost_id,)
  )

  # 2. 若有指定被確認的通報 ID，將其改為「飼主已確認」
  if sighting_id:
    cursor.execute(
        "UPDATE sightings SET status = '飼主已確認', matched_lost_id = ? WHERE"
        " id = ?",
        (lost_id, sighting_id),
    )
  else:
    # 若未指定特定通報，將所有關聯該公告的通報皆更新
    cursor.execute(
        "UPDATE sightings SET status = '飼主已確認' WHERE matched_lost_id = ?",
        (lost_id,),
    )

  conn.commit()
  conn.close()

  return (
      jsonify({
          "message": "恭喜！已確認尋回寶貝，案件正式結案！",
          "lost_id": lost_id,
          "lost_status": "已尋回",
          "sighting_status": "飼主已確認",
      }),
      200,
  )

#=========================================

if __name__ == "__main__":
  app.run(debug=True, port=5000)
