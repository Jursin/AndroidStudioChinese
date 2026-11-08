"""Minimal Java .properties reader/merger that preserves the original text.

Only the operation we need is supported: merge a translation overlay over a
base file, keeping every untouched logical line byte-for-byte (comments,
escaping, line continuations and UTF-8 Chinese all survive).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Line:
    key: str | None  # None for comments and blank lines
    raw: str  # logical line (continuations joined with "\n")


def _is_continued(line: str) -> bool:
    n = 0
    for ch in reversed(line):
        if ch != "\\":
            break
        n += 1
    return n % 2 == 1


def logical_lines(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if buf:
            buf.append(line)
            if not _is_continued(line):
                out.append("\n".join(buf))
                buf = []
        elif _is_continued(line):
            buf = [line]
        else:
            out.append(line)
    if buf:
        out.append("\n".join(buf))
    return out


def parse_key(logical: str) -> str | None:
    s = logical.lstrip()
    if not s or s[0] in "#!":
        return None
    key: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            key.append(s[i : i + 2])
            i += 2
            continue
        if ch in "=: \t\f":
            break
        key.append(ch)
        i += 1
    return "".join(key)


def parse_value(logical: str) -> str:
    s = logical.lstrip()
    if not s or s[0] in "#!":
        return ""
    i = 0
    while i < len(s) and s[i] not in "=: \t\f":
        if s[i] == "\\" and i + 1 < len(s):
            i += 1
        i += 1
    while i < len(s) and s[i] in " \t\f":
        i += 1
    if i < len(s) and s[i] in "=:":
        i += 1
    while i < len(s) and s[i] in " \t\f":
        i += 1
    return s[i:]


def entries(text: str) -> list[Line]:
    return [Line(parse_key(raw), raw) for raw in logical_lines(text)]


def key_map(text: str) -> dict[str, str]:
    """key -> raw logical line."""
    out: dict[str, str] = {}
    for entry in entries(text):
        if entry.key is not None:
            out[entry.key] = entry.raw
    return out


def values(text: str) -> dict[str, str]:
    """key -> raw (still escaped) value."""
    out: dict[str, str] = {}
    for raw in logical_lines(text):
        key = parse_key(raw)
        if key is not None:
            out[key] = parse_value(raw)
    return out


def merge(base: str, overlay: str) -> str:
    """Overlay wins per key; order and comments of the base are preserved."""
    over = key_map(overlay)
    used: set[str] = set()
    out: list[str] = []
    for entry in entries(base):
        if entry.key is None:
            out.append(entry.raw)
        elif entry.key in over:
            out.append(over[entry.key])
            used.add(entry.key)
        else:
            out.append(entry.raw)

    missing = [k for k in over if k not in used]
    if missing:
        if out and out[-1].strip():
            out.append("")
        for key in missing:
            out.append(over[key])
    return "\n".join(out) + "\n"


def load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_tree(root: Path) -> dict[str, str]:
    """relative path -> text, for every file below root."""
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return out
