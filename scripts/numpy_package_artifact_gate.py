#!/usr/bin/env python3
"""Build NumPy's core extension through the generic pcc package executor."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from pcc.package.build_exec import execute_build_actions
from pcc.package.metadata import current_platform_tag
from pcc.package.schema import pcc_native_extension_suffix
from scripts.numpy_first_blocker import evaluate_result
from scripts.numpy_head_gate import (
    _dynamic_dependencies,
    _exports_pyinit,
    _loader_probe,
    build_plan as build_numpy_plan,
    validate_plan as validate_numpy_plan,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "projects" / "numpy-2.4.4"
TARGET = "numpy/_core/_multiarray_umath.cpython-314-darwin.so"
MODULE_NAME = "_multiarray_umath"
SCHEMA = "pcc.numpy-package-artifact-gate.v1"


class GateError(RuntimeError):
    pass


def _write_result(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_gate(
    source: Path,
    build_root: Path,
    result_path: Path,
    *,
    jobs: int,
    compile_timeout: int,
    loader_timeout: int,
) -> dict[str, object]:
    started = time.monotonic()
    source = source.resolve()
    build_root = build_root.resolve()
    result_path = result_path.resolve()
    build_root.mkdir(parents=True, exist_ok=True)
    suffix = pcc_native_extension_suffix(current_platform_tag())
    artifact = build_root / "site" / f"{MODULE_NAME}{suffix}"
    source_plan = build_numpy_plan(source)
    validate_numpy_plan(source_plan)
    result: dict[str, object] = {
        "schema": SCHEMA,
        "status": "FAIL",
        "source": {
            "name": source_plan.source_name,
            "version": source_plan.source_version,
            "sha256": source_plan.source_sha256,
            "path": str(source),
            "meson_build_graph": str(source_plan.meson_build),
        },
        "target": TARGET,
        "mode": {
            "compiler": "host-pcc-current-source-package-executor",
            "backend": "self",
            "python_abi": "pcc-native",
            "libpython": "off",
            "ir_scaffold": "on",
        },
        "failure": None,
    }
    try:
        print(
            f"[numpy-package] replay target={TARGET} jobs={max(1, jobs)}",
            flush=True,
        )
        build_report = execute_build_actions(
            "numpy",
            source,
            execute=True,
            from_compile_commands=True,
            meson_target=TARGET,
            abi_mode="pcc-native",
            link_output=artifact,
            jobs=max(1, jobs),
            timeout=compile_timeout,
        )
        actions = build_report["actions"]
        compile_actions = [
            action
            for action in actions
            if str(action["kind"]).startswith("compile_command_")
        ]
        failed_actions = [
            {
                "kind": action["kind"],
                "source": action["source"],
                "status": action["status"],
                "stderr": str(action["stderr"])[-4000:],
            }
            for action in actions
            if action["status"] not in {"passed", "planned"}
        ]
        link_action = next(
            (action for action in actions if action["kind"] == "native_link"), None
        )
        cpython_header_tokens = [
            token
            for action in compile_actions
            for token in action["command"]
            if "python3.14" in token or "Python.framework" in token
        ]
        result["build"] = {
            "ok": build_report["ok"],
            "jobs": build_report["jobs"],
            "target_objects": len(build_report["meson_target_replay"]["objects"]),
            "compile_actions": len(compile_actions),
            "compile_passed": sum(
                action["status"] == "passed" for action in compile_actions
            ),
            "cxx_actions": sum(
                str(action["kind"]).endswith("cxx") for action in compile_actions
            ),
            "fresh_outputs": all(
                "pcc-native-target/objects" in str(action["output"])
                for action in compile_actions
            ),
            "cpython_header_tokens": cpython_header_tokens,
            "link_status": link_action["status"] if link_action else None,
            "link_inputs": (
                sum(str(token).endswith(".o") for token in link_action["command"])
                if link_action
                else 0
            ),
            "linker": link_action["command"][0] if link_action else None,
            "failed_actions": failed_actions,
            "diagnostics": build_report["diagnostics"],
            "linkage": build_report["linkage"],
        }
        if build_report["ok"] is not True or failed_actions:
            raise GateError("package executor build did not pass")
        if len(compile_actions) != 136:
            raise GateError(
                f"package executor selected {len(compile_actions)} objects, expected 136"
            )
        if cpython_header_tokens:
            raise GateError("package executor retained CPython include paths")
        if link_action is None or result["build"]["link_inputs"] != 136:
            raise GateError(
                "package executor link did not consume the 136-object closure"
            )
        if not artifact.is_file():
            raise GateError("package executor did not emit the requested artifact")
        linkage = build_report["linkage"]
        if linkage["links_libpython"] is not False:
            raise GateError("pcc-native NumPy artifact links libpython")

        dependencies, dependency_process = _dynamic_dependencies(artifact)
        result["artifact"] = {
            "path": str(artifact),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "suffix": suffix,
            "dependencies": dependencies,
            "dependency_returncode": dependency_process.returncode,
            "exports_pyinit": _exports_pyinit(artifact),
        }
        if result["artifact"]["exports_pyinit"] is not True:
            raise GateError(f"artifact does not export PyInit_{MODULE_NAME}")

        loader_root = build_root / "loader"
        loader_root.mkdir(parents=True, exist_ok=True)
        loader = _loader_probe(
            loader_root,
            artifact,
            source_root=source,
            timeout=loader_timeout,
        )
        result["loader"] = loader
        if loader.get("compile_returncode") != 0:
            raise GateError("strict self/no-libpython loader did not compile")
        if loader.get("links_libpython") is not False:
            raise GateError("strict loader links libpython")
        if loader.get("links_llvm") is not False:
            raise GateError("strict self-backend loader links LLVM")
        if loader.get("entered_pyinit") is not True:
            raise GateError("strict loader did not enter PyInit")
        if loader.get("entered_py_mod_exec") is not True:
            raise GateError("strict loader did not enter Py_mod_exec")
        blocker_ratchet = evaluate_result(result, "numpy-package-artifact")
        result["first_blocker_ratchet"] = blocker_ratchet
        if blocker_ratchet.get("accepted") is not True:
            raise GateError(
                "NumPy first-blocker ratchet rejected the run: "
                + "; ".join(str(item) for item in blocker_ratchet.get("errors", []))
            )
        result["status"] = "PASS"
    except (GateError, OSError, TypeError, KeyError) as exc:
        result["failure"] = str(exc)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    _write_result(result_path, result)
    print(
        f"[numpy-package] status={result['status']} "
        f"elapsed={result['duration_seconds']}s result={result_path}",
        flush=True,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(SOURCE))
    parser.add_argument(
        "--build-root", default=str(ROOT / "build/head-truth/numpy-package")
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "build/head-truth/numpy-package/result.json"),
    )
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--compile-timeout", type=int, default=90)
    parser.add_argument("--loader-timeout", type=int, default=120)
    args = parser.parse_args(argv)
    result = run_gate(
        Path(args.source),
        Path(args.build_root),
        Path(args.output),
        jobs=args.jobs,
        compile_timeout=args.compile_timeout,
        loader_timeout=args.loader_timeout,
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
