"""AI 照片比對的回傳解析測試（不需要 Azure、不需要資料庫）

執行：python test_ai_compare.py
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://x")
import ai_compare as AC


def item(c, s="高", r="毛色相近"):
  return {"candidate": c, "similarity": s, "reason": r}


class ParseResults(unittest.TestCase):
  def test_normal(self):
    out = AC.parse_results({"results": [item("A"), item("B", "低")]}, 2)
    self.assertEqual(out, {0: ("高", "毛色相近"), 1: ("低", "毛色相近")})

  def test_out_of_range_dropped(self):
    out = AC.parse_results({"results": [item("A"), item("C")]}, 2)
    self.assertEqual(list(out), [0])

  def test_duplicate_dropped_entirely(self):
    out = AC.parse_results({"results": [item("A"), item("A", "低"), item("B")]}, 2)
    self.assertEqual(list(out), [1])

  def test_bad_values_dropped(self):
    out = AC.parse_results({"results": [item("A", "超高"), {"candidate": "B"}, "x", item("C", "中", 5)]}, 3)
    self.assertEqual(out, {})

  def test_bad_shape(self):
    self.assertEqual(AC.parse_results({"results": "x"}, 2), {})
    self.assertEqual(AC.parse_results([], 2), {})

  def test_reason_clean_and_trim(self):
    out = AC.parse_results({"results": [item("A", "中", "候選A的耳朵與待比對照片" + "很像" * 40)]}, 1)
    self.assertTrue(out[0][1].startswith("這隻的耳朵與這隻"))
    self.assertLessEqual(len(out[0][1]), 60)


class Gate(unittest.TestCase):
  def test_enabled_only_when_1(self):
    for v, exp in (("1", True), ("0", False), ("true", False), ("", False)):
      with mock.patch.dict(os.environ, {"AI_COMPARE": v}):
        self.assertEqual(AC.enabled(), exp)
    with mock.patch.dict(os.environ, {}, clear=True):
      self.assertFalse(AC.enabled())

  def test_no_call_when_all_cached_or_not_https(self):
    conn = mock.Mock()
    conn.execute.return_value.fetchall.return_value = [{"cid": 1}]
    with mock.patch.object(AC, "call_model") as cm:
      self.assertEqual(AC.compare_and_store(conn, "sighting", 9, "https://a/x.jpg", [(1, "https://a/1.jpg")]), 0)
      self.assertEqual(AC.compare_and_store(conn, "sighting", 9, "/static/uploads/x.jpg", [(2, "https://a/2.jpg")]), 0)
      cm.assert_not_called()


if __name__ == "__main__":
  unittest.main()
