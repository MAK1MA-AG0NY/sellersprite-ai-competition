"""Conservative, experimental MIME/text parser. No HTML or attachments are executed."""
from __future__ import annotations

from email.message import Message
from email.utils import getaddresses
from html.parser import HTMLParser
import re

PARSER_VERSION = "0.1-experimental"
MAX_MIME_PARTS = 50
MAX_EXTRACTED_TEXT_CHARS = 256_000
_STATUS_NEGATION_PREFIX = re.compile(
    r"\b(?:not|never|cannot|can't|couldn't|unable\s+to|failed\s+to|no\s+longer|has\s+not|have\s+not|hasn't|haven't)"
    r"(?:\s+\w+){0,2}\s*$", re.I
)
_STATUS_PATTERNS = (
    ("RETURN_TO_SENDER", re.compile(r"\b(return(?:ed|ing)?\s+to\s+sender)\b", re.I)),
    ("DELIVERY_ATTEMPTED", re.compile(r"\b(delivery\s+attempted|attempted\s+delivery)\b", re.I)),
    ("OUT_FOR_DELIVERY", re.compile(r"\b(out\s+for\s+delivery)\b", re.I)),
    ("DELIVERED", re.compile(r"\b(delivered|signed\s+for)\b", re.I)),
    ("EXCEPTION", re.compile(r"\b(exception|delivery\s+exception|unable\s+to\s+deliver)\b", re.I)),
    ("DELAYED", re.compile(r"\b(delayed|delivery\s+delay)\b", re.I)),
    ("PICKED_UP", re.compile(r"\b(picked\s+up|picked\s+up\s+by\s+fedex)\b", re.I)),
    ("IN_TRANSIT", re.compile(r"\b(in\s+transit|on\s+the\s+way|arrived\s+at\s+fedex|departed\s+fedex)\b", re.I)),
    ("LABEL_CREATED", re.compile(r"\b(label\s+created|shipment\s+information\s+sent)\b", re.I)),
)
_ETA_RE = re.compile(r"(?:estimated|scheduled|expected|anticipated)\s+delivery(?:\s+(?:date|time|window))?\s*[:\-]?\s*([^\r\n]{1,120})", re.I)


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in {"script", "style", "noscript"}:
            self._skip += 1
        elif not self._skip and tag.casefold() in {"br", "p", "div", "li", "tr", "td", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        elif not self._skip and tag.casefold() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    charset = part.get_content_charset() or "utf-8"
    if payload is not None:
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    value = part.get_payload()
    return value if isinstance(value, str) else ""


def extract_message_text(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    used_chars = 0
    for index, part in enumerate(parts):
        if index >= MAX_MIME_PARTS or used_chars >= MAX_EXTRACTED_TEXT_CHARS:
            break
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        decoded = _decode_part(part)[:MAX_EXTRACTED_TEXT_CHARS - used_chars]
        used_chars += len(decoded)
        if content_type == "text/plain":
            plain.append(decoded)
        else:
            html.append(decoded)
    if plain:
        return "\n".join(plain)
    if not html:
        return ""
    parser = _VisibleHTML()
    for part in html:
        parser.feed(part)
    parser.close()
    return " ".join("".join(parser.parts).split())


def sender_authentication(message: Message) -> tuple[bool, str, str]:
    from_values = message.get_all("From", [])
    addresses = [address.casefold().strip() for _, address in getaddresses(from_values) if address]
    def is_fedex_domain(domain: str) -> bool:
        value = domain.casefold().rstrip(".")
        return value == "fedex.com" or value.endswith(".fedex.com")

    domains = [address.rsplit("@", 1)[1] for address in addresses if "@" in address]
    sender = ",".join(addresses)[:320]
    if not domains or not all(is_fedex_domain(domain) for domain in domains):
        return False, sender, "sender_domain_rejected"
    auth_headers = [str(value).casefold() for value in message.get_all("Authentication-Results", [])]
    if not auth_headers:
        return False, sender, "authentication_results_missing"
    for header in auth_headers:
        if re.search(r"\b(?:spf|dkim|dmarc)=(?:fail|softfail|permerror|temperror|none)\b", header):
            return False, sender, "authentication_failed"

    aligned_pass = False
    for header in auth_headers:
        # Filter for a Gmail-authserv-id and FedEx-aligned pass claim. This parser cannot
        # cryptographically prove who inserted this header; all accepted statuses stay manual-review.
        if not re.match(r"\s*(?:mx\.)?google\.com\s*;", header):
            continue
        identities = []
        for pattern in (
            r"\bdmarc=pass\b[^;]*?\bheader\.from=([a-z0-9.-]+)",
            r"\bdkim=pass\b[^;]*?\bheader\.d=([a-z0-9.-]+)",
            r"\bspf=pass\b[^;]*?\bsmtp\.mailfrom=([a-z0-9.@_-]+)",
        ):
            identities.extend(match.group(1).rsplit("@", 1)[-1] for match in re.finditer(pattern, header))
        if any(is_fedex_domain(domain) for domain in identities):
            aligned_pass = True
            break
    return (True, sender, "aligned_gmail_auth_claim") if aligned_pass else (False, sender, "authentication_unverified")


def parse_reply_text(text: str, expected_tracking_numbers: list[str], *, destination_timezone: str,
                     received_at_utc: str) -> list[dict]:
    """Extract only known shipment IDs; all outputs require a human review until real samples exist."""
    positions = []
    for number in expected_tracking_numbers:
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(number) + r"(?![A-Za-z0-9])", re.I)
        match = pattern.search(text)
        if match:
            positions.append((match.start(), match.end(), number))
    positions.sort()
    results = []
    for index, (start, end, number) in enumerate(positions):
        next_start = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        preceding = text[max(0, start - 180):start]
        following = text[end:min(next_start, end + 1600)]
        segment = preceding.rsplit("\n", 2)[-2:] and "\n".join(preceding.splitlines()[-2:]) + "\n" + following
        if not segment.strip():
            segment = following
        normalized = "UNKNOWN"
        raw_status = None
        candidates = []
        for candidate, pattern in _STATUS_PATTERNS:
            for found in pattern.finditer(segment):
                prefix = segment[max(0, found.start() - 60):found.start()]
                if _STATUS_NEGATION_PREFIX.search(prefix):
                    continue
                candidates.append((found.start(), candidate, found.group(0)[:250]))
        if candidates:
            # The last explicit phrase is usually the message's current status; any
            # negated phrase is excluded (e.g. "not delivered due to exception").
            _, normalized, raw_status = max(candidates, key=lambda item: item[0])
        eta_match = _ETA_RE.search(segment)
        eta_raw = " ".join(eta_match.group(1).split())[:120] if eta_match else None
        results.append({
            "tracking_number": number,
            "raw_status": raw_status,
            "normalized_status": normalized,
            "raw_eta_text": eta_raw,
            "eta_datetime_utc": None,
            "eta_date_local": None,
            "destination_timezone": destination_timezone,
            "scan_time_utc": None,
            "source": "fedex_email",
            "received_at_utc": received_at_utc,
            "parse_confidence": "low",
            "parser_version": PARSER_VERSION,
            "needs_manual_review": True,
        })
    return results
