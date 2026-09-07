"""Content-addressed cache for deterministic Python frontend IR bundles.

The cache boundary stops before IR passes, object emission, runtime selection,
and linking.  Metadata and digest work runs in the same explicit host-tool
subprocess boundary already used by the self-backend object cache.  If that
tool is unavailable, callers get a normal cache miss and compile as before.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CACHE_SCHEMA = "pcc.python-frontend-ir-cache.v2"
_CACHE_ENV = "PCC_PY_FRONTEND_IR_CACHE"
_CACHE_DIR_ENV = "PCC_PY_FRONTEND_IR_CACHE_DIR"
_CACHE_IDENTITY_ENV = "PCC_PY_FRONTEND_IR_CACHE_IDENTITY"
_OBJECT_CACHE_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE"
_OBJECT_CACHE_DIR_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE_DIR"
_OBJECT_CACHE_IDENTITY_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY"


_RETENTION_HELPER_CODE = r"""
import os
import sys

try:
    source_root = sys.argv[1]
    if source_root and source_root not in sys.path:
        sys.path.insert(0, source_root)
    from pcc.tools.compiler_cache_retention import (
        acquire_entry_lease,
        maintain_cache,
        record_successful_access,
        release_entry_lease,
    )

    mode = sys.argv[2]
    if mode == "lease-acquire":
        lease = acquire_entry_lease(sys.argv[3], owner_pid=os.getppid())
        print(lease)
    elif mode == "lease-release":
        release_entry_lease(sys.argv[3])
        print("1")
    elif mode == "touch":
        print("1" if record_successful_access(sys.argv[3]) else "0")
    elif mode == "auto":
        root = sys.argv[3]
        protected = sys.argv[4:]
        report = maintain_cache(root, automatic=True, protected_paths=protected)
        reclaimed = int(report.get("reclaimed_bytes", 0) or 0)
        failures = int(report.get("quarantine_failures", 0) or 0)
        pending = int(report.get("quarantine_pending", 0) or 0)
        if reclaimed or failures or pending:
            sys.stderr.write(
                "pcc compiler cache retention: reclaimed="
                + str(reclaimed)
                + " failures="
                + str(failures)
                + " pending="
                + str(pending)
                + "\n"
            )
        print("1")
    else:
        raise ValueError("unknown compiler cache retention helper mode")
except BaseException as exc:
    message = str(exc).replace("\n", " ")[:200]
    sys.stderr.write(
        "pcc compiler cache retention skipped: "
        + type(exc).__name__
        + (": " + message if message else "")
        + "\n"
    )
    raise SystemExit(2)
"""

# Settings that can change frontend-generated IR. Execution-only settings such
# as GC backend, runtime archive, worker count, profiling, IR passes, and native
# object-emitter policy intentionally remain outside this pre-pass key.
_CODEGEN_ENV_NAMES = (
    "PCC_DISABLE_BULK_GENERATOR_FRAME_INIT",
    "PCC_GENERATOR_FIRST_ENTRY_INIT",
    "PCC_FAST_COMPLETED_CONTINUATIONS",
    "PCC_DIRECT_GENERATOR_TASKS",
    "PCC_MOVE_GENERATOR_FRAME_OWNERS",
    "PCC_DEBUG_CODEGEN_PHASES",
    "PCC_DEBUG_RELEASES",
    "PCC_DEBUG_RUNTIME",
    "PCC_GPU_BACKEND",
    "PCC_PYTHON_LOW_IR",
    "PCC_PYTHON_TYPED_INT_ABI",
    "PCC_REFCOUNT_KIND",
    "PCC_WITH_THREADS",
)


_CACHE_HELPER_CODE = r"""
import hashlib
import json
import os
import shutil
import sys
import time

SCHEMA = "pcc.python-frontend-ir-cache.v2"

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()

def read_lines(path):
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read().splitlines()

def parse_plan_input(path):
    lines = read_lines(path)
    pos = 0
    if not lines or lines[pos] != SCHEMA:
        raise ValueError("invalid frontend cache plan schema")
    pos += 1
    identity, platform, machine, compiler_path = lines[pos:pos + 4]
    pos += 4
    entry, libpython_mode, scaffold = lines[pos:pos + 3]
    pos += 3
    sibling_count = int(lines[pos]); pos += 1
    siblings = lines[pos:pos + sibling_count]; pos += sibling_count
    env_count = int(lines[pos]); pos += 1
    environment = {}
    for _ in range(env_count):
        name, value = lines[pos].split("\t", 1); pos += 1
        environment[name] = value
    source_count = int(lines[pos]); pos += 1
    sources = []
    for _ in range(source_count):
        module, source = lines[pos].split("\t", 1); pos += 1
        sources.append({
            "module": module,
            "path": os.path.abspath(source),
            "sha256": sha256_file(source),
        })
    return {
        "schema": SCHEMA,
        "identity": identity,
        "platform": platform,
        "machine": machine,
        # A staged compiler is copied into one output directory per GC owner.
        # Its physical path is provenance, not a semantic input to frontend IR.
        # Key compiler identity by bytes so byte-identical pcc1/pcc2 copies can
        # share the same deterministic bundle while changed compiler bytes miss.
        "compiler_sha256": sha256_file(compiler_path),
        "entry_module": entry,
        "sibling_inits": siblings,
        "libpython_mode": libpython_mode,
        "ir_scaffold_mode": scaffold,
        "codegen_environment": environment,
        "sources": sources,
    }

def cache_paths(root, key):
    parent = os.path.join(root, key[:2])
    entry = os.path.join(parent, key)
    return parent, entry, entry + ".lock"

def valid_entry(entry, key):
    manifest_path = os.path.join(entry, "manifest.json")
    bundle_path = os.path.join(entry, "ir.bundle")
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if manifest.get("schema") != SCHEMA or manifest.get("key") != key:
            return None
        if sha256_file(bundle_path) != manifest.get("bundle_sha256"):
            return None
        return manifest, bundle_path
    except Exception:
        return None

def print_entry(entry, key):
    loaded = valid_entry(entry, key)
    if loaded is None:
        print("MISS")
        return False
    manifest, bundle_path = loaded
    libraries = manifest.get("libpython_modules", [])
    names = manifest.get("module_names", [])
    sizes = manifest.get("ir_char_sizes", [])
    if not isinstance(libraries, list) or not isinstance(names, list) or not isinstance(sizes, list):
        print("MISS")
        return False
    if len(names) != len(sizes) or not names:
        print("MISS")
        return False
    try:
        sizes = [int(size) for size in sizes]
        total_bytes = int(manifest.get("total_ir_bytes_before_passes", 0))
    except (TypeError, ValueError):
        print("MISS")
        return False
    if total_bytes < 0 or any(size < 1 for size in sizes):
        print("MISS")
        return False
    print("HIT")
    print("1" if manifest.get("needs_libpython") else "0")
    print("1" if manifest.get("needs_native_extension_exports") else "0")
    print(total_bytes)
    print(len(libraries))
    for name in libraries:
        print(str(name))
    print(len(names))
    for name, size in zip(names, sizes):
        print(str(name) + "\t" + str(size))
    print(bundle_path)
    return True

def parse_publish_input(path):
    lines = read_lines(path)
    pos = 0
    if not lines or lines[pos] != SCHEMA:
        raise ValueError("invalid frontend cache publish schema")
    pos += 1
    key = lines[pos]; pos += 1
    needs_libpython = lines[pos] == "1"; pos += 1
    needs_native = lines[pos] == "1"; pos += 1
    total_bytes = int(lines[pos]); pos += 1
    library_count = int(lines[pos]); pos += 1
    libraries = lines[pos:pos + library_count]; pos += library_count
    module_count = int(lines[pos]); pos += 1
    names = []
    sizes = []
    for _ in range(module_count):
        name, size = lines[pos].split("\t", 1); pos += 1
        names.append(name)
        sizes.append(int(size))
    return {
        "schema": SCHEMA,
        "key": key,
        "needs_libpython": needs_libpython,
        "needs_native_extension_exports": needs_native,
        "total_ir_bytes_before_passes": total_bytes,
        "libpython_modules": libraries,
        "module_names": names,
        "ir_char_sizes": sizes,
    }

mode = sys.argv[1]
if mode == "plan":
    input_path, root = sys.argv[2:4]
    try:
        material = parse_plan_input(input_path)
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(encoded).hexdigest()
        parent, entry, lock = cache_paths(root, key)
        graph_material = {
            "entry_module": material["entry_module"],
            "module_paths": [
                (source["module"], source["path"])
                for source in material["sources"]
            ],
            "schema": SCHEMA,
        }
        graph_key = hashlib.sha256(
            json.dumps(
                graph_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        option_material = {
            "codegen_environment": material["codegen_environment"],
            "entry_module": material["entry_module"],
            "ir_scaffold_mode": material["ir_scaffold_mode"],
            "libpython_mode": material["libpython_mode"],
            "sibling_inits": material["sibling_inits"],
        }
        options_digest = hashlib.sha256(
            json.dumps(
                option_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        runtime_abi_digest = hashlib.sha256(
            material["identity"].encode("utf-8")
        ).hexdigest()
        action_root = os.path.join(root, "module-actions", graph_key)
        fields = (
            key,
            parent,
            entry,
            lock,
            material["compiler_sha256"],
            runtime_abi_digest,
            material["platform"] + ":" + material["machine"],
            options_digest,
            action_root,
        )
        print("\t".join(fields))
    finally:
        try: os.unlink(input_path)
        except OSError: pass
elif mode == "load":
    print_entry(sys.argv[2], sys.argv[3])
elif mode == "wait":
    entry, key, lock, timeout_text = sys.argv[2:6]
    deadline = time.monotonic() + float(timeout_text)
    while time.monotonic() < deadline:
        if valid_entry(entry, key) is not None:
            print_entry(entry, key)
            break
        if not os.path.isdir(lock):
            print("MISS")
            break
        time.sleep(0.25)
    else:
        print("MISS")
elif mode == "acquire":
    parent, lock, entry = sys.argv[2:5]
    eviction = entry + ".pcc-evict"
    os.makedirs(parent, exist_ok=True)
    acquired = False
    if not os.path.exists(eviction):
        try:
            os.mkdir(lock)
            acquired = True
        except FileExistsError:
            try:
                owner_pid = 0
                try:
                    owner_path = os.path.join(lock, "owner.json")
                    with open(owner_path, "r", encoding="utf-8") as stream:
                        owner_pid = int(json.load(stream).get("pid", 0))
                except Exception:
                    owner_pid = 0
                owner_alive = False
                if owner_pid > 0:
                    try:
                        os.kill(owner_pid, 0)
                        owner_alive = True
                    except PermissionError:
                        owner_alive = True
                    except OSError:
                        owner_alive = False
                if owner_pid > 0:
                    stale = not owner_alive
                else:
                    stale = time.time() - os.path.getmtime(lock) > 900.0
                if stale:
                    shutil.rmtree(lock)
                    os.mkdir(lock)
                    acquired = True
            except OSError:
                acquired = False
        except OSError:
            acquired = False
    if acquired:
        try:
            with open(os.path.join(lock, "owner.json"), "w", encoding="utf-8") as stream:
                json.dump(
                    {"pid": os.getppid(), "created_ns": time.time_ns()},
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
        except OSError:
            shutil.rmtree(lock, ignore_errors=True)
            acquired = False
    if acquired and os.path.exists(eviction):
        try:
            shutil.rmtree(lock)
        finally:
            acquired = False
    print("1" if acquired else "0")
elif mode == "publish":
    input_path, bundle_path, parent, entry = sys.argv[2:6]
    temporary = entry + ".tmp." + str(os.getpid()) + "." + str(time.time_ns())
    try:
        manifest = parse_publish_input(input_path)
        key = manifest["key"]
        if valid_entry(entry, key) is not None:
            print("1")
        else:
            if os.path.isdir(entry):
                shutil.rmtree(entry)
            os.makedirs(parent, exist_ok=True)
            os.mkdir(temporary)
            target_bundle = os.path.join(temporary, "ir.bundle")
            os.replace(bundle_path, target_bundle)
            manifest["bundle_sha256"] = sha256_file(target_bundle)
            with open(os.path.join(temporary, "manifest.json"), "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
            os.rename(temporary, entry)
            temporary = ""
            print("1")
    finally:
        for path in (input_path, bundle_path):
            try: os.unlink(path)
            except OSError: pass
        if temporary:
            try: shutil.rmtree(temporary)
            except OSError: pass
elif mode == "release":
    try: shutil.rmtree(sys.argv[2])
    except OSError: pass
    print("1")
else:
    raise ValueError("unknown frontend cache helper mode")
"""


def _disabled(value: str) -> bool:
    return str(value or "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
        "disable",
        "disabled",
    )


def python_frontend_ir_cache_enabled() -> bool:
    if _disabled(os.environ.get(_CACHE_ENV, "")):
        return False
    explicit_identity = str(os.environ.get(_CACHE_IDENTITY_ENV, "") or "").strip()
    if explicit_identity:
        return True
    # Compatibility for callers that still use the former shared namespace:
    # their object-cache disable switch continues to disable both caches.
    if _disabled(os.environ.get(_OBJECT_CACHE_ENV, "")):
        return False
    return bool(python_frontend_ir_cache_identity())


def python_frontend_ir_cache_identity() -> str:
    """Return the frontend action namespace, with v1-env compatibility."""

    configured = str(os.environ.get(_CACHE_IDENTITY_ENV, "") or "").strip()
    if configured:
        return configured
    return str(os.environ.get(_OBJECT_CACHE_IDENTITY_ENV, "") or "").strip()


def python_frontend_ir_cache_dir() -> str:
    configured = str(os.environ.get(_CACHE_DIR_ENV, "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    object_dir = str(os.environ.get(_OBJECT_CACHE_DIR_ENV, "") or "").strip()
    if object_dir:
        base = os.path.abspath(os.path.expanduser(object_dir))
    else:
        base = os.path.join(
            os.path.expanduser("~"),
            ".cache",
            "pcc",
            "self-backend-object-cache",
        )
    return os.path.join(base, "frontend-ir")


def python_compiler_cache_root() -> str:
    """Return the shared lifecycle root for frontend and object entries."""

    frontend = python_frontend_ir_cache_dir()
    if os.path.basename(frontend) == "frontend-ir":
        return os.path.dirname(frontend)
    # An explicitly configured standalone frontend cache remains a valid
    # lifecycle root even when no object cache shares its parent.
    return frontend


def _machine_name() -> str:
    try:
        return str(os.uname().machine)
    except Exception:
        return "unknown"


def _safe_field(value) -> str:
    text = str(value)
    if "\n" in text or "\t" in text:
        raise ValueError("frontend cache fields cannot contain tabs or newlines")
    return text


def _scratch_path(kind: str) -> str:
    return os.path.join(
        "/tmp",
        "pcc_frontend_cache_" + kind + "_" + str(os.getpid()),
    )


def _helper_output(host_python: str, mode: str, args) -> str:
    command = [host_python, "-c", _CACHE_HELPER_CODE, mode]
    for arg in args:
        command.append(str(arg))
    return str(subprocess.check_output(command, text=True)).strip()


def _default_source_root() -> str:
    configured = str(
        os.environ.get("PCC_SOURCE_ROOT", "")
        or os.environ.get("PCC_REPO_ROOT", "")
        or ""
    ).strip()
    if configured:
        return os.path.abspath(configured)
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _retention_helper_output(plan, mode: str, args) -> str:
    command = [
        str(plan["host_python"]),
        "-c",
        _RETENTION_HELPER_CODE,
        str(plan.get("source_root") or _default_source_root()),
        mode,
    ]
    for arg in args:
        command.append(str(arg))
    return str(subprocess.check_output(command, text=True)).strip()


def _retention_touch(plan) -> None:
    try:
        _retention_helper_output(plan, "touch", (plan["entry"],))
    except Exception:
        pass


def _retention_auto(plan) -> None:
    try:
        _retention_helper_output(
            plan,
            "auto",
            (plan["cache_root"], plan["entry"]),
        )
    except Exception:
        # Cache lifecycle failures never turn successful compilation into an
        # error.  The next lookup safely behaves like a normal cache miss.
        pass


def plan_python_frontend_ir_cache(
    src_paths,
    module_names,
    *,
    compiler_executable: str,
    host_python: str,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    source_root: str = "",
):
    """Return a cache plan, or ``None`` when a safe key is unavailable."""
    if not python_frontend_ir_cache_enabled():
        return None
    if len(src_paths) != len(module_names) or not src_paths:
        return None
    if not compiler_executable or not os.path.isfile(compiler_executable):
        return None
    input_path = _scratch_path("plan")
    try:
        with open(input_path, "w", encoding="utf-8") as stream:
            stream.write(_CACHE_SCHEMA + "\n")
            stream.write(_safe_field(python_frontend_ir_cache_identity()) + "\n")
            stream.write(_safe_field(sys.platform) + "\n")
            stream.write(_safe_field(_machine_name()) + "\n")
            stream.write(_safe_field(os.path.abspath(compiler_executable)) + "\n")
            stream.write(_safe_field(entry_module) + "\n")
            stream.write(_safe_field(libpython_mode) + "\n")
            stream.write(_safe_field(ir_scaffold_mode) + "\n")
            stream.write(str(len(sibling_inits)) + "\n")
            for name in sibling_inits:
                stream.write(_safe_field(name) + "\n")
            stream.write(str(len(_CODEGEN_ENV_NAMES)) + "\n")
            for name in _CODEGEN_ENV_NAMES:
                stream.write(name + "\t" + _safe_field(os.environ.get(name, "")) + "\n")
            stream.write(str(len(src_paths)) + "\n")
            for src_path, module_name in zip(src_paths, module_names):
                stream.write(
                    _safe_field(module_name)
                    + "\t"
                    + _safe_field(os.path.abspath(src_path))
                    + "\n"
                )
        output = _helper_output(
            host_python,
            "plan",
            (input_path, python_frontend_ir_cache_dir()),
        )
        parts = output.split("\t")
        if len(parts) != 9:
            return None
        return {
            "schema": _CACHE_SCHEMA,
            "key": parts[0],
            "parent": parts[1],
            "entry": parts[2],
            "lock": parts[3],
            "compiler_digest": parts[4],
            "runtime_abi_digest": parts[5],
            "target": parts[6],
            "options_digest": parts[7],
            "action_root": parts[8],
            "host_python": str(host_python),
            "cache_root": python_compiler_cache_root(),
            "source_root": str(source_root or _default_source_root()),
        }
    except Exception:
        return None


def _parse_loaded_result(output: str, plan, expected_module_names):
    lines = output.splitlines()
    if not lines or lines[0] != "HIT":
        return None
    pos = 1
    try:
        needs_libpython = lines[pos] == "1"
        pos += 1
        needs_native = lines[pos] == "1"
        pos += 1
        total_bytes = int(lines[pos])
        pos += 1
        library_count = int(lines[pos])
        pos += 1
        libraries = lines[pos : pos + library_count]
        pos += library_count
        module_count = int(lines[pos])
        pos += 1
        names = []
        sizes = []
        for _index in range(module_count):
            name, size = lines[pos].split("\t", 1)
            pos += 1
            names.append(name)
            sizes.append(int(size))
        bundle_path = lines[pos]
        expected = [str(name) for name in expected_module_names]
        if names != expected or not os.path.isfile(bundle_path):
            return None
        module_ir_texts = []
        with open(bundle_path, "r", encoding="utf-8") as stream:
            for index, module_name in enumerate(names):
                char_size = int(stream.readline().strip())
                if char_size != sizes[index] or char_size < 1:
                    return None
                ir_text = stream.read(char_size)
                if len(ir_text) != char_size:
                    return None
                module_ir_texts.append((module_name, ir_text))
            if stream.read(1):
                return None
        return (
            module_ir_texts,
            needs_libpython,
            needs_native,
            total_bytes,
            libraries,
        )
    except Exception:
        return None


def load_python_frontend_ir_cache(plan, expected_module_names):
    if plan is None:
        return None
    lease_path = ""
    try:
        lease_path = _retention_helper_output(
            plan,
            "lease-acquire",
            (plan["entry"],),
        )
        if not lease_path:
            return None
        output = _helper_output(
            str(plan["host_python"]),
            "load",
            (plan["entry"], plan["key"]),
        )
        result = _parse_loaded_result(output, plan, expected_module_names)
        if result is not None:
            _retention_touch(plan)
        return result
    except Exception:
        return None
    finally:
        if lease_path:
            try:
                _retention_helper_output(
                    plan,
                    "lease-release",
                    (lease_path,),
                )
            except Exception:
                pass


def acquire_python_frontend_ir_cache(plan) -> bool:
    if plan is None:
        return False
    try:
        return (
            _helper_output(
                str(plan["host_python"]),
                "acquire",
                (plan["parent"], plan["lock"], plan["entry"]),
            )
            == "1"
        )
    except Exception:
        return False


def wait_python_frontend_ir_cache(
    plan,
    expected_module_names,
    *,
    timeout_seconds: float = 600.0,
):
    try:
        output = _helper_output(
            str(plan["host_python"]),
            "wait",
            (
                plan["entry"],
                plan["key"],
                plan["lock"],
                str(timeout_seconds),
            ),
        )
        if not output.startswith("HIT\n"):
            return None
        # Re-load under an explicit reader lease.  The readiness helper may
        # race with a pruner after it returns; the leased load either validates
        # the complete entry again or degrades to a normal miss.
        return load_python_frontend_ir_cache(plan, expected_module_names)
    except Exception:
        return None


def publish_python_frontend_ir_cache(plan, result) -> bool:
    if plan is None:
        return False
    descriptor_path = _scratch_path("publish")
    bundle_path = _scratch_path("bundle")
    try:
        module_ir_texts = result[0]
        with open(bundle_path, "w", encoding="utf-8") as stream:
            for _module_name, ir_text in module_ir_texts:
                text = str(ir_text)
                if not text:
                    return False
                stream.write(str(len(text)) + "\n")
                stream.write(text)
        with open(descriptor_path, "w", encoding="utf-8") as stream:
            stream.write(_CACHE_SCHEMA + "\n")
            stream.write(_safe_field(plan["key"]) + "\n")
            stream.write("1\n" if result[1] else "0\n")
            stream.write("1\n" if result[2] else "0\n")
            stream.write(str(int(result[3])) + "\n")
            stream.write(str(len(result[4])) + "\n")
            for name in result[4]:
                stream.write(_safe_field(name) + "\n")
            stream.write(str(len(module_ir_texts)) + "\n")
            for module_name, ir_text in module_ir_texts:
                stream.write(
                    _safe_field(module_name) + "\t" + str(len(str(ir_text))) + "\n"
                )
        published = (
            _helper_output(
                str(plan["host_python"]),
                "publish",
                (
                    descriptor_path,
                    bundle_path,
                    plan["parent"],
                    plan["entry"],
                ),
            )
            == "1"
        )
        if published:
            _retention_touch(plan)
            _retention_auto(plan)
        return published
    except Exception:
        return False


def release_python_frontend_ir_cache(plan) -> None:
    if plan is None:
        return
    try:
        _helper_output(
            str(plan["host_python"]),
            "release",
            (plan["lock"],),
        )
    except Exception:
        pass
