# -*- coding: utf-8 -*-
"""Conservative carrier suggestions; never rewrite the stored tracking number."""
from __future__ import annotations

import re

_TRACK_RE = re.compile(r"\A[A-Za-z0-9]{5,50}\Z")


def suggest_carrier(tracking_number: str) -> dict[str, str]:
    value = tracking_number.strip() if isinstance(tracking_number, str) else ""
    if not _TRACK_RE.fullmatch(value):
        return {"carrier_hint": "auto", "confidence": "low", "reason": "格式未识别；交由官网自动识别或人工确认"}
    if value.isdigit() and len(value) == 12:
        return {"carrier_hint": "fedex", "confidence": "medium", "reason": "12 位数字为 FedEx 候选；仅提示，仍需核对"}
    if value.isdigit() and len(value) in (30, 34) and value.startswith("420"):
        return {"carrier_hint": "usps", "confidence": "low", "reason": f"{len(value)} 位 420 前缀扩展条码候选；完整保留原号，不截断"}
    if value.isdigit() and len(value) in (20, 22):
        return {"carrier_hint": "usps", "confidence": "medium", "reason": f"{len(value)} 位数字为 USPS 候选；仅提示，仍需核对"}
    return {"carrier_hint": "auto", "confidence": "low", "reason": "无明确长度/格式规则；交由官网自动识别或人工确认"}


def carrier_value(tracking_number: str, override: str | None = None) -> dict[str, str]:
    if override in {"fedex", "usps"}:
        return {"carrier": override, "carrier_source": "manual", "carrier_hint": suggest_carrier(tracking_number)["carrier_hint"]}
    hint = suggest_carrier(tracking_number)
    return {"carrier": hint["carrier_hint"], "carrier_source": "automatic", **hint}
