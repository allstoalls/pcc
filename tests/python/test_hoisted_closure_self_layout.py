"""A closure hoisted out of a mixin method reads ``self`` with the host layout.

L1CodeGen's mixin methods are typed with the composed host class, so
``self.env`` lowers to the host's field slot.  A nested closure that captured
``self`` was hoisted with a dyn ``self`` parameter; ``self.attr`` then fell
back to the lexical class, the mixin, and read the mixin's own inferred
layout.  In pcc1, ``_lambda_free_vars``'s ``collect`` read field 4 instead of
``env`` (field 15), missed every local that shares a name with a first-class
module function, and a lambda capturing such a local called that function.
The hoisted capture now keeps the enclosing method's ``self`` type.
"""

import re
from pathlib import Path

from tests.python.test_hoist_free_names_native_shape import (
    _REPO_ROOT,
    _load_probe_module,
)


_MODULE = "pcc.frontends.python.codegen.lambda_helpers_lowering"


def test_hoisted_closure_reads_self_fields_with_the_host_layout(tmp_path):
    from pcc.frontends.python.pipeline import (
        compile_contextual_per_module_fallback_counts,
    )

    probe = _load_probe_module()
    srcs, mods = probe._tightened_closure(str(_REPO_ROOT / "pcc" / "__main__.py"))
    ir_dir = tmp_path / "ir"
    ir_dir.mkdir()
    counts = compile_contextual_per_module_fallback_counts(
        srcs,
        mods,
        {_MODULE},
        ir_scaffold_mode="on",
        strict_no_libpython=True,
        emit_ir_dir=str(ir_dir),
    )
    assert counts == {_MODULE: 0}
    ir = Path(ir_dir / "pcc_frontends_python_codegen_lambda_helpers_lowering.ll").read_text(
        encoding="utf-8"
    )
    fields = {}
    current = ""
    for line in ir.splitlines():
        if line.startswith("define "):
            current = re.search(r"@([^\s(]+)\(", line).group(1)
        match = re.search(
            r"%self\.env\.[0-9.]+ = call ptr \(ptr, i32\) "
            r"@py_instance_get_field\(ptr %[^,]+, i32 (\d+)\)",
            line,
        )
        if match:
            fields.setdefault(current, set()).add(int(match.group(1)))
    closures = [name for name in fields if name.endswith("___nested_collect")]
    assert closures, sorted(fields)
    methods = [name for name in fields if "LambdaHelperLoweringMixin_" in name]
    assert methods, sorted(fields)
    indexes = set()
    for name in fields:
        indexes |= fields[name]
    assert len(indexes) == 1, fields
