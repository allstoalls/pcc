"""Self-backend native-link orchestration behind the pipeline facade."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
import traceback

from .pipeline_modes import failed_process_detail
from typing import Optional


class SelfBackendLinkError(RuntimeError):
    """The selected self-backend link contract could not be completed."""


_failed_process_detail = failed_process_detail


def link_paths(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
    normalize_ir,
    link_ir_texts,
    profile_begin,
    profile_end,
) -> None:
    """Read LLVM modules and delegate the native self-backend link."""
    normalized_paths: list[str] = []
    for path in ll_paths:
        normalized_paths.append(str(path))
    normalized_out = str(out_path)
    normalized_runtime = (
        None if runtime_archive is None else str(runtime_archive)
    )

    with tempfile.TemporaryDirectory(prefix="pcc_py_self_") as tmp:
        ir_texts: list[str] = []
        started = profile_begin(profile)
        for ll_path in normalized_paths:
            with open(ll_path, "r", encoding="utf-8") as stream:
                ir_texts.append(normalize_ir(stream.read()))
        profile_end(profile, "link_self_read_ll", started)
        link_ir_texts(
            ir_texts,
            normalized_out,
            normalized_runtime,
            verbose,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
            tmp_dir=tmp,
            profile=profile,
        )


def finish_executable(
    tmp_out_path: str,
    out_path: str,
    profile,
    *,
    signature_owned_by_pcc: bool = False,
    profile_begin,
    profile_end,
    publish_sync_enabled,
) -> None:
    """Publish an executable without replacing a pcc-owned signature."""
    if sys.platform == "darwin" and not signature_owned_by_pcc:
        started = profile_begin(profile)
        subprocess.run(
            ["/usr/bin/codesign", "--force", "-s", "-", tmp_out_path],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--verify", tmp_out_path],
            check=True,
        )
        profile_end(profile, "link_self_codesign", started)

    started = profile_begin(profile)
    os.replace(tmp_out_path, out_path)
    profile_end(profile, "link_self_publish_move", started)
    if sys.platform != "darwin":
        return

    if not signature_owned_by_pcc:
        started = profile_begin(profile)
        subprocess.run(["/usr/bin/codesign", "--verify", out_path], check=True)
        profile_end(profile, "link_self_codesign", started)
    started = profile_begin(profile)
    with open(out_path, "rb") as stream:
        if publish_sync_enabled():
            os.fsync(stream.fileno())
        else:
            while stream.read(1024 * 1024):
                pass
    profile_end(profile, "link_self_publish_barrier", started)


def _owned_macho_link_covers_surface(
    *,
    asm_path,
    pcc_asm_inputs,
    pcc_native_object_inputs,
    pcc_internal_input_manifest,
    semantic_layout_policy,
    link_profile_path,
) -> bool:
    """True when the in-process owned Mach-O link covers this request.

    The owned in-process entry assembles one or more internal inputs, adds
    caller object inputs and one runtime archive, and links an executable.
    Ordered input manifests and profiles use this same owner. Semantic layout
    remains a separate migration boundary.
    """
    if semantic_layout_policy:
        return False
    if pcc_internal_input_manifest:
        return True
    if pcc_asm_inputs and pcc_native_object_inputs:
        return False
    if not asm_path and not pcc_asm_inputs and not pcc_native_object_inputs:
        return False
    return True


def _owned_macho_link_in_process(
    *,
    asm_path,
    pcc_asm_inputs,
    pcc_native_object_inputs,
    runtime_archive,
    extra_link_inputs,
    tmp_out_path,
    link_profile_path=None,
    pcc_internal_input_manifest=None,
) -> None:
    """Link with pcc's owned Mach-O toolchain in this process.

    The Mach-O implementation is imported here rather than at module import so
    a stage that never links pcc-owned output does not pull it in. Removing
    the host-Python link seam is the point: an installed native pcc1 has no
    interpreter to run ``scripts/pcc_link_macho.py``.
    """
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.macho_exec import prepare_executable_object, link_prepared_executable
    from pcc.backend.macho_codesign import parse_signature, build_signature
    from pcc.backend.native_object import NativeObject, decode_packed_native_object
    from pcc.backend.macho_internal_inputs import read_internal_input_manifest

    try:
        started = time.monotonic()
        phases = {"assemble_pool": 0.0, "decode_pco": 0.0, "prepare_link": 0.0,
                  "sign": 0.0, "validate": 0.0, "write": 0.0}
        objects = []
        if pcc_internal_input_manifest:
            if asm_path or pcc_asm_inputs or pcc_native_object_inputs:
                raise SelfBackendLinkError("ordered internal-input manifest cannot be combined with direct internal inputs")
            ordered = read_internal_input_manifest(pcc_internal_input_manifest)
        else:
            ordered = [("ASM", asm_path)] if asm_path else []
            ordered.extend(("ASM", path) for path in pcc_asm_inputs)
            ordered.extend(("PCO", path) for path in pcc_native_object_inputs)
        asm_count = 0
        native_count = 0
        for kind, path in ordered:
            item_started = time.monotonic()
            if kind == "ASM":
                with open(path, "r", encoding="utf-8") as stream:
                    sections, undefined = assemble_file(stream.read())
                objects.append(NativeObject.from_sections(sections, undefined=undefined))
                asm_count += 1
                phases["assemble_pool"] += (time.monotonic() - item_started) * 1000.0
            else:
                with open(path, "rb") as stream:
                    objects.append(decode_packed_native_object(stream.read()))
                native_count += 1
                phases["decode_pco"] += (time.monotonic() - item_started) * 1000.0
        before_extras = time.monotonic()
        for path in extra_link_inputs or ():
            with open(path, "rb") as stream:
                objects.append(stream.read())
        archives = []
        if runtime_archive is not None:
            with open(runtime_archive, "rb") as stream:
                archives.append(stream.read())
        before_link = time.monotonic()
        phases["decode_pco"] += (before_link - before_extras) * 1000.0
        pending = [prepare_executable_object(objects, archives=archives)]
        objects.clear()
        archives.clear()
        sign_times = [0.0, 0.0]

        def link_phase(event):
            if event == "sign_begin":
                sign_times[0] = time.monotonic()
            elif event == "sign_end":
                sign_times[1] += (time.monotonic() - sign_times[0]) * 1000.0

        image = link_prepared_executable(pending.pop(), entry="_main", phase_callback=link_phase)
        before_validate = time.monotonic()
        phases["sign"] = sign_times[1]
        phases["prepare_link"] = (before_validate - before_link) * 1000.0 - sign_times[1]
        signature = parse_signature(image)
        if signature.identifier != b"pcc-linked" or signature.dataoff + signature.datasize != len(image):
            raise SelfBackendLinkError("owned linker produced an invalid signature boundary")
        expected_signature = build_signature(
            image[:signature.dataoff], identifier=signature.identifier,
            exec_seg_base=signature.exec_seg_base, exec_seg_limit=signature.exec_seg_limit,
            exec_seg_flags=signature.exec_seg_flags,
        )
        if image[signature.dataoff:] != expected_signature:
            raise SelfBackendLinkError("owned linker produced stale signature page hashes")
        before_write = time.monotonic()
        phases["validate"] = (before_write - before_validate) * 1000.0
        temporary = str(tmp_out_path) + ".owned.tmp"
        with open(temporary, "wb") as stream:
            stream.write(image)
        os.chmod(temporary, 0o755)
        os.replace(temporary, str(tmp_out_path))
        finished = time.monotonic()
        phases["write"] = (finished - before_write) * 1000.0
        if link_profile_path:
            payload = {
                "schema": "pcc.macho-link-profile.v1",
                "phases_ms": phases,
                "total_ms": (finished - started) * 1000.0,
                "inputs": {
                    "asm": asm_count,
                    "native_object": native_count,
                    "object": len(extra_link_inputs or ()),
                    "archive": 1 if runtime_archive is not None else 0,
                },
            }
            with open(link_profile_path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
    except SelfBackendLinkError:
        raise
    except Exception as exc:
        # This link runs in-process, so the traceback -- not a child exit
        # code -- is the diagnosis.  `failed_process_detail` describes a
        # failed *child process*; on this path it reduces a real linker bug
        # to a bare type and message, which is how a stage that rebuilt and
        # failed nine times in a row reported no reason at all.
        raise SelfBackendLinkError(
            "owned in-process link failed: "
            + type(exc).__name__ + ": " + str(exc)
            + "\n"
            + traceback.format_exc()
        ) from exc


def run_link_command(
    cmd,
    asm_path: Optional[str],
    tmp_out_path,
    runtime_archive,
    extra_link_inputs,
    verbose,
    *,
    extra_link_args: tuple[str, ...] = (),
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    pcc_asm_inputs: tuple[str, ...] = (),
    pcc_native_object_inputs: tuple[str, ...] = (),
    pcc_internal_input_manifest: Optional[str] = None,
    semantic_layout_policy: Optional[str] = None,
    link_profile_path: Optional[str] = None,
    resolve_self_link_mode,
    validate_pcc_self_link_surface,
    repo_root_for_link,
    host_python_command,
    build_pcc_link_command,
    log,
    join_strings,
) -> None:
    """Run exactly the selected linker; never fall back after selection."""
    selected_mode = resolve_self_link_mode()
    if semantic_layout_policy and (
        selected_mode != "pcc" or sys.platform != "darwin"
    ):
        raise SelfBackendLinkError(
            "Mach-O semantic layout requires the pcc-owned Darwin linker"
        )
    if selected_mode != "pcc":
        subprocess.run(cmd, check=True)
        return

    validate_pcc_self_link_surface(
        extra_link_args=extra_link_args,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
    )
    linux_elf = sys.platform.startswith("linux")
    if not linux_elf and _owned_macho_link_covers_surface(
        asm_path=asm_path,
        pcc_asm_inputs=pcc_asm_inputs,
        pcc_native_object_inputs=pcc_native_object_inputs,
        pcc_internal_input_manifest=pcc_internal_input_manifest,
        semantic_layout_policy=semantic_layout_policy,
        link_profile_path=link_profile_path,
    ):
        log(verbose, "pcc link (in-process owned Mach-O): " + str(tmp_out_path))
        _owned_macho_link_in_process(
            asm_path=asm_path,
            pcc_asm_inputs=tuple(str(path) for path in pcc_asm_inputs),
            pcc_native_object_inputs=tuple(
                str(path) for path in pcc_native_object_inputs
            ),
            runtime_archive=(
                None if runtime_archive is None else str(runtime_archive)
            ),
            extra_link_inputs=tuple(
                str(path) for path in (extra_link_inputs or ())
            ),
            tmp_out_path=str(tmp_out_path),
            link_profile_path=link_profile_path,
            pcc_internal_input_manifest=pcc_internal_input_manifest,
        )
        if not os.path.isfile(tmp_out_path) or not os.access(tmp_out_path, os.X_OK):
            raise SelfBackendLinkError(
                "owned in-process link produced no executable output"
            )
        return
    driver_name = "pcc_link_elf.py" if linux_elf else "pcc_link_macho.py"
    driver = os.path.join(repo_root_for_link(), "scripts", driver_name)
    if not os.path.isfile(driver):
        raise SelfBackendLinkError(f"pcc self-link driver is missing: {driver}")
    # A compiled pcc stage exposes itself as ``sys.executable``.  Treating
    # that native compiler as the Python owner for ``pcc_link_macho.py`` makes
    # it recursively parse the link driver's ``--out`` argument as a pcc CLI
    # option.  Resolve the host interpreter through the same source/install
    # policy used by every other frontend subprocess instead.
    host_python = str(host_python_command() or "").strip()
    if not host_python:
        raise SelfBackendLinkError(
            "pcc self-link mode requires a host Python executable"
        )
    if pcc_asm_inputs and pcc_native_object_inputs:
        raise SelfBackendLinkError(
            "pcc self-link cannot mix internal assembly and native objects"
        )
    if pcc_internal_input_manifest and (
        asm_path or pcc_asm_inputs or pcc_native_object_inputs
    ):
        raise SelfBackendLinkError(
            "ordered internal-input manifest cannot be combined with direct "
            "internal inputs"
        )
    if pcc_internal_input_manifest and linux_elf:
        raise SelfBackendLinkError(
            "ordered mixed internal inputs are implemented only by Mach-O"
        )
    internal_inputs = (
        pcc_native_object_inputs if pcc_native_object_inputs else pcc_asm_inputs
    )
    internal_input_flag = (
        "--native-object" if pcc_native_object_inputs else "--asm"
    )
    link_cmd = build_pcc_link_command(
        host_python=host_python,
        driver=driver,
        output=str(tmp_out_path),
        asm_path=asm_path,
        internal_asm_inputs=tuple(str(path) for path in internal_inputs),
        runtime_archive=(
            None if runtime_archive is None else str(runtime_archive)
        ),
        extra_link_inputs=tuple(str(path) for path in (extra_link_inputs or ())),
        internal_input_flag=internal_input_flag,
        internal_input_manifest=pcc_internal_input_manifest,
        semantic_layout_policy=semantic_layout_policy,
        profile_json=link_profile_path,
    )
    log(verbose, "pcc link: " + join_strings(link_cmd, " "))
    try:
        subprocess.run(link_cmd, check=True)
    except FileNotFoundError as exc:
        raise SelfBackendLinkError(
            f"pcc self-link host Python not found: {host_python}"
        ) from exc
    if not os.path.isfile(tmp_out_path) or not os.access(tmp_out_path, os.X_OK):
        raise SelfBackendLinkError(
            "pcc self-link driver returned success without an executable "
            "regular output file"
        )


def link_ir_texts_run(
    ir_texts: list[str],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    tmp: str,
    profile,
    consume_ir_texts: bool = False,
    resolve_self_link_mode,
    validate_pcc_self_link_surface,
    profile_begin,
    profile_end,
    debug_dump_ir_texts,
    split_large_modules_enabled,
    split_threshold_bytes,
    emit_asm,
    emit_objects,
    runtime_archive_link_args,
    native_extension_export_flags,
    libpython_isolation_flags,
    platform_link_flags,
    append_libpython_link_flags,
    log,
    join_strings,
    run_self_link_command,
    finish_self_backend_executable,
    semantic_layout_enabled,
    write_semantic_layout_policy,
) -> None:
    signature_owned_by_pcc = resolve_self_link_mode() == "pcc"
    # Owned Darwin and Linux links consume internal assembly.  Their drivers
    # encode directly into Mach-O/ELF objects; external object inputs remain an
    # explicit, separately labelled boundary.
    pcc_elf_link = signature_owned_by_pcc and sys.platform.startswith("linux")
    link_profile = "link_self_pcc" if signature_owned_by_pcc else "link_self_cc"
    validate_pcc_self_link_surface(
        extra_link_args=extra_link_args,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
    )
    export_pcc_capi = needs_native_extension_exports and not needs_libpython
    asm_modules: list[str] = []
    needs_subsections_via_symbols = False
    input_ir_texts: list[str] = []
    started = profile_begin(profile)
    for text in ir_texts:
        input_ir_texts.append(str(text))
    profile_end(profile, "link_self_normalize_ir", started)
    debug_dump_ir_texts(input_ir_texts)
    split_large_modules = split_large_modules_enabled()
    has_large_module = False
    started = profile_begin(profile)
    if split_large_modules:
        threshold = split_threshold_bytes()
        for ir_text in input_ir_texts:
            if len(ir_text) >= threshold:
                has_large_module = True
                break
    profile_end(profile, "link_self_split_scan", started)
    semantic_layout_policy = None
    if semantic_layout_enabled():
        if has_large_module and split_large_modules:
            raise SelfBackendLinkError(
                "Mach-O semantic layout does not yet own split-module symbol "
                "renaming; reduce the module or disable the opt-in layout pass"
            )
        semantic_layout_policy = str(
            os.path.join(tmp, "macho-semantic-layout-policy.json")
        )
        write_semantic_layout_policy(
            semantic_layout_policy, input_ir_texts
        )
    cc = str(os.environ.get("CC", "") or "").strip() or "cc"

    if len(input_ir_texts) == 1 and not has_large_module and not pcc_elf_link:
        started = profile_begin(profile)
        host_results = [emit_asm(input_ir_texts[0], tmp, 0)]
        profile_end(profile, "link_self_emit_asm_host", started)
        if consume_ir_texts:
            input_ir_texts.clear()
            ir_texts.clear()
            text = ""
            ir_text = ""
    else:
        started = profile_begin(profile)
        object_results = emit_objects(
            input_ir_texts,
            tmp,
            cc,
            split_large_modules=split_large_modules and has_large_module,
            profile=profile,
            internal_link=signature_owned_by_pcc,
        )
        profile_end(profile, "link_self_emit_objects_host", started)
        if consume_ir_texts:
            input_ir_texts.clear()
            ir_texts.clear()
            text = ""
            ir_text = ""
        obj_paths: list[str] = []
        for target_id, obj_path in object_results:
            if target_id == "self-aarch64-darwin-v0":
                needs_subsections_via_symbols = True
            obj_paths.append(obj_path)
        tmp_out_path = out_path + ".tmp"
        cmd = [cc] + obj_paths + list(extra_link_inputs)
        if runtime_archive is not None:
            cmd.extend(runtime_archive_link_args(runtime_archive, export_pcc_capi))
        cmd.extend(["-o", tmp_out_path, "-lm"])
        cmd.extend(extra_link_args)
        cmd.extend(native_extension_export_flags(export_pcc_capi))
        cmd.extend(libpython_isolation_flags(runtime_archive, needs_libpython))
        if sys.platform == "darwin" and needs_subsections_via_symbols:
            cmd.append("-Wl,-dead_strip")
        cmd.extend(platform_link_flags())
        if needs_libpython:
            append_libpython_link_flags(cmd)
        log(verbose, "self link: " + join_strings(cmd, " "))
        try:
            total_started = profile_begin(profile)
            started = profile_begin(profile)
            run_self_link_command(
                cmd,
                None,
                tmp_out_path,
                runtime_archive,
                extra_link_inputs,
                verbose,
                extra_link_args=extra_link_args,
                needs_libpython=needs_libpython,
                needs_native_extension_exports=export_pcc_capi,
                pcc_asm_inputs=tuple(obj_paths) if signature_owned_by_pcc else (),
                semantic_layout_policy=semantic_layout_policy,
            )
            profile_end(profile, link_profile + "_driver", started)
            finish_self_backend_executable(
                tmp_out_path,
                out_path,
                profile,
                signature_owned_by_pcc=signature_owned_by_pcc,
            )
            profile_end(profile, link_profile, total_started)
        except FileNotFoundError as exc:
            raise SelfBackendLinkError(
                f"{cc} not found on PATH; cannot link Python frontend output"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise SelfBackendLinkError(
                "self backend link failed: " + _failed_process_detail(exc)
            ) from exc
        return

    started = profile_begin(profile)
    for target_id, asm_text in host_results:
        asm_lines = asm_text.splitlines()
        if asm_lines and asm_lines[-1] == ".subsections_via_symbols":
            asm_lines = asm_lines[:-1]
        if target_id == "self-aarch64-darwin-v0":
            needs_subsections_via_symbols = True
        asm_modules.append("\n".join(asm_lines).strip())

    asm_text = "\n\n".join(fragment for fragment in asm_modules if fragment)
    if needs_subsections_via_symbols:
        asm_text += "\n.subsections_via_symbols\n"
    asm_path = str(os.path.join(tmp, "self_backend.s"))
    with open(asm_path, "w", encoding="utf-8") as stream:
        stream.write(asm_text)
    profile_end(profile, "link_self_asm_join_write", started)
    tmp_out_path = out_path + ".tmp"
    cmd = [cc, asm_path, *extra_link_inputs, "-o", tmp_out_path, "-lm"]
    cmd.extend(extra_link_args)
    cmd.extend(native_extension_export_flags(export_pcc_capi))
    if sys.platform == "darwin" and needs_subsections_via_symbols:
        cmd.append("-Wl,-dead_strip")
    cmd.extend(platform_link_flags())
    if runtime_archive is not None:
        cmd[2:2] = runtime_archive_link_args(runtime_archive, export_pcc_capi)
    cmd.extend(libpython_isolation_flags(runtime_archive, needs_libpython))
    if needs_libpython:
        append_libpython_link_flags(cmd)
    log(verbose, "self link: " + join_strings(cmd, " "))
    try:
        total_started = profile_begin(profile)
        started = profile_begin(profile)
        run_self_link_command(
            cmd,
            asm_path,
            tmp_out_path,
            runtime_archive,
            extra_link_inputs,
            verbose,
            extra_link_args=extra_link_args,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=export_pcc_capi,
            semantic_layout_policy=semantic_layout_policy,
        )
        profile_end(profile, link_profile + "_driver", started)
        finish_self_backend_executable(
            tmp_out_path,
            out_path,
            profile,
            signature_owned_by_pcc=signature_owned_by_pcc,
        )
        profile_end(profile, link_profile, total_started)
    except FileNotFoundError as exc:
        raise SelfBackendLinkError(
            f"{cc} not found on PATH; cannot link Python frontend output"
        ) from exc
    except subprocess.CalledProcessError as exc:
        # Guarded read: an unguarded `exc.returncode` here raised
        # AttributeError while reporting the failure under pcc1.
        raise SelfBackendLinkError(
            "self backend link failed: " + _failed_process_detail(exc)
        ) from exc


def link_ir_texts(
    ir_texts: list[str],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    tmp_dir: Optional[str] = None,
    profile: Optional[dict] = None,
    consume_ir_texts: bool = False,
    link_run,
) -> None:
    normalized_out = str(out_path)
    normalized_runtime = (
        None if runtime_archive is None else str(runtime_archive)
    )

    if tmp_dir is None:
        with tempfile.TemporaryDirectory(prefix="pcc_py_self_") as tmp:
            link_run(
                ir_texts,
                normalized_out,
                normalized_runtime,
                verbose,
                needs_libpython=needs_libpython,
                needs_native_extension_exports=needs_native_extension_exports,
                extra_link_inputs=extra_link_inputs,
                extra_link_args=extra_link_args,
                tmp=tmp,
                profile=profile,
                consume_ir_texts=consume_ir_texts,
            )
        return
    link_run(
        ir_texts,
        normalized_out,
        normalized_runtime,
        verbose,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
        extra_link_inputs=extra_link_inputs,
        extra_link_args=extra_link_args,
        tmp=str(tmp_dir),
        profile=profile,
        consume_ir_texts=consume_ir_texts,
    )
