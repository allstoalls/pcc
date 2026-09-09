#!/usr/bin/env python3
"""Index historical investigation excerpts for targeted search.

Extracts denied/confirmed markers and symptom locators into `docs/knowledge/`.
This is lexical extraction, not validation or reconciliation of later verdicts.
The pages are search indexes, not mandatory startup reading or current facts.
Use current code and matching execution as first-hand evidence, then inspect
the relevant original experiment, revision and later corrections.

Usage:
    uv run python scripts/distill_investigations.py [--check]

`--check` exits non-zero when the generated pages are stale, so a test can
keep them in step with the investigations.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVESTIGATIONS = os.path.join(ROOT, "docs", "investigations")
KNOWLEDGE = os.path.join(ROOT, "docs", "knowledge")

DENIED_PAGE = os.path.join(KNOWLEDGE, "denied-experiments.md")
CONFIRMED_PAGE = os.path.join(KNOWLEDGE, "confirmed-root-causes.md")
ROUTING_PAGE = os.path.join(KNOWLEDGE, "symptom-routing.md")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_DENIED_MARK = re.compile(r"\[DENIED\]|\bDENIED\b", re.I)
_CONFIRMED_MARK = re.compile(r"\[CONFIRMED\]|\bCONFIRMED\b")
_DID_NOT_HELP = re.compile(
    r"did ?n[o']t help|no measurable|measured (?:zero|no) |made it slower"
    r"|regressed|no effect|not the cause|ruled out",
    re.I,
)
_NOISE_PREFIX = ("- [ ]", "- [x]", "```", "|")


def _files() -> list[str]:
    names = []
    for name in sorted(os.listdir(INVESTIGATIONS)):
        if name.endswith(".md") and name != "INDEX.md":
            names.append(name)
    return names


def _read(name: str) -> list[str]:
    path = os.path.join(INVESTIGATIONS, name)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().splitlines()


def _title(lines: list[str], fallback: str) -> str:
    for line in lines:
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return fallback


def _status(lines: list[str]) -> str:
    in_status = False
    for line in lines:
        match = _HEADING.match(line)
        if match:
            in_status = match.group(2).strip().lower().startswith("status")
            continue
        if in_status and line.strip():
            return _clean(line.strip())
    return ""


def _clean(text: str) -> str:
    text = text.strip().lstrip("-*0123456789. ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _context_sentence(lines: list[str], index: int) -> str:
    """One readable sentence for the verdict at ``index``.

    A verdict often sits on a heading whose body holds the substance, so this
    prefers the first non-empty prose line after the heading.
    """
    line = lines[index]
    match = _HEADING.match(line)
    if match:
        heading = _clean(match.group(2))
        for follow in lines[index + 1 : index + 8]:
            stripped = follow.strip()
            if not stripped or stripped.startswith(_NOISE_PREFIX):
                continue
            if _HEADING.match(follow):
                break
            return heading + " — " + _clean(stripped)[:400]
        return heading
    return _clean(line)[:400]


def _keywords(lines: list[str]) -> list[str]:
    """Error-text-ish tokens an agent is likely to search for."""
    found: list[str] = []
    seen: set[str] = set()
    for line in lines[:400]:
        for match in re.finditer(r"`([^`]{6,80})`", line):
            token = match.group(1).strip()
            if not token or token in seen:
                continue
            if " " not in token and "." not in token and "_" not in token:
                continue
            if token.startswith(("http", "docs/", "scripts/")):
                continue
            seen.add(token)
            found.append(token)
            if len(found) >= 6:
                return found
    return found


def _collect() -> tuple[list[dict], list[dict], list[dict]]:
    denied: list[dict] = []
    confirmed: list[dict] = []
    routing: list[dict] = []
    for name in _files():
        lines = _read(name)
        title = _title(lines, name)
        status = _status(lines)
        routing.append(
            {
                "file": name,
                "title": title,
                "status": status,
                "keywords": _keywords(lines),
            }
        )
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("```"):
                continue
            if _DENIED_MARK.search(stripped) or _DID_NOT_HELP.search(stripped):
                text = _context_sentence(lines, index)
                if len(text) < 12:
                    continue
                denied.append({"file": name, "title": title, "text": text})
            elif _CONFIRMED_MARK.search(stripped):
                text = _context_sentence(lines, index)
                if len(text) < 12:
                    continue
                confirmed.append({"file": name, "title": title, "text": text})
    return denied, confirmed, routing


def _dedupe(entries: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry["file"], entry["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _group(entries: list[dict]) -> list[tuple[str, str, list[str]]]:
    order: list[tuple[str, str]] = []
    bodies: dict[tuple[str, str], list[str]] = {}
    for entry in entries:
        key = (entry["file"], entry["title"])
        if key not in bodies:
            bodies[key] = []
            order.append(key)
        bodies[key].append(entry["text"])
    return [(f, t, bodies[(f, t)]) for f, t in order]


def _render_denied(entries: list[dict]) -> str:
    groups = _group(_dedupe(entries))
    total = sum(len(items) for _, _, items in groups)
    out = [
        "# Recorded denied experiments",
        "",
        "GENERATED by `scripts/distill_investigations.py` from",
        "`docs/investigations/*.md`. Do not edit by hand.",
        "",
        "Search index of historical markers, not a current prohibition list.",
        "Extraction does not reconcile later corrections or changed prerequisites.",
        "Search by symbol/symptom; inspect the matching experiment and its updates.",
        "Verify current code, source revision and artifacts before reusing a verdict.",
        "Do not repeat a matching failed experiment without identifying new evidence.",
        "",
        f"{total} verdicts across {len(groups)} investigations.",
        "",
    ]
    for name, title, items in groups:
        out.append(f"## [{title}](../investigations/{name})")
        out.append("")
        for text in items:
            out.append(f"- {text}")
        out.append("")
    return "\n".join(out) + "\n"


def _render_confirmed(entries: list[dict]) -> str:
    groups = _group(_dedupe(entries))
    total = sum(len(items) for _, _, items in groups)
    out = [
        "# Recorded confirmations",
        "",
        "GENERATED by `scripts/distill_investigations.py` from",
        "`docs/investigations/*.md`. Do not edit by hand.",
        "",
        "Search index of historical confirmation markers, not verified current facts.",
        "Extraction can include hypotheses, test status and superseded conclusions.",
        "Check the original experiment, revision and later updates against current",
        "code and matching execution before adopting a cause. Read only relevant hits.",
        "",
        f"{total} confirmations across {len(groups)} investigations.",
        "",
    ]
    for name, title, items in groups:
        out.append(f"## [{title}](../investigations/{name})")
        out.append("")
        for text in items:
            out.append(f"- {text}")
        out.append("")
    return "\n".join(out) + "\n"


def _render_routing(entries: list[dict]) -> str:
    out = [
        "# Symptom routing",
        "",
        "GENERATED by `scripts/distill_investigations.py` from",
        "`docs/investigations/*.md`. Do not edit by hand.",
        "",
        "Search for the error text or symbol after locating its current code path.",
        "Read the matching experiment and later corrections, expanding as needed.",
        "Titles, snippets and status are historical locators, not current diagnosis.",
        "",
        f"{len(entries)} investigations.",
        "",
    ]
    for entry in sorted(entries, key=lambda e: e["title"].lower()):
        status = entry["status"]
        head = f"- [{entry['title']}](../investigations/{entry['file']})"
        if status:
            head += f" — **{status[:160]}**"
        out.append(head)
        if entry["keywords"]:
            out.append("  - " + " · ".join("`" + k + "`" for k in entry["keywords"]))
    out.append("")
    return "\n".join(out) + "\n"


def _write(path: str, text: str, check: bool) -> bool:
    text = "\n".join(line.rstrip() for line in text.splitlines()).rstrip() + "\n"
    if check:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read() == text
        except OSError:
            return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the generated pages are stale",
    )
    args = parser.parse_args(argv)

    denied, confirmed, routing = _collect()
    pages = (
        (DENIED_PAGE, _render_denied(denied)),
        (CONFIRMED_PAGE, _render_confirmed(confirmed)),
        (ROUTING_PAGE, _render_routing(routing)),
    )
    stale = []
    for path, text in pages:
        if not _write(path, text, args.check):
            stale.append(os.path.relpath(path, ROOT))
    if args.check and stale:
        sys.stderr.write(
            "stale knowledge pages (run scripts/distill_investigations.py): "
            + ", ".join(stale)
            + "\n"
        )
        return 1
    if not args.check:
        print(
            f"denied verdicts: {len(denied)}  confirmations: {len(confirmed)}"
            f"  investigations: {len(routing)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
