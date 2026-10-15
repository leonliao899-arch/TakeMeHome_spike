"""AI 照片比對（參考意見）：只做顯示，不參與 matcher.py 的分數、門檻、排序與通知。

流程：
1. 協尋者送出通報／飼主發布公告後，app.py 在背景執行緒呼叫 run_job。
2. 一次 Azure 呼叫：1 張觸發方照片 + 最多 5 張候選照片（一律傳 https 網址，不轉 base64）。
3. 結果存進 ai_comparisons（同一組配對只算一次），畫面只讀資料庫。
4. 任何失敗（逾時、金鑰錯、格式不合）只記 log，畫面什麼都不顯示。

環境變數 AI_COMPARE=1 才啟用；沒設或其他值＝完全不呼叫 Azure、畫面不顯示任何 AI 區塊。
Azure 的設定沿用 vision.py（AZURE_OPENAI_*）。
"""

import json
import os
import re

from vision import VisionNotConfigured, get_client

MAX_CANDIDATES = 5
CALL_TIMEOUT = 15.0       # 秒；逾時就略過
PENDING_SECONDS = 90      # 「比對中」只在 job 開始後這段時間內成立
LETTERS = "ABCDE"
SIMILARITIES = ["高", "中", "低", "無法判斷"]
REASON_MAX = 60

SYSTEM_PROMPT = """你是協助比對走失寵物照片的助理。
第 1 張圖是「待比對照片」，其餘圖片依序是「候選 A、B、C、D、E」。
請逐一判斷每個候選與待比對照片是不是同一隻動物。

規則：
- 只根據照片中看得到的外觀（毛色、花紋、耳型、體型比例、配件、特殊記號）判斷。
- 不要猜測，不要編造照片裡看不到的特徵。
- 照片模糊、角度不足、或兩張不是同一物種時，回「無法判斷」或「低」。
- similarity 只能是：高（很可能同一隻）、中（有些特徵吻合，值得確認）、低（明顯不同）、無法判斷。
- reason 用繁體中文一句話說明依據，60 字內；直接描述外觀差異或相似處，不要提到「候選」、字母或「待比對照片」。
- 每個候選只回一筆，candidate 用字母 A～E。"""


def enabled():
  return os.environ.get("AI_COMPARE") == "1"


def _schema(n):
  return {
      "type": "object",
      "additionalProperties": False,
      "required": ["results"],
      "properties": {
          "results": {
              "type": "array",
              "items": {
                  "type": "object",
                  "additionalProperties": False,
                  "required": ["candidate", "similarity", "reason"],
                  "properties": {
                      "candidate": {"type": "string", "enum": list(LETTERS[:n])},
                      "similarity": {"type": "string", "enum": SIMILARITIES},
                      "reason": {"type": "string"},
                  },
              },
          }
      },
  }


def _is_https(url):
  return isinstance(url, str) and url.startswith("https://")


def call_model(trigger_photo, candidate_photos):
  """送出一次比對，回傳模型輸出的 dict；錯誤直接丟出，由呼叫端記 log。"""
  n = len(candidate_photos)
  client = get_client().with_options(timeout=CALL_TIMEOUT, max_retries=0)
  content = [{"type": "text", "text": "【待比對照片】"},
             {"type": "image_url", "image_url": {"url": trigger_photo}}]
  for i, url in enumerate(candidate_photos):
    content.append({"type": "text", "text": f"【候選 {LETTERS[i]}】"})
    content.append({"type": "image_url", "image_url": {"url": url}})
  resp = client.chat.completions.create(
      model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
      temperature=0.1,
      messages=[
          {"role": "system", "content": SYSTEM_PROMPT},
          {"role": "user", "content": content},
      ],
      response_format={
          "type": "json_schema",
          "json_schema": {"name": "pet_compare", "strict": True, "schema": _schema(n)},
      },
  )
  return json.loads(resp.choices[0].message.content)


def parse_results(raw, n):
  """回傳 {候選索引(0 起算): (similarity, reason)}。缺漏、重複、超出數量、值不合的一律丟棄。"""
  items = raw.get("results") if isinstance(raw, dict) else None
  if not isinstance(items, list):
    return {}
  seen, dup = {}, set()
  for it in items:
    if not isinstance(it, dict):
      continue
    letter = it.get("candidate")
    sim = it.get("similarity")
    reason = it.get("reason")
    if not isinstance(letter, str) or letter not in LETTERS[:n]:
      continue
    if letter in seen:
      dup.add(letter)
      continue
    if sim not in SIMILARITIES or not isinstance(reason, str):
      dup.add(letter)  # 這個候選的資料不可信，整個丟掉
      continue
    reason = re.sub(r"(候選\s*[A-E]|待比對照片)", "這隻", reason.strip())
    seen[letter] = (sim, reason[:REASON_MAX])
  return {LETTERS.index(k): v for k, v in seen.items() if k not in dup}


def _pair(trigger_type, trigger_id, cand_id):
  """回傳 (lost_id, sighting_id)"""
  return (cand_id, trigger_id) if trigger_type == "sighting" else (trigger_id, cand_id)


def existing_cand_ids(conn, trigger_type, trigger_id, cand_ids):
  """候選裡已經有結果的 id 集合。"""
  if not cand_ids:
    return set()
  if trigger_type == "sighting":
    rows = conn.execute(
        "SELECT lost_id AS cid FROM ai_comparisons WHERE sighting_id = %s AND lost_id = ANY(%s)",
        (trigger_id, list(cand_ids)),
    ).fetchall()
  else:
    rows = conn.execute(
        "SELECT sighting_id AS cid FROM ai_comparisons WHERE lost_id = %s AND sighting_id = ANY(%s)",
        (trigger_id, list(cand_ids)),
    ).fetchall()
  return {r["cid"] for r in rows}


def compare_and_store(conn, trigger_type, trigger_id, trigger_photo, cands):
  """cands: [(候選 id, 候選照片網址)]，已依 rank_key 排好、最多取前 5。
  回傳寫入筆數；沒有要呼叫的（全都有結果、沒有 https 照片）回 0 且不呼叫 Azure。
  失敗丟出例外。"""
  cands = [(cid, url) for cid, url in cands[:MAX_CANDIDATES]]
  done = existing_cand_ids(conn, trigger_type, trigger_id, [c[0] for c in cands])
  todo = [(cid, url) for cid, url in cands if cid not in done and _is_https(url)]
  if not todo or not _is_https(trigger_photo):
    return 0
  raw = call_model(trigger_photo, [url for _, url in todo])
  parsed = parse_results(raw, len(todo))
  written = 0
  for idx, (sim, reason) in parsed.items():
    lost_id, sighting_id = _pair(trigger_type, trigger_id, todo[idx][0])
    cur = conn.execute(
        "INSERT INTO ai_comparisons (lost_id, sighting_id, similarity, reason)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (lost_id, sighting_id) DO NOTHING",
        (lost_id, sighting_id, sim, reason),
    )
    written += cur.rowcount
  return written


# ==========================================
# job：用來判斷「比對中」
# ==========================================
def create_job(conn, trigger_type, trigger_id):
  row = conn.execute(
      "INSERT INTO ai_compare_jobs (trigger_type, trigger_id) VALUES (%s, %s) RETURNING id",
      (trigger_type, trigger_id),
  ).fetchone()
  return row["id"]


def finish_job(conn, job_id, ok):
  conn.execute(
      "UPDATE ai_compare_jobs SET finished_at = NOW(), ok = %s WHERE id = %s", (ok, job_id)
  )


def run_job(get_conn, job_id, trigger_type, trigger_id, pick):
  """背景執行緒的進入點。pick(conn) -> (觸發照片網址, [(候選 id, 照片網址)])。
  任何例外都只記 log，並把 job 標成結束，確保不會殘留「比對中」。"""
  ok = False
  try:
    with get_conn() as conn:
      photo, cands = pick(conn)
      n = compare_and_store(conn, trigger_type, trigger_id, photo, cands)
      print(f"[ai_compare] {trigger_type} job {job_id} 完成，寫入 {n} 筆")
    ok = True
  except VisionNotConfigured:
    print("[ai_compare] Azure 尚未設定，略過")
  except Exception as e:
    print(f"[ai_compare] job {job_id} 失敗：{type(e).__name__}: {str(e)[:200]}")
  finally:
    try:
      with get_conn() as conn:
        finish_job(conn, job_id, ok)
    except Exception as e:
      print(f"[ai_compare] 結束 job 失敗：{type(e).__name__}")


def pending_trigger_ids(conn, trigger_type, ids):
  """ids 裡有「進行中」job 的觸發 id 集合（未結束且 90 秒內開始）。"""
  if not ids:
    return set()
  rows = conn.execute(
      "SELECT DISTINCT trigger_id FROM ai_compare_jobs"
      " WHERE trigger_type = %s AND trigger_id = ANY(%s) AND finished_at IS NULL"
      f" AND started_at > NOW() - INTERVAL '{PENDING_SECONDS} seconds'",
      (trigger_type, list(ids)),
  ).fetchall()
  return {r["trigger_id"] for r in rows}
