#!/usr/bin/env python3
"""Generate the native runtime's table of CPython codec names.

The runtime implements the utf-8, ascii and latin-1 codecs. For any other
name, CPython's behaviour depends on whether the name is a codec at all:
``unknown encoding: X`` for an unknown name, "'X' is not a text encoding"
for a binary transform such as base64, and a working codec for a text
encoding such as cp1252 (which the runtime reports as not implemented).
This generator lists the names, normalized as ``codecs.normalize_encoding``
does after lowercasing, from the host CPython's ``encodings`` package, and
writes them into ``pcc/runtime/py/py_str_accessors.py`` between the
GENERATED CODEC NAMES markers.

Usage:
    uv run python scripts/gen_codec_names.py            # write
    uv run python scripts/gen_codec_names.py --check    # verify only
"""

from __future__ import annotations

import argparse
import codecs
import encodings
import encodings.aliases
import pkgutil
import sys
from pathlib import Path

BEGIN = "# BEGIN GENERATED CODEC NAMES (scripts/gen_codec_names.py)\n"
END = "# END GENERATED CODEC NAMES\n"
TARGET = Path("pcc/runtime/py/py_str_accessors.py")


def repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


def codec_names() -> tuple[list[str], list[str]]:
    """(text codec names, binary transform names), normalized and sorted."""
    names = set(encodings.aliases.aliases) | set(encodings.aliases.aliases.values())
    names |= {module.name for module in pkgutil.iter_modules(encodings.__path__)}
    text: set[str] = set()
    binary: set[str] = set()
    for name in names:
        try:
            info = codecs.lookup(name)
        except LookupError:
            continue  # a helper module or a platform codec (mbcs, oem)
        normalized = encodings.normalize_encoding(name.lower())
        (text if info._is_text_encoding else binary).add(normalized)
    return sorted(text), sorted(binary)


def render() -> str:
    text, binary = codec_names()
    version = ".".join(str(part) for part in sys.version_info[:3])
    return (
        BEGIN
        + f"# CPython {version}: {len(text)} text codec names, {len(binary)} binary transforms.\n"
        + "def _codec_text_names():\n"
        + '    return cstr(" ' + " ".join(text) + ' ")\n'
        + "\n\n"
        + "def _codec_binary_names():\n"
        + '    return cstr(" ' + " ".join(binary) + ' ")\n'
        + END
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed table is stale")
    options = parser.parse_args()
    path = repo_root() / TARGET
    source = path.read_text(encoding="utf-8")
    start = source.index(BEGIN)
    end = source.index(END, start) + len(END)
    updated = source[:start] + render() + source[end:]
    if options.check:
        if updated != source:
            print(f"{TARGET} codec names are stale; run scripts/gen_codec_names.py", file=sys.stderr)
            return 1
        return 0
    path.write_text(updated, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
