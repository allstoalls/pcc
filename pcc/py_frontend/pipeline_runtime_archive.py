"""Runtime-archive bundle validation and freshness policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from .pipeline_paths import join_strings


RUNTIME_PYTHON_IR_PASSES_ENV = "PCC_RUNTIME_PYTHON_IR_PASSES"


def runtime_python_ir_pass_mode() -> str:
    value = str(os.environ.get(RUNTIME_PYTHON_IR_PASSES_ENV, "") or "").strip()
    return value or "default"


class RuntimeArchiveError(RuntimeError):
    """Runtime archive selection or construction failed closed."""


def makefile_variable_words(runtime_dir: str, name: str) -> list[str]:
    makefile = os.path.join(str(runtime_dir), "Makefile")
    if not os.path.isfile(makefile):
        return []
    try:
        with open(makefile, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return []
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not (line.startswith(name + " =") or line.startswith(name + " +=")):
            index += 1
            continue
        line = line.split("=", 1)[1].strip()
        while True:
            continued = line.endswith("\\")
            if continued:
                line = line[:-1].strip()
            if line:
                out.extend(line.split())
            if not continued:
                break
            index += 1
            if index >= len(lines):
                break
            line = lines[index].strip()
        index += 1
    return out


def pcc_python_replaced_c_modules(runtime_dir: str) -> set[str]:
    py_modules = set(makefile_variable_words(runtime_dir, "PY_MODULES"))
    replaced: set[str] = set()
    for word in makefile_variable_words(runtime_dir, "PY_REPLACED_C_MODULES"):
        if word == "$(PY_MODULES)":
            replaced.update(py_modules)
        else:
            replaced.add(word)
    return replaced


def compiler_sources_newer_than(
    pcc_dir: str,
    archive_base: str,
    archive_mtime: float,
) -> bool:
    if archive_base not in (
        "libpy_runtime_pcc.a",
        "libpy_runtime_pcc_py.a",
        "libpy_runtime_pcc_py_libpython.a",
    ):
        return False
    roots = (
        os.path.join(pcc_dir, "backend"),
        os.path.join(pcc_dir, "codegen"),
        os.path.join(pcc_dir, "evaluater"),
        os.path.join(pcc_dir, "llvm_capi"),
        os.path.join(pcc_dir, "parse"),
        os.path.join(pcc_dir, "py_frontend"),
        os.path.join(pcc_dir, "tools"),
        os.path.join(pcc_dir, "__main__.py"),
        os.path.join(pcc_dir, "api.py"),
        os.path.join(pcc_dir, "cli_core.py"),
        os.path.join(pcc_dir, "pcc.py"),
        os.path.join(pcc_dir, "project.py"),
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
    return os.path.basename(archive) in (
        "libpy_runtime.a",
        "libpy_runtime_libpython.a",
        "libpy_runtime_pcc.a",
        "libpy_runtime_pcc_py_libpython.a",
    )


def c_bundle_valid(archive: str, *, host_python_command) -> bool:
    if not requires_c_bundle_validation(archive):
        return True
    try:
        with open(archive, "rb") as stream:
            if stream.read(8) != b"!<arch>\n":
                return False
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
    verify_code = (
        "import subprocess\n"
        "import sys\n"
        "archive = sys.argv[1]\n"
        "inventory_path = sys.argv[2]\n"
        "members = subprocess.check_output(['ar', 't', archive], text=True, timeout=30)\n"
        "real_members = []\n"
        "for raw in members.splitlines():\n"
        "    name = raw.strip()\n"
        "    if name and name not in ('/', '//') and not name.startswith('__.SYMDEF'):\n"
        "        real_members.append(name)\n"
        "if not real_members:\n"
        "    raise SystemExit(2)\n"
        "nm_text = subprocess.check_output(['nm', '-g', archive], stderr=subprocess.STDOUT, text=True, timeout=30)\n"
        "actual = set()\n"
        "for raw in nm_text.splitlines():\n"
        "    parts = raw.split()\n"
        "    if len(parts) != 3 or parts[1] == 'U' or not parts[1].isupper():\n"
        "        continue\n"
        "    symbol = parts[2]\n"
        "    bare = symbol[1:] if symbol.startswith('_') else symbol\n"
        "    if bare.startswith('Py') or bare.startswith('_Py'):\n"
        "        actual.add(symbol)\n"
        "with open(inventory_path, 'r', encoding='utf-8') as stream:\n"
        "    expected = stream.read().splitlines()\n"
        "if sorted(actual) != expected:\n"
        "    raise SystemExit(3)\n"
    )
    try:
        subprocess.run(
            [host_python_command(), "-c", verify_code, archive, capi_inventory(archive)],
            check=True,
            capture_output=True,
            timeout=90,
        )
    except Exception:
        return False
    return True


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
    host_code = (
        "import os\n"
        "import sys\n"
        "pcc_source_root = sys.argv[1]\n"
        "if pcc_source_root and pcc_source_root not in sys.path:\n"
        "    sys.path.insert(0, pcc_source_root)\n"
        "if pcc_source_root:\n"
        "    os.environ.setdefault('PCC_SOURCE_ROOT', pcc_source_root)\n"
        "    os.environ.setdefault('PCC_REPO_ROOT', pcc_source_root)\n"
        "from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest\n"
        "verify_runtime_archive_manifest(sys.argv[2], runtime_root=sys.argv[3])\n"
    )
    try:
        subprocess.run(
            [
                host_python_command(),
                "-c",
                host_code,
                pcc_source_root(),
                archive,
                str(root),
            ],
            check=True,
            capture_output=True,
            timeout=90,
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
    """Check an automatic local archive against the current compiler source.

    This intentionally crosses the existing host-Python boundary instead of
    importing host-only source identity code into the pcc1 closure.  Failure to
    spawn, import, read, or prove a checksum is stale for an automatically
    managed archive.
    """

    manifest = provenance_manifest(archive)
    if not os.path.isfile(manifest):
        return True
    host_code = (
        "import json\n"
        "import os\n"
        "import sys\n"
        "pcc_source_root = sys.argv[1]\n"
        "if pcc_source_root and pcc_source_root not in sys.path:\n"
        "    sys.path.insert(0, pcc_source_root)\n"
        "if pcc_source_root:\n"
        "    os.environ.setdefault('PCC_SOURCE_ROOT', pcc_source_root)\n"
        "    os.environ.setdefault('PCC_REPO_ROOT', pcc_source_root)\n"
        "from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen\n"
        "with open(sys.argv[2], 'r', encoding='utf-8') as stream:\n"
        "    manifest = json.load(stream)\n"
        "raise SystemExit(1 if manifest_is_stale_for_current_codegen(manifest) else 0)\n"
    )
    try:
        result = subprocess.run(
            [
                host_python_command(),
                "-c",
                host_code,
                pcc_source_root(),
                manifest,
            ],
            check=False,
            capture_output=True,
            timeout=90,
        )
    except Exception:
        return True
    try:
        return result.returncode != 0
    except Exception:
        # A compiled stage that cannot read ``returncode`` must not treat an
        # unverified manifest as valid.
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


def run_runtime_make(runtime_dir: str, make_cmd, *, verbose: bool) -> None:
    del verbose
    lock_dir = os.path.join(runtime_dir, ".pcc-runtime-build.lock")
    lock_script = (
        'lock="$1"; shift; owner="$lock/owner"; '
        'waited=0; ownerless_waited=0; '
        'while :; do '
        'if mkdir "$lock" 2>/dev/null; then '
        'if ! printf "%s\\n" "$$" > "$owner"; then '
        'rm -f "$owner"; rmdir "$lock" 2>/dev/null || :; exit 1; fi; '
        'break; fi; '
        'owner_pid=; if [ -r "$owner" ]; then '
        'IFS= read -r owner_pid < "$owner" || owner_pid=; fi; '
        'owner_valid=0; owner_alive=0; '
        'case "$owner_pid" in ""|*[!0-9]*) ;; '
        '*) owner_valid=1; kill -0 "$owner_pid" 2>/dev/null '
        '&& owner_alive=1 || : ;; esac; '
        'if [ "$owner_valid" -eq 1 ] && [ "$owner_alive" -eq 0 ]; then '
        'rm -f "$owner"; if rmdir "$lock" 2>/dev/null; then '
        'ownerless_waited=0; continue; fi; '
        'elif [ "$owner_valid" -eq 0 ]; then '
        'ownerless_waited=$((ownerless_waited + 1)); '
        'if [ "$ownerless_waited" -ge 50 ]; then '
        'rm -f "$owner"; if rmdir "$lock" 2>/dev/null; then '
        'ownerless_waited=0; continue; fi; fi; '
        'else ownerless_waited=0; fi; '
        'waited=$((waited + 1)); '
        'if [ "$waited" -ge 3000 ]; then '
        'echo "timed out waiting for pcc runtime build lock: $lock" >&2; '
        'exit 124; fi; sleep 0.1; done; '
        'cleanup_runtime_build_lock() { '
        'rm -f "$owner"; rmdir "$lock" 2>/dev/null || :; }; '
        "trap cleanup_runtime_build_lock EXIT; "
        "trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM; "
        '"$@"'
    )
    # The runtime archive is a build dependency, not the user's program: a
    # `-g` on the command line must not make the runtime ports compile with
    # debug info, which changes the archive every user build toggles the flag.
    build_env = dict(os.environ)
    build_env.pop("PCC_PY_DEBUG_INFO", None)
    subprocess.run(
        ["sh", "-c", lock_script, "sh", lock_dir, *make_cmd],
        check=True,
        capture_output=False,
        env=build_env,
    )


def cc_mode(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value in ("cc", "c", "host"):
        return "cc"
    if value in ("pcc", "self"):
        return "pcc"
    return "pcc"


def high_mode(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value in ("c", "cc"):
        return "c"
    if value in ("py", "python"):
        return "py"
    return "py"


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
    replaced_c_modules,
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
    archive_uses_libpython = archive_base in (
        "libpy_runtime_libpython.a",
        "libpy_runtime_pcc_py_libpython.a",
    )
    archive_uses_pcc_python = archive_base in (
        "libpy_runtime_pcc_py.a",
        "libpy_runtime_pcc_py_libpython.a",
    )
    replaced = replaced_c_modules() if archive_uses_pcc_python else set()
    header = os.path.join(runtime_dir, "include", "py_runtime.h")
    if os.path.isfile(header) and os.path.getmtime(header) > archive_mtime:
        return True
    src_dir = os.path.join(runtime_dir, "src")
    if os.path.isdir(src_dir):
        for name in os.listdir(src_dir):
            if not name.endswith(".c"):
                continue
            if name == "py_libpython.c" and not archive_uses_libpython:
                continue
            if name[:-2] in replaced:
                continue
            path = os.path.join(src_dir, name)
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
    archive_default: str,
    archive_libpython: str,
    archive_pcc: str,
    archive_pcc_py: str,
    archive_pcc_py_libpython: str,
    archive_stale_check,
    selected_cc_mode,
    selected_high_mode,
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
    cc = selected_cc_mode()
    high = selected_high_mode()
    if cc == "pcc":
        if high == "py":
            archive = archive_pcc_py_libpython if needs_libpython else archive_pcc_py
        elif needs_libpython:
            archive = archive_libpython
        else:
            archive = archive_pcc
    else:
        archive = archive_libpython if needs_libpython else archive_default
    if runtime_dir != runtime_dir_default:
        archive = os.path.join(runtime_dir, os.path.basename(archive))

    debug = bool(str(os.environ.get("PCC_DEBUG_RUNTIME", "")).strip())
    if debug:
        logger(True, "[runtime] runtime_dir=" + runtime_dir)
        logger(True, "[runtime] archive=" + str(archive))
        logger(True, "[runtime] makefile=" + os.path.join(runtime_dir, "Makefile"))
        logger(True, "[runtime] needs_libpython=" + str(needs_libpython))
        logger(True, "[runtime] cc_mode=" + str(cc))
        logger(True, "[runtime] high_mode=" + str(high))
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
        runtime_config_forces_rebuild = bool(
            str(os.environ.get("PCC_WITH_THREADS", "")).strip().lower()
            in ("1", "true", "yes", "on")
            or str(os.environ.get("PCC_REFCOUNT_KIND", "")).strip()
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
        if cc == "pcc":
            if high == "py":
                make_cmd.append(
                    "libpy_runtime_pcc_py_libpython.a"
                    if needs_libpython
                    else "libpy_runtime_pcc_py.a"
                )
            else:
                if needs_libpython:
                    make_cmd.extend(
                        [
                            "PCC_WITH_LIBPYTHON=1",
                            "LIB=libpy_runtime_libpython.a",
                            "OBJDIR=build_libpython",
                        ]
                    )
                else:
                    make_cmd.append("libpy_runtime_pcc.a")
            pcc_binary = resolve_pcc_binary()
            if pcc_binary and high == "py":
                make_cmd.append(f"PCC={pcc_binary}")
                make_cmd.append(f"PYTHON={runtime_host_python()}")
                make_cmd.append(
                    "PCC_PYTHON_IR_PASSES=" + runtime_python_ir_pass_mode()
                )
        elif needs_libpython:
            make_cmd.extend(
                [
                    "PCC_WITH_LIBPYTHON=1",
                    "LIB=libpy_runtime_libpython.a",
                    "OBJDIR=build_libpython",
                ]
            )
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
