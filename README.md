# us-head-shoulder-bottom-scanner

這個 repo 名稱沿用舊包裝，方便你直接覆蓋原本的 **us head shoulder** 版本；但目前最新版核心邏輯已同步為：

**美股「雙頂 → 破底翻 → 回調買點」掃描模板**

不是硬找 textbook 頭肩底，而是把交易邏輯拆成可維護的 4 段：

1. 雙頂偵測
2. 破底翻偵測
3. 近期下降趨勢線突破確認
4. 黃金回撤回調買點偵測

同時保留你之前成熟的工程骨架：
- 每月自動更新美股股票池（4 worker：prepare → update-shards → aggregate）
- 預先排除低流動性 / Yahoo 易報錯 / 疑似退市股票
- 主掃描前先套用 `config/exclude_symbols.txt` + `monthly_excluded_symbols.json`
- Stage1 流動性預篩 → Stage2 深掃
- GitHub Actions artifact + Discord webhook 推送
- 簡報分兩個流動性板塊：
  - 過去20日平均交易額：5000萬美元以上
  - 過去20日平均交易額：2000萬-5000萬美元

## 掃描核心定義

### Stage 1：先找嚴格雙頂 / 雙底母結構
- 先有兩個明顯雙頂（做多）或雙底（做空）
- 兩頂 / 兩底相隔至少 **20 個交易日**
- 雙頂兩頂價格差 **2% 以內**；雙底兩底價格差 **2% 以內**
- 若兩頂 / 兩底相隔 **60 個交易日以上**，作加分項
- **雙頂之間不可再出現高過兩個頂的價格**；否則不算雙頂
- **雙底之間不可再出現低過兩個底的價格**；否則不算雙底
- **第二個頂之後若曾跌破中間谷底，整個雙頂結構直接失效**
- **第二個底之後若曾升破中間峰頂，整個雙底結構直接失效**

### Stage 2：再找回調買點
- 母結構未失效前，不追第一腳
- 需先確認打破近期下降趨勢線（做多）或跌破近期上升趨勢線（做空）
- 之後等待價格回踩上升段的 **0.5~0.618** 黃金回撤區
- 若跌穿 **0.618**，但 **5 日內收回 0.618 上方**，也算合格
- 回調位左方優先有 **籌碼密集區**；沒有的話，也至少要有平台區 / 前高 / 支撐線其一
- 越多共振，分數越高；其中 **籌碼密集區權重最高**
- 到達回調區後，若日線重新轉強 / 轉弱，或當日 **30 分鐘圖** 出現反轉結構，也視作有效確認；排序改為**先看回調日，再看 30m**
- 若做多主路徑未形成標準「破底翻→突破確認」，會額外補一條對稱 fallback：**雙頂後先走出一段急跌，再回踩主升段 0.5-0.618 後重新轉強**，標記為 `雙頂→右側回調買點`
- 做空則保留原本 fallback：**雙底後先走出一段急升，再回抽大跌段 0.5-0.618 後重新轉弱**，標記為 `雙底→右肩回調賣點`

## 榜單顯示

- 總標題改為：`美股右肩打頂底`
- 榜單同時保留：
  - `回調買`：`代碼 / 回調日 / 30M反應`
  - `回調賣`：`代碼 / 回調日 / 30M反應`
- 顯示順序改為：**先兩個回調買榜，再兩個回調賣榜**
- `回調日` 若同一標的近期有多個候選窗口，**最多只顯示最近兩次**
- **不再顯示**：止損、目標1、目標2
- `30M反應` 欄位只顯示：
  - `有`
  - `無`

## 風控與目標

### 止損優先順序
1. **籌碼密集區下沿下方**
2. 平台 / 前高支撐下方
3. 若都沒有，退回 **5% 止損**

### 目標價
- **目標1**：雙頂壓力區
- **目標2**：1.618 趨勢延伸位

## 目前內建規則
- 局部高低點判定使用 `window=3`
- Stage1 正式流動性規則：**過去20個交易日平均成交額 >= 2000萬美元**
- 月更預排除池規則：
  - **過去30個交易日平均成交額 < 1500萬美元**
  - Yahoo 對不到 / 疑似退市 / 未上市代號
- 近期下降趨勢線 lookback：`30`
## 補充條件
- 若雙頂 / 雙底相隔 **60 個交易日以上**，視作額外加分
- 回調後的重新轉強 / 轉弱，不一定要日線收盤突破 / 跌破前一日高低；**同日 30m 反轉** 也可視作有效確認

## 规则文档
- `docs/double-top-breakdown-reclaim-spec.md`：完整規則文檔，適合給程序員/AI直接實作或繼續迭代

## 目录结构
- `us_pattern_scan.py`：主扫描程序（已同步支援回調買 / 回調賣，且 30m 反應為 long / short 對稱邏輯）
- `scripts/run_scan.sh`：运行入口（先切 universe、多 worker 並發；每個 worker 自己做 stage1 + stage2）
- `scripts/render_report.py`：把 JSON 渲染成 Markdown 简报
- `scripts/update_symbol_universe.py`：每月更新美股股票池與預先排除清單
- `.github/workflows/pullback-scan.yml`：GitHub Actions 掃描任務
- `.github/workflows/update-universe-cache.yml`：每月更新股票池與預排除清單
- `data/universe/`：本地股票池快取與月更排除池
- `config/exclude_symbols.txt`：手動黑名單
- `output/`：每次运行产物目录

## 本地运行

先安装依赖：

```bash
python -m pip install -r requirements.txt
```

先更新一次股票池與月更排除池：

```bash
python scripts/update_symbol_universe.py --shard-count 4
```

Smoke test：

```bash
HERMES_SCAN_MAX_SYMBOLS=200 bash scripts/run_scan.sh
```

全量运行：

```bash
bash scripts/run_scan.sh
```

## 可調參數

```bash
HERMES_SCAN_UNIVERSE_SHARDS=18
HERMES_SCAN_WORKER_CONCURRENCY=6
HERMES_SCAN_STAGE2_SHARDS_PER_WORKER=1
HERMES_SCAN_STAGE1_PERIOD=1mo
HERMES_SCAN_STAGE1_BATCH=120
HERMES_SCAN_STAGE2_BATCH=100
HERMES_SCAN_WORKER_STAGGER=0.5
```

## 股票池 / 預排除池設計

主掃描會優先讀取：
- `data/universe/nasdaqlisted.txt`
- `data/universe/otherlisted.txt`
- `data/universe/us_symbols.csv`
- `data/universe/monthly_excluded_symbols.json`
- `config/exclude_symbols.txt`

然後先做這些事：
1. 載入 `us_symbols.csv`
2. 套用手動黑名單 `config/exclude_symbols.txt`
3. 套用月更生成排除池 `monthly_excluded_symbols.json`
4. 才開始 shard + Stage1 流動性篩選

這樣可以先排除一大批：
- 小流動性股票
- Yahoo 常報錯 / 無數據股票
- 疑似退市 / 未上市代號

執行時會印出類似：

```text
Prepared universe: 1234 symbols (pre-filter 8765, manual excludes 10, generated excludes 3121)
```

## GitHub Actions 用法

### 手动试跑
1. 把整个仓库 push 到 GitHub
2. 打开仓库 `Actions`
3. 选择 `us-head-shoulder-bottom-scan`
4. 点击 `Run workflow`
5. 默认全量扫描；只想 smoke test 可填 `max_symbols=200`

### 定时执行
掃描 workflow 目前保留工作日定時執行；月更股票池 workflow 目前保留每月更新一次，並改成 4 worker 的 prepare → update-shards → aggregate。

## 产物
每次运行会产出：
- `head_shoulder_scan.md`
- `head_shoulder_scan.json`
- `head_shoulder_scan.stderr.log`
- `liquid_symbols.json`
- `artifacts/` worker 目录与分片 JSON

> 檔名先沿用舊版 head_shoulder 命名，方便你無痛替換既有 workflow / webhook / 打包流程。

## Discord 推送
如果 GitHub Actions secret 已配置：
- `DISCORD_WEBHOOK_URL`

workflow 跑完後會自動把最新 `head_shoulder_scan.md` 發到對應 Discord webhook 頻道。

## 当前交付说明
這版是 **在最新 us head shoulder 包上同步的新規則版本**：
- 已保留原本月更股票池 + 預篩工程流
- 已改成美股版 **雙頂→破底翻→回調買點**
- 已同步報表標題、欄位、README 與規格文檔
- 已同步為 long / short 雙榜板塊（回調買 + 回調賣）
