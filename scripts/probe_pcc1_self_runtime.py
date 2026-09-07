#!/usr/bin/env python3
"""Probe native pcc1 runtime emission and optional passes without LLVM.

This is a capability diagnostic, not a runtime rebuild, bootstrap gate or
throughput benchmark. Pass a native pcc1 executable, not an environment wrapper.
The host orchestrator uses pcc's own parser/codec to prepare indexed inputs;
runtime source lowering and ARM64/PCO emission execute inside native pcc1.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


GUARD = '''import importlib.abc
import sys
class BlockLLVM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "llvmlite" or fullname.startswith("llvmlite.") or fullname == "pcc.llvm_capi.binding":
            raise ImportError("LLVM dependency blocked: " + fullname)
sys.meta_path.insert(0, BlockLLVM())
'''


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcc1", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path,
                        help="optionally link/run a generator canary with the emitted PCOs")
    parser.add_argument("--modules", default="py_obj,py_list,py_gen,py_gc_backend,freestanding_gc_index_table")
    args = parser.parse_args()
    compiler = args.pcc1.resolve()
    root = args.source_root.resolve()
    output = args.output_dir.resolve()
    if compiler.read_bytes()[:4] != b"\xcf\xfa\xed\xfe":
        parser.error("this Darwin probe requires the native Mach-O pcc1, not a wrapper")
    names = args.modules.split(",")
    if not names or any(not name.replace("_", "").isalnum() for name in names):
        parser.error("modules must be comma-separated runtime module basenames")
    output.mkdir(parents=True, exist_ok=False)
    (output / "sitecustomize.py").write_text(GUARD)
    helper = output / "host-python"
    helper.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(str(output) + ":" + str(root))
                      + "\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    helper.chmod(0o755)
    exec(GUARD, {})
    sys.path.insert(0, str(root))
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
    from pcc.backend.native_object import decode_native_object

    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PCC_") and key != "LC_ALL"}
    env.update(PCC_SOURCE_ROOT=str(root), PCC_REPO_ROOT=str(root),
               PYTHONPATH=str(output) + ":" + str(root),
               PCC_HOST_PYTHON="/usr/bin/false", PCC_RUNTIME_CC="/usr/bin/false",
               CC="/usr/bin/false", PCC_PY_FRONTEND_IR_CACHE="0",
               PCC_SELF_BACKEND_OBJECT_CACHE="0")
    report = {"schema": "pcc.self-runtime-capability.v1", "complete": False,
              "compiler": str(compiler), "compiler_sha256": digest(compiler),
              "source_root": str(root), "script_sha256": digest(__file__),
              "claim": "native runtime IR/ASM/PCO emission; no full runtime or O2 parity claim",
              "environment": {k: v for k, v in env.items() if k.startswith("PCC_") or k == "CC"},
              "steps": [], "modules": []}

    def persist():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def run(name, arguments, extra_env=None):
        command = [str(compiler), *map(str, arguments)]
        with (output / (name + ".log")).open("w") as log:
            result = subprocess.run(command, env=env | (extra_env or {}), cwd=root,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=60)
        report["steps"].append({"name": name, "command": command,
                                "extra_environment": extra_env or {},
                                "returncode": result.returncode})
        persist()
        print(name + ": " + str(result.returncode), flush=True)
        return result.returncode

    persist()
    for name in names:
        source = root / "pcc" / "py_runtime" / "py" / (name + ".py")
        source_hash = digest(source)
        ir = output / (name + ".ll")
        asm = output / (name + ".s")
        sidecar = output / (name + ".pidx")
        pco = output / (name + ".pco")
        if run(name + "-frontend", ["--backend", "self", "--python-library",
                                    "--emit-llvm=" + str(ir), source]):
            return 1
        if run(name + "-asm", ["--pcc-self-backend-emit-worker", ir,
                               output / (name + ".result"), asm, ""]):
            return 1
        module = parse_self_backend_module(ir.read_text())
        for function in module.functions:
            get_indexed_function_kernel(function)
        encode_indexed_module_file(str(sidecar), module)
        if run(name + "-pco", ["--pcc-self-backend-indexed-emit-worker", sidecar, pco, "PCO"]):
            return 1
        native = decode_native_object(pco.read_bytes())
        if not native.sections or digest(source) != source_hash:
            raise RuntimeError("invalid native object or source changed: " + name)
        report["modules"].append({"module": name, "source_sha256": source_hash,
            "ir_sha256": digest(ir), "asm_sha256": digest(asm), "pco_sha256": digest(pco),
            "pco_bytes": pco.stat().st_size, "functions": len(module.functions)})
        persist()

    probe = output / "pass_probe.py"
    probe.write_text("def identity(value: int):\n    return value\n")
    for pass_name in ("simplifycfg", "inline"):
        status = run("pass-" + pass_name,
                     ["--backend", "self", "--python-library",
                      "--emit-llvm=" + str(output / (pass_name + ".ll")), probe],
                     {"PCC_HOST_PYTHON": str(helper), "PCC_PYTHON_IR_PASSES": pass_name})
        log = (output / ("pass-" + pass_name + ".log")).read_text()
        report["steps"][-1]["llvm_dependency_blocked"] = "LLVM dependency blocked:" in log
        report["steps"][-1]["pass_available_without_llvm"] = status == 0
        persist()
    if args.runtime_archive is not None:
        archive = args.runtime_archive.resolve()
        archive_hash = digest(archive)
        probe = output / "execute_probe.py"
        probe.write_text("def values():\n    yield 17\n    yield 25\n\n"
                         "def main():\n    total = 0\n    for value in values():\n"
                         "        total += value\n    print(total)\n\nmain()\n")
        ir = output / "execute_probe.ll"
        asm = output / "execute_probe.s"
        binary = output / "execute_probe"
        if run("execute-frontend", ["--backend", "self", "--emit-llvm=" + str(ir), probe]):
            return 1
        if run("execute-asm", ["--pcc-self-backend-emit-worker", ir,
                               output / "execute_probe.result", asm, ""]):
            return 1
        command = [str(helper), str(root / "scripts" / "pcc_link_macho.py"),
                   "--out", str(binary), "--asm", str(asm), "--archive", str(archive)]
        for name in names:
            command.extend(["--native-object", str(output / (name + ".pco"))])
        with (output / "execute-link.log").open("w") as log:
            subprocess.run(command, env=env, cwd=root, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=60)
        result = subprocess.run([str(binary)], env=env, capture_output=True,
                                text=True, timeout=10)
        report["execution"] = {"link_command": command, "stdout": result.stdout,
            "stderr": result.stderr, "returncode": result.returncode,
            "binary_sha256": digest(binary), "archive_sha256": archive_hash,
            "claim": "selected PCOs linked with remaining prebuilt runtime members; not a full rebuild"}
        persist()
        if result.returncode != 0 or result.stdout.strip() != "42" or digest(archive) != archive_hash:
            raise RuntimeError("generator execution failed or runtime archive changed")
        print("execute: 42", flush=True)
    report["complete"] = True
    report["compiler_unchanged"] = digest(compiler) == report["compiler_sha256"]
    persist()
    return 0 if report["compiler_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
