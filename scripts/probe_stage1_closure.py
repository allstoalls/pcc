#!/usr/bin/env python3
"""Discover the configured Stage1 source graph through the production pipeline.

The CLI stops before closed-world export collection, Stage1 module codegen,
native emission or linking. Production discovery includes provider-admission
trial codegen; each admission is recorded. A source graph is not a Stage1 build
or closed-world qualification. --lexical-only refuses trial codegen and reports
blocked admission instead of returning a partial-success graph.

Example:
    python scripts/probe_stage1_closure.py --json /tmp/stage1-sources.json

Legacy codegen helpers remain callable by the separate fallback diagnostics;
the discovery-only CLI never invokes them.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback


def _set_env_defaults() -> None:
    os.environ.setdefault("PCC_PYTHON_LIBPYTHON", "off")


def _try_per_module_lower(srcs: list[str], mods: list[str]):
    """Compile each module independently to surface codegen failures
    without aborting on the first error."""
    from pcc.frontends.python import pipeline as _pipeline
    from pcc.frontends.python import type_infer as _type_infer
    from pcc.frontends.python.codegen import layer1 as _layer1
    from pcc.frontends.python.py_lift import parse_and_lift

    results = []
    for src, mod in zip(srcs, mods):
        ok = True
        stage = "parse"
        err = None
        try:
            with open(src, "r", encoding="utf-8") as f:
                source = f.read()
            ast_mod = parse_and_lift(source, src, mod)
            stage = "needs_libpython"
            needs = _pipeline._module_needs_libpython(ast_mod, native_modules=mods)
            stage = "type_infer"
            typed = _type_infer.infer_module(ast_mod)
            stage = "codegen"
            cg = _layer1.L1CodeGen(typed, emit_cpy_main_exitcode=False)
            ir = cg.generate(typed)
            ir_text = str(ir)
            stage = "done"
            cpy_calls = _count_py_cpy_calls(ir_text)
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
            cpy_calls = 0
            needs = None
        results.append({
            "module": mod,
            "src": src,
            "ok": ok,
            "fail_stage": None if ok else stage,
            "error": err,
            "needs_libpython_ast": needs,
            "py_cpy_calls": cpy_calls,
        })
    return results


def _count_py_cpy_calls(ir_text: str) -> int:
    import re
    return len(re.findall(r"\bcall [^\n]*@py_cpy_", ir_text))


def _try_full_multi_compile(srcs: list[str], mods: list[str]) -> tuple[bool, str, str]:
    """Try the actual compile_python_multi over the closure with
    strict no-libpython ownership. Return (ok, ir_text_or_empty, error).

    This legacy IR diagnostic is used by fallback tests, not by the
    discovery-only CLI. It does not emit or execute a native Stage1 compiler.
    """
    from pcc.frontends.python.pipeline import compile_python_multi
    out_path = "/tmp/stage1_closure_probe.ll"
    try:
        compile_python_multi(
            srcs,
            out_path,
            verbose=False,
            emit_llvm_only=True,
            entry_module=mods[0],
            module_names=mods,
            libpython_mode="off",
            backend="self",
        )
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                ir_text = f.read()
            return True, ir_text, ""
        return False, "", "no IR produced"
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"


def _classify_top_fallbacks(ir_text: str) -> list[tuple[int, str]]:
    import re
    counts = {}
    for m in re.finditer(r"\bcall [^\n]*@(py_cpy_[a-z0-9_]+)", ir_text):
        name = m.group(1)
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def _categorize_error(err: str) -> str:
    if not err:
        return "ok"
    s = err.lower()
    if "notimplementederror" in s:
        if "lambda" in s:
            return "codegen.lambda"
        if "kwarg" in s or "keyword" in s:
            return "codegen.kwargs"
        if "comprehension" in s or "_list_comp" in s:
            return "codegen.comprehension"
        if "decorator" in s:
            return "codegen.decorator"
        return "codegen.other"
    if "typeinferenceerror" in s or "type_infer" in s:
        return "type_infer"
    if "pypipelineerror" in s:
        return "pipeline"
    if "syntaxerror" in s or "parse" in s:
        return "parse"
    return "other"


class DiscoveryRequiresCodegen(RuntimeError):
    """Production needs provider trial codegen to finish admission."""


@contextmanager
def _record_provider_admission(records, *, lexical_only):
    from pcc.frontends.python import pipeline_dependency_closure as closure

    previous = closure._stdlib_module_compiles

    def observe_admission(source, module):
        # Only owned stdlib sources bypass production's trial-codegen branch.
        trial_codegen = closure._native_stdlib_root_for_path(source) is None
        record = {"module": module, **_source_identity(source),
                  "trial_codegen": trial_codegen, "status": "ERROR"}
        records.append(record)
        if lexical_only and trial_codegen:
            record["status"] = "BLOCKED"
            raise DiscoveryRequiresCodegen(
                "source discovery requires trial codegen for provider "
                + module + ": " + source
                + "; lexical-only probe stopped before codegen"
            )
        admitted = previous(source, module)
        record["status"] = "ADMITTED" if admitted else "REJECTED"
        return admitted

    closure._stdlib_module_compiles = observe_admission
    try:
        yield
    finally:
        closure._stdlib_module_compiles = previous


def _discover_stage1_closure(
    entry_src: str,
    *,
    libpython_mode: str = "off",
    ir_scaffold_mode: str | None = None,
    recursive_stdlib: bool = False,
    lexical_only: bool = False,
):
    """Reuse both production discovery boundaries for an executable entry."""
    from pcc.frontends.python import pipeline

    entry_src = os.path.abspath(entry_src)
    if not os.path.isfile(entry_src):
        raise pipeline.PyPipelineError("input file not found: " + entry_src)
    module_name = pipeline._module_name_from_src(entry_src)
    libpython_mode = pipeline._resolve_libpython_mode(libpython_mode)
    ir_scaffold_mode = pipeline._resolve_ir_scaffold_mode(ir_scaffold_mode)
    direct_indexed_assembly = str(
        os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on")
    admissions = []
    with _record_provider_admission(admissions, lexical_only=lexical_only):
        sources, modules, effective_recursive_stdlib = (
            pipeline._prepare_single_source_compile_closure(
                entry_src,
                module_name,
                emit_llvm_only=False,
                libpython_mode=libpython_mode,
                ir_scaffold_mode=ir_scaffold_mode,
                recursive_stdlib=recursive_stdlib,
                python_library=False,
            )
        )
        if len(sources) > 1 or effective_recursive_stdlib or direct_indexed_assembly:
            sources, modules = pipeline._prepare_multi_source_compile_closure(
                sources,
                modules,
                recursive_stdlib=effective_recursive_stdlib,
                ir_scaffold_mode=ir_scaffold_mode,
            )
            pipeline._validate_package_site_no_libpython_abi(
                sources, libpython_mode=libpython_mode,
            )
    if (not modules or len(sources) != len(modules)
            or len(set(modules)) != len(modules) or module_name not in modules):
        raise pipeline.PyPipelineError("invalid production source closure")
    options = {
        "entry_module": module_name,
        "entry_source": entry_src,
        "backend": "self",
        "target": pipeline._host_target_triple_for_self_backend(),
        "libpython_mode": libpython_mode,
        "ir_scaffold_mode": ir_scaffold_mode,
        "recursive_stdlib_requested": recursive_stdlib,
        "recursive_stdlib_effective": effective_recursive_stdlib,
        "emit_llvm_only": False,
        "python_library": False,
        "direct_indexed_assembly": direct_indexed_assembly,
        "provider_admission_mode": "lexical_only" if lexical_only else "production",
    }
    return sources, modules, options, admissions


def _tightened_closure(entry_src: str) -> tuple[list[str], list[str]]:
    """Compatibility name for production discovery; no separate tight walker."""
    sources, modules, _options, _admissions = _discover_stage1_closure(entry_src)
    return sources, modules


def _source_identity(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _closure_receipt(sources, modules, options, admissions):
    from pcc.driver import paths
    from pcc.frontends.python import (
        pipeline,
        pipeline_dependency_closure,
        pipeline_import_policy,
        pipeline_import_scan,
        pipeline_packages,
    )
    from pcc.package import environment

    nodes = [
        {"module": module, **_source_identity(source)}
        for source, module in zip(sources, modules)
    ]
    canonical = json.dumps(nodes, sort_keys=True, separators=(",", ":"))
    setting_names = (
        "PCC_PY_STDLIB_ROOT", "PCC_SOURCE_ROOT", "PCC_REPO_ROOT",
        "PCC_PACKAGE_SITE", "PCC_ENVIRONMENT", "PCC_DATA_HOME",
        "PCC_PACKAGE_TARGET_PYTHON", "PCC_NATIVE_ABI_VERSION",
        "PCC_PACKAGE_ABI_MODE", "PCC_TARGET_TRIPLE", "PCC_TARGET_ARCH",
        "VIRTUAL_ENV", "XDG_DATA_HOME", "XDG_CACHE_HOME", "PCC_HOST_PYTHON",
        "PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "PYTHONNOUSERSITE",
        "PYTHONUSERBASE",
    )
    return {
        "schema": "pcc.stage1-source-discovery.v1",
        "status": "OK",
        "scope": "source_discovery",
        "stage1_qualified": False,
        "options": options,
        "provider_admissions": admissions,
        "provider_trial_codegen_count": sum(record["trial_codegen"] for record in admissions),
        "module_count": len(nodes),
        "nodes": nodes,
        "ordered_nodes_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "package_site_roots": pipeline_packages.package_site_roots(),
        "native_provider_roots": pipeline_dependency_closure._pcc_package_dir_candidates(),
        "environment": {name: os.environ.get(name) for name in setting_names},
        "host_interpreter": {"executable": sys.executable, "version": sys.version},
        "discovery_sources": [
            _source_identity(module.__file__)
            for module in (pipeline, pipeline_dependency_closure,
                           pipeline_import_policy, pipeline_import_scan,
                           pipeline_packages, paths, environment)
        ],
        "probe_source": _source_identity(__file__),
        "not_run": ["closed_world_exports", "stage1_module_codegen", "native_emission",
                    "link", "native_stage1_execution", "stage2_stage3_fixed_point"],
    }


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, default=root / "pcc" / "__main__.py")
    parser.add_argument(
        "--python-libpython", choices=("off", "auto", "on"),
        default=os.environ.get("PCC_BOOTSTRAP_PYTHON_LIBPYTHON") or "off",
    )
    parser.add_argument("--ir-scaffold", choices=("off", "auto", "on"))
    parser.add_argument("--recursive-stdlib", action="store_true")
    parser.add_argument(
        "--lexical-only", action="store_true",
        help="fail if normal provider admission requires trial codegen",
    )
    parser.add_argument("--json", type=Path, help="write a source-hashed discovery receipt")
    args = parser.parse_args(argv)
    if os.environ.get("PCC_PROBE_CLOSURE"):
        parser.error(
            "PCC_PROBE_CLOSURE tight/max walkers were removed; unset it and "
            "select the production entry and compiler options"
        )
    try:
        sources, modules, options, admissions = _discover_stage1_closure(
            str(args.entry),
            libpython_mode=args.python_libpython,
            ir_scaffold_mode=args.ir_scaffold,
            recursive_stdlib=args.recursive_stdlib,
            lexical_only=args.lexical_only,
        )
        receipt = _closure_receipt(sources, modules, options, admissions)
    except Exception as error:
        receipt = {
            "schema": "pcc.stage1-source-discovery.v1",
            "status": "ERROR",
            "scope": "source_discovery",
            "stage1_qualified": False,
            "provider_admission_mode": "lexical_only" if args.lexical_only else "production",
            "entry_source": str(args.entry.resolve()),
            "error": type(error).__name__ + ": " + str(error),
        }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        print("wrote " + str(args.json.resolve()), flush=True)
    if receipt["status"] == "ERROR":
        print(receipt["error"], file=sys.stderr, flush=True)
        return 1
    print(f"production source closure: {len(modules)} modules", flush=True)
    print("entry: " + options["entry_source"], flush=True)
    print("options: " + json.dumps(options, sort_keys=True), flush=True)
    print(f"provider-admission trial codegen checks: {receipt['provider_trial_codegen_count']}", flush=True)
    print("Discovery only; closed-world exports and native Stage1 are unqualified.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
