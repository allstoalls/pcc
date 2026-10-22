#!/usr/bin/env python3
"""Sample a running pcc binary and attribute self time to its own functions.

Why this exists
---------------
pcc1 carries ~14k text symbols, so function-level attribution needs no DWARF.
What repeatedly produced *confidently wrong* profiles was resolving addresses
against the wrong symbol table.  Two ways that happened here:

* an `nm` dump from a different build of the same day -- two such tables shared
  only their first 3 entries, and the profile claimed `fseek` was 10% of a
  workload that never seeks;
* sampling a `gtimeout`/`sh` wrapper instead of the compiler, then resolving its
  frames against pcc1 -- `__sigsuspend` came out as `_pcc_platform_waitpid` at
  53%, and there was no signal that anything was wrong.

So this script refuses to guess.  It reads the sampled process's own executable,
checks it against the image `sample` actually reports, derives the slide from
that image's load address rather than assuming one, and counts only frames
belonging to that image.  ``--binary`` is a *check*, not an override: a mismatch
is an error, because a mismatch is exactly the bug.

Usage
-----
    scripts/pcc_profile.py <pid> [seconds] [--binary PATH] [--top N]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_FRAME = re.compile(
    r"^(?P<lead>[\s+!:|]*)(?P<count>\d+) (?P<what>.*?)\s+\(in (?P<image>[^)]+)\)"
    r"(?:.*?load address 0x(?P<load>[0-9a-f]+) \+ 0x(?P<off>[0-9a-f]+))?"
)
_IMAGE_ROW = re.compile(r"^\s*0x([0-9a-f]+)\s+-\s+0x[0-9a-f]+\s+\+?(\S+)")


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


def call_graph_section(text: str) -> str:
    """Just the call tree.

    `sample` also prints a FLAT top-of-stack summary whose rows are indented
    about four columns. Folding those together with the tree invents shallow
    call paths -- the heaviest "stack" in one run came out as
    ``Thread;start;pcc_gc_managed_pointer_find_slot``, which cannot happen --
    and it double-counts every sample, because the summary re-reports what the
    tree already contains.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("Call graph:"):
            start = index + 1
            break
    if start is None:
        return text
    end = len(lines)
    for index in range(start, len(lines)):
        stripped = lines[index]
        if stripped and not stripped[0].isspace() and stripped.rstrip().endswith(":"):
            end = index
            break
    return "\n".join(lines[start:end])


def _busiest_leaf(pid: int) -> int:
    """Pick the descendant actually computing.

    A `gtimeout`/`sh` wrapper or a coordinator that only `waitpid`s reports high
    average CPU while doing no work, and its frames resolve to nonsense against
    the compiler's symbol table.  Prefer leaves, and rank by CPU.
    """
    children: dict[int, list[int]] = collections.defaultdict(list)
    cpu: dict[int, float] = {}
    comm: dict[int, str] = {}
    for line in _run(["ps", "-Ao", "pid=,ppid=,pcpu=,comm="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            child, parent, share = int(parts[0]), int(parts[1]), float(parts[2])
        except ValueError:
            continue
        children[parent].append(child)
        cpu[child] = share
        comm[child] = parts[3] if len(parts) > 3 else ""
    tree: list[int] = []
    stack = [pid]
    while stack:
        current = stack.pop()
        tree.append(current)
        stack.extend(children.get(current, ()))
    leaves = [node for node in tree if not children.get(node)]
    # A wrapper's tree also contains short-lived helpers (`sh`, `as`, `ar`)
    # that can win on CPU and then exit before the sample starts.  Prefer a
    # descendant running the same program as some member of the tree with the
    # highest CPU, and require it to still be alive.
    def alive(candidate: int) -> bool:
        return bool(_run(["ps", "-o", "pid=", "-p", str(candidate)]).strip())

    for pool in (leaves, tree):
        for candidate in sorted(pool, key=lambda item: -cpu.get(item, 0.0)):
            if cpu.get(candidate, 0.0) <= 0.0:
                continue
            if not alive(candidate):
                continue
            if candidate != pid:
                print(f"pid {pid} is a wrapper or coordinator; sampling "
                      f"{candidate} [{comm.get(candidate, '?')}] "
                      f"({cpu.get(candidate, 0.0):.0f}% cpu)")
            return candidate
    return pid


def _executable(pid: int) -> Path:
    name = _run(["ps", "-o", "comm=", "-p", str(pid)]).strip()
    if not name:
        raise SystemExit(f"pid {pid} is not running")
    return Path(name)


def _text_vmaddr(binary: Path) -> int:
    """__TEXT vmaddr, so the slide is derived rather than assumed."""
    lines = _run(["otool", "-l", str(binary)]).splitlines()
    for index, line in enumerate(lines):
        if "segname __TEXT" in line:
            for follow in lines[index : index + 6]:
                if "vmaddr" in follow:
                    return int(follow.split()[-1], 16)
    raise SystemExit(f"cannot read __TEXT vmaddr from {binary}")


_NM_SECTION_SYMBOL = re.compile(r"^([0-9a-fA-F]+) \(__TEXT,(__[A-Za-z0-9_]+)\) .* (\S+)$")


def _symbols(binary: Path) -> list[tuple[int, str]]:
    # pcc's owned linker splits code into __text, __text1, __text2, ...;
    # nm types symbols outside __TEXT,__text as "s", so read each symbol's
    # section rather than filtering on t/T.
    out = []
    for line in _run(["nm", "-n", "-m", str(binary)]).splitlines():
        match = _NM_SECTION_SYMBOL.match(line)
        if match is None:
            continue
        section = match.group(2)
        if section.startswith("__text") or section in ("__stubs", "__veneers"):
            out.append((int(match.group(1), 16), match.group(3)))
    out.sort()
    return out


def _resolve(symbols: list[tuple[int, str]], static: int) -> tuple[str, int]:
    index = bisect.bisect_right(symbols, (static, "\xff")) - 1
    if 0 <= index < len(symbols):
        return symbols[index][1], static - symbols[index][0]
    return f"<0x{static:x}>", 0


def _self_time(text: str, image_name: str) -> tuple[collections.Counter, int, int]:
    """Self time per absolute address, counting only frames in `image_name`.

    `sample` counts are tree-cumulative; reading them as self time makes every
    caller look like a hot leaf.  Subtract the direct children, which are the
    following rows at the first deeper indent level.
    """
    rows: list[tuple[int, int, int | None] | None] = []
    for line in call_graph_section(text).splitlines():
        match = _FRAME.match(line)
        if not match:
            rows.append(None)
            continue
        addr = None
        if match.group("load") and match.group("image") == image_name:
            addr = int(match.group("load"), 16) + int(match.group("off"), 16)
        rows.append((len(match.group("lead")), int(match.group("count")), addr))

    self_time: collections.Counter = collections.Counter()
    total = 0
    foreign = 0
    for index, row in enumerate(rows):
        if row is None:
            continue
        depth, count, addr = row
        total = max(total, count)
        child_depth = None
        children = 0
        for follow in rows[index + 1 :]:
            if follow is None:
                continue
            if follow[0] <= depth:
                break
            if child_depth is None:
                child_depth = follow[0]
            if follow[0] == child_depth:
                children += follow[1]
        own = max(0, count - children)
        if addr is None:
            foreign += own
        else:
            self_time[addr] += own
    return self_time, total, foreign


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("seconds", nargs="?", type=int, default=10)
    parser.add_argument("--binary", default=None,
                        help="expected executable; a mismatch is an error")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    pid = _busiest_leaf(args.pid)
    binary = _executable(pid)
    if not binary.is_absolute():
        binary = (Path.cwd() / binary).resolve()
    if args.binary:
        expected = Path(args.binary).resolve()
        if expected != binary:
            raise SystemExit(
                f"refusing to resolve symbols from a different binary:\n"
                f"  sampling  {binary}\n  --binary  {expected}\n"
                f"This mismatch is the bug this tool exists to prevent."
            )
    if not binary.is_file():
        raise SystemExit(f"executable not found: {binary}")

    symbols = _symbols(binary)
    if not symbols:
        raise SystemExit(f"{binary} has no text symbols; attribution is meaningless")
    stamp = _run(["stat", "-f", "%Sm", "-t", "%Y-%m-%d %H:%M:%S", str(binary)]).strip()
    print(f"binary  {binary}")
    print(f"built   {stamp}   text symbols {len(symbols)}")
    print(f"sampling pid {pid} for {args.seconds}s", flush=True)

    with tempfile.NamedTemporaryFile(suffix=".sample", delete=False) as handle:
        path = handle.name
    try:
        subprocess.run(["sample", str(pid), str(args.seconds), "-f", path],
                       capture_output=True, check=False)
        text = Path(path).read_text(errors="replace")
    finally:
        os.unlink(path)

    image = binary.name
    load = None
    for line in text.splitlines():
        row = _IMAGE_ROW.match(line)
        if row and row.group(2) == image:
            load = int(row.group(1), 16)
            break
    if load is None:
        raise SystemExit(
            f"`sample` reported no image named {image!r}; it sampled a different "
            f"program, so resolving its frames here would be fiction."
        )
    slide = load - _text_vmaddr(binary)

    footprint = re.search(r"Physical footprint:\s+(\S+)", text)
    if footprint:
        print(f"footprint {footprint.group(1)}")

    self_time, total, foreign = _self_time(text, image)
    if not total:
        raise SystemExit(f"no samples collected from pid {pid}")
    print(f"\nself time, {total} samples"
          + (f" ({100.0 * foreign / total:.1f}% outside {image})" if foreign else ""))
    print()
    merged: collections.Counter = collections.Counter()
    for addr, count in self_time.items():
        name, _ = _resolve(symbols, addr - slide)
        merged[name] += count
    for name, count in merged.most_common(args.top):
        if count > 0:
            print(f"{count:>6}  {100.0 * count / total:5.1f}%  {name[:78]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
