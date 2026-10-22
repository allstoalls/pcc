"""Portable, source-identified five-GC self-host qualification driver.

Runs a full host -> pcc1 -> pcc2 -> pcc3 chain for every selected GC. Unlike
the Darwin reference harness this entry needs neither Bash nor Mach-O tools.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.platform_process_watchdog import run


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _image_header_receipt(path):
    """Bounded metadata for failure evidence, never a loader/admission verdict."""
    receipt = {"path": str(path), "capture_limit_bytes": 4096}
    try:
        with Path(path).open("rb") as stream:
            receipt["file_size"] = os.fstat(stream.fileno()).st_size
            data = stream.read(receipt["capture_limit_bytes"])
        receipt.update({"captured_bytes": len(data), "magic_hex": data[:4].hex(),
                        "captured_prefix_sha256": hashlib.sha256(data).hexdigest()})
        if not data.startswith(b"MZ"):
            receipt["format"] = "ELF" if data.startswith(b"\x7fELF") else "unknown"
            return receipt
        receipt["format"] = "PE"
        if len(data) < 64:
            raise ValueError("DOS header is truncated")
        pe = struct.unpack_from("<I", data, 60)[0]
        receipt["pe_offset"] = pe
        if pe < 64 or pe + 24 > len(data):
            raise ValueError("PE header is outside the captured prefix")
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("PE signature is missing")
        machine, count = struct.unpack_from("<HH", data, pe + 4)
        optional_size, characteristics = struct.unpack_from("<HH", data, pe + 20)
        receipt.update({"machine": machine, "section_count": count,
                        "optional_header_size": optional_size, "characteristics": characteristics})
        optional = pe + 24
        optional_end = min(optional + optional_size, len(data))
        if optional + 72 > optional_end:
            raise ValueError("PE optional header is truncated in the captured prefix")
        magic = struct.unpack_from("<H", data, optional)[0]
        receipt["optional_magic"] = magic
        if magic not in (0x10B, 0x20B):
            raise ValueError("unrecognized PE optional header magic")
        receipt.update({
            "entry_point_rva": struct.unpack_from("<I", data, optional + 16)[0],
            "image_base": struct.unpack_from("<Q" if magic == 0x20B else "<I", data,
                                             optional + (24 if magic == 0x20B else 28))[0],
            "section_alignment": struct.unpack_from("<I", data, optional + 32)[0],
            "file_alignment": struct.unpack_from("<I", data, optional + 36)[0],
            "size_of_image": struct.unpack_from("<I", data, optional + 56)[0],
            "size_of_headers": struct.unpack_from("<I", data, optional + 60)[0],
            "subsystem": struct.unpack_from("<H", data, optional + 68)[0],
            "dll_characteristics": struct.unpack_from("<H", data, optional + 70)[0],
        })
        directories = optional + (112 if magic == 0x20B else 96)
        if directories <= optional_end:
            directory_count = struct.unpack_from("<I", data, directories - 4)[0]
            captured = min(directory_count, 16, (optional_end - directories) // 8)
            receipt["directory_count"] = directory_count
            receipt["directories_truncated"] = captured != directory_count
            receipt["directories"] = [
                {"index": index, "rva": rva, "size": size}
                for index in range(captured)
                for rva, size in [struct.unpack_from("<II", data, directories + index * 8)]
            ]
        sections = optional + optional_size
        captured = min(count, 32, max(0, (len(data) - sections) // 40))
        receipt["sections_truncated"] = captured != count
        receipt["sections"] = []
        for index in range(captured):
            section = sections + index * 40
            virtual_size, rva, raw_size, raw = struct.unpack_from("<IIII", data, section + 8)
            receipt["sections"].append({
                "index": index, "name": data[section:section + 8].rstrip(b"\0").decode("ascii", "replace"),
                "virtual_size": virtual_size, "rva": rva, "raw_size": raw_size, "raw_offset": raw,
                "characteristics": struct.unpack_from("<I", data, section + 36)[0],
            })
    except (OSError, ValueError, struct.error) as exc:
        receipt["inspection_error"] = str(exc)
    return receipt


@contextlib.contextmanager
def performance_lock():
    path = ROOT / "build" / ".pcc-performance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if stream.read(1) == b"":
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _source_snapshot():
    """Hash the complete compiler/runtime/harness inputs, not a cache namespace.

    Git supplies tracked and non-ignored new inputs, including data files and
    Makefile inventories. Generated objects/caches are deliberately excluded
    by the repository ignore rules. The frontend cache's source hash is too
    narrow here: it explicitly omits runtime, C and linker implementations.
    """
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
         "pcc", "scripts", "src", "include", "utils", "hatch_build.py",
         "pyproject.toml", "uv.lock", ".python-version"], cwd=ROOT,
    )
    names = sorted({os.fsdecode(name) for name in paths.split(b"\0") if name})
    digest = hashlib.sha256()
    for name in names:
        relative = name.encode("utf-8", errors="surrogateescape")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        path = ROOT / name
        if not path.is_file():
            # A tracked deletion is meaningful even in diagnostic dirty mode.
            digest.update(b"missing\0")
        else:
            digest.update(b"file\0")
            digest.update(bytes.fromhex(_digest(path)))
    return {"source_sha256": digest.hexdigest(), "source_file_count": len(names),
            "source_scope": "compiler-runtime-headers-build-harness-v1"}


def source_identity(allow_dirty=False):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True)
    if dirty and not allow_dirty:
        raise RuntimeError("qualification requires a clean source commit; --allow-dirty is diagnostic only")
    return {"commit": head, **_source_snapshot(), "clean_commit": not bool(dirty)}


def build_configuration(target):
    from pcc.frontends.python.owned_runtime_build import runtime_build_config, runtime_ir_passes
    from pcc.frontends.python.compile_cache import _CODEGEN_ENV_NAMES

    return {"target": target, "runtime": runtime_build_config(),
            "runtime_ir_passes": runtime_ir_passes(str(ROOT / "pcc" / "runtime")),
            "backend": "self", "object_emitter": "pcc", "python_ir_passes": "off",
            "codegen_environment": {name: os.environ.get(name, "") for name in _CODEGEN_ENV_NAMES}}


def shared_stage1_jobs(cpu_budget, rss_limit):
    from pcc.frontends.python.pipeline_frontend_workers import (
        frontend_jobs, HOST_SOURCE_WORKER_AUTO_CAP,
    )

    return frontend_jobs(HOST_SOURCE_WORKER_AUTO_CAP, "auto",
                         min(cpu_budget, os.cpu_count() or 1))


def normalized_image(data):
    if data.startswith(b"\x7fELF"):
        return data  # Owned static ELF has no variable signature/build-id.
    if not data.startswith(b"MZ") or len(data) < 64:
        raise RuntimeError("bootstrap output is not a native ELF/PE image")
    pe = struct.unpack_from("<I", data, 60)[0]
    if pe + 24 + 240 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        raise RuntimeError("malformed PE bootstrap artifact")
    return data


def dependency_receipt(path):
    data = Path(path).read_bytes()
    if data.startswith(b"\x7fELF"):
        from pcc.backend.elf_x86_64 import parse_static_executable
        shape = parse_static_executable(data)
        return {"format": "ELF", "no_dynamic_dependencies": True,
                "proof_scope": "ELF headers; does not identify statically linked source ownership", **shape}
    pe = struct.unpack_from("<I", data, 60)[0]
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if struct.unpack_from("<H", data, optional)[0] != 0x20B:
        raise RuntimeError("expected PE32+")
    ranges = []
    for index in range(count):
        section = optional + optional_size + index * 40
        virtual_size, rva, raw_size, raw = struct.unpack_from("<IIII", data, section + 8)
        ranges.append((rva, raw_size, raw))
    def offset(rva):
        for start, size, raw in ranges:
            if start <= rva < start + size:
                return raw + rva - start
        raise RuntimeError("PE RVA outside file-backed sections")
    import_rva, import_size = struct.unpack_from("<II", data, optional + 112 + 8)
    libraries = []
    if import_rva:
        for index in range(import_size // 20):
            at = offset(import_rva + index * 20)
            descriptor = struct.unpack_from("<IIIII", data, at)
            if descriptor == (0, 0, 0, 0, 0):
                break
            name_at = offset(descriptor[3])
            end = data.find(b"\0", name_at)
            if end < 0:
                raise RuntimeError("unterminated PE import name")
            libraries.append(data[name_at:end].decode("ascii").lower())
    allowed = {"kernel32.dll", "ntdll.dll", "ws2_32.dll", "shell32.dll", "psapi.dll", "bcrypt.dll", "advapi32.dll"}
    if set(libraries) - allowed:
        raise RuntimeError("non-system compiler dependencies: " + repr(libraries))
    return {"format": "PE32+", "system_dlls": sorted(libraries),
            "imports_only_system_dlls": True,
            "proof_scope": "PE import table; does not identify statically linked source ownership"}


def _stage_environment(gc_backend, jobs=1, rss_limit=17179869184):
    """Qualification always completes compilation in the supervised process."""
    environment = os.environ.copy()
    environment.pop("LC_ALL", None)
    environment.pop("PCC_STAGE2_EXPORT_EVIDENCE_DIR", None)
    environment.update({
        "PCC_GC_BACKEND": str(gc_backend), "PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
        "PCC_IR_TO_OBJ_EMITTER": "pcc", "PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py",
        "PCC_RUNTIME_DIR": str(ROOT / "pcc" / "runtime"),
        "PCC_PY_FRONTEND_IR_CACHE": "0", "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
        "PCC_PYTHON_IR_PASSES": "off", "PCC_PY_FRONTEND_JOBS": str(jobs),
        "PCC_SELF_BACKEND_JOBS": str(jobs), "PCC_DEFER_FRONTEND_CODEGEN": "0",
        "PCC_WORKER_TREE_BUDGET_BYTES": str(rss_limit),
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN": "", "PCC_DEFER_FRONTEND_OUTPUT": "",
        "PCC_DEFER_SELF_LINK_PLAN": "",
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1", "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
    })
    return environment


def _runtime_identity_matches(runtime, identity):
    for suffix, key in (("", "sha256"), (".provenance.json", "provenance_sha256"),
                        (".capi_syms", "capi_inventory_sha256")):
        if key in identity and _digest(str(runtime) + suffix) != identity[key]:
            return False
    return True


def _stage_compiler_environment(environment, out_dir, stage, gc_backend):
    stage_env = environment.copy()
    stage_env.pop("PCC_STAGE2_EXPORT_EVIDENCE_DIR", None)
    if stage > 1:
        stage_env["PCC_HOST_PYTHON"] = str(out_dir / "forbidden-host-python")
    if (
        stage == 2
        and str(gc_backend) == "0"
        and str(stage_env.get("PCC_PY_FRONTEND_WORKER_TIMING", "")).strip().lower()
        in ("1", "true", "yes", "on")
    ):
        stage_env["PCC_STAGE2_EXPORT_EVIDENCE_DIR"] = str(out_dir / "stage2-export-evidence")
    return stage_env


def run_chain(gc_backend, out_dir, *, stage_limit=3, timeout=2400, stage1_timeout=None,
              rss_limit=17179869184, allow_dirty=False, shared=None, jobs=1, lock_held=False, cancel=None,
              runtime_archive=None):
    if not lock_held:
        with performance_lock():
            return run_chain(gc_backend, out_dir, stage_limit=stage_limit, timeout=timeout,
                             stage1_timeout=stage1_timeout,
                             rss_limit=rss_limit, allow_dirty=allow_dirty, shared=shared,
                             jobs=jobs, lock_held=True, cancel=cancel, runtime_archive=runtime_archive)
    def guarded(command, **options):
        if cancel is not None:
            if cancel.is_set():
                raise RuntimeError("qualification cancelled after a peer failure")
            options["cancel"] = cancel
        return run(command, **options)
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from pcc.backend.self_backend_targets import resolve_self_backend_target
    target = host_target_triple()
    identity = resolve_self_backend_target(target).identity
    if identity not in ("self-aarch64-linux-v0", "self-x86_64-linux-v0", "self-x86_64-windows-v0"):
        raise RuntimeError("use the existing Darwin reference gate on this host")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Reusing an output directory must not expose an older success receipt
    # after this attempt fails before it can publish its own result.
    for name in ("receipt.json", "failure.json", "progress.json"):
        (out_dir / name).unlink(missing_ok=True)
    source = source_identity(allow_dirty)
    configuration = build_configuration(target)
    environment = _stage_environment(gc_backend, jobs, rss_limit)
    suffix = ".exe" if os.name == "nt" else ""
    records = []
    incomplete_stage = None
    compiler = [sys.executable, "-m", "pcc"]
    canary = out_dir / "canary.py"
    canary.write_text(
        "from pcc.unsafe import gc_backend_current\n"
        "class Box:\n"
        "    def __init__(self, value: int):\n        self.value = value\n"
        "def run() -> int:\n"
        "    box = Box(2 ** 80)\n"
        "    items = [box]\n"
        "    try:\n        raise ValueError('probe')\n"
        "    except ValueError:\n        return items[0].value + 42\n"
        "print(gc_backend_current())\nprint(run())\n", encoding="utf-8")
    with (contextlib.nullcontext() if lock_held else performance_lock()):
        try:
            if shared is None:
                if runtime_archive is not None:
                    from pcc.frontends.python.owned_runtime_build import ensure_target_runtime

                    # Explicit reuse is fail-closed: the existing owner checks
                    # target/config, every member/source, codegen and CAPI bytes.
                    # An invalid explicit archive never falls back to rebuilding.
                    runtime = Path(ensure_target_runtime(
                        str(ROOT / "pcc" / "runtime"), target,
                        explicit_archive=str(runtime_archive),
                    ))
                else:
                    runtime = out_dir / "runtime" / "libpy_runtime_pcc_py.a"
                    runtime_env = environment.copy()
                    runtime_env.pop("PCC_RUNTIME_ARCHIVE", None)
                    # The runtime builder locally masks executable/direct emission
                    # flags for each library-IR request, restoring them afterward.
                    guarded([sys.executable, "-m", "pcc.frontends.python.owned_runtime_build", "--target", target,
                         "--output", str(runtime)], cwd=ROOT, env=runtime_env,
                        log_path=out_dir / "runtime-build.log", timeout=timeout,
                        rss_limit=rss_limit)
                runtime_identity = {"path": str(runtime), "sha256": _digest(runtime),
                                    "provenance_sha256": _digest(str(runtime) + ".provenance.json")}
                if runtime_archive is not None:
                    runtime_identity["capi_inventory_sha256"] = _digest(str(runtime) + ".capi_syms")
            else:
                if shared["source"] != source or shared["target"] != target or shared.get("configuration") != configuration:
                    raise RuntimeError("shared Stage1 source or target identity differs")
                runtime_identity = dict(shared["runtime"])
                runtime = Path(runtime_identity["path"])
                if not _runtime_identity_matches(runtime, runtime_identity):
                    raise RuntimeError("shared runtime identity changed")
                compiler = [shared["compiler_path"]]
                if _digest(compiler[0]) != shared["stages"][0]["sha256"]:
                    raise RuntimeError("shared Stage1 identity changed")
            environment["PCC_RUNTIME_ARCHIVE"] = str(runtime)
            for stage in range(1, stage_limit + 1):
                if source_identity(allow_dirty) != source:
                    raise RuntimeError("source changed before stage " + str(stage))
                if not _runtime_identity_matches(runtime, runtime_identity):
                    raise RuntimeError("runtime archive changed before stage " + str(stage))
                compiler_digest = _digest(compiler[0])
                output = out_dir / ("pcc" + str(stage) + suffix)
                stage_env = _stage_compiler_environment(environment, out_dir, stage, gc_backend)
                command = compiler + ["--backend", "self", "--python-libpython", "off",
                                      "--ir-scaffold=on", str(ROOT / "pcc" / "__main__.py"), "-o", str(output)]
                print(f"GC{gc_backend} stage{stage}: {' '.join(command)}", flush=True)
                if shared is not None and stage == 1:
                    output = Path(shared["compiler_path"])
                    record = dict(shared["stages"][0])
                    record["reused"] = True
                    record["shared_path"] = str(output)
                    record["shared_build_seconds"] = record["seconds"]
                    record["seconds"] = 0
                    record["peak_tree_rss"] = 0
                else:
                    profile_directory = None
                    profile_fresh = False
                    profile_installed = False
                    arm_profile = stage_env.get("PCC_HOST_ARM_BACKEND_PROFILE") == "1"
                    elf_profile = stage_env.get("PCC_HOST_ELF_STAGE_PROFILE") == "1"
                    if stage == 1 and stage_env.get("PCC_HOST_ASSEMBLY_PROFILE") == "1":
                        profile_directory = out_dir / "assembly-function-profile"
                    elif stage == 1 and elf_profile:
                        profile_directory = out_dir / "elf-stage-function-profile"
                    elif stage == 1 and arm_profile:
                        profile_directory = out_dir / "arm-backend-function-profile"
                    if profile_directory is not None:
                        from scripts.host_assembly_profile import prepare_environment

                        profile_fresh = not profile_directory.exists()
                        profiled_env = prepare_environment(
                            ROOT, profile_directory, stage_env, source,
                            runtime_identity, compiler_digest, target, command=command,
                        )
                        profile_installed = profiled_env is not stage_env
                        stage_env = profiled_env
                    output.unlink(missing_ok=True)
                    record = guarded(command, cwd=ROOT, env=stage_env, log_path=out_dir / f"stage{stage}.log",
                                 timeout=stage1_timeout if stage == 1 and stage1_timeout is not None else timeout,
                                 rss_limit=rss_limit, native=stage > 1)
                    if profile_directory is not None:
                        # A killed worker may have claimed the sole attempt but
                        # left no terminal stats. Never call this baseline data.
                        record["arm_backend_function_profile" if arm_profile else
                               "elf_stage_function_profile" if elf_profile else "assembly_function_profile"] = {
                            "scope": "profiled overhead, not baseline performance",
                            "installation_applied": profile_installed,
                            "output_directory_preexisted": not profile_fresh,
                            "installation_status_present": profile_fresh and (profile_directory / "install-status.json").is_file(),
                            "claimed": profile_installed and (profile_directory / "claim.json").is_file(),
                            "result_present": profile_installed and (profile_directory / "result.json").is_file(),
                            "strong_kill_may_leave_no_stats": True,
                        }
                    if not output.is_file() or output.stat().st_size == 0:
                        raise RuntimeError("stage exited successfully without producing a compiler")
                if source_identity(allow_dirty) != source:
                    raise RuntimeError("source changed during stage " + str(stage))
                record.update({"stage": stage, "sha256": _digest(output),
                               "input_compiler_sha256": compiler_digest,
                               "dependencies": dependency_receipt(output)})
                if _digest(compiler[0]) != compiler_digest:
                    raise RuntimeError("input compiler changed during stage " + str(stage))
                # Preserve completed compilation even when the first native
                # launch fails. Keep stages/progress reserved for validated
                # canaries, and never borrow a reused compiler's old canary.
                incomplete_stage = dict(record, canary_status="pending",
                                        image=_image_header_receipt(output))
                incomplete_stage.pop("canary", None)
                compiler = [str(output)]
                executable = out_dir / (f"canary{stage}" + suffix)
                executable.unlink(missing_ok=True)
                smoke_env = environment.copy()
                smoke_env["PCC_HOST_PYTHON"] = str(out_dir / "forbidden-host-python")
                # A failing native worker otherwise reports only "Type: message".
                # Re-raising after its report lets the process handler add the
                # file and line to the canary log; success is unaffected.
                smoke_env["PCC_DEBUG_WORKER_RERAISE"] = "1"
                canary_compile = guarded(
                    compiler + [str(canary), "-o", str(executable), "--backend", "self", "--python-libpython", "off"],
                    cwd=ROOT, env=smoke_env, log_path=out_dir / f"canary{stage}-compile.log",
                    timeout=timeout, rss_limit=rss_limit, native=True)
                if not executable.is_file() or executable.stat().st_size == 0:
                    raise RuntimeError("canary compile exited successfully without producing an executable")
                log = out_dir / f"canary{stage}-run.log"
                canary_run = guarded([str(executable)], cwd=out_dir, env=smoke_env, log_path=log,
                                 timeout=60, rss_limit=rss_limit, native=True)
                expected = f"{gc_backend}\n{2 ** 80 + 42}\n"
                if log.read_text(encoding="utf-8").replace("\r\n", "\n") != expected:
                    raise RuntimeError("native GC/function/object/integer/exception canary failed")
                if source_identity(allow_dirty) != source:
                    raise RuntimeError("source changed during native canary " + str(stage))
                if not _runtime_identity_matches(runtime, runtime_identity):
                    raise RuntimeError("runtime archive/provenance changed during stage " + str(stage))
                if _digest(output) != record["sha256"]:
                    raise RuntimeError("stage compiler changed during native canary " + str(stage))
                record["canary"] = {"source_sha256": _digest(canary),
                                    "executable_sha256": _digest(executable),
                                    "dependencies": dependency_receipt(executable),
                                    "compile": canary_compile, "run": canary_run,
                                    "output_sha256": _digest(log)}
                records.append(record)
                incomplete_stage = None
                (out_dir / "progress.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
            fixed = False
            if stage_limit == 3:
                second = normalized_image((out_dir / ("pcc2" + suffix)).read_bytes())
                third = normalized_image((out_dir / ("pcc3" + suffix)).read_bytes())
                fixed = second == third
                if not fixed:
                    raise RuntimeError("pcc2/pcc3 bytes differ; classify drift before qualification")
            receipt = {"schema": "pcc.platform-bootstrap.v1", "target": target, "gc": str(gc_backend),
                       "source": source, "runtime": runtime_identity, "configuration": configuration,
                       "compiler_path": str(out_dir / ("pcc1" + suffix)) if shared is None else shared["compiler_path"],
                       "worker_jobs": jobs, "tree_rss_limit": rss_limit,
                       "stages": records, "fixed_point": fixed,
                       "qualification_scope": "source-stable self-host fixed point and native GC/object/integer/exception canary",
                       "execution_owner_evidence": {
                           "method": "process-tree executable sampling and artifact dependency headers",
                           "sampling_interval_seconds": 0.2,
                           "exhaustive_subprocess_trace": False,
                           "limitation": "processes that start and exit between samples may not be observed",
                       },
                       "qualified": fixed and source["clean_commit"]}
            (out_dir / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            return receipt
        except BaseException as exc:
            (out_dir / "failure.json").write_text(json.dumps({"source": source, "stages": records,
                                                              "incomplete_stage": incomplete_stage,
                                                              "error": str(exc)}, indent=2), encoding="utf-8")
            raise


@contextlib.contextmanager
def _same_run_admission(common, run_id, timeout):
    """Queue integration peers before they request the global build lock."""
    if run_id is None:
        yield
        return
    from scripts.file_lock import exclusive_file_lock

    key = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
    lock = Path(common) / (".integration-run-" + key + ".lock")
    deadline = time.monotonic() + timeout
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(exclusive_file_lock(lock, blocking=False))
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for a peer in this integration run") from exc
                time.sleep(0.1)
        yield


def run_from_shared(gc_backend, out_dir, *, stage_limit=3, timeout=2400,
                    rss_limit=17179869184, allow_dirty=False, run_id=None):
    """Reuse a verified common input for separately selected integration gates."""
    from pcc.frontends.python.pipeline_targets import host_target_triple

    out_dir = Path(out_dir).resolve()
    common = out_dir.parent / "shared"
    # Independently scheduled xdist peers can wait for this run's admitted
    # chain. A different run still meets the nonblocking global build lock.
    with _same_run_admission(common, run_id, timeout * 12 + 300), performance_lock():
        shared = None
        try:
            candidate = json.loads((common / "receipt.json").read_text(encoding="utf-8"))
            if candidate["source"] == source_identity(allow_dirty) and candidate.get("configuration") == build_configuration(host_target_triple()):
                if _digest(candidate["compiler_path"]) == candidate["stages"][0]["sha256"]:
                    shared = candidate
        except (OSError, ValueError, KeyError, IndexError):
            pass
        if shared is None:
            shared = run_chain("0", common, stage_limit=1, timeout=timeout, rss_limit=rss_limit,
                               allow_dirty=allow_dirty, jobs=shared_stage1_jobs(4, rss_limit), lock_held=True)
        return run_chain(gc_backend, out_dir, stage_limit=stage_limit, timeout=timeout,
                         rss_limit=rss_limit, allow_dirty=allow_dirty, shared=shared,
                         jobs=2, lock_held=True)


def run_matrix(backends, out_dir, *, stage_limit=3, timeout=2400, stage1_timeout=None,
               rss_limit=17179869184, allow_dirty=False, cpu_budget=4, lock_held=False,
               runtime_archive=None):
    """One immutable runtime/Stage1, then resource-bounded native chains."""
    out_dir = Path(out_dir).resolve()
    slots = min(len(backends), 2, max(1, cpu_budget // 2), max(1, rss_limit // (4 * 1024 ** 3)))
    jobs = max(1, min(2, cpu_budget // max(1, slots)))
    chain_rss = rss_limit // max(1, slots)
    with (contextlib.nullcontext() if lock_held else performance_lock()):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "matrix-receipt.json").unlink(missing_ok=True)
        shared = run_chain("0", out_dir / "shared", stage_limit=1, timeout=timeout,
                           stage1_timeout=stage1_timeout,
                           rss_limit=rss_limit, allow_dirty=allow_dirty,
                           jobs=shared_stage1_jobs(cpu_budget, rss_limit), lock_held=True,
                           runtime_archive=runtime_archive)
        cancelled = threading.Event()
        def execute(backend):
            if cancelled.is_set():
                raise RuntimeError("qualification cancelled after a peer failure")
            return run_chain(backend, out_dir / ("gc" + backend), stage_limit=stage_limit,
                             timeout=timeout, stage1_timeout=stage1_timeout,
                             rss_limit=chain_rss, allow_dirty=allow_dirty,
                             shared=shared, jobs=jobs, lock_held=True, cancel=cancelled)
        pool = ThreadPoolExecutor(max_workers=slots)
        futures = {pool.submit(execute, backend): backend for backend in backends}
        results = {}
        try:
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        except BaseException:
            cancelled.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        receipts = [results[backend] for backend in backends]
        if source_identity(allow_dirty) != shared["source"]:
            raise RuntimeError("source changed during five-GC matrix")
        receipt = {"schema": "pcc.platform-bootstrap-matrix.v1", "source": shared["source"],
                   "shared": shared, "gc_backends": list(backends), "chains": receipts,
                   "parallel_chains": slots, "jobs_per_chain": jobs,
                   "aggregate_cpu_budget": cpu_budget, "aggregate_rss_limit": rss_limit,
                   "qualified": all(item["qualified"] for item in receipts)}
        (out_dir / "matrix-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--gc", default="all", choices=("all", "0", "1", "2", "3", "4"))
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--stage", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--timeout", type=int, default=2400,
                        help="per-compile deadline, including runtime, later stages and native canaries")
    parser.add_argument("--stage1-timeout", type=int,
                        help="host Stage1 build deadline only (default: --timeout)")
    parser.add_argument("--rss-limit", type=int, default=17179869184)
    parser.add_argument("--cpu-budget", type=int, default=4)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--runtime-archive", help="Explicit source-matched archive; verify deeply and never rebuild")
    parser.add_argument("--inside-matrix", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lock-held", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.timeout <= 0 or (args.stage1_timeout is not None and args.stage1_timeout <= 0)
            or args.rss_limit <= 0 or args.cpu_budget <= 0):
        parser.error("timeout, CPU budget and RSS limit must be positive")
    backends = ("0", "1", "2", "3", "4") if args.gc == "all" else (args.gc,)
    if not args.inside_matrix:
        with performance_lock():
            out_dir = Path(args.out_dir).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            child_args = list(sys.argv[1:] if argv is None else argv)
            command = [sys.executable, str(Path(__file__).resolve()), *child_args, "--inside-matrix", "--lock-held"]
            rounds = (len(backends) + 1) // 2
            extra_stage1_time = max(0, args.stage1_timeout - args.timeout) if args.stage1_timeout is not None else 0
            return run(command, cwd=ROOT, env=os.environ.copy(), log_path=out_dir / "matrix.log",
                       timeout=args.timeout * (2 + 2 * rounds) + extra_stage1_time + 300,
                       rss_limit=args.rss_limit)
    return run_matrix(backends, args.out_dir, stage_limit=args.stage, timeout=args.timeout,
                      stage1_timeout=args.stage1_timeout,
                      rss_limit=args.rss_limit, allow_dirty=args.allow_dirty, cpu_budget=args.cpu_budget,
                      lock_held=args.lock_held, runtime_archive=args.runtime_archive)


if __name__ == "__main__":
    main()
