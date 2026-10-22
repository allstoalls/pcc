"""Runtime-archive bundle validation and freshness policy."""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Optional

from pcc.driver.paths import join_strings
from pcc.driver.native_provisioning import (
    NativeProvisioningError,
    require_native_provisioning_allowed,
)


RUNTIME_PYTHON_IR_PASSES_ENV = "PCC_RUNTIME_PYTHON_IR_PASSES"
RUNTIME_BUILD_ENV = "PCC_RUNTIME_BUILD"


def runtime_python_ir_pass_mode() -> str:
    value = str(os.environ.get(RUNTIME_PYTHON_IR_PASSES_ENV, "") or "").strip()
    return value or "default"


class RuntimeArchiveError(RuntimeError):
    """Runtime archive selection or construction failed closed."""


def runtime_build_mode() -> str:
    """Make is available only as an explicitly selected host reference."""
    value = str(os.environ.get(RUNTIME_BUILD_ENV, "") or "").strip().lower() or "owned"
    if value not in ("owned", "make"):
        raise RuntimeArchiveError("invalid PCC_RUNTIME_BUILD value " + repr(value) + "; expected 'owned' or 'make'")
    if value == "make":
        if sys.implementation.name != "cpython":
            raise RuntimeArchiveError("PCC_RUNTIME_BUILD=make is a CPython-host reference oracle; native pcc1 requires the owned runtime builder")
        sys.stderr.write("pcc: explicit host runtime reference oracle PCC_RUNTIME_BUILD=make (external Make/tool commands; not owned construction)\n")
    return value


def compiler_sources_newer_than(
    pcc_dir: str,
    archive_base: str,
    archive_mtime: float,
) -> bool:
    if archive_base not in (
        "libpy_runtime_pcc_py.a",
        "libpy_runtime_pcc_py_libpython.a",
    ):
        return False
    roots = (
        os.path.join(pcc_dir, "backend"),
        os.path.join(pcc_dir, "frontends"),
        os.path.join(pcc_dir, "driver"),
        os.path.join(pcc_dir, "ir"),
        os.path.join(pcc_dir, "tools"),
        os.path.join(pcc_dir, "__main__.py"),
        os.path.join(pcc_dir, "api.py"),
        os.path.join(pcc_dir, "pcc.py"),
    )
    for root in roots:
        if os.path.isfile(root):
            if os.path.getmtime(root) > archive_mtime:
                return True
            continue
        if not os.path.isdir(root):
            continue
        pending_dirs = [root]
        while pending_dirs:
            dirpath = pending_dirs.pop()
            try:
                names = os.listdir(dirpath)
            except OSError:
                continue
            for filename in names:
                if (
                    filename == "__pycache__"
                    or filename == ".pytest_cache"
                    or filename.startswith(".")
                ):
                    continue
                path = os.path.join(dirpath, filename)
                if os.path.isdir(path):
                    pending_dirs.append(path)
                    continue
                if not (
                    filename.endswith(".py")
                    or filename.endswith(".c")
                    or filename.endswith(".h")
                ):
                    continue
                if os.path.isfile(path) and os.path.getmtime(path) > archive_mtime:
                    return True
    return False


def is_library_source(runtime_dir: str, src_path: str) -> bool:
    runtime_py_dir = os.path.abspath(os.path.join(runtime_dir, "py"))
    source_path = os.path.abspath(src_path)
    if source_path.startswith(runtime_py_dir + os.sep):
        return True
    parts = source_path.split(os.sep)
    for index, part in enumerate(parts):
        if part != "py" or index == 0:
            continue
        parent = parts[index - 1]
        if parent == "py_runtime" or parent.startswith("py_runtime_"):
            return True
    return False


def target_stamp(archive: str) -> str:
    return str(archive) + ".target"


def provenance_manifest(archive: str) -> str:
    return str(archive) + ".provenance.json"


def capi_inventory(archive: str) -> str:
    return str(archive) + ".capi_syms"


def requires_provenance(archive: str) -> bool:
    return os.path.basename(archive) == "libpy_runtime_pcc_py.a"


def requires_c_bundle_validation(archive: str) -> bool:
    # The libpython archive carries a C-API inventory instead of a provenance
    # manifest (it adds the cc-built CPython bridge to the pcc-Python archive).
    return os.path.basename(archive) == "libpy_runtime_pcc_py_libpython.a"


def c_bundle_valid(archive: str, *, host_python_command) -> bool:
    if not requires_c_bundle_validation(archive):
        return True
    try:
        with open(archive, "rb") as stream:
            archive_data = stream.read()
        inventory_path = capi_inventory(archive)
        with open(inventory_path, "r", encoding="utf-8") as stream:
            inventory_text = stream.read()
    except OSError:
        return False
    lines = inventory_text.splitlines()
    if not lines or inventory_text != join_strings(lines, "\n") + "\n":
        return False
    if lines != sorted(set(lines)):
        return False
    for symbol in lines:
        bare = symbol[1:] if symbol.startswith("_") else symbol
        if not (bare.startswith("Py") or bare.startswith("_Py")):
            return False
    try:
        from pcc.backend.ar import read_members
        from pcc.backend.ar_writer import _defined_symbols

        members = read_members(archive_data)
        if not members:
            return False
        actual = set()
        for _name, payload in members:
            _format, symbols = _defined_symbols(payload)
            for symbol in symbols:
                bare = symbol[1:] if symbol.startswith("_") else symbol
                if bare.startswith("Py") or bare.startswith("_Py"):
                    actual.add(symbol)
        return sorted(actual) == lines
    except Exception:
        return False


def provenance_root(archive: str, runtime_dir: str) -> str:
    archive_dir = os.path.dirname(os.path.abspath(archive))
    if os.path.isdir(os.path.join(archive_dir, "py")):
        return archive_dir
    return runtime_dir


def provenance_valid(
    archive: str,
    *,
    runtime_dir: str,
    pcc_source_root,
    host_python_command,
    runtime_root: Optional[str] = None,
) -> bool:
    manifest = provenance_manifest(archive)
    if not os.path.isfile(archive) or not os.path.isfile(manifest):
        return False
    root = runtime_root or provenance_root(archive, runtime_dir)
    try:
        from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

        verify_runtime_archive_manifest(
            Path(archive), runtime_root=Path(root),
        )
    except Exception:
        return False
    return True


def provenance_codegen_stale(
    archive: str,
    *,
    pcc_source_root,
    host_python_command,
) -> bool:
    """Check freshness in the compiler process; an unproved identity is stale."""

    manifest = provenance_manifest(archive)
    if not os.path.isfile(manifest):
        return True
    try:
        from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen

        with open(manifest, "r", encoding="utf-8") as stream:
            records = json.loads(stream.read())
        return manifest_is_stale_for_current_codegen(records)
    except Exception:
        return True


def target_matches(archive: str, expected_target_id: str) -> bool:
    stamp = target_stamp(archive)
    if not os.path.isfile(stamp):
        return False
    try:
        with open(stamp, "r", encoding="utf-8") as stream:
            return stream.read().strip() == expected_target_id
    except OSError:
        return False


def wheel_stamp_matches(archive: str, expected_target_id: str) -> bool:
    """Validate an installed wheel's self-contained runtime receipt.

    The receipt is deliberately verifiable by compiled pcc1 itself.  A wheel
    consumer must not need to launch host Python merely to prove that the
    archive, provenance manifest, and C-API inventory are the exact bytes that
    the release build hook verified before publication.
    """
    marker = str(archive) + ".wheel"
    if not os.path.isfile(marker):
        return False
    try:
        with open(marker, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return False
    if len(lines) != 5 or lines[0] != "pcc.runtime-wheel-artifact.v2":
        return False
    if lines[1] != "target=" + expected_target_id:
        return False
    expected: list[str] = []
    prefixes = (
        "archive-sha256=",
        "manifest-sha256=",
        "capi-inventory-sha256=",
    )
    index = 0
    while index < len(prefixes):
        prefix = prefixes[index]
        line = lines[index + 2]
        if not line.startswith(prefix):
            return False
        digest = line[len(prefix) :]
        if len(digest) != 64:
            return False
        char_index = 0
        while char_index < len(digest):
            character = digest[char_index]
            if not ("0" <= character <= "9" or "a" <= character <= "f"):
                return False
            char_index += 1
        expected.append(digest)
        index += 1
    paths = (archive, provenance_manifest(archive), capi_inventory(archive))
    index = 0
    while index < len(paths):
        if not os.path.isfile(paths[index]):
            return False
        if _file_sha256(paths[index]) != expected[index]:
            return False
        index += 1
    return True


def _file_sha256(path: str) -> str:
    """Return a file digest in both host-pcc and compiled-pcc1 modes."""
    try:
        native_digest = str(os._pcc_sha256_file_hex(path) or "").lower()
    except AttributeError:
        import hashlib

        digest = hashlib.sha256()
        try:
            with open(path, "rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            return ""
        native_digest = digest.hexdigest().lower()
    except Exception:
        return ""
    if len(native_digest) != 64:
        return ""
    index = 0
    while index < len(native_digest):
        character = native_digest[index]
        if not ("0" <= character <= "9" or "a" <= character <= "f"):
            return ""
        index += 1
    return native_digest


def target_id(host_target_triple: str) -> str:
    import platform

    machine = platform.machine().lower()
    if machine in ("amd64", "x64"):
        machine = "x86_64"
    return f"{sys.platform}:{machine}:{host_target_triple}"


def write_target_stamp(archive: str, expected_target_id: str) -> None:
    try:
        with open(target_stamp(archive), "w", encoding="utf-8") as stream:
            stream.write(expected_target_id + "\n")
    except OSError:
        pass


def _runtime_lock_owner_alive(owner_pid: int) -> bool:
    if owner_pid <= 0:
        return False
    try:
        os.kill(owner_pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _remove_runtime_build_lock(lock_dir: str, owner_path: str) -> None:
    try:
        os.unlink(owner_path)
    except OSError:
        pass
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def _acquire_runtime_build_lock(lock_dir: str) -> None:
    owner_path = os.path.join(lock_dir, "owner")
    waited = 0
    ownerless_waited = 0
    while True:
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            owner_pid = 0
            try:
                with open(owner_path, "r", encoding="ascii") as stream:
                    raw_owner = stream.read().strip()
                if raw_owner.isdigit():
                    owner_pid = int(raw_owner)
            except (OSError, ValueError):
                pass
            owner_alive = _runtime_lock_owner_alive(owner_pid)
            stale = (
                (owner_pid > 0 and not owner_alive)
                or (owner_pid <= 0 and ownerless_waited >= 50)
            )
            if stale:
                _remove_runtime_build_lock(lock_dir, owner_path)
                if not os.path.isdir(lock_dir):
                    ownerless_waited = 0
                    continue
            if owner_pid <= 0:
                ownerless_waited += 1
            else:
                ownerless_waited = 0
            waited += 1
            if waited >= 3000:
                raise RuntimeArchiveError(
                    "timed out waiting for pcc runtime build lock: " + lock_dir
                )
            time.sleep(0.1)
            continue
        except OSError as exc:
            raise RuntimeArchiveError(
                "failed to create pcc runtime build lock: " + lock_dir
            ) from exc
        try:
            with open(owner_path, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()) + "\n")
        except OSError as exc:
            _remove_runtime_build_lock(lock_dir, owner_path)
            raise RuntimeArchiveError(
                "failed to publish pcc runtime build lock owner: " + lock_dir
            ) from exc
        return


def run_runtime_make(runtime_dir: str, make_cmd, *, verbose: bool) -> None:
    del verbose
    require_native_provisioning_allowed()
    lock_dir = os.path.join(runtime_dir, ".pcc-runtime-build.lock")
    owner_path = os.path.join(lock_dir, "owner")
    _acquire_runtime_build_lock(lock_dir)
    # The runtime archive is a build dependency, not the user's program: a
    # `-g` on the command line must not make the runtime ports compile with
    # debug info, which changes the archive every user build toggles the flag.
    build_env = dict(os.environ)
    build_env.pop("PCC_PY_DEBUG_INFO", None)
    try:
        subprocess.run(
            make_cmd,
            check=True,
            capture_output=False,
            env=build_env,
        )
    finally:
        _remove_runtime_build_lock(lock_dir, owner_path)


def host_python_for_make(executable: str) -> str:
    executable = str(executable)
    if executable and not os.path.isabs(executable):
        if os.sep in executable or (os.altsep and os.altsep in executable):
            return os.path.abspath(executable)
        resolved = shutil.which(executable)
        if resolved:
            return resolved
    return executable


def archive_stale(
    archive: str,
    *,
    runtime_dir: str,
    base_pcc_py_archive: str,
    c_bundle_valid,
    target_matches,
    archive_requires_provenance,
    archive_provenance_valid,
    archive_codegen_stale,
    wheel_matches,
    compiler_sources_newer,
) -> bool:
    if not os.path.isfile(archive):
        return True
    if not c_bundle_valid(archive) or not target_matches(archive):
        return True
    archive_base = str(os.path.basename(archive))
    if archive_requires_provenance(archive):
        if wheel_matches(archive):
            return False
        if not archive_provenance_valid(archive):
            return True
        if archive_codegen_stale(archive):
            return True
    archive_mtime = os.path.getmtime(archive)
    if compiler_sources_newer(archive_base, archive_mtime):
        return True
    if archive_base == "libpy_runtime_pcc_py_libpython.a":
        # The CPython bridge is the only C object in any runtime archive.
        for path in (
            os.path.join(runtime_dir, "src", "py_libpython.c"),
            os.path.join(runtime_dir, "src", "py_internal.h"),
            os.path.join(runtime_dir, "include", "py_runtime.h"),
        ):
            if os.path.isfile(path) and os.path.getmtime(path) > archive_mtime:
                return True
    if archive_base in (
        "libpy_runtime_pcc_py.a",
        "libpy_runtime_pcc_py_libpython.a",
    ):
        py_dir = os.path.join(runtime_dir, "py")
        if os.path.isdir(py_dir):
            for name in os.listdir(py_dir):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(py_dir, name)
                if os.path.isfile(path) and os.path.getmtime(path) > archive_mtime:
                    return True
        if archive_base == "libpy_runtime_pcc_py_libpython.a":
            if (
                os.path.isfile(base_pcc_py_archive)
                and os.path.getmtime(base_pcc_py_archive) > archive_mtime
            ):
                return True
    makefile = os.path.join(runtime_dir, "Makefile")
    return os.path.isfile(makefile) and os.path.getmtime(makefile) > archive_mtime


def ensure_runtime(
    verbose: bool,
    *,
    needs_libpython: bool,
    runtime_dir_default: str,
    archive_pcc_py: str,
    archive_pcc_py_libpython: str,
    archive_stale_check,
    c_bundle_valid,
    archive_requires_provenance,
    archive_provenance_valid,
    archive_codegen_stale,
    wheel_matches,
    archive_manifest,
    archive_target_matches,
    compiler_sources_newer,
    resolve_pcc_binary,
    runtime_host_python,
    run_make,
    write_archive_target_stamp,
    logger,
    target_triple: Optional[str] = None,
) -> str:
    from pcc.frontends.python.pipeline_targets import host_target_triple

    host_target = host_target_triple()
    target = target_triple or host_target
    if runtime_build_mode() == "owned":
        if needs_libpython:
            raise RuntimeArchiveError("owned platform runtime does not include libpython")
        from pcc.frontends.python.owned_runtime_build import ensure_target_runtime

        try:
            archive = ensure_target_runtime(
                runtime_dir_default, target, packaged_archive=archive_pcc_py,
                wheel_matches=wheel_matches if target == host_target else None,
            )
        except (NativeProvisioningError, ValueError, OSError) as exc:
            raise RuntimeArchiveError(str(exc)) from exc
        logger(verbose, "runtime archive (in-process owned): " + archive)
        return archive
    if target != host_target:
        raise RuntimeArchiveError("runtime Make reference oracle cannot build a cross target")
    return _ensure_runtime_make_reference(
        verbose, needs_libpython=needs_libpython,
        runtime_dir_default=runtime_dir_default, archive_pcc_py=archive_pcc_py,
        archive_pcc_py_libpython=archive_pcc_py_libpython,
        archive_stale_check=archive_stale_check, c_bundle_valid=c_bundle_valid,
        archive_requires_provenance=archive_requires_provenance,
        archive_provenance_valid=archive_provenance_valid,
        archive_codegen_stale=archive_codegen_stale, wheel_matches=wheel_matches,
        archive_manifest=archive_manifest, archive_target_matches=archive_target_matches,
        compiler_sources_newer=compiler_sources_newer, resolve_pcc_binary=resolve_pcc_binary,
        runtime_host_python=runtime_host_python, run_make=run_make,
        write_archive_target_stamp=write_archive_target_stamp, logger=logger,
    )


def _ensure_runtime_make_reference(
    verbose: bool,
    *,
    needs_libpython: bool,
    runtime_dir_default: str,
    archive_pcc_py: str,
    archive_pcc_py_libpython: str,
    archive_stale_check,
    c_bundle_valid,
    archive_requires_provenance,
    archive_provenance_valid,
    archive_codegen_stale,
    wheel_matches,
    archive_manifest,
    archive_target_matches,
    compiler_sources_newer,
    resolve_pcc_binary,
    runtime_host_python,
    run_make,
    write_archive_target_stamp,
    logger,
) -> str:
    explicit_archive = str(os.environ.get("PCC_RUNTIME_ARCHIVE", "") or "").strip()
    if explicit_archive:
        explicit_archive = os.path.abspath(explicit_archive)
        if not os.path.isfile(explicit_archive):
            raise RuntimeArchiveError(
                "explicit runtime archive not found: " + explicit_archive
            )
        if not c_bundle_valid(explicit_archive):
            raise RuntimeArchiveError(
                "explicit runtime archive has an invalid archive/inventory bundle: "
                + explicit_archive
            )
        if (
            archive_requires_provenance(explicit_archive)
            and not wheel_matches(explicit_archive)
            and not archive_provenance_valid(explicit_archive)
        ):
            raise RuntimeArchiveError(
                "explicit pcc-Python runtime archive has invalid provenance: "
                + archive_manifest(explicit_archive)
            )
        logger(verbose, "runtime archive (explicit): " + explicit_archive)
        return explicit_archive

    runtime_dir_raw = str(os.environ.get("PCC_RUNTIME_DIR", "") or "").strip()
    runtime_dir = (
        os.path.abspath(runtime_dir_raw) if runtime_dir_raw else runtime_dir_default
    )
    if not os.path.isdir(runtime_dir):
        raise RuntimeArchiveError(
            "explicit runtime directory not found: " + runtime_dir
        )
    # The runtime is the pcc-Python archive; libpython mode adds the CPython
    # bridge object to it.  The host-cc and pcc-C runtime archives are retired.
    archive = archive_pcc_py_libpython if needs_libpython else archive_pcc_py
    if runtime_dir != runtime_dir_default:
        archive = os.path.join(runtime_dir, os.path.basename(archive))

    debug = bool(str(os.environ.get("PCC_DEBUG_RUNTIME", "")).strip())
    if debug:
        logger(True, "[runtime] runtime_dir=" + runtime_dir)
        logger(True, "[runtime] archive=" + str(archive))
        logger(True, "[runtime] makefile=" + os.path.join(runtime_dir, "Makefile"))
        logger(True, "[runtime] needs_libpython=" + str(needs_libpython))
        logger(True, "[runtime] archive_exists=" + str(os.path.isfile(archive)))
        if os.path.isfile(archive):
            logger(
                True,
                "[runtime] archive_stale=" + str(archive_stale_check(archive)),
            )
        logger(
            True,
            "[runtime] makefile_exists="
            + str(os.path.isfile(os.path.join(runtime_dir, "Makefile"))),
        )

    stale = True
    if os.path.isfile(archive):
        stale = archive_stale_check(archive)
    if os.path.isfile(archive) and not stale:
        logger(verbose, "runtime archive: " + archive)
        return archive

    require_native_provisioning_allowed()
    makefile = os.path.join(runtime_dir, "Makefile")
    if debug:
        try:
            with open(
                "/tmp/pcc_runtime_debug_probe.txt", "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    "[probe] makefile="
                    + makefile
                    + " exists="
                    + str(os.path.isfile(makefile))
                    + " archive="
                    + archive
                    + " exists="
                    + str(os.path.isfile(archive))
                    + "\n"
                )
        except Exception:
            pass
    if os.path.isfile(makefile):
        # Threads are the one configuration; only an explicit refcount
        # strategy selects a different archive build.
        runtime_config_forces_rebuild = bool(
            str(os.environ.get("PCC_REFCOUNT_KIND", "")).strip()
        )
        archive_mtime = os.path.getmtime(archive) if os.path.isfile(archive) else 0.0
        managed_local_archive = archive_requires_provenance(
            archive
        ) and not wheel_matches(archive)
        provenance_is_valid = not managed_local_archive or archive_provenance_valid(
            archive,
            runtime_root=runtime_dir,
        )
        provenance_forces_rebuild = managed_local_archive and not provenance_is_valid
        codegen_forces_rebuild = (
            managed_local_archive
            and provenance_is_valid
            and archive_codegen_stale(archive)
        )
        full_rebuild = (
            runtime_config_forces_rebuild
            or provenance_forces_rebuild
            or codegen_forces_rebuild
            or not archive_target_matches(archive)
            or compiler_sources_newer(os.path.basename(archive), archive_mtime)
        )
        make_cmd = ["make", "-C", runtime_dir]
        if full_rebuild:
            # A cold rebuild recompiles every pcc-Python runtime module
            # through the pcc frontend (measured ~20-30 min serial for ~180
            # modules after an emit-identity change).  The module rules are
            # independent (each .py -> own .ll -> own .o), the single make
            # process already holds the build lock, and Make's own dependency
            # graph prevents double-building, so bounded -j parallelism is
            # safe.  Leave the no-rebuild fast path untouched.
            try:
                _runtime_make_jobs = min(8, max(1, os.cpu_count() or 1))
            except Exception:
                _runtime_make_jobs = 4
            make_cmd.insert(1, "-B")
            make_cmd.insert(2, "-j" + str(_runtime_make_jobs))
        make_cmd.append(os.path.basename(archive))
        pcc_binary = resolve_pcc_binary()
        if pcc_binary:
            make_cmd.append(f"PCC={pcc_binary}")
            make_cmd.append(f"PYTHON={runtime_host_python()}")
            make_cmd.append("PCC_PYTHON_IR_PASSES=" + runtime_python_ir_pass_mode())
        logger(verbose, "building runtime: " + join_strings(make_cmd, " "))
        try:
            run_make(make_cmd, verbose=verbose)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeArchiveError(
                "failed to build required Python runtime archive "
                + archive
                + ": "
                + str(exc)
            ) from exc
        if os.path.isfile(archive):
            manifest = archive_manifest(archive)
            if archive_requires_provenance(archive):
                if not os.path.isfile(manifest):
                    raise RuntimeArchiveError(
                        "pcc-Python runtime archive missing provenance manifest: "
                        + manifest
                    )
                if not archive_provenance_valid(archive, runtime_root=runtime_dir):
                    raise RuntimeArchiveError(
                        "pcc-Python runtime archive has invalid provenance: "
                        + manifest
                    )
                if archive_codegen_stale(archive):
                    raise RuntimeArchiveError(
                        "pcc-Python runtime archive has stale codegen provenance: "
                        + manifest
                    )
            if not c_bundle_valid(archive):
                raise RuntimeArchiveError(
                    "runtime build published an invalid archive/inventory bundle: "
                    + archive
                )
            write_archive_target_stamp(archive)
            logger(verbose, "runtime archive: " + archive)
            return archive

    manifest = archive_manifest(archive)
    if os.path.isfile(archive) and archive_requires_provenance(archive):
        if not os.path.isfile(manifest):
            raise RuntimeArchiveError(
                "pcc-Python runtime archive missing provenance manifest: " + manifest
            )
        if not archive_provenance_valid(archive, runtime_root=runtime_dir):
            raise RuntimeArchiveError(
                "pcc-Python runtime archive has invalid provenance: " + manifest
            )
        if archive_codegen_stale(archive):
            raise RuntimeArchiveError(
                "pcc-Python runtime archive has stale codegen provenance: "
                + manifest
            )
    raise RuntimeArchiveError(
        "required Python runtime archive was not produced: " + archive
    )
