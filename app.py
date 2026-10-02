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
    """建立 lost_reports 與 sightings 兩張核心資料表"""
    conn = get_db_connection()
    cursor = conn.cursor()

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

    conn.commit()
    conn.close()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/map-data")
def get_map_data():
    """檢查點三：整合兩張表的公開地圖資料回傳 JSON"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. 撈取走失公告（只撈取尋找中案件，且使用公開模糊座標 lat_public / lng_public，保護隱私）
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

    # 3. 整合成單一 JSON 物件回傳
    return jsonify({
        "lost_reports": lost_reports,
        "sightings": sightings
    })

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)