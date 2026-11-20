"""Safe recovery for Meta JSON Latin-1/UTF-8 (and CESU-8 surrogate) mojibake."""

from __future__ import annotations

from typing import Any


def _decode_latin1_run(run: str) -> str:
    """Attempt to recover UTF-8 or paired CESU-8 bytes from a single <=0xFF run."""
    if not any(ord(ch) >= 0x80 for ch in run):
        return run

    raw = run.encode("latin-1")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # Meta exports sometimes encode 4-byte emoji as two UTF-8-encoded UTF-16 surrogates.
    try:
        surrogate_str = raw.decode("utf-8", errors="surrogatepass")
        decoded = surrogate_str.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return run

    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in decoded):
        return run
    return decoded


def fix_mojibake(text: str) -> str:
    """Recover Meta Latin-1/UTF-8 mojibake while preserving valid UTF-8 and genuine Latin-1 text."""
    if not text or text.isascii():
        return text

    out: list[str] = []
    buf: list[str] = []
    in_latin1_run = ord(text[0]) <= 0xFF

    for ch in text:
        is_latin1_char = ord(ch) <= 0xFF
        if is_latin1_char != in_latin1_run:
            segment = "".join(buf)
            out.append(_decode_latin1_run(segment) if in_latin1_run else segment)
            buf.clear()
            in_latin1_run = is_latin1_char
        buf.append(ch)

    if buf:
        segment = "".join(buf)
        out.append(_decode_latin1_run(segment) if in_latin1_run else segment)

    return "".join(out)


def fix_mojibake_tree(obj: Any) -> Any:
    """Recursively fix mojibake across nested dicts, lists, and tuples without mutating input."""
    if isinstance(obj, str):
        return fix_mojibake(obj)
    if isinstance(obj, list):
        return [fix_mojibake_tree(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(fix_mojibake_tree(item) for item in obj)
    if isinstance(obj, dict):
        return {fix_mojibake_tree(k): fix_mojibake_tree(v) for k, v in obj.items()}
    return obj
