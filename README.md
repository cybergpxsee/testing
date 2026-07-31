# pullback-scan-github-template

這是把你目前的「回調買上漲的」掃描邏輯遷移到 **GitHub Actions** 自動執行的模板。

## 目前已內建的簡報設定
## 最新策略同步狀態

- 已同步目前正式任務的掃描規則
- 局部高低點判定使用 `window=3` 以降低雜訊
- 近期趨勢線使用最近 `30` 根K，仍為硬條件
- 長期趨勢線額外查看最近 `90` 根K；若同步突破/跌破，排序加 `+5` 分
- 簡報全文輸出為**繁體中文**
- 新版簡報已改成 **圖標美化 + 固定寬度對齊**，方便 Discord / 手機端閱讀
- 開頭固定列出：`數據來源`、`數據日期`
- 先按 **回調日過去 20 個交易日平均交易額** 分成兩組：
  - `過去20日平均交易額：5000萬美元以上`
  - `過去20日平均交易額：2000萬-5000萬美元`
- 每一組內再分為：`適合做多前10名`、`適合做空前10名`
- 表格固定只顯示：`股票代碼 | 做空或做多 | 回調日`
- `回調日` 不再壓縮成單一最新日期；會優先顯示每檔最近 **3 個 individually 合格窗口日**，格式如：`06-08 / 06-17 / 06-22`
- 每個窗口只取 1 個代表日：
  - 做多：窗口內**最低價**那天
  - 做空：窗口內**最高價**那天
- **每一個最終顯示出來的回調日，都必須 individually 通過雙重檢查**：
  - 方向過濾：
    - 做多：該代表日相對 5 個交易日前，需至少高出 1%
    - 做空：該代表日相對 5 個交易日前，需至少低出 1%
  - 流動性過濾：
    - 該代表日回看過去 20 個交易日平均交易額必須 **>= 2000 萬美元**
- 不合格的代表日會直接從顯示列表剔除，不是整檔股票一律刪掉
- 回調/回抽質量採加分制：碰平台位加分、碰籌碼密集區加分，**同時碰平台位 + 籌碼密集區更佳**
- 同一檔股票如果不同代表日落在不同流動性分組，會按分組拆開顯示
- 雙底／雙頂母結構有效性：兩腳之間**至少相隔 20 個交易日**；若兩腳之間**相隔 60 個交易日或以上**，分數會額外加分
- 雙底額外限制：兩個底點之間**不能再出現更低的谷底**，否則該雙底視為失效
- 雙頂額外限制：兩個頂點之間**不能再出現更高的峰頂**，否則該雙頂視為失效
- 簡報末尾固定追加：
  - `風險提示：這是AI掃描出的參考買賣點，不涉及投資建議，做多或做空都有風險`

## 安裝

## 目录结构

- `us_pattern_scan.py`：主扫描程序
- `scripts/run_scan.sh`：运行入口（先切 universe、多 worker 並發；每個 worker 自己做 stage1 + stage2）
- `scripts/render_report.py`：把 JSON 渲染成 Markdown 简报
- `scripts/update_symbol_universe.py`：每月更新美股股票池與預先排除清單（支援 `prepare / shard / aggregate`）
- `.github/workflows/pullback-scan.yml`：GitHub Actions 扫描任务
- `.github/workflows/update-universe-cache.yml`：每月更新美股股票池與預先排除清單（prepare → matrix shards → aggregate）
- `data/universe/`：本地股票池快取（`nasdaqlisted.txt`、`otherlisted.txt`、`us_symbols.csv`、`manifest.json`、`monthly_excluded_symbols.json/csv/txt`、`yahoo_bad_symbols.txt`）
- `output/`：每次运行的产物目录（本地运行时生成；不会 git commit 到仓库）

## 本地运行

```bash
python -m pip install -r requirements.txt
HERMES_SCAN_MAX_SYMBOLS=200 bash scripts/run_scan.sh
```

全量运行：

```bash
python -m pip install -r requirements.txt
bash scripts/run_scan.sh
```

可調參數（multi-worker 架構）：

```bash
HERMES_SCAN_UNIVERSE_SHARDS=18
HERMES_SCAN_WORKER_CONCURRENCY=6
HERMES_SCAN_STAGE2_SHARDS_PER_WORKER=1
HERMES_SCAN_STAGE1_PERIOD=1mo
HERMES_SCAN_STAGE1_BATCH=120
HERMES_SCAN_STAGE2_BATCH=100
HERMES_SCAN_WORKER_STAGGER=0.5
```

## 股票代号缓存设计

现在扫描任务会**优先读取本地缓存**：

- `data/universe/nasdaqlisted.txt`
- `data/universe/otherlisted.txt`
- `data/universe/manifest.json`
- `data/universe/yahoo_bad_symbols.txt`

只有当本地缓存不存在时，才会临时回退到在线抓取 Nasdaq Trader。

这样做的好处：
- 平时扫描少一次联网抓股票代号
- 运行更稳定
- 更容易排查问题
- 股票池来源固定，结果更可复现

### Yahoo-friendly universe filter（更嚴格）

主掃描在載入本地股票池後，會先套用：

- `config/exclude_symbols.txt` 手動黑名單
- `data/universe/monthly_excluded_symbols.json` 月更低流動性 / 疑似退市未上市排除池

然後再做一層更嚴格的 Yahoo-friendly 過濾，進一步排除：

- warrant / warrants
- right / rights
- unit / units
- preferred / preferred stock / trust preferred
- depositary / depository
- ETN / NextShares / notes / bonds
- `-V`、`-WI`、`-WS`、`-WD`、`-U`、`-R`、`-RT`、`-P` 等 Yahoo 常見高風險特殊後綴
- 少量已知常 timeout / 無數據 / quote not found 的 bad symbols（由 `data/universe/yahoo_bad_symbols.txt` 維護）

另外，現在 workflow / 本地腳本已改成：
- **universe 先切 shard**
- **多 worker 分散 stage1**
- **每個 worker 自己深掃**
- **worker 之間有 stagger + 下載前 sleep/retry/backoff**

## 本地先更新一次股票池與月更排除池

```bash
python scripts/update_symbol_universe.py
```

如果你想本地模擬 GitHub Actions 的 matrix 月更流程，可分三段跑：

```bash
python scripts/update_symbol_universe.py --mode prepare --workspace-dir .tmp/universe_update --shard-count 4
python scripts/update_symbol_universe.py --mode shard --workspace-dir .tmp/universe_update --shard-index 1
python scripts/update_symbol_universe.py --mode shard --workspace-dir .tmp/universe_update --shard-index 2
python scripts/update_symbol_universe.py --mode shard --workspace-dir .tmp/universe_update --shard-index 3
python scripts/update_symbol_universe.py --mode shard --workspace-dir .tmp/universe_update --shard-index 4
python scripts/update_symbol_universe.py --mode aggregate --workspace-dir .tmp/universe_update
```

如果只想檢查 workflow / commit 流程，不想在 25 天內重複重跑完整月更，可用：

```bash
python scripts/update_symbol_universe.py --skip-if-fresh-days 25
```

如需無視 freshness guard 強制全量重建：

```bash
python scripts/update_symbol_universe.py --force-refresh
```

跑完後會生成：
- `data/universe/nasdaqlisted.txt`
- `data/universe/otherlisted.txt`
- `data/universe/us_symbols.csv`
- `data/universe/manifest.json`
- `data/universe/monthly_excluded_symbols.json`
- `data/universe/monthly_excluded_symbols.csv`
- `data/universe/monthly_excluded_symbols.txt`
- `config/exclude_symbols.txt`（若原本不存在會自動建立）

月更排除規則：
- 過去 **30 天平均成交額 < 1500 萬美元**
- Yahoo 對不到 / 可能已退市 / 未上市的股票

`data/universe/yahoo_bad_symbols.txt` 不會被月更腳本覆蓋，適合你手動維護 Yahoo 常出問題的 symbol blacklist。

然后再跑扫描：

```bash
HERMES_SCAN_MAX_SYMBOLS=200 bash scripts/run_scan.sh
```

## GitHub Actions 用法

### 手动试跑
1. 把整个仓库 push 到 GitHub
2. 打开仓库 `Actions`
3. 选择 `pullback-scan`
4. 点击 `Run workflow`
5. 这版默认就是**全量扫描**（`max_symbols` 留空）
6. 如果只想先做 smoke test，可手动填 `max_symbols=200`
7. 如需調快/調穩，可覆蓋 `universe_shards`、`worker_concurrency`、`stage1_batch`、`stage2_batch`

### 定时执行
扫描工作流当前是：

```yaml
schedule:
  - cron: '0 9 * * 1-5'
```

这是 **UTC 09:00，周一到周五**。
如要换时间，改这个 cron 即可。

股票代号缓存更新工作流当前是：

```yaml
schedule:
  - cron: '0 2 1 * *'
```

這是 **每月 1 號 UTC 02:00** 自動更新一次 `data/universe/` 與 `config/exclude_symbols.txt`，並自動 commit 回倉庫。workflow 現在拆成 **prepare → matrix shards → aggregate**：先建立 universe / shard 清單，再並行下載每個 shard 的 Yahoo 資料，最後聚合結果並推回倉庫。push 前仍會先 `fetch + rebase` 再推送，以降低 remote branch 先更新造成的 non-fast-forward 失敗。

另外，這個 updater workflow 現在會在 cache/manifest 仍屬新鮮（25 天內）時自動跳過完整重建；如果你是手動點 `Run workflow` 想強制全量重跑，可把 `force_refresh` 勾成 `true`。你也可以在手動執行時調整 `shard_count` 與 `max_parallel`；一般建議從 `4 / 4` 或 `6 / 3` 開始。

## 产物

每次运行会产出：
- `pullback_scan.md`
- `pullback_scan.json`
- `pullback_scan.stderr.log`
- `liquid_symbols.json`
- `artifacts/` worker 目录与分片 JSON

GitHub Actions 会把整个 `output/` 上传成 artifact，供你下载；但 `output/` 不会被 git commit 到仓库。

## 推荐上线顺序

1. 先本地/Actions 手动跑 `max_symbols=200`
2. 看 artifact 里的 `stderr.log` 是否有正常进度
3. 看 `pullback_scan.md` 格式是否符合你要的简报样式
4. 看 `liquid_symbols.json` / `pullback_scan.json` 裡的 `run_config` 是否符合預期
5. 确认 `data/universe/` 已存在缓存文件
6. 再切换到全量运行

## 自动发送到 Discord 群组频道
你已经在 GitHub Actions secret 里配置了：

- `DISCORD_WEBHOOK_URL`

本模板已內置發送步驟：workflow 跑完後，會自動把最新的 `pullback_scan.md` 發到該 webhook 對應的 Discord 頻道。並且 workflow 最後一步已改成以 `env.DISCORD_WEBHOOK_URL` 做檢查，避免直接在 step `if:` 內判斷 `secrets.*`。

注意：
- Discord webhook 只能发到**频道**，不能发私信。
- 单条消息有 2000 字符限制；当前这版简报通常不会超。模板里仍做了截断保护。
- 如果 workflow 成功但 Discord 没收到，先检查 webhook 是否仍有效、secret 名称是否完全一致。

## 如果你还想扩展
如果你要，我可以下一步继续帮你补：
1. **GitHub 自动推送到 Telegram**
2. **每次运行后自动 commit 最新 Markdown 到仓库**
3. **Discord 发送失败时自动重试 / @某个角色**
