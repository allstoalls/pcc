"""Native target-pass replay: INPUT_ASM EDGES OUTPUT_PREFIX."""

import sys

from pcc.backend.self_backend_aarch64_darwin import (
    _thread_trampoline_branches,
    _fold_cond_branch_to_fallthrough,
    _drop_fallthrough_uncond_branches,
)


def save(prefix: str, name: str, lines: list[str]) -> None:
    with open(prefix + name + ".s", "w") as stream:
        stream.write("\n".join(lines) + "\n")


def main() -> None:
    if len(sys.argv) != 4:
        raise ValueError("expected INPUT_ASM EDGES OUTPUT_PREFIX")
    with open(sys.argv[1]) as stream:
        lines = stream.read().splitlines()
    with open(sys.argv[2]) as stream:
        edge_fields = stream.read().splitlines()
    edges: list[tuple[str, str, str]] = []
    for index in range(0, len(edge_fields), 3):
        edges.append((edge_fields[index], edge_fields[index + 1], edge_fields[index + 2]))
    prefix = sys.argv[3]
    lines = _thread_trampoline_branches(lines)
    save(prefix, "thread", lines)
    prior = lines
    lines = _fold_cond_branch_to_fallthrough(lines, edges)
    save(prefix, "fold", lines)
    save(prefix, "thread-after-fold", prior)
    prior = lines
    lines = _drop_fallthrough_uncond_branches(lines)
    save(prefix, "drop", lines)
    save(prefix, "fold-after-drop", prior)


if __name__ == "__main__":
    main()
