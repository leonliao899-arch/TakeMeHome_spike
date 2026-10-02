import math
import os
import random
import sqlite3
import time
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_NAME = "database.db"

# 設定圖片上傳儲存的目錄 (專案根目錄/static/uploads)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


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


# 關鍵架構：在全域直接執行 init_db()，確保無論本地開發還是雲端 Gunicorn 載入時皆完成建表
init_db()


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


# ==========================================
# API 路由
# ==========================================
@app.route("/api/map-data")
def get_map_data():
    """整合兩張表的公開地圖資料回傳 JSON"""
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
    """
    隱私模糊演算法：依據選擇的半徑 (30m 或 50m) 進行隨機角度與距離的固定偏移
    地球緯度 1 度約 111,000 公尺；經度 1 度約 111,000 * cos(lat) 公尺
    """
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

    # 1. 必填防呆驗證
    if not data.get("photo_1"):
        return jsonify({"error": "第 1 張全身照為必填項目"}), 400
    if not data.get("lat") or not data.get("lng"):
        return jsonify({"error": "請務必點擊按鈕帶入走失地點座標"}), 400
    if not data.get("email"):
        return jsonify({"error": "請填寫飼主聯絡 Email"}), 400

    try:
        lat_precise = float(data.get("lat"))
        lng_precise = float(data.get("lng"))
        privacy_radius = int(data.get("privacy_radius", 50))
    except ValueError:
        return jsonify({"error": "座標或模糊半徑格式不正確"}), 400

    # 2. 計算模糊公開座標
    lat_public, lng_public = calculate_public_coords(
        lat_precise, lng_precise, privacy_radius
    )

    # 3. 自動產生案件編號 (如 LOST-5821)
    case_code = f"LOST-{random.randint(1000, 9999)}"

    # 4. 寫入 SQLite database.db
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO lost_reports (
            case_code, species, size, fur_length,
            accessory, accessory_color, fur_color, special_mark, notes,
            photo_1, photo_2, photo_3,
            lat_precise, lng_precise, lat_public, lng_public, privacy_radius,
            lost_time, email, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '尋找中')
    """,
        (
            case_code,
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
            data.get("lost_time", ""),
            data.get("email"),
        ),
    )

    new_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return (
        jsonify(
            {
                "message": "公告發布成功！",
                "id": new_id,
                "case_code": case_code,
                "lat_public": lat_public,
                "lng_public": lng_public,
            }
        ),
        201,
    )


if __name__ == "__main__":
    # 本機直接執行 python3 app.py 啟動
    app.run(debug=True, port=5000)