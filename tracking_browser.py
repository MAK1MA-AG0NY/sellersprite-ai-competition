# -*- coding: utf-8 -*-
"""User-triggered, visible 17TRACK browser handoff; CAPTCHA is always human-operated."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

import tracking_import
import tracking_store

TRACKING_URL = "https://www.17track.net/zh-cn"
MAX_QUERY_NUMBERS = 40
MAX_LOG = 100

# Keep all selectors in one place; unsupported website changes safely fall back to manual use.
DOM_SELECTORS = {
    "inputs": (
        "textarea[placeholder*='tracking' i]", "textarea[placeholder*='单号']",
        "textarea", "input[placeholder*='tracking' i]", "input[placeholder*='单号']",
        "input[name*='number' i]", "input[type='text']",
    ),
    "submit": (
        "button:has-text('Track')", "button:has-text('查询')", "button:has-text('追踪')",
        "[role='button']:has-text('Track')", "[role='button']:has-text('查询')",
    ),
    "captcha": ("iframe[src*='captcha' i]", "#captcha", "[class*='captcha' i]", "[id*='captcha' i]"),
    "results": ("[data-tracking-result]", ".tracklist-item", ".track-result", ".tracking-result", "table tbody tr"),
}


class QueryError(RuntimeError):
    pass


class SessionBusy(QueryError):
    pass


class TrackingQuerySession:
    """A single explicitly started browser task; no page polling or auto CAPTCHA handling."""

    def __init__(self, importer: Callable[..., dict[str, Any]] | None = None,
                 playwright_factory: Callable[[], Any] | None = None,
                 site_url: str = TRACKING_URL) -> None:
        self._importer = importer or tracking_store.import_records
        self._playwright_factory = playwright_factory or async_playwright
        self._site_url = site_url
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._allowed: dict[str, str] = {}
        self._status = "idle"
        self._reason = ""
        self._log: list[str] = []
        self._result: dict[str, Any] = {}

    def _active(self) -> bool:
        return self._status in {"opening", "submitted", "waiting_user", "collecting"}

    def snapshot(self) -> dict[str, Any]:
        return {"status": self._status, "needs_human": self._status == "waiting_user",
                "expected_count": len(self._allowed), "reason": self._reason,
                "log": list(self._log[-MAX_LOG:]), "result": dict(self._result)}

    def _log_line(self, text: str) -> None:
        self._log.append(str(text)[:300])
        if len(self._log) > MAX_LOG:
            del self._log[:-MAX_LOG]

    async def _visible(self, page: Any, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                loc = page.locator(selector)
                for index in range(min(await loc.count(), 10)):
                    item = loc.nth(index)
                    if await item.is_visible():
                        return item
            except Exception:
                continue
        return None

    async def _cleanup(self) -> None:
        for resource in (self._context, self._browser, self._playwright):
            if resource is None:
                continue
            try:
                if resource is self._playwright and hasattr(resource, "stop"):
                    await resource.stop()
                elif hasattr(resource, "close"):
                    await resource.close()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None

    async def start(self, tracking_numbers: Any) -> dict[str, Any]:
        if not isinstance(tracking_numbers, list) or not tracking_numbers:
            raise QueryError("tracking_numbers 必须是非空数组；仅发送跟踪号，不接受订单号")
        normalized: dict[str, str] = {}
        for number in tracking_numbers:
            if not isinstance(number, str) or not re.fullmatch(r"[A-Za-z0-9]{5,50}", number.strip()):
                raise QueryError("跟踪号须为 5–50 位字母或数字")
            clean = number.strip()
            normalized.setdefault(clean.casefold(), clean)
        if len(normalized) > MAX_QUERY_NUMBERS:
            raise QueryError(f"每次最多查询 {MAX_QUERY_NUMBERS} 个唯一跟踪号")
        async with self._lock:
            if self._active():
                raise SessionBusy("已有 17TRACK 查询会话；请继续或取消后再开始")
            await self._cleanup()
            self._allowed = normalized
            self._status = "opening"
            self._reason = ""
            self._result = {}
            self._log = [f"用户已触发查询；本批 {len(normalized)} 个唯一跟踪号，不发送订单号。"]
            parsed = urlsplit(self._site_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                self._status = "error"
                self._reason = "配置的 17TRACK 页面地址无效"
                return self.snapshot()
            try:
                self._playwright = await self._playwright_factory().start()
                self._browser = await self._playwright.chromium.launch(headless=False)
                self._context = await self._browser.new_context(locale="zh-CN", accept_downloads=False)
                self._page = await self._context.new_page()
                async def restrict_navigation(route):
                    request = route.request
                    if request.is_navigation_request() and request.frame == self._page.main_frame:
                        dest = urlsplit(request.url)
                        expected_host = urlsplit(self._site_url).hostname
                        if dest.scheme != urlsplit(self._site_url).scheme or dest.hostname != expected_host:
                            await route.abort()
                            return
                    await route.continue_()
                await self._page.route("**/*", restrict_navigation)
                await self._page.goto(self._site_url, wait_until="domcontentloaded", timeout=30000)
                self._status = "submitted"
                query_input = await self._visible(self._page, DOM_SELECTORS["inputs"])
                if query_input is None:
                    raise QueryError("当前 17TRACK 布局未找到可见输入框；请使用手工查询/摘要回填")
                await query_input.fill("\n".join(normalized.values()), timeout=8000)
                submit = await self._visible(self._page, DOM_SELECTORS["submit"])
                if submit is None:
                    raise QueryError("当前 17TRACK 布局未找到可见查询按钮；请手工查询/摘要回填")
                await submit.click(timeout=8000)
                self._status = "waiting_user"
                self._reason = "请在打开的官方窗口中完成人工验证码/页面确认，再点击“继续读取结果”；本工具不会解题。"
                self._log_line("查询已提交；等待用户完成页面操作，不进行后台网站轮询。")
            except Exception as exc:
                self._status = "error"
                self._reason = str(exc)[:500]
                self._log_line(f"布局或浏览器错误：{self._reason}；可改用手工摘要回填。")
                await self._cleanup()
            return self.snapshot()

    async def _page_has_captcha(self) -> bool:
        try:
            body = (await self._page.locator("body").inner_text(timeout=3000))[:5000]
            if re.search(r"captcha|验证码|robot check|verify you are human|人机验证", body, re.I):
                return True
        except Exception:
            return False
        for selector in DOM_SELECTORS["captcha"]:
            try:
                if await self._page.locator(selector).count() and await self._visible(self._page, (selector,)):
                    return True
            except Exception:
                pass
        return False

    async def _read_summary(self) -> str:
        if self._page is None or self._page.is_closed():
            raise QueryError("官方浏览器窗口已关闭；可重新开始或使用手工摘要回填")
        if await self._page_has_captcha():
            self._status = "waiting_user"
            self._reason = "页面仍显示验证码/人工验证；请在官方窗口完成人工操作后再点击继续。"
            return ""
        for selector in DOM_SELECTORS["results"]:
            try:
                rows = self._page.locator(selector)
                count = min(await rows.count(), 80)
                texts = []
                for index in range(count):
                    item = rows.nth(index)
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text()).strip()
                    if text:
                        # A local site may render copied fields as tab-separated text;
                        # line breaks from visible row cells are normalized to TSV.
                        texts.append("\t".join(part.strip() for part in text.splitlines() if part.strip()))
                if texts:
                    candidate = "\n".join(texts)
                    if tracking_import.parse_summary(candidate)["records"]:
                        return candidate
            except Exception:
                continue
        raise QueryError("未识别到可见的 17TRACK 结果行；未导入任何数据，请手工复制摘要回填")

    async def continue_reading(self) -> dict[str, Any]:
        async with self._lock:
            if self._status not in {"waiting_user", "submitted"}:
                raise QueryError("当前没有等待人工确认的查询会话")
            self._status = "collecting"
            self._reason = ""
            try:
                summary = await self._read_summary()
                if not summary:
                    return self.snapshot()
                parsed = tracking_import.parse_summary(summary)
                expected = set(self._allowed)
                allowed_records = [record for record in parsed["records"]
                                   if record["tracking_number"].casefold() in expected]
                filtered = len(parsed["records"]) - len(allowed_records)
                result = self._importer(allowed_records)
                self._result = {**result, "parsed_errors": len(parsed["errors"]),
                                "ignored_unrequested": filtered,
                                "requested": len(self._allowed)}
                self._status = "done"
                self._reason = "查询结果已按本批跟踪号过滤并导入；页面其它记录被忽略。"
                self._log_line(f"结果：匹配 {result.get('matched', 0)}，未匹配 {result.get('unmatched', 0)}，未请求记录忽略 {filtered}。")
            except Exception as exc:
                self._status = "error"
                self._reason = str(exc)[:500]
                self._log_line(f"读取失败：{self._reason}；可用手工摘要回填。")
            finally:
                if self._status in {"done", "error"}:
                    await self._cleanup()
            return self.snapshot()

    async def cancel(self) -> dict[str, Any]:
        async with self._lock:
            await self._cleanup()
            if self._status not in {"done", "error", "idle"}:
                self._status = "cancelled"
                self._reason = "用户取消；浏览器上下文已关闭。"
            self._log_line("浏览器会话已关闭。")
            return self.snapshot()


__all__ = ["TRACKING_URL", "MAX_QUERY_NUMBERS", "DOM_SELECTORS", "QueryError", "SessionBusy", "TrackingQuerySession"]
