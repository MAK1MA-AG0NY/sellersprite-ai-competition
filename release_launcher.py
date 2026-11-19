"""Windowed entry point for the Windows portable release."""

import argparse
import asyncio
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import sys
import threading
import webbrowser

import uvicorn
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app_paths import APP_NAME, ASSET_DIR, DATA_DIR, IS_FROZEN, VERSION

HOST = "127.0.0.1"
TITLE = "SellerSprite 竞品工作台"
ICON_PATH = ASSET_DIR / "assets" / "app.ico"
LOG_PATH = DATA_DIR / "logs" / "desktop.log"
logger = logging.getLogger(__name__)


class ReadyServer(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.started:
            self.ready.set()


@contextmanager
def local_service(server):
    def serve():
        try:
            server.run()
        except (Exception, SystemExit):
            logger.exception("本地服务启动或运行失败")
        finally:
            server.ready.set()

    worker = threading.Thread(target=serve, name="local-server", daemon=True)
    worker.start()
    try:
        if not server.ready.wait(timeout=15) or not server.started or not worker.is_alive():
            raise RuntimeError(f"无法启动本地服务。若端口 {server.config.port} 被占用，请先关闭旧版。")
        yield
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        if worker.is_alive():
            logger.warning("本地服务未能在退出超时前完成清理")


def prepare_runtime():
    if not IS_FROZEN:
        return
    browsers = ASSET_DIR / "pw-browsers"
    if not browsers.is_dir():
        raise RuntimeError("内置浏览器缺失，请完整解压 ZIP，不要只复制 EXE。")
    temp_dir = DATA_DIR / ".pw-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(PLAYWRIGHT_BROWSERS_PATH=str(browsers), TEMP=str(temp_dir), TMP=str(temp_dir))
    os.chdir(DATA_DIR)


async def check_browser():
    """Exercise both Playwright browser variants without using external sites."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        for headless in (True, False):
            async with await playwright.chromium.launch(headless=headless) as browser:
                page = await browser.new_page()
                await page.set_content("<title>SellerSprite release check</title>")
                if await page.title() != "SellerSprite release check":
                    raise RuntimeError("内置浏览器未能加载测试页。")
                logger.info("Chromium (%s): OK", "headless" if headless else "headed")


def message_box(text, title=TITLE, *, error=False):
    show = ctypes.windll.user32.MessageBoxW
    show.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    show.restype = ctypes.c_int
    show(None, text, title, 0x10 if error else 0x40)


def installed_webview2(loader):
    """Ask Microsoft's loader rather than assuming Edge means WebView2 is installed."""
    dll = ctypes.WinDLL(str(loader))
    get_version = dll.GetAvailableCoreWebView2BrowserVersionString
    get_version.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_wchar_p)]
    get_version.restype = ctypes.c_long
    version = ctypes.c_wchar_p()
    try:
        return get_version(None, ctypes.byref(version)) == 0 and bool(version.value)
    finally:
        if version:
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            free(ctypes.cast(version, ctypes.c_void_p))


def show_desktop(url):
    """Return False for unavailable/failed WebView2; True for a normal close."""
    import webview

    loader = Path(webview.__file__).parent / "lib" / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"
    if not installed_webview2(loader):
        logger.warning("未检测到 WebView2 运行时")
        return False

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(TITLE, url=url, width=1280, height=840, min_size=(900, 620),
                                   background_color="#f2f5fa", text_select=True)
    completed = threading.Event()
    timed_out = threading.Event()

    def loaded():
        logger.info("WebView2 工作台已加载")
        completed.set()

    def watchdog():
        if not completed.wait(timeout=15) and not window.events.closed.is_set():
            timed_out.set()
            logger.warning("WebView2 页面没有在规定时间内加载")
            if window.events.shown.is_set():
                window.destroy()

    # pywebview can otherwise silently fall back to the obsolete MSHTML engine.
    window.events.initialized += lambda renderer: renderer == "edgechromium"
    window.events.loaded += loaded
    window.events.closed += completed.set
    try:
        webview.start(func=watchdog, gui="edgechromium", icon=str(ICON_PATH),
                      private_mode=False, storage_path=str(DATA_DIR / "webview2"))
    finally:
        completed.set()
    return window.events.closed.is_set() and not timed_out.is_set()


def show_in_browser(url, *, fallback):
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        logger.exception("无法打开默认浏览器")
        opened = False
    status = "独立窗口不可用，已切换到浏览器模式。" if fallback else "正在使用浏览器模式。"
    opening = "控制台已在默认浏览器中打开。" if opened else "请在浏览器中手动输入以下地址。"
    logger.info("浏览器模式：%s", url)
    message_box(f"{status}\n{opening}\n{url}\n\n请保留此对话框；使用完毕后点击“确定”退出软件。",
                title=f"{TITLE} · 浏览器模式")


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true", help="Use the default browser instead of WebView2")
    mode.add_argument("--no-browser", action="store_true", help="Start only the local service for testing")
    mode.add_argument("--check-browser", action="store_true", help="Check bundled Playwright Chromium offline")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in the range 1–65535")

    prepare_runtime()
    if args.check_browser:
        asyncio.run(check_browser())
        return 0

    from web_server import app

    local_app = TrustedHostMiddleware(app, allowed_hosts=[HOST, "localhost"])
    config = uvicorn.Config(
        local_app, host=HOST, port=args.port, loop="none", http="h11",
        ws="none", lifespan="on", log_config=None, log_level="warning",
        proxy_headers=False, timeout_graceful_shutdown=4,
    )
    server = ReadyServer(config)
    if args.no_browser:
        server.run()
        return 0 if server.started else 1

    url = f"http://{HOST}:{args.port}/"
    with local_service(server):
        logger.info("%s v%s 已启动：%s", APP_NAME, VERSION, url)
        if not args.browser:
            try:
                if show_desktop(url):
                    return 0
            except Exception:
                logger.exception("独立窗口打开失败")
        show_in_browser(url, fallback=not args.browser)
    return 0


def entrypoint():
    try:
        stream = nullcontext(sys.stderr)
        if IS_FROZEN:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            stream = LOG_PATH.open("a", encoding="utf-8", buffering=1)
        with stream as output, redirect_stdout(output), redirect_stderr(output):
            logging.basicConfig(level=logging.INFO, stream=output, format="%(asctime)s %(levelname)s %(message)s")
            try:
                return main()
            except KeyboardInterrupt:
                return 0
            except (Exception, SystemExit) as exc:
                if isinstance(exc, SystemExit) and exc.code == 0:
                    return 0
                logger.exception("工作台启动失败")
                if IS_FROZEN:
                    message_box(f"工作台启动失败。\n{exc}\n\n日志：{LOG_PATH}", error=True)
                return 1
    except OSError as exc:
        message_box(f"无法写入本地日志目录：\n{LOG_PATH.parent}\n\n{exc}", error=True)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint())
