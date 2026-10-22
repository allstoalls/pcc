"""Opt-in host-only profile of one cli_bootstrap assembly, ARM backend or PCO stage.

Only the admitted Stage1 worker/coordinator imports PCC. No production module
imports this adapter. The profiler observes Python and C calls: its overhead
and internal memory are not bounded by the bounded report. This is never a
baseline performance measurement. A killed attempt may leave only claim.json.
"""

import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import sys

MODULE = "pcc.driver.cli_bootstrap"
TARGET = "x86_64-unknown-linux-gnu"
ROUTE = "indexed-assembly"
FLAG = "PCC_HOST_ASSEMBLY_PROFILE"
ELF_FLAG = "PCC_HOST_ELF_STAGE_PROFILE"
ARM_FLAG = "PCC_HOST_ARM_BACKEND_PROFILE"
ARM_SCHEMA = "pcc.host-arm-backend-function-profile.v1"
ARM_TARGET = "aarch64-unknown-linux-gnu"
ARM_ROUTE = "indexed-native-sections"
ARM_CAPTURE = "emit_aarch64_darwin_indexed_transport"
ARM_OWNERS = ("pcc.backend.self_backend_aarch64_darwin",
              "pcc.backend.indexed_phase_timing",
              "pcc.backend.self_backend_aarch64_darwin_prologue",
              "pcc.backend.self_backend_aarch64_darwin_regalloc",
              "pcc.backend.self_backend_precise_stackmaps")
ELF_ARGV = "PCC_HOST_ELF_STAGE_PROFILE_ARGV"
ELF_TARGET = "aarch64-unknown-linux-gnu"
ELF_SCHEMA = "pcc.host-elf-stage-function-profile.v1"
ELF_OWNERS = ("pcc.frontends.python.pipeline", "pcc.backend.owned_elf_inputs",
              "pcc.backend.owned_elf_link", "pcc.backend.elf_x86_64")
CONFIG = "PCC_HOST_ASSEMBLY_PROFILE_CONFIG"
SOURCE_ROOT = "PCC_HOST_ASSEMBLY_PROFILE_ROOT"
SCHEMA = "pcc.host-assembly-function-profile.v1"
MANIFEST_LIMIT = 1_048_576
REPORT_LIMIT = 65_536
TOP = 20
EDGE_LIMIT = 12
# A failed 3.13 profiler can have destructive deallocation side effects.
# Keep at most this build's one claimed instance alive; do not pretend that
# public monitoring APIs can recover arbitrary partial enable/disable failure.
_FAILED_PROFILER = None
OWNERS = ("pcc.backend.x86_64_asm_driver", "pcc.backend.indexed_phase_timing",
          "pcc.backend.x86_64_encode", "pcc.backend.elf_x86_64")


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path, value):
    # Exclusive, bounded publications never replace evidence from a retry.
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    if len(payload.encode("ascii")) > REPORT_LIMIT:
        raise ValueError("assembly profile report exceeds its byte cap")
    with Path(path).open("x", encoding="ascii") as stream:
        stream.write(payload)
        stream.flush()


def _observer_conflict():
    # CPython 3.13 cProfile uses PEP 669, independently of sys.getprofile().
    try:
        return (sys.getprofile() is not None
                or sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is not None)
    except BaseException:
        return True


def _elf_environment(environment):
    expected = {"PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
                "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1", "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
                "PCC_DEFER_FRONTEND_CODEGEN": "0", "PCC_DEFER_FRONTEND_CODEGEN_PLAN": "",
                "PCC_DEFER_FRONTEND_OUTPUT": "", "PCC_DEFER_SELF_LINK_PLAN": ""}
    return all(environment.get(name) == value for name, value in expected.items())


def _elf_command(command, root):
    return (type(command) in (list, tuple) and len(command) == 11
            and all(type(item) is str for item in command)
            and list(command[:8]) == [sys.executable, "-m", "pcc", "--backend", "self",
                                      "--python-libpython", "off", "--ir-scaffold=on"]
            and command[8] == str(Path(root) / "pcc/__main__.py") and command[9] == "-o"
            and bool(command[10]))


def prepare_environment(root, directory, environment, source, runtime, compiler_sha256, target,
                        *, command=None):
    """Configure only the existing Stage1 command; never launch a process."""
    elf_mode = environment.get(ELF_FLAG) == "1"
    arm_mode = environment.get(ARM_FLAG) == "1"
    if environment.get(FLAG) != "1" and not elf_mode and not arm_mode:
        return environment
    directory = Path(directory)
    owned = False
    status = "SETUP_FAILED"
    try:
        directory.mkdir(parents=True, exist_ok=False)
        owned = True
        status = "PROFILE_MODE_CONFLICT"
        if sum(environment.get(flag) == "1" for flag in (FLAG, ELF_FLAG, ARM_FLAG)) != 1:
            raise ValueError(status)
        status = "UNSUPPORTED_HOST"
        machines = ("aarch64", "arm64") if elf_mode or arm_mode else ("x86_64", "amd64")
        if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13)
                or sys.platform != "linux" or platform.machine().lower() not in machines
                or target != (ARM_TARGET if arm_mode else ELF_TARGET if elf_mode else TARGET)):
            raise ValueError(status)
        status = "COORDINATOR_ADMISSION_CONFLICT"
        if elf_mode and (not _elf_environment(environment) or not _elf_command(command, root)
                         or len(json.dumps(command, separators=(",", ":"))) > 16384):
            raise ValueError(status)
        status = "WORKER_ENVIRONMENT_CONFLICT"
        if arm_mode and not _elf_environment(environment):
            raise ValueError(status)
        status = "PROFILE_HOOK_CONFLICT"
        if _observer_conflict():
            raise ValueError(status)
        # Before adding any path: never shadow an existing startup hook.
        # The cwd check covers the script-orchestrator versus child -m path.
        status = "SITECUSTOMIZE_CONFLICT"
        if ("sitecustomize" in sys.modules
                or importlib.util.find_spec("sitecustomize") is not None
                or importlib.machinery.PathFinder.find_spec("sitecustomize", [str(root)]) is not None):
            raise ValueError(status)
        status = "STARTUP_PATH_CONFLICT"
        if environment.get("PYTHONPATH", "") != os.environ.get("PYTHONPATH", ""):
            raise ValueError(status)
        status = "SETUP_FAILED"
        adapter = Path(__file__).read_bytes()
        (directory / "_pcc_host_assembly_profile.py").write_bytes(adapter)
        (directory / "sitecustomize.py").write_text(
            "from _pcc_host_assembly_profile import install\ninstall()\n", encoding="ascii")
        identity = {
            "source": source,
            "runtime": {key: value for key, value in runtime.items() if key.endswith("sha256")},
            "python": {"implementation": "cpython", "version": list(sys.version_info),
                       "executable_sha256": compiler_sha256},
            "target": target, "module": MODULE,
            "adapter_sha256": hashlib.sha256(adapter).hexdigest(),
            "owner_sources": {name.replace(".", "/") + ".py":
                              _digest(Path(root) / (name.replace(".", "/") + ".py"))
                              for name in (*(ARM_OWNERS if arm_mode else ELF_OWNERS if elf_mode else OWNERS), MODULE)},
        }
        if arm_mode:
            identity["capture"] = ARM_CAPTURE
        if elf_mode:
            identity["capture"] = "ElfInputStore.stage"
        # Paths stay in the child environment, not uploaded diagnostic JSON.
        _write(directory / "setup.json", {"identity": identity})
        result = environment.copy()
        result[CONFIG] = str(directory / "setup.json")
        result[SOURCE_ROOT] = str(root)
        if elf_mode:
            result[ELF_ARGV] = json.dumps(command, separators=(",", ":"))
        result["PYTHONPATH"] = str(directory) + os.pathsep + environment.get("PYTHONPATH", "")
        return result
    except BaseException:
        # Conflicts disable only observation, never the ordinary Stage1 job.
        if owned:
            try:
                _write(directory / "install-status.json", {
                    "schema": ARM_SCHEMA if arm_mode else ELF_SCHEMA if elf_mode else SCHEMA, "status": status,
                    "module": MODULE, "target": ARM_TARGET if arm_mode else ELF_TARGET if elf_mode else TARGET})
            except BaseException:
                pass
        return environment


def _admit(argv, root):
    """Read only the current bounded ordinary manifest, not the PCC parser."""
    if (len(argv) != 5 or argv[:4] != [sys.executable, "-m", "pcc",
                                        "--pcc-python-multi-codegen-worker"]):
        return None
    with open(argv[4], "rb") as stream:
        raw = stream.read(MANIFEST_LIMIT + 1)
    if len(raw) > MANIFEST_LIMIT:
        return None
    lines = raw.decode("utf-8").splitlines()
    # Checkpoint v5 is deliberately outside this normal-Stage1 measurement.
    if (len(lines) < 14 or lines[0] != "pcc.frontends.python.codegen_worker.v4"
            or lines[4] != "codegen" or lines[7:9] != ["off", "on"]):
        return None
    siblings = int(lines[10])
    if not 0 <= siblings <= 4096:
        return None
    position = 11 + siblings
    count = int(lines[position])
    position += 1
    if not 1 <= count <= 4096 or len(lines) != position + count + 2:
        return None
    if lines[position + count] != "1":
        return None
    assigned = int(lines[position + count + 1])
    if not 0 <= assigned < count:
        return None
    row = lines[position + assigned].split("\t")
    if (len(row) != 3 or row[:2] != [str(assigned), MODULE]
            or Path(row[2]).resolve() != Path(root) / "pcc/driver/cli_bootstrap.py"):
        return None
    return hashlib.sha256(raw).hexdigest()


def _safe(text):
    text = re.sub(r"0x[0-9a-fA-F]+", "address", text)
    return re.sub(r"[^a-zA-Z0-9_./:<> -]", "_", text)[:160]


def _labels(modules):
    # IDs, not code equality or pstats (filename, line, name) keys. In
    # particular, different dataclass __init__ functions can share <string>:2.
    labels = {}
    for module in modules:
        for name, value in vars(module).items():
            if isinstance(value, type) and value.__module__ == module.__name__:
                code = getattr(vars(value).get("__init__"), "__code__", None)
                if code is not None:
                    labels[id(code)] = module.__name__ + "." + name + ".__init__"
    return labels


def _summary(entries, root, labels, key_codes, *, pinned_edges=None, attribution=None):
    rows, ids, all_edges = [], {}, []
    for entry in entries:
        code = entry.code
        key = id(code)
        ids[key] = len(rows)
        if isinstance(code, str):
            name, path, line = _safe(code), "<builtin>", 0
        else:
            name = _safe(labels.get(key, code.co_qualname))
            try:
                path = str(Path(code.co_filename).resolve().relative_to(root))
            except ValueError:
                path = "<generated>" if code.co_filename.startswith("<") else "<external>"
            path, line = _safe(path), code.co_firstlineno
        rows.append({"id": ids[key], "name": name, "file": path, "line": line,
                     "calls": entry.callcount, "recursive_calls": entry.reccallcount,
                     "self_s": entry.inlinetime, "cumulative_s": entry.totaltime})
        for child in entry.calls or ():
            all_edges.append((key, id(child.code), child))
    top_self = sorted(rows, key=lambda row: (-row["self_s"], row["id"]))[:TOP]
    top_cum = sorted(rows, key=lambda row: (-row["cumulative_s"], row["id"]))[:TOP]
    selected = {row["id"] for row in top_self + top_cum}
    # Pin the encode caller edges first. Remaining slots show direct key-owner
    # edges only. Shared deeper helpers' self remains combined across callers.
    root_code, encode_caller, encode, _finisher = key_codes
    pinned = ({(root_code, encode), (encode_caller, encode)} if pinned_edges is None else set(pinned_edges))
    key_edges = [edge for edge in all_edges if edge[0] in key_codes]
    eligible = [edge for edge in key_edges if edge[0] in ids and edge[1] in ids]
    eligible.sort(key=lambda edge: ((edge[0], edge[1]) not in pinned,
                                   -edge[2].totaltime, ids[edge[0]], ids[edge[1]]))
    edges = []
    for caller, callee, entry in eligible[:EDGE_LIMIT]:
        # Include bounded endpoint descriptors without promoting extra rows
        # into either top-N ranking.
        edges.append({"caller": rows[ids[caller]], "callee": rows[ids[callee]],
                      "calls": entry.callcount, "recursive_calls": entry.reccallcount,
                      "self_s": entry.inlinetime, "cumulative_s": entry.totaltime})
    total_self = sum(row["self_s"] for row in rows)
    shown_self = sum(rows[index]["self_s"] for index in selected)
    return {
        "functions": [row for row in rows if row["id"] in selected],
        "top_self_ids": [row["id"] for row in top_self],
        "top_cumulative_ids": [row["id"] for row in top_cum], "key_caller_edges": edges,
        "coverage": {"functions_total": len(rows), "functions_shown": len(selected),
                     "functions_omitted": len(rows) - len(selected),
                     "self_s_total": total_self, "self_s_shown": shown_self,
                     "self_s_omitted": total_self - shown_self,
                     "edges_total": len(all_edges), "key_edges_total": len(key_edges),
                     "key_edges_identity_unresolved": len(key_edges) - len(eligible),
                     "edges_shown": len(edges), "edges_omitted": len(all_edges) - len(edges)},
        "limits": {"top_per_ranking": TOP, "key_edges": EDGE_LIMIT, "report_bytes": REPORT_LIMIT},
        "attribution": attribution or "Direct encode caller edges show the single placing pass; deeper helper self is combined. Cumulative times overlap and must not be summed.",
        "edge_identity": "Raw code identity only. Unresolved edge identities, including any separately materialized builtin labels, are counted as omitted; labels are never merged.",
    }


def _attach(config, manifest_sha256, assembly, timing, encoder, elf):
    original = assembly._assemble_file
    original_report = timing.ModulePhaseTiming.report
    state = {"used": False, "owned": False, "profiler": None, "timing_id": None,
             "status": "NOT_CAPTURED", "diagnostic_errors": [], "disabled": False}
    directory = Path(config["directory"])
    base = {"schema": SCHEMA, "identity": config["identity"],
            "worker_manifest_sha256": manifest_sha256,
            "scope": "one real _assemble_file; profiled overhead, not baseline performance",
            "selected_worker_startup": "ordinary eager PCC import after singleton admission; perturbed",
            "observer_failure": "Partial enable/disable may leave profiling active beyond assembly; invalid captures publish no stats. Arbitrary same-name monitoring takeover is not guaranteed safe.",
            "strong_kill": "may leave claim.json without stats; no completion is inferred"}

    def note(value):
        try:
            if len(state["diagnostic_errors"]) < 8:
                state["diagnostic_errors"].append(value)
        except BaseException:
            pass

    def restore_assembly():
        try:
            if assembly._assemble_file is measured:
                assembly._assemble_file = original
        except BaseException:
            note("assembly_wrapper_restore_failed")

    def measured(*args, **kwargs):
        phase = kwargs.get("phase_timing", args[6] if len(args) > 6 else None)
        if (state["used"] or phase is None or phase.module_name != MODULE
                or phase.target != TARGET or phase.route != ROUTE):
            return original(*args, **kwargs)
        state["used"] = True
        state["timing_id"] = id(phase)
        try:
            _write(directory / "claim.json", dict(base, status="CLAIMED"))
        except BaseException:
            # Another attempt owns this build's one claim, or diagnostics are
            # unavailable. Neither changes ordinary compilation or retries.
            return original(*args, **kwargs)
        state["owned"] = True
        if _observer_conflict():
            state["status"] = "PROFILE_HOOK_CONFLICT"
            return original(*args, **kwargs)
        try:
            from cProfile import Profile
            profiler = Profile(subcalls=True, builtins=True)
        except BaseException:
            state["status"] = "PROFILE_SETUP_FAILED"
            return original(*args, **kwargs)
        state["profiler"] = profiler
        # Imports/audit callbacks may themselves install a profiler. Never
        # override a hook that appeared since the first conflict check.
        if _observer_conflict():
            state["profiler"] = None
            state["status"] = "PROFILE_HOOK_CONFLICT"
            return original(*args, **kwargs)
        try:
            profiler.enable()
        except BaseException:
            note("enable_failed")
        state["status"] = "ASSEMBLY_RAISED"
        try:
            result = original(*args, **kwargs)
            state["status"] = "ASSEMBLY_RETURNED"
            return result
        finally:
            # Only the official API, once. Do not reset legacy hooks, swap
            # monitoring callbacks, or free a slot as a recovery mechanism.
            # The public slot name cannot prove instance identity after an
            # arbitrary same-name takeover; that case is not guaranteed safe.
            try:
                if sys.getprofile() is not None:
                    note("foreign_legacy_hook_preserved")
                slot = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID)
                if slot not in (None, "cProfile"):
                    note("foreign_monitoring_slot_preserved")
                else:
                    profiler.disable()
                    state["disabled"] = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None
                    if not state["disabled"]:
                        note("profile_state_not_restored")
            except BaseException:
                note("disable_failed_state_not_restored")
            if state["diagnostic_errors"] or not state["disabled"]:
                global _FAILED_PROFILER
                _FAILED_PROFILER = profiler
            # No report here: do not extend the assembly/ELF return lifetime
            # through serialization or add that work to the codegen stopwatch.
            restore_assembly()

    def report(phase, *args, **kwargs):
        try:
            return original_report(phase, *args, **kwargs)
        finally:
            if state["timing_id"] == id(phase):
                profiler = state.pop("profiler", None)
                try:
                    if timing.ModulePhaseTiming.report is report:
                        timing.ModulePhaseTiming.report = original_report
                except BaseException:
                    note("report_wrapper_restore_failed")
                restore_assembly()
                try:
                    if state["owned"]:
                        value = dict(base, status=state["status"],
                                     diagnostic_errors=state["diagnostic_errors"],
                                     capture_valid=profiler is not None and state["disabled"]
                                     and not state["diagnostic_errors"])
                        if value["capture_valid"]:
                            value.update(_summary(profiler.getstats(), Path(config["root"]),
                                                  _labels((assembly, encoder, elf)),
                                                  (id(original.__code__), id(assembly._encode_file.__code__),
                                                   id(encoder.encode_instruction.__code__), id(assembly._finish_section.__code__))))
                        _write(directory / "result.json", value)
                except BaseException:
                    # An original compiler/report exception always has priority.
                    try:
                        _write(directory / "result.json", dict(base, status="PROFILE_REPORT_FAILED",
                                                               assembly_status=state["status"], capture_valid=False))
                    except BaseException:
                        pass
                finally:
                    if profiler is not None and state["disabled"]:
                        try:
                            profiler.clear()
                        except BaseException:
                            pass

    try:
        assembly._assemble_file = measured
        timing.ModulePhaseTiming.report = report
    except BaseException:
        # Roll back only this adapter's own partial installation.
        restore_assembly()
        try:
            if timing.ModulePhaseTiming.report is report:
                timing.ModulePhaseTiming.report = original_report
        except BaseException:
            pass
        raise


# Count bounds, not latency bounds. Explicit transient buffer payload is
# two 4-byte (B+1) vectors and a B-byte bitmap: 9B+8, excluding Python metadata.
FACTS_BLOCK_CAP = 1 << 19
FACTS_CENSUS_CAP = 1 << 23
FACTS_INITIALIZED_CAP = 1 << 20
FACTS_POP_CAP = 1 << 22
FACTS_CALL_CAP = 1024
FACTS_DETAIL_CAP = 32


class _FactsStructureObserver:
    """One home per call; census synchronously borrows exact builtin inputs.

    begin() reads bounded existing containers without mutation, retaining or
    exporting none. Later callbacks receive scalars and the bounded sample.
    """

    def __init__(self):
        self.calls = self.census = self.initialized = self.pops = 0
        self.skipped = self.invalid = self.selected = self.completed = 0
        self.disabled = None
        self.active = None
        self.details = []
        self.totals = dict(P=0, Q=0, U=0, E_seen=0, B=0, E_all=0,
                           seed_active=0, seed_traffic=0, walking_queries=0,
                           memo_hits=0, censored_calls=0)

    def _spend(self, count):
        if self.census + count > FACTS_CENSUS_CAP:
            self.disabled = "census_cap"
            return False
        self.census += count
        return True

    def begin(self, definitions, uses, predecessors, bases, ends):
        if self.active is not None:
            # An earlier facts invocation raised before complete().
            self.fail("unfinished_facts")
        if self.disabled is not None:
            return None
        if self.calls >= FACTS_CALL_CAP:
            self.disabled = "call_cap"
            return None
        self.calls += 1
        self.skipped += 1
        if (type(definitions) is not dict or type(uses) is not dict
                or type(predecessors) is not list or type(bases) is not list
                or type(ends) is not list):
            return None
        B = len(predecessors)
        if not 0 < B <= FACTS_BLOCK_CAP or len(bases) != B or len(ends) != B:
            return None
        # Validate before any lookup that could invoke custom equality/indexing.
        for value, home in definitions.items():
            if not self._spend(1):
                return None
            if type(value) is not int or type(home) is not int or not -1 <= home < B:
                return None
        if self.initialized + B > FACTS_INITIALIZED_CAP:
            self.disabled = "initialized_blocks_cap"
            return None
        self.initialized += B
        from array import array
        counts = array("I", [0])
        if counts.itemsize != 4 or FACTS_CENSUS_CAP >= 1 << 32:
            self.fail("counter_width")
            return None
        counts *= B + 1
        traffic = counts[:]
        for value, seeds in uses.items():
            if not self._spend(1):
                return None
            # -1 aliases the original visited sentinel; never observe it.
            if type(value) is not int or value == -1 or type(seeds) is not list:
                return None
            home = definitions.get(value, -1)
            nonhome = 0
            for seed in seeds:
                if not self._spend(1):
                    return None
                if type(seed) is not int or not 0 <= seed < B:
                    return None
                if seed != home:
                    nonhome += 1
            if nonhome:
                counts[home + 1] += 1
                traffic[home + 1] += nonhome
        E_all = 0
        # These rows are constructed locally by facts, containing its integer
        # block_id. Inspect each existing row once; do not traverse its edges.
        for row in predecessors:
            if not self._spend(1):
                return None
            if type(row) is not list:
                return None
            E_all += len(row)
        if not self._spend(B + 1):
            return None
        selected = -1
        for index in range(B + 1):
            if counts[index] >= 2 and (selected < 0
                    or counts[index] > counts[selected]
                    or (counts[index] == counts[selected] and traffic[index] > traffic[selected])):
                selected = index
        if selected < 0:
            return None
        sample = dict(call=self.calls, home=selected - 1, B=B, E_all=E_all,
                      seed_active=counts[selected], seed_traffic=traffic[selected],
                      walking_queries=0, memo_hits=0, P=0, Q=0, U=0, E_seen=0,
                      censored=None, seen=bytearray(B))
        # No vectors or references to graph/kernel/uses leave this method.
        self.active = sample
        self.selected += 1
        self.skipped -= 1
        return sample

    def query(self, sample, home, walking, memo_hit):
        if home != sample["home"]:
            return False
        if self.disabled is not None or sample["censored"] is not None:
            return False
        if memo_hit:
            sample["memo_hits"] += 1
        if walking:
            sample["walking_queries"] += 1
            return True
        return False

    def row(self, sample, block, edges):
        # Called only after the complete original row body, never per edge.
        # Any partial bookkeeping failure invalidates the entire sample.
        sample["P"] += 1
        sample["Q"] += edges
        self.pops += 1
        if not sample["seen"][block]:
            sample["seen"][block] = 1
            sample["U"] += 1
            sample["E_seen"] += edges
        if self.pops >= FACTS_POP_CAP:
            self.disabled = "global_pop_cap"
            sample["censored"] = self.disabled
        elif sample["P"] >= 8 * sample["B"]:
            sample["censored"] = "call_pop_cap"
        if sample["censored"] is not None:
            sample["seen"] = None
            return False
        return True

    def complete(self, sample):
        sample["seen"] = None
        row = {key: value for key, value in sample.items() if key != "seen"}
        row["repeated_pops_lower_bound"] = row["P"] - row["U"]
        row["repeated_edges_lower_bound"] = row["Q"] - row["E_seen"]
        row["compression_lower_bound"] = 1 + (
            row["repeated_pops_lower_bound"] + row["repeated_edges_lower_bound"]
        ) / (row["B"] + row["E_all"])
        # Only successful facts calls contribute evidence; invalid samples
        # never publish partial P/Q or a ratio.
        totals = {key: value + (int(sample["censored"] is not None)
                               if key == "censored_calls" else sample[key])
                  for key, value in self.totals.items()}
        details = self.details + [row] if len(self.details) < FACTS_DETAIL_CAP else self.details
        completed = self.completed + 1
        # Prepare all allocating work before committing the successful row.
        self.totals, self.details, self.completed = totals, details, completed
        self.active = None

    def fail(self, reason="observer_error"):
        self.disabled = reason
        if self.active is not None:
            self.active["seen"] = None
            self.active = None
            self.invalid += 1

    def report(self):
        return dict(
            schema="pcc.host-facts-structure.v1", calls=self.calls,
            selected=self.selected, completed=self.completed, skipped=self.skipped,
            invalid=self.invalid, disabled=self.disabled, census_items=self.census,
            initialized_blocks=self.initialized, pop_budget_used=self.pops,
            totals=self.totals.copy(), details=self.details,
            details_omitted=self.completed - len(self.details),
            limits=dict(blocks=FACTS_BLOCK_CAP, census=FACTS_CENSUS_CAP,
                        initialized_blocks=FACTS_INITIALIZED_CAP, pops=FACTS_POP_CAP,
                        calls=FACTS_CALL_CAP, detail_rows=FACTS_DETAIL_CAP,
                        per_call_pops_per_block=8, explicit_buffer_bytes_per_block=9),
            selection="One home with at least two seed-active values, ranked by active values, raw nonhome seed traffic, then lowest home. Home -1 is valid. Home-only values are excluded; duplicate seeds count traffic. This biases toward reuse; one negative home is not a global negative.",
            interpretation="P/Q count completed observed walk rows/row lengths after existing memoization. walking_queries and memo_hits cover selected queries admitted before censoring; seed_active is the full census count. U/E_seen count first-seen rows/row lengths. Caps censor a prefix, never a whole-home ratio. P-U and Q-E_seen are repeated-work lower bounds; 1+(P+Q-U-E_seen)/(B+E_all) is a conservative whole-home compression lower bound for successful facts. No SCC was computed; SCC constants, native fit and net benefit are unknown. Profile overhead and profiler memory are not bounded by this report.",
        )


def _invalidate_facts(observer, reason):
    try:
        observer.fail(reason)
    except BaseException:
        # Diagnostic cleanup must not replace the backend's result/exception.
        try:
            observer.active = None
            observer.disabled = "observer_error"
        except BaseException:
            pass


def _facts_pop_edge(entries, facts_code):
    """Resolve one raw facts code and its unique actual list.pop child."""
    unavailable = dict(available=False, reason="raw_identity_unresolved_or_ambiguous")
    parents = [entry for entry in entries if entry.code is facts_code]
    if len(parents) != 1:
        return unavailable
    children = [child for child in parents[0].calls or ()
                if type(child.code) is str and child.code == "<method 'pop' of 'list' objects>"]
    if len(children) != 1:
        return unavailable
    child = children[0]
    matches = [entry for entry in entries if entry.code is child.code]
    if len(matches) != 1:
        return unavailable
    return dict(available=True, calls=child.callcount, recursive_calls=child.reccallcount,
                self_s=child.inlinetime, cumulative_s=child.totaltime,
                interpretation="Raw facts-to-list.pop edge: C includes actual walk pops plus memo-hit drain pops. C is not selected P; no selected or capped count is subtracted from backend totals.")


def _attach_arm(config, manifest_sha256, backend, timing, prologue, regalloc, stackmaps):
    original = backend.emit_aarch64_darwin_indexed_transport
    phase_type = timing.ModulePhaseTiming
    original_report = phase_type.report
    facts_code = regalloc._function_level_facts.__code__
    facts_observer = _FactsStructureObserver()
    # No IR module, invocation arguments, result, phase or traceback is retained.
    state = {"used": False, "owned": False, "profiler": None, "timing_id": None,
             "status": "NOT_CAPTURED", "diagnostic_errors": [], "disabled": False}
    directory = Path(config["directory"])
    base = {"schema": ARM_SCHEMA, "identity": config["identity"],
            "worker_manifest_sha256": manifest_sha256,
            "scope": "one real cli_bootstrap ARM indexed transport backend; profiled overhead, not baseline performance",
            "selected_worker_startup": "ordinary eager PCC import after singleton admission; perturbed",
            "publication": "ordinary phase report after codegen stopwatch stops; no backend-return serialization",
            "observer_failure": "Partial enable/disable may leave profiling active beyond backend; invalid captures publish no stats. Arbitrary same-name monitoring takeover is not guaranteed safe.",
            "strong_kill": "may leave claim.json without stats; no completion is inferred"}

    def note(value):
        try:
            if len(state["diagnostic_errors"]) < 8:
                state["diagnostic_errors"].append(value)
        except BaseException:
            pass

    def restore_backend():
        try:
            if backend.emit_aarch64_darwin_indexed_transport is measured:
                backend.emit_aarch64_darwin_indexed_transport = original
        except BaseException:
            note("backend_wrapper_restore_failed")

    def measured(*args, **kwargs):
        # Exact signature/owned phase only. No user conversions, IR inspection,
        # input replay, keyword rewriting or iterator consumption.
        try:
            phase = kwargs.get("phase_timing")
            optimize = args[1] if len(args) > 1 else kwargs.get("optimize", True)
            structured = args[2] if len(args) > 2 else kwargs.get("structured_instructions", True)
            bypass = (state["used"] or len(args) > 3
                or bool(args) == ("module" in kwargs)
                or (len(args) > 1 and "optimize" in kwargs)
                or (len(args) > 2 and "structured_instructions" in kwargs)
                or set(kwargs) - {"module", "optimize", "structured_instructions", "phase_timing"}
                or type(phase) is not phase_type
                or type(phase.module_name) is not str or phase.module_name != MODULE
                or type(phase.target) is not str or phase.target != ARM_TARGET
                or type(phase.route) is not str or phase.route != ARM_ROUTE
                or optimize is not False or structured is not True)
        except BaseException:
            bypass = True
        if bypass:
            return original(*args, **kwargs)
        state["used"] = True
        state["timing_id"] = id(phase)
        profiler = None
        facts_previous = None
        facts_installed = False
        try:
            try:
                _write(directory / "claim.json", dict(base, status="CLAIMED"))
                state["owned"] = True
                if _observer_conflict():
                    state["status"] = "PROFILE_HOOK_CONFLICT"
                else:
                    from cProfile import Profile
                    profiler = Profile(subcalls=True, builtins=True)
                    if _observer_conflict():
                        profiler = None
                        state["status"] = "PROFILE_HOOK_CONFLICT"
                    else:
                        state["profiler"] = profiler
                        try:
                            profiler.enable()
                        except BaseException:
                            note("enable_failed")
            except BaseException:
                state["status"] = "PROFILE_SETUP_FAILED"
            # Even a failed claim/setup/enable calls the same business function
            # exactly once. Its BaseException is never caught or replaced.
            if profiler is not None:
                state["status"] = "BACKEND_RAISED"
            if profiler is not None and not state["diagnostic_errors"]:
                try:
                    facts_previous = regalloc._HOST_FACTS_STRUCTURE_OBSERVER
                    if facts_previous is not None:
                        _invalidate_facts(facts_observer, "foreign_hook")
                    else:
                        regalloc._HOST_FACTS_STRUCTURE_OBSERVER = facts_observer
                        facts_installed = True
                except BaseException:
                    _invalidate_facts(facts_observer, "hook_install_failed")
            result = original(*args, **kwargs)
            if profiler is not None:
                state["status"] = "BACKEND_RETURNED"
            return result
        finally:
            try:
                # An assignment that raised after mutation still belongs to us.
                if regalloc._HOST_FACTS_STRUCTURE_OBSERVER is facts_observer:
                    regalloc._HOST_FACTS_STRUCTURE_OBSERVER = facts_previous
                elif facts_installed:
                    _invalidate_facts(facts_observer, "foreign_hook_preserved")
                if facts_observer.active is not None:
                    _invalidate_facts(facts_observer, "facts_or_backend_raised")
            except BaseException:
                _invalidate_facts(facts_observer, "hook_restore_failed")
            if profiler is not None:
                # Official API only, once; no slot freeing or hook clearing.
                # A slot name cannot identify arbitrary same-name takeovers.
                try:
                    if sys.getprofile() is not None:
                        note("foreign_legacy_hook_preserved")
                    slot = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID)
                    if slot not in (None, "cProfile"):
                        note("foreign_monitoring_slot_preserved")
                    else:
                        profiler.disable()
                        state["disabled"] = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None
                        if not state["disabled"]:
                            note("profile_state_not_restored")
                except BaseException:
                    note("disable_failed_state_not_restored")
                if state["diagnostic_errors"] or not state["disabled"]:
                    global _FAILED_PROFILER
                    _FAILED_PROFILER = profiler
            restore_backend()

    def report(phase, *args, **kwargs):
        try:
            return original_report(phase, *args, **kwargs)
        finally:
            if state["timing_id"] == id(phase):
                profiler = state.pop("profiler", None)
                try:
                    if phase_type.report is report:
                        phase_type.report = original_report
                except BaseException:
                    note("report_wrapper_restore_failed")
                restore_backend()
                try:
                    if state["owned"]:
                        value = dict(base, status=state["status"],
                                     diagnostic_errors=state["diagnostic_errors"],
                                     capture_valid=profiler is not None and state["disabled"]
                                     and not state["diagnostic_errors"])
                        if value["capture_valid"]:
                            keys = (id(original.__code__),
                                    id(backend._emit_prepared_aarch64_darwin_lines_active.__code__),
                                    id(backend._emit_function.__code__),
                                    id(prologue.emit_function_prologue.__code__))
                            entries = profiler.getstats()
                            value.update(_summary(
                                entries, Path(config["root"]),
                                _labels((backend, prologue, regalloc, stackmaps)), keys,
                                pinned_edges=((keys[1], id(stackmaps.build_stack_map_plans.__code__)),
                                              (keys[1], keys[2]),
                                              (keys[2], id(backend.note_aarch64_reload_destinations.__code__)),
                                              (keys[2], keys[3]),
                                              (keys[2], id(backend._emit_dense_indexed_function_blocks.__code__)),
                                              (keys[3], id(regalloc.allocate_aarch64_block_registers.__code__))),
                                attribution="Backend transport includes prepare, planning, function emission and finalization. Function emission includes reload/prologue setup and bodies; prologue includes regalloc. Direct owner edges overlap these inclusive scopes; shared helper self is combined. Cumulative times must not be summed."))
                            value["facts_pop_edge"] = _facts_pop_edge(entries, facts_code)
                        value["facts_structure"] = facts_observer.report()
                        _write(directory / "result.json", value)
                except BaseException:
                    try:
                        _write(directory / "result.json", dict(base, status="PROFILE_REPORT_FAILED",
                                                               backend_status=state["status"], capture_valid=False))
                    except BaseException:
                        pass
                finally:
                    if profiler is not None and state["disabled"]:
                        try:
                            profiler.clear()
                        except BaseException:
                            pass

    try:
        backend.emit_aarch64_darwin_indexed_transport = measured
        phase_type.report = report
    except BaseException:
        restore_backend()
        try:
            if phase_type.report is report:
                phase_type.report = original_report
        except BaseException:
            pass
        raise


def _elf_selected_path(artifacts):
    # Inspect only the owner's existing exact containers/strings. Never
    # consume an iterator, call a user conversion, or inspect input files.
    if type(artifacts) is not list or not 1 <= len(artifacts) <= 4096:
        return None
    paths, selected = [], []
    for row in artifacts:
        if (type(row) is not tuple or len(row) != 3
                or any(type(value) is not str for value in row) or len(row[2]) > 4096):
            return None
        module, kind, raw_path = row
        path = os.path.abspath(raw_path)
        paths.append(path)
        if module == MODULE:
            if kind != "PCO":
                return None
            selected.append(path)
    if len(selected) != 1 or paths.count(selected[0]) != 1:
        return None
    return selected[0]


def _attach_elf(config, pipeline, inputs, linker, elf):
    original_link = pipeline._link_with_self_backend_direct_artifacts
    store_type = inputs.ElfInputStore
    original_stage = store_type.stage
    state = {"link_seen": False, "used": False, "active": False}
    directory = Path(config["directory"])
    base = {"schema": ELF_SCHEMA, "identity": config["identity"],
            "scope": "one real cli_bootstrap PCO ElfInputStore.stage; profiled overhead, not baseline performance",
            "selection": "unique module/kind/path in the original artifacts list; unchanged input order",
            "selected_coordinator_startup": "ordinary eager PCC import after exact argv admission; perturbed",
            "publication": "immediate after the selected call; report overhead and return-object lifetime are included in coordinator timing",
            "observer_failure": "Partial enable/disable may leave profiling active beyond stage; invalid captures publish no stats. Arbitrary same-name monitoring takeover is not guaranteed safe.",
            "strong_kill": "the selected stage may not return; claim.json may exist without stats"}

    def selection_status(status):
        try:
            _write(directory / "selection-status.json", dict(base, status=status))
        except BaseException:
            pass

    def restore_stage():
        try:
            if store_type.stage is selected_stage:
                store_type.stage = original_stage
            return True
        except BaseException:
            return False

    def restore_link():
        try:
            if pipeline._link_with_self_backend_direct_artifacts is selected_link:
                pipeline._link_with_self_backend_direct_artifacts = original_link
            return True
        except BaseException:
            return False

    def selected_stage(*args, **kwargs):
        store = args[0] if args else None
        path = args[1] if len(args) == 2 else kwargs.get("path")
        kind = kwargs.get("kind", "PCO")
        offset = kwargs.get("offset", 0)
        label = kwargs.get("label", "")
        try:
            bypass = (not state["active"] or state["used"] or len(args) not in (1, 2)
                or (len(args) == 2 and "path" in kwargs)
                or set(kwargs) - {"path", "kind", "offset", "size", "label"}
                or type(store) is not store_type or type(store.target) is not str
                or store.target != ELF_TARGET or type(path) is not str or path != state.get("path")
                or type(kind) is not str or kind != "PCO" or type(offset) is not int or offset != 0
                or kwargs.get("size") is not None or type(label) is not str or label != "")
        except BaseException:
            bypass = True
        if bypass:
            return original_stage(*args, **kwargs)
        state["used"] = True
        try:
            _write(directory / "claim.json", dict(base, status="CLAIMED"))
        except BaseException:
            return original_stage(*args, **kwargs)
        errors = []
        profiler = None
        disabled = False
        status = "STAGE_RAISED"
        source_count = None

        def note(value):
            try:
                if len(errors) < 8:
                    errors.append(value)
            except BaseException:
                pass

        try:
            source_count = len(store.sources)
            if _observer_conflict():
                note("profile_hook_conflict")
            else:
                from cProfile import Profile
                profiler = Profile(subcalls=True, builtins=True)
                if _observer_conflict():
                    profiler = None
                    note("profile_hook_conflict")
                else:
                    try:
                        profiler.enable()
                    except BaseException:
                        note("enable_failed")
        except BaseException:
            note("profile_setup_failed")
        try:
            result = original_stage(*args, **kwargs)
            status = "STAGE_RETURNED"
            return result
        finally:
            # Everything below is observational; original return/BaseException
            # wins. No store, result or traceback is saved in adapter state.
            try:
                if profiler is not None:
                    try:
                        if sys.getprofile() is not None:
                            note("foreign_legacy_hook_preserved")
                        slot = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID)
                        if slot not in (None, "cProfile"):
                            note("foreign_monitoring_slot_preserved")
                        else:
                            profiler.disable()
                            disabled = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None
                            if not disabled:
                                note("profile_state_not_restored")
                    except BaseException:
                        note("disable_failed_state_not_restored")
                    if errors or not disabled:
                        global _FAILED_PROFILER
                        _FAILED_PROFILER = profiler
                if not restore_stage():
                    note("stage_wrapper_restore_failed")
                value = dict(base, status=status, diagnostic_errors=errors,
                             capture_valid=profiler is not None and disabled and not errors)
                if status == "STAGE_RETURNED":
                    # Use the actual source appended by this stage; no reopen,
                    # payload/relocation property access or return-object walk.
                    if (source_count is not None and len(store.sources) == source_count + 1):
                        source = store.sources[source_count]
                        if (source.path == path and source.kind == "PCO" and source.whole_file
                                and type(source.size) is int and source.size >= 0
                                and type(source.sha256) is str
                                and re.fullmatch(r"[0-9a-f]{64}", source.sha256)):
                            value["input"] = {"module": MODULE, "kind": "PCO",
                                              "bytes": source.size, "sha256": source.sha256}
                        else:
                            raise ValueError("stage source identity differs")
                    else:
                        raise ValueError("stage source count differs")
                if value["capture_valid"]:
                    keys = (id(original_stage.__code__), id(store_type._read.__code__),
                            id(elf._parse_relocatable_indexed.__code__), id(elf._parse_relocatable.__code__))
                    value.update(_summary(
                        profiler.getstats(), Path(config["root"]), _labels((inputs, elf)), keys,
                        pinned_edges=((keys[0], keys[1]), (keys[0], keys[2]), (keys[2], keys[3])),
                        attribution="Direct stage/read/indexed-parse edges describe this one PCO input. Shared helper self is combined; cumulative times overlap and must not be summed."))
                _write(directory / "result.json", value)
            except BaseException:
                try:
                    _write(directory / "result.json", dict(base, status="PROFILE_REPORT_FAILED",
                                                           stage_status=status, capture_valid=False))
                except BaseException:
                    pass
            finally:
                if profiler is not None and disabled:
                    try:
                        profiler.clear()
                    except BaseException:
                        pass

    def selected_link(*args, **kwargs):
        if state["link_seen"]:
            return original_link(*args, **kwargs)
        state["link_seen"] = True
        attached = False
        try:
            artifacts = args[0] if args else kwargs.get("artifacts")
            selected = _elf_selected_path(artifacts)
            if selected is None or _observer_conflict():
                selection_status("SELECTION_OR_PROFILE_CONFLICT")
            elif store_type.stage is not original_stage:
                selection_status("STAGE_OWNER_CHANGED")
            else:
                state["path"] = selected
                state["active"] = True
                store_type.stage = selected_stage
                attached = True
        except BaseException:
            state["active"] = False
            restore_stage()
            selection_status("SELECTION_SETUP_FAILED")
        try:
            return original_link(*args, **kwargs)
        finally:
            state["active"] = False
            stage_restored = restore_stage()
            link_restored = restore_link()
            if not stage_restored or not link_restored:
                selection_status("OWNER_RESTORE_FAILED")
            elif attached and not state["used"]:
                selection_status("SELECTED_STAGE_NOT_REACHED")
            state.pop("path", None)

    try:
        pipeline._link_with_self_backend_direct_artifacts = selected_link
    except BaseException:
        restore_link()
        raise


def _install_elf():
    # The opt-in reaches child workers through the environment, but they must
    # return before PCC imports or any wrapper installation.
    if (os.environ.get(FLAG) == "1" or os.environ.get(ARM_FLAG) == "1"
            or not os.environ.get(CONFIG)
            or sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13)
            or sys.platform != "linux" or platform.machine().lower() not in ("aarch64", "arm64")
            or not _elf_environment(os.environ)):
        return
    argv = getattr(sys, "orig_argv", ())
    expected = os.environ.get(ELF_ARGV, "")
    if (len(expected) > 16384 or not _elf_command(argv, os.environ.get(SOURCE_ROOT, ""))
            or json.dumps(argv, separators=(",", ":")) != expected):
        return
    status = "SETUP_UNREADABLE"
    config = None
    try:
        with open(os.environ[CONFIG], encoding="ascii") as stream:
            raw = stream.read(REPORT_LIMIT + 1)
        if len(raw) > REPORT_LIMIT:
            return
        config = json.loads(raw)
        config.update(root=os.environ[SOURCE_ROOT], directory=str(Path(os.environ[CONFIG]).parent))
        status = "ADAPTER_IDENTITY_MISMATCH"
        if (config["identity"]["adapter_sha256"] != _digest(__file__)
                or config["identity"]["target"] != ELF_TARGET
                or config["identity"].get("capture") != "ElfInputStore.stage"):
            return
        status = "PROFILE_HOOK_CONFLICT"
        if _observer_conflict():
            return
        status = "OWNER_IMPORT_FAILED"
        loaded = [importlib.import_module(name) for name in ELF_OWNERS]
        status = "OWNER_IDENTITY_MISMATCH"
        for module, name in zip(loaded, ELF_OWNERS):
            path = Path(config["root"]) / (name.replace(".", "/") + ".py")
            if (Path(module.__file__).resolve() != path
                    or _digest(path) != config["identity"]["owner_sources"][name.replace(".", "/") + ".py"]):
                return
        pipeline, inputs, linker, elf = loaded
        status = "OWNER_HOOK_CONFLICT"
        if type(inputs.ElfInputStore) is not type or linker.ElfInputStore is not inputs.ElfInputStore:
            return
        for function, module, name in (
                (pipeline._link_with_self_backend_direct_artifacts, pipeline, "_link_with_self_backend_direct_artifacts"),
                (inputs.ElfInputStore.stage, inputs, "ElfInputStore.stage")):
            if (function.__module__ != module.__name__ or function.__qualname__ != name
                    or Path(function.__code__.co_filename).resolve() != Path(module.__file__).resolve()):
                return
        status = "PROFILE_HOOK_CONFLICT"
        if _observer_conflict():
            return
        status = "ATTACH_FAILED"
        _attach_elf(config, pipeline, inputs, linker, elf)
        status = "INSTALLED"
    except BaseException:
        return
    finally:
        try:
            _write(Path(os.environ[CONFIG]).parent / "install-status.json", {
                "schema": ELF_SCHEMA, "status": status,
                "identity": config["identity"] if config is not None else {},
                "scope": "exact admitted ARM Stage1 coordinator; observation only"})
        except BaseException:
            pass


def install():
    """sitecustomize entry; no hooks, PCC imports or profiler for other jobs."""
    if os.environ.get(ELF_FLAG) == "1":
        try:
            _install_elf()
        except BaseException:
            # A failure before exact coordinator admission must not alter
            # ordinary startup or publish evidence for an unselected worker.
            pass
        return
    arm_mode = os.environ.get(ARM_FLAG) == "1"
    if (arm_mode and os.environ.get(FLAG) == "1"):
        return
    if ((os.environ.get(FLAG) != "1" and not arm_mode) or not os.environ.get(CONFIG)
            or sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13)
            or sys.platform != "linux"
            or platform.machine().lower() not in (("aarch64", "arm64") if arm_mode else ("x86_64", "amd64"))
            or (arm_mode and not _elf_environment(os.environ))):
        return
    argv = getattr(sys, "orig_argv", ())
    if (len(argv) != 5 or list(argv[:4]) != [sys.executable, "-m", "pcc",
                                             "--pcc-python-multi-codegen-worker"]):
        return
    config, manifest = None, None
    status = "SETUP_UNREADABLE"

    def publish_status():
        if manifest is not None:
            try:
                _write(Path(config["directory"]) / "install-status.json", {
                    "schema": ARM_SCHEMA if arm_mode else SCHEMA, "status": status,
                    "scope": "first admitted worker installation; retries do not replace this record",
                    "identity": config["identity"], "worker_manifest_sha256": manifest,
                })
            except BaseException:
                pass

    try:
        with open(os.environ[CONFIG], encoding="ascii") as stream:
            raw = stream.read(REPORT_LIMIT + 1)
        if len(raw) > REPORT_LIMIT:
            return
        config = json.loads(raw)
        config.update(root=os.environ[SOURCE_ROOT], directory=str(Path(os.environ[CONFIG]).parent))
        manifest = _admit(list(argv), config["root"])
        if manifest is None:
            return
        status = "ADAPTER_IDENTITY_MISMATCH"
        if (config["identity"]["adapter_sha256"] != _digest(__file__)
                or (arm_mode and (config["identity"].get("target") != ARM_TARGET
                    or config["identity"].get("module") != MODULE
                    or config["identity"].get("capture") != ARM_CAPTURE))):
            return
        status = "PROFILE_HOOK_CONFLICT"
        if _observer_conflict():
            return
        status = "OWNER_IMPORT_FAILED"
        owners = ARM_OWNERS if arm_mode else OWNERS
        loaded = [importlib.import_module(name) for name in owners]
        status = "OWNER_IDENTITY_MISMATCH"
        for module, name in zip(loaded, owners):
            path = Path(config["root"]) / (name.replace(".", "/") + ".py")
            if (Path(module.__file__).resolve() != path
                    or _digest(path) != config["identity"]["owner_sources"][name.replace(".", "/") + ".py"]):
                return
        if arm_mode:
            # Check the selected input source and exact existing callable owners.
            relative = MODULE.replace(".", "/") + ".py"
            if _digest(Path(config["root"]) / relative) != config["identity"]["owner_sources"][relative]:
                return
            backend, timing, prologue, regalloc, stackmaps = loaded
            status = "OWNER_HOOK_CONFLICT"
            if type(timing.ModulePhaseTiming) is not type:
                return
            for function, module, name in (
                    (backend.emit_aarch64_darwin_indexed_transport, backend, ARM_CAPTURE),
                    (timing.ModulePhaseTiming.report, timing, "ModulePhaseTiming.report"),
                    (backend._emit_prepared_aarch64_darwin_lines_active, backend, "_emit_prepared_aarch64_darwin_lines_active"),
                    (backend._emit_function, backend, "_emit_function"),
                    (prologue.emit_function_prologue, prologue, "emit_function_prologue"),
                    (backend._emit_dense_indexed_function_blocks, backend, "_emit_dense_indexed_function_blocks"),
                    (regalloc.note_aarch64_reload_destinations, regalloc, "note_aarch64_reload_destinations"),
                    (regalloc.allocate_aarch64_block_registers, regalloc, "allocate_aarch64_block_registers"),
                    (regalloc._function_level_facts, regalloc, "_function_level_facts"),
                    (stackmaps.build_stack_map_plans, stackmaps, "build_stack_map_plans")):
                if (function.__module__ != module.__name__ or function.__qualname__ != name
                        or Path(function.__code__.co_filename).resolve() != Path(module.__file__).resolve()):
                    return
            if (backend._prologue_emit_function_prologue is not prologue.emit_function_prologue
                    or backend.build_stack_map_plans is not stackmaps.build_stack_map_plans
                    or backend.note_aarch64_reload_destinations is not regalloc.note_aarch64_reload_destinations
                    or prologue.allocate_aarch64_block_registers is not regalloc.allocate_aarch64_block_registers):
                return
            status = "PROFILE_HOOK_CONFLICT"
            if _observer_conflict():
                return
            status = "ATTACH_FAILED"
            _attach_arm(config, manifest, *loaded)
        else:
            assembly, timing, encoder, elf = loaded
            status = "ATTACH_FAILED"
            _attach(config, manifest, assembly, timing, encoder, elf)
        status = "INSTALLED"
    except BaseException:
        # Startup diagnostics never replace the worker's ordinary import/error.
        # A bounded status for admitted workers distinguishes installation
        # failure from capture loss, without exporting exception text/paths.
        return
    finally:
        publish_status()
