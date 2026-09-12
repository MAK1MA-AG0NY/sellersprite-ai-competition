# -*- coding: utf-8 -*-
"""
卖家精灵竞品深挖 - 本地 Web 控制台
====================================
后端：FastAPI + Playwright（复用 sellersprite_deep_dive.py 中已验证的接口逻辑）
前端：web_index.html（本地单页，无外部依赖）

启动：见 start_web.bat（或 python web_server.py 后访问 http://127.0.0.1:8765）
"""

import asyncio
import json
import re
import time
import random
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import async_playwright

import sellersprite_deep_dive as sd
import sellersprite_ai_report as ai   # CL 页面下拉框操作(detect_selects/set_select)在这里
import local_db
import make_report
import ads_insights as adv

BASE = sd.BASE
HERE = sd.HERE
STATE_FILE = sd.STATE_FILE
MEMBER_STATE_FILE = sd.MEMBER_STATE_FILE
LOOKUP_CFG = sd.LOOKUP_CFG
OUT_DIR = HERE / "deep_results"
WEB_INDEX = HERE / "web_index.html"
POOL_DIR = HERE / "states"          # 主账号池登录态：states/main_{N}.json

FETCH_GET = sd.FETCH_GET
CL_URL = f"{BASE}/v3/ai-history?module=CL"
OPTS_FILE = HERE / "cl_options.json"   # 下拉框可选值的磁盘缓存
HISTORY_URL = f"{BASE}/v3/ai-history?historyType=all&backModule=CL"
# 三个互斥页面状态：提交面板 / 历史列表 / 进行中任务进度面板 —— 操作前必须导航到对应面板
PANEL_TARGET = {
    "submit":  (CL_URL,      "textarea.el-textarea__inner"),
    "history": (HISTORY_URL, "div.history-item"),
}

async def goto_panel(page, kind, timeout_ms=20000):
    """把主账号页面导航到指定面板并等待其特征元素出现；两次尝试失败返回 False。"""
    url, sel = PANEL_TARGET[kind]
    for _ in range(2):
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(sel, state="visible", timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False

app = FastAPI(title="SellerSprite Deep-Dive")

S = {
    "pw": None, "browser": None,
    "main_page": None, "vip_page": None,
    "pool_pages": {},          # slot -> page（账号池上下文缓存）
    "pool_dead": set(),        # "main" / slot int：会话已失效、暂时拉黑
    "kw_rr": 0,                # kw_miner 批次轮转计数（跨调用连续轮换）
    "tasks": [],
    "asins": [], "ai_rows": [], "current_task": None,
    "jobs": {},
}
LOGIN_SESSIONS = {}   # role -> {browser, ctx, page}


def _pool_file(slot: int) -> Path:
    return POOL_DIR / f"main_{slot}.json"


def _role_path(role: str):
    """角色 → 登录态文件；main/member/pool{N}，其余 None。"""
    if role == "main":
        return STATE_FILE
    if role == "member":
        return MEMBER_STATE_FILE
    m = re.fullmatch(r"pool(\d+)", role)
    if m:
        return _pool_file(int(m.group(1)))
    return None


# ---------- 基础设施 ----------

async def get_pw():
    if S["pw"] is None:
        S["pw"] = await async_playwright().start()
    return S["pw"]

async def get_browser():
    pw = await get_pw()
    if S["browser"] is None or not S["browser"].is_connected():
        S["browser"] = await pw.chromium.launch(headless=True)
    return S["browser"]

async def get_main_page():
    if S["main_page"] is None:
        browser = await get_browser()
        ctx = await browser.new_context(storage_state=str(STATE_FILE))
        ctx.set_default_timeout(30_000)
        page = await ctx.new_page()
        await page.goto(f"{BASE}/v3/ai-history?module=CL", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        S["main_page"] = page
    return S["main_page"]

async def get_vip_page():
    if S["vip_page"] is None:
        browser = await get_browser()
        ctx = await browser.new_context(storage_state=str(MEMBER_STATE_FILE))
        ctx.set_default_timeout(60_000)
        page = await ctx.new_page()
        await page.goto(f"{BASE}/v3/competitor-lookup", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        S["vip_page"] = page
    return S["vip_page"]

async def get_pool_page(slot: int):
    """账号池槽位的卖家精灵页面（与主账号页相互独立）。"""
    if slot not in S["pool_pages"]:
        f = _pool_file(slot)
        if not f.exists():
            raise RuntimeError(f"账号池 {slot} 未保存登录态")
        browser = await get_browser()
        ctx = await browser.new_context(storage_state=str(f))
        ctx.set_default_timeout(30_000)
        page = await ctx.new_page()
        await page.goto(f"{BASE}/v3/keyword-miner/", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        S["pool_pages"][slot] = page
    return S["pool_pages"][slot]


def reset_ctx(role):
    """登录确认后使缓存的上下文失效，下次访问用新登录态重建；同时解除该角色的失效拉黑。"""
    if role == "main":
        S["main_page"] = None
        S["pool_dead"].discard("main")
    elif role == "member":
        S["vip_page"] = None
    else:
        m = re.fullmatch(r"pool(\d+)", role)
        if m:
            S["pool_pages"].pop(int(m.group(1)), None)
            S["pool_dead"].discard(int(m.group(1)))

def fmt_account(d):
    exp = d.get("expiredDate")
    exp_s = datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d") if isinstance(exp, (int, float)) else "-"
    return {"nickname": d.get("nickname") or "-", "memberLevel": d.get("memberLevel") or "-",
            "expired": exp_s}

# ---------- 登录 ----------

@app.post("/api/login/{role}")
async def login_start(role: str):
    if _role_path(role) is None:
        raise HTTPException(400, "role 必须是 main、member 或 poolN（账号池）")
    pw = await get_pw()
    if role in LOGIN_SESSIONS:
        try:
            await LOGIN_SESSIONS[role]["page"].goto(BASE, wait_until="domcontentloaded")
            return {"ok": True, "message": "登录窗口已打开（如被关闭会自动重开），完成登录后点“我登录好了”"}
        except Exception:
            pass
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(BASE, wait_until="domcontentloaded")
    LOGIN_SESSIONS[role] = {"browser": browser, "ctx": ctx, "page": page}
    return {"ok": True, "message": "登录窗口已打开，完成登录后点“我登录好了”"}

@app.post("/api/login/{role}/confirm")
async def login_confirm(role: str):
    s = LOGIN_SESSIONS.get(role)
    if not s:
        raise HTTPException(400, "请先点击“打开登录窗口”")
    page = s["page"]
    try:
        if "sellersprite.com" not in page.url:
            await page.goto(BASE, wait_until="domcontentloaded")
        r = await page.evaluate(FETCH_GET, "/v3/api/client")
        d = (r.get("j") or {}).get("data") or {}
    except Exception as e:
        return {"ok": False, "message": f"校验失败: {e}"}
    if not (d.get("id") and (d.get("mobile") or d.get("email"))):
        return {"ok": False, "message": "尚未检测到登录态，请在弹出的窗口完成登录后再试"}
    path = _role_path(role)
    if path is None:
        raise HTTPException(400, "role 必须是 main、member 或 poolN（账号池）")
    await s["ctx"].storage_state(path=str(path))
    try:
        await s["browser"].close()
    except Exception:
        pass
    LOGIN_SESSIONS.pop(role, None)
    reset_ctx(role)
    return {"ok": True, "account": fmt_account(d),
            "message": f"已保存登录态到 {path.name}"}

@app.post("/api/login/{role}/cancel")
async def login_cancel(role: str):
    s = LOGIN_SESSIONS.pop(role, None)
    if s:
        try:
            await s["browser"].close()
        except Exception:
            pass
    return {"ok": True}

# ---------- 主账号池 ----------

@app.get("/api/pool")
async def pool_list():
    """账号池状态：已保存登录态的槽位 + 正在登录中的槽位（等待点"完成"）+ 失效标记。"""
    slots = {}
    if POOL_DIR.exists():
        for f in sorted(POOL_DIR.glob("main_*.json")):
            m = re.fullmatch(r"main_(\d+)", f.stem)
            if m:
                slot = int(m.group(1))
                slots[slot] = {"slot": slot, "exists": True,
                               "dead": slot in S["pool_dead"], "pending": False}
    # 有登录窗口开着但还没保存登录态的槽位 → pending（前端显示"等待完成登录"+完成按钮）
    for role in LOGIN_SESSIONS:
        m = re.fullmatch(r"pool(\d+)", role)
        if m:
            slot = int(m.group(1))
            e = slots.setdefault(slot, {"slot": slot, "exists": False,
                                        "dead": slot in S["pool_dead"], "pending": False})
            e["pending"] = True
    out = sorted(slots.values(), key=lambda s: s["slot"])
    nxt = 1 + max((s["slot"] for s in out), default=0)
    return {"slots": out, "next_slot": nxt, "main_dead": "main" in S["pool_dead"]}


@app.post("/api/pool/remove/{slot}")
async def pool_remove(slot: int):
    """移除池内账号：关页面、删登录态文件、解除拉黑。"""
    page = S["pool_pages"].pop(slot, None)
    if page:
        try:
            await page.context.close()
        except Exception:
            pass
    S["pool_dead"].discard(slot)
    f = _pool_file(slot)
    existed = f.exists()
    if existed:
        f.unlink()
    return {"ok": True, "removed": existed}

# ---------- 账号状态 ----------

@app.get("/api/accounts")
async def accounts():
    out = {}
    for role, path in (("main", STATE_FILE), ("member", MEMBER_STATE_FILE)):
        if not path.exists():
            out[role] = {"logged_in": False}
            continue
        try:
            page = await (get_main_page() if role == "main" else get_vip_page())
            j = await sd.api_get(page, "/v3/api/client")
            d = j.get("data") or {}
            out[role] = {"logged_in": True, **fmt_account(d)}
        except Exception as e:
            out[role] = {"logged_in": False, "error": str(e)}
    out["lookup_cfg"] = LOOKUP_CFG.exists()
    try:
        out["db"] = local_db.stats()
    except Exception:
        out["db"] = {"snapshots": 0, "asins": 0, "batches": 0}
    return out

# ---------- 历史任务 ----------

@app.get("/api/history")
async def history():
    page = await get_main_page()
    tasks = await sd.fetch_history(page, pages=8)
    api_n = len(tasks)
    dom_tasks = await sd.dom_history(page)
    known = {t["taskId"] for t in tasks}
    dom_extra = 0
    for t in dom_tasks:
        if t["taskId"] not in known:
            tasks.append(t)
            dom_extra += 1
    tasks.sort(key=lambda t: t.get("createdTime") or "", reverse=True)
    S["tasks"] = tasks
    return {"api_count": api_n, "dom_count": len(dom_tasks), "dom_extra": dom_extra,
            "tasks": [{"taskId": t.get("taskId"), "createdTime": sd.fmt_cn_time(t.get("createdTime")),
                       "taskStatus": t.get("taskStatus"), "market": t.get("market") or "-",
                       "asin": (re.search(r"\(([A-Z0-9]{10})\)", t.get("tabTitle") or "") or [None, "?"])[1]
                       if re.search(r"\(([A-Z0-9]{10})\)", t.get("tabTitle") or "") else "?",
                       "source": t.get("_source", "api")} for t in tasks]}

# ---------- ASIN 提取 ----------

@app.get("/api/task/{task_id}/asins")
async def task_asins(task_id: str):
    page = await get_main_page()
    asins, ai_rows = await sd.extract_asins(page, task_id)
    market = next((t.get("market") for t in S["tasks"] if t.get("taskId") == task_id), "US")
    # 种子ASIN = 该 CL 任务 tabTitle 括号里的 ASIN（"我分析的ASIN"，用于报告分页归属）
    t = next((t for t in S["tasks"] if t.get("taskId") == task_id), {})
    m = re.search(r"\(([A-Z0-9]{10})\)", t.get("tabTitle") or "")
    seed = m.group(1) if m else None
    S["asins"], S["ai_rows"], S["current_task"] = asins, ai_rows, task_id
    S["current_market"] = (market or "US").upper()
    S["current_seed"] = seed
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"asins_{task_id[:12]}.json").write_text(
        json.dumps({"taskId": task_id, "seed_asin": seed, "asins": asins, "ai_rows": ai_rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return {"taskId": task_id, "seed_asin": seed, "count": len(asins),
            "preview": asins[:30], "ai_rows_saved": len(ai_rows)}

# ---------- 竞品查询（后台任务） ----------

async def run_lookup(job_id: str, chunk: int):
    job = S["jobs"][job_id]
    page = await get_vip_page()
    asins = S["asins"]
    job["total"] = len(asins)
    cfg = None
    if LOOKUP_CFG.exists():
        cfg = json.loads(LOOKUP_CFG.read_text(encoding="utf-8"))
        job["log"].append(f"[i] 使用已探测接口: {cfg['method']} {cfg['url'][:80]}")
    else:
        job["log"].append("[!] 未探测过查询接口，首批将驱动真实页面自动探测")
    all_rows = []
    batches = [asins[i:i + chunk] for i in range(0, len(asins), chunk)]
    for bi, batch in enumerate(batches, 1):
        job["log"].append(f"—— 批次 {bi}/{len(batches)}：{len(batch)} 个 ASIN ——")
        try:
            if cfg:
                rows = await sd.lookup_api(page, cfg, batch)
            else:
                disc, rows = await sd.probe_lookup(page, batch, chunk)
                if disc:
                    cfg = disc
                    LOOKUP_CFG.write_text(json.dumps(disc, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
                    job["log"].append("[+] 接口已探测并保存到 lookup_api.json")
            for r in rows:
                r["_batch_asins"] = ",".join(batch)
            all_rows.extend(rows)
            got = {str(r.get("asin") or "").strip().upper() for r in rows}
            missing = [a for a in batch if a.upper() not in got]
            job["log"].append(f"[+] 本批返回 {len(rows)} 行（请求 {len(batch)} 个，缺数据 {len(missing)} 个）")
            if missing:
                job["log"].append(f"[!] 无数据: {', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}")
        except Exception as e:
            job["log"].append(f"[x] 批次失败: {type(e).__name__}: {e}")
        job["done"] = min(bi * chunk, len(asins))
        await asyncio.sleep(1.5)
    if all_rows:
        fp = OUT_DIR / f"deep_dive_{(S['current_task'] or 'web')[:12]}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        n, k = sd.export_excel(all_rows, fp)
        job["xlsx"] = str(fp)
        job["log"].append(f"[+] 导出完成: {fp.name} ({n} 行 x {k} 列)")
        try:
            local_db.save_batch(job_id, S["current_task"], len(asins), all_rows)
            S["last_rows"] = all_rows
            job["log"].append("[+] 已写入本地数据库 deepdive.db（可在第4步生成美化报告）")
        except Exception as e:
            job["log"].append(f"[!] 本地数据库写入失败: {e}")
    job["status"] = "done" if all_rows else "error"
    job["rows"] = len(all_rows)

# ---------- AI 找竞品（CL 提交 + 轮询） ----------

async def wait_cl_task(page, tid, job, timeout_s=900):
    """轮询 CL 任务状态直到完成；返回最终状态或 None(超时)。"""
    path = f"/v3/api/ai-analysis/task/original/summary/{tid}"
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            j = await sd.api_get(page, path)
            t = (j.get("data") or [{}])[0]
            st = t.get("taskStatus")
            if st and st != "SUBMITTED":
                return st
            job["log"][-1] = f"[+] 任务 {tid[:12]}… 分析中 {int(time.time() - t0)}s（预计5-8分钟）"
        except Exception:
            pass
        await asyncio.sleep(5)
    return None

async def run_cl_job(job_id, asins, site, count, auto_extract):
    job = S["jobs"][job_id]
    job["total"] = len(asins)
    page = await get_main_page()
    # 导航到提交面板（页面可能停在历史列表/进度面板）
    panel_ok = await goto_panel(page, "submit")
    if not panel_ok:
        rt_tid, rt_title = await ai.get_running_task(page)
        if rt_tid:
            job["log"].append(f"[!] 提交面板被进行中任务占用: {rt_title}，先等待其完成…")
            st = await wait_cl_task(page, rt_tid, job, timeout_s=900)
            job["log"].append(f"[+] 进行中任务已结束 ({st})，重新进入提交面板")
            panel_ok = await goto_panel(page, "submit")
    if not panel_ok:
        job["log"].append("[x] 无法进入 AI 找竞品提交面板（无输入框）。请截图页面发我")
        job["status"] = "error"
        return
    # 设置下拉框
    sels = await ai.detect_selects(page)
    site_sel = next((s for s in sels if s["kind"] == "site"), None)
    count_sel = next((s for s in sels if s["kind"] == "count"), None)
    try:
        if site and site_sel:
            await ai.set_select(page, site_sel["idx"], site)
            job["log"].append(f"[+] 站点已设置: {site}")
        if count and count_sel:
            await ai.set_select(page, count_sel["idx"], count)
            job["log"].append(f"[+] 数量已设置: {count}")
        await page.keyboard.press("Escape")   # 收起可能残留的下拉浮层
        await page.wait_for_timeout(800)
    except Exception as e:
        job["log"].append(f"[!] 下拉框设置失败（使用页面默认）: {e}")
    # 提交前确认输入表单真的存在
    ta = page.locator("textarea.el-textarea__inner")
    if await ta.count() == 0 or not await ta.first.is_visible():
        fp = OUT_DIR / f"debug_cl_{datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            await page.screenshot(path=str(fp))
        except Exception:
            pass
        msgs = await page.locator(".el-message, .el-form-item__error, .el-message__content").all_inner_texts()
        job["log"].append(f"[x] 页面上没有可用的 ASIN 输入框！截图: {fp.name}，页面提示: {msgs}")
        job["log"].append("[x] 常见原因：①每日配额已用尽 ②页面弹窗未关闭 ③结构变化（把截图发我）")
        job["status"] = "error"
        return
    # 逐个提交
    done_tids = []
    for i, asin in enumerate(asins, 1):
        job["log"].append(f"—— [{i}/{len(asins)}] 提交 {asin} ——")
        try:
            tid = await ai.submit_asin(page, asin)
        except Exception as e:
            tid = None
            job["log"].append(f"[x] 提交异常: {type(e).__name__}: {e}")
            try:
                fp = OUT_DIR / f"debug_cl_{datetime.now():%Y%m%d_%H%M%S}.png"
                await page.screenshot(path=str(fp))
                msgs = await page.locator(".el-message, .el-form-item__error, .el-message__content").all_inner_texts()
                job["log"].append(f"[i] 已保存现场截图 {fp.name}；页面提示: {msgs}")
            except Exception:
                pass
        if not tid:
            job["log"].append(f"[x] {asin} 提交失败（检查配额/格式）")
            job["done"] = i
            continue
        job["log"].append(f"[+] 任务已创建: {tid[:12]}… 等待报告生成")
        status = await wait_cl_task(page, tid, job)
        if status:
            done_tids.append(tid)
            job["log"].append(f"[+] {asin} 分析完成 ({status})")
        else:
            job["log"].append(f"[!] {asin} 等待超时（任务仍在后台，可稍后在历史任务里收割）")
        job["done"] = i
        if i < len(asins):
            d = random.uniform(15, 30)
            job["log"].append(f"[i] 随机等待 {d:.0f}s 后处理下一个…")
            await asyncio.sleep(d)
    # 自动提取最后一个完成任务
    if auto_extract and done_tids:
        tid = done_tids[-1]
        asins2, ai_rows = await sd.extract_asins(page, tid)
        S["asins"], S["ai_rows"], S["current_task"] = asins2, ai_rows, tid
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / f"asins_{tid[:12]}.json").write_text(
            json.dumps({"taskId": tid, "asins": asins2, "ai_rows": ai_rows},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        job["log"].append(f"[+] 已自动提取 {len(asins2)} 个竞品 ASIN（任务 {tid[:12]}…），可直接执行第4步批量查询")
    job["status"] = "done" if done_tids else "error"
    job["task_ids"] = done_tids

@app.get("/api/cl/options")
async def cl_options():
    page = await get_main_page()
    # 启动后首次先加载磁盘缓存
    if S.get("cl_options") is None and OPTS_FILE.exists():
        try:
            S["cl_options"] = json.loads(OPTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    cached = S.get("cl_options")
    # 先确保停在提交面板（可能停在历史列表或进度面板）
    panel_ok = await goto_panel(page, "submit")
    if not panel_ok:
        if cached:
            return {**cached, "note": "提交面板未就位（可能被进行中任务占用），已使用缓存的选项"}
        return {"site": [], "count": [],
                "note": "提交面板未就位（可能被进行中任务占用），等它完成后再读，或先用页面默认"}
    sels = await ai.detect_selects(page)
    out = {"site": [], "count": []}
    for s in sels:
        out[s["kind"]] = s["items"]
    if out["site"] or out["count"]:
        S["cl_options"] = out
        try:
            OPTS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        return out
    # 读取失败：回退缓存
    if cached:
        return {**cached, "note": "本次未读到选项（页面状态异常），已使用缓存"}
    if OPTS_FILE.exists():
        try:
            return {**json.loads(OPTS_FILE.read_text(encoding="utf-8")),
                    "note": "本次未读到选项（页面状态异常），已使用磁盘缓存"}
        except Exception:
            pass
    return {"site": [], "count": [], "note": "读取失败且无缓存，稍后再试或先用页面默认"}

@app.post("/api/cl/submit")
async def cl_submit(req: dict):
    raw = req.get("asins") or ""
    if isinstance(raw, str):
        asin_list = [a.strip().upper() for a in re.split(r"[,，\s]+", raw) if a.strip()]
    else:
        asin_list = [str(a).strip().upper() for a in raw if str(a).strip()]
    if not asin_list:
        raise HTTPException(400, "请输入至少一个 ASIN")
    job_id = f"cl{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0,
                         "total": len(asin_list), "rows": 0, "xlsx": None, "type": "cl"}
    asyncio.create_task(run_cl_job(job_id, asin_list, req.get("site"), req.get("count"),
                                   bool(req.get("auto_extract"))))
    return {"job_id": job_id}

@app.post("/api/lookup")
async def lookup(req: dict):
    asins = S["asins"]
    if not asins:
        raise HTTPException(400, "请先选择历史任务并提取 ASIN")
    chunk = int(req.get("chunk") or 20)
    if chunk <= 0:
        chunk = 20
    job_id = f"job{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0,
                         "total": len(asins), "rows": 0, "xlsx": None}
    asyncio.create_task(run_lookup(job_id, chunk))
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = S["jobs"].get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return {k: (v[-30:] if k == "log" else v) for k, v in job.items()}

@app.get("/api/job/{job_id}/download")
async def job_download(job_id: str):
    job = S["jobs"].get(job_id)
    if not job or not job.get("xlsx"):
        raise HTTPException(404, "尚无可下载文件")
    return FileResponse(job["xlsx"], filename=Path(job["xlsx"]).name)

# ---------- 美化报告（从本地数据库生成） ----------

@app.post("/api/report")
async def build_styled_report():
    # 优先用最近一次查询的完整返回（含变体代表行）；否则回退本地库取全部最新快照
    rows = S.get("last_rows") or local_db.latest(S["asins"] or None)
    if not rows:
        raise HTTPException(400, "本地库中还没有数据，请先运行批量查询")
    fp = OUT_DIR / f"styled_report_{(S['current_task'] or 'web')[:12]}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    make_report.build_report(rows, S["current_task"], str(fp),
                             market=S.get("current_market") or "US")
    S["last_report"] = str(fp)
    return {"ok": True, "count": len(rows), "file": fp.name}

@app.get("/api/report/download")
async def report_download():
    p = S.get("last_report")
    if not p or not Path(p).exists():
        raise HTTPException(404, "先生成美化报告")
    return FileResponse(p, filename=Path(p).name)

# ---------- ⑥ 广告分析（ads-insights 关键词历史） ----------

@app.post("/api/ads/import")
async def ads_import():
    """解析 adv_reserach/ 下全部 KeywordHistory-*.xlsx 入库"""
    files = adv.import_all()
    if not files:
        raise HTTPException(400, f"目录下没有 KeywordHistory-*.xlsx：{adv.ADS_DIR}")
    total, errs = 0, []
    for f in files:
        if f.get("error"):
            errs.append(f"{f['file']}: {f['error']}")
            continue
        total += local_db.save_ads(f["source"], f["market"], f["rows"])
    return {"ok": True, "files": len(files), "rows": total,
            "errors": errs, "db": local_db.stats()}

# ---------- 报告分页：按"我分析的ASIN"归属 ----------

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _seed_map():
    """竞品ASIN → 我分析的ASIN（CL任务的种子）。数据来自 deep_results/asins_*.json（含 seed_asin），
    当前会话的任务映射优先（用最新一次提取覆盖历史）。"""
    m = {}
    if OUT_DIR.exists():
        for fp in sorted(OUT_DIR.glob("asins_*.json"), key=lambda p: p.stat().st_mtime):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            seed = d.get("seed_asin")
            if seed:
                for a in d.get("asins") or []:
                    m[str(a).strip().upper()] = seed.upper()
    if S.get("asins"):
        t = next((t for t in S["tasks"] if t.get("taskId") == S.get("current_task")), {})
        mm = re.search(r"\(([A-Z0-9]{10})\)", t.get("tabTitle") or "")
        if mm:
            for a in S["asins"]:
                m[str(a).strip().upper()] = mm.group(1).upper()
    return m


def _build_page_groups(kept, seed_map):
    """把漏斗保留词按"我分析的ASIN"分页、页内按来源（竞品ASIN/导入来源）分区域。
    未关联到任何任务的ASIN → "其他来源"页；非ASIN来源（导入种子词等）→ "导入来源"页。"""
    seed_blocks, other_blocks, word_blocks = {}, {}, {}   # seed→comp→rows / src→rows / src→rows
    for r in kept:
        for s in (r.get("_sources") or []):
            su = str(s).strip().upper()
            if ASIN_RE.match(su):
                seed = seed_map.get(su)
                if seed:
                    seed_blocks.setdefault(seed, {}).setdefault(su, []).append(r)
                else:
                    other_blocks.setdefault(su, []).append(r)
            else:
                word_blocks.setdefault(str(s), []).append(r)
    groups = []

    def _blocks(d):
        return [{"label": k, "rows": v} for k, v in sorted(d.items(), key=lambda x: -len(x[1]))]

    for seed, comps in sorted(seed_blocks.items(),
                              key=lambda kv: -sum(len(v) for v in kv[1].values())):
        groups.append({"title": seed, "blocks": _blocks(comps)})
    if other_blocks:
        groups.append({"title": "其他来源", "blocks": _blocks(other_blocks)})
    if word_blocks:
        groups.append({"title": "导入来源", "blocks": _blocks(word_blocks)})
    return groups


@app.post("/api/ads/report")
async def ads_report(req: dict = None):
    """广告分析报告（后台任务，轮询 /api/job/{job_id}）：
    数据源 = ⚡自动抓取入库的广告词(ads_kw_full) + ①导入的导出文件(ads_keywords)。
    scope=all（默认）：全部历史数据；scope=current：只统计当前分析任务的竞品ASIN广告词。
    backfill=true：补齐**全体保留词**的官方指标（月购买量/购买率）——20词/批、多账号轮转、
    批间3-6秒、跳过已查过的词；中断时已完成批次已入库，报告照常生成。
    分页按"我分析的ASIN"（CL任务种子），页内按竞品ASIN分区域标注广告词归属。
    kw_miner 数据只做指标补位（enrich），不计入覆盖来源数（不影响分层/综合分）。"""
    min_vol = int((req or {}).get("min_vol") or 500)
    scope = ((req or {}).get("scope") or "all").lower()
    if scope not in ("all", "current"):
        scope = "all"
    backfill = bool((req or {}).get("backfill"))
    if scope == "current" and not S["asins"]:
        raise HTTPException(400, "当前没有分析中的 ASIN，请先完成第③步提取")
    job_id = f"rep{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0, "total": 0,
                         "phase": "漏斗合并", "detail": "", "pct": 0, "type": "report"}
    asyncio.create_task(run_ads_report(job_id, min_vol, scope, backfill))
    return {"job_id": job_id}


async def run_ads_report(job_id: str, min_vol: int, scope: str, backfill: bool):
    job = S["jobs"][job_id]
    try:
        imported_all = local_db.ads_all()
        km_rows = [r for g in imported_all if g.get("source") == "kw_miner" for r in g["rows"]]
        imported = [g for g in imported_all if g.get("source") != "kw_miner"]
        harvest_all = local_db.ads_kw_full_all()
        if scope == "current":
            cur = {a.strip().upper() for a in S["asins"]}
            harvest = adv.full_rows_to_funnel(
                [g for g in harvest_all if str(g.get("source_asin") or "").upper() in cur])
            imported = []   # 仅当前分析：只看当前竞品的广告词，导入文件不混入
        else:
            harvest = adv.full_rows_to_funnel(harvest_all)
        brands_ = sorted({(p.get("brand") or "").strip().lower()
                          for p in local_db.latest() if p.get("brand")})

        def _funnel():
            return adv.funnel(imported + harvest, min_vol=min_vol, brands=brands_, enrich_rows=km_rows)

        job["log"].append(f"[i] 漏斗合并：⚡抓取 {len(harvest)} 竞品 + ①导入 {len(imported)} 来源"
                          f" + kw_miner 官方 {len(km_rows)} 行（补位不计覆盖）")
        f = _funnel()
        if not f["kept"]:
            raise RuntimeError("当前分析的竞品还没有广告词数据，请先运行 ⚡自动抓取" if scope == "current"
                               else "本地库还没有广告关键词数据：点 ⚡自动抓取（结果直接入库），"
                                    f"或把导出文件放入 {adv.ADS_DIR.name}/ 后点①导入")
        job["log"].append(f"[+] 保留 {len(f['kept'])} 词（阈值 {min_vol}，过滤 {f['dropped']}）")

        # 链式补齐：全体保留词（综合分降序、品牌词沉底）→ 跳过已查询 → 官方指标 → 重新漏斗
        bf = None
        if backfill:
            candidates = [a["keyword"] for a in f["kept"]]
            done = local_db.kw_miner_done(candidates)
            todo = [k for k in candidates if k.lower() not in done]
            bf = {"candidates": len(candidates), "already_have": len(done), "todo": len(todo),
                  "batches": (len(todo) + 19) // 20, "done": 0, "saved": 0}
            job["log"].append(f"[i] 官方指标补齐：候选 {len(candidates)} 词，已查过 {len(done)}，"
                              f"本次查询 {len(todo)} 词 / {bf['batches']} 批（多账号轮转）")
            if todo:
                job["phase"], job["total"] = "官方指标补齐", bf["batches"]

                def on_batch(batch_rows, label):
                    bf["saved"] += local_db.save_ads("kw_miner", "US", batch_rows)
                    bf["done"] += 1
                    job["done"] = bf["done"]
                    job["pct"] = round(bf["done"] / max(bf["batches"], 1) * 90)
                    job["detail"] = f"{label}号 · 累计入库 {bf['saved']} 行"
                    job["log"].append(f"[补齐] 批次 {bf['done']}/{bf['batches']}（{label}号）"
                                      f"完成，累计 {bf['saved']} 行")
                try:
                    await _kw_miner_fetch(todo, on_batch=on_batch)
                except Exception as e:
                    bf["error"] = str(e)
                    job["log"].append(f"[!] 补齐中断: {e}（已完成批次已入库，下次自动跳过）")
                km_rows = [r for g in local_db.ads_all()
                           if g.get("source") == "kw_miner" for r in g["rows"]]
                f = _funnel()
                job["log"].append(f"[+] 补齐后重新漏斗：保留 {len(f['kept'])} 词"
                                  f"（官方指标已随合并进入报告）")

        if not f["kept"]:
            raise RuntimeError(f"阈值 {min_vol} 下 {f['total_kw']} 个词全部被过滤，请调低阈值")

        job["phase"] = "生成 Excel"
        fp = OUT_DIR / f"ads_report_{scope}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        meta = {"min_vol": min_vol, "total_kw": f["total_kw"], "dropped": f["dropped"],
                "scope": "仅当前分析的ASIN" if scope == "current" else "全部历史数据",
                "sources": f"⚡自动抓取 {len(harvest)} 个竞品 + ①导入 {len(imported)} 个来源"}
        make_report.build_ads_report(f["kept"], meta, str(fp),
                                     page_groups=_build_page_groups(f["kept"], _seed_map()))
        S["last_report"] = str(fp)
        job["xlsx"] = str(fp)
        job["result"] = {"ok": True, "scope": scope, "kept": len(f["kept"]), "dropped": f["dropped"],
                         "total_kw": f["total_kw"], "harvest_asins": len(harvest),
                         "imported_sources": len(imported), "backfill": bf, "file": fp.name}
        job["pct"], job["detail"] = 100, "完成"
        job["status"] = "done"
    except Exception as e:
        job["log"].append(f"[x] {type(e).__name__}: {e}")
        job["status"] = "error"

@app.post("/api/ads/clean")
async def ads_clean(req: dict = None):
    """广告词价值排序与清洗：价值函数 v2（多因子：购买体量/购买转化/需求规模/广告验证/市场定价/
    供需空间，官方 ARA 数据参与评分），品牌词按本地库品牌表数据驱动分层，不剔除任何词"""
    min_searches = int((req or {}).get("min_searches") or 0)
    grouped = local_db.ads_kw_full_all()
    rows = []
    for g in grouped:
        for r in g["rows"]:
            r["_asin"] = g["source_asin"]
            rows.append(r)
    if not rows:
        raise HTTPException(400, "本地库还没有广告词数据，请先运行自动抓取")
    brands = sorted({(p.get("brand") or "").strip().lower()
                     for p in local_db.latest() if p.get("brand")})
    official = local_db.official_metrics()
    kept, stat = adv.relevance_filter(rows, min_searches=min_searches, brands=brands,
                                      official=official)
    fp = OUT_DIR / f"ads_keywords_clean_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    make_report.build_clean_report(kept, str(fp))   # 总览 + 按来源ASIN分页
    S["last_report"] = str(fp)
    return {"ok": True, "kept": len(kept), "total": len(rows), **stat, "file": fp.name}

# ---------- ⑥-B 全自动广告抓取（组结构+关键词收割） ----------

async def run_ads_auto(job_id: str, limit: int, top_groups: int, weeks_budget: int):
    job = S["jobs"][job_id]
    snaps = local_db.latest(S["asins"] or None)
    ranked = sorted(snaps, key=lambda r: -(r.get("totalUnits") or 0))[:limit]
    if not ranked:
        job["log"].append("[x] 本地库没有产品数据，请先完成第②③④步")
        job["status"] = "error"
        return
    job["total"] = len(ranked)
    page_vip = await get_vip_page()
    page_main = await get_main_page()
    kw_page_choice = {"vip": 0, "main": 0}   # 统计哪种账号能拉关键词
    dead = {"main": False, "vip": False}     # 会话失效的账号自动拉黑

    # ===== 阶段A（VIP·轻）：只拉组结构，每个 ASIN 一次调用，秒级完成（占进度前10%） =====
    vip_alive = True
    for i, s in enumerate(ranked, 1):
        asin = s.get("asin")
        job["phase"] = "A·组结构"
        job["detail"] = f"{i}/{len(ranked)}"
        job["pct"] = round(i / len(ranked) * 10)
        job["log"].append(f"—— [A {i}/{len(ranked)}] {asin} 拉组结构（VIP·轻）——")
        if vip_alive:
            try:
                r = await adv.fetch_ads_groups(page_vip, asin)
                local_db.save_groups(asin, r)
                job["log"].append(f"[+] 组结构已缓存: {r.get('adCount')} 组 / "
                                  f"关键词上限 {r.get('maxKeywordsCount')}")
                job["done"] = i
                await asyncio.sleep(1)
                continue
            except adv.SessionExpired as e:
                vip_alive = False
                job["log"].append(f"[!] VIP 会话失效: {e}")
                job["log"].append("[i] 已缓存的组结构不受影响；VIP 层未完成的 ASIN 将跳过")
            except Exception as e:
                job["log"].append(f"[x] {asin} 组结构失败: {type(e).__name__}: {e}")
        else:
            job["log"].append(f"[skip] {asin} （VIP 已失效，无法拉组结构）")
        job["done"] = i

    # ===== 阶段B（重）：关键词收割，优先主账号，失败自动回退 VIP =====
    targets = []
    for s in ranked:
        asin = s.get("asin")
        gs = local_db.groups_for(asin)
        if not gs:
            continue
        # 组排序：按投放热度（各周 keywordsCount 之和）取前 top_groups 个
        gs = sorted(gs, key=lambda g: -sum((h.get("keywordsCount") or 0)
                                           for h in (g.get("history") or [])))[:top_groups]
        targets.append((asin, gs))
    # 预计"组×周"总调用数：每组取 min(活跃周数, 周预算)，与收割循环的实际周数口径一致
    # （此前误按组数估算，units 却按周递增，导致进度 >100%）
    est_units = sum(min(len([h for h in (g.get("history") or [])
                            if (h.get("keywordsCount") or 0) > 0]), weeks_budget)
                    for _, gs in targets for g in gs)
    job["plan"] = {"asins": len(ranked), "groups": sum(len(t[1]) for t in targets),
                   "weeks_budget": weeks_budget, "est_calls": est_units}
    job["phase"] = "B·关键词收割"
    job["units"], job["pct"] = 0, 10
    job["log"].append(f"[i] 阶段B：{len(targets)} 个 ASIN / {sum(len(t[1]) for t in targets)} 个组"
                      f"（按投放热度取前 {top_groups}），周预算 {weeks_budget} + 边际停止规则"
                      f"，预计 ≤ {est_units} 次调用")

    async def kw_fetch(asin, ad_id, b, e):
        """账号熔断切换：主账号失败→VIP；主账号会话失效→拉黑主账号改用 VIP；
        两个账号都失效才抛异常中止。每周只取前 2 页（100 词），交给价值函数排序。"""
        attempts = []
        if not dead["main"]:
            attempts.append("main")
        if vip_alive and not dead["vip"]:
            attempts.append("vip")
        last_err = None
        for who in attempts:
            try:
                rows = await adv.fetch_group_keywords(page_main if who == "main" else page_vip,
                                                      ad_id, b, e, max_pages=2)
                kw_page_choice[who] += 1
                return rows, who
            except adv.SessionExpired as e1:
                dead[who] = True
                last_err = e1
                job["log"].append(f"[!] {who}号 会话失效，拉黑该账号"
                                  f"（剩余可用: {', '.join(a for a in ('main', 'vip') if not dead[a]) or '无'}）")
            except Exception as e1:
                last_err = e1
                if who == "main" and vip_alive and not dead["vip"]:
                    continue   # 主账号其他错误 → 回退 VIP
                raise
        raise last_err or RuntimeError("无可用账号")

    total_kw = 0
    official = local_db.official_metrics()   # 官方指标参与收割时的价值函数 v2 评分
    for ai_, (asin, gs) in enumerate(targets, 1):
        for gi, g in enumerate(gs, 1):
            total = g.get("totalCount") or 0
            hist = sorted(g.get("history") or [], key=lambda h: h.get("beginDate") or 0)
            weeks = sorted([h for h in hist if (h.get("keywordsCount") or 0) > 0],
                           key=lambda h: -(h.get("keywordsCount") or 0))[:weeks_budget]
            seen = {}
            prev_n = 0
            for wi, w in enumerate(weeks, 1):
                job["detail"] = f"正在第{ai_}/{len(targets)}个竞品 · 组{gi}/{len(gs)} · 周{wi}/{len(weeks)}"
                job["units"] += 1
                job["pct"] = min(100, 10 + round(90 * job["units"] / max(est_units, 1)))
                try:
                    rows, used = await kw_fetch(asin, g["adId"], w["beginDate"], w["endDate"])
                except adv.SessionExpired as e:
                    job["log"].append(f"[x] {e} —— 中止；已收割数据已入库")
                    job["log"].append(f"[i] 关键词账号使用统计: 主账号×{kw_page_choice['main']} "
                                      f"VIP×{kw_page_choice['vip']}")
                    job["status"] = "error"
                    return
                except Exception as e:
                    job["log"].append(f"[!] {asin} 组{gi} 周{wi}: {type(e).__name__}: {e}")
                    await asyncio.sleep(2)
                    continue
                # 价值函数 v2：本周词按 价值分 取 Top10（不全量拉取；官方 ARA 指标参与评分）
                top = adv.value_top(rows, 10, official=official)
                new_words = [str(k.get("keyword")) for _, k in top]
                for _, k in top:
                    seen.setdefault(str(k.get("id") or k.get("keyword")), k)
                gain = len(seen) - prev_n
                prev_n = len(seen)
                job["log"].append(f"    [{ai_}/{len(targets)}] 组{gi} 周{wi}/{len(weeks)} "
                                  f"（{w.get('keywordsCount')}词/周，{used}号，实取{len(rows)}）"
                                  f" 取Top10 累计 {len(seen)}（新增 {gain}）"
                                  f" Top3: {' / '.join(new_words[:3])}")
                # 边际收益停止：本周新增 < max(1, 累计10%) 时放弃剩余周
                if prev_n and gain < max(1, prev_n * 0.10):
                    job["log"].append(f"    [i] 组{gi} 边际收益不足（新增 {gain}），停止该组剩余周")
                    break
                if total and len(seen) >= total:
                    break
                await asyncio.sleep(1)
            # 每组收割完即入库（断点可续）
            n = local_db.save_ads_full(asin, {"results": [{
                "adId": g["adId"], "campaignId": g.get("campaignId"),
                "组词数": total, "已覆盖词数": len(seen), "keywords": list(seen.values())}]})
            total_kw += n
        job["done"] = ai_
    job["log"].append(f"[+] 全部完成：收割关键词 {total_kw} 条。"
                      f"账号使用统计: 主账号×{kw_page_choice['main']} VIP×{kw_page_choice['vip']}")
    job["log"].append("[i] 广告词已入 ads_kw_full 表，可直接生成广告报告")
    job["pct"], job["detail"] = 100, "完成"
    job["status"] = "done"

@app.post("/api/ads/auto")
async def ads_auto(req: dict):
    if not S["asins"]:
        raise HTTPException(400, "请先完成第③步提取 ASIN")
    limit = int(req.get("limit") or 10)
    top_groups = int(req.get("top_groups") or 4)    # 按投放热度取前4组（控量）
    weeks_budget = int(req.get("weeks_budget") or 3)  # 周预算+边际停止规则
    if limit <= 0: limit = 10
    if top_groups <= 0: top_groups = 4
    if weeks_budget <= 0: weeks_budget = 3
    job_id = f"ads{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0,
                         "total": 0, "rows": 0, "xlsx": None,
                         "phase": "准备中", "detail": "", "pct": 0, "plan": None}
    asyncio.create_task(run_ads_auto(job_id, limit, top_groups, weeks_budget))
    return {"job_id": job_id}

# ---------- ⑦ 关键词补齐（关键词挖掘批量分析 · 官方购买率/月购买量） ----------

async def _kw_accounts():
    """可用主账号列表 [(label, page)]：主账号 + 账号池，失效拉黑的槽位跳过。"""
    accts = []
    if STATE_FILE.exists() and "main" not in S["pool_dead"]:
        try:
            accts.append(("main", await get_main_page()))
        except Exception:
            pass
    if POOL_DIR.exists():
        for f in sorted(POOL_DIR.glob("main_*.json")):
            m = re.fullmatch(r"main_(\d+)", f.stem)
            if not m:
                continue
            slot = int(m.group(1))
            if slot in S["pool_dead"]:
                continue
            try:
                accts.append((slot, await get_pool_page(slot)))
            except Exception:
                S["pool_dead"].add(slot)
    return accts


def _mark_dead(label):
    """会话失效拉黑：丢弃缓存页面，后续批次/调用不再使用该账号。"""
    S["pool_dead"].add(label)
    if label == "main":
        S["main_page"] = None
    else:
        S["pool_pages"].pop(label, None)


async def _kw_miner_fetch(kws, market_id=1, on_batch=None):
    """关键词挖掘批量分析核心：20词/批（免费会员上限），多主账号轮转分摊
    （主账号 + 账号池，跨调用连续轮换），批间随机等 3-6 秒；
    某账号会话失效→自动拉黑并换号重试本批，全部失效才报错。返回 关键词×月份 行列表。
    on_batch(batch_rows, label)：每批成功后的回调（用于增量入库/进度上报）。"""
    accts = await _kw_accounts()
    if not accts:
        raise RuntimeError("没有可用的主账号登录态（主账号与账号池均缺失或已失效）——请在控制台登录")
    all_rows = []
    batches = [kws[i:i + 20] for i in range(0, len(kws), 20)]
    for bi, batch in enumerate(batches, 1):
        body = {"keywordList": batch, "market": market_id, "pageNum": 1, "pageSize": 50,
                "historyDate": "", "orderBy": None, "desc": True, "filterRootWord": 0,
                "matchType": 0, "amazonChoice": False, "keywordBidMatchType": "exact"}
        got_rows, last_err, tried = None, None, 0
        while tried < len(accts):
            label, page = accts[S["kw_rr"] % len(accts)]
            S["kw_rr"] += 1
            tried += 1
            try:
                j = await sd.api_post(page, "/v3/api/keyword-miner", body)
                data = j.get("data") if isinstance(j, dict) else None
                if not isinstance(data, dict):
                    raise RuntimeError("返回异常（登录态可能已失效）")
                got_rows = adv.parse_kw_miner_items(data.get("items") or [])
                break
            except RuntimeError as e:
                last_err = e
                if any(x in str(e) for x in ("失效", "登陆", "登录")):
                    _mark_dead(label)
                    accts = [(l, p) for l, p in accts if l != label]
                    tried = 0   # 剩余账号全部重试一遍本批
                    if not accts:
                        raise RuntimeError(f"全部主账号会话失效（{e}）——请重新登录")
                await asyncio.sleep(1)
        if got_rows is None:
            raise last_err or RuntimeError("关键词挖掘查询失败")
        all_rows.extend(got_rows)
        if on_batch:
            on_batch(got_rows, label)
        if bi < len(batches) and accts:
            await asyncio.sleep(random.uniform(3, 6))
    return all_rows


@app.post("/api/kwminer")
async def kw_miner_lookup(req: dict):
    """批量查询关键词挖掘接口，把官方 20 字段（含 月购买量/购买率/ABA排名/需供比）入库。
    source=kw_miner 存入 ads_keywords 表，②广告分析报告的逐字段合并逻辑自动补位。
    主账号执行；免费会员每次批量上限 20 词，超出自动分批；手动点击触发，批间随机等待。"""
    raw = req.get("keywords") or ""
    kws = [k.strip() for k in re.split(r"[,，;；\n\r]+", raw) if k.strip()]
    seen = set()
    kws = [k for k in kws if not (k.lower() in seen or seen.add(k.lower()))]
    if not kws:
        raise HTTPException(400, "请输入至少一个关键词")
    if len(kws) > 100:
        raise HTTPException(400, f"单次最多 100 词（收到 {len(kws)} 个），请分几次查询")
    market = (req.get("market") or "US").upper()
    market_id = int(req.get("market_id") or 1)   # 1=US（与页面行为一致）
    rows = await _kw_miner_fetch(kws, market_id)
    got_kws = {str(r.get("keyword") or "").lower() for r in rows}
    saved = local_db.save_ads("kw_miner", market, rows)
    missing = [k for k in kws if k.lower() not in got_kws]
    return {"ok": True, "keywords": len(kws), "batches": (len(kws) + 19) // 20,
            "got": len(got_kws), "saved": saved, "missing": missing[:20],
            "note": "数据已入库（来源 kw_miner），重新生成②广告分析报告即自动补齐官方指标"}

# ---------- ⑧ 维护 ----------

@app.post("/api/cleanup")
async def cleanup(req: dict = None):
    """清理历史无效资源：
    ① deep_results/ 下的历史 Excel 报告（styled_report_/ads_report_/ads_keywords_clean_/deep_dive_），
       每类保留最新 keep_reports 份；
    ② ads_debug/ 与 lookup_debug/ 的接口调试转储（全部）；
    ③ deep_results/ 下的 debug_cl_*.png 现场截图（全部）。
    保留：asins_*.json（报告分页依赖的"我分析的ASIN"映射）、数据库、登录态。"""
    keep = int((req or {}).get("keep_reports") or 3)
    dry = bool((req or {}).get("dry_run"))
    freed = 0
    n_files = 0
    detail = {}

    def _rm(p: Path):
        nonlocal freed, n_files
        try:
            freed += p.stat().st_size
            n_files += 1
            if not dry:
                p.unlink()
        except Exception:
            pass

    # ① 历史报告：按前缀分组，各组保留最新 keep 份
    prefixes = ("styled_report_", "ads_report_", "ads_keywords_clean_", "deep_dive_")
    rep_rm = 0
    if OUT_DIR.exists():
        for pre in prefixes:
            files = sorted(OUT_DIR.glob(f"{pre}*.xlsx"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[max(keep, 0):]:
                _rm(p)
                rep_rm += 1
    # ② 调试转储
    dbg_rm = 0
    for d in (HERE / "ads_debug", HERE / "lookup_debug"):
        if d.exists():
            for p in d.iterdir():
                if p.is_file():
                    _rm(p)
                    dbg_rm += 1
    # ③ 现场截图
    png_rm = 0
    if OUT_DIR.exists():
        for p in OUT_DIR.glob("debug_cl_*.png"):
            _rm(p)
            png_rm += 1
    return {"ok": True, "dry_run": dry, "files": n_files,
            "freed_mb": round(freed / 1048576, 1),
            "detail": {"旧报告": rep_rm, "调试转储": dbg_rm, "截图": png_rm}}

# ---------- 页面 ----------

@app.get("/")
async def index():
    if WEB_INDEX.exists():
        return FileResponse(WEB_INDEX, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "web_index.html 缺失"}, status_code=500)

if __name__ == "__main__":
    print("=" * 55)
    print("  SellerSprite Deep-Dive Web 控制台  http://127.0.0.1:8765")
    print("=" * 55)
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
