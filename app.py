import base64
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime
from flask import Flask, abort, jsonify, render_template, render_template_string, request
from werkzeug.middleware.proxy_fix import ProxyFix

# 讀取專案根目錄的 .env（放 Azure 金鑰、DATABASE_URL、CLOUDINARY_URL、LINE 金鑰；沒裝 python-dotenv 也不影響執行）
try:
  from dotenv import load_dotenv

  load_dotenv()
except ImportError:
  pass

# Postgres（Neon）連線：psycopg 3。dict_row 讓查詢結果能像字典一樣用欄位名稱存取，
# 用法跟原本 sqlite3.Row 一樣（row["species"]、dict(row)、row.keys() 都能用）
import psycopg
from psycopg.rows import dict_row

# 呼叫 LINE Messaging API（回覆／推播綁定結果）
import requests

# 照片存放：Cloudinary（需要 CLOUDINARY_URL）。
# Render 每次重新部署或重啟，伺服器上的檔案都會被清掉，所以照片不能再存在 static/uploads
import cloudinary
import cloudinary.uploader

# 引入檢查點一驗證完成的比對核心模組
from matcher import NOTIFY_THRESHOLD, calculate_match, compare_special_marks, rank_key

# AI 照片辨識（Azure AI Foundry）：提供 /api/analyze-photo 預填外觀欄位
from vision import vision_bp

app = Flask(__name__)
app.register_blueprint(vision_bp)

# Render 前面有一層代理：讓 Flask 從 X-Forwarded-Proto / Host 得知原本是 https，
# request.url_root 才會是 https://takemehome-spike.onrender.com/（LINE 回覆裡的連結要用它）
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Neon 的連線字串（請用 Connection pooling 版本，主機名稱含 -pooler）
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
  raise RuntimeError(
      "找不到 DATABASE_URL：本機請寫在 .env，Render 請在 Environment 設定"
  )

# 有設定 CLOUDINARY_URL 才上傳到 Cloudinary；沒設定時退回存在本機 static/uploads，
# 方便還沒申請 Cloudinary 時也能在本機開發（但部署到 Render 一定要設定）
USE_CLOUDINARY = bool(os.environ.get("CLOUDINARY_URL"))
if USE_CLOUDINARY:
  cloudinary.config(secure=True)  # 自動讀取 CLOUDINARY_URL，回傳 https 網址
CLOUDINARY_FOLDER = "takemehome"

# ==========================================
# LINE 設定（階段 0 在 Render → Environment 設好）
# ==========================================
# Channel secret：驗證 webhook 請求真的是 LINE 送來的
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
# Channel access token：呼叫回覆／推播 API 用
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# 機器人的 Basic ID（@ 開頭，例如 @123abcde）：成功頁的「用 LINE 追蹤」按鈕用
LINE_BOT_BASIC_ID = os.environ.get("LINE_BOT_BASIC_ID", "").strip()
# 選填：LINE 回覆裡的網站網址。沒設定就用請求本身的網址（經過 ProxyFix 會是 https）
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
LINE_API_BASE = "https://api.line.me"
# webhook 預設「先回 200、再在背景處理」；測試時設 LINE_WEBHOOK_SYNC=1 改成同步處理，方便立刻檢查結果
LINE_WEBHOOK_SYNC = os.environ.get("LINE_WEBHOOK_SYNC") == "1"

# 單次請求上限 32MB；超過時 Flask 會自動回 413
# 不設 10MB 的原因：飼主表單目前直接上傳手機原圖，AI 辨識一次最多送 5 張照片，
# iPhone 照片轉成 JPEG 後單張就可能超過 5MB，設太小會讓上傳在中途被切斷。
# 協尋者表單已在前端壓縮到約 200KB，不受影響；單張 10MB 的限制由 vision.py 檢查。
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")

# matcher.py 計算天數時使用的時間格式，所有寫入資料庫的時間都要統一成這個格式
TIME_FORMAT = "%Y-%m-%d %H:%M"


@app.context_processor
def inject_line_settings():
  """所有頁面都拿得到 LINE 機器人的 Basic ID（沒設定時是空字串，前端就不顯示 LINE 按鈕）"""
  return {"line_bot_basic_id": LINE_BOT_BASIC_ID}


# ==========================================
# 案件憑證（token）
# ==========================================
# 飼主：own_ + 32 位 16 進位字元（128 位元隨機）。持有它的人才能看案件通知、確認或排除通報。
# 協尋者：trk_ + 32 位 16 進位字元。持有它的人才能追蹤通報、指認公告。
# 公開的數字 id 與 case_code（LOST-1234）只拿來在地圖上辨識，絕對不能當憑證。
OWNER_TOKEN_PREFIX = "own_"
TRACK_TOKEN_PREFIX = "trk_"
OWNER_TOKEN_RE = re.compile(r"^own_[0-9a-f]{32}$")
TRACK_TOKEN_RE = re.compile(r"^trk_[0-9a-f]{32}$")

# 飼主表單的「送出編號」：前端每次開表單產生一組，重送時沿用，用來擋重複發布
CLIENT_SUBMIT_ID_RE = re.compile(r"^[0-9A-Za-z-]{16,64}$")


def new_token(prefix):
  """由伺服器產生一組不可猜的憑證；前端永遠不能自己決定 token"""
  return prefix + secrets.token_hex(16)


def token_matches(stored, given):
  """比對兩個 token 是否相同（固定時間比對，避免從回應時間猜出內容）"""
  if not stored or not isinstance(given, str) or not given:
    return False
  return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))


def find_lost_id_by_owner_token(conn, token):
  """用飼主 token 反查走失公告的 id；格式不對或查不到都回傳 None。

  飼主端所有頁面與 API 都只認 token，由伺服器自己查出是哪一則公告，
  前端沒有機會指定「我要操作哪一個 id」，所以改數字、猜數字都沒有用。
  """
  if not isinstance(token, str) or not OWNER_TOKEN_RE.match(token):
    return None
  row = conn.execute(
      "SELECT id FROM lost_reports WHERE owner_token = %s", (token,)
  ).fetchone()
  return row["id"] if row else None


def owner_not_found():
  """token 錯誤與案件不存在回同一個訊息，不透露案件是否存在"""
  return jsonify({"error": "找不到這個案件，請確認連結是否完整"}), 404


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


# 公開給前端的欄位清單：刻意不含 email、精確座標、追蹤碼、LINE 帳號，
# 避免任何人（包含飼主與協尋者彼此）從 API 拿到對方的聯絡方式或精確位置
# 注意：所有查詢都要明確列出欄位，不要用 SELECT *，新增的 owner_token / line_user_id 才不會外洩
LOST_PUBLIC_FIELDS = """
    id, case_code, pet_name, species, size, fur_length, accessory, accessory_color,
    fur_color, special_mark, special_mark_note, notes, photo_1, photo_2, photo_3,
    photo_4, photo_5, lat_public, lng_public, lost_time, status
"""
SIGHTING_PUBLIC_FIELDS = """
    id, species, size, fur_length, accessory, accessory_color, fur_color,
    special_mark, special_mark_note, photo_1, photo_2, photo_3, photo_4, photo_5, description,
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
  """建立 Postgres 連線；row_factory=dict_row 讓查詢結果能像字典一樣用欄位名稱存取。

  注意：Postgres 的參數佔位符是 %s（SQLite 是 ?）。
  prepare_threshold=None：關掉 psycopg 的自動 prepared statement。
  Neon 的 pooled 連線（主機名稱含 -pooler）經過 PgBouncer，
  prepared statement 在這種連線上可能出現「prepared statement does not exist」錯誤。

  可以用 with get_db_connection() as conn: 的寫法：區塊正常結束會自動 commit，
  發生例外會 rollback，不論如何離開區塊都會關閉連線。
  """
  return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)


def init_db():
  """建立資料表（已存在就跳過）。

  與 SQLite 版的差異：
  - id 用 SERIAL（Postgres 的自動編號），取代 INTEGER PRIMARY KEY AUTOINCREMENT
  - 座標一律用 DOUBLE PRECISION：Postgres 的 REAL 只有 6～7 位有效數字，
    121.543012 這種座標會被截掉尾數，距離與隱私模糊都會失準
  - 舊版的 ensure_columns（PRAGMA table_info）用不到了：所有欄位直接寫進 CREATE TABLE；
    之後要加欄位，在下面的 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 補一行即可
  - 不再自動放種子資料：既有的展示資料由 migrate_to_neon.py 從 database.db 搬過來
  """
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 走失公告表
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS lost_reports (
            id SERIAL PRIMARY KEY,
            case_code TEXT NOT NULL,
            pet_name TEXT,
            species TEXT NOT NULL,
            size TEXT NOT NULL,
            fur_length TEXT NOT NULL,
            accessory TEXT NOT NULL,
            accessory_color TEXT,
            fur_color TEXT NOT NULL,
            special_mark TEXT NOT NULL,
            special_mark_note TEXT,
            notes TEXT,
            photo_1 TEXT,
            photo_2 TEXT,
            photo_3 TEXT,
            photo_4 TEXT,
            photo_5 TEXT,
            lat_precise DOUBLE PRECISION NOT NULL,
            lng_precise DOUBLE PRECISION NOT NULL,
            lat_public DOUBLE PRECISION NOT NULL,
            lng_public DOUBLE PRECISION NOT NULL,
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
            id SERIAL PRIMARY KEY,
            species TEXT NOT NULL,
            size TEXT NOT NULL,
            fur_length TEXT NOT NULL,
            accessory TEXT NOT NULL,
            accessory_color TEXT,
            fur_color TEXT NOT NULL,
            special_mark TEXT NOT NULL,
            special_mark_note TEXT,
            photo_1 TEXT,
            photo_2 TEXT,
            photo_3 TEXT,
            photo_4 TEXT,
            photo_5 TEXT,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            description TEXT,
            sighting_time TEXT NOT NULL,
            matched_lost_id INTEGER,
            status TEXT DEFAULT '待確認',
            score INTEGER DEFAULT 0,
            tracking_token TEXT,
            contact_email TEXT,
            ai_prefill TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

  # 3. 飼主排除紀錄：記下「哪一則公告的飼主」排除了「哪一筆通報」
  #    不能只改 sightings.status，因為一筆通報可能同時符合多則公告：
  #    飼主 A 說「不是我的」，不代表也不是飼主 B 的，B 仍然要看得到。
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS sighting_rejections (
            id SERIAL PRIMARY KEY,
            lost_id INTEGER NOT NULL,
            sighting_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (lost_id, sighting_id)
        )
    """)

  # 3-0. 協尋者指認紀錄：一筆通報可以同時指認多則公告（matched_lost_id 只剩「最近一次指認」）
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS sighting_claims (
            id SERIAL PRIMARY KEY,
            sighting_id INTEGER NOT NULL,
            lost_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (sighting_id, lost_id)
        )
    """)
  # 舊資料：把原本單一的 matched_lost_id 搬進指認紀錄（已存在就跳過）
  cursor.execute("""
        INSERT INTO sighting_claims (sighting_id, lost_id, score)
        SELECT id, matched_lost_id, COALESCE(score, 0) FROM sightings
        WHERE matched_lost_id IS NOT NULL
        ON CONFLICT (sighting_id, lost_id) DO NOTHING
    """)

  # 3-1. 已處理過的 LINE 事件（用 webhookEventId 去重）：
  #      LINE 沒收到 200 會重送同一個事件，記下來就不會重複綁定、重複回覆
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS line_webhook_events (
            webhook_event_id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  # LINE 只會在短時間內重送，7 天前的紀錄用不到了，每次啟動順手清掉
  cursor.execute(
      "DELETE FROM line_webhook_events WHERE created_at < NOW() - INTERVAL '7 days'"
  )

  # 4. 之後新增欄位寫在這裡（Postgres 支援 IF NOT EXISTS，重複執行不會出錯）
  #    owner_token：飼主憑證；line_user_id：綁定的 LINE 帳號（之後推播用，絕不出現在任何公開 API）
  #    client_submit_id：飼主表單的送出編號（階段 5：擋重複發布）
  cursor.execute("ALTER TABLE lost_reports ADD COLUMN IF NOT EXISTS owner_token TEXT")
  cursor.execute("ALTER TABLE lost_reports ADD COLUMN IF NOT EXISTS line_user_id TEXT")
  cursor.execute("ALTER TABLE lost_reports ADD COLUMN IF NOT EXISTS client_submit_id TEXT")
  cursor.execute("ALTER TABLE sightings ADD COLUMN IF NOT EXISTS line_user_id TEXT")

  # 5. 補發 token：舊資料（沒有 owner_token 的公告、沒有追蹤碼的通報）也要有，
  #    確保「每一筆資料都有 token」。補發的 token 只存在資料庫，沒有人知道，
  #    舊公告的飼主頁因此無法再從網址打開（這正是預期的結果）。
  cursor.execute("SELECT id FROM lost_reports WHERE owner_token IS NULL OR owner_token = ''")
  for row in cursor.fetchall():
    cursor.execute(
        "UPDATE lost_reports SET owner_token = %s WHERE id = %s",
        (new_token(OWNER_TOKEN_PREFIX), row["id"]),
    )
  cursor.execute("SELECT id FROM sightings WHERE tracking_token IS NULL OR tracking_token = ''")
  for row in cursor.fetchall():
    cursor.execute(
        "UPDATE sightings SET tracking_token = %s WHERE id = %s",
        (new_token(TRACK_TOKEN_PREFIX), row["id"]),
    )
  conn.commit()

  # 6. 唯一索引：token 不可重複；client_submit_id 不可重複（NULL 可以有很多筆，舊資料不受影響）
  #    萬一舊資料有重複值導致建立失敗，只印警告、不讓網站起不來
  for index_name, table, column in (
      ("idx_lost_reports_owner_token", "lost_reports", "owner_token"),
      ("idx_sightings_tracking_token", "sightings", "tracking_token"),
      ("idx_lost_reports_client_submit_id", "lost_reports", "client_submit_id"),
  ):
    try:
      cursor.execute(
          f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} ({column})"
      )
      conn.commit()
    except psycopg.Error as e:
      conn.rollback()
      print(f"[init_db] 建立唯一索引 {index_name} 失敗：{e}")

  conn.close()


# 全域執行初始化建表，符合 Gunicorn 與本地開發標準
init_db()


# ==========================================
# 錯誤處理與回應標頭
# ==========================================
NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>找不到頁面 - TakeMeHome</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
max-width:500px;margin:0 auto;padding:60px 16px;text-align:center;color:#333;line-height:1.6}
a{display:inline-block;margin-top:20px;padding:11px 20px;border-radius:6px;background:#2563eb;
color:#fff;text-decoration:none;font-weight:bold}</style></head>
<body><div style="font-size:44px">🤔</div><h2>找不到這個頁面</h2>
<p>連結可能不完整，或這個案件不存在。<br>請確認是否完整複製了連結。</p>
<a href="/">🗺️ 回到地圖</a></body></html>"""


@app.errorhandler(404)
def not_found(_err):
  """API 回 JSON；頁面回友善的中文頁面（不透露是「沒有這個案件」還是「token 不對」）"""
  if request.path.startswith("/api/"):
    return jsonify({"error": "找不到資料"}), 404
  return render_template_string(NOT_FOUND_HTML), 404


@app.errorhandler(413)
def file_too_large(_err):
  """上傳超過 MAX_CONTENT_LENGTH 時回傳前端看得懂的 JSON，而不是 HTML 錯誤頁"""
  return jsonify({"error": "照片太大，請改選較小的照片"}), 413


# 網址裡帶有 token 的頁面與 API
PRIVATE_PATH_PREFIXES = ("/case/", "/api/owner/", "/track/", "/api/bind-status/", "/api/track/")


@app.after_request
def add_private_headers(resp):
  """帶有 token 的頁面與 API：不快取、不被搜尋引擎收錄、不把完整網址（含 token）送給第三方。

  Referrer-Policy 用 strict-origin（只送網站網域，不送路徑），不要用 no-referrer：
  OpenStreetMap 的圖磚伺服器要求請求必須帶 Referer，完全拿掉會讓地圖載不出來。
  """
  if request.path.startswith(PRIVATE_PATH_PREFIXES):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "strict-origin"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
  return resp


# ==========================================
# 頁面路由
# ==========================================
@app.route("/")
def index():
  """首頁路由：回傳地圖頁面"""
  return render_template("index.html")


@app.route("/healthz")
def healthz():
  """給定時 ping 用的簡單路徑：不查資料庫，只確認 Render 服務醒著"""
  return "ok", 200


@app.route("/lost-form")
def lost_form():
  """飼主走失公告表單頁面"""
  return render_template("lost_form.html")


@app.route("/sighting-form")
def sighting_form():
  """協尋者目擊通報表單頁面（手機優先）"""
  return render_template("sighting_form.html")


@app.route("/case/<token>")
def case_page(token):
  """飼主的案件頁（通知欄）：網址裡的 token 就是憑證，數字 id 的舊網址一律 404"""
  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
  if lost_id is None:
    abort(404)
  return render_template("case.html", owner_token=token)


@app.route("/case/<token>/sighting/<int:sighting_id>")
def sighting_detail_page(token, sighting_id):
  """飼主看單筆通報的詳情頁：照片與特徵並排，按下確認或排除"""
  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
  if lost_id is None:
    abort(404)
  return render_template(
      "sighting_detail.html", owner_token=token, sighting_id=sighting_id
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
        WHERE s.tracking_token = %s
    """,
      (token,),
  )
  row = cursor.fetchone()
  conn.close()

  if not row:
    return render_template("track.html", sighting=None, tracking_token=None), 404
  return render_template("track.html", sighting=dict(row), tracking_token=token)


# ==========================================
# LINE 綁定（階段 4）
# ==========================================
def verify_line_signature(body, signature):
  """驗證 X-Line-Signature：用 Channel secret 對「原始請求內容」做 HMAC-SHA256，再 Base64。

  沒設定 LINE_CHANNEL_SECRET 時一律視為驗證失敗，避免有人偽造事件。
  """
  if not LINE_CHANNEL_SECRET or not signature:
    return False
  digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
  expected = base64.b64encode(digest).decode("utf-8")
  return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


# 從使用者訊息裡找 token 的寬鬆規則：
# 先用 NFKC 把全形字轉半形（ｏｗｎ＿ → own_）、轉小寫、拿掉所有空白與零寬字元，
# 再找「own 或 trk」+（底線可有可無，也接受 -）+ 32 個十六進位字元。
# 這樣「綁定 OWN_ABC…」「綁定\nown_ abc…」「綁定 ｏｗｎ＿ａｂｃ…」都抓得到。
TOKEN_IN_TEXT_RE = re.compile(r"(own|trk)[_-]?([0-9a-f]{32})")
INVISIBLE_RE = re.compile(r"[\s​-‍⁠﻿]+")


def extract_tokens(text):
  """從一段文字抓出所有 token（去重、保留順序、最多 5 組），回傳標準格式 own_xxx / trk_xxx"""
  if not isinstance(text, str):
    return []
  cleaned = INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", text)).lower()
  found = []
  for kind, hex32 in TOKEN_IN_TEXT_RE.findall(cleaned):
    token = f"{kind}_{hex32}"
    if token not in found:
      found.append(token)
  return found[:5]


def public_base_url():
  """LINE 回覆裡的網站網址：優先用 PUBLIC_BASE_URL，否則用這次請求的網址"""
  return PUBLIC_BASE_URL or request.url_root.rstrip("/")


def bind_token(conn, token, line_user_id, base):
  """把 LINE 帳號綁到 token 對應的公告或通報（後綁覆蓋前綁）；回傳要回覆的文字。

  base 是網站網址（給回覆裡的連結用）。在背景執行緒裡拿不到 request，所以由呼叫端先算好傳進來。
  """
  if token.startswith(OWNER_TOKEN_PREFIX):
    row = conn.execute(
        "UPDATE lost_reports SET line_user_id = %s WHERE owner_token = %s"
        " RETURNING pet_name, case_code",
        (line_user_id, token),
    ).fetchone()
    if not row:
      return "找不到這組追蹤碼，請確認完整複製"
    return (
        f"已綁定「{row['pet_name'] or '未命名'}」{row['case_code']} ✓\n"
        f"案件頁：{base}/case/{token}\n"
        "（這個連結就是你的鑰匙，請不要轉傳給別人）"
    )

  row = conn.execute(
      "UPDATE sightings SET line_user_id = %s WHERE tracking_token = %s RETURNING id",
      (line_user_id, token),
  ).fetchone()
  if not row:
    return "找不到這組追蹤碼，請確認完整複製"
  return f"已綁定你的目擊通報 ✓\n處理進度：{base}/track/{token}"


HELP_TEXT = (
    "你好，這裡是 TakeMeHome 走失寵物協尋 🐾\n"
    "要用 LINE 追蹤案件，請在網站送出成功的頁面按「用 LINE 追蹤」，"
    "或把「綁定 own_…」／「綁定 trk_…」整段文字貼到這裡。"
)
GROUP_TEXT = "為了保護你的案件，請在和官方帳號的一對一聊天室裡傳送綁定訊息，不要在群組裡貼。"


def line_api_post(path, payload):
  """呼叫 LINE Messaging API；成功回 True。日誌只記狀態碼，不記訊息內容（內容有 token）"""
  if not LINE_CHANNEL_ACCESS_TOKEN:
    print("[line] 沒有設定 LINE_CHANNEL_ACCESS_TOKEN，略過回覆")
    return False
  try:
    resp = requests.post(
        LINE_API_BASE + path,
        json=payload,
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=5,
    )
  except requests.RequestException as e:
    print(f"[line] {path} 連線失敗：{type(e).__name__}")
    return False
  if resp.status_code != 200:
    print(f"[line] {path} 回應 {resp.status_code}：{resp.text[:200]}")
    return False
  return True


def line_send(event, texts):
  """回覆使用者：先用 reply（免費）；reply token 失效時（服務剛醒、或 LINE 重送的事件）改用 push。"""
  messages = [{"type": "text", "text": t[:4900]} for t in texts][:5]
  if not messages:
    return
  redelivered = bool((event.get("deliveryContext") or {}).get("isRedelivery"))
  reply_token = event.get("replyToken")
  if reply_token and not redelivered:
    if line_api_post("/v2/bot/message/reply", {"replyToken": reply_token, "messages": messages}):
      return
  user_id = (event.get("source") or {}).get("userId")
  if user_id:
    line_api_post("/v2/bot/message/push", {"to": user_id, "messages": messages})


def line_push_async(user_id, texts):
  """背景推播給指定的 LINE 使用者（不拖慢網頁請求）；沒綁 LINE 的人直接略過"""
  if not user_id:
    return
  messages = [{"type": "text", "text": t[:4900]} for t in texts][:5]
  threading.Thread(
      target=line_api_post,
      args=("/v2/bot/message/push", {"to": user_id, "messages": messages}),
      daemon=True,
  ).start()


def claim_line_event(event):
  """把事件 ID 記進資料庫；第一次看到回 True，已經處理過（LINE 重送）回 False。

  用 INSERT ... ON CONFLICT DO NOTHING：就算原本那次還在處理、重送的同時抵達，
  也只有一個能搶到，不會兩邊都回覆。沒有 webhookEventId 的事件（理論上不會有）一律照常處理。
  """
  event_id = event.get("webhookEventId")
  if not event_id:
    return True
  with get_db_connection() as conn:
    row = conn.execute(
        "INSERT INTO line_webhook_events (webhook_event_id) VALUES (%s)"
        " ON CONFLICT (webhook_event_id) DO NOTHING RETURNING webhook_event_id",
        (str(event_id)[:100],),
    ).fetchone()
  return row is not None


def handle_line_event(event, base):
  """處理一個 webhook 事件：加好友 → 說明；文字訊息 → 找 token 綁定"""
  etype = event.get("type")
  source = event.get("source") or {}

  if not claim_line_event(event):
    print(f"[line] 略過已處理過的事件 type={etype}")
    return

  if etype == "follow":
    line_send(event, [HELP_TEXT])
    return

  message = event.get("message") or {}
  if etype != "message" or message.get("type") != "text":
    return

  tokens = extract_tokens(message.get("text", ""))
  if not tokens:
    line_send(event, [HELP_TEXT])
    return

  # 群組／多人聊天室裡不綁定：貼 token 等於把案件鑰匙給整個群組
  if source.get("type") != "user" or not source.get("userId"):
    line_send(event, [GROUP_TEXT])
    return

  with get_db_connection() as conn:
    replies = [bind_token(conn, t, source["userId"], base) for t in tokens]
  line_send(event, replies)


def process_line_events(events, base):
  """逐一處理事件；單一事件出錯只記日誌，不影響其他事件"""
  for event in events:
    if not isinstance(event, dict):
      continue
    source = event.get("source") or {}
    redelivery = (event.get("deliveryContext") or {}).get("isRedelivery")
    # 日誌只記事件類型，不記訊息內容（綁定訊息裡會有 token）
    print(f"[line] event type={event.get('type')} source={source.get('type')}"
          f" redelivery={bool(redelivery)}")
    try:
      handle_line_event(event, base)
    except Exception as e:
      print(f"[line] 處理事件失敗：{type(e).__name__}: {e}")


@app.route("/line/webhook", methods=["POST"])
def line_webhook():
  """LINE 官方帳號的 webhook。

  1. 用原始請求內容驗簽，不符直接回 400
  2. 簽章有效就「先回 200，再在背景處理事件」：
     連 Neon、寫資料庫、呼叫 LINE 回覆加起來可能要好幾秒，
     等處理完才回應的話，LINE 會判定逾時並重送同一個事件，使用者就會收到好幾則回覆。
     （LINE 官方也建議 webhook 先回應、再非同步處理）
  3. 每個事件用 webhookEventId 去重，就算 LINE 還是重送了，也只會處理一次
  LINE 後台按 Verify 時會送一個 events 為空的請求，這裡也回 200。
  """
  body = request.get_data()  # 一定要用原始內容驗簽，不能先 get_json 再重組
  if not verify_line_signature(body, request.headers.get("X-Line-Signature", "")):
    return jsonify({"error": "invalid signature"}), 400

  try:
    payload = json.loads(body or b"{}")
  except ValueError:
    payload = {}
  events = payload.get("events", []) if isinstance(payload, dict) else []
  if not isinstance(events, list) or not events:
    return "OK", 200

  base = public_base_url()  # 背景執行緒拿不到 request，先在這裡算好
  if LINE_WEBHOOK_SYNC:
    process_line_events(events, base)
  else:
    threading.Thread(target=process_line_events, args=(events, base), daemon=True).start()
  return "OK", 200


@app.route("/api/bind-status/<token>")
def bind_status(token):
  """成功頁輪詢用：只回答這個 token 有沒有綁定 LINE，不回傳任何其他資料。

  token 格式不對或查不到，都回 {bound: false}，不透露是哪一種。
  """
  if OWNER_TOKEN_RE.match(token):
    sql = "SELECT line_user_id FROM lost_reports WHERE owner_token = %s"
  elif TRACK_TOKEN_RE.match(token):
    sql = "SELECT line_user_id FROM sightings WHERE tracking_token = %s"
  else:
    return jsonify({"bound": False}), 200
  with get_db_connection() as conn:
    row = conn.execute(sql, (token,)).fetchone()
  return jsonify({"bound": bool(row and row["line_user_id"])}), 200


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
  """通用照片上傳 API：上傳到 Cloudinary 並回傳圖片網址。

  前端只會把回傳的 file_path 存進 photo_1～photo_5，再拿來當 <img src>，
  所以從本機路徑換成 Cloudinary 的完整網址，前端完全不用改。
  沒設定 CLOUDINARY_URL 時退回存在本機 static/uploads（只適合本機開發）。
  """
  if "photo" not in request.files:
    return jsonify({"error": "沒有上傳檔案"}), 400

  file = request.files["photo"]
  if file.filename == "":
    return jsonify({"error": "未選擇任何檔案"}), 400

  ext = os.path.splitext(file.filename)[1].lower()
  if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
    return jsonify({"error": "僅支援 jpg、png、webp 格式圖片"}), 400

  new_name = f"photo_{int(time.time() * 1000)}"

  if USE_CLOUDINARY:
    try:
      result = cloudinary.uploader.upload(
          file.stream,
          folder=CLOUDINARY_FOLDER,
          public_id=new_name,
          resource_type="image",
      )
    except Exception as e:  # 網路或額度問題：回傳前端看得懂的錯誤，讓使用者重試
      print(f"[upload] Cloudinary 上傳失敗：{type(e).__name__}: {e}")
      return jsonify({"error": "照片上傳失敗，請稍後再試"}), 502
    return jsonify({"file_path": result["secure_url"]})

  os.makedirs(UPLOAD_FOLDER, exist_ok=True)
  file.save(os.path.join(UPLOAD_FOLDER, new_name + ext))
  return jsonify({"file_path": f"/static/uploads/{new_name}{ext}"})


def calculate_public_coords(lat, lng, radius_meters):
  """隱私模糊演算法：依據選擇的半徑 (30m 或 50m) 進行隨機角度與距離的固定偏移"""
  angle = random.uniform(0, 2 * math.pi)
  distance = random.uniform(radius_meters * 0.7, radius_meters)
  delta_lat = (distance * math.cos(angle)) / 111000.0
  delta_lng = (distance * math.sin(angle)) / (
      111000.0 * math.cos(math.radians(lat))
  )
  return round(lat + delta_lat, 6), round(lng + delta_lng, 6)


def lost_created_response(row, status_code, duplicate=False):
  """發布成功的回應（第一次發布與重複送出回同樣的內容，前端不用分辨）"""
  return (
      jsonify({
          "message": "公告發布成功！",
          "id": row["id"],
          "case_code": row["case_code"],
          # 飼主憑證：只回給送出這張表單的人；之後沒有任何公開 API 會再回傳它
          "owner_token": row["owner_token"],
          "lat_public": row["lat_public"],
          "lng_public": row["lng_public"],
          "duplicate": duplicate,
      }),
      status_code,
  )


@app.route("/api/lost_reports", methods=["POST"])
def create_lost_report():
  """接收飼主發布走失公告，寫入資料庫，並由伺服器產生飼主憑證 owner_token。

  防重複送出（階段 5）：前端每次開表單產生一組 client_submit_id，重送時沿用同一組。
  資料庫對它有唯一索引，所以就算「第一次其實成功了、只是回應在路上斷掉」再按一次，
  也只會有一筆公告，並回傳同一個 owner_token。
  """
  data = request.get_json(silent=True)
  if not data:
    return jsonify({"error": "未收到任何資料"}), 400

  client_submit_id = data.get("client_submit_id")
  if not isinstance(client_submit_id, str) or not CLIENT_SUBMIT_ID_RE.match(client_submit_id):
    client_submit_id = None  # 舊版前端沒有送編號：照常發布，只是沒有重複保護

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
  except (TypeError, ValueError):
    return jsonify({"error": "座標或模糊半徑格式不正確"}), 400

  lat_public, lng_public = calculate_public_coords(
      lat_precise, lng_precise, privacy_radius
  )
  case_code = f"LOST-{random.randint(1000, 9999)}"
  owner_token = new_token(OWNER_TOKEN_PREFIX)

  # 欄位與值成對列出，再自動產生 %s 佔位符，避免新增欄位時數錯數量
  record = [
      ("case_code", case_code),
      ("pet_name", data.get("pet_name").strip()),
      ("species", data.get("species", "狗")),
      ("size", data.get("size", "中")),
      ("fur_length", data.get("fur_length", "短毛")),
      ("accessory", data.get("accessory", "沒有")),
      ("accessory_color", data.get("accessory_color", "無")),
      ("fur_color", data.get("fur_color", "黑/深棕")),
      ("special_mark", data.get("special_mark", "沒有")),
      ("special_mark_note", special_mark_note),
      ("notes", data.get("notes", "")),
      ("photo_1", data.get("photo_1")),
      ("photo_2", data.get("photo_2", "")),
      ("photo_3", data.get("photo_3", "")),
      ("photo_4", data.get("photo_4", "")),
      ("photo_5", data.get("photo_5", "")),
      ("lat_precise", lat_precise),
      ("lng_precise", lng_precise),
      ("lat_public", lat_public),
      ("lng_public", lng_public),
      ("privacy_radius", privacy_radius),
      ("lost_time", lost_time),
      ("email", email),
      ("status", "尋找中"),
      ("owner_token", owner_token),
      ("client_submit_id", client_submit_id),
  ]
  columns = ", ".join(name for name, _ in record)
  placeholders = ", ".join(["%s"] * len(record))
  returning = "RETURNING id, case_code, owner_token, lat_public, lng_public"

  with get_db_connection() as conn:
    # 同一個 client_submit_id 已經發布過：ON CONFLICT DO NOTHING 不會新增，RETURNING 也沒有資料
    row = conn.execute(
        f"INSERT INTO lost_reports ({columns}) VALUES ({placeholders})"
        f" ON CONFLICT (client_submit_id) DO NOTHING {returning}",
        [value for _, value in record],
    ).fetchone()
    if row:
      return lost_created_response(row, 201)

    existing = conn.execute(
        "SELECT id, case_code, owner_token, lat_public, lng_public"
        " FROM lost_reports WHERE client_submit_id = %s",
        (client_submit_id,),
    ).fetchone()
  if not existing:  # 理論上不會發生；保險起見回可重試的錯誤
    return jsonify({"error": "發布時發生問題，請再按一次送出"}), 500
  return lost_created_response(existing, 200, duplicate=True)


def build_candidates(lost_rows, current):
  """把尋找中的公告逐一算分，回傳通過門檻、排序好的候選清單（新通報與追蹤頁「再指認」共用）。

  lost_rows 需要的欄位見 create_sighting 的 SELECT；current 是 calculate_match 要的目擊字典。
  """
  candidates = []
  for row in lost_rows:
    lost_obj = lost_to_match_obj(row)  # 使用後端精確座標比對
    match_res = calculate_match(lost_obj, current)

    # 門檻過濾：排除者剔除，外觀分數 >= 40 就進入候選清單（寧可多推，不要漏掉）
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

  # 排序：先看外觀等級（60 分以上優先），同一級內再依時空分數由高到低
  candidates.sort(
      key=lambda x: rank_key(x["appearance_score"], x["spatiotemporal_score"]),
      reverse=True,
  )
  return candidates


@app.route("/api/sightings", methods=["POST"])
def create_sighting():
  """【檢查點二】協尋者目擊通報 API：

  1. 先存檔保底寫入 sightings
  2. 由伺服器產生專屬 tracking_token（trk_ 開頭、128 位元隨機）
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

  tracking_token = new_token(TRACK_TOKEN_PREFIX)

  # 2. 先存檔保底：寫入 sightings 資料表
  conn = get_db_connection()
  cursor = conn.cursor()

  cursor.execute(
      """
        INSERT INTO sightings (
            species, size, fur_length, accessory, accessory_color,
            fur_color, special_mark, photo_1, photo_2, photo_3, photo_4, photo_5,
            lat, lng, description, sighting_time,
            status, score, tracking_token, contact_email, ai_prefill, special_mark_note
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  '待確認', 0, %s, %s, %s, %s)
        RETURNING id
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
          data.get("photo_4", ""),
          data.get("photo_5", ""),
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

  new_sighting_id = cursor.fetchone()["id"]
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

  candidates = build_candidates(lost_candidates, current_sighting)

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
@app.route("/api/track/<token>/candidates", methods=["GET"])
def track_candidates(token):
  """追蹤頁「再指認其他公告」：依通報特徵重新算候選清單。

  排除：已經指認過的、這則公告的飼主已排除過這筆通報的、已結案的公告。
  通報已被飼主確認（寵物已找到）時回 closed，不再列出候選。
  """
  if not TRACK_TOKEN_RE.match(token):
    return jsonify({"error": "找不到這筆通報"}), 404

  with get_db_connection() as conn:
    sighting = conn.execute(
        """
        SELECT id, status, species, size, fur_length, accessory, accessory_color,
               fur_color, special_mark, special_mark_note, lat, lng, sighting_time
        FROM sightings WHERE tracking_token = %s
        """,
        (token,),
    ).fetchone()
    if not sighting:
      return jsonify({"error": "找不到這筆通報"}), 404

    claimed = conn.execute(
        """
        SELECT l.id AS lost_id, l.case_code, l.pet_name
        FROM sighting_claims c JOIN lost_reports l ON l.id = c.lost_id
        WHERE c.sighting_id = %s ORDER BY c.id
        """,
        (sighting["id"],),
    ).fetchall()

    if sighting["status"] == "飼主已確認":
      return jsonify({"sighting_id": sighting["id"], "closed": True,
                      "claimed": claimed, "candidates": []}), 200

    lost_rows = conn.execute(
        """
        SELECT id, case_code, pet_name, species, size, fur_length, accessory, accessory_color,
               fur_color, special_mark, special_mark_note, lat_precise, lng_precise,
               lost_time, photo_1
        FROM lost_reports
        WHERE status = '尋找中'
          AND id NOT IN (SELECT lost_id FROM sighting_claims WHERE sighting_id = %s)
          AND id NOT IN (SELECT lost_id FROM sighting_rejections WHERE sighting_id = %s)
        """,
        (sighting["id"], sighting["id"]),
    ).fetchall()

  candidates = build_candidates(lost_rows, sighting_to_match_obj(sighting))
  return jsonify({"sighting_id": sighting["id"], "closed": False,
                  "claimed": claimed, "candidates": candidates}), 200


@app.route("/api/sightings/<int:sighting_id>/match", methods=["POST"])
def match_sighting(sighting_id):
  """【檢查點三】協尋者反向指認 API（「就是這隻！」）：

  1. 必須帶這筆通報自己的 tracking_token，後端比對相符才處理
     （通報不存在與追蹤碼不符回同一個錯誤，不透露通報是否存在）
  2. 外觀分數由伺服器用 calculate_match 重新計算，不採用前端傳來的任何分數
  3. 在 sighting_claims 新增一筆指認紀錄（同一筆通報可指認多則公告，重複指認同一則不會重複新增）；
     sightings.matched_lost_id 記最近一次指認，status 更新為 '已通知飼主'
  """
  data = request.get_json(silent=True) or {}
  token = data.get("tracking_token")
  try:
    lost_id = int(data.get("lost_id"))
  except (TypeError, ValueError):
    return jsonify({"error": "缺少要關聯的走失公告 ID (lost_id)"}), 400

  with get_db_connection() as conn:
    sighting = conn.execute(
        """
        SELECT id, status, tracking_token, species, size, fur_length, accessory,
               accessory_color, fur_color, special_mark, special_mark_note,
               lat, lng, sighting_time
        FROM sightings WHERE id = %s
        """,
        (sighting_id,),
    ).fetchone()

    if not sighting or not token_matches(sighting["tracking_token"], token):
      return jsonify({"error": "找不到這筆通報，或追蹤碼不正確"}), 404

    # 已被某位飼主確認（寵物已找到）的通報不能再指認
    if sighting["status"] == "飼主已確認":
      return jsonify({"error": "這筆通報飼主已確認過，無法再指認"}), 409
    # 這則公告的飼主已經排除過這筆通報：不能再指認同一則（其他公告不受影響）
    if conn.execute(
        "SELECT 1 FROM sighting_rejections WHERE lost_id = %s AND sighting_id = %s",
        (lost_id, sighting_id),
    ).fetchone():
      return jsonify({"error": "這則公告的飼主已排除這筆通報，無法再指認"}), 409

    lost = conn.execute(
        """
        SELECT id, case_code, pet_name, status, species, size, fur_length, accessory,
               accessory_color, fur_color, special_mark, special_mark_note,
               lat_precise, lng_precise, lost_time
        FROM lost_reports WHERE id = %s
        """,
        (lost_id,),
    ).fetchone()
    if not lost:
      return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
    if lost["status"] != "尋找中":
      return jsonify({"error": "這則公告已經結案，無法再指認"}), 409

    match_res = calculate_match(lost_to_match_obj(lost), sighting_to_match_obj(sighting))
    if match_res["is_excluded"]:
      return jsonify({"error": "物種不同，無法指認這則公告"}), 400
    score = int(match_res["appearance_score"])

    already_claimed = conn.execute(
        "SELECT 1 FROM sighting_claims WHERE sighting_id = %s AND lost_id = %s",
        (sighting_id, lost_id),
    ).fetchone()

    # 新增指認紀錄（同一則公告重複指認只更新分數）；通報可同時指認多則公告
    conn.execute(
        """
        INSERT INTO sighting_claims (sighting_id, lost_id, score) VALUES (%s, %s, %s)
        ON CONFLICT (sighting_id, lost_id) DO UPDATE SET score = EXCLUDED.score
        """,
        (sighting_id, lost_id, score),
    )
    # 第一次指認這則公告：推 LINE 給飼主（飼主有綁定才推）
    if not already_claimed:
      owner = conn.execute(
          "SELECT owner_token, line_user_id FROM lost_reports WHERE id = %s", (lost_id,)
      ).fetchone()
      line_push_async(owner["line_user_id"], [
          f"🔔 有協尋者指認了你的寶貝！\n「{lost['pet_name'] or '未命名'}」{lost['case_code']}\n"
          f"外觀吻合 {score} 分\n"
          f"查看通報：{public_base_url()}/case/{owner['owner_token']}\n"
          "（這個連結就是你的鑰匙，請不要轉傳給別人）"
      ])

    # 更新目擊通報狀態；matched_lost_id 記最近一次指認（追蹤頁顯示用）
    conn.execute(
        """
        UPDATE sightings
        SET matched_lost_id = %s,
            status = '已通知飼主',
            score = %s
        WHERE id = %s
        """,
        (lost_id, score, sighting_id),
    )

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
# 【檢查點四】飼主端 API：全部改用飼主 token（/api/owner/<token>/...）
# 伺服器用 token 反查是哪一則公告，前端不再傳 lost_id，所以不可能操作別人的案件。
# 公開的 GET /api/lost_reports/<id> 保留給協尋者「我看過這隻」顯示參考公告用。
# ==========================================
@app.route("/api/lost_reports/<int:lost_id>", methods=["GET"])
def get_lost_report(lost_id):
  """取單則走失公告的公開資訊（不含 email、精確座標、token）。

  協尋者從「我看過這隻」進入通報時會用到。
  """
  conn = get_db_connection()
  row = conn.execute(
      f"SELECT {LOST_PUBLIC_FIELDS} FROM lost_reports WHERE id = %s", (lost_id,)
  ).fetchone()
  conn.close()
  if not row:
    return jsonify({"error": f"找不到走失公告 ID {lost_id}"}), 404
  return jsonify(dict(row)), 200


@app.route("/api/owner/<token>/case", methods=["GET"])
def owner_get_case(token):
  """飼主案件頁用：取自己這則公告的公開資訊（不含 email 與精確座標）"""
  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
    if lost_id is None:
      return owner_not_found()
    row = conn.execute(
        f"SELECT {LOST_PUBLIC_FIELDS} FROM lost_reports WHERE id = %s", (lost_id,)
    ).fetchone()
  return jsonify(dict(row)), 200


def compute_notifications(conn, lost_id):
  """算出一則公告的通知清單（已排序）。

  1. 撈出這筆走失公告本身的特徵
  2. 撈出還沒被確認的目擊通報，並排除「這則公告的飼主」已經排除過的
  3. 逐筆套用 calculate_match，外觀分數 >= NOTIFY_THRESHOLD（40）就算有效通知
  4. 已被協尋者手動指認（sighting_claims 有本案）的通報，無論分數都強制納入
  5. 指認本案的置頂，其餘依外觀等級與時空分數由高到低排序

  隱私：回傳內容不含協尋者的 email 與追蹤碼。
  """
  lost_row = conn.execute(
      """
      SELECT species, size, fur_length, accessory, accessory_color,
             fur_color, special_mark, special_mark_note, lat_precise, lng_precise, lost_time
      FROM lost_reports WHERE id = %s
      """,
      (lost_id,),
  ).fetchone()
  lost_obj = lost_to_match_obj(lost_row)

  # 已被飼主確認（寵物已找到）的通報不再推給任何人；本案飼主按過「不是我的寶貝」的也不再出現
  sighting_rows = conn.execute(
      """
      SELECT id, species, size, fur_length, accessory, accessory_color,
             fur_color, special_mark, special_mark_note, photo_1, lat, lng, description,
             sighting_time, status, matched_lost_id,
             EXISTS (
                 SELECT 1 FROM sighting_claims c
                 WHERE c.sighting_id = sightings.id AND c.lost_id = %s
             ) AS claimed_by_this
      FROM sightings
      WHERE status != '飼主已確認'
        AND id NOT IN (
            SELECT sighting_id FROM sighting_rejections WHERE lost_id = %s
        )
      """,
      (lost_id, lost_id),
  ).fetchall()

  notifications = []
  for row in sighting_rows:
    match_res = calculate_match(lost_obj, sighting_to_match_obj(row))

    # 門檻判斷：外觀分數 >= 40 就通知；但已被協尋者手動指認本案的，強制納入
    already_picked = row["claimed_by_this"]
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

  notifications.sort(
      key=lambda x: (
          x["manually_confirmed"],
          rank_key(x["appearance_score"], x["spatiotemporal_score"]),
      ),
      reverse=True,
  )
  return notifications


@app.route("/api/owner/<token>/notifications", methods=["GET"])
def owner_get_notifications(token):
  """查詢這則公告的通知清單（即時計算版），邏輯見 compute_notifications"""
  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
    if lost_id is None:
      return owner_not_found()
    notifications = compute_notifications(conn, lost_id)

  return (
      jsonify({
          "notification_count": len(notifications),
          "notifications": notifications,
      }),
      200,
  )


MY_BADGES_LIMIT = 20  # 一次最多查幾組 token，避免被拿來大量掃描


@app.route("/api/my-badges", methods=["POST"])
def my_badges():
  """首頁「我的案件／我的通報」的通知數字用。

  前端把這台裝置存的 token 用 POST 送來（不放在網址，避免出現在日誌裡），回傳：
    cases:     {owner_token: [目前通知清單裡的通報 id]}，前端跟「看過的 id」相減就是新通知數
    sightings: {tracking_token: 通報目前的狀態}，前端比對有沒有新的處理結果
  格式不對或查不到的 token 直接略過，不透露原因。
  """
  data = request.get_json(silent=True) or {}
  owner_tokens = [t for t in (data.get("owner_tokens") or [])
                  if isinstance(t, str) and OWNER_TOKEN_RE.match(t)][:MY_BADGES_LIMIT]
  track_tokens = [t for t in (data.get("tracking_tokens") or [])
                  if isinstance(t, str) and TRACK_TOKEN_RE.match(t)][:MY_BADGES_LIMIT]

  cases, sightings = {}, {}
  with get_db_connection() as conn:
    for token in owner_tokens:
      lost_id = find_lost_id_by_owner_token(conn, token)
      if lost_id is None:
        continue
      status = conn.execute(
          "SELECT status FROM lost_reports WHERE id = %s", (lost_id,)
      ).fetchone()["status"]
      # 已結案的公告不再有新通知
      cases[token] = (
          [n["sighting_id"] for n in compute_notifications(conn, lost_id)]
          if status == "尋找中" else []
      )
    for token in track_tokens:
      row = conn.execute(
          "SELECT status FROM sightings WHERE tracking_token = %s", (token,)
      ).fetchone()
      if row:
        sightings[token] = row["status"]
  return jsonify({"cases": cases, "sightings": sightings}), 200


@app.route("/api/owner/<token>/sightings/<int:sighting_id>", methods=["GET"])
def owner_get_sighting(token, sighting_id):
  """詳情頁資料：本案公告 + 一筆通報 + 兩者的比對結果與特徵並排。

  距離用後端精確座標計算，前端只拿到算好的公里數，拿不到精確座標。
  """
  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
    if lost_id is None:
      return owner_not_found()
    lost = conn.execute(
        f"SELECT {LOST_PUBLIC_FIELDS}, lat_precise, lng_precise FROM lost_reports WHERE id = %s",
        (lost_id,),
    ).fetchone()
    sighting = conn.execute(
        f"SELECT {SIGHTING_PUBLIC_FIELDS}, lat, lng FROM sightings WHERE id = %s",
        (sighting_id,),
    ).fetchone()
    rejected = conn.execute(
        "SELECT 1 FROM sighting_rejections WHERE lost_id = %s AND sighting_id = %s",
        (lost_id, sighting_id),
    ).fetchone()
    claimed = conn.execute(
        "SELECT 1 FROM sighting_claims WHERE lost_id = %s AND sighting_id = %s",
        (lost_id, sighting_id),
    ).fetchone()

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
      "manually_confirmed": bool(claimed),
      "rejected_by_owner": bool(rejected),
  }), 200


@app.route("/api/owner/<token>/confirm_found", methods=["POST"])
def owner_confirm_found(token):
  """飼主點擊「這是我的寶貝！」完成確認閉環：

  1. lost_reports.status -> '已尋回'（系統停止參與後續比對，地圖紅釘消失）
  2. 該公告對應的通報 sightings.status -> '飼主已確認'
  3. 回傳通知協尋者的確認訊息
  """
  data = request.get_json(silent=True) or {}
  sighting_id = data.get("sighting_id")

  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
    if lost_id is None:
      return owner_not_found()

    lost = conn.execute(
        "SELECT status FROM lost_reports WHERE id = %s", (lost_id,)
    ).fetchone()
    if lost["status"] != "尋找中":
      return jsonify({"error": f"這則公告已經是「{lost['status']}」，不能重複確認"}), 409
    if sighting_id and not conn.execute(
        "SELECT 1 FROM sightings WHERE id = %s", (sighting_id,)
    ).fetchone():
      return jsonify({"error": f"找不到通報 ID {sighting_id}"}), 404

    # 先記下要通知的目擊者（更新狀態前查，之後才能推播）
    if sighting_id:
      targets = conn.execute(
          "SELECT id, line_user_id, tracking_token FROM sightings WHERE id = %s", (sighting_id,)
      ).fetchall()
    else:
      targets = conn.execute(
          "SELECT id, line_user_id, tracking_token FROM sightings WHERE id IN"
          " (SELECT sighting_id FROM sighting_claims WHERE lost_id = %s)",
          (lost_id,),
      ).fetchall()
    lost_info = conn.execute(
        "SELECT pet_name, case_code FROM lost_reports WHERE id = %s", (lost_id,)
    ).fetchone()
    base = public_base_url()

    # 1. 將走失公告改為「已尋回」
    conn.execute("UPDATE lost_reports SET status = '已尋回' WHERE id = %s", (lost_id,))

    # 2. 若有指定被確認的通報 ID，將其改為「飼主已確認」
    if sighting_id:
      conn.execute(
          "UPDATE sightings SET status = '飼主已確認', matched_lost_id = %s WHERE id = %s",
          (lost_id, sighting_id),
      )
    else:
      # 若未指定特定通報，將所有關聯該公告的通報皆更新
      conn.execute(
          "UPDATE sightings SET status = '飼主已確認' WHERE id IN"
          " (SELECT sighting_id FROM sighting_claims WHERE lost_id = %s)",
          (lost_id,),
      )

  for t in targets:
    line_push_async(t["line_user_id"], [
        f"🎉 你通報的寵物找到主人了！\n「{lost_info['pet_name'] or '未命名'}」{lost_info['case_code']}"
        " 已由飼主確認尋回，感謝你的協助 🐾\n"
        f"查看處理進度：{base}/track/{t['tracking_token']}"
    ])

  return (
      jsonify({
          "message": "恭喜！已確認尋回寶貝，案件正式結案！",
          "lost_status": "已尋回",
          "sighting_status": "飼主已確認",
      }),
      200,
  )


@app.route("/api/owner/<token>/reject_sighting", methods=["POST"])
def owner_reject_sighting(token):
  """飼主點擊「不是我的寶貝」：

  1. 在 sighting_rejections 記下「這則公告排除了這筆通報」
     → 這筆通報不再出現在本案的通知欄，但其他飼主仍看得到
  2. sightings.status -> '飼主已排除'（地圖上變成灰色圖釘）
     已被別的飼主確認的通報不改狀態，避免蓋掉「飼主已確認」
  """
  data = request.get_json(silent=True) or {}
  sighting_id = data.get("sighting_id")
  if not sighting_id:
    return jsonify({"error": "缺少通報 ID (sighting_id)"}), 400

  with get_db_connection() as conn:
    lost_id = find_lost_id_by_owner_token(conn, token)
    if lost_id is None:
      return owner_not_found()
    if not conn.execute("SELECT 1 FROM sightings WHERE id = %s", (sighting_id,)).fetchone():
      return jsonify({"error": f"找不到通報 ID {sighting_id}"}), 404

    # ON CONFLICT DO NOTHING（取代 SQLite 的 INSERT OR IGNORE）：重複按也只會有一筆紀錄
    conn.execute(
        """
        INSERT INTO sighting_rejections (lost_id, sighting_id) VALUES (%s, %s)
        ON CONFLICT (lost_id, sighting_id) DO NOTHING
        """,
        (lost_id, sighting_id),
    )
    conn.execute(
        "UPDATE sightings SET status = '飼主已排除' WHERE id = %s AND status != '飼主已確認'",
        (sighting_id,),
    )

  return jsonify({
      "message": "已記錄，這筆通報不會再出現在你的通知欄",
      "sighting_id": sighting_id,
      "sighting_status": "飼主已排除",
  }), 200

#=========================================

if __name__ == "__main__":
  app.run(debug=True, port=5001)
