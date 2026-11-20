# 价格巡查工作区（预览）

这是一页独立的 Amazon US 价格巡查界面，使用 Preact、TypeScript 和原生 CSS。保持旧版 `/#amazon` 及现有 API、SQLite 和采集规则不变。

## 先看离线演示

需要 Node.js 22.12 或更新版本来**构建**；试玩无需 Node、Python 或联网。

```powershell
cd frontend/price-workspace
npm ci --ignore-scripts
npm run build
```

直接双击生成的 `dist/index.html`。它是单文件 HTML，使用虚构商品，可以尝试切换/编辑分组、设定本品价、排序筛选、预览全部分组及模拟巡查。刷新页面会恢复初始示例；不会访问 Amazon 或读写业务库。

开发时可运行 `npm run dev` 后打开 <http://127.0.0.1:5173/?demo=1>。该地址只用于开发演示，不代理业务 API。

## 接入本地数据

构建后，从仓库根目录用现有 Python 环境运行 `python web_server.py`，访问 <http://127.0.0.1:8765/price-workspace/>。加 `?demo=1` 切回离线示例；`/` 和 `/#amazon` 仍是旧界面。正常模式的分组和价格会写入同一份本地数据库。只有手动点击「巡查本组」或确认「巡查全部分组」才会访问 Amazon US；任务进行时允许换组查看，但不重复启动和修改名单。

未构建时预览地址返回 404 并提示构建，旧版仍可正常使用。此 PR 不修改 Windows 便携包，也不依赖桌面窗口 PR。

## 维护与验证

- `src/api.ts`：现有 API 的类型和调用；`src/useWorkspace.ts`：可取消的分组读取和仅在页面可见时的任务轮询。
- `src/App.tsx` 和 `src/Results.tsx`：工作区与结果展示；`src/model.ts`：输入校验、筛选和排序。对比、库存、购买价和优惠仍以服务端结果为准。
- `src/demo.ts`：浏览器内示例适配器，不读写本地或远程服务。构建脚本把样式和 JavaScript 内联进 HTML，不加载外部字体、图片或 CDN。

从仓库根目录用已有 Python/Playwright 环境运行；需要已安装的 Chromium（或已有 `PLAYWRIGHT_BROWSERS_PATH`）。用例拦截 API 请求，不登录真实账号：

```powershell
$env:AMZ_PREVIEW_BROWSER_TESTS = '1'
python -B -m unittest discover -s tests -p 'test_price_workspace_ui.py' -v
python -B -m unittest discover -s tests -p 'test_amazon_*.py'
```
