"""Build a self-contained Windows x64 release from a clean dependency env."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
from uuid import uuid4
import zipfile

from app_paths import APP_NAME, ASSET_DIR as ROOT, VERSION


def browser_layout():
    import playwright

    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    revisions = {item["name"]: item["revision"] for item in json.loads(manifest.read_text(encoding="utf-8"))["browsers"]}
    return {
        f"chromium-{revisions['chromium']}": "chrome-win64/chrome.exe",
        f"chromium_headless_shell-{revisions['chromium-headless-shell']}":
            "chrome-headless-shell-win64/chrome-headless-shell.exe",
    }


def prepare_browsers(cache, layout, env, download):
    def missing():
        return [name for name, executable in layout.items() if not (cache / name / executable).is_file()]

    if missing() and download:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            env={**env, "PLAYWRIGHT_BROWSERS_PATH": str(cache)}, cwd=ROOT, check=True,
        )
    if missing():
        raise SystemExit(f"Missing matching Chromium in {cache}: {', '.join(missing())}")


def add_tree(archive, source, prefix):
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, (prefix / path.relative_to(source).as_posix()).as_posix())


def build(browser_cache=None):
    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        raise SystemExit("Build this release on Windows x64 using 64-bit Python.")

    if sys.maxsize <= 2**32:
        raise SystemExit("Use 64-bit Python for this release.")

    cache = Path(browser_cache).resolve() if browser_cache else ROOT / ".pw-browsers"
    work_root = ROOT / ".release-build"
    output_root = ROOT / "dist"
    for folder in (work_root, output_root):
        folder.mkdir(exist_ok=True)
        if folder.resolve() != folder:
            raise SystemExit(f"Build output must be inside the project: {folder}")
    archive = output_root / f"{APP_NAME}-v{VERSION}-windows-x64.zip"
    layout = browser_layout()

    # Build files carry no local data; use a regular project staging directory.
    work = work_root / f"stage-{uuid4().hex[:12]}"
    work.mkdir()
    try:
        env = {
            **os.environ,
            "TEMP": str(work), "TMP": str(work),
            "PYINSTALLER_CONFIG_DIR": str(work_root / "pyinstaller-cache"),
        }
        prepare_browsers(cache, layout, env, download=browser_cache is None)
        icon = ROOT / "assets" / "app.ico"
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--noupx",
            "--name", APP_NAME, "--distpath", str(work / "app"), "--workpath", str(work / "work"),
            "--specpath", str(work), "--contents-directory", "_internal",
            "--icon", str(icon), "--add-data", f"{icon}:assets",
            "--add-data", f"{ROOT / 'web_index.html'}:.",
            "--hidden-import", "uvicorn.protocols.http.h11_impl", "--hidden-import", "uvicorn.lifespan.on",
            "release_launcher.py",
        ], env=env, cwd=ROOT, check=True)

        app_dir = work / "app" / APP_NAME
        internal = app_dir / "_internal"
        if not (internal / "playwright" / "driver" / "node.exe").is_file():
            raise SystemExit("Playwright driver was not bundled by PyInstaller.")
        guide = (ROOT / "docs" / "Windows-portable-readme.txt").read_text(encoding="utf-8")
        (app_dir / "README.txt").write_text(f"版本：v{VERSION}\n\n{guide}", encoding="utf-8-sig")

        draft = work / archive.name
        prefix = PurePosixPath(APP_NAME)
        print(f"Creating {archive.name} ...", flush=True)
        with zipfile.ZipFile(draft, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as out:
            add_tree(out, app_dir, prefix)
            # Add browser files directly to avoid deep temporary Windows paths.
            # Do not copy the entire cache (.links etc.) or anything from user data.
            for name in layout:
                add_tree(out, cache / name, prefix / "_internal" / "pw-browsers" / name)
        draft.replace(archive)
    finally:
        if work.resolve() == work:
            shutil.rmtree(work)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-cache", help="An existing trusted Playwright Chromium cache (skip download)")
    args = parser.parse_args()
    print(build(args.browser_cache))
