"""Double-click entry point for the Windows portable release."""

import argparse
import asyncio
import os
import sys
import traceback
import webbrowser

import uvicorn
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app_paths import APP_NAME, ASSET_DIR, DATA_DIR, IS_FROZEN, VERSION

HOST = "127.0.0.1"


class DesktopServer(uvicorn.Server):
    def __init__(self, config, open_browser):
        super().__init__(config)
        self.open_browser = open_browser

    async def startup(self, sockets=None):
        await super().startup(sockets)
        if not self.started:
            return
        url = f"http://{HOST}:{self.config.port}/"
        print(f"{APP_NAME} v{VERSION} 已启动：{url}", flush=True)
        print(f"数据目录：{DATA_DIR}\n使用完毕后按 Ctrl+C 退出。", flush=True)
        if self.open_browser:
            try:
                if webbrowser.open(url):
                    return
            except (OSError, webbrowser.Error):
                pass
            print(f"无法自动打开浏览器，请手动访问 {url}", flush=True)


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
    """Exercise both bundled browser variants without using external sites."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        for headless in (True, False):
            async with await playwright.chromium.launch(headless=headless) as browser:
                page = await browser.new_page()
                await page.set_content("<title>SellerSprite release check</title>")
                if await page.title() != "SellerSprite release check":
                    raise RuntimeError("内置浏览器未能加载测试页。")
                print(f"Chromium ({'headless' if headless else 'headed'}): OK", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the default browser")
    parser.add_argument("--check-browser", action="store_true", help="Check bundled Chromium offline")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in the range 1–65535")

    prepare_runtime()
    if args.check_browser:
        asyncio.run(check_browser())
        return 0

    from web_server import app

    # The portable console is local-only. Reject non-local Host headers as well.
    local_app = TrustedHostMiddleware(app, allowed_hosts=[HOST, "localhost"])
    config = uvicorn.Config(
        local_app, host=HOST, port=args.port, loop="none", http="h11",
        ws="none", lifespan="on", log_level="warning", proxy_headers=False,
    )
    server = DesktopServer(config, open_browser=not args.no_browser)
    server.run()
    return 0 if server.started else 1


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 0
    except Exception:
        traceback.print_exc()
        code = 1
    except SystemExit as exc:
        code = exc.code
    if code and IS_FROZEN and sys.stdin.isatty():
        print("启动失败。如果端口被占用，请先关闭已运行的控制台。")
        try:
            input("按 Enter 关闭窗口…")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
