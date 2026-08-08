# momentum-rank-scanner

美股動量排名週報掃描器 — 基於相對 SPY 的超額報酬百分位排名 (20R/60R/120R/Rank)，每週六自動產出三類別簡報並推送至 Discord。

## 核心邏輯

### 動量排名計算
- **20R/60R/120R**：標的在 20/60/120 天窗口的報酬率減去 SPY 同期報酬率，再在全市場做百分位排名 (1-99，越大越強)
- **Rank 綜合排名**：`Rank = 0.2 × 20R + 0.4 × 60R + 0.4 × 120R`
- 數據來源：Nasdaq Trader 股票池 + Yahoo Finance (yfinance) 日線 OHLCV
- 流動性門檻：回調日過去 20 個交易日平均交易額 ≥ $20M

### 三類別篩選條件
| 類別 | 條件 | 圖示 | 含義 |
|------|------|------|------|
| **類別 1** | 20R∈[75,89] **且** 60R∈[75,89] **且** 120R<80 | 🟡 | 短中期動量強但長期動量落後 — 可能是反轉/追趨機會 |
| **類別 2** | 20R≥90 **且** 60R≥90 **且** 120R<80 | 🟢 | 短中期極強但長期落後 — 動量加速但長期基礎較弱 |
| **類別 3** | Rank ≥ 90 | 🔵 | 綜合動量極強 — 全時段表現優異 |

### 輸出格式
- **Markdown 簡報**：含標題、掃描資訊、三類別表格（代碼 | 20R | 60R | 120R | Rank，按 Rank 降序）
- **Discord Embed**：三個 field 分別對應三類別，每類顯示前 20 檔
- **JSON 完整資料**：含完整排名、分類結果、掃描統計

## 目錄結構

```
momentum-rank-scanner/
├── .github/workflows/
│   └── momentum-rank-scan.yml      # GitHub Actions 工作流 (每週六 06:00 UTC)
├── config/
│   └── exclude_symbols.txt         # 手動排除代碼清單
├── scripts/
│   ├── momentum_rank_scanner.py    # 核心掃描邏輯
│   ├── render_momentum_report.py   # 報告渲染器 (Markdown + Discord JSON)
│   ├── run_momentum_scan.sh        # 執行入口腳本
│   ├── post_to_discord.py          # Discord Webhook 發送
│   └── render_discord.py           # (舊版相容)
├── requirements.txt                # yfinance, pandas, numpy
└── README.md
```

## 本地運行

### 安裝依賴
```bash
python -m pip install -r requirements.txt
```

### 快速測試 (200 檔標的)
```bash
MOMENTUM_SCAN_MAX_SYMBOLS=200 bash scripts/run_momentum_scan.sh
```

### 完整掃描
```bash
bash scripts/run_momentum_scan.sh
```

### 可調參數 (環境變數)
```bash
# 掃描參數
MOMENTUM_SCAN_MAX_SYMBOLS=0          # 0 = 全量，>0 限制數量 (測試用)
MOMENTUM_SCAN_STAGE1_PERIOD=1mo      # Stage1 流動性看回期
MOMENTUM_SCAN_STAGE1_BATCH=120       # Stage1 下載批次大小
MOMENTUM_SCAN_STAGE2_BATCH=100       # Stage2 下載批次大小
MOMENTUM_SCAN_STAGE2_PERIOD=1y       # Stage2 深度數據看回期
MOMENTUM_SCAN_SHARDS=4               # 並行分片數

# 執行範例
MOMENTUM_SCAN_MAX_SYMBOLS=200 \
MOMENTUM_SCAN_STAGE1_BATCH=80 \
MOMENTUM_SCAN_STAGE2_BATCH=80 \
MOMENTUM_SCAN_SHARDS=4 \
bash scripts/run_momentum_scan.sh
```

### 輸出產物
執行後會在 `output/<timestamp>/` 產生：
```
output/20260808T052320Z/
├── artifacts/
│   └── momentum_rank_output.json   # 完整 JSON (含錯誤時也會產出)
├── momentum_rank_output.json       # 複製到最終目錄
├── momentum_rank_report.md         # Markdown 簡報
├── momentum_discord_embed.json     # Discord Embed payload
└── momentum_scan.log               # 詳細執行日誌
```

## GitHub Actions 部署

### 1. 推送到 GitHub
```bash
git init
git add .
git commit -m "Initial commit: momentum rank scanner"
git remote add origin https://github.com/<your-org>/<your-repo>.git
git push -u origin main
```

### 2. 設定 Secrets
在 Repository → Settings → Secrets and variables → Actions 新增：
- `DISCORD_WEBHOOK_URL`：Discord Webhook URL (用於發送週報)
- (可選) `PAT_FOR_DATA_SHARE`：若使用私有共享倉庫存放 universe cache

### 3. 啟用 Workflow
- 進入 Actions 頁面，選擇 `momentum-rank-scan`，啟用工作流
- 預設排程：**每週六 06:00 UTC** (台灣時間週六 14:00)

### 4. 手動觸發測試
1. Actions → `momentum-rank-scan` → `Run workflow`
2. 可調整參數：
   - `max_symbols`：留空 = 全量，填 `200` = 測試模式
   - `stage1_period` / `stage2_period`：調整看回期
   - `shards`：並行分片數 (預設 4)
3. 點擊 `Run workflow`

## 排程設定

```yaml
# .github/workflows/momentum-rank-scan.yml
schedule:
  - cron: '0 6 * * 6'   # 每週六 06:00 UTC
```

如需修改時間，編輯 workflow 中的 `cron` 表達式。

## 手動排除代碼

編輯 `config/exclude_symbols.txt`，每行一個代碼 (支援註解 `#`)：
```text
# 手動排除範例
SYMBOL1
SYMBOL2
```

## Discord 簡報範例

```
📊 美股動量排名週報
📅 掃描日期：2026-08-08
🔢 掃描標的數：3245
✅ 有效數據：2847
📈 數據來源：Nasdaq Trader + Yahoo Finance (yfinance)

🟡 類別 1：20R&60R在 75-89，但 120R < 80 （共 12 檔）
| 代碼 | 20R | 60R | 120R | Rank |
|------|-----|-----|------|------|
| ELVN | 95  | 94  | 78   | 91.4 |
| DUM  | 98  | 95  | 72   | 89.8 |

🟢 類別 2：20R&60R ≥ 90，但 120R < 80 （共 5 檔）
| 代碼 | 20R | 60R | 120R | Rank |
|------|-----|-----|------|------|
| HPE  | 99  | 98  | 75   | 94.2 |

🔵 類別 3：總 Rank ≥ 90 （共 28 檔）
| 代碼 | 20R | 60R | 120R | Rank |
|------|-----|-----|------|------|
| ELVN | 95  | 94  | 98   | 95.8 |
| HPE  | 75  | 98  | 99   | 94.0 |

⚠️ 風險提示：此為動量排名篩選結果，非買賣建議。排名基於相對 SPY 的超額報酬百分位，數值越大代表相對動量越強。請自行判斷風險。
```

## 錯誤處理與除錯

### 常見問題
1. **SPY data not available**：SPY 下載失敗，檢查 `momentum_scan.log` 中的 Stage 2 下載情況
2. **Yahoo Finance 限流**：log 中出現 `YFRateLimitError`，可調大 batch 間隔或減小 batch size
3. **無有效排名**：`valid_count=0`，通常是 Stage 1 流動性門檻過高或 Stage 2 數據長度不足

### 除錯步驟
```bash
# 1. 查看詳細日誌
cat output/<timestamp>/momentum_scan.log

# 2. 檢查 JSON 輸出
cat output/<timestamp>/momentum_rank_output.json

# 3. 本地測試小量標的
MOMENTUM_SCAN_MAX_SYMBOLS=50 MOMENTUM_SCAN_STAGE1_BATCH=20 MOMENTUM_SCAN_STAGE2_BATCH=20 bash scripts/run_momentum_scan.sh
```

## 進階：共享 Universe Cache (可選)

為避免每次掃描都重新下載 Nasdaq Trader 股票池，可建立共享倉庫：

1. 建立獨立 repo (如 `your-org/data-share`)
2. 結構：
   ```
   data-share/
   └── data/universe/
       ├── nasdaqlisted.txt
       ├── otherlisted.txt
       ├── us_symbols.csv
       ├── monthly_excluded_symbols.json
       └── yahoo_bad_symbols.txt
   ```
3. 在 workflow 中已包含 checkout 步驟，若為私有 repo 需設定 `PAT_FOR_DATA_SHARE` secret

## 與 Pullback Scanner 的差異

| 特性 | Momentum Rank Scanner | Pullback Scanner |
|------|----------------------|------------------|
| **策略類型** | 動量排名 (相對 SPY) | 回調形態 (雙底/雙頂 + 0.618) |
| **輸出頻率** | 每週六 | 每日 (工作日) |
| **核心指標** | 20R/60R/120R/Rank | 破底翻/假突破 + 形態質量分 |
| **適用場景** | 動量選股、板塊輪動、趨勢跟隨 | 精確進場點位、二次進場、VCP 形態 |
| **簡報結構** | 三類別排名表格 | 流動性分組 + 回調日時間軸 |

兩者可並行部署，互補使用。

## 授權

MIT License