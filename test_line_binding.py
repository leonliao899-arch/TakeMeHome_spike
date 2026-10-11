"""階段 3～5 自動檢驗：成功頁、LINE webhook 綁定、bind-status、防重複送出。

用法（專案根目錄、啟用 venv）：
  python test_line_binding.py

注意：
- 會連到 .env 裡 DATABASE_URL 指向的資料庫，寫入幾筆測試資料，跑完自動刪除。
  請用 Neon 的 dev branch，不要對 production 跑。
- LINE 的金鑰在這支測試裡換成假的，對 LINE API 的呼叫也會被攔下來記錄，
  不會真的傳訊息給任何人。
"""
import base64
import hashlib
import hmac
import json
import os
import uuid

# 必須在 import app 之前設定：測試用的假金鑰與 Basic ID
TEST_SECRET = "test-channel-secret"
os.environ["LINE_CHANNEL_SECRET"] = TEST_SECRET
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "test-access-token"
os.environ["LINE_BOT_BASIC_ID"] = "@123abcde"
os.environ["PUBLIC_BASE_URL"] = "https://takemehome-spike.onrender.com"
# 測試時 webhook 改成同步處理，送出後就能立刻檢查資料庫（正式環境是先回 200 再背景處理）
os.environ["LINE_WEBHOOK_SYNC"] = "1"

import app as app_module  # noqa: E402  （會執行 init_db，補上新欄位與索引）

client = app_module.app.test_client()
results = []
created_lost, created_sightings = [], []


def check(name, ok, detail=""):
  results.append(ok)
  print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail and not ok else ""))


# ------------------------------------------------------------
# 攔截 LINE API：記下每次呼叫，可以指定回應的狀態碼
# ------------------------------------------------------------
class FakeResp:
  def __init__(self, status):
    self.status_code = status
    self.text = "" if status == 200 else '{"message":"Invalid reply token"}'


line_calls = []
reply_status = {"code": 200}


def fake_post(url, json=None, headers=None, timeout=None):
  line_calls.append({"url": url, "json": json, "headers": headers})
  if url.endswith("/reply"):
    return FakeResp(reply_status["code"])
  return FakeResp(200)


app_module.requests.post = fake_post


def sign(body_bytes, secret=TEST_SECRET):
  digest = hmac.new(secret.encode(), body_bytes, hashlib.sha256).digest()
  return base64.b64encode(digest).decode()


def send_webhook(events, secret=TEST_SECRET, signature=None):
  body = json.dumps({"destination": "Uxxxx", "events": events}, ensure_ascii=False).encode()
  sig = signature if signature is not None else sign(body, secret)
  return client.post("/line/webhook", data=body,
                     headers={"X-Line-Signature": sig, "Content-Type": "application/json"})


def text_event(text, user_id="U_test_user_A", source_type="user", redelivery=False, event_id=None):
  source = {"type": source_type, "userId": user_id}
  if source_type == "group":
    source["groupId"] = "C_test_group"
  return {
      "type": "message",
      "webhookEventId": event_id or ("TEST" + uuid.uuid4().hex),
      "replyToken": "test-reply-token",
      "source": source,
      "deliveryContext": {"isRedelivery": redelivery},
      "message": {"type": "text", "id": "1", "text": text},
  }


def last_texts():
  return [m["text"] for m in line_calls[-1]["json"]["messages"]] if line_calls else []


def db_value(sql, params):
  with app_module.get_db_connection() as conn:
    row = conn.execute(sql, params).fetchone()
  return row


LOST_PAYLOAD = {
    "pet_name": "測試小黑", "species": "狗", "size": "中", "fur_length": "短毛",
    "accessory": "項圈", "accessory_color": "紅色", "fur_color": "黑/深棕",
    "special_mark": "白襪子", "photo_1": "https://example.com/a.jpg",
    "lat": "25.026", "lng": "121.543", "privacy_radius": "50",
    "lost_time": "2026-10-11T08:30", "email": "test@example.com",
}

try:
  print("=" * 60)
  print("階段 3：成功頁與 LINE 按鈕")
  print("=" * 60)
  r = client.get("/lost-form")
  html = r.get_data(as_text=True)
  check("飼主表單帶入 Basic ID 與 line_bind.js",
        r.status_code == 200 and '"@123abcde"' in html and "line_bind.js" in html)
  r = client.get("/sighting-form")
  html = r.get_data(as_text=True)
  check("協尋者表單帶入 Basic ID 與 line_bind.js",
        r.status_code == 200 and '"@123abcde"' in html and "line_bind.js" in html)
  r = client.get("/static/line_bind.js")
  js = r.get_data(as_text=True)
  check("line_bind.js 可下載，且使用 oaMessage 網址", r.status_code == 200 and "line.me/R/oaMessage/" in js)

  print()
  print("=" * 60)
  print("階段 5（先做）：飼主表單防重複送出")
  print("=" * 60)
  submit_id = str(uuid.uuid4())
  r1 = client.post("/api/lost_reports", json={**LOST_PAYLOAD, "client_submit_id": submit_id})
  d1 = r1.get_json()
  created_lost.append(d1["id"])
  r2 = client.post("/api/lost_reports", json={**LOST_PAYLOAD, "client_submit_id": submit_id})
  d2 = r2.get_json()
  check("第一次送出回 201", r1.status_code == 201, r1.status_code)
  check("同一個送出編號再送一次回 200、標記 duplicate", r2.status_code == 200 and d2.get("duplicate") is True)
  check("兩次拿到同一筆公告與同一組 token", d1["id"] == d2["id"] and d1["owner_token"] == d2["owner_token"])
  n = db_value("SELECT COUNT(*) AS n FROM lost_reports WHERE client_submit_id = %s", (submit_id,))["n"]
  check("資料庫只有一筆", n == 1, n)
  r3 = client.post("/api/lost_reports", json=LOST_PAYLOAD)   # 舊版前端：沒有送出編號
  d3 = r3.get_json()
  created_lost.append(d3["id"])
  check("沒有送出編號仍可發布（相容舊前端）", r3.status_code == 201 and d3["id"] != d1["id"])

  owner_token = d1["owner_token"]
  check("owner_token 長度 36 且格式正確",
        len(owner_token) == 36 and app_module.OWNER_TOKEN_RE.match(owner_token) is not None)

  print()
  print("=" * 60)
  print("階段 4：Webhook 綁定")
  print("=" * 60)
  r = client.get(f"/api/bind-status/{owner_token}")
  check("綁定前 bind-status = false", r.get_json() == {"bound": False})
  check("bind-status 加上 no-store", r.headers.get("Cache-Control") == "no-store")

  r = send_webhook([], signature="forged-signature")
  check("偽造簽章被拒（400）", r.status_code == 400)
  r = send_webhook([], secret="wrong-secret")
  check("用錯的 secret 簽章被拒（400）", r.status_code == 400)
  r = send_webhook([])
  check("LINE 後台 Verify（空 events）回 200", r.status_code == 200)

  # 寬鬆比對：大寫、全形、換行、多餘空白都要抓得到
  messy = "綁定\n  " + owner_token.upper().replace("OWN_", "ＯＷＮ＿")[:20] + "  " + owner_token.upper()[20:]
  line_calls.clear()
  r = send_webhook([text_event(messy, user_id="U_test_user_A")])
  bound_row = db_value("SELECT line_user_id FROM lost_reports WHERE id = %s", (d1["id"],))
  check("大寫＋全形＋換行的 token 也能綁定", r.status_code == 200 and bound_row["line_user_id"] == "U_test_user_A",
        bound_row)
  texts = last_texts()
  check("用 reply API 回覆", line_calls and line_calls[-1]["url"].endswith("/v2/bot/message/reply"))
  check("回覆「已綁定『測試小黑』LOST-xxxx ✓」並附案件連結",
        texts and texts[0].startswith("已綁定「測試小黑」LOST-") and "✓" in texts[0]
        and f"https://takemehome-spike.onrender.com/case/{owner_token}" in texts[0], texts)
  check("帶 Channel access token", line_calls[-1]["headers"]["Authorization"] == "Bearer test-access-token")

  r = client.get(f"/api/bind-status/{owner_token}")
  check("綁定後 bind-status = true", r.get_json() == {"bound": True})

  send_webhook([text_event(f"綁定 {owner_token}", user_id="U_test_user_B")])
  bound_row = db_value("SELECT line_user_id FROM lost_reports WHERE id = %s", (d1["id"],))
  check("另一支手機再綁 → 後綁覆蓋前綁", bound_row["line_user_id"] == "U_test_user_B")

  # 協尋者 token
  sr = client.post("/api/sightings", json={
      "species": "狗", "size": "中", "fur_length": "短毛", "accessory": "沒有",
      "accessory_color": "無", "fur_color": "黑/深棕", "special_mark": "看不清楚",
      "photo_1": "https://example.com/s.jpg", "lat": 25.027, "lng": 121.544,
      "sighting_time": "2026-10-11 09:00",
  })
  sd = sr.get_json()
  created_sightings.append(sd["sighting_id"])
  trk = sd["tracking_token"]
  check("tracking_token 長度 36 且格式正確", len(trk) == 36 and app_module.TRACK_TOKEN_RE.match(trk) is not None)
  send_webhook([text_event(f"綁定 {trk}", user_id="U_test_finder")])
  s_row = db_value("SELECT line_user_id FROM sightings WHERE id = %s", (sd["sighting_id"],))
  check("協尋者 trk_ token 綁定成功", s_row["line_user_id"] == "U_test_finder")
  check("協尋者回覆附追蹤連結",
        f"https://takemehome-spike.onrender.com/track/{trk}" in last_texts()[0], last_texts())
  check("協尋者 bind-status = true", client.get(f"/api/bind-status/{trk}").get_json() == {"bound": True})

  # 亂打、格式對但不存在、群組
  send_webhook([text_event("你好，請問怎麼用？")])
  check("亂打的字串得到使用說明", "綁定 own_" in last_texts()[0], last_texts())
  fake = "own_" + "0" * 32
  send_webhook([text_event(f"綁定 {fake}")])
  check("格式對但不存在的 token → 找不到這組追蹤碼", last_texts() == ["找不到這組追蹤碼，請確認完整複製"], last_texts())
  before = db_value("SELECT line_user_id FROM lost_reports WHERE id = %s", (d3["id"],))["line_user_id"]
  send_webhook([text_event(f"綁定 {d3['owner_token']}", source_type="group", user_id="U_group_member")])
  after = db_value("SELECT line_user_id FROM lost_reports WHERE id = %s", (d3["id"],))["line_user_id"]
  check("群組裡貼 token 不會綁定，並提醒改用一對一", before == after is None and "一對一" in last_texts()[0])

  # 冷啟動相關：reply token 失效時改用 push；LINE 重送的事件直接用 push
  reply_status["code"] = 400
  line_calls.clear()
  send_webhook([text_event(f"綁定 {d3['owner_token']}", user_id="U_test_user_C")])
  check("reply 失敗（token 過期）→ 改用 push 補送",
        len(line_calls) == 2 and line_calls[1]["url"].endswith("/push")
        and line_calls[1]["json"]["to"] == "U_test_user_C")
  reply_status["code"] = 200
  line_calls.clear()
  send_webhook([text_event(f"綁定 {d3['owner_token']}", user_id="U_test_user_C", redelivery=True)])
  check("LINE 重送的事件不用 reply，直接 push",
        len(line_calls) == 1 and line_calls[0]["url"].endswith("/push"))

  # 去重：同一個 webhookEventId 再送一次（LINE 重送），不能再綁定、不能再回覆
  dup_id = "TEST" + uuid.uuid4().hex
  line_calls.clear()
  send_webhook([text_event(f"綁定 {owner_token}", user_id="U_dup_first", event_id=dup_id)])
  first_calls = len(line_calls)
  send_webhook([text_event(f"綁定 {owner_token}", user_id="U_dup_second", event_id=dup_id, redelivery=True)])
  send_webhook([text_event(f"綁定 {owner_token}", user_id="U_dup_third", event_id=dup_id, redelivery=True)])
  owner_now = db_value("SELECT line_user_id FROM lost_reports WHERE id = %s", (d1["id"],))["line_user_id"]
  check("同一個事件重送 2 次：只回覆 1 次", first_calls == 1 and len(line_calls) == 1, len(line_calls))
  check("同一個事件重送：綁定結果不被重送的內容改掉", owner_now == "U_dup_first", owner_now)

  # 先回 200 再處理：LINE API 故意卡 2 秒，webhook 仍要立刻回應
  import threading
  import time as _time
  slow_done = threading.Event()
  def slow_post(url, json=None, headers=None, timeout=None):
    _time.sleep(2)
    line_calls.append({"url": url, "json": json, "headers": headers})
    slow_done.set()
    return FakeResp(200)
  app_module.requests.post = slow_post
  app_module.LINE_WEBHOOK_SYNC = False
  t0 = _time.time()
  r = send_webhook([text_event(f"綁定 {owner_token}", user_id="U_async")])
  elapsed = _time.time() - t0
  app_module.LINE_WEBHOOK_SYNC = True
  check("webhook 不等處理完就回 200（< 1 秒）", r.status_code == 200 and elapsed < 1, f"{elapsed:.2f}s")
  check("背景處理仍會完成綁定並回覆", slow_done.wait(10) and
        db_value("SELECT line_user_id FROM lost_reports WHERE id = %s", (d1["id"],))["line_user_id"] == "U_async")
  app_module.requests.post = fake_post

  # 加好友
  line_calls.clear()
  send_webhook([{"type": "follow", "webhookEventId": "TEST" + uuid.uuid4().hex, "replyToken": "rt",
                 "source": {"type": "user", "userId": "U_new"}}])
  check("加好友時回覆使用說明", line_calls and "綁定" in last_texts()[0])

  # 單一事件出錯不影響回 200
  r = send_webhook([{"type": "message", "message": "not-a-dict"}, text_event("hi")])
  check("事件格式怪異時仍回 200", r.status_code == 200)

  print()
  print("=" * 60)
  print("隱私與既有功能回歸")
  print("=" * 60)
  check("bind-status 亂打格式 → false", client.get("/api/bind-status/hello").get_json() == {"bound": False})
  m = client.get("/api/map-data").get_data(as_text=True)
  c = client.get(f"/api/owner/{owner_token}/case").get_data(as_text=True)
  p = client.get(f"/api/lost_reports/{d1['id']}").get_data(as_text=True)
  check("公開 API 不含 line_user_id / owner_token / client_submit_id",
        all(k not in s for s in (m, c, p) for k in ("line_user_id", "owner_token", "client_submit_id",
                                                    "U_test_user")))
  check("案件頁 /case/<token> 打得開", client.get(f"/case/{owner_token}").status_code == 200)
  check("舊網址 /case/<數字> 打不開", client.get(f"/case/{d1['id']}").status_code == 404)
  t = client.get(f"/track/{trk}")
  check("追蹤頁打得開、含 LINE 區塊", t.status_code == 200 and "line_bind.js" in t.get_data(as_text=True))
  check("案件頁含 LINE 區塊", "line_bind.js" in client.get(f"/case/{owner_token}").get_data(as_text=True))

finally:
  # 清理測試資料
  with app_module.get_db_connection() as conn:
    conn.execute("DELETE FROM line_webhook_events WHERE webhook_event_id LIKE 'TEST%%'")
    for lid in created_lost:
      conn.execute("DELETE FROM sighting_rejections WHERE lost_id = %s", (lid,))
      conn.execute("DELETE FROM lost_reports WHERE id = %s", (lid,))
    for sid in created_sightings:
      conn.execute("DELETE FROM sightings WHERE id = %s", (sid,))
  print()
  print(f"結果：{sum(results)} / {len(results)} 項通過")
