"""Freestanding modules may name an integer constant without paying for it.

A freestanding module has no module init, so it cannot hold a runtime global:
``define_global_i64`` builds a *mutable* global whose reads are memory loads,
and the allocator's hottest predicate cannot afford one. Module-scope
``NAME = <integer literal>`` is therefore folded into its uses before type
inference, and the property that makes that safe is exact rather than measured:
the renamed module must emit byte-identical IR.

The IR comparison shells out to the same ``pcc --python-library --emit-llvm``
command line the runtime Makefile uses. Calling ``compile_python`` in-process
takes a different emission path (host target triple, ordinary global linkage),
which would compare two things neither of which is the shipped runtime.

The comparison is named-source against literal-source, deliberately, rather
than against ``build_py/*.ll``. Those are untracked build artifacts, and every
addition to ``RUNTIME_SIGNATURES`` rewrites all of them by one ``declare``
line, so a test anchored there would drift or compare a file with itself.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap

from pcc.parse.py_lift import parse_and_lift


REPO_ROOT = Path(__file__).resolve().parents[2]

_NAMED_MODULE = """
    from pcc import i64
    from pcc.extern import c_abi_export
    from pcc.unsafe import load_i64, store_i64

    __pcc_freestanding__ = True

    GC_STATE_LIVE = 5783538902897647428
    GC_STATE_FREE = 5783538902897647427
    GRANULE_STATE_OFFSET = -48
    GRANULE_USABLE_SIZE_OFFSET = -16


    @c_abi_export("probe_granule_state")
    def probe_granule_state(ptr) -> i64:
        if load_i64(ptr, GRANULE_STATE_OFFSET) == GC_STATE_LIVE:
            return load_i64(ptr, GRANULE_USABLE_SIZE_OFFSET)
        store_i64(ptr, GRANULE_STATE_OFFSET, GC_STATE_FREE)
        return 0
"""

_SUBSTITUTIONS = (
    ("GC_STATE_LIVE", "5783538902897647428"),
    ("GC_STATE_FREE", "5783538902897647427"),
    ("GRANULE_STATE_OFFSET", "-48"),
    ("GRANULE_USABLE_SIZE_OFFSET", "-16"),
)


def _literal_spelling(named: str) -> str:
    """The same module with every constant typed out at its use sites."""
    out = named
    for name, raw in _SUBSTITUTIONS:
        out = out.replace("    " + name + " = " + raw + "\n", "")
        out = out.replace(name, raw)
    return out


def _emit_module_ir(source_path: Path, out_path: Path) -> str:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--python-library",
            "--emit-llvm=" + str(out_path),
            str(source_path),
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=600,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return out_path.read_text(encoding="utf-8")


def test_named_constants_emit_the_same_ir_as_literals(tmp_path):
    # The module name reaches the IR as its ModuleID, so both arms are
    # compiled under the same file name in separate directories.
    named_dir = tmp_path / "named"
    literal_dir = tmp_path / "literal"
    named_dir.mkdir()
    literal_dir.mkdir()
    (named_dir / "granule.py").write_text(
        textwrap.dedent(_NAMED_MODULE), encoding="utf-8"
    )
    (literal_dir / "granule.py").write_text(
        textwrap.dedent(_literal_spelling(_NAMED_MODULE)), encoding="utf-8"
    )
    named_ir = _emit_module_ir(named_dir / "granule.py", named_dir / "granule.ll")
    literal_ir = _emit_module_ir(
        literal_dir / "granule.py", literal_dir / "granule.ll"
    )
    assert named_ir == literal_ir


def test_module_scope_constant_is_folded_into_its_uses():
    module = parse_and_lift(textwrap.dedent(_NAMED_MODULE), "<named>", "granule")
    kinds = [type(stmt).__name__ for stmt in module.body]
    # The four declarations are consumed, leaving only the marker; nothing is
    # left at module scope for the freestanding rejection to trip over.
    assert kinds.count("Assign") == 1
    assert "FuncDef" in kinds
    body = repr(module.body[-1])
    assert "GC_STATE_LIVE" not in body
    assert "5783538902897647428" in body


def test_a_function_that_binds_the_name_keeps_its_own_binding():
    module = parse_and_lift(
        textwrap.dedent(
            """
            from pcc import i64
            from pcc.extern import c_abi_export

            __pcc_freestanding__ = True

            LIMIT = 7


            @c_abi_export("shadowed")
            def shadowed(n) -> i64:
                LIMIT = n
                return LIMIT


            @c_abi_export("unshadowed")
            def unshadowed() -> i64:
                return LIMIT
            """
        ),
        "<shadow>",
        "shadow",
    )
    shadowed, unshadowed = [
        stmt for stmt in module.body if type(stmt).__name__ == "FuncDef"
    ]
    assert type(shadowed.body[-1].value).__name__ == "Name"
    assert type(unshadowed.body[-1].value).__name__ == "IntLit"
    assert unshadowed.body[-1].value.value == 7


def test_non_freestanding_module_keeps_its_global():
    module = parse_and_lift(
        "LIMIT = 7\n\ndef f():\n    return LIMIT\n", "<plain>", "plain"
    )
    assert len(module.body) == 2
    assert type(module.body[-1].body[-1].value).__name__ == "Name"


def test_non_literal_module_scope_assignment_is_still_rejected(tmp_path):
    src = tmp_path / "computed.py"
    src.write_text(
        textwrap.dedent(
            """
            from pcc import i64
            from pcc.extern import c_abi_export

            __pcc_freestanding__ = True

            LIMIT = 3 + 4


            @c_abi_export("noop")
            def noop() -> i64:
                return 0
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--python-library",
            "--emit-llvm=" + str(tmp_path / "computed.ll"),
            str(src),
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=600,
        env=env,
    )
    assert build.returncode != 0
    assert "module-scope statements" in build.stdout + build.stderr
