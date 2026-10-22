"""Generic extension ABI planning for pcc-native package builds."""
from __future__ import annotations

import argparse
import json

from pcc.frontends.c.capi_surface import extension_abi_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package ext-abi")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--provider", default="extension")
    parser.add_argument("--expected-abi", type=int, default=None)
    parser.add_argument("--actual-abi", type=int, default=None)
    parser.add_argument("--abi", dest="abi_mode", default="pcc-native")
    parser.add_argument("--include-dir", default=None)
    parser.add_argument("--require-capsule", action="store_true")
    parser.add_argument("--require-buffer", action="store_true")
    parser.add_argument("--require-memoryview", action="store_true")
    parser.add_argument("--require-numpy-capi", action="store_true")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    plan = extension_abi_plan(
        ns.symbol,
        provider=ns.provider,
        expected_abi=ns.expected_abi,
        actual_abi=ns.actual_abi,
        abi_mode=ns.abi_mode,
        include_dir=ns.include_dir,
        require_capsule=ns.require_capsule,
        require_buffer=ns.require_buffer,
        require_memoryview=ns.require_memoryview,
        require_numpy_capi=ns.require_numpy_capi,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
