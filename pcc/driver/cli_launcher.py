"""Installed ``pcc`` launcher.

The console script and ``python -m pcc`` share the same dispatcher. CPython
executes this entry; pcc1 executes the native build of the same command path.
"""
from __future__ import annotations

import sys


# Flags the native compiler has no implementation for: a command using one
# stays on the host even when the test suite routes ``pcc`` to pcc1.
_HOST_ONLY_VALUES = {
    "--python-libpython": ("on", "auto"),
}


def _host_only_reason(argv) -> str:
    index = 0
    while index < len(argv):
        arg = argv[index]
        for flag, values in _HOST_ONLY_VALUES.items():
            value = None
            if arg == flag and index + 1 < len(argv):
                value = argv[index + 1]
            elif arg.startswith(flag + "="):
                value = arg[len(flag) + 1:]
            if value in values:
                return flag + "=" + value
        index += 1
    return ""


def _route_to_test_compiler(argv) -> None:
    """Exec ``PCC_TEST_COMPILER`` (the test suite's native-compiler switch).

    See tests/pcc1_route.py: the environment matches its native compiles,
    and a command the native compiler cannot run is logged and stays here.
    """
    import os

    target = os.environ.get("PCC_TEST_COMPILER", "")
    if not target or os.environ.get("PCC_TEST_COMPILER_ROUTED"):
        return
    reason = _host_only_reason(argv)
    if reason:
        log_path = os.environ.get("PCC_TEST_COMPILER_LOG", "")
        if log_path:
            import json

            record = {
                "kind": "pcc",
                "detail": reason,
                "test": os.environ.get("PYTEST_CURRENT_TEST", ""),
            }
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        return
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        PCC_HOST_PYTHON="/usr/bin/false",
        PCC_RUNTIME_CC="/usr/bin/false",
        PCC_NO_AUTO_PCC1="1",
        PCC_TEST_COMPILER_ROUTED="1",
    )
    os.execve(target, [target] + list(argv), env)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    _route_to_test_compiler(argv)

    # Deferred on purpose: importing the 11.7k-line dispatcher at module scope
    # is host startup cost for anything that only imports this launcher, and
    # ``pcc/__main__.py`` anchors the stage1 closure on the dispatcher itself.
    from pcc.driver.cli_bootstrap import bootstrap_cli_main

    return bootstrap_cli_main(list(argv))


if __name__ == "__main__":
    raise SystemExit(main())
