"""Build a target runtime in process using pcc's frontend, emitter and ar.

The checked-in module inventory remains the Makefile's two module variables;
Make is not executed. Explicit target selection reaches both frontend ABI
lowering and object emission. Existing provenance receipts remain authoritative.
"""

import os
import json
from pathlib import Path
from pcc.backend.self_backend_target_match import target_os_name
from pcc.driver.native_provisioning import require_native_provisioning_allowed
from pcc.frontends.python.worker_process_pool import run_worker_processes
from pcc.frontends.python.thread_runtime_config import reject_single_thread_runtime_request


# Runtime construction requests library IR, even when its caller is building
# an executable through the direct or deferred frontend routes.
_RUNTIME_IR_OUTPUT_ENV = (
    "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
    "PCC_DIRECT_INDEXED_KERNEL_EMIT",
    "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
    "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES",
    "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
    "PCC_DIRECT_INDEXED_KERNEL_VALIDATE",
    "PCC_DIRECT_INDEXED_SIDECAR",
    "PCC_TEXT_INDEXED_KERNEL_EMIT",
    "PCC_DEFER_FRONTEND_CODEGEN",
    "PCC_DEFER_FRONTEND_CODEGEN_PLAN",
    "PCC_DEFER_FRONTEND_OUTPUT",
    "PCC_DEFER_SELF_LINK_PLAN",
)


def runtime_build_config() -> dict:
    # Every runtime is threaded; the single-thread kernel was removed.
    reject_single_thread_runtime_request()
    threads = True
    refcount = str(os.environ.get("PCC_REFCOUNT_KIND", "") or "").strip().lower() or "atomic"
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in refcount):
        raise ValueError("invalid refcount configuration for runtime cache")
    return {"threads": threads, "refcount": refcount}


# The Makefile compiles these modules with automatic safepoint polls off
# (PCC_IMPLICIT_THREAD_SAFEPOINTS=0) and the kernel without the runtime pass
# list; the owned builder mirrors both settings.
_THREAD_KERNEL_MODULE = "freestanding_thread_kernel_pthread"
_NO_IMPLICIT_POLL_MODULES = (
    _THREAD_KERNEL_MODULE, "py_runtime_log", "py_tempfile", "py_os_mkdir",
    "py_cleanup_runtime", "py_exc_traceback",
)


def runtime_ir_passes(runtime_dir: str) -> str:
    """The Makefile's PCC_RUNTIME_IR_PASSES; like its ``?=``, the environment wins."""
    explicit = str(os.environ.get("PCC_RUNTIME_IR_PASSES", "") or "").strip()
    if explicit:
        return explicit
    with open(os.path.join(runtime_dir, "Makefile"), encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("PCC_RUNTIME_IR_PASSES ?="):
                return line.split("=", 1)[1].strip()
    raise ValueError("runtime Makefile does not define PCC_RUNTIME_IR_PASSES")


def runtime_modules(runtime_dir: str, target: str, threads: bool = True) -> list[str]:
    names = []
    with open(os.path.join(runtime_dir, "Makefile"), encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(("PY_MODULES =", "PY_MODULES +=", "FREESTANDING_PY_MODULES =", "FREESTANDING_PY_MODULES +=")):
                for name in line.split("=", 1)[1].split():
                    if name not in names:
                        names.append(name)
    # The threaded kernel is the only kernel; ``threads`` stays a
    # parameter for receipt compatibility and must be true.
    if not threads:
        raise ValueError("the single-thread runtime kernel was removed")
    names = [name for name in names if name != "freestanding_thread_kernel_pthread"]
    names.append("freestanding_thread_kernel_pthread")
    if target_os_name(target) == "win32":
        names = [name for name in names if name != "freestanding_platform_rss"]
        names.append("freestanding_windows_rss")
        names.extend(("freestanding_windows", "freestanding_windows_process", "freestanding_windows_start", "freestanding_windows_socket", "freestanding_windows_system", "freestanding_windows_file_lock", "freestanding_windows_time", "freestanding_time_format", "freestanding_windows_directory", "freestanding_windows_signal", "freestanding_signal_set"))
        if threads:
            names.append("freestanding_windows_threads")
    elif target_os_name(target) == "linux":
        if threads:
            names.append("freestanding_linux_threads")
        names.append("freestanding_c_linux_start")
        names.extend(("freestanding_linux_libc", "freestanding_linux_time", "freestanding_time_format", "freestanding_posix_timezone", "freestanding_linux_directory", "freestanding_linux_signal", "freestanding_linux_static_loader", "freestanding_signal_set"))
        if target.startswith(("arm64-", "aarch64-")):
            names.append("freestanding_linux_aarch64")
    return names


def _compile_runtime_module(name, source, ir_path, target):
    from pcc.driver.paths import module_name_from_src
    from pcc.frontends.python.codegen.debug_info_lowering import debug_info_requested
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_pass_config import (
        python_ir_pass_should_skip_module,
        resolve_python_ir_pass_names,
    )
    from pcc.frontends.python.pipeline_runtime_archive import runtime_python_ir_pass_mode

    # Match the Makefile: implicit kernel polls recurse before initialization;
    # implicit logger polls can park while its sink lock is held. Explicit
    # unsafe.thread_safepoint calls in lock-free wait loops remain real calls.
    suppress_polls = name in _NO_IMPLICIT_POLL_MODULES
    names = list(_RUNTIME_IR_OUTPUT_ENV)
    # Match run_runtime_make: the shared archive has its own pass/cache
    # policy. An application's -g must not alter its dependency build, nor
    # feed source locations to runtime-only transforms without debug support.
    names.append("PCC_PY_DEBUG_INFO")
    names.append("PCC_PYTHON_IR_PASSES")
    names.append("PCC_RUNTIME_DIR")
    if suppress_polls:
        names.append("PCC_IMPLICIT_THREAD_SAFEPOINTS")
    previous = {key: os.environ.get(key) for key in names}
    try:
        for key in _RUNTIME_IR_OUTPUT_ENV:
            os.environ.pop(key, None)
        os.environ.pop("PCC_PY_DEBUG_INFO", None)
        # The explicit build input owns its ABI-constant context, even when
        # copied outside the compiler checkout or named simply runtime/py.
        # Keep this scoped to the call so a previous runtime selection cannot
        # leak into a later build (and restore it on a failed compile too).
        os.environ["PCC_RUNTIME_DIR"] = os.path.dirname(os.path.dirname(os.path.abspath(source)))
        mode = runtime_python_ir_pass_mode()
        os.environ["PCC_PYTHON_IR_PASSES"] = mode
        # Only this exact owned tier can supply a prefix-elision proof below.
        # Use the frontend's source-derived name, including package prefixes.
        frontend_prefix = ""
        if (
            mode.lower() == "default"
            and resolve_python_ir_pass_names(raw=mode) == ["mem2reg", "sroa"]
            and not debug_info_requested()
            and not python_ir_pass_should_skip_module(module_name_from_src(source))
        ):
            frontend_prefix = "mem2reg,sroa"
        if suppress_polls:
            os.environ["PCC_IMPLICIT_THREAD_SAFEPOINTS"] = "0"
        compile_python(source, ir_path, emit_llvm_only=True, python_library=True,
                       libpython_mode="off", backend="self", target_triple=target)
        return frontend_prefix
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _runtime_passes_after_frontend(passes, frontend_prefix, ir_text):
    """Elide only a proven no-op repeat of the frontend's owned default tier."""
    from pcc.frontends.python.compiled_default_passes import _has_py_cpy_call

    prefix = "mem2reg,sroa"
    if (
        frontend_prefix != prefix
        or str(os.environ.get("PCC_RUNTIME_IR_PASSES", "") or "").strip()
        or "alloca " in ir_text
        or _has_py_cpy_call(ir_text)
    ):
        return passes
    # Running this pair once does not prove a fixed point: removing pointer
    # spills can expose new promotion candidates for a second round. With no
    # alloca token accepted by either parser, both the CFG pass and its textual
    # cleanup are unchanged. Include its space, not names such as allocator.
    # The strict frontend also returns early on py_cpy calls, so exclude those.
    if passes == prefix:
        return None
    if passes.startswith(prefix + ",") and len(passes) > len(prefix) + 1:
        return passes[len(prefix) + 1:]
    return passes


def runtime_build_jobs(module_count: int) -> int:
    """Worker count for a runtime build: PCC_RUNTIME_BUILD_JOBS, else up to 8 cores."""
    raw = str(os.environ.get("PCC_RUNTIME_BUILD_JOBS", "") or "").strip()
    if raw:
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError("PCC_RUNTIME_BUILD_JOBS must be a positive integer")
        jobs = int(raw)
    else:
        jobs = min(8, os.cpu_count() or 1)
    return max(1, min(jobs, module_count))


def _build_runtime_module(runtime_dir, name, target, output_dir, passes, config) -> bytes:
    """Compile, optimize and emit one runtime module; write its object and receipt."""
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.ir.optimization.driver import optimize_ir
    from pcc.tools.runtime_archive_provenance import write_pcc_python_receipt

    source = os.path.join(runtime_dir, "py", name + ".py")
    ir_path = os.path.join(output_dir, name + ".ll")
    object_path = os.path.join(output_dir, name + ".o")
    frontend_prefix = _compile_runtime_module(name, source, ir_path, target)
    with open(ir_path, encoding="utf-8") as stream:
        ir_text = stream.read()
    # As in the Makefile rule, the object and its receipt describe the
    # optimized IR: precise stack maps expect promoted frame-slot
    # locals, and unoptimized runtime code is not what Darwin ships.
    if name != _THREAD_KERNEL_MODULE:
        remaining_passes = _runtime_passes_after_frontend(passes, frontend_prefix, ir_text)
        if remaining_passes is not None:
            ir_text = optimize_ir(ir_text, remaining_passes)
        with open(ir_path, "w", encoding="utf-8") as stream:
            stream.write(ir_text)
    data = emit_owned_object(ir_text, target)
    with open(object_path, "wb") as stream:
        stream.write(data)
    write_pcc_python_receipt(object_path=Path(object_path), ir_path=Path(ir_path),
                             source_path=Path(source), runtime_root=Path(runtime_dir),
                             target_triple=target, object_emitter="pcc", object_bytes=data,
                             runtime_build_config=config)
    return data


RUNTIME_MODULE_WORKER_ARG = "--pcc-runtime-module-worker"


def run_runtime_module_worker(manifest_path: str) -> int:
    """Worker entry: build every module a manifest names, in order."""
    with open(manifest_path, encoding="utf-8") as stream:
        manifest = json.loads(stream.read())
    config = runtime_build_config()
    for name in manifest["names"]:
        _build_runtime_module(manifest["runtime_dir"], name, manifest["target"],
                              manifest["output_dir"], manifest["passes"], config)
    return 0


def _build_runtime_modules_in_workers(runtime_dir, names, target, output_dir, passes, jobs):
    """Compile *names* in worker batches; a failed worker fails the build."""
    from pcc.frontends.python.pipeline import _python_frontend_worker_command_prefix, _shell_quote_arg

    prefix = _python_frontend_worker_command_prefix()
    if not prefix:
        raise ValueError("runtime build workers have no compiler executable")
    # Largest sources first, dealt across more batches than workers, so the
    # pool keeps every worker busy while one module is still compiling.
    ordered = sorted(names, key=lambda name: -_runtime_source_size(runtime_dir, name))
    batch_count = min(len(ordered), jobs * 4)
    batches = [ordered[index::batch_count] for index in range(batch_count)]
    # A worker that never wrote its object must not let a previous build's
    # object stand in for it.
    for name in names:
        stale = os.path.join(output_dir, name + ".o")
        if os.path.exists(stale):
            os.remove(stale)
    commands = []
    manifests = []
    for index, batch in enumerate(batches):
        manifest = os.path.join(output_dir, "worker-" + str(index) + ".json")
        manifests.append(manifest)
        with open(manifest, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "runtime_dir": runtime_dir, "target": target, "output_dir": output_dir,
                "passes": passes, "names": batch,
            }))
        argv = list(prefix) + [RUNTIME_MODULE_WORKER_ARG, manifest]
        commands.append(" ".join(_shell_quote_arg(arg) for arg in argv))
    run_worker_processes(commands, jobs)
    for manifest in manifests:
        os.remove(manifest)


def _runtime_source_size(runtime_dir, name) -> int:
    source = os.path.join(runtime_dir, "py", name + ".py")
    return os.path.getsize(source) if os.path.isfile(source) else 0


def build_runtime_archive(runtime_dir: str, archive: str, target: str) -> None:
    require_native_provisioning_allowed()
    from pcc.frontends.python.pipeline_runtime_archive import _acquire_runtime_build_lock, _remove_runtime_build_lock
    from pcc.backend.ar_writer import write_archive, _defined_symbols
    from pcc.tools.runtime_archive_provenance import (
        assemble_runtime_archive_manifest, codegen_checksum,
    )
    from pcc.backend.self_backend_targets import resolve_self_backend_target
    resolve_self_backend_target(target)
    if target_os_name(target) not in ("darwin", "linux", "win32"):
        raise ValueError("owned platform runtime builder target is unsupported: " + target)
    # Production objects cannot be consumed with an unknown compiler identity.
    # Validate the complete source closure before creating outputs or locking:
    # a partial source copy must fail here, not after building the archive.
    compiler_identity = codegen_checksum()
    if (
        not isinstance(compiler_identity, str)
        or len(compiler_identity) != 64
        or any(character not in "0123456789abcdef" for character in compiler_identity)
    ):
        raise ValueError("runtime build compiler identity is unavailable; provide the complete compiler source closure")
    config = runtime_build_config()
    threads = config["threads"]
    passes = runtime_ir_passes(runtime_dir)
    output_dir = archive + ".objects"
    os.makedirs(output_dir, exist_ok=True)
    lock = os.path.join(runtime_dir, ".pcc-runtime-build.lock")
    _acquire_runtime_build_lock(lock)
    try:
        members = []
        objects = []
        capi = set()
        names = runtime_modules(runtime_dir, target, threads)
        jobs = runtime_build_jobs(len(names))
        if jobs > 1:
            _build_runtime_modules_in_workers(runtime_dir, names, target, output_dir, passes, jobs)
        for name in names:
            object_path = os.path.join(output_dir, name + ".o")
            if jobs > 1:
                with open(object_path, "rb") as stream:
                    data = stream.read()
            else:
                data = _build_runtime_module(runtime_dir, name, target, output_dir, passes, config)
            members.append((name + ".o", data))
            objects.append(Path(object_path))
            _kind, definitions = _defined_symbols(data)
            for symbol in definitions:
                bare = symbol[1:] if symbol.startswith("_") else symbol
                if bare.startswith(("Py", "_Py")):
                    capi.add(symbol)
        temporary = archive + ".tmp"
        with open(temporary, "wb") as stream:
            stream.write(write_archive(members))
        capi_path = temporary + ".capi_syms"
        # Provenance hashes canonical ASCII/LF bytes, independent of host
        # text-mode newline translation (in particular Windows CRLF).
        with open(capi_path, "wb") as stream:
            stream.write(("\n".join(sorted(capi)) + "\n").encode("ascii"))
        assemble_runtime_archive_manifest(Path(temporary), objects, runtime_root=Path(runtime_dir),
                                           capi_inventory_path=Path(capi_path))
        os.replace(capi_path, archive + ".capi_syms")
        os.replace(temporary, archive)
        # Publish the completed receipt after its payloads, as the Makefile's
        # archive builder does. Interrupted publication cannot bless old data.
        os.replace(temporary + ".provenance.json", archive + ".provenance.json")
    finally:
        _remove_runtime_build_lock(lock, os.path.join(lock, "owner"))


def _manifest_configuration_mismatch_reason(
    receipt: dict, runtime_dir: str, target: str, config: dict,
) -> str:
    if not isinstance(receipt, dict):
        return "runtime manifest is not an object"
    if receipt.get("target_triple") != target:
        return "target triple differs"
    members = receipt.get("members")
    if not isinstance(members, list) or not members:
        return "member inventory is missing or invalid"
    expected = {name + ".o" for name in runtime_modules(runtime_dir, target, config["threads"])}
    if len(members) != len(expected):
        return "member inventory count differs"
    actual = set()
    for member in members:
        if not isinstance(member, dict):
            return "member inventory contains an invalid record"
        if member.get("runtime_build_config") != config:
            return "runtime build configuration differs"
        actual.add(member.get("member"))
    if actual != expected:
        return "member inventory differs"
    return ""


def _manifest_matches_config(receipt: dict, runtime_dir: str, target: str, config: dict) -> bool:
    return not _manifest_configuration_mismatch_reason(receipt, runtime_dir, target, config)


def ensure_target_runtime(runtime_dir: str, target: str, *, packaged_archive: str = "",
                          wheel_matches=None, explicit_archive: str | None = None) -> str:
    """Reuse only a source/codegen/target/configuration-matched owned archive."""
    from pcc.backend.self_backend_targets import resolve_self_backend_target
    from pcc.tools.runtime_archive_provenance import (
        verify_runtime_archive_manifest, manifest_is_stale_for_current_codegen,
        manifest_codegen_staleness_reason,
    )
    resolve_self_backend_target(target)
    if target_os_name(target) not in ("darwin", "linux", "win32"):
        raise ValueError("owned platform runtime builder target is unsupported: " + target)
    override = str(os.environ.get("PCC_RUNTIME_DIR", "") or "").strip()
    if override:
        runtime_dir = os.path.abspath(override)
        if packaged_archive:
            packaged_archive = os.path.join(runtime_dir, os.path.basename(packaged_archive))
    if not os.path.isdir(runtime_dir):
        raise ValueError("runtime directory not found: " + runtime_dir)
    config = runtime_build_config()
    threads = config["threads"]
    refcount = config["refcount"]
    output = os.path.join(runtime_dir, "build_owned", target,
                          ("threads-" if threads else "single-") + refcount,
                          "libpy_runtime_pcc_py.a")
    explicit = (str(os.environ.get("PCC_RUNTIME_ARCHIVE", "") or "").strip()
                if explicit_archive is None else str(explicit_archive).strip())
    if explicit_archive is not None and not explicit:
        raise ValueError("explicit runtime archive path is empty")
    candidates = [os.path.abspath(explicit)] if explicit else [packaged_archive, output]
    for candidate in candidates:
        if not candidate:
            continue
        if not os.path.isfile(candidate):
            if explicit:
                raise ValueError("explicit runtime archive is missing: " + candidate)
            continue
        try:
            # A wheel's stamp binds archive, manifest and inventory bytes.
            # Its source/codegen need not match the consumer's checkout, but
            # target and build configuration must still match the request.
            installed = wheel_matches is not None and wheel_matches(candidate)
            if installed:
                with open(candidate + ".provenance.json", encoding="utf-8") as stream:
                    receipt = json.loads(stream.read())
            else:
                receipt = verify_runtime_archive_manifest(Path(candidate), runtime_root=Path(runtime_dir))
            mismatch = _manifest_configuration_mismatch_reason(receipt, runtime_dir, target, config)
            if not mismatch and not installed:
                mismatch = manifest_codegen_staleness_reason(receipt)
            if not mismatch:
                return candidate
        except (ValueError, OSError, KeyError, TypeError):
            if explicit:
                raise
        if explicit:
            raise ValueError(
                "explicit runtime archive does not match the target/source configuration: " + mismatch
            )
    require_native_provisioning_allowed()
    os.makedirs(os.path.dirname(output), exist_ok=True)
    build_runtime_archive(runtime_dir, output, target)
    receipt = verify_runtime_archive_manifest(Path(output), runtime_root=Path(runtime_dir))
    if not _manifest_matches_config(receipt, runtime_dir, target, config):
        raise ValueError("owned runtime build produced a mismatched target/configuration")
    if manifest_is_stale_for_current_codegen(receipt):
        raise ValueError("owned runtime build produced stale codegen provenance")
    return output


def main(argv=None):
    import argparse
    from pcc.frontends.python.pipeline_targets import host_target_triple
    parser = argparse.ArgumentParser(description="Build pcc's owned target runtime")
    parser.add_argument("--target", default=host_target_triple())
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-dir", default=str(Path(__file__).resolve().parents[2] / "runtime"))
    args = parser.parse_args(argv)
    require_native_provisioning_allowed()
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    build_runtime_archive(args.runtime_dir, os.path.abspath(args.output), args.target)


if __name__ == "__main__":
    main()
