"""依 lost_reports.csv / sightings.csv 到 Pexels 搜尋並下載照片到對應資料夾。

用法（在 demo_seed/ 目錄、你自己的電腦上執行）：
  1. 到 https://www.pexels.com/api/ 免費申請 API Key
  2. export PEXELS_API_KEY=你的key        (Windows: set PEXELS_API_KEY=你的key)
  3. pip install requests
  4. python download_photos.py             # 下載全部（已存在的檔案會略過）
     python download_photos.py --reuse-pair-photos   # 高分配對的通報直接沿用公告照片（看起來是「同一隻」）
     python download_photos.py --only L01 S01        # 只下載指定 key

下載後請務必逐張打開檢查：stock 照片不保證毛色、品種、配件與 CSV 完全一致，
不符的就自己換掉（檔名維持 1.jpg / 2.jpg）。下載來源記錄在 photos/SOURCES.csv。
"""
import argparse, csv, os, shutil, sys, time
import requests

API = "https://api.pexels.com/v1/search"
KEY = os.environ.get("PEXELS_API_KEY")

SPECIES = {"狗": "dog", "貓": "cat"}
COLOR = {"黑/深棕": "black", "白/米白/淺黃": "white", "黃/棕/褐色": "brown tan",
         "三花/雙色": "tricolor", "虎斑/花紋": "tabby"}
LENGTH = {"短毛": "short hair", "長毛": "long hair", "捲毛": "curly"}
ACC = {"項圈": "collar", "胸背帶": "harness", "衣服": "wearing clothes", "名牌": "collar tag"}
# 品種提示（讓搜尋更貼近台灣常見的樣子）
BREED = {
    "L02": "mixed breed street", "L03": "shiba inu", "L04": "poodle", "L05": "golden retriever",
    "L06": "labrador", "L07": "beagle", "L08": "corgi", "L09": "maltese", "L10": "mixed breed",
    "L11": "domestic shorthair", "L12": "orange tabby", "L13": "persian", "L14": "domestic shorthair",
    "L15": "calico",
}
# 與公告「同一隻」的高分通報（--reuse-pair-photos 時直接複製公告的 1.jpg）
PAIRS = {"S01": "L01", "S03": "L02", "S05": "L03", "S06": "L04", "S08": "L06",
         "S09": "L07", "S12": "L09", "S22": "L12", "S25": "L14", "S27": "L15"}

def query(row):
    parts = [COLOR.get(row["fur_color"], ""), LENGTH.get(row["fur_length"], ""),
             BREED.get(row["key"], ""), SPECIES[row["species"]]]
    if row["accessory"] in ACC:
        parts.append(ACC[row["accessory"]])
    q = " ".join(p for p in parts if p)
    return q

def read(fn):
    with open(fn, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def search(q, n=15):
    r = requests.get(API, params={"query": q, "per_page": n}, headers={"Authorization": KEY}, timeout=30)
    r.raise_for_status()
    return r.json().get("photos", [])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-pair-photos", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    if not KEY:
        sys.exit("請先設定環境變數 PEXELS_API_KEY")
    used, log = set(), []
    jobs = [("lost", r) for r in read("lost_reports.csv")] + [("sightings", r) for r in read("sightings.csv")]
    for kind, row in jobs:
        key = row["key"]
        if a.only and key not in a.only:
            continue
        files = [x for x in row["photo_files"].split(";") if x]
        folder = f"photos/{kind}/{key}"
        os.makedirs(folder, exist_ok=True)
        if a.reuse_pair_photos and kind == "sightings" and key in PAIRS:
            src = f"photos/lost/{PAIRS[key]}/1.jpg"
            if os.path.exists(src):
                shutil.copy(src, f"{folder}/{files[0]}")
                log.append([key, files[0], "copied from " + src, "", ""])
                continue
        q = query(row)
        try:
            hits = search(q)
        except Exception as e:
            print(f"[{key}] 搜尋失敗：{e}")
            continue
        hits = [h for h in hits if h["id"] not in used]
        for i, fn in enumerate(files):
            dest = f"{folder}/{fn}"
            if os.path.exists(dest):
                continue
            if i >= len(hits):
                print(f"[{key}] 找不到足夠照片：{q}")
                break
            h = hits[i]
            used.add(h["id"])
            img = requests.get(h["src"]["large"], timeout=60)
            img.raise_for_status()
            open(dest, "wb").write(img.content)
            log.append([key, fn, h["url"], h.get("photographer", ""), q])
            print(f"[{key}] {fn} ← {h['url']}")
            time.sleep(0.4)
    if log:
        new = not os.path.exists("photos/SOURCES.csv")
        with open("photos/SOURCES.csv", "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["key", "file", "source_url", "photographer", "query"])
            w.writerows(log)

if __name__ == "__main__":
    main()
