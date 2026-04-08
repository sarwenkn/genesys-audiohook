import json
import re
import unicodedata
from typing import Iterable, Optional, Set

def format_json(obj: dict) -> str:
    return json.dumps(obj, indent=2)

def parse_iso8601_duration(duration_str: str) -> float:
    match = re.match(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', duration_str)
    if not match:
        raise ValueError(f"Invalid ISO 8601 duration format: {duration_str}")
    days, hours, minutes, seconds = match.groups()
    total_seconds = 0
    if days:
        total_seconds += int(days) * 86400
    if hours:
        total_seconds += int(hours) * 3600
    if minutes:
        total_seconds += int(minutes) * 60
    if seconds:
        total_seconds += float(seconds)
    return total_seconds


def _is_latin(cp: int) -> bool:
    # Basic Latin + Latin-1 Supplement + Latin Extended blocks.
    return (
        0x0041 <= cp <= 0x007A
        or 0x00C0 <= cp <= 0x024F
        or 0x1E00 <= cp <= 0x1EFF
        or 0x2C60 <= cp <= 0x2C7F
        or 0xA720 <= cp <= 0xA7FF
    )


def _is_han(cp: int) -> bool:
    # CJK Unified Ideographs + extensions commonly used for Chinese.
    return (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x2A6DF
        or 0x2A700 <= cp <= 0x2B73F
        or 0x2B740 <= cp <= 0x2B81F
        or 0x2B820 <= cp <= 0x2CEAF
        or 0x2CEB0 <= cp <= 0x2EBEF
    )


def sanitize_transcript_text(
    text: str,
    allowed_scripts: Iterable[str] = ("latin", "han"),
    *,
    min_letters: int = 2,
) -> Optional[str]:
    """
    Keep only characters belonging to allowed scripts + whitespace/digits/punctuation.

    This is a safety net for STT systems that sometimes "jump" to other scripts
    (e.g. Devanagari/Bengali/Kana) due to noise/auto-language detection.

    Returns:
      - sanitized string if it contains at least `min_letters` letters after filtering
      - None if there's nothing usable to keep
    """
    if not text:
        return None

    allowed: Set[str] = {s.strip().lower() for s in allowed_scripts if s and str(s).strip()}

    kept_chars = []
    letter_count = 0

    for ch in text:
        cp = ord(ch)

        if ch.isspace() or ch.isdigit():
            kept_chars.append(ch)
            continue

        cat = unicodedata.category(ch)
        if cat and cat[0] in ("P", "S"):  # punctuation/symbols
            kept_chars.append(ch)
            continue

        if ch.isalpha():
            ok = False
            if "latin" in allowed and _is_latin(cp):
                ok = True
            if "han" in allowed and _is_han(cp):
                ok = True

            if ok:
                kept_chars.append(ch)
                letter_count += 1
            continue

        # Keep non-spacing marks only if we already kept something (helps with accent marks).
        if cat == "Mn" and kept_chars:
            kept_chars.append(ch)

    sanitized = "".join(kept_chars).strip()
    if not sanitized or letter_count < min_letters:
        return None

    return sanitized
