"""Keep writable data separate from the bundled application assets."""

import os
import sys
from pathlib import Path

APP_NAME = "SellerSpriteCompetition"
VERSION = "0.2.0-preview.1"
ASSET_DIR = Path(__file__).resolve().parent
IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    DATA_DIR = local_app_data / APP_NAME
else:
    DATA_DIR = ASSET_DIR
