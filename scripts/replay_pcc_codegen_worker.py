#!/usr/bin/env python3
"""Replay one frozen Stage2 codegen-worker manifest under a chosen pcc1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


MANIFEST_SCHEMA = "pcc.frontends.python.codegen_worker.v4"
REPLAY_SCHEMA = "pcc.codegen-worker-replay.v1"


class ReplayError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def prepare_replay(
    *,
    compiler: Path,
    manifest: Path,
    stage_receipt: Path,
    output_dir: Path,
    native_object: int,
    exports_path: Path | None = None,
    host_source_root: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    compiler = compiler.expanduser().resolve(strict=True)
    manifest = manifest.expanduser().resolve(strict=True)
    stage_receipt = stage_receipt.expanduser().resolve(strict=True)
    resolved_exports = None
    if exports_path is not None:
        resolved_exports = exports_path.expanduser().resolve(strict=True)
        if not resolved_exports.is_file():
            raise ReplayError(
                "export override is not a file: " + str(resolved_exports)
            )
    output_dir = output_dir.expanduser().absolute()
    if native_object not in (0, 1):
        raise ReplayError("native-object mode must be 0 or 1")
    if not os.access(compiler, os.X_OK):
        raise ReplayError("compiler is not executable: " + str(compiler))
    host_sources = None
    if host_source_root is not None:
        host_sources = host_source_root.expanduser().resolve(strict=True)
        if not (host_sources / "pcc" / "__main__.py").is_file():
            raise ReplayError("host source root has no pcc entrypoint")
    if output_dir.exists():
        raise ReplayError("refusing existing replay output: " + str(output_dir))

    lines = manifest.read_text(encoding="utf-8").splitlines()
    if len(lines) < 14 or lines[0] != MANIFEST_SCHEMA:
        raise ReplayError("unsupported or truncated codegen-worker manifest")
    receipt = json.loads(stage_receipt.read_text(encoding="utf-8"))
    receipt_environment = receipt.get("environment")
    if not isinstance(receipt_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in receipt_environment.items()
    ):
        raise ReplayError("Stage2 receipt has no string environment map")

    output_dir.mkdir(parents=True)
    artifact_dir = output_dir / "artifacts"
    private_root = output_dir / "private"
    profile_dir = output_dir / "profile"
    for path in (
        artifact_dir,
        private_root / "home",
        private_root / "tmp",
        private_root / "cache",
        private_root / "pycache",
        profile_dir,
    ):
        path.mkdir(parents=True)
    replay_manifest = output_dir / "worker.manifest"
    result_path = output_dir / "worker.tsv"
    lines[1] = str(result_path)
    lines[2] = str(artifact_dir)
    if resolved_exports is not None:
        lines[3] = str(resolved_exports)
    replay_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(receipt_environment)
    environment.pop("LC_ALL", None)
    # The Stage2 receipt records bootstrap's outer environment, while
    # bootstrap.py and native_deferred._command construct the actual worker
    # environment later. In particular, replaying with an ambient default
    # pass tier silently changes a PIDX-only worker into an LLVM-text worker.
    for child, bootstrap in (
        ("PCC_PYTHON_IR_PASSES", "PCC_BOOTSTRAP_PYTHON_IR_PASSES"),
        ("PCC_RUNTIME_HIGH", "PCC_BOOTSTRAP_RUNTIME_HIGH"),
        ("PCC_RUNTIME_CC", "PCC_BOOTSTRAP_RUNTIME_CC"),
    ):
        if bootstrap in receipt_environment:
            environment[child] = receipt_environment[bootstrap]
    if native_object == 1 and host_sources is None:
        environment.update({
            "PCC_DEFER_FRONTEND_CODEGEN_PLAN": "",
            "PCC_DEFER_FRONTEND_OUTPUT": "",
            "PCC_DEFER_SELF_LINK_PLAN": "",
            "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN": "0",
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_SIDECAR": "1",
        })
    environment.update(
        {
            "HOME": str(private_root / "home"),
            "TMPDIR": str(private_root / "tmp"),
            "XDG_CACHE_HOME": str(private_root / "cache"),
            "PYTHONPYCACHEPREFIX": str(private_root / "pycache"),
            "PCC_BOOTSTRAP_PROFILE_DIR": str(profile_dir),
            "PCC_PY_FRONTEND_JOBS": "1",
            "PCC_DIRECT_INDEXED_NATIVE_OBJECT": str(native_object),
        }
    )
    compiler_prefix = [str(compiler)]
    if host_sources is not None:
        # Select the host implementation while retaining the manifest's frozen
        # AST/export inputs. This is a Stage1 owner probe, never a pcc1 claim.
        environment["PYTHONPATH"] = str(host_sources)
        environment["PCC_SOURCE_ROOT"] = str(host_sources)
        environment["PCC_REPO_ROOT"] = str(host_sources)
        environment["PCC_DIRECT_INDEXED_SIDECAR"] = "0"
        # -m would otherwise prepend cwd and load the editable checkout ahead
        # of the requested frozen PYTHONPATH root, making both arms identical.
        compiler_prefix.extend(["-P", "-m", "pcc"])
    command = [
        "/usr/bin/time",
        "-lp",
        *compiler_prefix,
        "--pcc-python-multi-codegen-worker",
        str(replay_manifest),
    ]
    replay_receipt = {
        "schema": REPLAY_SCHEMA,
        "compiler": str(compiler),
        "compiler_sha256": _sha256(compiler),
        "source_manifest": str(manifest),
        "source_manifest_sha256": _sha256(manifest),
        "stage_receipt": str(stage_receipt),
        "stage_receipt_sha256": _sha256(stage_receipt),
        "native_object": native_object,
        "execution_owner": "host-cpython" if host_sources is not None else "native-compiler",
        "host_source_root": "" if host_sources is None else str(host_sources),
        "exports_override": (
            "" if resolved_exports is None else str(resolved_exports)
        ),
        "replay_manifest": str(replay_manifest),
        "result": str(result_path),
        "artifact_dir": str(artifact_dir),
        "command": command,
        "effective_worker_environment": {
            key: environment.get(key, "") for key in (
                "PCC_PYTHON_IR_PASSES",
                "PCC_RUNTIME_HIGH",
                "PCC_RUNTIME_CC",
                "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
                "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES",
                "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
                "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
                "PCC_DIRECT_INDEXED_NATIVE_OBJECT",
                "PCC_DIRECT_INDEXED_SIDECAR",
            )
        },
    }
    (output_dir / "replay.json").write_text(
        json.dumps(replay_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return command, environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--stage-receipt", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--native-object", required=True, type=int, choices=(0, 1))
    parser.add_argument("--exports-path", type=Path)
    parser.add_argument("--host-source-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        command, environment = prepare_replay(
            compiler=args.compiler,
            manifest=args.manifest,
            stage_receipt=args.stage_receipt,
            output_dir=args.output_dir,
            native_object=args.native_object,
            exports_path=args.exports_path,
            host_source_root=args.host_source_root,
        )
        os.execve(command[0], command, environment)
    except (OSError, ValueError, json.JSONDecodeError, ReplayError) as exc:
        print("codegen worker replay error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
