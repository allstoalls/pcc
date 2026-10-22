"""Expose the owned target-runtime inventory to the explicit Make builder."""

from __future__ import annotations

import argparse
import sys

from pcc.backend.self_backend_targets import resolve_self_backend_target
from pcc.frontends.python.owned_runtime_build import runtime_modules
from pcc.frontends.python.pipeline_targets import host_target_triple


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--target")
    # Every runtime is threaded; "--threads 1" stays accepted.
    parser.add_argument("--threads", choices=("1",), default="1")
    parser.add_argument("--field", choices=("target", "modules"), default="modules")
    args = parser.parse_args(argv)
    try:
        target = args.target if args.target is not None else host_target_triple()
        resolve_self_backend_target(target)
        if args.field == "target":
            print(target)
        else:
            modules = runtime_modules(args.runtime_root, target, True)
            if not modules:
                raise ValueError("runtime module inventory is empty")
            print(" ".join(modules))
    except (ValueError, OSError, RuntimeError) as exc:
        print("runtime inventory: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
