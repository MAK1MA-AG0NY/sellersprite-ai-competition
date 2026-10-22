"""Configuration loading for FETA; credentials are never persisted by this module."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        if "=" not in text:
            raise ConfigError(f"{path.name}:{line_no} 不是 KEY=VALUE 格式")
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError(f"{path.name}:{line_no} 配置键无效")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _bool(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是 true/false")


def _int(values: Mapping[str, str], key: str, default: int) -> int:
    try:
        number = int(values.get(key, str(default)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是整数") from exc
    return number


def _float(values: Mapping[str, str], key: str, default: float) -> float:
    try:
        number = float(values.get(key, str(default)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是数字") from exc
    if number <= 0 or number > 168:
        raise ConfigError(f"{key} 必须在 0 到 168 小时之间")
    return number


@dataclass(frozen=True)
class Settings:
    db_path: Path
    env_file: Path
    mail_provider: str
    smtp_host: str
    smtp_port: int
    imap_host: str
    imap_port: int
    mail_user: str = field(repr=False)
    mail_app_password: str = field(repr=False)
    alert_email: str = field(repr=False)
    query_interval_hours: float
    reply_timeout_minutes: int
    display_timezone: str
    dry_run: bool

    @property
    def smtp_configured(self) -> bool:
        return bool(self.mail_user and self.mail_app_password and self.smtp_host and self.smtp_port)

    @property
    def imap_configured(self) -> bool:
        return bool(self.mail_user and self.mail_app_password and self.imap_host and self.imap_port)


def load_settings(environ: Mapping[str, str] | None = None, env_file: str | Path | None = None) -> Settings:
    """Load `.env.feta`, then let process environment override it; defaults are offline-safe."""
    process_env = os.environ if environ is None else environ
    selected_env_file = Path(env_file or process_env.get("FETA_ENV_FILE", ROOT / ".env.feta"))
    if not selected_env_file.is_absolute():
        selected_env_file = (Path.cwd() / selected_env_file).resolve()
    values = _read_env_file(selected_env_file)
    values.update({key: str(value) for key, value in process_env.items()})

    smtp_port = _int(values, "SMTP_PORT", 465)
    imap_port = _int(values, "IMAP_PORT", 993)
    timeout = _int(values, "REPLY_TIMEOUT_MINUTES", 60)
    if not 1 <= smtp_port <= 65535 or not 1 <= imap_port <= 65535:
        raise ConfigError("SMTP_PORT/IMAP_PORT 须为 1–65535")
    if not 1 <= timeout <= 7 * 24 * 60:
        raise ConfigError("REPLY_TIMEOUT_MINUTES 须为 1–10080")

    db_path = Path(values.get("FETA_DB_PATH", ROOT / "deep_results" / "feta.sqlite3")).expanduser()
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()
    provider = values.get("MAIL_PROVIDER", "gmail").strip().casefold()
    if provider != "gmail":
        raise ConfigError("当前 MVP 仅配置 Gmail SMTP/IMAP；未实现 OAuth")
    dry_run = _bool(values.get("DRY_RUN", "true"), "DRY_RUN")
    return Settings(
        db_path=db_path,
        env_file=selected_env_file,
        mail_provider=provider,
        smtp_host=values.get("SMTP_HOST", "smtp.gmail.com").strip(),
        smtp_port=smtp_port,
        imap_host=values.get("IMAP_HOST", "imap.gmail.com").strip(),
        imap_port=imap_port,
        mail_user=values.get("MAIL_USER", "").strip(),
        mail_app_password=values.get("MAIL_APP_PASSWORD", "").strip().replace(" ", ""),
        alert_email=values.get("ALERT_EMAIL", "").strip(),
        query_interval_hours=_float(values, "QUERY_INTERVAL_HOURS", 12.0),
        reply_timeout_minutes=timeout,
        display_timezone=values.get("TZ_DISPLAY", "America/Chicago").strip(),
        dry_run=dry_run,
    )


def require_mail_config(settings: Settings, *, smtp: bool = False, imap: bool = False) -> None:
    missing = []
    if not settings.mail_user:
        missing.append("MAIL_USER")
    if not settings.mail_app_password:
        missing.append("MAIL_APP_PASSWORD")
    if smtp and (not settings.smtp_host or not settings.smtp_port):
        missing.extend(name for name, value in (("SMTP_HOST", settings.smtp_host), ("SMTP_PORT", settings.smtp_port)) if not value)
    if imap and (not settings.imap_host or not settings.imap_port):
        missing.extend(name for name, value in (("IMAP_HOST", settings.imap_host), ("IMAP_PORT", settings.imap_port)) if not value)
    if missing:
        raise ConfigError("邮件配置缺少：" + ", ".join(missing))
    if smtp and imap and settings.smtp_host == settings.imap_host and settings.smtp_port == settings.imap_port:
        raise ConfigError("SMTP 与 IMAP 端口配置冲突")


def validate_alert_destination(settings: Settings) -> None:
    if not settings.alert_email or "@" not in settings.alert_email:
        raise ConfigError("告警前必须配置有效 ALERT_EMAIL")
    if settings.alert_email.casefold() == settings.mail_user.casefold():
        raise ConfigError("ALERT_EMAIL 应与查询邮箱不同，避免把自身告警当成 FedEx 回复")
