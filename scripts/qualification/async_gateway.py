"""Execute the mandatory async and gateway regressions with durable accounting.

Every async node runs separately so one compiler failure cannot hide the other
original cases. Gateway files resume untouched nodes after first-failure stopping. Resource
stops preserve remaining nodes as UNRUN and never count as successful execution. Collection never counts as execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


CORE = Path(__file__).resolve().parents[2]
ASYNC_FILE = "tests/python/test_async_await.py"


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _verify_source(root, records):
    for relative, expected in records.items():
        name = Path(relative)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("invalid source manifest path: " + relative)
        path = root / name
        is_link = path.is_symlink()
        data = os.fsencode(os.readlink(path)) if is_link else path.read_bytes()
        mode = 0o777 if is_link else 0o755 if path.stat().st_mode & 0o111 else 0o644
        if (hashlib.sha256(data).hexdigest() != expected["sha256"]
                or mode != expected["mode"]
                or ("symlink" if is_link else "file") != expected["kind"]):
            raise ValueError("source identity mismatch: " + str(path))


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _account(events, planned):
    results = {node: {"state": "UNRUN", "reports": []} for node in planned}
    for event in events:
        if event.get("event") != "report":
            continue
        node = event["nodeid"]
        row = results.setdefault(node, {"state": "UNRUN", "reports": []})
        row["reports"].append(event)
        if event["outcome"] == "failed":
            row["state"] = "FAIL" if event["when"] == "call" else "SETUP_OR_TEARDOWN_ERROR"
        elif event["outcome"] == "skipped" and row["state"] == "UNRUN":
            row["state"] = "SKIP"
        elif event["when"] == "call" and event["outcome"] == "passed":
            row["state"] = "PASS"
    return results


def _process_succeeded(row):
    """Call reports alone cannot prove successful teardown or process exit."""
    watchdog = row.get("watchdog", {})
    return (row.get("returncode") == 0
            and watchdog.get("status") == "COMPLETE"
            and watchdog.get("returncode") == 0)


def _run(args, name, repo, nodes, environment, *, collect=False):
    output = args.output / name
    output.mkdir()
    live = output / "live.jsonl"
    command = [args.python, "-B", "-m", "pytest", "-x", "-n0", "-vv",
               "--tb=short", "-p", "no:cacheprovider", "-m", "",
               "-p", "scripts.pytest_live_report", "--pcc-live-report", str(live),
               "--basetemp", str(output / "tmp"),
               "--junitxml", str(output / "junit.xml"), *nodes]
    if collect:
        command.append("--collect-only")
    guard = [args.python, "-B", str(args.core / "scripts/run_process_tree_sample.py"),
             "--result", str(output / "watchdog.json"),
             "--samples", str(output / "samples.csv"),
             "--stdout", str(output / "stdout.log"),
             "--stderr", str(output / "stderr.log"), "--cwd", str(repo),
             "--timeout", str(args.timeout),
             "--max-tree-rss-bytes", str(args.max_rss)]
    if collect:
        guard.append("--no-performance-lock")
    guard += ["--", *command]
    _write(output / "launch.json", {"command": guard, "collection_only": collect,
                                    "source_manifest": args.source_manifest,
                                    "runtime_archive": str(args.runtime_archive)})
    with (output / "guard.stdout").open("w") as stdout, (output / "guard.stderr").open("w") as stderr:
        result = subprocess.run(guard, env=environment, stdout=stdout, stderr=stderr)
    events = _events(live)
    selected = [node for event in events if event.get("event") == "collected"
                for node in event["nodeids"]]
    deselected = [node for event in events if event.get("event") == "deselected"
                  for node in event["nodeids"]]
    watchdog = json.loads((output / "watchdog.json").read_text()) if (output / "watchdog.json").exists() else {}
    return {"name": name, "returncode": result.returncode,
            "collection_only": collect, "selected": sorted(set(selected)),
            "deselected": sorted(set(deselected)), "watchdog": watchdog,
            "cases": {} if collect else _account(events, nodes),
            "live_report": str(live)}


def _execute_file_nodes(args, name, repo, nodes, environment):
    """Continue after a terminal test failure without retrying any reported node."""
    pending = list(nodes)
    attempt = 0
    while pending:
        suffix = "" if attempt == 0 else "-continue-" + str(attempt).zfill(3)
        row = _run(args, name + suffix, repo, pending, environment)
        yield row
        completed = {node for node, case in row["cases"].items()
                     if case["state"] != "UNRUN"}
        if not completed or row["watchdog"].get("status") != "COMPLETE":
            break
        pending = [node for node in pending if node not in completed]
        attempt += 1


def _execution_environment(args):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_ADDOPTS="",
                       PCC_NO_AUTO_PCC1="1", PCC_TEST_NO_NATIVE_PROVISIONING="1",
                       PCC_RUNTIME_ARCHIVE=str(args.runtime_archive),
                       PCC_WITH_THREADS=args.threads, PCC_REFCOUNT_KIND=args.refcount,
                       PCC_SOURCE_ROOT=str(args.core), PCC_REPO_ROOT=str(args.core),
                       PCC_RUNTIME_DIR=str(args.core / "pcc/runtime"),
                       PCC_VALIDATION_SOURCE_MANIFEST=args.source_manifest,
                       PYTHONPATH=os.pathsep.join((str(args.core), str(args.core / "tests/python"), str(args.gateway))))
    # An omitted compiler must remain unavailable, even if the coordinator's
    # shell retains selectors from an earlier immutable source.
    for name in ("LC_ALL", "PCC_TEST_PCC", "PCC_TEST_PCC1", "PCC_CURRENT_PCC1",
                 "PCC_TEST_COMPILER", "PCC_THREADED_RUNTIME_ARCHIVE",
                 "PCC_INDEXED_EMIT_TEST_COMPILER", "PCC_CONTEXTUAL_IR_OUTPUT"):
        environment.pop(name, None)
    environment.update(PCC_SELF_LINK="pcc", PCC_SELF_OBJ="pcc",
                       PCC_IR_TO_OBJ_EMITTER="pcc")
    for option, variable in ((args.pcc0, "PCC_TEST_PCC"), (args.pcc1, "PCC_TEST_PCC1")):
        if option is not None:
            environment[variable] = str(option.resolve(strict=True))
    if args.pcc1 is not None:
        environment["PCC_CURRENT_PCC1"] = str(args.pcc1.resolve(strict=True))
    return environment

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, default=CORE)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True,
                        help="source-identities.json containing pcc and pcc-gateway inventories")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--pcc0", type=Path, help="verified host PCC command for gateway tests")
    parser.add_argument("--pcc1", type=Path, help="verified native PCC command; never synthesized")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--max-rss", type=int, default=1536 * 1024 * 1024)
    parser.add_argument("--threads", choices=("0", "1"), required=True)
    parser.add_argument("--refcount", choices=("atomic", "local"), required=True)
    args = parser.parse_args(argv)
    for field in ("core", "gateway", "runtime_archive"):
        setattr(args, field, getattr(args, field).resolve(strict=True))
    args.source_manifest = args.source_manifest.resolve(strict=True)
    manifest_bytes = args.source_manifest.read_bytes()
    identities = json.loads(manifest_bytes)
    _verify_source(args.core, identities["pcc"])
    _verify_source(args.gateway, identities["pcc-gateway"])
    args.source_manifest = str(args.source_manifest)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    environment = _execution_environment(args)
    report = {"schema": "pcc.async-gateway-execution.v1", "groups": [],
              "core": str(args.core), "gateway": str(args.gateway),
              "source_manifest": args.source_manifest,
              "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "runtime_archive_sha256": hashlib.sha256(args.runtime_archive.read_bytes()).hexdigest(),
              "compiler_commands": {name: environment.get(name) for name in ("PCC_TEST_PCC", "PCC_TEST_PCC1")},
              "qualification": "Original test execution only. Skips, resource stops and unrun nodes are not passes. Inspect raw failures to distinguish code from infrastructure."}
    for area, repo, selection in (("async", args.core, [ASYNC_FILE]),
                                   ("gateway", args.gateway, ["tests"])):
        inventory = _run(args, area + "-collection", repo, selection, environment, collect=True)
        report[area + "_inventory"] = inventory
        _write(args.output / "summary.json", report)
        if inventory["returncode"] != 0:
            continue
        nodes = inventory["selected"]
        groups = [[node] for node in nodes] if area == "async" else [
            [node for node in nodes if node.split("::", 1)[0] == file]
            for file in sorted({node.split("::", 1)[0] for node in nodes})]
        for index, group in enumerate(groups):
            name = area + "-" + str(index).zfill(3)
            for row in _execute_file_nodes(args, name, repo, group, environment):
                report["groups"].append(row)
                _write(args.output / "summary.json", report)
    _verify_source(args.core, identities["pcc"])
    _verify_source(args.gateway, identities["pcc-gateway"])
    report["source_stable"] = True
    final_cases = {}
    for row in report["groups"]:
        for node, case in row["cases"].items():
            if node not in final_cases or case["state"] != "UNRUN":
                final_cases[node] = case
    report["final_cases"] = final_cases
    inventories_complete = all(
        _process_succeeded(report.get(area + "_inventory", {}))
        and not report[area + "_inventory"]["deselected"]
        for area in ("async", "gateway"))
    report["all_nodes_reported"] = bool(final_cases) and inventories_complete and all(
        case["state"] != "UNRUN" for case in final_cases.values())
    report["execution_processes_succeeded"] = bool(report["groups"]) and all(
        _process_succeeded(row) for row in report["groups"])
    report["complete_execution"] = report["all_nodes_reported"] and report["execution_processes_succeeded"] and all(
        case["state"] == "PASS" for case in final_cases.values())
    _write(args.output / "summary.json", report)
    return 0 if report["complete_execution"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
