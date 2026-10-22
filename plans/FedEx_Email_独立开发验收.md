# FedEx Email Tracking Automation（FETA）实现与系统集成验收

日期：2026-10-22

状态：查询与解析仍属实验阶段；已将收件箱同步和人工复核写回接入订单物流页，但并非免人工或生产级自动化。

## 结果概览

- `feta/` 包提供 CSV 导入、最多 30 号批次预览/发送、IMAP 增量收件、实验性解析、人工复核、异常告警和 CSV 报表；另有本地物流页的 FETA 审核区，可只读同步收件箱、显示待复核结果，并在人工点击接受后写入本地订单物流库。
- FETA 原始数据仍写入独立的 `deep_results/feta.sqlite3`；订单数据仍由 `tracking_store` 管理。跨库只按跟踪号关联，接受后在主库事件记录保存 FETA 事件 ID、邮件收件时间和原始 ETA 文本。邮件收件时间不会伪装成扫描时间，ETA 不转换，也不填造地点。当前只映射 `IN_TRANSIT → in_transit`、`DELIVERED → delivered`；`UNKNOWN`、`LABEL_CREATED` 等状态只留审计事件、不改当前状态。
- 用户授权后只读抽取了 21 条候选 FedEx 跟踪关系所需字段，发送一封含 21 个号码的真实查询邮件并检查邮箱。21 个解析事件目前仍为待复核；尚未接受，未将这批状态写入主订单库。
- 发送默认 `DRY_RUN=true`。本次真实发送只使用一次性进程环境覆盖；本机 `.env.feta` 仍保持 `DRY_RUN=true`，没有被读取到聊天或改写。网页新增的邮箱按钮只读收件箱，不会发送查询邮件。
- 本说明随 FETA 源码集成到 Git 仓库；不包含 `.env.feta`、真实 FETA SQLite、邮件正文或业务数据库。其他未提交的广告/性能改动不属于本次 FETA 提交范围。

## 本地接口

```powershell
# 安全初始化检查，不连接邮箱
python -m feta doctor

# 导入 UTF-8 CSV；表头 order_id,tracking_number,carrier,destination_timezone,active
python -m feta import .\tracking.csv

# 纯本地预览；不创建批次、不发信
python -m feta query preview

# 真实查询只有明确授权后才执行；每轮最多 30 单
# .env.feta 必须设 DRY_RUN=false
python -m feta query send --confirm-send

# 认证测试不发送查询信；收件为只读 IMAP
python -m feta mail test-auth --confirm-network
python -m feta inbox fetch --confirm-network
python -m feta inbox messages
python -m feta inbox pending
python -m feta inbox show <邮件摘要ID>

# 对此前未认证邮件按保存的 UID 单封重取；只在认证通过后产生待复核事件
python -m feta inbox reprocess <邮件摘要ID> --confirm-network
python -m feta inbox review <事件ID> --decision accept   # 或 ignore

python -m feta alerts run
python -m feta alerts list
python -m feta report export --output .\feta-report.csv
```

使用前将 `.env.feta.example` 复制成 `.env.feta`，限制该文件只供本机运营用户读取；模板默认 `DRY_RUN=true`。项目 `.gitignore` 已忽略 `.env.feta` 和默认 FETA SQLite 文件。当前 MVP 只支持 Gmail SMTP/IMAP + 应用专用密码，不实现 OAuth；普通 Gmail 登录密码不得填入。

## 安全/状态行为

- 查询邮件收件人为 `track@fedex.com`，不设置 `Subject` header；邮件正文严格是跟踪号逐行排列，不含订单号/批次 ID。唯一号码去重，一单多订单仍只查询一次。每轮最多 30 单，默认 12 小时间隔；明确发送失败和回复超时采用 30/60/120 分钟退避，最多三次重试，未答运单才进入下一次重试。SMTP 结果不确定、程序中断超过 5 分钟时转 `unknown` 并等待人工核对，不盲目重发。
- IMAP 使用 TLS、只读 INBOX、UIDVALIDITY/UID 增量游标和 Message-ID 去重；每次最多处理 50 封。单封 UID 读取失败会中止本轮且不越过游标缺口，下一次拉取可重试；大小先于正文读取，单封 RFC822 上限 1 MiB、认证头 64 KiB、MIME 部件数 50、抽取文本 256 KiB。只对头部域名及 Gmail `Authentication-Results` 中与 FedEx 对齐的通过声明作候选验证；无效/缺失/不确定邮件不会解析状态。超限邮件只存摘要并标为 `oversize`。
- **认证边界仍未经过真实 Gmail 邮件验证**：本地代码不能证明 `Authentication-Results` header 确实由接收方追加，也没有用真实信件校准 Gmail 的头部顺序/净化行为。即使认证候选通过，解析结果置信度仍是 `low` 且 `needs_manual_review=true`；必须人工复核，不能把该解析器作为免人工的签收依据。
- 为减少敏感信息留存，不存整封原始邮件/附件，仅保存消息 UID、Message-ID、SHA-256 和解析摘要；未认证邮件首次只存受限 header 摘要，授权用户可用 `inbox reprocess` 从邮箱按 UID 单封重取。附件、脚本、远端 HTML 资源不执行/不访问。
- FETA 收件/超时本身不会生成包裹异常；解析结果始终需要人工复核。CLI 的 `inbox review ... accept` 只更新 FETA 数据库。物流页接受操作先校验 FETA 复核门槛，再以幂等事件 ID 写入主物流库；只有 `IN_TRANSIT` / `DELIVERED` 更新主状态，其他状态仅保存审核事件。主库不把邮件收件时间当作扫描时间，也不生成地点/UTC ETA。
- 异常告警按运单/规则/状态去重。告警邮件同样要求 `DRY_RUN=false`、`alerts run --send --confirm-send`；发送结果不确定会阻止自动再次发送，核查邮箱后才可人工重新排队。
- Windows 若本机 IANA TZif 数据库缺失，CSV 校验只接受代码明确列出的美国常用时区 fallback；其他名称明确拒绝并要求安装 `tzdata`。ETA 时间仅保存原始文本，当前不解析/转换成 UTC，不推算 ETA。

## 未实现/未验证

- 规格 P0 要求至少三封匿名化真实 FedEx 回信。本次仅经授权发送了一封查询邮件并收到一封候选回信（匹配 21 个结果）；解析器已初步解析，但置信度低、事件待人工复核，且原始邮件未归档。**P0 仍未完成**：真实邮件样本不足，不能宣称回复模板稳定、ETA 覆盖率或官方往返时限已验证。
- 本次 Gmail SMTP/IMAP 认证各成功一次，单次邮件提交及回信处理成功；没有验证 FedEx 拒收规则、允许频率或长期稳定性。未访问 FedEx 网页、未启动/重启服务、未建立计划任务。
- 现有物流页已增加 FETA 邮件待复核列表、只读收件箱同步和接受/忽略操作；但网页尚未集成出站查询邮件发送，出站仍使用 FETA CLI，并受 `DRY_RUN`、批次上限和 12 小时间隔保护。现有 ERP 导入和手工物流工作流保留。

## 验证证据

自动化测试均使用合成 `.example.test` 邮件、fake SMTP/IMAP、本地临时 SQLite 和 fake clock。另有一次用户明确授权的真实运行：只读订单库抽取 21 条候选关系、发送一批查询并只读检查专用邮箱；没有访问承运商网站，没有写入现有订单库，也没有把跟踪号或邮件正文写入报告。

- FETA 专项：`python -m unittest tests.test_feta` —— **31 项通过**；物流/FETA 集成定向测试：`python -m unittest tests.test_tracking_store tests.test_tracking_api tests.test_tracking_import tests.test_feta tests.test_feta_integration` —— **56 项通过**。
- 当前仓库全项目：`python -m unittest discover -s tests -p 'test_*.py'` —— **109 项通过，9 项跳过**。本次未启用可选 Chromium 测试；静态 JavaScript 语法检查通过，不能据此声称浏览器交互验证通过。
- CLI 离线冒烟：`doctor`、`query preview` 可运行；不加 `--confirm-network` 的认证/收件命令均在连接前拒绝。冒烟 SQLite 使用独立合成文件，已删除。
- 当前虚拟环境没有 `pytest` 模块（`python -m pytest` 不可用），所以以上验证使用项目 Python 的标准库 `unittest`；没有为安装测试依赖访问网络。
