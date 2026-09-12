# -*- coding: utf-8 -*-
"""
卖家精灵 AI-找竞品(module=CL) 交互式自动化脚本
================================================
功能：
  - 彩色 ASCII banner + 交互式菜单（站点 / 竞品数量 下拉框实时读取页面选项）
  - 复用登录态（首次运行自动弹出浏览器登录）
  - 提交 ASIN 后持续轮询任务状态（动画进度），完成后自动保存报告 JSON
  - 支持连续提交多个 ASIN，每步可中断

用法：
  python sellersprite_ai_report.py [ASIN ...]
  （传入 ASIN 则跳过 ASIN 输入提示，其余流程仍为交互式）
  $env:HEADLESS="0"  # 有界面观察模式
"""

import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://www.sellersprite.com"
PAGE_URL = f"{BASE}/v3/ai-history?module=CL"
HERE = Path(__file__).parent
STATE_FILE = HERE / "storage_state.json"
RESULT_DIR = HERE / "results"
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
POLL_INTERVAL = 5          # 任务状态轮询间隔（秒）
TASK_TIMEOUT = 15 * 60     # 单任务最长等待（秒）
DELAY_RANGE = (15, 30)     # 多个 ASIN 之间的随机间隔（秒）

# ---------- 终端美化 ----------

os.system("")  # Windows 控制台启用 ANSI 转义

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    BLUE   = "\033[34m"
    MAGENTA= "\033[35m"
    CYAN   = "\033[36m"
    ORANGE = "\033[38;5;208m"

BANNER = f"""{C.ORANGE}{C.BOLD}
   ____  _____ _   _  ____ _____ _____ ____
  / ___|| ____| \\ | |/ ___|_   _| ____|  _ \\
  \\___ \\|  _| |  \\| |\\___ \\ | | |  _| | |_) )
   ___) | |___| |\\  | ___) || | | |___|  _ (
  |____/|_____|_| \\_|____/ |_| |_____|_| \\_)
{C.RESET}{C.DIM}        AI-找竞品 (module=CL) 自动化 v2  ·  交互式{C.RESET}
"""

SPINNER = "|/-\\"

def info(msg):    print(f"{C.BLUE}[i]{C.RESET} {msg}")
def ok(msg):      print(f"{C.GREEN}[+]{C.RESET} {msg}")
def warn(msg):    print(f"{C.YELLOW}[!]{C.RESET} {msg}")
def err(msg):     print(f"{C.RED}[x]{C.RESET} {msg}")
def title(msg):   print(f"\n{C.BOLD}{C.ORANGE}== {msg} =={C.RESET}")

def menu(pick_items, header, required=False):
    """通用编号菜单。required=True 时必须选中一项才返回。"""
    print(f"\n{C.CYAN}{header}{C.RESET}")
    for i, it in enumerate(pick_items, 1):
        print(f"  {C.BOLD}{i:>2}{C.RESET}. {it}")
    while True:
        tip = "请输入编号" if required else "请输入编号（直接回车=保持页面默认）"
        raw = input(f"{C.CYAN}{tip}:{C.RESET} ").strip()
        if not raw:
            if required:
                warn("此项为必选，请输入编号")
                continue
            return None
        try:
            idx = int(raw)
            if 1 <= idx <= len(pick_items):
                return pick_items[idx - 1]
        except ValueError:
            pass
        warn(f"无效编号：{raw!r}，请输入 1-{len(pick_items)} 之间的数字")

# ---------- 登录 ----------

async def ensure_login(pw, force=False):
    """force=True 时丢弃旧登录态，弹出浏览器重新登录。"""
    if force and STATE_FILE.exists():
        STATE_FILE.unlink()
    if STATE_FILE.exists():
        ok(f"使用已保存的登录态 {STATE_FILE.name}")
        return
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(BASE)
    input(f"{C.YELLOW}>>> 请在弹出的浏览器中完成登录（确认是会员账号），成功后回到这里按回车…{C.RESET}")
    await ctx.storage_state(path=str(STATE_FILE))
    await browser.close()
    ok(f"登录态已保存到 {STATE_FILE.name}")

# ---------- 页面元素 ----------

async def open_work_page(ctx):
    page = await ctx.new_page()
    await page.goto(PAGE_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    return page

async def get_running_task(page):
    """页面加载后若已有进行中任务，返回其 taskId。"""
    try:
        data = await page.evaluate(
            "async () => { const r = await fetch('/v3/api/ai-analysis/get-submitted'); return await r.json(); }")
        for t in (data.get("data") or []):
            if t.get("taskStatus") == "SUBMITTED":
                return t.get("taskId"), t.get("title") or ""
    except Exception:
        pass
    return None, ""

async def read_select_options(page, select_idx):
    """点开第 select_idx 个下拉框，实时读取选项文本后按 Esc 收起。"""
    sel = page.locator(".el-select .el-input__inner").nth(select_idx)
    await sel.click()
    try:
        await page.locator(".el-select-dropdown:visible").first.wait_for(state="visible", timeout=5000)
        await page.wait_for_timeout(300)
        items = await page.locator(".el-select-dropdown:visible .el-select-dropdown__item").all_inner_texts()
        items = [t.strip() for t in items if t.strip()]
    except Exception:
        items = []
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    return items

async def set_select(page, select_idx, value):
    sel = page.locator(".el-select .el-input__inner").nth(select_idx)
    await sel.click()
    await page.locator(".el-select-dropdown:visible").first.wait_for(state="visible", timeout=5000)
    await page.wait_for_timeout(300)
    await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", has_text=value).first.click()
    await page.wait_for_timeout(300)

async def detect_selects(page):
    """识别页面上的下拉框：按选项内容猜测用途。"""
    n = await page.locator(".el-select .el-input__inner").count()
    found = []
    for i in range(min(n, 4)):
        items = await read_select_options(page, i)
        if not items:
            continue
        numeric = sum(1 for t in items if re.search(r"\d", t))
        kind = "count" if numeric >= len(items) * 0.6 else "site"
        found.append({"idx": i, "kind": kind, "items": items})
    return found

# ---------- 提交与轮询 ----------

async def submit_asin(page, asin):
    """填 ASIN + 点生成，返回 taskId（优先从创建接口的响应里抓）。"""
    task_id = {"v": None}

    async def on_response(resp):
        req = resp.request
        if req.method == "POST" and "ai-analysis" in resp.url and "usage-log" not in resp.url and "message" not in resp.url:
            try:
                j = await resp.json()
                tid = (j.get("data") or {}).get("taskId") if isinstance(j.get("data"), dict) else None
                if tid:
                    task_id["v"] = tid
                    ok(f"捕获任务创建响应: {tid}  <-  {resp.url}")
            except Exception:
                pass

    page.on("response", on_response)
    try:
        ta = page.locator("textarea.el-textarea__inner")
        await ta.wait_for(state="visible", timeout=30000)
        await ta.fill(asin)
        btn = page.locator("button.panel-submit-btn").filter(has_text="生成")
        if await btn.count() == 0:
            btn = page.locator("button.panel-submit-btn")
        await btn.first.click()
        info("已点击生成按钮，等待任务创建…")
        for _ in range(16):  # 最多 8s 找创建响应
            await page.wait_for_timeout(500)
            if task_id["v"]:
                break
        if not task_id["v"]:  # 兜底：从 get-submitted 里按 ASIN 匹配
            data = await page.evaluate(
                "async () => { const r = await fetch('/v3/api/ai-analysis/get-submitted'); return await r.json(); }")
            for t in (data.get("data") or []):
                if asin in (t.get("title") or "") and t.get("taskStatus") == "SUBMITTED":
                    task_id["v"] = t.get("taskId")
                    info(f"从提交列表定位到任务: {task_id['v']}")
                    break
    finally:
        page.remove_listener("response", on_response)
    return task_id["v"]

async def poll_task(page, task_id, quiet=False):
    """轮询任务直到完成，带动画进度；返回最终 task dict。"""
    url = f"/v3/api/ai-analysis/task/original/summary/{task_id}"
    t0, frame, last = time.time(), 0, None
    print()
    while time.time() - t0 < TASK_TIMEOUT:
        try:
            data = await page.evaluate(
                "async (u) => { const r = await fetch(u); return await r.json(); }", url)
            t = (data.get("data") or [{}])[0]
            last = t
            status = t.get("taskStatus")
            elapsed = int(time.time() - t0)
            spin = SPINNER[frame % len(SPINNER)]; frame += 1
            bar = "█" * min(elapsed // 10, 25) + "░" * max(25 - elapsed // 10, 0)
            if status == "SUBMITTED":
                print(f"\r  {C.ORANGE}{spin}{C.RESET} 分析中 [{C.CYAN}{bar}{C.RESET}] {elapsed:>3}s "
                      f"(预计5-8分钟, Ctrl+C可中断保留后台)", end="", flush=True)
            else:
                print()
                if t.get("taskErrMsg"):
                    err(f"任务失败: {t['taskErrMsg']}")
                    return t
                ok(f"任务完成: {status}  用时 {elapsed}s")
                return t
        except Exception as e:
            if not quiet:
                warn(f"轮询异常 {e!r}，重试中…")
        await asyncio.sleep(POLL_INTERVAL)
    warn("等待超时，任务仍在后台运行")
    return last or {}

async def harvest(ctx, page, task_id, tag):
    """拉取完整任务详情并保存。"""
    data = await page.evaluate(
        "async (u) => { const r = await fetch(u); return await r.json(); }",
        f"/v3/api/ai-analysis/task/{task_id}")
    RESULT_DIR.mkdir(exist_ok=True)
    fp = RESULT_DIR / f"{tag}_{task_id[:12]}.json"
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    d = data.get("data") or {}
    ok(f"报告已保存: {fp}")
    print(f"  标题:   {C.BOLD}{d.get('title') or '-'}{C.RESET}")
    print(f"  状态:   {d.get('taskStatus')}   完成时间: {d.get('completedTime') or '-'}")
    content = d.get("content") or ""
    if content:
        print(f"  内容预览: {content[:400]}{'…' if len(content) > 400 else ''}")
    return d

# ---------- 主流程 ----------

async def workflow(ctx, page, asin):
    tag = asin
    tid, _ = await get_running_task_for(page, asin)
    if tid:
        warn(f"发现 {asin} 已有进行中任务，直接进入等待: {tid}")
    else:
        tid = await submit_asin(page, asin)
        if not tid:
            err("未能定位任务 ID（检查终端是否有页面错误提示/配额是否用尽）")
            return
    t = await poll_task(page, tid)
    if t.get("taskStatus") not in (None, "SUBMITTED"):
        await harvest(ctx, page, tid, tag)

async def get_running_task_for(page, asin):
    tid, ttl = await get_running_task(page)
    if tid and asin in ttl:
        return tid, ttl
    return None, ""

async def main():
    print(BANNER)
    argv_asins = [a.strip().upper() for a in sys.argv[1:] if a.strip()]

    async with async_playwright() as pw:
        force = input(f"{C.YELLOW}是否重新登录（直接回车=是，输入 n 跳过）？{C.RESET} ").strip().lower() != "n"
        await ensure_login(pw, force)
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(storage_state=str(STATE_FILE))
        ctx.set_default_timeout(30_000)
        page = await open_work_page(ctx)

        # 配额
        try:
            q = await page.evaluate("async () => { const r = await fetch('/v3/api/ai-analysis/daily-remaining-quota'); return await r.json(); }")
            info(f"今日剩余配额: {C.BOLD}{q.get('data')}{C.RESET} 次")
        except Exception:
            pass

        # 已有进行中的任务？
        tid, ttl = await get_running_task(page)
        if tid:
            warn(f"检测到进行中的任务: {ttl}")
            if input(f"{C.CYAN}是否先等它完成并收割？[Y/n]:{C.RESET} ").strip().lower() != "n":
                t = await poll_task(page, tid)
                if t.get("taskStatus") not in (None, "SUBMITTED"):
                    await harvest(ctx, page, tid, ttl.split(":")[-1].strip() or tid[:12])
                await page.goto(PAGE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

        # 主循环：每一轮 = 选站点 → 选数量 → 输入 ASIN → 逐个处理
        pending = list(argv_asins)
        while True:
            # 刷新页面，确保提交面板为初始状态
            await page.goto(PAGE_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            title("第 1 步：选择 Amazon 站点")
            selects = await detect_selects(page)
            site_sel = next((s for s in selects if s["kind"] == "site"), None)
            if not site_sel:
                err("未在页面上识别到站点下拉框（选项内容无法识别），退出")
                break
            site_pick = menu(site_sel["items"], "可选站点", required=True)
            await set_select(page, site_sel["idx"], site_pick)
            ok(f"站点已设置为 {C.BOLD}{site_pick}{C.RESET}")

            title("第 2 步：选择竞品查找数量")
            count_sel = next((s for s in selects if s["kind"] == "count"), None)
            if not count_sel:
                err("未在页面上识别到数量下拉框，退出")
                break
            count_pick = menu(count_sel["items"], "可选数量", required=True)
            await set_select(page, count_sel["idx"], count_pick)
            ok(f"数量已设置为 {C.BOLD}{count_pick}{C.RESET}")

            # 输入 ASIN（命令行传入的只在第一轮使用一次）
            if pending:
                asins, pending = pending, []
                info(f"使用命令行传入的 ASIN: {asins}")
            else:
                while True:
                    raw = input(f"\n{C.CYAN}第 3 步：请输入 ASIN（多个用空格/逗号分隔）:{C.RESET} ").strip()
                    asins = [a.strip().upper() for a in re.split(r"[,，\s]+", raw) if a.strip()]
                    if asins:
                        break
                    warn("至少输入一个 ASIN")

            for k, asin in enumerate(asins, 1):
                title(f"[{k}/{len(asins)}] 处理 {asin}")
                await workflow(ctx, page, asin)
                if k < len(asins):
                    wait = random.uniform(*DELAY_RANGE)
                    info(f"随机等待 {wait:.0f}s 后处理下一个（降低风控风险）…")
                    await asyncio.sleep(wait)

            again = input(f"{C.CYAN}继续提交新的 ASIN（将重新选择站点/数量）？[y/N]:{C.RESET} ").strip().lower()
            if again != "y":
                break

        await browser.close()
        print(f"\n{C.GREEN}{C.BOLD}全部完成，报告见 results/ 目录。{C.RESET}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}已中断（进行中的任务不受影响，下次运行可选择收割）。{C.RESET}")
