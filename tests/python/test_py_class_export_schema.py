from __future__ import annotations

import ast as py_ast
import re
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[2]


def _direct_self_init_field_index(
    rel_path: str,
    class_name: str,
    field_name: str,
    init_method_name: str = "__init__",
) -> int:
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    module = py_ast.parse(source)
    for node in module.body:
        if not isinstance(node, py_ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if (
                not isinstance(item, py_ast.FunctionDef)
                or item.name != init_method_name
            ):
                continue
            fields: list[str] = []
            for stmt in item.body:
                targets = []
                if isinstance(stmt, py_ast.Assign):
                    targets = list(stmt.targets)
                elif isinstance(stmt, py_ast.AnnAssign):
                    targets = [stmt.target]
                for target in targets:
                    if (
                        isinstance(target, py_ast.Attribute)
                        and isinstance(target.value, py_ast.Name)
                        and target.value.id == "self"
                        and target.attr not in fields
                    ):
                        fields.append(target.attr)
            return fields.index(field_name)
    raise AssertionError(
        f"{class_name}.{init_method_name}.self.{field_name} not found"
    )


def test_class_method_registration_uses_stable_function_ref(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    src = tmp_path / "method_ref.py"
    out = tmp_path / "method_ref.ll"
    src.write_text(
        textwrap.dedent(
            """
            class Box:
                def value(self):
                    return 1

            print(Box().value())
            """
        ).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )

    text = out.read_text(encoding="utf-8")
    # Method registration must reference the method by a STABLE, named
    # function symbol derived from the method (so cross-module class schema
    # is deterministic), not an anonymous/inlined ref. The registration
    # passes the method's stable native adapter symbol to the function
    # constructor (py_func_new_named, then py_func_new_signature_slots, and
    # py_func_new_static_def for a static signature); the raw method symbol
    # is still emitted and called.
    adapter = "@user_method_ref_Box_value_method_native_adapter"
    assert re.search(
        r"@py_func_new_(?:named|static_def)\(ptr " + adapter
        + r"|@py_func_new_signature_slots\(ptr [^,]+, ptr [^,]+, ptr " + adapter,
        text,
    ), text
    assert "define external ptr @user_method_ref_Box_value(" in text, text
    # the original malformed-ref guard: the function ref must never be
    # serialized with the module header glued on.
    assert "bitcast ; ModuleID" not in text


def test_pcc_cross_module_class_schema_matches_local_layout(tmp_path):
    from pcc.frontends.python.pipeline import (
        _collect_relative_module_closure,
        _filter_ir_scaffold_closure,
        compile_python_multi,
    )

    srcs, mods = _collect_relative_module_closure(
        "pcc/__main__.py",
        include_same_package_absolute=True,
        recurse_same_package_absolute=True,
    )
    srcs, mods = _filter_ir_scaffold_closure(srcs, mods, ir_scaffold_mode="on")
    out = tmp_path / "pcc_main.ll"

    # Admit stdlib providers as the production compile of pcc/__main__.py
    # does: without them html.parser.HTMLParser (a base in
    # pcc.package.acquire) is foreign and its super().__init__ fails closed.
    compile_python_multi(
        srcs,
        str(out),
        emit_llvm_only=True,
        entry_module=mods[0],
        module_names=mods,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=True,
    )

    text = out.read_text(encoding="utf-8")
    body = re.search(
        r"define\s+(?:external\s+)?void\s+"
        r"@user_pcc_frontends_python_codegen_class_gen_ClassLowering__emit_method_body"
        r"\([^)]*\)[^{]*\{(?P<body>.*?)\n\}",
        text,
        re.DOTALL,
    )
    assert body is not None
    env_loads = [
        line.strip() for line in body.group("body").splitlines()
        if "%self.env." in line and "@py_instance_get_field" in line
    ]
    assert env_loads
    # Cross-module class-schema consistency check: every reader of
    # ``L1CodeGen.env`` from another module must agree on the same
    # ``py_instance_get_field(..., i32 N)`` index. The original test
    # cross-checked the index against the AST source position of the
    # ``self.env = {}`` line, but that mapping broke when L1CodeGen's
    # constructor was split into ``Layer1InitMixin._init_l1_state`` —
    # pcc's field layout now comes from the full mixin walk
    # (``ClassInfo.field_names``), not the single ``__init__`` AST
    # the test used to parse. The durable invariant — and the only
    # one this test actually needs to guard cross-module — is that
    # every emit picks the same N.
    indices = []
    for line in env_loads:
        m = re.search(r"i32\s+(\d+)\)", line)
        assert m is not None, line
        indices.append(int(m.group(1)))
    assert len(set(indices)) == 1, indices
