import sqlite3
from flask import Flask, jsonify, render_template

app = Flask(__name__)
DB_NAME = "database.db"


def get_db_connection():
  """建立資料庫連線，設定 row_factory 讓查詢結果能像字典一樣用欄位名稱存取"""
  conn = sqlite3.connect(DB_NAME)
  conn.row_factory = sqlite3.Row
  return conn


def init_db():
  """檢查點一與檢查點二：建立資料表，並在首次啟動時自動載入測試資料"""
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 建立走失公告表 (lost_reports)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS lost_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_code TEXT NOT NULL,
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

  # 2. 建立目擊通報表 (sightings)
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

  # 3. 自動預載測試資料（若無資料則寫入）
  cursor.execute("SELECT COUNT(*) FROM lost_reports")
  if cursor.fetchone()[0] == 0:
    cursor.execute("""
            INSERT INTO lost_reports (
                case_code, species, size, fur_length, accessory, accessory_color,
                fur_color, special_mark, notes, photo_1,
                lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
                lost_time, email, status
            ) VALUES (
                'LOST-0423', '狗', '中', '短毛', '項圈', '紅色',
                '黑/深棕', '白襪子', '親人、貪吃', '/static/uploads/demo_dog.jpg',
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
                '黑/深棕', '看不清楚', '/static/uploads/sighting_dog.jpg',
                25.0295, 121.5360, '躲在紅色車輛車底', '2026-10-01 23:20',
                '待確認', 0, 'token_xyz123', 'finder@example.com'
            )
        """)

  conn.commit()
  conn.close()


# 關鍵修復：在全域直接呼叫 init_db()，確保 Render 的 Gunicorn 載入時一定會執行建表與預塞資料
init_db()


@app.route("/")
def index():
  """首頁路由：回傳地圖頁面"""
  return render_template("index.html")


@app.route("/api/map-data")
def get_map_data():
  """檢查點三：整合兩張表的公開地圖資料回傳 JSON"""
  conn = get_db_connection()
  cursor = conn.cursor()

  # 1. 撈取走失公告（隱私保護：使用公開模糊座標 lat_public / lng_public，且不外洩 Email）
  cursor.execute("""
        SELECT id, case_code, species, size, fur_length,
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

  # 3. 整合成單一 JSON 回傳
  return jsonify({"lost_reports": lost_reports, "sightings": sightings})


if __name__ == "__main__":
  # 本機直接 python3 app.py 啟動時使用
  app.run(debug=True, port=5000)