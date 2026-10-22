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
from datetime import date, datetime
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
import amazon_patrol as amz
import tracking_store as track_store
import tracking_import as track_import
import feta.config as feta_config
import feta.repository as feta_repo
import feta.service as feta_service

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
        if "/user/login" in page.url:   # 被踢到登录页 = 会话失效，fail-fast
            raise RuntimeError("主账号登录态已失效——请在控制台重新登录主账号")
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
    "amazon_job_id": None,     # 独立巡查任务，不复用卖家精灵当前 ASIN/账号
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

async def _check_login(page, role):
    """页面登录态快速校验：失效时关闭坏页面并抛出带指引的异常（fail-fast，避免后续 40s 超时）。"""
    try:
        r = await page.evaluate(sd.FETCH_GET, "/v3/api/client")
    except Exception as e:
        raise RuntimeError(f"{role}页面校验失败: {e}")
    d = (r or {}).get("j") or {}
    data = d.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        try:
            await page.context.close()
        except Exception:
            pass
        raise RuntimeError(f"{role}登录态已失效——请在控制台重新登录{role}后重试")


async def get_main_page():
    if S["main_page"] is None:
        browser = await get_browser()
        ctx = await browser.new_context(storage_state=str(STATE_FILE))
        ctx.set_default_timeout(30_000)
        page = await ctx.new_page()
        await page.goto(f"{BASE}/v3/ai-history?module=CL", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await _check_login(page, "主账号")   # 失效则抛错，坏页面不缓存
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
        await _check_login(page, "会员账号")
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


def _scope_label(scope="current"):
    """输出文件名的 [ASIN/ALL] 段：当前分析 → 我分析的ASIN（未知则 ASIN）；全局 → ALL。"""
    if scope == "global":
        return "ALL"
    return (S.get("current_seed") or "ASIN").upper()

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
    try:
        page = await get_main_page()
        tasks = await sd.fetch_history(page, pages=8)
    except Exception as e:
        if any(x in str(e) for x in ("失效", "登陆", "登录")):
            reset_ctx("main")   # 坏页面自愈：下次调用用存储的登录态重建
        raise HTTPException(400, str(e))
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
    seen_in = set()
    asins = [a for a in S["asins"] if not (a.strip().upper() in seen_in or seen_in.add(a.strip().upper()))]
    if len(asins) < len(S["asins"]):
        job["log"].append(f"[i] 输入去重: {len(S['asins'])} → {len(asins)} 个 ASIN")
    job["total"] = len(asins)
    page = await get_vip_page()
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
            # 变体家族展开去重（数据端后处理）：同批内同一 ASIN 的代表行只保留一条
            uniq = {}
            for r in rows:
                uniq.setdefault(str(r.get("asin") or "").strip().upper(), r)
            dup_n = len(rows) - len(uniq)
            rows = list(uniq.values())
            if dup_n:
                job["log"].append(f"[i] 变体展开去重: {len(rows) + dup_n} → {len(rows)} 行（移除 {dup_n} 行同 ASIN 重复）")
            all_rows.extend(rows)
            got = set(uniq.keys())
            missing = [a for a in batch if a.upper() not in got]
            job["log"].append(f"[+] 本批返回 {len(rows)} 行（请求 {len(batch)} 个，缺数据 {len(missing)} 个）")
            if missing:
                job["log"].append(f"[!] 无数据: {', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}")
        except Exception as e:
            job["log"].append(f"[x] 批次失败: {type(e).__name__}: {e}")
        job["done"] = min(bi * chunk, len(asins))
        await asyncio.sleep(1.5)
    if all_rows:
        fp = OUT_DIR / f"{_scope_label('current')}_deepdive_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        n, k = sd.export_excel(all_rows, fp)
        job["xlsx"] = str(fp)
        job["log"].append(f"[+] 导出完成: {fp.name} ({n} 行 x {k} 列)")
        try:
            local_db.save_batch(job_id, S["current_task"], len(asins), all_rows)
            S["last_rows"] = all_rows
            S["last_rows_task"] = S["current_task"]   # 标记数据归属，供⑤"当前分析"范围校验
            job["log"].append("[+] 已写入本地数据库 deepdive.db（可在第4步生成美化报告）")
        except Exception as e:
            job["log"].append(f"[!] 本地数据库写入失败: {e}")
    job["status"] = "done" if all_rows else "error"
    job["rows"] = len(all_rows)

# ---------- AI 找竞品（CL 提交 + 轮询） ----------

async def wait_cl_task(page, tid, job, timeout_s=900):
    """轮询 CL 任务状态直到完成；返回最终状态或 None(超时)/"POLL_ERROR"(页面卡死)。
    每次轮询强制 15s 超时——page.evaluate 本身无超时，页面卡死会永久挂起。"""
    path = f"/v3/api/ai-analysis/task/original/summary/{tid}"
    t0, last_log, err_streak = time.time(), -1, 0
    while time.time() - t0 < timeout_s:
        try:
            j = await asyncio.wait_for(sd.api_get(page, path), timeout=15)
            err_streak = 0
            t = (j.get("data") or [{}])[0]
            st = t.get("taskStatus")
            if st and st != "SUBMITTED":
                return st
            elapsed = int(time.time() - t0)
            if elapsed - last_log >= 60:
                job["log"].append(f"[+] 任务 {tid[:12]}… 分析中 {elapsed}s（预计5-8分钟）")
                last_log = elapsed
        except Exception:
            err_streak += 1
        if err_streak >= 8:   # 连续 ~2.5 分钟轮询失败/超时 = 页面卡死，不再干等
            job["log"].append("[x] 轮询连续超时/失败（页面可能卡死），放弃等待该任务")
            return "POLL_ERROR"
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
        try:
            asins2, ai_rows = await asyncio.wait_for(sd.extract_asins(page, tid), timeout=120)
        except asyncio.TimeoutError:
            job["log"].append("[!] 提取竞品超时（任务已完成，可稍后在历史任务里手动提取）")
            asins2, ai_rows = [], []
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
    # 启动后首次先加载磁盘缓存
    if S.get("cl_options") is None and OPTS_FILE.exists():
        try:
            S["cl_options"] = json.loads(OPTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    cached = S.get("cl_options")
    # 页面获取 / 面板切换 fail-fast：会话失效立即返回明确原因（缓存兜底），不再傻等 40s 超时
    try:
        page = await get_main_page()
        panel_ok = await goto_panel(page, "submit")
    except Exception as e:
        if any(x in str(e) for x in ("失效", "登陆", "登录")):
            reset_ctx("main")   # 坏页面自愈：下次调用用存储的登录态重建
        note = str(e)
        if cached:
            return {**cached, "note": note + "（本次为缓存值）"}
        return {"site": [], "count": [], "note": note}
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
    # 手动 ASIN 模式：直接粘贴自有/零散 ASIN，查询并入本地库（批次归属 manual-<ts>）
    manual = req.get("asins")
    if manual:
        raw = manual if isinstance(manual, str) else ",".join(str(x) for x in manual)
        seen, manual_list = set(), []
        for a in re.split(r"[,，\s]+", raw):
            a = a.strip().upper()
            if not a:
                continue
            if not re.fullmatch(r"[A-Z0-9]{10}", a):
                raise HTTPException(400, f"ASIN 格式不对: {a}（应为 10 位字母数字，如 B0H4W51ZMH）")
            if a not in seen:
                seen.add(a)
                manual_list.append(a)
        if not manual_list:
            raise HTTPException(400, "手动 ASIN 解析后为空")
        S["asins"] = manual_list
        S["current_task"] = f"manual-{datetime.now():%Y%m%d_%H%M%S}"
        S["current_seed"] = None
        S["current_market"] = "US"
    asins = S["asins"]
    if not asins:
        raise HTTPException(400, "请先选择历史任务并提取 ASIN，或在④卡片粘贴 ASIN 手动查询")
    chunk = int(req.get("chunk") or 20)
    if chunk <= 0:
        chunk = 20
    job_id = f"job{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0,
                         "total": len(asins), "rows": 0, "xlsx": None}
    if manual:
        S["jobs"][job_id]["log"].append(
            f"[i] 手动 ASIN 模式：{len(asins)} 个（{S['current_task']}），查询后入库 deepdive.db")
    asyncio.create_task(run_lookup(job_id, chunk))
    return {"job_id": job_id, "mode": "manual" if manual else "task",
            "asins": len(asins), "label": S["current_task"]}

# ---------- 监控名单（④ 例跑积累趋势：自家 ASIN + 类目 TOP10 + 手动补充） ----------

WATCHLIST_FILE = OUT_DIR / "watchlist.json"
BSR_CFG_FILE = OUT_DIR / "bsr_api.json"   # 类目BSR榜接口配置（probe 捕获），{类目名: cfg}
DEFAULT_CATEGORIES = [
    {"name": "便携电站", "tokens": ["power station", "solar generator"], "bsr_n": 50},
    {"name": "太阳能板", "tokens": ["solar panel"], "bsr_n": 50},
    {"name": "户外三防手机", "tokens": ["rugged phone"], "bsr_n": 50},
]
BSR_N_DEFAULT = 50   # 边际收益测算：41-50 名边际仍占 4-7%，51 名起降至 <5% 且逐段下滑


def _load_bsr_cfg():
    if BSR_CFG_FILE.exists():
        try:
            return json.loads(BSR_CFG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_bsr_cfg(cfg):
    BSR_CFG_FILE.parent.mkdir(exist_ok=True)
    BSR_CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_watchlist():
    if WATCHLIST_FILE.exists():
        try:
            wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            wl = None
        if isinstance(wl, dict):
            wl.setdefault("manual", [])
            wl.setdefault("categories", json.loads(json.dumps(DEFAULT_CATEGORIES)))
            wl.setdefault("bsr_cache", {})
            wl.setdefault("blocked", [])
            wl.setdefault("own_phones", [])
            for c in wl["categories"]:   # 旧文件迁移：补 bsr_n
                c.setdefault("bsr_n", BSR_N_DEFAULT)
            return wl
    wl = json.loads(json.dumps(DEFAULT_CATEGORIES))
    wl = {"manual": [], "categories": wl, "bsr_cache": {}, "own_phones": []}
    WATCHLIST_FILE.parent.mkdir(exist_ok=True)
    WATCHLIST_FILE.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    return wl


def _save_watchlist(wl):
    WATCHLIST_FILE.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")


def _own_source(title):
    """自家 ASIN 细分来源：电站/太阳能板/手机/其他（便于按线屏蔽）。"""
    t = str(title or "").lower()
    if "solar panel" in t or "solar" in t and "panel" in t:
        return "自家·太阳能板"
    if "power station" in t or "generator" in t:
        return "自家·电站"
    if "phone" in t or "smartphone" in t or "mobile" in t:
        return "自家·手机"
    return "自家·其他"


def _watchlist_all_items():
    """全量名单（未应用屏蔽）：资料在售手机 + 数据库自家产品 + 类目榜 + 手动补充。"""
    wl = _load_watchlist()
    rows = local_db.latest()
    own = [r for r in rows if "fossibot" in str(r.get("brand", "")).lower()]
    curated, curated_seen = [], set()
    for entry in (wl.get("own_phones") or []):
        if not isinstance(entry, dict):
            continue
        asin = str(entry.get("asin") or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9]{10}", asin) and asin not in curated_seen:
            curated_seen.add(asin)
            curated.append({**entry, "asin": asin})
    own_set = {str(r.get("asin")).strip().upper() for r in own} | curated_seen
    items, seen = [], set()

    def add(asin, source, note=""):
        a = str(asin or "").strip().upper()
        if a and re.fullmatch(r"[A-Z0-9]{10}", a) and a not in seen:
            seen.add(a)
            items.append({"asin": a, "source": source, "note": note})

    # 先加明确在售清单：优先于数据库自动分类，防止旧“自家·手机”屏蔽将其过滤。
    for entry in curated:
        model = str(entry.get("model") or "FOSSIBOT 手机").strip()
        checked_at = str(entry.get("source_checked_at") or "资料日期未注明").strip()
        note = f"{model} · Amazon US 在售（资料核验 {checked_at}）"
        add(entry["asin"], "自家·手机（在售清单）", note)
    for r in own:
        add(r.get("asin"), _own_source(r.get("title")), str(r.get("title") or "")[:40])
    cache = wl.get("bsr_cache") or {}
    for cat in wl.get("categories", []):
        name = cat.get("name") or "?"
        n = int(cat.get("bsr_n") or BSR_N_DEFAULT)
        live = cache.get(name) or {}
        live_items = live.get("items") or []
        if live.get("asins"):
            for it in live_items:
                add(it.get("asin"), f"BSR榜·{name}", str(it.get("title") or "")[:40])
            continue
        toks = [t.lower() for t in cat.get("tokens", []) if t]
        cand = [r for r in rows
                if str(r.get("asin")).upper() not in own_set
                and any(t in str(r.get("title", "")).lower() for t in toks)]
        cand.sort(key=lambda r: -(r.get("totalUnits") or 0))
        for r in cand[:n]:
            add(r.get("asin"), f"TOP·{name}", str(r.get("title") or "")[:40])
    for m in wl.get("manual", []):
        if isinstance(m, dict):
            add(m.get("asin"), "手动", m.get("note") or "")
        else:
            add(m, "手动")
    own_n = sum(1 for i in items
                if i["source"] == "自家" or i["source"].startswith("自家·"))
    return items, own_n


def _watchlist_items():
    """应用屏蔽后的名单（屏蔽的来源不参与④查询/例跑）。"""
    wl = _load_watchlist()
    blocked = set(wl.get("blocked") or [])
    all_items, _ = _watchlist_all_items()
    items = [i for i in all_items if i["source"] not in blocked]
    own_active = sum(1 for i in items
                     if i["source"] == "自家" or i["source"].startswith("自家·"))
    return items, own_active


@app.get("/api/watchlist")
async def watchlist_get():
    bcfg = _load_bsr_cfg()
    wl = _load_watchlist()
    blocked = set(wl.get("blocked") or [])
    all_items, own_n = _watchlist_all_items()
    items = [i for i in all_items if i["source"] not in blocked]
    bgroups = {}
    for i in all_items:
        if i["source"] in blocked:
            bgroups[i["source"]] = bgroups.get(i["source"], 0) + 1
    bsr = {name: (v.get("ts") or "") for name, v in (wl.get("bsr_cache") or {}).items()}
    return {"count": len(items), "own": own_n, "items": items,
            "categories": wl.get("categories", []),
            "bsr_ts": bsr, "bsr_configured": sorted(bcfg.keys()),
            "blocked": bgroups}


@app.post("/api/watchlist/block")
async def watchlist_block(req: dict):
    """右键胶囊切换屏蔽：屏蔽的来源不参与④查询/例跑；再右键恢复。"""
    source = str(req.get("source") or "").strip()
    if not source:
        raise HTTPException(400, "source 不能为空")
    wl = _load_watchlist()
    blocked = wl.setdefault("blocked", [])
    if source in blocked:
        blocked.remove(source)
        state = False
    else:
        blocked.append(source)
        state = True
    _save_watchlist(wl)
    return {"ok": True, "source": source, "blocked": state}


@app.post("/api/watchlist/add")
async def watchlist_add(req: dict):
    raw = req.get("asins") or ""
    note = str(req.get("note") or "").strip()
    wl = _load_watchlist()
    have = {str(m.get("asin") if isinstance(m, dict) else m).upper() for m in wl["manual"]}
    added = 0
    for a in re.split(r"[,，\s]+", str(raw)):
        a = a.strip().upper()
        if not a:
            continue
        if not re.fullmatch(r"[A-Z0-9]{10}", a):
            raise HTTPException(400, f"ASIN 格式不对: {a}")
        if a not in have:
            wl["manual"].append({"asin": a, "note": note})
            have.add(a)
            added += 1
    _save_watchlist(wl)
    items, _ = _watchlist_items()
    return {"ok": True, "added": added, "count": len(items)}


@app.post("/api/watchlist/remove")
async def watchlist_remove(req: dict):
    asin = str(req.get("asin") or "").strip().upper()
    wl = _load_watchlist()
    n0 = len(wl["manual"])
    wl["manual"] = [m for m in wl["manual"]
                    if str(m.get("asin") if isinstance(m, dict) else m).upper() != asin]
    _save_watchlist(wl)
    return {"ok": True, "removed": n0 - len(wl["manual"]),
            "note": "仅手动补充项可移除；自家/TOP10 项随库自动计算"}


# ---------- 类目 BSR 实时榜（competitor-lookup 类目模式，非会员可查） ----------

async def _bsr_refresh_all(job_log=None, only_category=None):
    """按已捕获的接口配置，实时拉各类目 BSR 榜 Top bsr_n，写入 watchlist.json 缓存。"""
    wl = _load_watchlist()
    bcfg = _load_bsr_cfg()
    cats = [c for c in wl.get("categories", [])
            if (not only_category or c.get("name") == only_category) and bcfg.get(c.get("name"))]
    if not cats:
        msg = "没有可用的类目接口配置——先点「探测类目BSR接口」捕获一次"
        if job_log:
            job_log.append(f"[!] {msg}")
        return {"ok": False, "note": msg, "summary": msg}
    page = await get_main_page()
    summary, cache = [], wl.setdefault("bsr_cache", {})
    for c in cats:
        name = c.get("name") or "?"
        n = int(c.get("bsr_n") or BSR_N_DEFAULT)
        try:
            rows = await sd.bsr_fetch(page, bcfg[name], n)
            items = [{"asin": str(r.get("asin")).upper(),
                      "title": str(r.get("title") or "")[:80],
                      "units": r.get("totalUnits") if isinstance(r.get("totalUnits"), (int, float)) else None}
                     for r in rows]
            cache[name] = {"ts": datetime.now().isoformat(timespec="seconds"),
                           "asins": [i["asin"] for i in items], "items": items}
            summary.append(f"{name} {len(items)}")
            if job_log:
                job_log.append(f"[+] BSR榜·{name}：实时获取 {len(items)} 个（前3: "
                               f"{', '.join(i['asin'] for i in items[:3])}）")
        except Exception as e:
            summary.append(f"{name} 失败:{type(e).__name__}")
            if job_log:
                job_log.append(f"[x] BSR榜·{name} 获取失败: {type(e).__name__}: {e}")
        await asyncio.sleep(1)
    _save_watchlist(wl)
    return {"ok": True, "summary": " / ".join(summary) or "无"}


@app.post("/api/watchlist/bsr/probe")
async def bsr_probe(req: dict = None):
    """后台任务：打开【可见】浏览器（带已保存登录态）跑类目BSR捕获。
    用户在窗口里像官网一样选类目→查询（可连续多个），关闭窗口或超时即结束。"""
    seconds = int((req or {}).get("seconds") or 300)
    state_file = STATE_FILE if STATE_FILE.exists() else (
        MEMBER_STATE_FILE if MEMBER_STATE_FILE.exists() else None)
    if state_file is None:
        raise HTTPException(400, "没有已保存的登录态——请先在控制台登录主账号或会员账号")
    job_id = f"bsr{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0, "total": 0, "rows": 0,
                         "xlsx": None, "type": "bsr_probe", "pct": 0, "detail": "正在打开可见窗口…"}
    asyncio.create_task(run_bsr_probe(job_id, seconds, state_file))
    return {"job_id": job_id}


async def run_bsr_probe(job_id: str, seconds: int, state_file):
    job = S["jobs"][job_id]
    browser = None
    try:
        pw = await get_pw()
        browser = await pw.chromium.launch(headless=False)   # 捕获需要人工操作，必须可见
        ctx = await browser.new_context(storage_state=str(state_file))
        page2 = await ctx.new_page()
        job["log"].append("[+] 可见窗口已打开：像官网一样选类目→查询，可连续查多个类目")
        job["log"].append("[i] 操作完直接关闭该窗口即结束捕获；不关则最长 5 分钟自动收")
        job["detail"] = "等待你在窗口里查询…"
        results = await sd.probe_bsr_capture(page2, seconds=seconds)
        if not results:
            job["log"].append("[x] 未捕获到类目查询接口——至少完成一次类目查询再关窗口")
            job["status"] = "error"
            return
        cfg = _load_bsr_cfg()
        wl = _load_watchlist()
        cat_names = [c.get("name") for c in wl.get("categories", [])]
        auto_map = [("便携电站", ("generator", "power station", "portable power")),
                    ("太阳能板", ("solar panel", "solar")),
                    ("户外三防手机", ("cell phone", "phone", "mobile"))]
        for k in [k for k in cfg if k.startswith("capture")]:
            del cfg[k]   # 清理旧版未解析标签的脏键
        out = []
        for r in results:
            entry = {k: r[k] for k in ("method", "url", "post_data", "captured_at")}
            lab = r["label"].lower()
            mapped = next((cn for cn, kws in auto_map
                           if cn in cat_names and any(k in lab for k in kws)), None)
            key = mapped or r["label"]
            cfg[key] = entry
            out.append({"label": r["label"], "category": mapped, "rows": r["rows"],
                        "sample": r["sample"]})
        _save_bsr_cfg(cfg)
        job["captures"] = out
        job["status"] = "done"
        job["log"].append(f"[+] 捕获 {len(out)} 个类目榜："
                          + "；".join(f"{c['label'][:40]}({c['rows']}行)→{c['category'] or '待绑定'}"
                                      for c in out))
    except Exception as e:
        job["log"].append(f"[x] 捕获异常: {type(e).__name__}: {e}")
        job["status"] = "error"
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


@app.get("/api/bsr/sources")
async def bsr_sources_list():
    """长期 Amazon BSR 榜单源列表（含最近一次抓取时间与 ASIN 数）。"""
    cfg = _load_routine()
    wl = _load_watchlist()
    cache = wl.get("bsr_cache") or {}
    items = []
    for e in cfg.get("amz_bsr_urls") or []:
        cat = e.get("category")
        c = cache.get(cat) or {}
        items.append({"category": cat, "url": e.get("url"), "n": e.get("n") or 50,
                      "count": len(c.get("asins") or []), "ts": c.get("ts") or ""})
    return {"items": items}


@app.post("/api/bsr/sources/add")
async def bsr_sources_add(req: dict):
    url = str(req.get("url") or "").strip()
    category = str(req.get("category") or "").strip()
    n = int(req.get("n") or 50)
    if not re.match(r"^https://www\.amazon\.com/", url) or ("bestsellers" not in url and "zgbs" not in url):
        raise HTTPException(400, "URL 需是 amazon.com 类目 BSR 榜单页（含 bestsellers/zgbs）")
    if not category:
        raise HTTPException(400, "备注名称（类目名）不能为空")
    cfg = _load_routine()
    lst = cfg.setdefault("amz_bsr_urls", [])
    if any(e.get("category") == category for e in lst):
        raise HTTPException(400, f"类目「{category}」已存在，如需更新请先删除")
    lst.append({"category": category, "url": url, "n": max(10, min(n, 100))})
    _save_routine(cfg)
    return {"ok": True, "count": len(lst)}


@app.post("/api/bsr/sources/remove")
async def bsr_sources_remove(req: dict):
    category = str(req.get("category") or "").strip()
    cfg = _load_routine()
    n0 = len(cfg.get("amz_bsr_urls") or [])
    cfg["amz_bsr_urls"] = [e for e in cfg.get("amz_bsr_urls", [])
                           if e.get("category") != category]
    _save_routine(cfg)
    return {"ok": True, "removed": n0 - len(cfg["amz_bsr_urls"])}


@app.post("/api/watchlist/bsr/amazon")
async def bsr_amazon(req: dict):
    """从 Amazon 类目 BSR 榜单页（公开榜单）直接提取 ASIN TopN。
    无需会员/插件：榜单页本身按 Amazon 真实 BSR 排序列出 ASIN；
    销量/价格等数据随后由 ④ 按 ASIN 批量查询补齐。"""
    url = str((req or {}).get("url") or "").strip()
    category = str((req or {}).get("category") or "").strip()
    n = int((req or {}).get("n") or 50)
    if not re.match(r"^https://www\.amazon\.com/", url):
        raise HTTPException(400, "请粘贴 amazon.com 的榜单页 URL（gp/bestsellers 或 zgbs）")
    if "bestsellers" not in url and "zgbs" not in url:
        raise HTTPException(400, "URL 需是类目 BSR 榜单页（含 bestsellers/zgbs）")
    if not category:
        raise HTTPException(400, "请填写类目名")
    n = max(10, min(n, 100))
    job_id = f"amz{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0, "total": 0, "rows": 0,
                         "xlsx": None, "type": "amz_bsr", "pct": 0, "detail": "打开榜单页…"}
    asyncio.create_task(run_bsr_amazon(job_id, url, category, n))
    return {"job_id": job_id}


async def _amz_bsr_fetch(url: str, category: str, n: int):
    """打开 Amazon 类目 BSR 榜单页（公开），按页面顺序提取前 n 个 ASIN 并写入名单缓存。"""
    pw = await get_pw()
    browser = await pw.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled"])
    try:
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900}, locale="en-US")
        page2 = await ctx.new_page()
        asins, seen = [], set()
        # 榜单分页：bestsellers 第一页只有 30 个，pg=2/3 续抓到 n 为止
        for pg in range(1, 4):
            u = url if pg == 1 else f"{url}{'&' if '?' in url else '?'}pg={pg}"
            await page2.goto(u, wait_until="domcontentloaded", timeout=60000)
            html = await page2.content()
            if "api-services-support@amazon.com" in html or "captcha" in html.lower():
                raise RuntimeError("Amazon 弹了验证码——稍后重试，或人工打开该 URL 过一次验证再试")
            new = 0
            for a in re.findall(r"/(?:dp|gp/product)/([A-Z0-9]{10})", html):
                if a not in seen:
                    seen.add(a)
                    asins.append(a)
                    new += 1
                if len(asins) >= n:
                    break
            if len(asins) >= n or new == 0:
                break
            await asyncio.sleep(1.5)
        if len(asins) < 10:
            raise RuntimeError(f"榜单页只解析出 {len(asins)} 个 ASIN——页面结构可能变了，把 URL 发我")
        wl = _load_watchlist()
        if not any(c.get("name") == category for c in wl["categories"]):
            wl["categories"].append({"name": category, "tokens": [], "bsr_n": n})
        wl.setdefault("bsr_cache", {})[category] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "asins": asins,
            "items": [{"asin": a, "title": "", "units": None} for a in asins]}
        _save_watchlist(wl)
        return asins
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def run_bsr_amazon(job_id: str, url: str, category: str, n: int):
    job = S["jobs"][job_id]
    try:
        asins = await _amz_bsr_fetch(url, category, n)
        job["rows"] = len(asins)
        job["log"].append(f"[+] {category}：Amazon BSR 实时取到 {len(asins)} 个 ASIN（前5: "
                          f"{', '.join(asins[:5])}）")
        job["log"].append("[i] 名单已更新——点「按名单查询并入库」即可用④管线补齐销量/价格数据")
        job["status"] = "done"
    except Exception as e:
        job["log"].append(f"[x] {type(e).__name__}: {e}")
        job["status"] = "error"


@app.post("/api/watchlist/bsr/assign")
async def bsr_assign(req: dict):
    """把捕获的接口配置绑定到名单类目；类目不存在则自动新建（bsr_n=50）。"""
    label = str(req.get("label") or "").strip()
    category = str(req.get("category") or "").strip()
    if not label or not category:
        raise HTTPException(400, "label/category 不能为空")
    cfg = _load_bsr_cfg()
    if label not in cfg:
        raise HTTPException(404, "捕获记录不存在（可能已绑定过）")
    cfg[category] = cfg.pop(label)
    _save_bsr_cfg(cfg)
    wl = _load_watchlist()
    if not any(c.get("name") == category for c in wl["categories"]):
        wl["categories"].append({"name": category, "tokens": [], "bsr_n": BSR_N_DEFAULT})
        _save_watchlist(wl)
    return {"ok": True, "category": category, "note": "点「刷新实时BSR榜」立即拉取该类目"}


@app.post("/api/watchlist/bsr/refresh")
async def bsr_refresh(req: dict = None):
    res = await _bsr_refresh_all(only_category=(req or {}).get("category"))
    return res

@app.post("/api/watchlist/run")
async def watchlist_run(req: dict = None):
    """把监控名单整体灌入④批量查询（同 run_lookup 管线：快照+Excel+入库）。先尽力刷新实时 BSR 榜。"""
    try:
        await _bsr_refresh_all()   # 已配置接口才生效；失败不阻塞
    except Exception:
        pass
    items, _ = _watchlist_items()
    asins = [i["asin"] for i in items]
    if not asins:
        raise HTTPException(400, "监控名单为空——本地库无数据且无手动补充项")
    label = f"watchlist-{datetime.now():%Y%m%d_%H%M%S}"
    S["asins"] = asins
    S["current_task"] = label
    S["current_seed"] = None
    S["current_market"] = "US"
    chunk = int((req or {}).get("chunk") or 150)
    if chunk <= 0:
        chunk = 150
    job_id = f"job{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0,
                         "total": len(asins), "rows": 0, "xlsx": None}
    S["jobs"][job_id]["log"].append(
        f"[i] 监控名单例跑：{len(asins)} 个 ASIN（{label}）；同 ASIN 重复例跑会累积快照历史，供趋势对比")
    asyncio.create_task(run_lookup(job_id, chunk))
    return {"job_id": job_id, "asins": len(asins), "label": label}

# ---------- 每日例跑（一键按星期排任务：④名单 / ⑦补齐 / CL / ⑥收割 / 周报） ----------

ROUTINE_FILE = OUT_DIR / "routine.json"
ROUTINE_DEFAULTS = {
    "cl_seeds": {   # 周一~周四 = 已知类目刷新；周五 = 新品类探边（手动填种子）；周六日不花 CL 额度
        "Mon": "B0HBPT6N7H",   # F3600 Pro（主战场电站）
        "Tue": "B0BN3VMQRD",   # SP200（太阳能板线）
        "Wed": "B0H4W51ZMH",   # F2400
        "Thu": "B0H4W8F84N",   # F1800
        "Fri": "",             # 新品类探边：在 routine.json 里填一个该品类代表 ASIN
        "Sat": "", "Sun": "",
    },
    "kw_tokens": ["solar", "panel", "power station", "generator"],
    "kw_daily": 100,        # ⑦ 每日补齐词数（20词/批，主账号+账号池轮转）
    "cl_count": "150",      # CL 每次请求的竞品数量档位（额度固定5/次，档位拉满最划算）
    "watchlist_chunk": 150,
    "ads_limit": 5,         # ⑥ 周五收割的竞品数
    "amz_bsr_urls": [],     # Amazon 类目BSR榜单页：[{"category":"便携电站","url":"https://www.amazon.com/gp/bestsellers/...","n":50}]
    "history": {},          # date -> phases 结果（保留最近 30 天）
}


def _load_routine():
    if ROUTINE_FILE.exists():
        try:
            return json.loads(ROUTINE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    ROUTINE_FILE.parent.mkdir(exist_ok=True)
    ROUTINE_FILE.write_text(json.dumps(ROUTINE_DEFAULTS, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return json.loads(json.dumps(ROUTINE_DEFAULTS))


def _save_routine(cfg):
    cfg["history"] = dict(list(cfg.get("history", {}).items())[-30:])
    ROUTINE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def _kw_fill_candidates(tokens, n):
    """从三源合并漏斗里挑缺官方指标的意图词（月搜索量降序）供 ⑦ 补齐。"""
    official = local_db.official_metrics()
    imported_all = local_db.ads_all()
    km_rows = [r for g in imported_all if g.get("source") == "kw_miner" for r in g["rows"]]
    imported = [g for g in imported_all if g.get("source") != "kw_miner"]
    harvest = adv.full_rows_to_funnel(local_db.ads_kw_full_all())
    f = adv.funnel(imported + harvest, min_vol=100,
                   brands=sorted({"jackery", "ecoflow", "bluetti", "renogy", "anker"}))
    toks = [t.lower() for t in (tokens or []) if t]
    cand = [k for k in f["kept"]
            if k.get("_tier") != "品牌词"
            and str(k.get("keyword", "")).lower() not in official
            and (not toks or any(t in str(k.get("keyword", "")).lower() for t in toks))]
    cand.sort(key=lambda k: -(k.get("月搜索量") or 0))
    return [str(k.get("keyword")) for k in cand[:n]]


@app.get("/api/routine/plan")
async def routine_plan():
    cfg = _load_routine()
    today = date.today()
    wd = today.strftime("%a")
    seed = (cfg.get("cl_seeds") or {}).get(wd) or ""
    plan = ["④ 监控名单快照", "⑦ 官方指标补齐（100词）"]
    plan.append(f"CL 找竞品（{seed}）" if seed else "CL：今日无种子，跳过")
    if wd == "Fri":
        plan += ["⑥ 广告收割 TOP 竞品", "周报归档（全部数据 HTML）"]
    return {"date": str(today), "weekday": wd, "cl_seed": seed, "plan": plan,
            "last_run": list((cfg.get("history") or {}).items())[-1:]}


@app.post("/api/routine/run")
async def routine_run():
    job_id = f"routine{int(time.time() * 1000)}"
    S["jobs"][job_id] = {"status": "running", "log": [], "done": 0, "total": 0, "rows": 0,
                         "xlsx": None, "pct": 0, "detail": "排任务中", "type": "routine"}
    asyncio.create_task(run_routine(job_id))
    return {"job_id": job_id}


async def run_routine(job_id: str):
    job = S["jobs"][job_id]
    cfg = _load_routine()
    today = date.today()
    wd = today.strftime("%a")
    phases = {}

    async def phase(name, factory, key, timeout=None):
        job["log"].append(f"—— 阶段 {name} ——")
        sub = f"job{int(time.time() * 1000)}{key}"
        S["jobs"][sub] = {"status": "running", "log": [], "done": 0, "total": 0,
                          "rows": 0, "xlsx": None}
        try:
            if timeout:
                await asyncio.wait_for(factory(sub), timeout=timeout)
            else:
                await factory(sub)
            subj = S["jobs"][sub]
            for ln in (subj.get("log") or [])[-200:]:
                job["log"].append(f"    {ln}")
            st = subj.get("status") or "done"
            phases[name] = {"status": st, "rows": subj.get("rows") or 0}
            job["log"].append(f"[{'+' if st == 'done' else '!'}] 阶段 {name} 结束：{st}"
                              f"（{subj.get('rows') or 0} 行）")
        except asyncio.TimeoutError:
            subj = S["jobs"][sub]
            for ln in (subj.get("log") or [])[-30:]:
                job["log"].append(f"    {ln}")
            try:
                subj["status"] = "cancelled"
            except Exception:
                pass
            job["log"].append(f"[x] 阶段 {name} 超时（{timeout}s）——强制跳过，防止卡死")
            phases[name] = {"status": "timeout"}
        except Exception as e:
            subj = S["jobs"][sub]
            for ln in (subj.get("log") or [])[-30:]:
                job["log"].append(f"    {ln}")
            job["log"].append(f"[x] 阶段 {name} 失败: {type(e).__name__}: {e}")
            phases[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        job["pct"] = min(95, (job["pct"] or 0) + 20)
        job["detail"] = f"已完成 {len(phases)} 个阶段"

    job["log"].append(f"[i] 今日 {today:%Y-%m-%d}（{wd}）例跑开始")

    # ④前：Amazon 类目 BSR 榜单刷新（routine.json amz_bsr_urls 配置的类目，公开榜单低频访问）
    for e in (cfg.get("amz_bsr_urls") or []):
        try:
            asins = await _amz_bsr_fetch(str(e.get("url")), str(e.get("category")),
                                         int(e.get("n") or 50))
            job["log"].append(f"[+] AmazonBSR·{e.get('category')}：{len(asins)} 个 ASIN（实时榜单）")
        except Exception as e2:
            job["log"].append(f"[!] AmazonBSR·{e.get('category')}：{e2}")

    # ④ 监控名单快照（先尽力刷新卖家精灵实时 BSR 榜，已配置接口才生效）
    try:
        await _bsr_refresh_all(job_log=job["log"])
    except Exception as e:
        job["log"].append(f"[!] 实时BSR刷新跳过: {type(e).__name__}: {e}")
    items, own_n = _watchlist_items()
    asins = [i["asin"] for i in items]
    if asins:
        S["asins"] = asins
        S["current_task"] = f"routine-{today:%Y%m%d}"
        S["current_seed"] = None
        S["current_market"] = "US"
        chunk = int(cfg.get("watchlist_chunk") or 150)
        await phase(f"④监控名单快照（{len(asins)}个）",
                    lambda sid: run_lookup(sid, chunk), "a", timeout=1200)
    else:
        job["log"].append("[!] 监控名单为空，跳过阶段④")
        phases["④监控名单快照"] = {"status": "skipped"}

    # ⑦ 官方指标补齐（漏斗选缺词 → kw_miner 入库）
    async def _kw_fill(sid):
        kws = _kw_fill_candidates(cfg.get("kw_tokens"), int(cfg.get("kw_daily") or 100))
        S["jobs"][sid]["total"] = len(kws)
        if not kws:
            S["jobs"][sid]["log"].append("[i] 意图词官方指标已全部补齐，无需查询")
            S["jobs"][sid]["status"] = "done"
            return
        S["jobs"][sid]["log"].append(f"[i] 待补 {len(kws)} 词（缺购买率/需供比，月搜索降序）")
        def on_batch(rows, label):
            S["jobs"][sid]["rows"] = (S["jobs"][sid]["rows"] or 0) + local_db.save_ads("kw_miner", "US", rows)
            S["jobs"][sid]["done"] = (S["jobs"][sid]["done"] or 0) + 20
            S["jobs"][sid]["log"].append(f"[+] 批次入库 {len(rows)} 行（{label}号）")
        rows = await _kw_miner_fetch(kws, 1, on_batch)
        S["jobs"][sid]["log"].append(f"[+] 完成：返回 {len(rows)} 行，已在库")
        S["jobs"][sid]["status"] = "done"
    await phase("⑦官方指标补齐", _kw_fill, "b", timeout=1200)

    # CL 找竞品（按星期；5 额度/次，竞品数量档位拉满最划算）
    seed = (cfg.get("cl_seeds") or {}).get(wd) or ""
    if seed:
        cl_count = str(cfg.get("cl_count") or "150")
        await phase(f"CL找竞品（{seed} · {cl_count}竞品位）",
                    lambda sid: run_cl_job(sid, [seed], None, cl_count, True),
                    "c", timeout=1800)
        # CL 产出竞品当天入库快照（找 → 存 闭环）
        if S["asins"] and not str(S.get("current_task") or "").startswith("routine"):
            n_ext = len(S["asins"])
            await phase(f"④′ CL产出竞品快照（{n_ext}个）",
                        lambda sid: run_lookup(sid, 150), "c2", timeout=1800)
            job["log"].append("[i] CL 产出竞品已完成当日快照入库；名单类目仍由长期 BSR 榜驱动")
        else:
            job["log"].append("[i] CL 未产出新竞品（可能提交失败或配额用尽），跳过快照")
            phases["④′ CL产出快照"] = {"status": "skipped"}
    else:
        job["log"].append(f"[i] {wd} 未配置 CL 种子，跳过（routine.json → cl_seeds.{wd}）")
        phases["CL找竞品"] = {"status": "skipped"}

    # 周五：⑥ 广告收割 + 周报归档
    if wd == "Fri":
        if S["asins"]:
            await phase("⑥广告收割TOP竞品",
                        lambda sid: run_ads_auto(sid, int(cfg.get("ads_limit") or 5), 4, 3),
                        timeout=3600, key="d")
        try:
            rows = local_db.latest(None)
            if rows:
                fp = OUT_DIR / f"weekly_{today:%Y%m%d}_html.html"
                make_report.build_html_report(rows, f"weekly-{today:%Y%m%d}", str(fp),
                                              market="US")
                job["log"].append(f"[+] 周报已归档: {fp.name}（{len(rows)} 行）")
                phases["周报归档"] = {"status": "done", "file": fp.name}
        except Exception as e:
            job["log"].append(f"[x] 周报归档失败: {type(e).__name__}: {e}")
            phases["周报归档"] = {"status": "error", "error": str(e)}

    cfg.setdefault("history", {})[str(today)] = {
        "phases": phases, "at": datetime.now().isoformat(timespec="seconds")}
    _save_routine(cfg)
    job["pct"], job["detail"] = 100, "完成"
    bad = [k for k, v in phases.items() if v.get("status") == "error"]
    job["status"] = "done"
    job["log"].append(f"[i] 例跑结束。成功 {len(phases) - len(bad)}/{len(phases)}"
                      + (f"；异常阶段: {', '.join(bad)}（多为会话失效，登录后明天会重试）" if bad else ""))


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

def _report_rows(scope: str):
    """⑤报告的数据范围：
    current = 当前分析（本次查询的完整返回优先，含变体展开行；
              否则当前任务 ASIN 集合的各自最新快照）；
    global  = 本地库全部 ASIN 的最新快照（跨所有历史批次）。"""
    if scope == "current":
        if S.get("last_rows") and S.get("last_rows_task") == S.get("current_task"):
            return S["last_rows"]
        if S["asins"]:
            return local_db.latest(S["asins"])
        raise HTTPException(400, "当前没有分析中的任务——请先在第②步选择任务并在第③步提取 ASIN，"
                                 "或把范围切换为'全部数据'")
    return local_db.latest(None)


@app.post("/api/report")
async def build_styled_report(req: dict = None):
    scope = ((req or {}).get("scope") or "current").lower()
    if scope not in ("current", "global"):
        scope = "current"
    rows = _report_rows(scope)
    if not rows:
        raise HTTPException(400, "所选范围内没有数据，请先运行批量查询")
    fp = OUT_DIR / f"{_scope_label(scope)}_style_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    make_report.build_report(rows, S["current_task"], str(fp),
                             market=S.get("current_market") or "US")
    S["last_report"] = str(fp)
    return {"ok": True, "scope": scope, "count": len(rows), "file": fp.name}

@app.post("/api/report_html")
async def build_html(req: dict = None):
    """美化报告 HTML 版（阅读/决策视图）：统计卡片 + 销量趋势图 + 可排序/筛选/缩略图表格。
    自包含单文件，xlsx 保留负责原始字段与后续计算。
    scope=current（默认）= 仅当前分析任务的数据；scope=global = 本地库全部数据。"""
    scope = ((req or {}).get("scope") or "current").lower()
    if scope not in ("current", "global"):
        scope = "current"
    rows = _report_rows(scope)
    if not rows:
        raise HTTPException(400, "所选范围内没有数据，请先运行批量查询")
    fp = OUT_DIR / f"{_scope_label(scope)}_html_{datetime.now():%Y%m%d_%H%M%S}.html"
    make_report.build_html_report(rows, S["current_task"], str(fp),
                                  market=S.get("current_market") or "US")
    S["last_report"] = str(fp)
    return {"ok": True, "scope": scope, "count": len(rows), "file": fp.name}

@app.post("/api/ai_pack")
async def ai_pack(req: dict = None):
    """生成 AI 分析包（Markdown，预聚合、紧凑上下文，可直接粘贴给外部 AI）。
    scope 同⑤；产品层 Top N（默认200）、关键词层 Top M（默认150，标准词按综合分）。"""
    scope = ((req or {}).get("scope") or "current").lower()
    if scope not in ("current", "global"):
        scope = "current"
    top_products = min(int((req or {}).get("top_products") or 200), 1000)
    top_keywords = min(int((req or {}).get("top_keywords") or 150), 500)
    rows = _report_rows(scope)
    if not rows:
        raise HTTPException(400, "所选范围内没有数据，请先运行批量查询")
    # 关键词层：全局漏斗（与②同源），官方指标补位
    imported_all = local_db.ads_all()
    km_rows = [r for g in imported_all if g.get("source") == "kw_miner" for r in g["rows"]]
    imported = [g for g in imported_all if g.get("source") != "kw_miner"]
    harvest = adv.full_rows_to_funnel(local_db.ads_kw_full_all())
    brands = sorted({(p.get("brand") or "").strip().lower()
                     for p in local_db.latest() if p.get("brand")})
    f = adv.funnel(imported + harvest, min_vol=500, brands=brands, enrich_rows=km_rows)
    fp = OUT_DIR / f"{_scope_label(scope)}_aipack_{datetime.now():%Y%m%d_%H%M%S}.md"
    make_report.build_ai_pack(rows, f["kept"], str(fp), scope_label=_scope_label(scope),
                              top_products=top_products, top_keywords=top_keywords)
    S["last_report"] = str(fp)
    size_kb = fp.stat().st_size / 1024
    return {"ok": True, "count": len(rows), "kept_kw": len(f["kept"]),
            "file": fp.name, "size_kb": round(size_kb, 1),
            "est_tokens": round(size_kb * 1024 / 4 / 1000, 1)}   # 粗估 token（~4字符/token）

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
        fp = OUT_DIR / f"{_scope_label(scope)}_ads_report_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
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
    """广告词价值排序与清洗：价值函数 v2（多因子：购买体量/购买转化/需求规模/广告排名(ASIN)/
    市场点击热度/市场定价/供需空间，官方 ARA 数据参与评分——注意官方指标与搜索/点击量均为
    词级市场口径，ASIN 级信号仅广告排名与来源归属），品牌词按本地库品牌表数据驱动分层，不剔除任何词"""
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
    fp = OUT_DIR / f"{_scope_label('global')}_clean_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
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
                job["log"].append(f"[+] 组结构已缓存: 自身 {len(r.get('groups') or [])} 组"
                                  f"（接口返回 {r.get('adCount')} 组，剔除发散 ASIN 组 "
                                  f"{r.get('skippedGroups')}）/ 关键词上限 {r.get('maxKeywordsCount')}")
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
                                                      ad_id, b, e, max_pages=2, asin=asin)
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
    ③ deep_results/ 下的 debug_cl_*.png 现场截图（全部）；
    ④ 数据库内历史误抓的发散 ASIN 广告组/关键词行（ads_groups/ads_kw_full 归属校验清除）。
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

    # ① 历史报告：按功能 token 分组（新命名 [ASIN/ALL]_[功能]_[时间]；旧命名归入 other），各组保留最新 keep 份
    utils = ("_deepdive_", "_style_", "_html_", "_ads_report_", "_clean_", "_aipack_")
    rep_rm = 0
    if OUT_DIR.exists():
        groups = {}
        for p in OUT_DIR.iterdir():
            if p.is_file() and p.suffix in (".xlsx", ".html", ".md"):
                u = next((u for u in utils if u in p.name), "_other_")
                groups.setdefault(u, []).append(p)
        for u, files in groups.items():
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
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
    # ④ 数据库内发散 ASIN 误抓数据（旧版无 ASIN 归属约束时期入库的）
    purged = {"groups_deleted": 0, "keywords_deleted": 0}
    if not dry:
        purged = local_db.purge_foreign_ads()

    return {"ok": True, "dry_run": dry, "files": n_files,
            "freed_mb": round(freed / 1048576, 1),
            "detail": {"旧报告": rep_rm, "调试转储": dbg_rm, "截图": png_rm,
                       "发散ASIN组清除": purged["groups_deleted"],
                       "发散ASIN关键词行清除": purged["keywords_deleted"]}}

# ---------- Amazon US 手动价格巡查（独立于卖家精灵链路） ----------

def _amazon_running_job():
    job_id = S.get("amazon_job_id")
    return job_id if S["jobs"].get(job_id, {}).get("status") == "running" else None


def _amazon_group(group_id=None):
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise HTTPException(400, "group_id 必须是正整数")
    try:
        return local_db.amz_get_group(group_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


def _amazon_require_idle():
    if _amazon_running_job():
        raise HTTPException(409, "巡查进行中，请结束后再修改分组或名单")


def _amazon_watch_payload(group_id=None):
    group = _amazon_group(group_id)
    return {"group_id": group["id"], "group_name": group["name"],
            "items": local_db.amz_watchlist(group["id"]), "max_asins": amz.MAX_ASINS,
            "running_job_id": _amazon_running_job()}


@app.get("/api/amazon/groups")
async def amazon_groups():
    groups = local_db.amz_groups()
    return {"items": groups, "default_group_id": groups[0]["id"],
            "running_job_id": _amazon_running_job()}


@app.post("/api/amazon/groups")
async def amazon_create_group(req: dict):
    _amazon_require_idle()
    try:
        group = local_db.amz_create_group(req.get("name"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"group": group, "items": local_db.amz_groups()}


@app.patch("/api/amazon/groups/{group_id}")
async def amazon_rename_group(group_id: int, req: dict):
    _amazon_require_idle()
    _amazon_group(group_id)
    try:
        group = local_db.amz_rename_group(group_id, req.get("name"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"group": group, "items": local_db.amz_groups()}


@app.delete("/api/amazon/groups/{group_id}")
async def amazon_delete_group(group_id: int):
    _amazon_require_idle()
    _amazon_group(group_id)
    try:
        local_db.amz_delete_group(group_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    groups = local_db.amz_groups()
    return {"items": groups, "group_id": groups[0]["id"]}


@app.get("/api/amazon/watch")
async def amazon_watch(group_id: int | None = None):
    return _amazon_watch_payload(group_id)


@app.post("/api/amazon/watch")
async def amazon_save_watch(req: dict):
    _amazon_require_idle()
    group = _amazon_group(req.get("group_id"))
    try:
        asins = amz.parse_asins(req.get("asins", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    local_db.amz_save_watchlist(asins, group["id"])
    return _amazon_watch_payload(group["id"])


@app.post("/api/amazon/run")
async def amazon_run(req: dict):  # 要求 JSON 请求，拒绝外站普通表单触发本地浏览器
    if _amazon_running_job():
        raise HTTPException(409, "已有 Amazon 巡查进行中")
    group = _amazon_group(req.get("group_id"))
    asins = [item["asin"] for item in local_db.amz_watchlist(group["id"])]
    if not asins:
        raise HTTPException(400, "请先在当前分组保存 Amazon 巡查名单")
    job_id = f"amzprice{time.time_ns()}"
    job = {"status": "running", "log": [], "done": 0, "total": len(asins),
           "rows": 0, "type": "amazon_price", "changed": 0, "baselines": 0,
           "unchanged": 0, "failed": 0, "results": [], "detail": "打开独立 Amazon 浏览器…",
           "group_id": group["id"], "group_name": group["name"]}
    S["jobs"][job_id] = job
    S["amazon_job_id"] = job_id
    asyncio.create_task(amz.run_patrol(asins, job))
    return {"job_id": job_id, "group_id": group["id"], "group_name": group["name"]}


@app.get("/api/amazon/changes")
async def amazon_changes(asin: str = "", limit: int = 100, group_id: int | None = None):
    asin = asin.strip().upper()
    if asin and not amz.ASIN_RE.fullmatch(asin):
        raise HTTPException(400, "ASIN 格式错误")
    group = _amazon_group(group_id) if group_id is not None else None
    return {"items": local_db.amz_changes(asin or None, limit, group_id=group["id"] if group else None)}


# ---------- 17TRACK 人工查询结果导入（不自动访问第三方网站） ----------

@app.get("/api/tracking/orders")
async def tracking_orders():
    return {"items": track_store.list_orders(), "max_orders": track_store.MAX_ORDERS,
            "batch_size": track_store.BATCH_SIZE}


@app.post("/api/tracking/orders")
async def tracking_add_orders(req: dict):
    pairs = req.get("pairs")
    if not isinstance(pairs, list):
        raise HTTPException(400, "pairs 必须是订单号-跟踪号数组")
    try:
        return track_store.add_pairs(pairs)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/tracking/next-batch")
async def tracking_next_batch(limit: int = 40):
    if limit < 1 or limit > track_store.BATCH_SIZE:
        raise HTTPException(400, f"limit 必须在 1–{track_store.BATCH_SIZE} 之间")
    items = track_store.next_batch(limit=limit)
    return {"items": items, "total": track_store.pending_count()}


@app.post("/api/tracking/import-summary")
async def tracking_import_summary(req: dict):
    text = req.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "请粘贴17TRACK复制的查询概要")
    parsed = track_import.parse_summary(text)
    result = track_store.import_records(parsed["records"])
    return {**result, "errors": parsed["errors"]}


@app.post("/api/tracking/orders/{order_row_id}/enabled")
async def tracking_set_enabled(order_row_id: int, req: dict):
    if type(req.get("enabled")) is not bool:
        raise HTTPException(400, "enabled 必须是布尔值")
    try:
        return {"item": track_store.set_enabled(order_row_id, req["enabled"])}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.delete("/api/tracking/orders/{order_row_id}")
async def tracking_delete_order(order_row_id: int):
    try:
        return {"deleted": track_store.remove_order(order_row_id)}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/tracking/orders/{order_row_id}/events")
async def tracking_order_events(order_row_id: int):
    try:
        return {"items": track_store.events(order_row_id)}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


def _tracking_feta_settings():
    try:
        return feta_config.load_settings()
    except feta_config.ConfigError as exc:
        raise HTTPException(503, f"FETA 配置无效：{exc}") from exc


@app.get("/api/tracking/fedex-email/reviews")
async def tracking_fedex_email_reviews():
    settings = _tracking_feta_settings()
    try:
        items = await asyncio.to_thread(feta_repo.review_events, db_path=settings.db_path)
    except Exception as exc:
        raise HTTPException(503, f"无法读取 FETA 待复核事件（{type(exc).__name__}）") from exc
    for item in items:
        item["system_status"] = track_store.feta_target_status(item.get("normalized_status"))
    return {"items": items, "total": len(items)}


@app.post("/api/tracking/fedex-email/sync")
async def tracking_fedex_email_sync():
    settings = _tracking_feta_settings()
    if not settings.imap_configured:
        raise HTTPException(503, "FETA IMAP 尚未配置；请检查本地 .env.feta")
    try:
        result = await asyncio.to_thread(feta_service.fetch_inbox, settings)
    except feta_config.ConfigError as exc:
        raise HTTPException(503, f"FETA 邮箱配置错误：{exc}") from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"FETA 收件箱同步失败（{type(exc).__name__}）；主物流状态未修改") from exc
    return result


@app.post("/api/tracking/fedex-email/events/{event_id}/review")
async def tracking_fedex_email_review(event_id: int, req: dict):
    if set(req) != {"decision"} or req.get("decision") not in {"accept", "ignore"}:
        raise HTTPException(400, "decision 必须是 accept 或 ignore")
    settings = _tracking_feta_settings()
    try:
        pending = await asyncio.to_thread(feta_repo.review_events, db_path=settings.db_path)
        event = next((item for item in pending if item["id"] == event_id), None)
        if event is None:
            raise HTTPException(404, "FETA 待复核事件不存在或已处理")
        if req["decision"] == "ignore":
            reviewed = await asyncio.to_thread(feta_service.review_event, event_id, "ignore", settings)
            return {"review": reviewed, "integration": None}

        await asyncio.to_thread(feta_repo.validate_review_event, event_id, "accept", db_path=settings.db_path)
        integration = await asyncio.to_thread(track_store.apply_feta_event, event)
        if not integration["matched"]:
            raise HTTPException(409, "跟踪号在当前物流系统中没有匹配项；事件仍保持待复核，未写入主系统")
        try:
            reviewed = await asyncio.to_thread(feta_service.review_event, event_id, "accept", settings)
        except ValueError as exc:
            raise HTTPException(409, f"主系统已幂等保存该事件，但 FETA 复核未完成；可安全重试：{exc}") from exc
        return {"review": reviewed, "integration": integration}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"复核/写回失败（{type(exc).__name__}）") from exc


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
