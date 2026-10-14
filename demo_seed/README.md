# demo_seed：Demo 資料匯入包

```
demo_seed/
├── README.md              ← 本說明
├── options_reference.txt  ← 欄位可填的值（照著填）
├── lost_reports.csv       ← 走失公告（飼主端），一列一筆
├── sightings.csv          ← 目擊通報（協尋者端），一列一筆
├── photos/
│   ├── lost/<key>/        ← 例如 photos/lost/L01/1.jpg, 2.jpg
│   └── sightings/<key>/   ← 例如 photos/sightings/S01/1.jpg
└── output/                ← 匯入後產生（不要手動放）：tokens.csv（每筆的 own_/trk_ 連結）、匯入記錄
```

## 規則
- `key` 是你自己取的代號（L01…、S01…），用來對應照片資料夾與預期結果。
- `lost_reports.csv` 先匯入、`sightings.csv` 後匯入（通報送出時才會和既有公告比對）。
- `sightings.csv` 的 `expect_match_lost_key`、`scenario` 只給你自己看，匯入時忽略；用來確認 Demo 時每一種情境都有。
- 建議配比（共約 40 筆）：公告 ~12、通報 ~28，其中包含強相關（≥60）、中度（40–59）、不相關、同物種但外觀差很多、「看不清楚」「其他」文字、已排除等情境。
- 每筆資料用固定標記以便一鍵清除（例如 email 用 @demo.local，或 notes 加前綴 [DEMO]）。
- 匯入走 API（/api/upload → /api/lost_reports → /api/sightings），並預留「不推播」開關。
