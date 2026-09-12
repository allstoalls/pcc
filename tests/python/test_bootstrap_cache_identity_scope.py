"""The frontend IR cache namespace must not be the whole source tree.

Per-module cache keys already bind the module's own sources, its transitive
imports, the compiler binary's own sha256, the target, and the options. The
identity on top of that is a *namespace*, and hashing all 999 bootstrap-relevant
files into it meant an edit anywhere -- a GUI file, a runtime C source, a
documentation-only tool -- invalidated every cached frontend IR module.

`pcc/backend/self_backend_cache_identity.py` already solved this for the object
cache and says so in its docstring: "Hashing the whole compiler source tree here
would invalidate every object after an unrelated frontend, package, runtime, or
documentation change." These tests hold the frontend identity to the same rule.
"""

from __future__ import annotations

import pcc.bootstrap_cache_identity as identity


def _identity_with(tmp_path, rel_writes):
    root = tmp_path
    (root / "pcc").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "bootstrap.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    for rel, text in rel_writes.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return identity.bootstrap_source_sha256(root)


def test_frontend_sources_change_the_identity(tmp_path):
    base = {
        "pcc/py_frontend/type_infer.py": "x = 1\n",
        "pcc/gui/window.py": "gui = 1\n",
    }
    before = _identity_with(tmp_path, base)
    after = _identity_with(
        tmp_path, {**base, "pcc/py_frontend/type_infer.py": "x = 2\n"}
    )
    assert before != after, "a frontend edit must change the namespace"


def test_unrelated_subtrees_do_not_change_the_identity(tmp_path):
    """A GUI or runtime-C edit cannot change frontend IR, so it must not
    invalidate every cached frontend module."""
    base = {
        "pcc/py_frontend/type_infer.py": "x = 1\n",
        "pcc/gui/window.py": "gui = 1\n",
        "pcc/py_runtime/src/py_obj.c": "int a;\n",
    }
    before = _identity_with(tmp_path, base)
    for rel, changed in (
        ("pcc/gui/window.py", "gui = 2\n"),
        ("pcc/py_runtime/src/py_obj.c", "int a; int b;\n"),
    ):
        after = _identity_with(tmp_path, {**base, rel: changed})
        assert before == after, f"{rel} must not invalidate frontend IR cache"


def test_macho_object_and_link_surface_does_not_change_the_identity(tmp_path):
    """The assembler, object writer and linker consume IR; they cannot change
    the IR a module compiles to.

    `macho_linker_source_identity` already binds exactly these files for the
    link action -- the key that does depend on them -- so counting them here
    too only forced a cold frontend after every object-writer edit.  Measured
    cost of one such edit before this rule: 170 runtime objects rebuilt plus
    every cached frontend IR module, about 9.5 minutes.
    """
    base = {
        "pcc/py_frontend/type_infer.py": "x = 1\n",
        "pcc/backend/native_object.py": "native = 1\n",
        "pcc/backend/macho_exec.py": "link = 1\n",
        "pcc/backend/arm64_asm_driver.py": "asm = 1\n",
    }
    before = _identity_with(tmp_path, base)
    for rel, changed in (
        ("pcc/backend/native_object.py", "native = 2\n"),
        ("pcc/backend/macho_exec.py", "link = 2\n"),
        ("pcc/backend/arm64_asm_driver.py", "asm = 2\n"),
    ):
        after = _identity_with(tmp_path, {**base, rel: changed})
        assert before == after, f"{rel} must not invalidate frontend IR cache"


def test_self_backend_emitter_sources_still_change_the_identity(tmp_path):
    """The IR->asm emitters are not excluded: `pcc/py_frontend/codegen`
    imports from them, so they can reach IR shape."""
    base = {
        "pcc/py_frontend/type_infer.py": "x = 1\n",
        "pcc/backend/self_backend_aarch64_darwin.py": "emit = 1\n",
    }
    before = _identity_with(tmp_path, base)
    after = _identity_with(
        tmp_path,
        {**base, "pcc/backend/self_backend_aarch64_darwin.py": "emit = 2\n"},
    )
    assert before != after


def test_excluded_files_are_exactly_the_macho_link_surface():
    """One list, not two: a file added to the link identity must stop
    invalidating the frontend identity at the same moment."""
    from pcc.backend.self_backend_cache_identity import MACHO_LINK_SOURCE_PATHS

    assert identity._FRONTEND_IRRELEVANT_FILES == frozenset(
        MACHO_LINK_SOURCE_PATHS
    )


def test_c_toolchain_does_not_change_the_identity(tmp_path):
    """`pcc/codegen` (C code generator) and `pcc/evaluater` (C driver) are not
    imported by `pcc/py_frontend`, so they cannot change the IR a *Python*
    module compiles to.

    Editing the C driver used to invalidate every cached frontend IR module
    and force a fully cold stage; measured cost of one such edit was about
    9.5 minutes of runtime-module recompilation.
    """
    base = {
        "pcc/py_frontend/type_infer.py": "x = 1\n",
        "pcc/codegen/c_codegen.py": "cgen = 1\n",
        "pcc/evaluater/c_evaluator.py": "drive = 1\n",
    }
    before = _identity_with(tmp_path, base)
    for rel, changed in (
        ("pcc/codegen/c_codegen.py", "cgen = 2\n"),
        ("pcc/evaluater/c_evaluator.py", "drive = 2\n"),
    ):
        after = _identity_with(tmp_path, {**base, rel: changed})
        assert before == after, f"{rel} must not invalidate frontend IR cache"


def test_python_parser_still_changes_the_identity(tmp_path):
    """`pcc/parse` holds the *Python* front door (`py_lift`, `py_parse`), not
    only the C parser, so it must stay bound."""
    base = {
        "pcc/py_frontend/type_infer.py": "x = 1\n",
        "pcc/parse/py_lift.py": "lift = 1\n",
    }
    before = _identity_with(tmp_path, base)
    after = _identity_with(
        tmp_path, {**base, "pcc/parse/py_lift.py": "lift = 2\n"}
    )
    assert before != after
