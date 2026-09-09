#!/usr/bin/env python3
"""Regenerate ``docs/investigations/INDEX.md``.

Reads every ``docs/investigations/*.md`` file (skipping ``INDEX.md``),
extracts a title from the ``# `` heading and a one-line summary from
the first prose paragraph after the heading, and writes a grouped
index of every investigation so AGENTS / humans can scan available
documents without ``ls``-ing the directory.

Run:

    env -u LC_ALL uv run python scripts/regen_investigations_index.py

The script is idempotent — re-running overwrites ``INDEX.md`` with
the current state of the directory.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INVESTIGATIONS = REPO_ROOT / "docs" / "investigations"


def extract_summary(path: Path) -> tuple[str | None, str | None]:
    """Return ``(title, one_line_summary)`` for ``path``.

    Title comes from the first ``# ...`` heading (``Investigation:`` prefix
    is stripped). Summary is the first substantial prose line that follows
    — headings, bullet lists, fences, table rows, and bare status keywords
    are skipped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None, None
    lines = text.splitlines()
    title: str | None = None
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            if title.lower().startswith("investigation:"):
                title = title.split(":", 1)[1].strip()
            break
    summary: str | None = None
    for ln in lines[1:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if s.startswith("```"):
            continue
        if s.startswith("|"):
            continue
        if s.startswith("- "):
            continue
        if s.startswith("* "):
            continue
        if s.startswith("**Status"):
            continue
        if re.match(r"^[a-z]+\s*$", s.lower()):
            continue
        summary = s
        break
    if summary:
        summary = re.sub(r"\s+", " ", summary)
        if len(summary) > 140:
            summary = summary[:137].rstrip() + "..."
    return title, summary


def topic_for(name: str) -> str:
    if name.startswith("bootstrap-five-gc"):
        return "gc"
    if name.startswith("gc-"):
        return "gc"
    if name.startswith("bootstrap-"):
        return "bootstrap"
    if name.startswith("python-data-model"):
        return "python-data-model"
    if name.startswith("python-self-host"):
        return "python-self-host"
    if name.startswith("python-"):
        return "python"
    if name.startswith("lua-"):
        return "lua"
    if name.startswith("llvm-") or name.split("-")[0] == "llvm":
        return "llvm"
    if name.startswith("self-backend") or "self-backend" in name:
        return "self-backend"
    if name.startswith("pcc-py-codegen") or "codegen" in name:
        return "codegen"
    if name.startswith("pcc1"):
        return "pcc1-stage"
    if any(
        name.startswith(prefix)
        for prefix in (
            "sqlite",
            "postgres",
            "pcre",
            "zlib",
            "lz4",
            "zstd",
            "openssl",
            "readline",
            "nginx",
        )
    ):
        return "projects"
    return "other"


TOPIC_ORDER = [
    "codegen",
    "self-backend",
    "pcc1-stage",
    "bootstrap",
    "gc",
    "python-self-host",
    "python-data-model",
    "python",
    "lua",
    "projects",
    "llvm",
    "other",
]


def main() -> int:
    files = sorted(
        p
        for p in INVESTIGATIONS.iterdir()
        if p.is_file() and p.suffix == ".md" and p.name != "INDEX.md"
    )

    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(topic_for(f.stem), []).append(f)

    lines: list[str] = []
    lines.append("# Investigations index")
    lines.append("")
    lines.append("Auto-generated from `docs/investigations/*.md`.")
    lines.append(
        "Each entry shows the doc title and a one-line summary pulled "
        "from the first prose paragraph."
    )
    lines.append("")
    lines.append(
        "Historical search index, not current implementation truth. Start with "
        "source and execution; check a matching experiment's revision and later "
        "corrections before adopting its conclusion."
    )
    lines.append("")
    lines.append(
        "Regenerate with `env -u LC_ALL uv run python "
        "scripts/regen_investigations_index.py`."
    )
    lines.append("")

    seen_topics: set[str] = set()
    for topic in TOPIC_ORDER:
        if topic not in groups:
            continue
        seen_topics.add(topic)
        lines.append(f"## {topic}")
        lines.append("")
        for f in sorted(groups[topic]):
            title, summary = extract_summary(f)
            display = title or f.stem
            lines.append(f"- [{f.name}]({f.name}) — **{display}**")
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")
    for topic in sorted(set(groups) - seen_topics):
        lines.append(f"## {topic}")
        lines.append("")
        for f in sorted(groups[topic]):
            title, summary = extract_summary(f)
            display = title or f.stem
            lines.append(f"- [{f.name}]({f.name}) — **{display}**")
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")

    (INVESTIGATIONS / "INDEX.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    print(
        f"wrote {INVESTIGATIONS / 'INDEX.md'} — {len(files)} entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
