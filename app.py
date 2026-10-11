import json
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
from matcher import NOTIFY_THRESHOLD, calculate_match, compare_special_marks, rank_key

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


# 公開給前端的欄位清單：刻意不含 email、精確座標、追蹤碼，
# 避免任何人（包含飼主與協尋者彼此）從 API 拿到對方的聯絡方式或精確位置
LOST_PUBLIC_FIELDS = """
    id, case_code, pet_name, species, size, fur_length, accessory, accessory_color,
    fur_color, special_mark, special_mark_note, notes, photo_1, photo_2, photo_3,
    lat_public, lng_public, lost_time, status
"""
SIGHTING_PUBLIC_FIELDS = """
    id, species, size, fur_length, accessory, accessory_color, fur_color,
    special_mark, special_mark_note, photo_1, photo_2, photo_3, description,
    sighting_time, status, matched_lost_id
"""

# 比對用的欄位（matcher.py 需要的 key）
MATCH_KEYS = (
    "species", "size", "fur_length", "accessory",
    "accessory_color", "fur_color", "special_mark",
)


def mark_note_of(row):
  """取出「其他」特殊記號的手寫描述；舊資料或查詢沒選這個欄位時回傳空字串"""
  return (row["special_mark_note"] or "") if "special_mark_note" in row.keys() else ""


def lost_to_match_obj(row):
  """把 lost_reports 的一列轉成 calculate_match() 要的字典（用精確座標計算距離）"""
  obj = {k: row[k] for k in MATCH_KEYS}
  obj["special_mark_note"] = mark_note_of(row)
  obj.update(lat=row["lat_precise"], lng=row["lng_precise"], lost_time=row["lost_time"])
  return obj


def sighting_to_match_obj(row):
  """把 sightings 的一列轉成 calculate_match() 要的字典"""
  obj = {k: row[k] for k in MATCH_KEYS}
  obj["special_mark_note"] = mark_note_of(row)
  obj.update(lat=row["lat"], lng=row["lng"], sighting_time=row["sighting_time"])
  return obj


def accessory_text(acc, color):
  """配件顯示文字：沒有 → 沒有；有 → 顏色＋類型，例如「紅色項圈」"""
  if not acc or acc == "沒有":
    return "沒有"
  return f"{color or ''}{acc}"


def build_compare(lost, sighting):
  """詳情頁的「特徵並排」：逐欄比較飼主公告與協尋者通報，標出吻合狀態。

  status 共四種，前端依此上色：
    match   吻合
    partial 部分吻合（體型差一級、配件同類但顏色不同）
    maybe   可能脫落（飼主有配件、協尋者沒看到；計分規則也不扣分）
    unclear 看不清楚（如實顯示，計分給中立分）
    diff    不同
  """
  size_rank = {"小": 1, "中": 2, "大": 3}
  rows = []

  def add(label, l_val, s_val, status, note=""):
    rows.append({"label": label, "lost": l_val, "sighting": s_val,
                 "status": status, "note": note})

  # 物種、毛長、毛色：相同即吻合
  add("物種", lost["species"], sighting["species"],
      "match" if lost["species"] == sighting["species"] else "diff")

  # 體型：差一級仍計分，標成部分吻合
  gap = abs(size_rank.get(lost["size"], 2) - size_rank.get(sighting["size"], 2))
  add("體型", f"{lost['size']}型", f"{sighting['size']}型",
      "match" if gap == 0 else ("partial" if gap == 1 else "diff"),
      "差一級，目測容易有誤差" if gap == 1
      else ("差兩級，照片沒有比例尺，請看照片判斷" if gap == 2 else ""))

  add("毛長", lost["fur_length"], sighting["fur_length"],
      "match" if lost["fur_length"] == sighting["fur_length"] else "diff")
  add("毛色色系", lost["fur_color"], sighting["fur_color"],
      "match" if lost["fur_color"] == sighting["fur_color"] else "diff")

  # 配件
  l_acc, s_acc = lost["accessory"], sighting["accessory"]
  l_text = accessory_text(l_acc, lost["accessory_color"])
  s_text = accessory_text(s_acc, sighting["accessory_color"])
  if l_acc == "沒有" and s_acc == "沒有":
    add("配件", l_text, s_text, "match", "雙方都沒有配件")
  elif l_acc != "沒有" and s_acc == "沒有":
    add("配件", l_text, "沒有 / 沒看到", "maybe", "走失後配件可能已脫落")
  elif l_acc == s_acc:
    same_color = lost["accessory_color"] == sighting["accessory_color"]
    add("配件", l_text, s_text, "match" if same_color else "partial",
        "" if same_color else "類型相同、顏色不同")
  else:
    add("配件", l_text, s_text, "diff")

  # 特殊記號：直接用 matcher.py 的同一套規則判斷，畫面與計分不會不一致
  l_mark, s_mark = lost["special_mark"], sighting["special_mark"]
  l_note, s_note = mark_note_of(lost), mark_note_of(sighting)
  l_text = f"其他：{l_note}" if l_mark == "其他" and l_note else l_mark
  s_text = f"其他：{s_note}" if s_mark == "其他" and s_note else s_mark
  score, reason = compare_special_marks(l_mark, l_note, s_mark, s_note)
  if "看不清楚" in (l_mark, s_mark):
    add("特殊記號", l_text, s_text, "unclear", "協尋者看不清楚，請用照片確認")
  elif l_mark == s_mark == "沒有":
    add("特殊記號", l_text, s_text, "match", "雙方都沒有特殊記號")
  elif score == 35:
    add("特殊記號", l_text, s_text, "match",
        "描述的部位與特徵相同" if "其他" in (l_mark, s_mark) else "")
  elif score == 20 and "沒有" in (l_mark, s_mark):
    add("特殊記號", l_text, s_text, "partial", "描述沒有明確的特殊特徵，請對照照片確認")
  elif score == 20:
    add("特殊記號", l_text, s_text, "partial", "部位相同、特徵不同，請對照照片確認")
  elif score == 10:
    add("特殊記號", l_text, s_text, "unclear", "描述無法自動比對，請自己對照文字")
  else:
    add("特殊記號", l_text, s_text, "diff")

  return rows


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

  # 2.2 飼主排除紀錄：記下「哪一則公告的飼主」排除了「哪一筆通報」
  #     不能只改 sightings.status，因為一筆通報可能同時符合多則公告：
  #     飼主 A 說「不是我的」，不代表也不是飼主 B 的，B 仍然要看得到。
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS sighting_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lost_id INTEGER NOT NULL,
            sighting_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (lost_id, sighting_id)
        )
    """)

  # 2.5 補上舊資料庫缺少的欄位
  #     CREATE TABLE IF NOT EXISTS 只會在「表不存在」時建表；如果表是舊版程式建的，
  #     之後新增的欄位（例如 pet_name）不會自動補上，查詢時就會出現 no such column。
  #     所以逐一檢查欄位，缺少的用 ALTER TABLE 補上（補上的欄位舊資料會是空值）。
  ensure_columns(cursor, "lost_reports", {
      "pet_name": "TEXT",
      # 特殊記號選「其他」時，飼主手寫的描述（用關鍵字規則跟協尋者描述比對）
      "special_mark_note": "TEXT",
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
      # AI 原始預填結果（JSON 文字）：只用來統計協尋者改了多少，不參與計分
      "ai_prefill": "TEXT",
      # 特殊記號選「其他」時，目擊者手寫的描述（只給飼主參考，不參與計分）
      "special_mark_note": "TEXT",
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


@app.route("/case/<int:lost_id>")
def case_page(lost_id):
  """飼主的案件頁（通知欄）：列出這則公告收到的目擊通報"""
  return render_template("case.html", lost_id=lost_id)


@app.route("/case/<int:lost_id>/sighting/<int:sighting_id>")
def sighting_detail_page(lost_id, sighting_id):
  """飼主看單筆通報的詳情頁：照片與特徵並排，按下確認或排除"""
  return render_template(
      "sighting_detail.html", lost_id=lost_id, sighting_id=sighting_id
  )


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
  #    photo_1 給紅色圖釘的彈出視窗顯示公告照片
  cursor.execute("""
        SELECT id, case_code, pet_name, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark,
               photo_1, lat_public AS lat, lng_public AS lng, lost_time, status
        FROM lost_reports
        WHERE status = '尋找中'
    """)
  lost_rows = cursor.fetchall()
  lost_reports = [dict(row) for row in lost_rows]

  # 2. 撈取目擊通報（photo_1 給藍色圖釘的彈出視窗顯示目擊照片；不含 email 與追蹤碼）
  cursor.execute("""
        SELECT id, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark,
               photo_1, lat, lng, description, sighting_time, status
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

  # 特殊記號選「其他」時必須描述內容（會用來跟協尋者的描述比對）
  special_mark_note = (data.get("special_mark_note") or "").strip()[:50]
  if data.get("special_mark") == "其他":
    if not special_mark_note:
      return jsonify({"error": "特殊記號選「其他」時，請描述寵物的特徵"}), 400
  else:
    special_mark_note = ""

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
            accessory, accessory_color, fur_color, special_mark, special_mark_note, notes,
            photo_1, photo_2, photo_3,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '尋找中')
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
          special_mark_note,
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
  4. 回傳外觀分數 >= NOTIFY_THRESHOLD（40）的候選清單
     （60 分以上排前面，同一級內依時空分數由高至低）
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

  # 特殊記號選「其他」時必須描述內容，其餘情況不存
  special_mark_note = (data.get("special_mark_note") or "").strip()[:50]
  if data.get("special_mark") == "其他":
    if not special_mark_note:
      return jsonify({"error": "特殊記號選「其他」時，請描述你看到的特徵"}), 400
  else:
    special_mark_note = ""

  # AI 原始預填：只接受物件，轉成 JSON 文字另存；格式不對就當作沒有，不擋通報
  ai_prefill = data.get("ai_prefill")
  ai_prefill_json = (
      json.dumps(ai_prefill, ensure_ascii=False)[:4000]
      if isinstance(ai_prefill, dict) else None
  )

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
            status, score, tracking_token, contact_email, ai_prefill, special_mark_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '待確認', 0, ?, ?, ?, ?)
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
          ai_prefill_json,
          special_mark_note,
      ),
  )

  new_sighting_id = cursor.lastrowid
  conn.commit()

  # 3. 反向查詢推薦：撈出所有 status = '尋找中' 的走失公告進行算分
  cursor.execute("""
        SELECT id, case_code, pet_name, species, size, fur_length,
               accessory, accessory_color, fur_color, special_mark, special_mark_note,
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
      "special_mark_note": special_mark_note,
      "lat": lat,
      "lng": lng,
      "sighting_time": sighting_time,
  }

  for row in lost_candidates:
    lost_obj = lost_to_match_obj(row)  # 使用後端精確座標比對

    match_res = calculate_match(lost_obj, current_sighting)

    # 門檻過濾：排除者剔除，外觀分數 >= 40 就進入反向推薦清單（寧可多推，不要漏掉）
    if not match_res["is_excluded"] and match_res["appearance_score"] >= NOTIFY_THRESHOLD:
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

  # 4. 排序：先看外觀等級（60 分以上優先），同一級內再依時空分數由高到低
  candidates.sort(
      key=lambda x: rank_key(x["appearance_score"], x["spatiotemporal_score"]),
      reverse=True,
  )

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
# 【檢查點四】飼主端 API：公告詳情、通知清單、通報詳情、確認 / 排除
# ==========================================
@app.route("/api/lost_reports/<int:lost_id>", methods=["GET"])
def get_lost_report(lost_id):
  """取單則走失公告的公開資訊（不含 email 與精確座標）。

  案件頁、詳情頁、協尋者從「我看過這隻」進入通報時都會用到。
  """
  conn = get_db_connection()
  row = conn.execute(
      f"SELECT {LOST_PUBLIC_FIELDS} FROM lost_reports WHERE id = ?", (lost_id,)
  ).fetchone()
  conn.close()
  if not row:
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
  return jsonify(dict(row)), 200


@app.route("/api/lost_reports/<int:lost_id>/notifications", methods=["GET"])
def get_lost_notifications(lost_id):
  """查詢特定走失公告的通知清單（即時計算版）：

  1. 撈出這筆走失公告本身的特徵
  2. 撈出還沒被認領的目擊通報，並排除「這則公告的飼主」已經排除過的
  3. 逐筆套用 calculate_match，外觀分數 >= NOTIFY_THRESHOLD（40）就算有效通知
  4. 已被協尋者手動指認（matched_lost_id = 本案）的通報，無論分數都強制納入
  5. 依時空分數由高到低排序回傳

  隱私：回傳內容不含協尋者的 email 與追蹤碼。
  """
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 先確認走失公告存在，並取出比對所需欄位
  cursor.execute(
      """
        SELECT species, size, fur_length, accessory, accessory_color,
               fur_color, special_mark, special_mark_note, lat_precise, lng_precise, lost_time
        FROM lost_reports WHERE id = ?
    """,
      (lost_id,),
  )
  lost_row = cursor.fetchone()
  if not lost_row:
    conn.close()
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
  lost_obj = lost_to_match_obj(lost_row)

  # 2. 撈出候選通報：
  #    - 已被飼主確認（寵物已找到）的通報不再推給任何人
  #    - 本案飼主按過「不是我的寶貝」的通報，不再出現在本案的通知欄
  cursor.execute(
      """
        SELECT id, species, size, fur_length, accessory, accessory_color,
               fur_color, special_mark, special_mark_note, photo_1, lat, lng, description,
               sighting_time, status, matched_lost_id
        FROM sightings
        WHERE status != '飼主已確認'
          AND id NOT IN (
              SELECT sighting_id FROM sighting_rejections WHERE lost_id = ?
          )
    """,
      (lost_id,),
  )
  sighting_rows = cursor.fetchall()
  conn.close()

  notifications = []
  for row in sighting_rows:
    match_res = calculate_match(lost_obj, sighting_to_match_obj(row))

    # 3. 門檛判斷：外觀分數 >= 40 就通知；但已被協尋者手動指認本案的，強制納入
    already_picked = row["matched_lost_id"] == lost_id
    if already_picked or (
        not match_res["is_excluded"] and match_res["appearance_score"] >= NOTIFY_THRESHOLD
    ):
      notifications.append({
          "sighting_id": row["id"],
          "photo_1": row["photo_1"],
          "description": row["description"],
          "sighting_time": row["sighting_time"],
          "status": row["status"],
          "appearance_score": match_res["appearance_score"],
          "match_level": match_res["match_level"],
          "spatiotemporal_score": match_res["spatiotemporal_score"],
          "distance_km": match_res["distance_km"],
          "manually_confirmed": already_picked,
      })

  # 4. 排序：先看外觀等級（60 分以上優先），同一級內再依時空分數由高到低
  notifications.sort(
      key=lambda x: rank_key(x["appearance_score"], x["spatiotemporal_score"]),
      reverse=True,
  )

  return (
      jsonify({
          "lost_id": lost_id,
          "notification_count": len(notifications),
          "notifications": notifications,
      }),
      200,
  )


@app.route(
    "/api/lost_reports/<int:lost_id>/sightings/<int:sighting_id>", methods=["GET"]
)
def get_sighting_for_lost(lost_id, sighting_id):
  """詳情頁資料：一則公告 + 一筆通報 + 兩者的比對結果與特徵並排。

  距離用後端精確座標計算，前端只拿到算好的公里數，拿不到精確座標。
  """
  conn = get_db_connection()
  lost = conn.execute(
      f"SELECT {LOST_PUBLIC_FIELDS}, lat_precise, lng_precise FROM lost_reports WHERE id = ?",
      (lost_id,),
  ).fetchone()
  sighting = conn.execute(
      f"SELECT {SIGHTING_PUBLIC_FIELDS}, lat, lng FROM sightings WHERE id = ?",
      (sighting_id,),
  ).fetchone()
  rejected = conn.execute(
      "SELECT 1 FROM sighting_rejections WHERE lost_id = ? AND sighting_id = ?",
      (lost_id, sighting_id),
  ).fetchone()
  conn.close()

  if not lost:
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
  if not sighting:
    return jsonify({"error": f"找不到通報 ID {sighting_id}"}), 404

  match_res = calculate_match(lost_to_match_obj(lost), sighting_to_match_obj(sighting))

  # 飼主的精確座標不回傳（只給公開模糊座標 lat_public / lng_public）；
  # 目擊座標要回傳，飼主才知道去哪裡找（它本來就以藍色圖釘公開顯示在地圖上）
  lost_public = {k: lost[k] for k in lost.keys() if k not in ("lat_precise", "lng_precise")}
  sighting_public = dict(sighting)

  return jsonify({
      "lost": lost_public,
      "sighting": sighting_public,
      "match": {
          "appearance_score": match_res["appearance_score"],
          "match_level": match_res["match_level"],
          "distance_km": match_res["distance_km"],
          "spatiotemporal_score": match_res["spatiotemporal_score"],
      },
      "compare": build_compare(lost, sighting),
      "manually_confirmed": sighting["matched_lost_id"] == lost_id,
      "rejected_by_owner": bool(rejected),
  }), 200


@app.route("/api/lost_reports/<int:lost_id>/confirm_found", methods=["POST"])
def confirm_found_pet(lost_id):
  """飼主點擊「這是我的寶貝！」完成確認閉環：

  1. lost_reports.status -> '已尋回'（系統停止參與後續比對，地圖紅釘消失）
  2. 該公告對應的通報 sightings.status -> '飼主已確認'
  3. 回傳通知協尋者的確認訊息
  """
  data = request.get_json() or {}
  sighting_id = data.get("sighting_id")

  conn = get_db_connection()
  cursor = conn.cursor()

  lost = cursor.execute(
      "SELECT status FROM lost_reports WHERE id = ?", (lost_id,)
  ).fetchone()
  if not lost:
    conn.close()
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
  if lost["status"] != "尋找中":
    conn.close()
    return jsonify({"error": f"這則公告已經是「{lost['status']}」，不能重複確認"}), 409
  if sighting_id and not cursor.execute(
      "SELECT 1 FROM sightings WHERE id = ?", (sighting_id,)
  ).fetchone():
    conn.close()
    return jsonify({"error": f"找不到通報 ID {sighting_id}"}), 404

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


@app.route("/api/lost_reports/<int:lost_id>/reject_sighting", methods=["POST"])
def reject_sighting(lost_id):
  """飼主點擊「不是我的寶貝」：

  1. 在 sighting_rejections 記下「這則公告排除了這筆通報」
     → 這筆通報不再出現在本案的通知欄，但其他飼主仍看得到
  2. sightings.status -> '飼主已排除'（地圖上變成灰色圖釘）
     已被別的飼主確認的通報不改狀態，避免蓋掉「飼主已確認」
  """
  data = request.get_json() or {}
  sighting_id = data.get("sighting_id")
  if not sighting_id:
    return jsonify({"error": "缺少通報 ID (sighting_id)"}), 400

  conn = get_db_connection()
  cursor = conn.cursor()
  if not cursor.execute("SELECT 1 FROM lost_reports WHERE id = ?", (lost_id,)).fetchone():
    conn.close()
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
  if not cursor.execute("SELECT 1 FROM sightings WHERE id = ?", (sighting_id,)).fetchone():
    conn.close()
    return jsonify({"error": f"找不到通報 ID {sighting_id}"}), 404

  # INSERT OR IGNORE：重複按也只會有一筆紀錄
  cursor.execute(
      "INSERT OR IGNORE INTO sighting_rejections (lost_id, sighting_id) VALUES (?, ?)",
      (lost_id, sighting_id),
  )
  cursor.execute(
      "UPDATE sightings SET status = '飼主已排除' WHERE id = ? AND status != '飼主已確認'",
      (sighting_id,),
  )
  conn.commit()
  conn.close()

  return jsonify({
      "message": "已記錄，這筆通報不會再出現在你的通知欄",
      "lost_id": lost_id,
      "sighting_id": sighting_id,
      "sighting_status": "飼主已排除",
  }), 200

#=========================================

if __name__ == "__main__":
  app.run(debug=True, port=5001)
