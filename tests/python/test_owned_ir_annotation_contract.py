"""Compiler annotations name the actual owned IR API, not removed LLVM classes."""
import ast
import inspect
from pathlib import Path

from pcc.ir.compat import ir
from pcc.frontends.python.codegen.core_helpers import CoreHelperMixin


ROOT = Path(__file__).resolve().parents[2]


def test_entry_alloca_producer_declares_actual_owned_value():
    annotations = inspect.get_annotations(CoreHelperMixin._alloca_in_entry, eval_str=True)
    assert annotations["return"] is ir.Value
    assert inspect.get_annotations(ir.IRBuilder.alloca, eval_str=True)["return"] is ir.Value


def test_owned_ir_qualified_annotations_resolve_to_provider():
    missing = []
    checked = 0
    for path in sorted((ROOT / "pcc").rglob("*.py")):
        tree = ast.parse(path.read_text())
        aliases = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in ("pcc.ir.compat", "pcc.ir"):
                aliases.update(item.asname or item.name for item in node.names if item.name == "ir")
            elif isinstance(node, ast.Import):
                aliases.update(item.asname for item in node.names
                               if item.name in ("pcc.ir.ir", "pcc.ir.compat.ir") and item.asname)
        if not aliases:
            continue
        for node in ast.walk(tree):
            annotations = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations = [node.returns]
                annotations += [arg.annotation for arg in
                                node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
                annotations += [arg.annotation for arg in (node.args.vararg, node.args.kwarg) if arg]
            elif isinstance(node, ast.AnnAssign):
                annotations = [node.annotation]
            for annotation in annotations:
                if annotation is None:
                    continue
                if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                    annotation = ast.parse(annotation.value, mode="eval")
                for attr in ast.walk(annotation):
                    if (isinstance(attr, ast.Attribute) and isinstance(attr.value, ast.Name)
                            and attr.value.id in aliases):
                        checked += 1
                        if not hasattr(ir, attr.attr):
                            missing.append((str(path.relative_to(ROOT)), node.lineno, attr.attr))
    assert checked > 1000
    assert not missing, missing
