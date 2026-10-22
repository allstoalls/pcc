"""Self-backend native-object cache planning and publication."""

from __future__ import annotations

import os


OBJECT_CACHE_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE"
OBJECT_CACHE_DIR_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE_DIR"
OBJECT_CACHE_IDENTITY_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY"
OBJECT_CACHE_VERSION = "pcc.self-backend-object-cache.v4"


def enabled() -> bool:
    value = str(os.environ.get(OBJECT_CACHE_ENV, "") or "")
    if value.strip().lower() in (
        "0",
        "false",
        "no",
        "off",
        "disable",
        "disabled",
    ):
        return False
    # A profile is a mutable external code-generation input. Until the worker
    # consumes a frozen, content-identified profile, bypass both lookup and
    # publication rather than keying only its path or racing its contents.
    if str(os.environ.get("PCC_CODE_PROFILE", "") or "").strip():
        return False
    identity = str(os.environ.get(OBJECT_CACHE_IDENTITY_ENV, "") or "").strip()
    return bool(identity)


def cache_dir() -> str:
    configured = str(os.environ.get(OBJECT_CACHE_DIR_ENV, "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(
        os.path.expanduser("~"),
        ".cache",
        "pcc",
        "self-backend-object-cache",
    )


def path_allowed(cache_path: str) -> bool:
    if not cache_path or not enabled():
        return False
    cache_root = os.path.abspath(cache_dir())
    candidate = os.path.abspath(cache_path)
    return candidate.startswith(cache_root + os.sep)


def _copy_digest(source: str, destination: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        while True:
            block = reader.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            writer.write(block)
    return digest.hexdigest()


def maintain(protected_paths: list[str], *, host_python_command,
             pcc_source_root, retention_host_code: str) -> None:
    if not enabled():
        return
    _maintain_enabled(protected_paths)


def _maintain_enabled(protected_paths: list[str]) -> None:
    from pcc.tools.compiler_cache_retention import maintain_cache
    try:
        maintain_cache(cache_dir(), automatic=True, protected_paths=protected_paths)
    except (OSError, ValueError):
        # Cache retention is optional. It never changes compilation ownership.
        pass


def plan(worker_items: list[tuple[str, str, str]], target_id: str, cc: str,
         tmp_dir: str, *, host_python_command, plan_host_code: str,
         small_int_decimal) -> list[tuple[str, str]]:
    disabled = [("", "off") for _item in worker_items]
    if not worker_items or not enabled():
        return disabled
    return _plan_enabled(worker_items, target_id, cc)


def _effective_backend_configuration() -> str:
    """Canonical output-affecting settings for the owned worker cache.

    Keep this allowlist aligned with environment reads in the self backend.
    Use the emitter's resolvers so unset/default and explicit equivalent
    settings share a key. Preserve pass order and duplicates. The four
    AArch64 switches are conservatively included on every target.

    Diagnostic trace switches do not affect object bytes. Frontend settings
    and upstream IR optimization are represented by the complete emitted IR.
    Worker/source identity and target/artifact mode remain separate key fields;
    caller identity must bind the compiler/worker implementation and ABI.
    External function-order profiles bypass this cache in enabled().
    """
    import json
    from pcc.backend.self_backend_target_passes import (
        resolve_self_target_pass_names,
        resolve_self_target_pass_transport,
    )
    from pcc.backend.self_backend_aarch64_darwin_regalloc import (
        call_result_registers_enabled,
        callee_saved_registers_enabled,
        function_live_intervals_enabled,
    )
    from pcc.backend.self_backend_aarch64_darwin_branch_protection import (
        branch_protection_enabled,
    )
    from pcc.backend.precise_stackmap import precise_stackmap_tables_enabled

    transport = resolve_self_target_pass_transport()
    return json.dumps(
        [
            ["target-pass-transport", transport],
            ["target-passes", resolve_self_target_pass_names(transport=transport)],
            ["call-result-registers", call_result_registers_enabled()],
            ["callee-saved-registers", callee_saved_registers_enabled()],
            ["function-live-intervals", function_live_intervals_enabled()],
            ["branch-protection", branch_protection_enabled()],
            ["precise-stackmap-tables", precise_stackmap_tables_enabled()],
        ],
        separators=(",", ":"),
    )


def _plan_enabled(worker_items: list[tuple[str, str, str]], target_id: str,
                  cc: str) -> list[tuple[str, str]]:
    import hashlib
    from pcc.backend.self_backend_cache_identity import self_backend_emitter_source_identity
    from pcc.tools.compiler_cache_retention import acquire_entry_lease, release_entry_lease, record_successful_access
    identity = str(os.environ.get(OBJECT_CACHE_IDENTITY_ENV, "") or "")
    source_identity = self_backend_emitter_source_identity()
    configuration = _effective_backend_configuration()
    root = cache_dir()
    result = []
    for result_path, object_path, ir_path in worker_items:
        digest = hashlib.sha256()
        for field in (OBJECT_CACHE_VERSION, identity, source_identity, target_id, cc, configuration):
            digest.update(field.encode("utf-8"))
            digest.update(b"\0")
        with open(ir_path, "rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        key = digest.hexdigest()
        suffix = ".pco" if object_path.endswith(".pco") else ".s" if object_path.endswith(".s") else ".o"
        cached = os.path.join(root, key[:2], key + suffix)
        status = "miss"
        lease = ""
        try:
            if os.path.isfile(cached) and os.path.isfile(cached + ".sha256"):
                lease = acquire_entry_lease(cached)
            if lease:
                with open(cached + ".sha256", encoding="ascii") as stream:
                    expected = stream.read().strip()
                actual = _copy_digest(cached, object_path)
                if actual == expected and len(expected) == 64 and os.path.getsize(object_path) > 0:
                    with open(result_path, "w", encoding="utf-8") as stream:
                        stream.write(target_id + "\n" + object_path + "\nhit\n")
                    record_successful_access(cached)
                    status = "hit"
                else:
                    os.unlink(object_path)
        except OSError:
            status = "miss"
        finally:
            if lease:
                release_entry_lease(lease)
        result.append((cached, status))
    return result


def publish(worker_items: list[tuple[str, str, str]], cache_plan: list[tuple[str, str]],
            tmp_dir: str, *, host_python_command, publish_host_code: str) -> bool:
    if not enabled():
        return True
    return _publish_enabled(worker_items, cache_plan)


def _publish_enabled(worker_items: list[tuple[str, str, str]],
                     cache_plan: list[tuple[str, str]]) -> bool:
    from pcc.tools.compiler_cache_retention import record_successful_access
    for index, item in enumerate(worker_items):
        _result_path, object_path, _ir_path = item
        cached, status = cache_plan[index]
        if status != "miss" or not cached or not os.path.isfile(object_path):
            continue
        if not path_allowed(cached):
            return False
        temporary = cached + ".tmp." + str(os.getpid())
        checksum_temp = temporary + ".sha256"
        try:
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            digest = _copy_digest(object_path, temporary)
            with open(checksum_temp, "w", encoding="ascii") as stream:
                stream.write(digest + "\n")
            os.replace(temporary, cached)
            os.replace(checksum_temp, cached + ".sha256")
            record_successful_access(cached)
        except OSError:
            return False
        finally:
            for path in (temporary, checksum_temp):
                if os.path.exists(path):
                    os.unlink(path)
    return True
