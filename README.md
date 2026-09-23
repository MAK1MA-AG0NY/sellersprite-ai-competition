# sellersprite-ai-competition

> 卖家精灵（SellerSprite）竞品深挖工具链 —— 把原本要手工点五遍的流程，串成一条本地自动化链路。

基于 **FastAPI + Playwright** 的本地 Web 控制台 + CLI 双形态工具。从「AI 找竞品」拉取竞品清单，
到「竞品详情批量查询」写入本地 SQLite，再到「广告洞察关键词历史」解析与漏斗筛选，
最后统一产出带条件格式的美化 Excel。

只在本机 `127.0.0.1` 运行，不对外暴露端口。

---

## 一、核心能力（控制台五步链路）

```
① AI找竞品提交    选站点 / 竞品数量 → 填 ASIN → 自动等待报告生成
② 历史任务         API + DOM 双通道拉取历史任务列表
③ 提取竞品 ASIN    从任务 content JSON 中解析出竞品 ASIN 集合
④ 批量查询竞品详情  会员账号查询，结果结构化写入本地 SQLite
⑤ 生成美化 Excel   总览 / 销量趋势矩阵 / 原始数据 / 说明 四个 sheet
```

除主链路外还有以下扩展模块：

| 模块 | 说明 |
|---|---|
| **广告洞察** | 解析 `KeywordHistory-*.xlsx`（ABA 排名、月搜索量、购买率、PPC 价格等），并提供「搜索量阈值 + 打分 + 分层标签」的漏斗筛选 |
| **关键词挖掘** | kw-miner 接口探测与批量关键词分析 |
| **本地快照库** | 每次查询按批次留档，`latest(asins)` 可取每个 ASIN 最近一次快照，支持跨批次对比 |
| **监控名单（watchlist）** | 维护关注 ASIN 清单，支持免登录的 Amazon 端 BSR 抓取，以及 API / 探测两条通道刷新卖家精灵数据 |
| **类目 BSR 榜** | 按品类（便携电站 / 太阳能板 / 户外三防手机）拉取类目 BSR Top-N 榜单并分配入库；榜单接口配置由 `bsr/probe` 在真实浏览器中捕获后缓存复用 |
| **每日例行（routine）** | 按星期编排的自动化流水线：监控名单快照 + 官方指标补齐 + CL 找竞品（周一到周五各配一个种子 ASIN，周五用于新品类探边），一键 `routine/run` 跑完整链路 |
| **HTML 报告** | 自包含单文件决策视图：统计卡片 + 销量趋势图 + 可排序 / 筛选 / 缩略图表格（xlsx 仍负责原始字段与后续计算） |
| **AI 分析包** | 预聚合的紧凑 Markdown（产品层 Top N + 关键词层 Top M），可直接粘贴给外部 AI 做分析 |
| **风格报告** | 输出 `*_style_*.xlsx`，用于对标竞品 listing 风格 |

---

## 二、目录结构

```
sellersprite-ai-competition/
├── web_server.py               # Web 后端（FastAPI，五步全链路 + 扩展模块路由）
├── web_index.html              # Web 前端（本地单文件，无外部 CDN 依赖）
├── sellersprite_deep_dive.py   # 接口逻辑库：历史报告 / ASIN 提取 / 竞品查询 / 导出
├── sellersprite_ai_report.py   # CLI 版 AI 找竞品（交互式，Web 版的来源模块）
├── ads_insights.py             # 广告洞察：xlsx 解析 / 漏斗筛选 / UI 自动化取数
├── local_db.py                 # 本地 SQLite 快照库
├── make_report.py              # 美化 Excel 生成器
├── lookup_api.json             # 已探测的竞品查询接口配置（存在则跳过接口探测）
├── requirements.txt            # Python 依赖（含精确版本）
├── start_web.bat               # 一键启动 Web 控制台
├── run_sellersprite.ps1        # CLI 一键启动（也支持 -LoginOnly 仅刷新登录态）
├── setup_env.ps1               # 环境校验 / 重建（venv + 浏览器内核）
├── download_chromium.py        # 备用：从 playwright CDN 直接拉取 Chromium
└── docs/
    ├── 竞品广告打法备忘录.md
    └── 关键词挖掘-关键词反查-出单词反查.md
```

### 模块依赖关系

```
web_server.py  ──┬── sellersprite_deep_dive.py   （历史 / 查询 / 导出 / BSR 抓取）
                 ├── sellersprite_ai_report.py   （页面下拉框操作：detect_selects / set_select）
                 ├── local_db.py                 （SQLite 快照 + 广告/关键词库）
                 ├── make_report.py              （Excel / HTML / AI包 生成）
                 └── ads_insights.py             （广告洞察）
```

### 运行时数据目录（**不入库**）

程序运行时会在项目内生成以下目录与文件，它们由 `.gitignore` 整体拦截，clone 后会自动重建：

| 路径 | 内容 |
|---|---|
| `deep_results/` | 运行产物总目录：`deepdive.db`、导出的 xlsx / html / md 报告、`watchlist.json`（监控名单）、`routine.json`（例行编排配置）、`bsr_api.json`（BSR 接口配置缓存）、`asins_*.json` |
| `lookup_debug/` | 竞品查询与 BSR 探测的原始响应留档 |
| `ads_debug/` | 广告洞察流程抓包 |
| `states/` | 分账号登录态快照 |
| `*.db` | 本地 SQLite 数据库（可能有数百 MB） |

> 这些目录里的 `watchlist.json` / `routine.json` 属于**个人业务配置**，需要迁移时请手动复制，不要提交到仓库。

---

## 三、快速开始

### 前置条件

- Windows 10/11
- Python 3.10+（原开发环境为 3.14）
- [uv](https://docs.astral.sh/uv/)（用于重建 venv；已有可用 venv 时可跳过）

### 步骤

1. **克隆到本地**（建议路径不含中文和空格）

   ```powershell
   git clone <your-repo-url> sellersprite-ai-competition
   cd sellersprite-ai-competition
   ```

2. **校验 / 重建环境**

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\setup_env.ps1
   ```

   脚本会检查 `.pw-browsers\` 内核是否到位（缺则用 playwright 官方源下载），
   以及 `.venv\` 是否可用（损坏或换机器时用 uv 自动重建并安装 `requirements.txt`）。

3. **启动 Web 控制台**

   双击 `start_web.bat`，浏览器会自动打开 <http://127.0.0.1:8765>。

4. **登录账号**

   在页面顶部的账号卡片里分别登录：

   - **主账号** —— 跑「AI 找竞品」（免费号 6 次/天配额）
   - **会员账号** —— 跑「竞品查询」与「广告洞察」

   登录态会保存到本地 `storage_state*.json`，之后复用，无需重复登录。

### CLI 用法

```powershell
# 交互式 AI 找竞品
.\run_sellersprite.ps1 B07Z82895W B0XXXXXXXX

# 仅刷新登录态（会删除旧登录态并带界面打开浏览器）
.\run_sellersprite.ps1 -LoginOnly
```

### 环境变量

| 变量 | 说明 |
|---|---|
| `PLAYWRIGHT_BROWSERS_PATH` | 指向项目内 `.pw-browsers\`。`start_web.bat` 已自动设置；直接 `python web_server.py` 时需自行设置 |
| `HEADLESS` | 设为 `0` 可开启有界面观察模式（排查问题时很有用） |
| `PYTHONIOENCODING` | 建议设为 `utf-8`，避免 Windows 控制台中文乱码 |

---

## 四、⚠️ 安全须知（重要）

- **`storage_state.json` / `storage_state_member.json` 是账号登录态，内含完整 cookie**
  —— 等同于账号密码，**已被 `.gitignore` 排除，请勿提交、勿外传**。换机器后重新登录即可。
- 本仓库**不包含**任何业务数据、抓包 dump、运行时产物与虚拟环境；
  `.gitignore` 已对 `deep_results/`、`results/`、`*.xlsx`、`*.db`、各类 debug 目录做兜底拦截。
- 首次运行必须人工在弹出的浏览器里登录，工具本身不存储账号密码。
- 「AI 找竞品」消耗每日配额（免费号 6 次/天），「竞品查询」与「广告洞察」为会员功能。

---

## 五、依赖

见 `requirements.txt`，主要组件：

| 包 | 版本 | 用途 |
|---|---|---|
| `playwright` | 1.62.0 | 浏览器自动化 |
| `fastapi` + `uvicorn[standard]` | 0.141.1 / 0.52.4 | Web 后端 |
| `openpyxl` | 3.1.5 | Excel 读写与美化 |
| `greenlet` / `pyee` / `typing-extensions` | — | playwright 运行时依赖 |

> 浏览器内核为 Chromium 151.0.7922.34（完整版 + headless shell），由 `setup_env.ps1` 或
> `download_chromium.py` 下载到项目内 `.pw-browsers\`，不污染系统目录。

---

## 六、数据口径说明

- 所有搜索量 / 点击量 / PPC 出价等指标均为**卖家精灵的估算口径**，非亚马逊官方精确值，
  适合做方向性判断，不适合当作财务依据。
- 广告洞察导出数据的词库为**父体级**（变体共享同一词库），分析时需注意变体合并问题。
- 竞品查询存在「变体合并」行为：传入 N 个 ASIN 可能返回少于 N 行，属正常现象。

---

## 七、开发备注

- 项目内源码为 **UTF-8** 编码；`.bat` 保持纯 ASCII（cmd 按 ANSI 代码页解析）。
- 若需重新探测竞品查询接口，删除 `lookup_api.json` 即可让程序重新走 UI 探测流程。
- 调试期间的临时脚本（接口探测、字段核对、一次性对比）**不入库**，保持主干干净。

---

## 八、许可

内部业务工具，未附开源许可证。如需引用请先与作者确认。
