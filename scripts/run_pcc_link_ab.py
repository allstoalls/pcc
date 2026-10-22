#!/usr/bin/env python3
"""Receipt-bound A/B runner for pcc's owned Darwin Mach-O linker.

The expensive self-backend IR-to-assembly phase is deliberately outside this
harness.  One frozen directory of ``.s`` inputs is assembled exactly once to
encoded pcc-native objects; control and candidate then link the identical
``.pco`` paths in alternating order.  Every measured image is executed with
``--help`` and must be byte-identical across both arms.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import re
from contextlib import contextmanager
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "build" / ".pcc-performance.lock"


class LinkABError(RuntimeError):
    pass


def _progress(message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


_COMPETING_PCC = re.compile(
    r"(?:/pcc[123](?:\s|$)|scripts/bootstrap\.py|"
    r"--pcc-python-multi-codegen-worker)"
)


def _parse_competing_processes(process_table: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for line in process_table.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdecimal():
            continue
        pid = int(fields[0])
        command = fields[1]
        if pid != os.getpid() and _COMPETING_PCC.search(command):
            found.append((pid, command))
    return found


def _assert_no_competing_pcc(context: str) -> None:
    result = subprocess.run(
        ["ps", "-Ao", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    found = _parse_competing_processes(result.stdout)
    if found:
        summary = ", ".join(
            f"pid {pid}: {command[:160]}" for pid, command in found[:4]
        )
        raise LinkABError(
            f"competing pcc/bootstrap work detected {context}: {summary}"
        )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _source_receipt() -> dict[str, str]:
    paths = sorted((REPO_ROOT / "pcc" / "backend").glob("*.py"))
    paths.extend([
        REPO_ROOT / "scripts" / "pcc_link_macho.py",
        Path(__file__).resolve(),
    ])
    return {
        path.relative_to(REPO_ROOT).as_posix(): _sha256_path(path)
        for path in paths
    }


def _parse_time_metrics(text: str) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) == 2 and fields[0] in ("real", "user", "sys"):
            metrics[fields[0] + "_s"] = float(fields[1])
            continue
        if len(fields) >= 2 and fields[0].lstrip("-").isdigit():
            label = " ".join(fields[1:])
            key = {
                "maximum resident set size": "max_rss_bytes",
                "instructions retired": "instructions",
                "cycles elapsed": "cycles",
                "peak memory footprint": "peak_footprint_bytes",
            }.get(label)
            if key is not None:
                metrics[key] = int(fields[0])
    for required in ("real_s", "user_s", "sys_s", "max_rss_bytes"):
        if required not in metrics:
            raise LinkABError(f"time output is missing {required}")
    return metrics


def _balanced_pair_order(pair_index: int) -> tuple[str, str]:
    return (
        ("control", "candidate")
        if pair_index % 2 == 1
        else ("candidate", "control")
    )


def _parse_env_assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, setting = value.partition("=")
        if not separator or not name or not name.replace("_", "A").isalnum():
            raise LinkABError(
                f"environment override must be NAME=VALUE, got {value!r}"
            )
        result[name] = setting
    return result


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _terminate_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_group(process)
        raise LinkABError(
            f"command timed out after {timeout}s: {' '.join(command)}"
        ) from exc
    except BaseException:
        _terminate_group(process)
        raise
    return subprocess.CompletedProcess(
        command, process.returncode, stdout, stderr,
    )


@contextmanager
def _performance_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LinkABError(
                f"another performance run holds {LOCK_PATH}"
            ) from exc
        owner = {
            "active": True,
            "argv": sys.argv,
            "pid": os.getpid(),
            "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps(owner, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        try:
            yield
        finally:
            owner["active"] = False
            owner["completed_at_utc"] = (
                dt.datetime.now(dt.timezone.utc).isoformat()
            )
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(owner, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _assemble_one(item: tuple[str, str]) -> tuple[str, bytes]:
    source_path, relative_name = item
    from pcc.backend.macho_assemble_worker import (
        assemble_asm_path_to_encoded,
    )

    return relative_name, assemble_asm_path_to_encoded(source_path)


def _prepare_native_objects(
    assembly_root: Path,
    output_dir: Path,
    *,
    jobs: int,
    expected_count: int | None,
) -> tuple[list[Path], list[dict[str, object]]]:
    assembly_paths = sorted(assembly_root.rglob("*.s"))
    if not assembly_paths:
        raise LinkABError(f"no .s inputs under {assembly_root}")
    if expected_count is not None and len(assembly_paths) != expected_count:
        raise LinkABError(
            f"expected {expected_count} assembly inputs, found "
            f"{len(assembly_paths)}"
        )
    output_dir.mkdir(parents=True)
    items = [
        (str(path), path.relative_to(assembly_root).as_posix())
        for path in assembly_paths
    ]
    if jobs == 1:
        encoded = [_assemble_one(item) for item in items]
    else:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(jobs, len(items)),
        ) as pool:
            encoded = list(pool.map(_assemble_one, items))
    native_paths: list[Path] = []
    manifest: list[dict[str, object]] = []
    for index, ((source_path, relative_name), (_, payload)) in enumerate(
        zip(items, encoded, strict=True)
    ):
        native_path = output_dir / f"{index:04d}.pco"
        native_path.write_bytes(payload)
        native_paths.append(native_path)
        manifest.append({
            "assembly": relative_name,
            "assembly_bytes": Path(source_path).stat().st_size,
            "assembly_sha256": _sha256_path(Path(source_path)),
            "native_bytes": len(payload),
            "native_sha256": hashlib.sha256(payload).hexdigest(),
        })
    return native_paths, manifest


def _measure(
    *,
    arm: str,
    run_name: str,
    native_paths: list[Path],
    archives: list[Path],
    output_dir: Path,
    timeout: int,
    arm_env: dict[str, str],
) -> dict[str, object]:
    _assert_no_competing_pcc("before " + run_name)
    output_path = output_dir / (run_name + ".macho")
    time_path = output_dir / (run_name + ".time")
    command = [
        "/usr/bin/time", "-lp", "-o", str(time_path),
        sys.executable,
        str(REPO_ROOT / "scripts" / "pcc_link_macho.py"),
    ]
    for path in native_paths:
        command.extend(("--native-object", str(path)))
    for path in archives:
        command.extend(("--archive", str(path)))
    command.extend(("--out", str(output_path)))
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env["PCC_MACHO_INCREMENTAL_LINK_CACHE"] = "off"
    env.update(arm_env)
    result = _run(command, env=env, timeout=timeout)
    if result.returncode != 0:
        raise LinkABError(
            f"{run_name} link failed ({result.returncode}): {result.stderr}"
        )
    help_result = _run(
        [str(output_path), "--help"], env=env, timeout=30,
    )
    if help_result.returncode != 0:
        raise LinkABError(
            f"{run_name} --help failed ({help_result.returncode}): "
            f"{help_result.stderr}"
        )
    _assert_no_competing_pcc("after " + run_name)
    return {
        "arm": arm,
        "help_sha256": hashlib.sha256(
            (help_result.stdout + "\0" + help_result.stderr).encode("utf-8")
        ).hexdigest(),
        "image_bytes": output_path.stat().st_size,
        "image_sha256": _sha256_path(output_path),
        "metrics": _parse_time_metrics(time_path.read_text(encoding="utf-8")),
        "name": run_name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_pcc_link_ab")
    parser.add_argument("--assembly-root", required=True)
    parser.add_argument("--archive", action="append", default=[])
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--assembly-jobs", type=int, default=8)
    parser.add_argument("--expected-input-count", type=int)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--control-env", action="append", default=[])
    parser.add_argument("--candidate-env", action="append", default=[])
    args = parser.parse_args(argv)
    if args.pairs <= 0 or args.assembly_jobs <= 0 or args.timeout <= 0:
        parser.error("pairs, assembly-jobs and timeout must be positive")
    assembly_root = Path(args.assembly_root).resolve()
    try:
        arm_environments = {
            "control": _parse_env_assignments(args.control_env),
            "candidate": _parse_env_assignments(args.candidate_env),
        }
    except LinkABError as exc:
        parser.error(str(exc))
    archives = [Path(path).resolve() for path in args.archive]
    output_dir = Path(args.out_dir).resolve()
    if output_dir.exists():
        parser.error("--out-dir must not already exist")
    for path in [assembly_root, *archives]:
        if not path.exists():
            parser.error(f"input does not exist: {path}")
    output_dir.mkdir(parents=True)
    manifest_path = output_dir / "manifest.json"
    source_before = _source_receipt()
    archive_receipt = {
        str(path): {"bytes": path.stat().st_size, "sha256": _sha256_path(path)}
        for path in archives
    }
    manifest: dict[str, object] = {
        "archives": archive_receipt,
        "assembly_root": str(assembly_root),
        "arm_environments": arm_environments,
        "pairs": args.pairs,
        "runs": [],
        "source_before": source_before,
        "status": "preparing",
    }
    _atomic_json(manifest_path, manifest)
    _assert_no_competing_pcc("before acquiring the performance lock")
    with _performance_lock():
        _progress("performance lock acquired; assembling frozen inputs once")
        native_paths, inputs = _prepare_native_objects(
            assembly_root,
            output_dir / "native",
            jobs=args.assembly_jobs,
            expected_count=args.expected_input_count,
        )
        manifest["inputs"] = inputs
        manifest["status"] = "warming"
        _atomic_json(manifest_path, manifest)
        _progress(f"assembled {len(native_paths)} native objects; warmups begin")
        runs = manifest["runs"]
        assert isinstance(runs, list)
        for arm in ("control", "candidate"):
            _progress(f"starting warmup {arm}")
            runs.append(_measure(
                arm=arm,
                run_name="warmup." + arm,
                native_paths=native_paths,
                archives=archives,
                output_dir=output_dir,
                timeout=args.timeout,
                arm_env=arm_environments[arm],
            ))
            _atomic_json(manifest_path, manifest)
            _progress(
                f"finished warmup {arm}: "
                f"{runs[-1]['metrics']['real_s']}s"
            )
        manifest["status"] = "measuring"
        for pair_index in range(1, args.pairs + 1):
            for arm in _balanced_pair_order(pair_index):
                _progress(f"starting pair {pair_index} {arm}")
                runs.append(_measure(
                    arm=arm,
                    run_name=f"pair{pair_index}.{arm}",
                    native_paths=native_paths,
                    archives=archives,
                    output_dir=output_dir,
                    timeout=args.timeout,
                    arm_env=arm_environments[arm],
                ))
                _atomic_json(manifest_path, manifest)
                _progress(
                    f"finished pair {pair_index} {arm}: "
                    f"{runs[-1]['metrics']['real_s']}s"
                )
        hashes = {run["image_sha256"] for run in runs}
        help_hashes = {run["help_sha256"] for run in runs}
        if len(hashes) != 1 or len(help_hashes) != 1:
            raise LinkABError("control/candidate output or --help bytes differ")
        source_after = _source_receipt()
        if source_after != source_before:
            raise LinkABError("linker source changed during A/B")
        for path, receipt in archive_receipt.items():
            archive = Path(path)
            if (
                archive.stat().st_size != receipt["bytes"]
                or _sha256_path(archive) != receipt["sha256"]
            ):
                raise LinkABError(f"archive changed during A/B: {archive}")
        manifest["source_after"] = source_after
        manifest["status"] = "complete"
        _atomic_json(manifest_path, manifest)
        _progress("A/B complete; images, --help output and source receipts match")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LinkABError as exc:
        print(f"run_pcc_link_ab: {exc}", file=sys.stderr)
        raise SystemExit(1)
