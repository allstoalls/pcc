"""Public compiler strict closure and real host-target object emission."""

import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import time

import pytest

from tests.strict_exception_oracle import require_no_notimplemented_stub
from tests.tar_attribute_strict_oracle import (
    TAR_COMPILER_MODULES, CONSTANT_ONLY_MODULE, require_tar_attribute_module,
)


MODULES = tuple("pcc.frontends.python." + name for name in (
    "worker_resource_plan", "pipeline_frontend_workers",
    "pipeline_frontend_indexed_stage", "pipeline_indexed_handoff",
    "worker_process_pool",
)) + ("pcc.backend.self_backend_aarch64_darwin_regalloc",
      "pcc.backend.self_backend_kernel",
      "pcc.backend.self_backend_precise_stackmaps",
      "pcc.backend.self_backend_verify",
      "pcc.backend.self_backend_aarch64_darwin_flow",
      "pcc.backend.self_backend_aarch64_darwin_memory",
      "pcc.backend.self_backend_aarch64_darwin_calls",
      "pcc.backend.self_backend_aarch64_darwin_ops",
      "pcc.backend.self_backend_aarch64_darwin_prologue",
      "pcc.backend.self_backend_aarch64_darwin_returns",
      'pcc.backend.precise_stackmap',
      'pcc.backend.macho_link',
      'pcc.backend.macho_exec',
      'pcc.backend.native_object',
      'pcc.backend.macho_obj',
      'pcc.frontends.python.pipeline_self_backend_link',
      "pcc.backend.owned_elf_inputs", "pcc.backend.elf_x86_64",
      "pcc.backend.elf_aarch64_relocations",
      "pcc.backend.x86_64_asm_driver",
      "pcc.backend.x86_64_encode") + TAR_COMPILER_MODULES + (
    "pcc.backend.owned_elf_link", "pcc.backend.indexed_phase_timing",
)
SECTION = re.compile(r"^; ---- module: ([A-Za-z_][\w.]*) ----$", re.M)
DEFINITION = re.compile(r'^define[^\n]*@"?([A-Za-z_][\w.$]*)"?\(', re.M)


_DEFINITION_BODY = re.compile(
    r'^define[^\n]*@"?(?P<symbol>[A-Za-z_][\w.$]*)"?\([^\n]*\)[^\n]*\{\n(?P<body>.*?)^\}',
    re.M | re.S,
)
_RECEIPT_MAX_BYTES = 1024 * 1024


def _index_definition_bodies(text):
    bodies = {}
    for match in _DEFINITION_BODY.finditer(text):
        header = text[match.start():match.start("body")]
        if header.count("@") != 1 or re.search(r"^define\b", match.group("body"), re.M):
            # Ambiguous headers and nested definitions can match differently
            # for each symbol. Preserve the original exact-symbol matcher.
            return None
        bodies.setdefault(match.group("symbol"), []).append(match)
    return bodies


_COMPILE_PROFILE_PHASES = (
    "collect_multi_source_relative_closure", "filter_ir_scaffold_closure",
    "validate_package_site_abi", "frontend_imports", "order_module_inits",
    "build_closed_world_context", "build_closed_world_context_import_py_ast",
    "build_closed_world_context_import_py_lift", "build_closed_world_context_parse",
    "build_closed_world_context_lift", "build_closed_world_context_module",
    "multi_type_infer", "multi_codegen_layer1", "python_ir_pass_pipeline_many",
    "libpython_scan", "emit_ll_many_combined", "compile_python_multi_total",
)
_COMPILE_PROFILE_MODULE_PHASES = (
    "build_closed_world_context_parse", "build_closed_world_context_lift",
    "build_closed_world_context_module", "multi_type_infer", "multi_codegen_layer1",
)
_COMPILE_PROFILE_COUNTERS = (
    "multi_input_files", "multi_files", "multi_frontend_jobs",
    "multi_ir_bytes_before_passes", "multi_ir_modules", "multi_ir_bytes",
)
_COMPILE_PROFILE_MODULE_LIMIT = 8


def _bounded_compile_profile(profile, *, complete):
    """Snapshot existing timers after return/exception; no live compiler hook."""
    def bounded_count(value):
        return type(value) is int and 0 <= value <= 9223372036854775807

    totals = profile.get("phase_totals_ms", {})
    counters = profile.get("counters", {})
    events = profile.get("events", [])
    if not isinstance(totals, dict):
        totals = {}
    if not isinstance(counters, dict):
        counters = {}
    if not isinstance(events, list):
        events = []
    phase_totals = {name: totals[name] for name in _COMPILE_PROFILE_PHASES
                    if name in totals and bounded_count(totals[name])}
    safe_counters = {name: counters[name] for name in _COMPILE_PROFILE_COUNTERS
                     if name in counters and bounded_count(counters[name])}
    module_events = {name: [] for name in _COMPILE_PROFILE_MODULE_PHASES}
    event_counts = {name: 0 for name in _COMPILE_PROFILE_MODULE_PHASES}
    for event in events:
        if not isinstance(event, dict):
            continue
        phase, detail, duration = event.get("name"), event.get("detail"), event.get("ms")
        if not isinstance(phase, str) or phase not in module_events:
            continue
        if (not isinstance(detail, str) or len(detail) > 160
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", detail) is None
                or not bounded_count(duration)):
            continue
        event_counts[phase] += 1
        rows = module_events[phase]
        rows.append({"module": detail, "ms": duration})
        rows.sort(key=lambda row: (-row["ms"], row["module"]))
        del rows[_COMPILE_PROFILE_MODULE_LIMIT:]
    return {
        "schema": "pcc.strict-compile-profile.v1",
        "capture": "return-or-python-exception",
        "complete": complete,
        "phase_totals_ms": phase_totals,
        "counters": safe_counters,
        "module_events": module_events,
        "module_event_counts": event_counts,
        "module_events_truncated": any(count > _COMPILE_PROFILE_MODULE_LIMIT
                                       for count in event_counts.values()),
    }


def _publish_closed_world_receipt(path, receipt):
    payload = (json.dumps(receipt, indent=2, allow_nan=False) + "\n").encode("utf-8")
    assert len(payload) <= _RECEIPT_MAX_BYTES, "closed-world receipt exceeds byte bound"
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record_closed_world_progress(path, receipt, started, phase, state, module=""):
    assert phase in ("compile", "whole-verify", "whole-parse", "whole-verify-parsed",
                     "module-verify", "module-emit")
    assert state in ("started", "complete")
    assert (module in MODULES) if phase.startswith("module-") else module == ""
    progress = receipt.setdefault("progress", [])
    assert len(progress) < 8 + 4 * len(MODULES), "closed-world progress exceeds phase bound"
    progress.append({"phase": phase, "state": state, "module": module,
                     "elapsed_s": time.monotonic() - started})
    _publish_closed_world_receipt(path, receipt)


def require_definition(text, symbol, *, bodies=None):
    pattern = (r'^define[^\n]*@"?' + re.escape(symbol)
               + r'"?\([^\n]*\)[^\n]*\{\n(?P<body>.*?)^\}')
    # A missing/unindexable spelling uses the exact original matcher. Normal
    # complete sections reuse one scan; duplicate definitions remain a list.
    matches = bodies.get(symbol) if bodies is not None else None
    if matches is None:
        matches = list(re.finditer(pattern, text, re.M | re.S))
    assert len(matches) == 1, (symbol, "missing/duplicate definition")
    body = matches[0].group("body")
    assert not re.search(r"^define\b", body, re.M), (symbol, "cross-function match")
    assert "strict.nolib" not in body and "@py_cpy_" not in body and '@"py_cpy_' not in body
    require_no_notimplemented_stub(body, symbol)
    assert re.search(r"\bcall\b", body), (symbol, "empty/return-only replacement")


def require_object_symbols(payload, target, required):
    """Owned readers validate the emitted container and every required code symbol."""
    darwin, windows = "-apple-" in target, "-windows-" in target
    if darwin:
        from pcc.backend.native_object import decode_native_object
        obj = decode_native_object(payload)
        machine = "PCO has no machine field; verified PIDX target and Darwin emitter route"
    elif windows:
        from pcc.backend.coff_x86_64 import parse_object
        obj = parse_object(payload)  # The reader requires AMD64 machine 0x8664.
        machine = 0x8664
    else:
        from pcc.backend.elf_x86_64 import parse_relocatable
        obj = parse_relocatable(payload)
        machine = 62 if target.startswith("x86_64-") else 183
        assert obj.machine == machine, "wrong ELF machine"
    symbols_by_name = {}
    for symbol in obj.symbols:
        symbols_by_name.setdefault(symbol.name, []).append(symbol)
    for name in required:
        symbols = symbols_by_name.get(("_" if darwin else "") + name, [])
        assert len(symbols) == 1, (name, "missing/duplicate object definition")
        symbol = symbols[0]
        index = symbol.section if windows else symbol.section_index
        assert 1 <= index <= len(obj.sections), (name, "undefined object symbol")
        section = obj.sections[index - 1]
        offset = symbol.offset if darwin else symbol.value
        assert 0 <= offset < len(section.data), (name, "symbol outside code")
        if darwin:
            assert symbol.external and (section.segname, section.sectname) == ("__TEXT", "__text")
        elif windows:
            assert symbol.external and symbol.function and section.flags & 0x20000000
        else:
            assert symbol.binding == 1 and symbol.type == 2 and section.flags & 4
    return {"machine": machine, "required_defined_symbols": list(required)}


@pytest.mark.integration
def test_worker_size_prior_modules_strict_target_emission(tmp_path, monkeypatch):
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
    from pcc.backend.self_backend_indexed_emit import emit_indexed_module_file
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from pcc.tools.runtime_archive_provenance import codegen_checksum
    from tests.owned_ir_validation import verify_ir_text

    root = Path(__file__).resolve().parents[2]
    sources = [root / (name.replace(".", "/") + ".py") for name in MODULES]
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest()
               for name, path in zip(MODULES, sources)}
    target = host_target_triple()
    assert target.startswith(("arm64-apple-", "aarch64-apple-", "aarch64-unknown-linux-",
                              "arm64-unknown-linux-", "x86_64-unknown-linux-", "x86_64-pc-windows-")), target
    # Ordinary public closure construction with no fake exports and no old PIDX.
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASS_JOBS", "1")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                 "PCC_HOST_INDEXED_PROCESS_SPLIT"):
        monkeypatch.setenv(name, "0")
    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.delenv("PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES", raising=False)
    compile_profile = {}
    compile_complete = False
    primary_failed = False
    receipt = {"status": "RUNNING", "target": target, "sources": digests,
               "codegen_sha256": codegen_checksum(), "modules": {},
               "scope": f"public strict full closure and {len(MODULES)} actual target objects",
               "native_execution": False,
               "compile_profile": _bounded_compile_profile(compile_profile, complete=False)}
    output = tmp_path / "closed-world.ll"
    receipt_path = tmp_path / "worker-size-prior-closed-world.json"
    started = time.monotonic()
    try:
        _record_closed_world_progress(receipt_path, receipt, started, "compile", "started")
        compile_python_multi(
            [str(path) for path in sources], str(output), module_names=list(MODULES),
            entry_module=MODULES[0], emit_llvm_only=True, target_triple=target,
            libpython_mode="off", ir_scaffold_mode="on", backend="self",
            recursive_stdlib=False, profile=compile_profile,
        )
        receipt["compile_profile"] = _bounded_compile_profile(compile_profile, complete=True)
        compile_complete = True
        del compile_profile
        _record_closed_world_progress(receipt_path, receipt, started, "compile", "complete")
        _record_closed_world_progress(receipt_path, receipt, started, "whole-verify", "started")
        text = output.read_text()
        assert "strict.nolib.stub" not in text
        assert not re.search(r'\b(?:call|invoke)\b[^\n]*@"?py_cpy_', text)
        markers = list(SECTION.finditer(text))
        names = [match.group(1) for match in markers]
        assert len(names) == len(set(names)) and set(MODULES) <= set(names)
        assert "pcc.frontends.python.worker_resource_host_windows" not in names
        assert "pcc.backend.stackmap_host_budget" not in names
        assert "pcc.frontends.python.worker_resource_host_recovery" not in names
        assert "pcc.frontends.python.worker_process_host_pool" not in names
        assert "pcc.frontends.python.worker_resource_host_rss" not in names
        whole = verify_ir_text(
            text, progress=lambda phase, state: _record_closed_world_progress(
                receipt_path, receipt, started,
                {"parse": "whole-parse", "verify": "whole-verify-parsed"}[phase], state,
            ),
        )
        assert Counter(function.name for function in whole.functions) == Counter(DEFINITION.findall(text))
        receipt["closure_modules"] = names
        receipt["closure_definitions"] = len(whole.functions)
        del whole
        sections = {match.group(1): text[match.end():markers[index + 1].start()
                    if index + 1 < len(markers) else len(text)]
                    for index, match in enumerate(markers) if match.group(1) in MODULES}
        del text, markers
        _record_closed_world_progress(receipt_path, receipt, started, "whole-verify", "complete")
        for name, source in zip(MODULES, sources):
            _record_closed_world_progress(receipt_path, receipt, started, "module-verify", "started", name)
            declared = [node.name for node in ast.parse(source.read_bytes()).body
                        if isinstance(node, ast.FunctionDef)]
            if name == CONSTANT_ONLY_MODULE:
                assert declared == [], "constant-table module acquired unqualified functions"
            else:
                assert declared and len(declared) == len(set(declared))
            section = sections.pop(name)
            required = ["user_" + name.replace(".", "_") + "_" + function for function in declared]
            definition_bodies = _index_definition_bodies(section)
            if name == "pcc.frontends.python.worker_resource_plan":
                # Compile the actual dispatcher and reader in the existing
                # native strict gate. The host Win32 adapter cannot leak in.
                assert {"_open_worker_resource", "read_worker_resource",
                        "_replace_worker_resource", "publish_worker_resource"} <= set(declared)
                assert "worker_resource_host_windows" not in section
                for function, callee in (("_open_worker_resource", "py_file_open_options"),
                                         ("read_worker_resource", "_open_worker_resource"),
                                         ("publish_worker_resource", "_replace_worker_resource"),
                                         ("_replace_worker_resource", "py_os_replace"),
                                         ("_replace_worker_resource", "pcc_worker_resource_replace")):
                    symbol = "user_" + name.replace(".", "_") + "_" + function
                    require_definition(section, symbol, bodies=definition_bodies)
                    matches = (definition_bodies.get(symbol, []) if definition_bodies is not None
                               else [match for match in _DEFINITION_BODY.finditer(section)
                                     if match.group("symbol") == symbol])
                    assert len(matches) == 1
                    assert re.search(r'\bcall\b[^\n]*' + re.escape(callee), matches[0].group("body"))
                    del matches
            if name == "pcc.frontends.python.worker_process_pool":
                assert {"run_resource_worker_processes", "_host_pool",
                        "_host_weighted_pool", "_host_chained_pool"} <= set(declared)
                assert "worker_resource_host_recovery" not in section
                assert "worker_process_host_pool" not in section
            if name == "pcc.frontends.python.pipeline_frontend_workers":
                assert {"_host_coordinator_rss_bytes", "_host_windows_rss_bytes"} <= set(declared)
                assert "worker_resource_host_rss" not in section
            if name == "pcc.backend.x86_64_encode":
                initializer = "_pcc_py_module_top_pcc_backend_x86_64_encode"
                required.append(initializer)
                require_definition(section, initializer, bodies=definition_bodies)
                matches = (definition_bodies.get(initializer, []) if definition_bodies is not None
                           else [match for match in _DEFINITION_BODY.finditer(section)
                                 if match.group("symbol") == initializer])
                assert len(matches) == 1
                initialization = matches[0].group("body")
                # Actual production globals must be initialized in the owned
                # module body, not only present as dormant declarations.
                for table in ("_X87_FIXED", "_X87_STACK"):
                    symbol = ".modvar.pcc_backend_x86_64_encode." + table
                    assert re.search(r'^@"?' + re.escape(symbol) + r'"?\s*=\s*[^\n]*\bglobal ptr\b',
                                     section, re.M), symbol
                    assert symbol in initialization, symbol
                assert len(re.findall(r'\bcall\b[^\n]*@"?py_dict_new(?:_presized)?"?\(', initialization)) >= 2
                assert len(re.findall(r'\bcall\b[^\n]*@"?py_bytes_new"?\(', initialization)) >= 16
                assert re.search(r'\bcall\b[^\n]*@"?py_module_attr_set"?\(', initialization)
                del matches, initialization
            for symbol in required:
                require_definition(section, symbol, bodies=definition_bodies)
            required.extend(require_tar_attribute_module(
                name, source.read_text(), section, sections,
                require_definition, bodies=definition_bodies,
            ))
            del definition_bodies
            module = verify_ir_text(section)
            assert module.triple == target
            assert Counter(function.name for function in module.functions) == Counter(DEFINITION.findall(section))
            assert all(function.indexed_kernel is not None for function in module.functions)
            _record_closed_world_progress(receipt_path, receipt, started, "module-verify", "complete", name)
            sidecar, obj = tmp_path / (name + ".pidx"), tmp_path / (name + ".pco")
            _record_closed_world_progress(receipt_path, receipt, started, "module-emit", "started", name)
            encode_indexed_module_file(str(sidecar), module)
            del module
            emit_indexed_module_file(str(sidecar), str(obj), "PCO", optimize=False)
            assert obj.stat().st_size > 0 and not Path(str(obj) + ".tmp").exists()
            object_contract = require_object_symbols(obj.read_bytes(), target, required)
            receipt["modules"][name] = {"object_contract": object_contract, "functions": declared, "ir_sha256": hashlib.sha256(section.encode()).hexdigest(),
                                       "object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest(),
                                       "object_bytes": obj.stat().st_size}
            del section
            _record_closed_world_progress(receipt_path, receipt, started, "module-emit", "complete", name)
        assert digests == {name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for name, path in zip(MODULES, sources)}
        receipt["status"] = "PASS"
    except BaseException as error:
        primary_failed = True
        receipt.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        try:
            if not compile_complete:
                receipt["compile_profile"] = _bounded_compile_profile(compile_profile, complete=False)
            _publish_closed_world_receipt(receipt_path, receipt)
        except BaseException:
            # Preserve the compiler/verification exception already unwinding.
            # A reporting failure on an otherwise normal path stays fatal.
            if not primary_failed:
                raise
