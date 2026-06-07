from flask import Flask, send_from_directory, request, jsonify
from flask_cors import CORS
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            telegram VARCHAR(100) NOT NULL,
            email VARCHAR(100) NOT NULL,
            experience VARCHAR(50),
            reason TEXT,
            status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# 初始化資料庫
try:
    init_db()
    print("資料庫初始化成功")
except Exception as e:
    print(f"資料庫初始化失敗: {e}")

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:path>')
def serve_file(path):
    return send_from_directory('.', path)

# 提交申請
@app.route('/api/apply', methods=['POST'])
def apply():
    data = request.json
    name = data.get('name', '').strip()
    telegram = data.get('telegram', '').strip()
    email = data.get('email', '').strip()
    experience = data.get('experience', '').strip()
    reason = data.get('reason', '').strip()

    if not name or not telegram or not email:
        return jsonify({"ok": False, "error": "請填入所有必填欄位"}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO applications (name, telegram, email, experience, reason)
            VALUES (%s, %s, %s, %s, %s)
        """, (name, telegram, email, experience, reason))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "message": "申請已送出！"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# 查詢所有申請（後台用）
@app.route('/api/applications', methods=['GET'])
def get_applications():
    admin_key = request.headers.get('X-Admin-Key')
    if admin_key != os.environ.get("ADMIN_KEY", "xauelite2024"):
        return jsonify({"ok": False, "error": "未授權"}), 401

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM applications ORDER BY created_at DESC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "data": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# 更新申請狀態（核准／拒絕）
@app.route('/api/applications/<int:app_id>', methods=['PATCH'])
def update_application(app_id):
    admin_key = request.headers.get('X-Admin-Key')
    if admin_key != os.environ.get("ADMIN_KEY", "xauelite2024"):
        return jsonify({"ok": False, "error": "未授權"}), 401

    data = request.json
    status = data.get('status')
    if status not in ['approved', 'rejected', 'pending']:
        return jsonify({"ok": False, "error": "無效狀態"}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET status = %s WHERE id = %s", (status, app_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
