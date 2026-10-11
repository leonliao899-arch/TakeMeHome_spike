"""AI 照片辨識模組測試（不需要 Azure 金鑰：用假的模型回應測試）

執行：python test_vision.py
"""

import io
import os
import re
import unittest
from unittest import mock

from flask import Flask
from PIL import Image

import vision

HERE = os.path.dirname(os.path.abspath(__file__))


def make_jpeg_bytes(size=(2000, 1500), color=(120, 80, 40)):
  buf = io.BytesIO()
  Image.new("RGB", size, color).save(buf, format="JPEG")
  return buf.getvalue()


def form_options(select_id, template="lost_form.html"):
  """從表單 HTML 抓出某個 <select> 的所有非空 option value。"""
  with open(os.path.join(HERE, "templates", template), encoding="utf-8") as f:
    html = f.read()
  block = re.search(rf'<select id="{select_id}".*?</select>', html, re.S).group(0)
  return [v for v in re.findall(r'<option value="([^"]*)"', block) if v]


def make_client():
  app = Flask(__name__)
  app.register_blueprint(vision.vision_bp)
  return app.test_client()


GOOD_MODEL_OUTPUT = {
    "is_pet": True,
    "photo_checks": [
        {"photo_index": 1, "has_pet": True, "species": "狗"},
        {"photo_index": 2, "has_pet": True, "species": "狗"},
    ],
    "same_animal": "是",
    "same_animal_evidence": "兩張都是黑色短毛狗",
    "species": {"value": "狗", "confidence": "高", "evidence": "立耳、狗的臉型"},
    "fur_color": {"value": "黑/深棕", "confidence": "高", "evidence": "全身黑色短毛"},
    "accessory": {"value": "項圈", "confidence": "高", "evidence": "脖子上有紅色項圈"},
    "accessory_color": {"value": "紅色", "confidence": "中", "evidence": "項圈為紅色"},
    "special_mark": {"value": "白襪子", "confidence": "低", "evidence": "四隻腳掌白色"},
}


class TestOptionsMatchForm(unittest.TestCase):
  """AI 能選的值必須是表單選項，否則 matcher.py 的字串比對會對不上。"""

  def test_ai_options_are_lost_form_options(self):
    self.assertEqual(vision.FUR_COLORS, form_options("fur_color"))
    self.assertEqual(vision.ACCESSORIES, form_options("accessory"))
    self.assertEqual(vision.ACCESSORY_COLORS, form_options("accessory_color"))
    # 特殊記號：AI 不選「跛腳」（照片看不出來），其餘必須都是表單選項
    self.assertTrue(set(vision.SPECIAL_MARKS) <= set(form_options("special_mark")))
    self.assertNotIn("跛腳", vision.SPECIAL_MARKS)

  def test_ai_options_are_sighting_form_options(self):
    """協尋者表單也共用這支 API，選項同樣要對得上。"""
    t = "sighting_form.html"
    self.assertEqual(vision.FUR_COLORS, form_options("fur_color", t))
    self.assertEqual(vision.ACCESSORIES, form_options("accessory", t))
    self.assertEqual(vision.ACCESSORY_COLORS, form_options("accessory_color", t))
    self.assertTrue(set(vision.SPECIAL_MARKS) <= set(form_options("special_mark", t)))
    # 協尋者表單的「看不清楚」是低信心時的預設值，必須存在
    self.assertIn("看不清楚", form_options("special_mark", t))


class TestNormalize(unittest.TestCase):

  def test_good_result(self):
    r = vision.normalize_result(GOOD_MODEL_OUTPUT)
    self.assertTrue(r["is_pet"])
    self.assertEqual(r["species"], "狗")   # 字串格式，飼主表單的物種交叉檢查用
    self.assertEqual(r["fields"]["species"]["confidence"], "高")
    self.assertEqual(r["fields"]["accessory_color"]["value"], "紅色")
    self.assertEqual(r["fields"]["special_mark"]["evidence"], "四隻腳掌白色")

  def test_confidence_passed_through(self):
    r = vision.normalize_result(GOOD_MODEL_OUTPUT)
    self.assertEqual(r["fields"]["fur_color"]["confidence"], "高")
    self.assertEqual(r["fields"]["accessory_color"]["confidence"], "中")
    self.assertEqual(r["fields"]["special_mark"]["confidence"], "低")

  def test_missing_or_invalid_confidence_becomes_none(self):
    raw = dict(
        GOOD_MODEL_OUTPUT,
        fur_color={"value": "黑/深棕", "evidence": ""},
        accessory={"value": "項圈", "confidence": "0.9", "evidence": ""},
    )
    r = vision.normalize_result(raw)
    self.assertIsNone(r["fields"]["fur_color"]["confidence"])
    self.assertIsNone(r["fields"]["accessory"]["confidence"])

  def test_unknown_becomes_none(self):
    raw = dict(GOOD_MODEL_OUTPUT, special_mark={"value": "無法判斷", "confidence": "低", "evidence": "腳被擋住"})
    r = vision.normalize_result(raw)
    self.assertIsNone(r["fields"]["special_mark"]["value"])

  def test_invalid_value_rejected(self):
    raw = dict(GOOD_MODEL_OUTPUT, fur_color={"value": "紫色", "confidence": "高", "evidence": ""})
    self.assertIsNone(vision.normalize_result(raw)["fields"]["fur_color"]["value"])

  def test_no_accessory_clears_color(self):
    raw = dict(
        GOOD_MODEL_OUTPUT,
        accessory={"value": "沒有", "confidence": "高", "evidence": "脖子沒有東西"},
        accessory_color={"value": "紅色", "confidence": "高", "evidence": ""},
    )
    r = vision.normalize_result(raw)
    self.assertEqual(r["fields"]["accessory"]["value"], "沒有")
    self.assertIsNone(r["fields"]["accessory_color"]["value"])

  def test_not_a_pet(self):
    raw = dict(GOOD_MODEL_OUTPUT, is_pet=False)
    r = vision.normalize_result(raw)
    self.assertFalse(r["is_pet"])
    self.assertTrue(all(f["value"] is None for f in r["fields"].values()))


class TestPhotoGuard(unittest.TestCase):
  """照片防呆：沒有貓狗、或不是同一隻時要擋下來。"""

  def test_all_good_passes(self):
    g = vision.normalize_result(GOOD_MODEL_OUTPUT, photo_count=2)["guard"]
    self.assertTrue(g["ok"])
    self.assertEqual(g["no_pet_photos"], [])

  def test_one_photo_without_pet(self):
    raw = dict(GOOD_MODEL_OUTPUT, photo_checks=[
        {"photo_index": 1, "has_pet": True, "species": "狗"},
        {"photo_index": 2, "has_pet": False, "species": "無法判斷"},
    ])
    g = vision.normalize_result(raw, photo_count=2)["guard"]
    self.assertFalse(g["ok"])
    self.assertEqual(g["no_pet_photos"], [2])
    self.assertIn("第 2 張", g["message"])

  def test_no_pet_at_all(self):
    raw = dict(GOOD_MODEL_OUTPUT, is_pet=False, photo_checks=[])
    r = vision.normalize_result(raw, photo_count=3)
    self.assertFalse(r["guard"]["ok"])
    self.assertEqual(r["guard"]["no_pet_photos"], [1, 2, 3])

  def test_dog_and_cat_mixed(self):
    raw = dict(GOOD_MODEL_OUTPUT, photo_checks=[
        {"photo_index": 1, "has_pet": True, "species": "狗"},
        {"photo_index": 2, "has_pet": True, "species": "貓"},
    ])
    g = vision.normalize_result(raw, photo_count=2)["guard"]
    self.assertFalse(g["ok"])
    self.assertTrue(g["mismatch"])

  def test_model_says_different_animal(self):
    raw = dict(GOOD_MODEL_OUTPUT, same_animal="否", same_animal_evidence="一隻黑狗一隻白狗")
    g = vision.normalize_result(raw, photo_count=2)["guard"]
    self.assertFalse(g["ok"])
    self.assertIn("一隻黑狗一隻白狗", g["message"])

  def test_unsure_same_animal_does_not_block(self):
    """局部特寫無法比較時填「無法判斷」，不應擋下。"""
    raw = dict(GOOD_MODEL_OUTPUT, same_animal="無法判斷")
    self.assertTrue(vision.normalize_result(raw, photo_count=2)["guard"]["ok"])

  def test_single_photo_never_mismatch(self):
    raw = dict(GOOD_MODEL_OUTPUT, same_animal="否",
               photo_checks=[{"photo_index": 1, "has_pet": True, "species": "狗"}])
    self.assertTrue(vision.normalize_result(raw, photo_count=1)["guard"]["ok"])

  def test_out_of_range_index_ignored(self):
    raw = dict(GOOD_MODEL_OUTPUT, photo_checks=[
        {"photo_index": 1, "has_pet": True, "species": "狗"},
        {"photo_index": 5, "has_pet": False, "species": "無法判斷"},
    ])
    self.assertTrue(vision.normalize_result(raw, photo_count=1)["guard"]["ok"])


class TestSchema(unittest.TestCase):

  def test_every_field_requires_confidence(self):
    for key in ("species", "fur_color", "accessory", "accessory_color", "special_mark"):
      prop = vision.RESPONSE_SCHEMA["properties"][key]
      self.assertIn("confidence", prop["required"])
      self.assertEqual(prop["properties"]["confidence"]["enum"], ["高", "中", "低"])


class TestPrepareImage(unittest.TestCase):

  def test_resized_and_jpeg(self):
    url = vision.prepare_image(make_jpeg_bytes((3000, 2000)))
    self.assertTrue(url.startswith("data:image/jpeg;base64,"))
    import base64
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    self.assertLessEqual(max(img.size), vision.MAX_SIDE_PX)

  def test_png_with_alpha(self):
    buf = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(buf, format="PNG")
    self.assertTrue(vision.prepare_image(buf.getvalue()).startswith("data:image/jpeg"))


class TestApi(unittest.TestCase):

  def setUp(self):
    self.client = make_client()

  def post(self, files):
    data = {k: (io.BytesIO(v), f"{k}.jpg") for k, v in files.items()}
    return self.client.post(
        "/api/analyze-photo", data=data, content_type="multipart/form-data"
    )

  def test_requires_photo_1(self):
    res = self.post({"photo_2": make_jpeg_bytes()})
    self.assertEqual(res.status_code, 400)

  def test_success_sends_all_photos(self):
    with mock.patch.object(vision, "call_model", return_value=GOOD_MODEL_OUTPUT) as m:
      res = self.post({"photo_1": make_jpeg_bytes(), "photo_3": make_jpeg_bytes()})
    self.assertEqual(res.status_code, 200)
    self.assertEqual(len(m.call_args[0][0]), 2)  # 兩張照片都有送進模型
    body = res.get_json()
    self.assertEqual(body["fields"]["fur_color"]["value"], "黑/深棕")
    self.assertEqual(body["fields"]["fur_color"]["confidence"], "高")

  def test_not_configured_returns_503(self):
    with mock.patch.dict(os.environ, {}, clear=True):
      vision._client = None
      res = self.post({"photo_1": make_jpeg_bytes()})
    self.assertEqual(res.status_code, 503)

  def test_model_error_returns_502(self):
    with mock.patch.object(vision, "call_model", side_effect=RuntimeError("timeout")):
      res = self.post({"photo_1": make_jpeg_bytes()})
    self.assertEqual(res.status_code, 502)

  def test_bad_image_returns_400(self):
    res = self.post({"photo_1": b"not an image"})
    self.assertEqual(res.status_code, 400)

  def test_request_shape_sent_to_azure(self):
    """確認送給 Azure 的請求有帶 structured output schema 與圖片。"""
    fake = mock.MagicMock()
    import json
    fake.chat.completions.create.return_value.choices = [
        mock.MagicMock(message=mock.MagicMock(content=json.dumps(GOOD_MODEL_OUTPUT)))
    ]
    with mock.patch.object(vision, "get_client", return_value=fake), \
         mock.patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "gpt-4.1-mini"}):
      res = self.post({"photo_1": make_jpeg_bytes()})
    self.assertEqual(res.status_code, 200)
    kwargs = fake.chat.completions.create.call_args.kwargs
    self.assertEqual(kwargs["model"], "gpt-4.1-mini")
    self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])
    user_content = kwargs["messages"][1]["content"]
    self.assertEqual(user_content[1]["type"], "image_url")


if __name__ == "__main__":
  unittest.main(verbosity=2)
