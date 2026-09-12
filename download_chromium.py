# -*- coding: utf-8 -*-
"""用 urllib(OSSL) 下载 playwright 的 Chromium 压缩包并解压到位。"""
import os
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://cdn.playwright.dev/builds/cft/151.0.7922.34/win64"

# 相对脚本所在目录定位，clone 到任何路径都能用
HERE = Path(__file__).resolve().parent
ROOT = HERE / ".pw-browsers"
TMP = HERE / ".pw-tmp"

JOBS = [
    ("chrome-win64.zip", "chromium-1234", "chrome-win64/chrome.exe"),
    ("chrome-headless-shell-win64.zip", "chromium_headless_shell-1234",
     "chrome-headless-shell-win64/chrome-headless-shell.exe"),
]


def download(url: str, dest: Path):
    t0 = time.time()
    last = 0.0
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while True:
            chunk = resp.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 5:
                pct = f"{done * 100 // total}%" if total else f"{done} B"
                speed = done / max(now - t0, 0.1) / 1e6
                print(f"  {dest.name}: {pct} ({speed:.1f} MB/s)", flush=True)
                last = now
    print(f"  OK {dest.name}: {done / 1e6:.1f} MB in {time.time() - t0:.0f}s", flush=True)


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    for fname, target_dir, exe_rel in JOBS:
        url = f"{BASE}/{fname}"
        zpath = TMP / fname
        print(f">> {url}", flush=True)
        if not zpath.exists() or zpath.stat().st_size < 1_000_000:
            download(url, zpath)
        dest = ROOT / target_dir
        print(f"  解压到 {dest} …", flush=True)
        if dest.exists():
            shutil.rmtree(dest)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
        (dest / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
        exe = dest / exe_rel
        print(f"  chrome.exe 存在: {exe.exists()} -> {exe}", flush=True)
        zpath.unlink()
    shutil.rmtree(TMP, ignore_errors=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
