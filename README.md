# US Pullback Scanner

美股回調買點 / 回抽賣點掃描器 — 基於 GitHub Actions 自動化執行，支援多 Worker 並發、月更股票池更新、Discord 簡報推送。

## 核心特性

- **兩階段掃描**：Stage 1 流動性篩選 (1mo) → Stage 2 深度形態掃描 (1y)
- **多 Worker 並發**：Matrix 策略，支援 18 shards × 6 workers 並發
- **月更股票池**：自動更新 Nasdaq/OtherListed，過濾低流動性/疑似退市
- **Discord 簡報**：繁體中文、圖標美化、固定寬度對齊、風險提示
- **本地緩存優先**：Stock universe / bars 緩存，減少聯網依賴
- **Yahoo Finance 反封禁**：curl_cffi Chrome 120 指紋 + yfinance 回退 + 退避重試

---

## 掃描邏輯完整流程

### 1. Universe 準備 (prepare)

```
Nasdaq Trader + OtherListed 官方列表
    ↓
解析合併 → 去重 (Symbol)
    ↓
Yahoo-friendly 過濾：
  - 排除 warrant/right/unit/preferred/ETN/NextShares 等
  - 排除 -V/-WI/-WS/-WD/-U/-R/-RT/-P 等 Yahoo 高風險後綴
  - 排除已知 bad symbols (yahoo_bad_symbols.txt)
  - 僅保留 Common Stock / Ordinary Shares / Class A/B
    ↓
應用排除池：
  - config/exclude_symbols.txt (手動黑名單)
  - data/universe/monthly_excluded_symbols.json (月更低流動性)
    ↓
輸出：
  - data/universe/us_symbols.csv (完整股票池)
  - shards/shard_01.csv ~ shard_NN.csv (分片)
  - prepare.json (元數據，含 matrix 給 GitHub Actions)
```

### 2. Stage 1 - 流動性篩選 (1mo, daily)

```
每個 shard 並發執行：
    ↓
下載 1mo daily bars (yfinance batch 模式)
    ↓
計算過去 20 個交易日平均成交額
    ↓
分組：
  - 高流動性：≥ $50M (band_high)
  - 中流動性：$20M ~ $50M (min_avg_dollar_volume_20d)
  - 低流動性：< $20M → 直接淘汰
    ↓
輸出液性分組標記供 Stage 2 使用
```

### 3. Stage 2 - 深度形態掃描 (1y, daily)

```
對通過 Stage 1 的股票：
    ↓
下載 1y daily bars (已緩存優先)
    ↓
計算局部高低點：
  - Swing Window = 3 (左右各 3 根 K 線)
  - 局部高點：中間 K 最高，左右各 3 根皆較低
  - 局部低點：中間 K 最低，左右各 3 根皆較高
    ↓
識別雙底 / 雙頂：
  - 雙底：兩個低點間 ≥ 20 天，第二底不低於第一底，中間有明顯高點
  - 雙頂：兩個高點間 ≥ 20 天，第二頂不高於第一頂，中間有明顯低點
  - 寬間隔加分：間隔 ≥ 60 天 → +5 分
    ↓
趨勢線判定：
  - 短期 (30 根 K)：Close > SMA20 + 斜率向上/向下
  - 長期 (90 根 K)：同步突破/跌破 → 排序加分
    ↓
回調/回抽窗口識別：
  - 做多：局部高點後跌至支撐區 (趨勢線/均線/前高/平台位/籌碼密集區)
  - 做空：局部低點後漲至壓力區
    ↓
代表日選取 (每個合格窗口取 1 天)：
  - 做多：窗口內 **最低價** 那天
  - 做空：窗口內 **最高價** 那天
    ↓
每個代表日必須獨立通過雙重檢查：
  1. 方向過濾 (5 個交易日前)：
     - 做多：漲幅 ≥ 1% (direction_filter_min_pct)
     - 做空：跌幅 ≥ 1%
  2. 流動性過濾 (代表日回看 20 日)：
     - 平均成交額 ≥ $20M
  3. 確認日新鮮度：
     - 代表日距今 ≤ 90 天 (PULLBACK_MAX_CONFIRM_AGE_DAYS)
    ↓
評分系統：
  - 基礎分：形態類型 (雙底/雙頂/單邊回調)
  - 加分項：
    - 碰平台位 +3
    - 碰籌碼密集區 +3
    - 同時碰平台位 + 籌碼密集區 +5
    - 寬間隔雙底/雙頂 (≥60天) +5
    - 長期趨勢同步 +5
  - 減分項：
    - 破支撐/壓力失效 -10
    - 成交量不縮減 -3
```

### 4. 結果聚合與簡報生成

```
所有 shard 結果合併
    ↓
按總分排序 → Top 10 做多 / Top 10 做空
    ↓
按流動性分組：
  - 50M+ 做多
  - 20M-50M 做多
  - 50M+ 做空
  - 20M-50M 做空
    ↓
簡報生成 (Markdown)：
  - 標題：數據來源 + 數據日期
  - 4 個表格 (做多50M+ / 做多20M-50M / 做空50M+ / 做空20M-50M)
  - 表格欄位：編號 | 代碼 | 回調日 (最多 3 個，格式 MM-DD / MM-DD / MM-DD) | 總分
  - 風險提示固定附加
    ↓
Discord Embed 發送 (含 Thread 支援)
```

---

## 月更股票池更新 (update-universe-cache.yml)

### 三階段 Matrix 流程

```
prepare (1 job)
  ├─ 下載 Nasdaq/OtherListed
  ├─ 解析 → Yahoo-friendly 過濾
  ├─ 切分 4 shards (預設)
  ├─ 輸出 shard_01.csv ~ shard_04.csv
  └─ 輸出 prepare.json (含 matrix)

update-shards (4 parallel jobs, max-parallel=4)
  ├─ 下載 prepare workspace
  ├─ 讀取對應 shard CSV
  ├─ 下載 2mo daily bars (curl_cffi 優先)
  ├─ 計算 30日平均成交額
  ├─ 分類：
      - 下載失敗/可能退市
      - 30日均額 < $15M (小市值)
  └─ 輸出 shard_NN.json + shard_NN.csv

aggregate (1 job)
  ├─ 合併所有 shard 結果
  ├─ 生成月更排除列表
  ├─ 更新 data/universe/
  └─ Commit & Push 回倉庫
```

### 月更排除規則

| 類別 | 條件 | 來源 |
|------|------|------|
| 小市值 | 30日平均成交額 < $15M | 即時計算 |
| 下載失敗 | Yahoo 無數據 / 可能退市 / 未上市 | 下載失敗集合 |
| 手動排除 | config/exclude_symbols.txt | 人工維護 |

---

## 目錄結構

```
.
├── us_pattern_scan.py          # 主掃描邏輯 (76 KB)
├── scan_cli.py                 # CLI 入口 (替代 run_scan.sh 內聯 Python)
├── yahoo_fetcher.py            # Yahoo 下載器 (curl_cffi + yfinance 回退)
├── scripts/
│   ├── run_scan.sh             # 執行入口 (bash)
│   ├── update_symbol_universe.py  # 月更主腳本 (prepare/shard/aggregate)
│   ├── scan_cli.py             # 掃描 CLI (供 run_scan.sh 調用)
│   ├── render_discord.py       # Discord Embed 渲染
│   ├── post_to_discord.py      # Discord 發送
│   └── run_scan.sh             # 執行腳本
├── .github/workflows/
│   ├── pullback-scan.yml       # 掃描任務 (手動觸發 + 可選排程)
│   └── update-universe-cache.yml  # 月更任務 (每月 1 號 02:00 UTC)
├── config/
│   ├── exclude_symbols.txt     # 手動排除黑名單
│   └── config.yaml             # 所有閾值配置
├── config.py                   # 配置加載器 (環境變量覆蓋)
├── cache_utils.py              # Parquet Bars 緩存
├── bar_cache.py                # 下載緩存整合
├── logging_utils.py            # 標準 logging
├── render_utils.py             # 表格/簡報渲染工具
├── scan_cli.py                 # 掃描 CLI 參數解析
├── data/universe/
│   ├── nasdaqlisted.txt
│   ├── otherlisted.txt
│   ├── us_symbols.csv
│   ├── monthly_excluded_symbols.json/csv/txt
│   ├── manifest.json
│   └── yahoo_bad_symbols.txt
├── requirements.txt
└── README.md
```

---

## 配置參數 (config.yaml)

### 流動性閾值
```yaml
liquidity:
  min_avg_dollar_volume_20d: 20000000   # $20M 最小入選
  band_high: 50000000                    # $50M 高流動性分組
  smallcap_avg_dollar_volume_30d: 15000000  # $15M 小市值閾值
```

### 形態參數
```yaml
scan:
  swing_window: 3                        # 局部高低點窗口
  short_trend_lookback: 30               # 短期趨勢看回天數
  long_trend_lookback: 90                # 長期趨勢看回天數
  long_term_trend_bonus: 5               # 長期趨勢同步加分
  min_double_structure_gap: 20           # 雙底/雙頂最小間隔天數
  double_structure_wide_gap_bonus: 5     # 寬間隔 (≥60天) 加分
  double_structure_wide_gap_threshold: 60
  direction_filter_days: 5               # 方向過濾看回天數
  direction_filter_min_pct: 1.0          # 方向過濾最小漲跌幅
  week52_lookback: 252                   # 52週高低看回
  week52_proximity_bonus_max: 15         # 52週接近度最大加分
  pullback_20d_filter: true              # 啟用 20日均線過濾
  max_confirmation_age_days: 90          # 確認日最大天數
```

### 下載參數
```yaml
download:
  batch_size_stage1: 120                 # Stage 1 批次大小
  batch_size_stage2: 100                 # Stage 2 批次大小
  timeout: 45                            # 請求超時秒數
  retry_count: 3                         # 重試次數
  retry_delay_base: 0.8                  # 退避基礎延遲
```

### Universe 參數
```yaml
universe:
  smallcap_avg_dollar_volume_30d: 15000000
  cache_fresh_days: 25                   # 緩存新鮮天數
  shard_count: 18                        # 預分片數
```

---

## 環境變量覆蓋

所有 `config.yaml` 參數均可通過環境變量覆蓋，格式：`HERMES_<SECTION>_<KEY>`

```bash
# 例子
HERMES_LIQUIDITY_MIN_AVG_DOLLAR_VOL_20D=30000000
HERMES_SCAN_SWING_WINDOW=5
HERMES_DOWNLOAD_BATCH_SIZE_STAGE1=100
```

---

## 本地運行

### 依賴安裝
```bash
python -m pip install -r requirements.txt
```

### 快速測試 (200 支股票)
```bash
HERMES_SCAN_MAX_SYMBOLS=200 bash scripts/run_scan.sh
```

### 全量運行
```bash
bash scripts/run_scan.sh
```

### 參數覆蓋
```bash
HERMES_SCAN_UNIVERSE_SHARDS=18 HERMES_SCAN_WORKER_CONCURRENCY=6 HERMES_SCAN_STAGE1_BATCH=120 HERMES_SCAN_STAGE2_BATCH=100 bash scripts/run_scan.sh
```

### 月更手動觸發
```bash
# 完整流程
python scripts/update_symbol_universe.py --mode prepare --shard-count 4
python scripts/update_symbol_universe.py --mode shard --shard-index 1
python scripts/update_symbol_universe.py --mode shard --shard-index 2
python scripts/update_symbol_universe.py --mode shard --shard-index 3
python scripts/update_symbol_universe.py --mode shard --shard-index 4
python scripts/update_symbol_universe.py --mode aggregate

# 或單行 (自動分片)
python scripts/update_symbol_universe.py

# 跳過 25 天內已更新
python scripts/update_symbol_universe.py --skip-if-fresh-days 25

# 強制全量重建
python scripts/update_symbol_universe.py --force-refresh
```

---

## GitHub Actions 部署

### 掃描任務
1. Push 到 GitHub
2. Actions → `pullback-scan` → Run workflow
3. 可選參數：`max_symbols` (smoke test 填 200)

### 月更任務
- 自動：每月 1 號 02:00 UTC
- 手動：Actions → `update-universe-cache` → Run workflow
- 參數：`force_refresh=true` 強制重建

### Secrets 需設置
| Secret | 用途 |
|--------|------|
| `DISCORD_WEBHOOK_URL` | Discord 簡報推送 |

---

## 關鍵文件說明

| 文件 | 職責 |
|------|------|
| `us_pattern_scan.py` | 核心掃描邏輯：解析、下載、形態識別、評分、聚合 |
| `scan_cli.py` | CLI 參數解析、Stage 1/2 協調、輸出格式控制 |
| `yahoo_fetcher.py` | 多後端下載：curl_cffi (Chrome 120) → yfinance 回退 |
| `scripts/update_symbol_universe.py` | 月更三階段：prepare → shard → aggregate |
| `bar_cache.py` / `cache_utils.py` | Parquet 格式 Bars 緩存 (TTL 30 天) |
| `render_utils.py` / `scripts/render_discord.py` | 表格對齊、Markdown/Embed 渲染 |
| `scripts/post_to_discord.py` | Discord Webhook 發送 (Embed + Thread) |

---

## 版本歷史

| 版本 | 關鍵變更 |
|------|----------|
| v3.27 | yahoo_fetcher 併發優化 (4 workers, 移除冗餘 sleep) |
| v3.26 | 恢復 curl_cffi 下載器，修復 Yahoo 封禁導致全量失敗 |
| v3.25 | 簡化月更邏輯 (硬編碼 Symbol, 移除緩存) |
| v3.24 | 列名標準化 + utf-8-sig 處理 BOM |
| v3.23 | 強制列名檢測，修復 KeyError |
| v3.22 | 動態列名支持，移除硬編碼 |
| v3.20 | 修復 write_shard_frames UnboundLocalError |
| v3.15 | 月更 KeyError: 'Symbol' 系列修復 |
| v3.14 | 修復 GitHub Actions merge-multiple 覆蓋問題 |
| v3.13 | Aggregate 遞歸 glob 讀取 shard 結果 |
| v3.11 | Matrix 模式 Discord 報告顯示真實掃描範圍 |
| v3.10 | scan_cli.py 導入路徑修復 |
| v3.0+ | Matrix 並行架構重構 |

---

## 常見問題排查

### 掃描耗時過長
- 檢查 `yahoo_fetcher.py` 併發設置 (`max_workers=4`)
- 減少 `stage1_batch` / `stage2_batch`
- 確認 curl_cffi 正常導入 (`USE_CURL_CFFI=True`)

### 月更全部下載失敗
- 確認 `curl_cffi` 已安裝且版本 ≥ 0.15
- 檢查 GitHub Actions 網絡是否通 (需 Cloudflare WARP)
- 查看 stderr log 中的 `DOWNLOAD_RATE_LIMIT` / `DOWNLOAD_ERROR`

### KeyError: 'Symbol'
- 確保 CSV 讀取使用 `encoding='utf-8-sig'` 處理 BOM
- 所有列名操作統一用硬編碼 `'Symbol'`

### Discord 推送失敗
- 檢查 `DISCORD_WEBHOOK_URL` 是否正確
- 確認 Webhook 權限包含 `Send Messages` + `Manage Threads`
- 查看 `post_to_discord.py` 的錯誤響應體日誌

---

## 授權

MIT License — 內部量化研究使用，非投資建議。
