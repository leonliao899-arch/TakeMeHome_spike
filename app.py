import sqlite3
from flask import Flask, jsonify, render_template

app = Flask(__name__)
DB_NAME = "database.db"


def init_db():
    """初始化資料庫：確保資料表與大安區測試座標存在"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    cursor.execute("SELECT COUNT(*) FROM reports")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO reports (lat, lng) VALUES (?, ?)", (25.026, 121.543)
        )
        conn.commit()
    conn.close()


@app.route("/")
def index():
    """首頁路由：回傳渲染好的 index.html 網頁"""
    return render_template("index.html")


@app.route("/api/reports")
def get_reports():
    """API 路由：從 SQLite 撈出所有資料，以 JSON 格式回傳給前端 Leaflet 使用"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, lat, lng, created_at FROM reports")
    rows = cursor.fetchall()
    conn.close()

    # 將查詢結果整理成 List of Dict 結構
    reports = []
    for r in rows:
        reports.append(
            {"id": r[0], "lat": r[1], "lng": r[2], "created_at": r[3]}
        )

    # jsonify 會將 Python 的字典/陣列轉為標準的 JSON 格式傳遞
    return jsonify(reports)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)