"""AI 照片辨識模組：呼叫 Azure AI Foundry 部署的多模態模型，預填表單的外觀欄位。

飼主走失公告表單與協尋者目擊通報表單共用這支 API（/api/analyze-photo）。

設計重點：
1. 模型只能從「表單下拉選單的選項」中挑答案（Structured Outputs 的 enum 限制），
   因為 matcher.py 是用字串完全相等來比對，AI 回傳的值必須跟表單選項一字不差。
2. 看不清楚就回「無法判斷」，前端會留空讓使用者自己選，不亂猜。
3. 每個欄位附上信心等級（高／中／低）。模型自報的信心不是真的機率，
   所以只用「分級」而不用數字門檻；協尋者表單只預填「高」的欄位。
4. 照片防呆：逐張判斷「有沒有貓狗」、多張照片「是不是同一隻」，
   不通過時由後端回傳 guard.ok = false，前端擋在照片步驟要求重新上傳，
   避免把沒有寵物、或混了不同動物的照片送進資料庫。
5. AI 只是「預填」，使用者送出前一定會在表單上看到並可修改；
   AI 服務沒設定或出錯時回傳錯誤碼，前端照常讓使用者手動填寫，不會擋住流程。

需要的環境變數（可寫在專案根目錄的 .env）：
  AZURE_OPENAI_ENDPOINT     例：https://<你的資源名稱>.openai.azure.com/
  AZURE_OPENAI_API_KEY      Foundry 專案 / 資源的金鑰
  AZURE_OPENAI_DEPLOYMENT   你部署的模型名稱（例：gpt-4.1-mini；gpt-4o-mini 已於 2026/10/1 退役）
  AZURE_OPENAI_API_VERSION  選填，預設 2024-10-21
"""

import base64
import io
import json
import os

from flask import Blueprint, jsonify, request
from PIL import Image, ImageOps, UnidentifiedImageError

# ==========================================
# 允許的選項：必須與 lost_form.html / sighting_form.html 的 <option value> 完全一致
# ==========================================
UNKNOWN = "無法判斷"

FUR_COLORS = ["黑/深棕", "白/米白/淺黃", "黃/棕/褐色", "虎斑/花紋", "三花/雙色"]
ACCESSORIES = ["沒有", "項圈", "胸背帶", "衣服", "名牌", "其他"]
ACCESSORY_COLORS = ["紅色", "藍色", "黃色", "綠色", "黑色", "其他"]
# 「跛腳」是行為特徵，靜態照片看不出來，所以不讓 AI 選
SPECIAL_MARKS = ["沒有", "白襪子", "胸前白斑", "眼周色塊", "斷尾", "缺耳", "其他"]
SPECIES = ["狗", "貓"]

# 信心等級：只分三級，不用數字（模型自報的 0.8 不代表 80% 會對）
CONFIDENCE_LEVELS = ["高", "中", "低"]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 單張 10MB
MAX_SIDE_PX = 1024  # 送給模型前先縮圖，省錢也比較快


def _field_schema(options):
  return {
      "type": "object",
      "additionalProperties": False,
      "required": ["value", "confidence", "evidence"],
      "properties": {
          "value": {"type": "string", "enum": options + [UNKNOWN]},
          "confidence": {
              "type": "string",
              "enum": CONFIDENCE_LEVELS,
              "description": "對這個欄位答案的把握程度",
          },
          "evidence": {
              "type": "string",
              "description": "用繁體中文簡短說明在照片哪裡看到（20 字內）",
          },
      },
  }


# 同一隻判斷的三種答案
SAME_ANIMAL = ["是", "否", "無法判斷"]

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_pet",
        "photo_checks",
        "same_animal",
        "same_animal_evidence",
        "species",
        "fur_color",
        "accessory",
        "accessory_color",
        "special_mark",
    ],
    "properties": {
        "is_pet": {
            "type": "boolean",
            "description": "照片中是否清楚出現狗或貓",
        },
        # 逐張檢查：每張照片各自有沒有貓狗、是哪一種
        "photo_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["photo_index", "has_pet", "species"],
                "properties": {
                    "photo_index": {"type": "integer", "description": "第幾張照片，從 1 開始"},
                    "has_pet": {"type": "boolean"},
                    "species": {"type": "string", "enum": SPECIES + [UNKNOWN]},
                },
            },
        },
        "same_animal": {"type": "string", "enum": SAME_ANIMAL},
        "same_animal_evidence": {
            "type": "string",
            "description": "判斷是否同一隻的理由，繁體中文 30 字內",
        },
        "species": _field_schema(SPECIES),
        "fur_color": _field_schema(FUR_COLORS),
        "accessory": _field_schema(ACCESSORIES),
        "accessory_color": _field_schema(ACCESSORY_COLORS + ["無"]),
        "special_mark": _field_schema(SPECIAL_MARKS),
    },
}

SYSTEM_PROMPT = f"""你是走失寵物協尋平台的照片辨識助手。使用者會上傳 1 到 5 張同一隻寵物的照片，
可能是飼主拍的清楚照片，也可能是路人匆忙遠拍、夜間拍攝、動物躲在車底等品質較差的照片。
請綜合所有照片，只依照「照片中看得到的內容」填寫欄位，每個欄位只能從給定選項中選一個。

【欄位說明】
- fur_color 毛色色系（選最主要的整體印象）：
  黑/深棕＝全身以黑色或深咖啡色為主；白/米白/淺黃＝以白、奶油色、淺金為主；
  黃/棕/褐色＝以橘、黃、棕、紅棕為主（如橘貓、柴犬赤色）；
  虎斑/花紋＝有明顯條紋或斑紋，玳瑁（黑棕橘混雜的斑駁花色）也歸此類；三花/雙色＝兩到三種大面積色塊（如三花貓、賓士貓、黑白狗）。
- accessory 配件：脖子上的環狀物＝項圈；跨過胸口和背部的背帶＝胸背帶；有穿衣服＝衣服；
  只看到吊牌名牌＝名牌；項圈加名牌時選「項圈」。脖子和身體清楚可見且沒有配件時才選「沒有」。
- accessory_color 配件顏色：配件的主要顏色；accessory 為「沒有」時填「無」。
- special_mark 特殊記號（選最明顯、最有辨識度的一個）：
  白襪子＝四肢末端是白色但身體不是全白；胸前白斑＝胸口有一塊白毛但身體其他地方不是白色；
  眼周色塊＝眼睛周圍有與臉部不同顏色的色塊；斷尾＝尾巴明顯比正常短或缺一截；
  缺耳＝耳朵有缺角或剪耳（如 TNR 剪耳）；有其他明顯特徵（疤痕、特殊斑點）選「其他」並在 evidence 描述。
  四肢、胸口、臉、尾巴都清楚可見且都沒有上述特徵時才選「沒有」。

- species 物種：狗或貓。

【照片檢查 photo_checks 與 same_animal】
- photo_checks：依照片順序，每張照片各給一筆（photo_index 從 1 開始）。
  has_pet：這張照片裡有沒有狗或貓。只拍到身體局部的特寫（腳掌、項圈、尾巴、臉）也算有，
  但照片裡只有人、車、風景、物品、其他動物（鳥、兔子等）時填 false。
- same_animal：多張照片是否為同一隻動物。
  只有在明顯是不同動物時才填「否」（例如一張貓一張狗、毛色或花紋完全不同）；
  局部特寫無法比較、或只有一張照片時填「無法判斷」或「是」，不要因為角度不同就填「否」。
- same_animal_evidence：用繁體中文 30 字內說明判斷理由。

【信心等級 confidence】
- 高：該部位在照片中清楚可見，你非常確定答案。
- 中：大致看得到，但光線、角度或距離讓你有些不確定。
- 低：只能勉強猜測。
照片偏暗、模糊、太遠、被遮擋時，請降低信心等級，不要因為想給答案就填「高」。

【規則】
- 看不清楚、被擋住、照片太模糊就選「{UNKNOWN}」，不要猜。寧可留空也不要填錯。
- 所有照片都沒有狗或貓時，is_pet 填 false，其餘欄位全部填「{UNKNOWN}」、信心填「低」。
- evidence 用繁體中文、20 字內，說明你在照片哪裡看到這個特徵。
"""


# ==========================================
# 圖片前處理
# ==========================================
def prepare_image(raw_bytes):
  """修正手機照片方向、轉成 RGB、縮到最長邊 1024px，回傳 data URL。"""
  img = Image.open(io.BytesIO(raw_bytes))
  img = ImageOps.exif_transpose(img)
  img = img.convert("RGB")
  img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
  buf = io.BytesIO()
  img.save(buf, format="JPEG", quality=85)
  b64 = base64.b64encode(buf.getvalue()).decode("ascii")
  return f"data:image/jpeg;base64,{b64}"


# ==========================================
# 呼叫模型
# ==========================================
_client = None


class VisionNotConfigured(Exception):
  pass


def get_client():
  """延遲建立 Azure OpenAI client；環境變數沒設定時丟出 VisionNotConfigured。"""
  global _client
  if _client is not None:
    return _client

  endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
  api_key = os.environ.get("AZURE_OPENAI_API_KEY")
  if not endpoint or not api_key or not os.environ.get(
      "AZURE_OPENAI_DEPLOYMENT"
  ):
    raise VisionNotConfigured("尚未設定 Azure OpenAI 環境變數")

  from openai import AzureOpenAI  # 延遲載入：沒設定 AI 時整個網站照樣能跑

  _client = AzureOpenAI(
      azure_endpoint=endpoint,
      api_key=api_key,
      api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
      timeout=20.0,  # 現場網路不穩時不要讓使用者等太久
  )
  return _client


def call_model(image_data_urls):
  """把圖片送給 Foundry 部署的模型，回傳模型輸出的 dict（已符合 RESPONSE_SCHEMA）。"""
  client = get_client()
  content = [{"type": "text", "text": "請辨識以下寵物照片並依規則填寫欄位。"}]
  for url in image_data_urls:
    content.append({"type": "image_url", "image_url": {"url": url}})

  resp = client.chat.completions.create(
      model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
      messages=[
          {"role": "system", "content": SYSTEM_PROMPT},
          {"role": "user", "content": content},
      ],
      response_format={
          "type": "json_schema",
          "json_schema": {
              "name": "pet_features",
              "strict": True,
              "schema": RESPONSE_SCHEMA,
          },
      },
  )
  return json.loads(resp.choices[0].message.content)


# ==========================================
# 結果整理：轉成前端好用的格式
# ==========================================
def _pick(field, allowed):
  """取出欄位值與信心；不在允許清單或「無法判斷」時 value 回傳 None（前端就不預填）。"""
  if not isinstance(field, dict):
    return {"value": None, "confidence": None, "evidence": ""}
  value = field.get("value")
  evidence = (field.get("evidence") or "").strip()[:60]
  confidence = field.get("confidence")
  if confidence not in CONFIDENCE_LEVELS:
    confidence = None
  if value not in allowed:
    value = None
  return {"value": value, "confidence": confidence, "evidence": evidence}


def check_photos(raw, photo_count):
  """照片防呆：根據模型的逐張檢查，找出「沒有貓狗的照片」與「不是同一隻」的情況。

  回傳 guard 字典，前端看 ok 決定能不能進下一步：
    ok              True＝通過；False＝要擋下來請使用者重新上傳
    no_pet_photos   哪幾張照片看不到貓狗（從 1 開始的編號）
    mismatch        照片之間是否明顯不是同一隻
    message         給使用者看的說明
  判斷寫在後端，飼主表單與協尋者表單用同一套規則。
  """
  checks = raw.get("photo_checks")
  if not isinstance(checks, list):
    checks = []

  no_pet, species_seen = [], set()
  for c in checks:
    if not isinstance(c, dict):
      continue
    idx = c.get("photo_index")
    if not isinstance(idx, int) or not (1 <= idx <= photo_count):
      continue
    if c.get("has_pet") is False:
      no_pet.append(idx)
    elif c.get("species") in SPECIES:
      species_seen.add(c.get("species"))

  # 整體判斷「沒有寵物」但逐張檢查缺漏時，視為全部照片都沒有
  if not raw.get("is_pet") and not no_pet:
    no_pet = list(range(1, photo_count + 1))
  no_pet = sorted(set(no_pet))

  # 不同照片判斷出不同物種，或模型明確說不是同一隻 → 不一致
  mismatch = photo_count > 1 and (
      len(species_seen) > 1 or raw.get("same_animal") == "否"
  )

  messages = []
  if no_pet:
    nums = "、".join(str(i) for i in no_pet)
    messages.append(f"第 {nums} 張照片裡看不到貓或狗，請重新上傳有貓狗的照片。")
  if mismatch:
    reason = (raw.get("same_animal_evidence") or "").strip()[:40]
    messages.append(
        "這幾張照片看起來不是同一隻動物"
        + (f"（{reason}）" if reason else "")
        + "，請確認都是同一隻後再上傳。"
    )

  return {
      "ok": not no_pet and not mismatch,
      "no_pet_photos": no_pet,
      "mismatch": mismatch,
      "message": " ".join(messages),
  }


def normalize_result(raw, photo_count=1):
  guard = check_photos(raw, photo_count)
  is_pet = bool(raw.get("is_pet"))
  empty = {"value": None, "confidence": None, "evidence": ""}

  if not is_pet:
    return {
        "is_pet": False,
        "species": None,
        "guard": guard,
        "fields": {
            k: dict(empty)
            for k in ("species", "fur_color", "accessory", "accessory_color", "special_mark")
        },
    }

  species_field = _pick(raw.get("species"), SPECIES)
  fields = {
      "species": species_field,
      "fur_color": _pick(raw.get("fur_color"), FUR_COLORS),
      "accessory": _pick(raw.get("accessory"), ACCESSORIES),
      "accessory_color": _pick(raw.get("accessory_color"), ACCESSORY_COLORS),
      "special_mark": _pick(raw.get("special_mark"), SPECIAL_MARKS),
  }
  # 沒有配件（或配件不確定）時，配件顏色沒有意義
  if fields["accessory"]["value"] in (None, "沒有"):
    fields["accessory_color"] = dict(empty)

  # species 另外保留字串格式，飼主表單用它做「物種交叉檢查」
  return {
      "is_pet": True,
      "species": species_field["value"],
      "guard": guard,
      "fields": fields,
  }


# ==========================================
# API 路由
# ==========================================
vision_bp = Blueprint("vision", __name__)


@vision_bp.route("/api/analyze-photo", methods=["POST"])
def analyze_photo():
  """接收表單照片（photo_1 必填、photo_2～photo_5 選填），回傳 AI 預填建議。

  照片不在這裡存檔，只用來辨識；正式送出時仍走原本的 /api/upload。
  """
  if "photo_1" not in request.files or request.files["photo_1"].filename == "":
    return jsonify({"error": "請至少提供第 1 張照片"}), 400

  data_urls = []
  for key in ("photo_1", "photo_2", "photo_3", "photo_4", "photo_5"):
    f = request.files.get(key)
    if not f or f.filename == "":
      continue
    raw = f.read()
    if len(raw) > MAX_UPLOAD_BYTES:
      return jsonify({"error": "照片太大（單張上限 10MB）"}), 400
    try:
      data_urls.append(prepare_image(raw))
    except (UnidentifiedImageError, OSError):
      return jsonify({"error": "無法讀取照片格式，請改用 jpg 或 png"}), 400

  try:
    raw_result = call_model(data_urls)
  except VisionNotConfigured:
    return jsonify({"error": "AI 辨識尚未設定，請手動填寫"}), 503
  except Exception as e:  # 網路、額度、內容過濾等錯誤都不該擋住使用者
    print(f"[vision] AI 辨識失敗：{type(e).__name__}: {e}")
    return jsonify({"error": "AI 辨識暫時無法使用，請手動填寫"}), 502

  return jsonify(normalize_result(raw_result, photo_count=len(data_urls))), 200
