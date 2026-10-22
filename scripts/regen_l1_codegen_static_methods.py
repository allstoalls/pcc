"""Regenerate ``pcc/frontends/python/codegen/_l1_codegen_static_methods.py``.

This is a HOST-PYTHON-ONLY tool that uses ``inspect.signature`` to extract
real call signatures from the live L1CodeGen class (with all mixins
assembled) and emits a pure-data Python module that the bootstrap-safe
``layer1_support.py`` can import.

Why this layer of indirection: ``layer1_support.py`` is in the no-libpython
bootstrap closure for ``pcc1``.  ``inspect.signature`` and friends aren't
natively lowered by the pcc-Python frontend, so any direct use of them in
layer1_support.py forces a libpython fallback and aborts the strict
``--python-libpython=off`` build.  Generating the static data offline keeps
the runtime module data-only and bootstrap-safe.

Run when ``host_contract.py::L1_CODEGEN_HOST_METHODS`` changes or the
mixin source code's signatures change:

    env -u LC_ALL uv run python scripts/regen_l1_codegen_static_methods.py

The output is committed; the runtime never invokes this script.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _format_param(
    p: inspect.Parameter,
    kw_only_emitted: list,
) -> list[tuple[str, str, bool]]:
    """Return compact ``(name, kind, has_default)`` parameter specs."""
    kind = p.kind
    if kind is inspect.Parameter.VAR_POSITIONAL:
        return [(p.name, "*args", False)]
    if kind is inspect.Parameter.VAR_KEYWORD:
        return [(p.name, "**kwargs", False)]
    if kind is inspect.Parameter.KEYWORD_ONLY:
        specs = []
        if not kw_only_emitted[0]:
            specs.append(("", "kw_only", False))
            kw_only_emitted[0] = True
        has_default = p.default is not inspect.Parameter.empty
        specs.append((p.name, "pos", has_default))
        return specs
    has_default = p.default is not inspect.Parameter.empty
    return [(p.name, "pos", has_default)]


def _format_method_entry(name: str, sig: inspect.Signature) -> str:
    kw_only_emitted = [False]
    param_specs: list[tuple[str, str, bool]] = []
    for p in sig.parameters.values():
        param_specs.extend(_format_param(p, kw_only_emitted))
    return f"    _append_method(out, {name!r}, {tuple(param_specs)!r})"


# Keep each generated function comfortably below the self-backend's 1 MB
# target.  The compact specs make eight entries small while still avoiding a
# giant module-top constructor.
_CHUNK_SIZE = 8


def main() -> int:
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    method_entries: list[str] = []
    skipped: list[tuple[str, str]] = []
    for method_name in L1_CODEGEN_HOST_METHODS:
        fn = getattr(L1CodeGen, method_name, None)
        if fn is None or not callable(fn):
            skipped.append((method_name, "not-callable"))
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError) as e:
            skipped.append((method_name, f"signature-fail: {e!r}"))
            continue
        method_entries.append(_format_method_entry(method_name, sig))

    out_path = (
        _REPO_ROOT
        / "pcc" / "frontends" / "python"
        / "codegen"
        / "_l1_codegen_static_methods.py"
    )
    # Emit the table in per-function chunks instead of one module-level
    # literal.  A 301-entry nested literal lowered to ONE module-top function
    # with 72100 basic blocks and ~234000 GC barrier calls -- 41 MB of IR, the
    # single largest shard of a self-hosted build, and the reason the emit
    # phase serialises onto one oversized worker (measured: 53.8% of stage2).
    #
    # Splitting the *statement* would not help: block count follows the
    # enclosing function, so each chunk has to be its own function.  Tuple
    # elements are independent literals, so concatenating the chunks is
    # exactly the original tuple -- the regeneration gate compares the two
    # for equality.
    chunk_size = _CHUNK_SIZE
    chunks: list[list[str]] = []
    index = 0
    while index < len(method_entries):
        chunks.append(method_entries[index : index + chunk_size])
        index += chunk_size

    part_defs: list[str] = []
    part_calls: list[str] = []
    part_index = 0
    while part_index < len(chunks):
        chunk_body = "\n".join(chunks[part_index])
        part_defs.append(
            f"def _part_{part_index}(out):\n{chunk_body}\n"
        )
        part_calls.append(f"    _part_{part_index}(out)")
        part_index += 1
    parts_text = "\n\n".join(part_defs)
    joined = "\n".join(part_calls)
    body = (
        "_DYN_TYPE = ('dyn',)\n\n"
        "def _append_method(out, method_name, param_specs):\n"
        "    param_types = []\n"
        "    call_sig = []\n"
        "    for param_spec in param_specs:\n"
        "        param_name = param_spec[0]\n"
        "        param_kind = param_spec[1]\n"
        "        has_default = param_spec[2]\n"
        "        call_sig.append({\n"
        "            'name': param_name,\n"
        "            'kind': param_kind,\n"
        "            'annotation': _DYN_TYPE,\n"
        "            'default': None,\n"
        "            'has_default': has_default,\n"
        "        })\n"
        "        if param_kind != 'kw_only':\n"
        "            param_types.append(_DYN_TYPE)\n"
        "    out.append({\n"
        "        'name': method_name,\n"
        "        'kind': 'instance',\n"
        "        'return_ty': _DYN_TYPE,\n"
        "        'param_types': tuple(param_types),\n"
        "        'call_sig': tuple(call_sig),\n"
        "        'box_int_abi': False,\n"
        "    })\n\n"
        + parts_text
        + "\n\ndef _build_static_methods():\n"
        "    out = []\n"
        + joined
        + "\n    return tuple(out)\n\n"
        "L1_CODEGEN_STATIC_METHODS = _build_static_methods()\n"
    )
    content = (
        '"""Auto-generated pure-data static method entries for L1CodeGen.\n'
        "\n"
        "DO NOT EDIT BY HAND.  Regenerate via\n"
        "``scripts/regen_l1_codegen_static_methods.py`` after changing\n"
        "``host_contract.L1_CODEGEN_HOST_METHODS`` or a mixin source\n"
        "signature.\n"
        "\n"
        "This file lives in the no-libpython bootstrap closure for pcc1,\n"
        "so it stays restricted to tuples, lists, dicts, string literals,\n"
        "and the eager chunk functions below.\n"
        "\n"
        "The generated source stores compact parameter triples and inflates\n"
        "the original tuple-of-dicts schema eagerly.  Repeating that schema\n"
        "inline made the previous parts total tens of megabytes of IR.\n"
        "Chunk functions append into one list, avoiding repeated tuple\n"
        "concatenation while keeping each lowering unit bounded.\n"
        '"""\n'
        "from __future__ import annotations\n"
        "\n"
        f"{body}"
    )
    args = sys.argv[1:]
    if args not in ([], ["--check"]):
        print("usage: regen_l1_codegen_static_methods.py [--check]", file=sys.stderr)
        return 2
    if args == ["--check"]:
        current = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        if current != content:
            print(f"stale generated file: {out_path}", file=sys.stderr)
            return 1
        print(f"current {out_path}", file=sys.stderr)
    else:
        out_path.write_text(content, encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
    print(f"  {len(method_entries)} method entries", file=sys.stderr)
    if skipped:
        print(f"  {len(skipped)} skipped:", file=sys.stderr)
        for name, reason in skipped:
            print(f"    - {name}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
