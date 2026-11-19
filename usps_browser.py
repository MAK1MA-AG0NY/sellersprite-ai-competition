# -*- coding: utf-8 -*-
"""User-triggered Playwright USPS lookup with automatic result import.

This module interacts with the public USPS page through its visible UI, waits
for the rendered results, and imports only allowlisted tracking IDs. It does not
call undocumented endpoints or attempt to solve/bypass human verification.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

import tracking_import
import tracking_store

USPS_TRACKING_URL = "https://tools.usps.com/tracking/"
TRACK718_TRACKING_URL = "https://www.track718.com/zh-CN"
MAX_USPS_QUERY_NUMBERS = 35
MAX_TRACK718_QUERY_NUMBERS = 40
MAX_LOG = 60
AUTO_WAIT_SECONDS = 45
AUTO_POLL_INTERVAL_SECONDS = 1.0
AUTO_STABLE_POLLS = 3
DEFAULT_FLCLASH_PROXY_HOST = "127.0.0.1:7897"
_USPS_RESULT_MARKER_RE = re.compile(r"latest update|tracking history|tracking results", re.I)

USPS_DOM_SELECTORS = {
    "inputs": (
        "textarea[placeholder*='35' i]", "textarea[aria-label*='track' i]",
        "textarea[name*='label' i]", "textarea",
        "input[name*='tLabels' i]", "input[placeholder*='35' i]",
        "input[placeholder*='tracking' i]", "input[aria-label*='tracking' i]",
        "input[id*='tracking' i]", "input[type='text']",
    ),
    "submit": (
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Track')", "[role='button']:has-text('Track')",
        "button:has-text('Search')", "[role='button']:has-text('Search')",
    ),
}

TRACK718_DOM_SELECTORS = {
    "inputs": ("input.search-input", "textarea[placeholder*='40' i]", "textarea", "input[type='text']"),
    "submit": ("button:has-text('查询')", "[role='button']:has-text('查询')",
               "button:has-text('Track')", "button[type='submit']"),
}


class USPSQueryError(RuntimeError):
    pass


class USPSQueryBusy(USPSQueryError):
    pass


class USPSQuerySession:
    """One user-triggered batch, automatically queried and imported when possible."""

    provider_name = "USPS"
    result_marker_re = _USPS_RESULT_MARKER_RE
    default_site_url = USPS_TRACKING_URL
    expected_site_host = "tools.usps.com"
    max_query_numbers = MAX_USPS_QUERY_NUMBERS
    tracking_pattern = re.compile(r"[A-Za-z0-9]{4,34}")
    batch_separator = ","
    dom_selectors = USPS_DOM_SELECTORS

    def _is_allowed_mainframe_host(self, host: str) -> bool:
        return host == "usps.com" or host.endswith(".usps.com")

    def _parse_provider_body(self, body: str) -> dict[str, Any]:
        return tracking_import.parse_usps_summary(body, list(self._allowed.values()))

    def __init__(self, importer: Callable[..., dict[str, Any]] | None = None,
                 playwright_factory: Callable[[], Any] | None = None,
                 site_url: str | None = None,
                 proxy_url: str | None = None) -> None:
        self._importer = importer or tracking_store.import_records
        self._playwright_factory = playwright_factory or async_playwright
        self._site_url = site_url or self.default_site_url
        self._proxy_url = proxy_url
        self._proxy_used: str | None = None
        self._page_issue: str | None = None
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._collector_task: asyncio.Task | None = None
        self._allowed: dict[str, str] = {}
        self._status = "idle"
        self._reason = ""
        self._log: list[str] = []
        self._result: dict[str, Any] = {}

    def _active(self) -> bool:
        return self._status in {"opening", "submitted", "waiting_user", "collecting"}

    def snapshot(self) -> dict[str, Any]:
        return {"status": self._status, "provider": self.provider_name.casefold(),
                "needs_human": self._status == "waiting_user",
                "expected_count": len(self._allowed), "tracking_numbers": list(self._allowed.values()),
                "proxy_mode": self._proxy_used,
                "reason": self._reason, "log": list(self._log[-MAX_LOG:]),
                "result": dict(self._result)}

    def _proxy_candidates(self) -> list[tuple[str, str | None]]:
        configured = self._proxy_url
        if configured is None:
            configured = os.environ.get("USPS_PROXY_URL", "")
        configured = configured.strip()
        if configured.casefold() in {"direct", "none", "off"}:
            return [("direct", None)]
        if not configured:
            configured = DEFAULT_FLCLASH_PROXY_HOST
        if "://" not in configured:
            return [("http", f"http://{configured}"),
                    ("socks5", f"socks5://{configured}")]
        parsed = urlsplit(configured)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
            return []
        candidates = [(parsed.scheme, configured)]
        try:
            local_flclash = parsed.hostname.casefold() in {"127.0.0.1", "localhost"} and parsed.port == 7897
        except ValueError:
            local_flclash = False
        if local_flclash:
            alternate = "socks5" if parsed.scheme in {"http", "https"} else "http"
            candidates.append((alternate, f"{alternate}://{parsed.netloc}"))
        return candidates

    def _record_response(self, response: Any) -> None:
        try:
            path = urlsplit(response.url).path.casefold()
            if "anyapp_outage_apology.htm" in path:
                self._page_issue = f"{self.provider_name} 将查询页重定向到了 outage apology 页面"
            elif (response.status in {301, 302, 303, 307, 308}
                  and response.request.resource_type in {"xhr", "fetch"}
                  and path.rstrip("/") == "/tracking"):
                self._page_issue = f"{self.provider_name} 初始化请求被重定向；这不是输入框选择器问题"
        except Exception:
            pass

    def _record_request_failure(self, request: Any) -> None:
        try:
            path = urlsplit(request.url).path.casefold()
            failure = str(request.failure or "").casefold()
            if "anyapp_outage_apology.htm" in path or (
                    path.rstrip("/") == "/tracking" and "failed" in failure):
                self._page_issue = f"{self.provider_name} tracking 初始化请求失败（可能是 CORS/重定向问题）"
        except Exception:
            pass

    def _record_interstitial_body(self, body: str) -> None:
        if re.search(r"processing your request|resubmit your request", body, re.I):
            self._page_issue = f"{self.provider_name}/Akamai 显示 Processing your request 验证页；不会尝试绕过"
        elif re.search(r"anyapp_outage_apology", body, re.I):
            self._page_issue = f"{self.provider_name} 返回 outage apology 页面"

    def _log_line(self, text: str) -> None:
        self._log.append(str(text)[:300])
        if len(self._log) > MAX_LOG:
            del self._log[:-MAX_LOG]

    async def _visible(self, root: Any, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                locator = root.locator(selector)
                for index in range(min(await locator.count(), 10)):
                    item = locator.nth(index)
                    if await item.is_visible():
                        return item
            except Exception:
                continue
        return None

    async def _fill_query_batch(self, query_input: Any, tracking_numbers: list[str]) -> None:
        await query_input.fill(self.batch_separator.join(tracking_numbers), timeout=10000)

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
            raise USPSQueryError(f"tracking_numbers 必须是非空数组；仅接受 {self.provider_name} 跟踪号")
        normalized: dict[str, str] = {}
        for number in tracking_numbers:
            if not isinstance(number, str) or not self.tracking_pattern.fullmatch(number.strip()):
                raise USPSQueryError(f"{self.provider_name} 跟踪号格式无效")
            clean = number.strip()
            normalized.setdefault(clean.casefold(), clean)
        if len(normalized) > self.max_query_numbers:
            raise USPSQueryError(f"{self.provider_name} 每批最多查询 {self.max_query_numbers} 个唯一跟踪号")
        async with self._lock:
            if self._active():
                raise USPSQueryBusy(f"已有 {self.provider_name} 自动查询会话；请等待结果或取消后再开始")
            await self._cleanup()
            self._allowed = normalized
            self._status = "opening"
            self._reason = ""
            self._result = {}
            self._log = [f"用户已触发 {self.provider_name} 查询；本批 {len(normalized)} 个唯一跟踪号，不发送订单号。"]
            parsed = urlsplit(self._site_url)
            if parsed.scheme != "https" or parsed.hostname != self.expected_site_host:
                self._status = "error"
                self._reason = f"{self.provider_name} 页面地址无效；仅允许访问配置的官方域名"
                return self.snapshot()
            self._proxy_used = None
            proxy_candidates = self._proxy_candidates()
            if not proxy_candidates:
                self._status = "error"
                self._reason = "USPS_PROXY_URL 配置无效；请使用 http(s)://、socks5:// 或 direct。"
                return self.snapshot()
            failures: list[str] = []
            human_verification_found = False
            query_input = None
            for proxy_mode, proxy_server in proxy_candidates:
                self._page_issue = None
                try:
                    self._playwright = await self._playwright_factory().start()
                    launch_options = {"headless": False}
                    if proxy_server:
                        launch_options["proxy"] = {"server": proxy_server}
                    self._browser = await self._playwright.chromium.launch(**launch_options)
                    self._context = await self._browser.new_context(locale="zh-CN", accept_downloads=False)
                    self._page = await self._context.new_page()

                    async def restrict_navigation(route):
                        request = route.request
                        if request.is_navigation_request() and request.frame == self._page.main_frame:
                            destination = urlsplit(request.url)
                            host = (destination.hostname or "").casefold()
                            if destination.scheme != "https" or not self._is_allowed_mainframe_host(host):
                                await route.abort()
                                return
                        await route.continue_()

                    await self._page.route("**/*", restrict_navigation)
                    self._page.on("response", self._record_response)
                    self._page.on("requestfailed", self._record_request_failure)
                    await self._page.goto(self._site_url, wait_until="domcontentloaded",
                                          timeout=18000 if proxy_server else 30000)
                    await asyncio.sleep(0.35)
                    body = (await self._page.locator("body").inner_text(timeout=3000))[:5000]
                    self._record_interstitial_body(body)
                    try:
                        await self._page.wait_for_selector(",".join(self.dom_selectors["inputs"]),
                                                         state="visible", timeout=8000)
                    except Exception:
                        pass
                    body = (await self._page.locator("body").inner_text(timeout=3000))[:5000]
                    self._record_interstitial_body(body)
                    if self._page_issue:
                        failures.append(f"{proxy_mode}:site_interstitial")
                        await self._cleanup()
                        break
                    query_input = await self._visible(self._page, self.dom_selectors["inputs"])
                    if query_input is not None:
                        self._proxy_used = proxy_mode
                        break
                    if re.search(r"captcha|verify you are human|robot check|人机验证|验证码", body, re.I):
                        failures.append(f"{proxy_mode}:human_verification")
                        human_verification_found = True
                        await self._cleanup()
                        break
                    failures.append(f"{proxy_mode}:input_not_found")
                    await self._cleanup()
                except Exception as exc:
                    failures.append(f"{proxy_mode}:{type(exc).__name__}")
                    await self._cleanup()
            if query_input is None:
                self._status = "error"
                modes = ", ".join(mode for mode, _ in proxy_candidates)
                if self._page_issue:
                    self._reason = f"{self._page_issue}；查询已停止，跟踪号未提交。"
                elif human_verification_found:
                    self._reason = f"{self.provider_name} 页面要求人工验证；自动查询未启动，也未尝试绕过验证。"
                else:
                    self._reason = f"{self.provider_name} 查询框未加载；已尝试代理模式：{modes}。未发送本批号码，也未复用 Chrome Cookie。"
                if failures:
                    self._log_line(f"{self.provider_name} 页面加载检查：" + ", ".join(failures))
                else:
                    self._log_line("没有可用的 USPS 代理配置。")
                await self._cleanup()
                return self.snapshot()

            try:
                await self._fill_query_batch(query_input, list(normalized.values()))
                submit = None
                try:
                    form = query_input.locator("xpath=ancestor::form[1]")
                    submit = await self._visible(form, self.dom_selectors["submit"])
                except Exception:
                    pass
                if submit is None:
                    submit = await self._visible(self._page, self.dom_selectors["submit"])
                if submit is None:
                    raise USPSQueryError(f"{self.provider_name} 页面未找到可见的查询按钮；未提交本批查询")
                await submit.click(timeout=10000)
                self._status = "submitted"
                self._reason = f"{self.provider_name} 查询已提交（{self._proxy_used}）；Playwright 正在自动等待结果并导入本批号码。"
                self._log_line(f"{self.provider_name} 查询已提交；代理模式 {self._proxy_used}，开始自动读取结果。")
                self._collector_task = asyncio.create_task(self._auto_collect())
            except Exception as exc:
                self._status = "error"
                self._reason = str(exc)[:500]
                self._log_line(f"{self.provider_name} 页面或查询按钮错误：{self._reason}；可手动查询后回填。")
                await self._cleanup()
            return self.snapshot()

    async def _page_has_human_check(self) -> bool:
        if self._page is None or self._page.is_closed():
            return False
        try:
            body = (await self._page.locator("body").inner_text(timeout=3000))[:6000]
        except Exception:
            return False
        return bool(re.search(r"captcha|verify you are human|robot check|human verification|processing your request|resubmit your request|人机验证|验证码", body, re.I))

    def _parse_body(self, body: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        parsed = self._parse_provider_body(body)
        expected = set(self._allowed)
        records = [record for record in parsed["records"]
                   if record["tracking_number"].casefold() in expected]
        return parsed, records

    def _import_parsed(self, parsed: dict[str, Any], records: list[dict[str, Any]]) -> None:
        result = self._importer(records)
        self._result = {**result, "parsed_errors": len(parsed["errors"]),
                        "requested": len(self._allowed), "imported_tracks": len(records)}
        self._status = "done"
        missing = max(0, len(self._allowed) - len(records))
        self._reason = (f"{self.provider_name} 可识别结果已自动导入；另有 {missing} 个号码未能识别，保持未查。"
                        if missing else f"{self.provider_name} 本批结果已自动识别并导入本地订单。")
        self._log_line(f"{self.provider_name} 自动导入：匹配 {result.get('matched', 0)}，未匹配 {result.get('unmatched', 0)}，解析提示 {len(parsed['errors'])}。")

    async def _auto_collect(self) -> None:
        self._status = "collecting"
        self._reason = f"{self.provider_name} 页面查询中；Playwright 正在自动等待结果。"
        deadline = asyncio.get_running_loop().time() + AUTO_WAIT_SECONDS
        previous_body = None
        stable_polls = 0
        latest_parsed: dict[str, Any] = {"records": [], "errors": []}
        latest_records: list[dict[str, Any]] = []
        try:
            while asyncio.get_running_loop().time() < deadline:
                if self._page is None or self._page.is_closed():
                    raise USPSQueryError(f"{self.provider_name} 浏览器窗口已关闭；本批没有自动完成")
                if await self._page_has_human_check():
                    self._status = "waiting_user"
                    self._reason = f"检测到 {self.provider_name} 人工验证；自动流程已暂停，请完成验证后点击“验证后重试读取”。"
                    self._log_line("页面要求人工验证；未尝试绕过。")
                    return
                body = (await self._page.locator("body").inner_text(timeout=8000))[:100000]
                parsed, records = self._parse_body(body)
                latest_parsed, latest_records = parsed, records
                if len(records) >= len(self._allowed):
                    self._import_parsed(parsed, records)
                    return

                has_result_marker = bool(self.result_marker_re.search(body)) or bool(records)
                if has_result_marker and body == previous_body:
                    stable_polls += 1
                else:
                    stable_polls = 0
                previous_body = body
                if has_result_marker and stable_polls >= AUTO_STABLE_POLLS:
                    if records:
                        self._import_parsed(parsed, records)
                    else:
                        self._status = "error"
                        self._reason = f"{self.provider_name} 结果页已稳定，但状态未能安全识别；没有修改本地订单。可粘贴页面文本手工解析。"
                        self._log_line("页面结果稳定但解析无匹配；未写入订单状态。")
                    return
                await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)

            if latest_records:
                self._import_parsed(latest_parsed, latest_records)
                self._reason = f"自动等待超时；已导入识别到的 {len(latest_records)} 个 {self.provider_name} 结果，其余保持未查。"
            else:
                self._status = "error"
                self._reason = f"等待 {self.provider_name} 结果超时；未识别到可安全导入的状态。可粘贴页面文本手工解析。"
                self._log_line(f"{self.provider_name} 结果等待超时；未写入订单状态。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status = "error"
            self._reason = str(exc)[:500]
            self._log_line(f"{self.provider_name} 自动读取失败：{self._reason}")
        finally:
            if self._status in {"done", "error"}:
                await self._cleanup()

    async def continue_reading(self) -> dict[str, Any]:
        async with self._lock:
            if self._collector_task is not None and not self._collector_task.done():
                raise USPSQueryBusy("Playwright 正在自动读取结果，请稍候")
            if self._status != "waiting_user":
                raise USPSQueryError(f"当前没有等待人工验证后重试的 {self.provider_name} 会话")
            self._status = "collecting"
            self._reason = ""
            try:
                if self._page is None or self._page.is_closed():
                    raise USPSQueryError(f"{self.provider_name} 浏览器窗口已关闭；请使用手工回填")
                if await self._page_has_human_check():
                    self._status = "waiting_user"
                    self._reason = f"{self.provider_name} 页面仍在等待人工验证；完成后再点一次重试读取。"
                    return self.snapshot()
                body = (await self._page.locator("body").inner_text(timeout=8000))[:100000]
                parsed, records = self._parse_body(body)
                if not records:
                    self._status = "waiting_user"
                    self._reason = f"尚未识别到 {self.provider_name} 当前状态；请确认结果已完整显示，或使用下方文本回填。"
                    self._result = {"matched": 0, "requested": len(self._allowed),
                                    "parsed_errors": len(parsed["errors"]), "items": []}
                    self._log_line(f"人工重试读取未识别到 {self.provider_name} 结果；没有写入本地订单状态。")
                    return self.snapshot()
                self._import_parsed(parsed, records)
            except Exception as exc:
                self._status = "error"
                self._reason = str(exc)[:500]
                self._log_line(f"{self.provider_name} 结果读取失败：{self._reason}；可手动摘要回填。")
            finally:
                if self._status in {"done", "error"}:
                    await self._cleanup()
            return self.snapshot()

    async def cancel(self) -> dict[str, Any]:
        task = self._collector_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            await self._cleanup()
            if self._status not in {"done", "error", "idle"}:
                self._status = "cancelled"
                self._reason = f"{self.provider_name} 查询已取消；浏览器上下文已关闭。"
            self._collector_task = None
            self._log_line(f"{self.provider_name} 浏览器会话已关闭。")
            return self.snapshot()


class Track718QuerySession(USPSQuerySession):
    provider_name = "Track718"
    result_marker_re = re.compile(r"轨迹翻译|复制结果|查不到|暂未上线|签收成功|到达待取|派送途中|运输途中|投递失败|可能异常|包裹退回|运输过久", re.I)
    default_site_url = TRACK718_TRACKING_URL
    expected_site_host = "www.track718.com"
    max_query_numbers = MAX_TRACK718_QUERY_NUMBERS
    tracking_pattern = re.compile(r"[A-Za-z0-9]{4,50}")
    dom_selectors = TRACK718_DOM_SELECTORS

    async def _fill_query_batch(self, query_input: Any, tracking_numbers: list[str]) -> None:
        token_selector = ".track-box .track-number"
        tokens = self._page.locator(token_selector)
        if await tokens.count():
            raise USPSQueryError("Track718 查询框已含有号码；为避免错查，本批未提交。")
        for index, number in enumerate(tracking_numbers, 1):
            await query_input.fill(number, timeout=10000)
            await query_input.press("Enter", timeout=5000)
            await self._page.wait_for_function(
                "expected => document.querySelectorAll('.track-box .track-number').length === expected",
                arg=index, timeout=5000,
            )
        actual = [value.strip().casefold() for value in await tokens.all_inner_texts()]
        expected = [number.casefold() for number in tracking_numbers]
        if actual != expected:
            raise USPSQueryError(
                f"Track718 逐个回车录入校验失败（{len(actual)}/{len(expected)}）；查询未提交。"
            )

    def _is_allowed_mainframe_host(self, host: str) -> bool:
        return (host == "track718.com" or host.endswith(".track718.com")
                or host == "track718.net" or host.endswith(".track718.net"))

    def _parse_provider_body(self, body: str) -> dict[str, Any]:
        return tracking_import.parse_track718_summary(body, list(self._allowed.values()))


__all__ = ["USPS_TRACKING_URL", "TRACK718_TRACKING_URL", "MAX_USPS_QUERY_NUMBERS",
           "MAX_TRACK718_QUERY_NUMBERS", "USPS_DOM_SELECTORS", "TRACK718_DOM_SELECTORS",
           "USPSQueryError", "USPSQueryBusy", "USPSQuerySession", "Track718QuerySession"]
