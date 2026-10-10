"""AI 照片辨識模組：呼叫 Azure AI Foundry 部署的多模態模型，預填走失表單的外觀欄位。

設計重點：
1. 模型只能從「表單下拉選單的選項」中挑答案（Structured Outputs 的 enum 限制），
   因為 matcher.py 是用字串完全相等來比對，AI 回傳的值必須跟表單選項一字不差。
2. 看不清楚就回「無法判斷」，前端會留空讓使用者自己選，不亂猜。
3. AI 只是「預填」，使用者送出前一定會在表單上看到並可修改；
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
# 允許的選項：必須與 lost_form.html 的 <option value> 完全一致
# ==========================================
UNKNOWN = "無法判斷"

FUR_COLORS = ["黑/深棕", "白/米白/淺黃", "黃/棕/褐色", "虎斑/花紋", "三花/雙色"]
ACCESSORIES = ["沒有", "項圈", "胸背帶", "衣服", "名牌", "其他"]
ACCESSORY_COLORS = ["紅色", "藍色", "黃色", "綠色", "黑色", "其他"]
# 「跛腳」是行為特徵，靜態照片看不出來，所以不讓 AI 選
SPECIAL_MARKS = ["沒有", "白襪子", "胸前白斑", "眼周色塊", "斷尾", "缺耳", "其他"]
SPECIES = ["狗", "貓"]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 單張 10MB
MAX_SIDE_PX = 1024  # 送給模型前先縮圖，省錢也比較快


def _field_schema(options):
  return {
      "type": "object",
      "additionalProperties": False,
      "required": ["value", "evidence"],
      "properties": {
          "value": {"type": "string", "enum": options + [UNKNOWN]},
          "evidence": {
              "type": "string",
              "description": "用繁體中文簡短說明在照片哪裡看到（20 字內）",
          },
      },
  }


RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_pet",
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
        "species": {"type": "string", "enum": SPECIES + [UNKNOWN]},
        "fur_color": _field_schema(FUR_COLORS),
        "accessory": _field_schema(ACCESSORIES),
        "accessory_color": _field_schema(ACCESSORY_COLORS + ["無"]),
        "special_mark": _field_schema(SPECIAL_MARKS),
    },
}

SYSTEM_PROMPT = f"""你是走失寵物協尋平台的照片辨識助手。使用者會上傳 1 到 3 張同一隻寵物的照片：
第 1 張是全身照，第 2 張（若有）是特殊記號特寫，第 3 張（若有）是配件特寫。
請綜合所有照片，只依照「照片中看得到的內容」填寫欄位，每個欄位只能從給定選項中選一個。

【欄位說明】
- fur_color 毛色色系（選最主要的整體印象）：
  黑/深棕＝全身以黑色或深咖啡色為主；白/米白/淺黃＝以白、奶油色、淺金為主；
  黃/棕/褐色＝以橘、黃、棕、紅棕為主（如橘貓、柴犬赤色）；
  虎斑/花紋＝有明顯條紋或斑紋；三花/雙色＝兩到三種大面積色塊（如三花貓、賓士貓、黑白狗）。
- accessory 配件：脖子上的環狀物＝項圈；跨過胸口和背部的背帶＝胸背帶；有穿衣服＝衣服；
  只看到吊牌名牌＝名牌；項圈加名牌時選「項圈」。脖子和身體清楚可見且沒有配件時才選「沒有」。
- accessory_color 配件顏色：配件的主要顏色；accessory 為「沒有」時填「無」。
- special_mark 特殊記號（選最明顯、最有辨識度的一個）：
  白襪子＝四肢末端是白色但身體不是全白；胸前白斑＝胸口有一塊白毛但身體其他地方不是白色；
  眼周色塊＝眼睛周圍有與臉部不同顏色的色塊；斷尾＝尾巴明顯比正常短或缺一截；
  缺耳＝耳朵有缺角或剪耳（如 TNR 剪耳）；有其他明顯特徵（疤痕、特殊斑點）選「其他」並在 evidence 描述。
  四肢、胸口、臉、尾巴都清楚可見且都沒有上述特徵時才選「沒有」。

【規則】
- 看不清楚、被擋住、照片太模糊就選「{UNKNOWN}」，不要猜。寧可留空也不要填錯。
- 照片中沒有狗或貓時，is_pet 填 false，其餘欄位全部填「{UNKNOWN}」。
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
  """取出欄位值；不在允許清單或「無法判斷」時回傳 None（前端就不預填）。"""
  if not isinstance(field, dict):
    return {"value": None, "evidence": ""}
  value = field.get("value")
  evidence = (field.get("evidence") or "").strip()[:60]
  if value not in allowed:
    value = None
  return {"value": value, "evidence": evidence}


def normalize_result(raw):
  is_pet = bool(raw.get("is_pet"))
  species = raw.get("species") if raw.get("species") in SPECIES else None

  if not is_pet:
    empty = {"value": None, "evidence": ""}
    return {
        "is_pet": False,
        "species": None,
        "fields": {
            k: dict(empty)
            for k in ("fur_color", "accessory", "accessory_color", "special_mark")
        },
    }

  fields = {
      "fur_color": _pick(raw.get("fur_color"), FUR_COLORS),
      "accessory": _pick(raw.get("accessory"), ACCESSORIES),
      "accessory_color": _pick(raw.get("accessory_color"), ACCESSORY_COLORS),
      "special_mark": _pick(raw.get("special_mark"), SPECIAL_MARKS),
  }
  # 沒有配件（或配件不確定）時，配件顏色沒有意義
  if fields["accessory"]["value"] in (None, "沒有"):
    fields["accessory_color"] = {"value": None, "evidence": ""}

  return {"is_pet": True, "species": species, "fields": fields}


# ==========================================
# API 路由
# ==========================================
vision_bp = Blueprint("vision", __name__)


@vision_bp.route("/api/analyze-photo", methods=["POST"])
def analyze_photo():
  """接收表單照片（photo_1 必填、photo_2/photo_3 選填），回傳 AI 預填建議。

  照片不在這裡存檔，只用來辨識；正式送出公告時仍走原本的 /api/upload。
  """
  if "photo_1" not in request.files or request.files["photo_1"].filename == "":
    return jsonify({"error": "請至少提供第 1 張全身照"}), 400

  data_urls = []
  for key in ("photo_1", "photo_2", "photo_3"):
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

  return jsonify(normalize_result(raw_result)), 200