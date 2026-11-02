# fetch-history 抓数据套件 · 使用说明

## 这个包是干什么的
把 6 张截图对应的**完整日线历史（含成交量）**从 Yahoo 抓下来，存成 CSV/pkl，
用来做「买点圈」的点时回放和参数标定。

为什么要绕一圈 GitHub：本机实测 `query1.finance.yahoo.com:443` 网络通，但
yfinance 返回 `YFRateLimitError`、直连 chart API 返回 **403**，本地直抓抓不到。
GitHub Actions（美国 IP）没有这个问题。

---

## 你要做的 3 步

### 1) 上传到 GitHub 仓库
把下面 5 个文件放到仓库里**同名路径**（`fetch-kit/` 下的目录结构已经和仓库一致）：

    scripts/fetch_history.py
    .github/workflows/fetch-history.yml
    examples/labels.csv
    examples/parse_yahoo_html.py
    examples/replay_v2.py

（用 GitHub 网页上传也行：进入仓库 → Add file → Upload files，注意 `.github/` 这种带点的目录
 网页上传有时会失败，失败就把 `.github/workflows/fetch-history.yml` 单独用
 「Create new file」粘贴内容创建。）

### 2) 跑工作流
GitHub 仓库 → **Actions** 标签 → 左侧选 **fetch-history** → 右上 **Run workflow**
→ **输入框全部留空**（留空=抓内置的 9 个代码，日期区间已按你的截图设好）→ Create。

跑完后在 Summary 页面最下方 **Artifacts** 里下载
`history-<数字>` 这个 zip。

### 3) 解压到本地仓库
解压后把里面的 `data/history/` 整个文件夹放到：

    D:\AI working place\Strata working place\US-pullback-scanner\US-pullback-scanner-main\data\history\

本地验证（不联网）：

    python examples\replay_v2.py

---

## 顺手把圈日期改准（很重要）
`examples/labels.csv` 里的日期是我从截图读像素估的，误差 ±5 个交易日。
请改成你实际下单的日期，一行一个：

    symbol,date,side,note
    TSLA,2024-04-08,long,圈1 ...

TSLA 的两个圈我已经拆成两行。BTC-USD 和棉花的日期我也估了，如果记不准就删掉那行。

---

## 抓的是哪些区间（已对齐你的截图横轴）

| 代码 | 区间 | 对应截图 |
|---|---|---|
| HOOD | 2025-08-01 → 2026-07-01 | 圈约 2026-05 |
| TSLA | 2023-09-01 → 2025-01-31 | 圈 2024-04 / 2024-05 |
| SIRI | 2024-09-01 → 2026-08-01 | 圈约 2026-02 |
| MRNA | 2024-09-01 → 2026-02-01 | 圈约 2025-12 |
| P | 2025-06-01 → 2026-08-01 | 圈约 2026-06 |
| BTC-USD | 2022-02-01 → 2023-05-01 | 圈约 2023-03 |
| CTK28.NYB | 2023-01-01 → 2024-07-01 | 圈约 2024-03 |
| CT28.NYB / CT.NYB | 2023-01-01 → 2024-07-01 | 棉花主连对照（CTK28 存档成交量为 0） |

价格口径：`auto_adjust=True`（复权），接近 TradingView 图上的数值。
生产扫描的抓数逻辑（`auto_adjust=False`）没有改动。

## 跑失败了怎么办
- Summary 里的 `manifest.csv` 会列出每个代码抓到多少行 / FAILED。
- 某个代码 FAILED（Yahoo 代码写法不对，尤其棉花）：在 Run workflow 的 `symbols` 框里填
  你想试的写法单独重跑，例如 `CT28.NYB,CT.NYB,US.COT`。
- 全部 FAILED：说明 Actions 也被 Yahoo 限流了，告诉我，我把抓取改成走 stooq/Polygon 等备用源。
