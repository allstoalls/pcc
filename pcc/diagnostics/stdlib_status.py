"""Native-stdlib support matrix.

The roadmap requires each Python feature/module to be classified as native
static, native dynamic, compatibility bridge, or unsupported.  Keeping this as
code makes status drift testable instead of leaving it as prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

MODE_NATIVE_STATIC = "native_static"
MODE_NATIVE_DYNAMIC = "native_dynamic"
MODE_COMPAT_BRIDGE = "compat_bridge"
MODE_UNSUPPORTED = "unsupported"

VALID_MODES = {
    MODE_NATIVE_STATIC,
    MODE_NATIVE_DYNAMIC,
    MODE_COMPAT_BRIDGE,
    MODE_UNSUPPORTED,
}


@dataclass(frozen=True)
class StdlibModuleStatus:
    module: str
    mode: str
    summary: str
    tests: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "mode": self.mode,
            "summary": self.summary,
            "tests": list(self.tests),
            "notes": list(self.notes),
        }


_STATUSES: Dict[str, StdlibModuleStatus] = {
    "sys": StdlibModuleStatus(
        "sys",
        MODE_NATIVE_DYNAMIC,
        "native argv/stdout/stderr/platform subset",
        ("tests/test_native_sys_platform.py",),
    ),
    "os": StdlibModuleStatus(
        "os",
        MODE_NATIVE_DYNAMIC,
        "native getenv/path/listdir/access subset",
        ("tests/test_native_os_misc.py",),
    ),
    "pathlib": StdlibModuleStatus(
        "pathlib",
        MODE_NATIVE_DYNAMIC,
        "pcc-native Path subset",
        ("tests/test_recursive_stdlib_import_codegen.py",),
    ),
    "re": StdlibModuleStatus(
        "re",
        MODE_NATIVE_DYNAMIC,
        "runtime-backed match subset",
        ("tests/test_python_cpython_alignment.py",),
    ),
    "gc": StdlibModuleStatus(
        "gc",
        MODE_NATIVE_DYNAMIC,
        "pcc_gc_* backed controls and telemetry",
        ("tests/test_gc_api.py",),
    ),
    "weakref": StdlibModuleStatus(
        "weakref",
        MODE_NATIVE_DYNAMIC,
        "weakref.ref subset",
        ("tests/test_gc_g3_weakref.py",),
    ),
    "threading": StdlibModuleStatus(
        "threading",
        MODE_NATIVE_DYNAMIC,
        "native Thread/Lock/Event/Condition/Semaphore subset",
        ("tests/test_threading_module_native.py",),
        ("threading.local still tracked separately",),
    ),
    "dataclasses": StdlibModuleStatus(
        "dataclasses",
        MODE_NATIVE_DYNAMIC,
        "compile-time expansion plus runtime helpers",
        ("tests/test_dataclasses_full.py",),
    ),
    "decimal": StdlibModuleStatus(
        "decimal",
        MODE_NATIVE_DYNAMIC,
        "native Decimal type identity; value construction is unsupported",
        ("tests/python/test_native_decimal_import_no_libpython.py",),
        ("raises NotImplementedError instead of approximating decimal arithmetic",),
    ),
    "typing": StdlibModuleStatus(
        "typing", MODE_NATIVE_STATIC, "mostly compile-time annotations", ()
    ),
    "json": StdlibModuleStatus(
        "json",
        MODE_COMPAT_BRIDGE,
        "minimal pure-Python port, not full CPython parity",
        (),
    ),
    "multiprocessing": StdlibModuleStatus(
        "multiprocessing",
        MODE_UNSUPPORTED,
        "placeholder; process pool not implemented",
        (),
    ),
}


def get_status(module: str) -> StdlibModuleStatus:
    try:
        return _STATUSES[module]
    except KeyError:
        return StdlibModuleStatus(
            module, MODE_COMPAT_BRIDGE, "not in pcc native matrix yet"
        )


def iter_statuses() -> tuple[StdlibModuleStatus, ...]:
    return tuple(_STATUSES[name] for name in sorted(_STATUSES))


def validate_statuses() -> None:
    for status in _STATUSES.values():
        if status.mode not in VALID_MODES:
            raise AssertionError(
                f"invalid stdlib mode for {status.module}: {status.mode}"
            )
        if not status.summary:
            raise AssertionError(f"missing stdlib summary for {status.module}")
