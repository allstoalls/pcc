from __future__ import annotations

import subprocess


def test_set_bindings_infer_first_class_set_type() -> None:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import SetType
    from pcc.frontends.python.type_infer import infer_module

    module = infer_module(
        parse_and_lift(
            "literal = {1, 2}\nconstructed = set([3])\n",
            "set_type.py",
            "set_type",
        )
    )

    assert all(isinstance(stmt.value.ty, SetType) for stmt in module.body)


def test_set_annotations_preserve_mutability_and_element_shape() -> None:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import IntType, SetType, StrType, TupleType
    from pcc.frontends.python.type_infer import infer_module

    lifted = parse_and_lift(
        "mutable: set[int] = set([1])\n"
        "immutable: frozenset[tuple[int, str]] = frozenset({(1, 'x')})\n",
        "set_annotations.py",
        "set_annotations",
    )
    mutable, immutable = (stmt.annotation for stmt in lifted.body)

    assert isinstance(mutable, SetType)
    assert mutable.name == "set"
    assert isinstance(mutable.elem, IntType)
    assert isinstance(immutable, SetType)
    assert immutable.name == "frozenset"
    assert isinstance(immutable.elem, TupleType)
    assert isinstance(immutable.elem.elems[0], IntType)
    assert isinstance(immutable.elem.elems[1], StrType)

    inferred = infer_module(lifted)
    assert [stmt.targets[0].ty.name for stmt in inferred.body] == [
        "set",
        "frozenset",
    ]


def test_cross_module_set_export_keeps_native_operator_lowering(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    entry = tmp_path / "entry.py"
    provider = tmp_path / "provider.py"
    executable = tmp_path / "set_export"
    entry.write_text(
        "from pkg.provider import base\n"
        "print(sorted(base | {'right'}))\n",
        encoding="utf-8",
    )
    provider.write_text("base = {'left'}\n", encoding="utf-8")

    compile_python_multi(
        [str(entry), str(provider)],
        str(executable),
        module_names=["pkg.entry", "pkg.provider"],
        entry_module="pkg.entry",
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "['left', 'right']\n"


def test_generator_expression_iterates_first_class_set_type(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "set_generator.py"
    executable = tmp_path / "set_generator"
    source.write_text(
        "class Item:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "    def __hash__(self):\n"
        "        return self.value\n"
        "items = {Item(1), Item(2)}\n"
        "print(sum(item.value for item in items))\n",
        encoding="utf-8",
    )

    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "3\n"


def test_list_and_singleton_unpack_accept_first_class_set_type(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "set_consumers.py"
    executable = tmp_path / "set_consumers"
    source.write_text(
        "values = {3, 1, 2}\n"
        "print(sorted(list(values)))\n"
        "left = {'b'}\n"
        "right = {'a'}\n"
        "print(sorted(list(left | right)))\n"
        "only = {'value'}\n"
        "item, = only\n"
        "print(item)\n",
        encoding="utf-8",
    )

    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(executable)], text=True, capture_output=True, timeout=30
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "[1, 2, 3]\n['a', 'b']\nvalue\n"


def test_set_unpack_rejects_wrong_arity(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "set_unpack_arity.py"
    executable = tmp_path / "set_unpack_arity"
    source.write_text(
        "try:\n"
        "    first, second = {'only'}\n"
        "    print('accepted-short')\n"
        "except ValueError:\n"
        "    print('short')\n"
        "try:\n"
        "    first, = {'one', 'two'}\n"
        "    print('accepted-long')\n"
        "except ValueError:\n"
        "    print('long')\n",
        encoding="utf-8",
    )

    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(executable)], text=True, capture_output=True, timeout=30
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "short\nlong\n"


def test_dict_keys_view_union_with_set_is_set_operation(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "dict_keys_set_union.py"
    executable = tmp_path / "dict_keys_set_union"
    source.write_text(
        "alpha = 1\n"
        "def public_names():\n"
        "    names = globals().keys() | {'extra'}\n"
        "    return names\n"
        "result = public_names()\n"
        "print('alpha' in result)\n"
        "print('extra' in result)\n",
        encoding="utf-8",
    )

    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(executable)], text=True, capture_output=True, timeout=30
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nTrue\n"


def test_set_augassign_accepts_dynamic_set_and_preserves_identity(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "set_augassign_dynamic.py"
    executable = tmp_path / "set_augassign_dynamic"
    source.write_text(
        "def merge(values):\n"
        "    result = set()\n"
        "    alias = result\n"
        "    for value in values:\n"
        "        result |= value\n"
        "    print(result is alias)\n"
        "    return sorted(result)\n"
        "print(merge([{'a'}, {'b'}]))\n"
        "def reject(value):\n"
        "    result = set()\n"
        "    try:\n"
        "        result |= value\n"
        "    except TypeError:\n"
        "        return 'type-error'\n"
        "    return 'accepted'\n"
        "print(reject(1))\n",
        encoding="utf-8",
    )

    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(executable)], text=True, capture_output=True, timeout=30
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\n['a', 'b']\ntype-error\n"


def test_set_projection_has_no_syntax_side_table_workaround() -> None:
    from pathlib import Path

    source = (
        Path.cwd()
        / "pcc" / "frontends" / "python"
        / "type_infer.py"
    ).read_text(encoding="utf-8")

    assert "set_typed_names" not in source
    assert "_restore_degraded_set_operand" not in source
