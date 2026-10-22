#!/usr/bin/env python3
"""Measure pcc's CPython compatibility with CPython's own ``Lib/test``.

Every ``Lib/test`` test module of a CPython checkout -- each top-level
``test_*.py``, each ``test_*`` package and each ``test_*`` module inside such
a package, as dotted names below ``test`` -- runs twice with the same
minimal ``test.support`` (``support/`` next to this file) and the same
per-case runner (``runner.py``):

1. under CPython (the interpreter running this script, or ``--python``),
   which collects the cases as unittest's loader does, prints that plan
   and the result of every case -- the baseline;
2. compiled natively by pcc (``--pcc``, the host ``pcc`` of this checkout
   by default) from a driver that replays the same plan, then executed.

The compatibility rate is the share of the cases that succeed under CPython
(PASS or expected failure) that also succeed natively. A module that does
not compile, crashes or times out natively keeps its unreported cases as
failures; a module whose baseline does not import (the shim lacks a name)
has no denominator and is listed separately. Native results whose ids are
not in the baseline are reported but never counted.

Everything is written below ``--out``, which must be outside the
repository: ``stage/`` (the test package: the shim plus links to the
checkout's ``Lib/test``), ``work/`` (per-module working directories and
outputs), ``results/<module>.json`` (written as each module finishes, so a
run can be resumed with ``--resume``), ``identity.json`` (the source,
compiler, runtime and harness identity every result is keyed by) and
``summary.json``/``summary.md``.

Example::

    uv run python scripts/cpython_conformance/run.py \\
        --cpython-src ~/pcc-conformance/cpython-v3.15.0 \\
        --out ~/pcc-conformance/runs/<name> \\
        --runtime-archive <libpy_runtime_pcc_py.a> --modules test_bool test_int
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MARK = "@@PCC-CONFORMANCE@@"
SUCCESS = ("PASS", "XFAIL")
# Pure-Python helpers of CPython's test.support that are used as they are.
REAL_SUPPORT_FILES = (
    "testcase.py", "numbers.py", "hashlib_helper.py", "i18n_helper.py",
    "hypothesis_helper.py", "_hypothesis_stubs", "pty_helper.py", "isolation.py",
    "refleak_helper.py", "ast_helper.py", "asyncore.py", "asynchat.py",
    "logging_helper.py", "smtpd.py", "venv.py",
)
TAIL_BYTES = 4096


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _tail(path: Path) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    return data[-TAIL_BYTES:].decode("utf-8", "replace")


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def remove_tree(path: Path) -> None:
    """Remove ``path``, also when a test left entries without permissions;
    what still cannot be removed is moved aside so the run continues."""
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    # Make every directory searchable and writable before descending into it.
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    for root, directories, _files in os.walk(path):
        for name in directories:
            try:
                os.chmod(os.path.join(root, name), 0o700, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
    try:
        shutil.rmtree(path)
    except OSError:
        if path.exists():
            aside = path.with_name(path.name + f".stale-{os.getpid()}-{time.monotonic_ns()}")
            os.replace(path, aside)


def harness_files() -> list[Path]:
    return [HERE / "run.py", HERE / "runner.py", *sorted((HERE / "support").glob("*.py"))]


def identity(args) -> dict:
    """What every result of a run depends on."""
    diff = subprocess.run(["git", "diff", "HEAD"], cwd=REPO, capture_output=True).stdout
    untracked = _git(["ls-files", "--others", "--exclude-standard"], REPO)
    archive = args.runtime_archive
    python_version = subprocess.run(
        [args.python, "-c", "import sys; print(sys.version)"],
        capture_output=True, text=True,
    ).stdout.strip()
    return {
        "pcc_head": _git(["rev-parse", "HEAD"], REPO),
        "pcc_diff_sha256": _sha256_bytes(diff),
        "pcc_untracked": untracked.splitlines(),
        "pcc_command": args.pcc,
        "runtime_archive": str(archive) if archive else "",
        "runtime_archive_sha256": _sha256_file(Path(archive)) if archive else "",
        "cpython_src": str(args.cpython_src),
        "cpython_head": _git(["rev-parse", "HEAD"], args.cpython_src),
        "cpython_describe": _git(["describe", "--tags", "--always"], args.cpython_src),
        "baseline_python": args.python,
        "baseline_python_version": python_version,
        "harness_sha256": {path.name if path.parent == HERE else "support/" + path.name:
                           _sha256_file(path) for path in harness_files()},
        "platform": platform.platform(),
    }


def identity_key(value: dict) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True).encode("utf-8"))[:16]


def stage(cpython_src: Path, stage_dir: Path) -> None:
    """The ``test`` package: the shim, the real pure helpers, and links to
    every other entry of the checkout's ``Lib/test``."""
    lib_test = cpython_src / "Lib" / "test"
    if stage_dir.exists():
        remove_tree(stage_dir)
    test_dir = stage_dir / "test"
    test_dir.mkdir(parents=True)
    for entry in sorted(lib_test.iterdir()):
        if entry.name in ("support", "__pycache__"):
            continue
        (test_dir / entry.name).symlink_to(entry)
    support = test_dir / "support"
    support.mkdir()
    for path in sorted((HERE / "support").glob("*.py")):
        shutil.copy2(path, support / path.name)
    for name in REAL_SUPPORT_FILES:
        source = lib_test / "support" / name
        if source.is_dir():
            shutil.copytree(source, support / name)
        elif source.exists():
            shutil.copy2(source, support / name)
    shutil.copy2(HERE / "runner.py", stage_dir / "_pcc_conformance_runner.py")


def discover_modules(cpython_src: Path) -> list[str]:
    """Every top-level ``test_*`` module, every ``test_*`` package (its
    ``__init__`` may hold the cases) and every ``test_*`` module inside such
    a package, nested packages included -- as dotted names below ``test``."""
    lib_test = cpython_src / "Lib" / "test"
    modules = [path.stem for path in lib_test.glob("test_*.py")]
    for init in lib_test.glob("test_*/__init__.py"):
        package = init.parent
        modules.append(package.name)
        for path in package.rglob("test_*.py"):
            parts = path.relative_to(lib_test).with_suffix("").parts
            if all((lib_test.joinpath(*parts[:index]) / "__init__.py").exists()
                   for index in range(1, len(parts))):
                modules.append(".".join(parts))
    return sorted(set(modules))


def import_line(module: str) -> str:
    """The driver's import of ``test.<module>`` as ``conformance_module``."""
    package, _, name = ("test." + module).rpartition(".")
    return f"from {package} import {name} as conformance_module"


def file_stem(module: str) -> str:
    return module.replace(".", "__")


def run_process(command: list[str], *, cwd: Path, stdout: Path, stderr: Path,
                timeout: float, env: dict | None = None) -> dict:
    """Run ``command`` in its own process group; kill the group on timeout."""
    started = time.monotonic()
    with open(stdout, "wb") as out, open(stderr, "wb") as err:
        process = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err,
                                   env=env, start_new_session=True)
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait()
    result = {
        "returncode": returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 2),
    }
    if returncode < 0:
        try:
            result["signal"] = signal.Signals(-returncode).name
        except ValueError:
            result["signal"] = str(-returncode)
    return result


def parse_output(path: Path) -> dict:
    """The plan, the case results and the DONE marker of a runner output."""
    plan, cases, done = [], {}, None
    if path.exists():
        for raw in path.read_bytes().decode("utf-8", "replace").splitlines():
            if not raw.startswith(MARK + " "):
                continue
            line = raw[len(MARK) + 1:]
            if line.startswith("CLASS "):
                parts = line[6:].split()
                if len(parts) >= 2:
                    plan.append([parts[0], parts[1], parts[2:]])
            elif line.startswith("CASE "):
                body, _, detail = line[5:].partition(" :: ")
                status, _, case_id = body.partition(" ")
                cases[case_id] = [status, detail]
            elif line.startswith("DONE "):
                done = int(line[5:].strip() or 0)
    return {"plan": plan, "cases": cases, "done": done}


def first_error(text: str) -> str:
    """The most specific error line of a compiler or interpreter stderr."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for prefix in ("pcc frontend worker failed:", "error:"):
        for line in lines:
            if line.startswith(prefix):
                return line[:500]
    for line in reversed(lines):
        if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b", line):
            return line[:500]
    return lines[-1][:500] if lines else ""


def native_driver(module: str, plan: list) -> str:
    lines = [
        "import _pcc_conformance_runner",
        import_line(module),
        "PLAN = [",
    ]
    for attribute, class_id, names in plan:
        lines.append(f"    ({attribute!r}, {class_id!r}, {names!r}),")
    lines += ["]", "_pcc_conformance_runner.run_plan(conformance_module, PLAN)", ""]
    return "\n".join(lines)


def score(baseline: dict, native: dict) -> dict:
    expected = [case_id for case_id, (status, _) in baseline.items() if status in SUCCESS]
    passed = [case_id for case_id in expected if native.get(case_id, ["", ""])[0] in SUCCESS]
    return {
        "baseline_cases": len(baseline),
        "baseline_success": len(expected),
        "native_success": len(passed),
        "native_unreported": sum(1 for case_id in expected if case_id not in native),
        "native_only": sorted(set(native) - set(baseline)),
    }


def run_module(module: str, args, paths: dict, key: str) -> dict:
    work = paths["work"] / module
    if work.exists():
        remove_tree(work)
    (work / "cpython").mkdir(parents=True)
    (work / "native").mkdir()
    stage_dir = paths["stage"]
    baseline_driver = stage_dir / f"conformance_{file_stem(module)}.py"
    baseline_driver.write_text(
        "import _pcc_conformance_runner\n"
        + import_line(module) + "\n"
        "_pcc_conformance_runner.run_module(conformance_module)\n",
        encoding="utf-8",
    )
    result = {"module": module, "identity": key}
    cpython = run_process(
        [args.python, "-E", "-s", str(baseline_driver)],
        cwd=work / "cpython", stdout=work / "cpython.out", stderr=work / "cpython.err",
        timeout=args.run_timeout,
    )
    parsed = parse_output(work / "cpython.out")
    cpython["done"] = parsed["done"]
    cpython["error"] = "" if parsed["done"] is not None else first_error(_tail(work / "cpython.err"))
    result["cpython"] = cpython
    result["plan"] = parsed["plan"]
    baseline = parsed["cases"]
    result["baseline"] = baseline
    native: dict = {}
    if args.baseline_only:
        result["native"] = {"skipped": "baseline only"}
    elif not parsed["plan"]:
        result["native"] = {"skipped": "no baseline plan"}
    else:
        driver = stage_dir / f"conformance_{file_stem(module)}_native.py"
        driver.write_text(native_driver(module, parsed["plan"]), encoding="utf-8")
        binary = paths["bin"] / file_stem(module)
        if binary.exists():
            binary.unlink()
        env = dict(os.environ)
        if args.runtime_archive:
            env["PCC_RUNTIME_ARCHIVE"] = str(args.runtime_archive)
        compile_result = run_process(
            [*shlex.split(args.pcc), "--backend", "self", "--python-libpython=off",
             str(driver), "-o", str(binary)],
            cwd=REPO, stdout=work / "compile.out", stderr=work / "compile.err",
            timeout=args.compile_timeout, env=env,
        )
        compile_result["error"] = ""
        if compile_result["returncode"] != 0 or not binary.exists():
            compile_result["error"] = first_error(_tail(work / "compile.err")) or (
                "timeout" if compile_result["timed_out"] else "no binary")
        native_result = {"compile": compile_result}
        if not compile_result["error"]:
            run_result = run_process(
                [str(binary)], cwd=work / "native", stdout=work / "native.out",
                stderr=work / "native.err", timeout=args.run_timeout,
            )
            parsed_native = parse_output(work / "native.out")
            native = parsed_native["cases"]
            run_result["done"] = parsed_native["done"]
            run_result["error"] = ""
            if parsed_native["done"] is None:
                run_result["error"] = first_error(_tail(work / "native.err")) or (
                    "timeout" if run_result["timed_out"] else "no DONE marker")
            native_result["run"] = run_result
        result["native"] = native_result
    result["native_cases"] = native
    result["score"] = score(baseline, native)
    _write_json(paths["results"] / f"{module}.json", result)
    return result


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def category(detail: str) -> str:
    """A failure detail with its varying parts (quoted text, numbers) masked."""
    text = _QUOTED.sub("'…'", detail)
    text = re.sub(r"\b0x[0-9a-fA-F]+\b|\d+", "N", text)
    marker = "no-libpython function unavailable:"
    if marker in text:
        text = text[:text.index(marker) + len(marker)] + " …"
    return text[:160]


def summarize(paths: dict, key: str) -> dict:
    results = []
    for path in sorted(paths["results"].glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("identity") == key:
            results.append(value)
    totals = {"modules": len(results), "baseline_cases": 0, "baseline_success": 0,
              "native_success": 0, "native_unreported": 0}
    no_baseline, compile_failed, native_crashed = [], [], []
    failure_categories: dict = {}
    compile_categories: dict = {}
    per_module = []
    for result in results:
        module_score = result["score"]
        for name in ("baseline_cases", "baseline_success", "native_success", "native_unreported"):
            totals[name] += module_score[name]
        if not result["plan"]:
            no_baseline.append([result["module"], result["cpython"].get("error", "")])
            continue
        native = result.get("native", {})
        compile_result = native.get("compile", {})
        if compile_result.get("error"):
            compile_failed.append([result["module"], compile_result["error"]])
            name = category(compile_result["error"])
            compile_categories[name] = compile_categories.get(name, 0) + module_score["baseline_success"]
        run_result = native.get("run", {})
        if run_result.get("error"):
            native_crashed.append([result["module"], run_result["error"]])
        for case_id, (status, _detail) in result["baseline"].items():
            if status not in SUCCESS:
                continue
            native_status, native_detail = result["native_cases"].get(case_id, ["", ""])
            if native_status in SUCCESS or not result["native_cases"]:
                continue
            name = category(native_status + " " + (native_detail or "(no detail)"))
            failure_categories[name] = failure_categories.get(name, 0) + 1
        per_module.append([result["module"], module_score["baseline_success"],
                           module_score["native_success"]])
    rate = (totals["native_success"] / totals["baseline_success"]) if totals["baseline_success"] else 0.0
    summary = {
        "identity": key,
        "totals": totals,
        "compatibility_rate": round(rate, 6),
        "modules_without_baseline": no_baseline,
        "modules_compile_failed": compile_failed,
        "modules_native_incomplete": native_crashed,
        "compile_failure_categories": sorted(compile_categories.items(), key=lambda item: -item[1]),
        "case_failure_categories": sorted(failure_categories.items(), key=lambda item: -item[1]),
        "per_module": sorted(per_module, key=lambda item: item[2] - item[1]),
    }
    _write_json(paths["out"] / "summary.json", summary)
    (paths["out"] / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    return summary


def summary_markdown(summary: dict) -> str:
    totals = summary["totals"]
    lines = [
        "# CPython Lib/test conformance",
        "",
        f"- identity: `{summary['identity']}` (see identity.json)",
        f"- modules: {totals['modules']}, without a baseline: {len(summary['modules_without_baseline'])}, "
        f"compile failures: {len(summary['modules_compile_failed'])}, "
        f"incomplete native runs: {len(summary['modules_native_incomplete'])}",
        f"- cases succeeding under CPython: {totals['baseline_success']} "
        f"(of {totals['baseline_cases']} collected)",
        f"- of those, succeeding natively: {totals['native_success']} "
        f"(unreported natively: {totals['native_unreported']})",
        f"- compatibility rate: {summary['compatibility_rate'] * 100:.2f}%",
        "",
        "## Compile failures by cause (CPython-succeeding cases lost)",
        "",
    ]
    for name, count in summary["compile_failure_categories"][:40]:
        lines.append(f"- {count}: {name}")
    lines += ["", "## Native case failures by cause", ""]
    for name, count in summary["case_failure_categories"][:60]:
        lines.append(f"- {count}: {name}")
    lines += ["", "## Modules without a baseline", ""]
    for module, error in summary["modules_without_baseline"]:
        lines.append(f"- {module}: {error}")
    lines += ["", "## Per module (CPython-succeeding, native-succeeding)", ""]
    for module, expected, passed in summary["per_module"]:
        lines.append(f"- {module}: {passed}/{expected}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cpython-src", type=Path, required=True,
                        help="CPython checkout whose Lib/test is measured")
    parser.add_argument("--out", type=Path, required=True,
                        help="output directory, outside the repository")
    parser.add_argument("--modules", nargs="*", default=None,
                        help="test module names (default: every Lib/test/test_*.py)")
    parser.add_argument("--python", default=sys.executable,
                        help="CPython interpreter for the baseline")
    parser.add_argument("--pcc", default=f"uv run --project {shlex.quote(str(REPO))} pcc",
                        help="pcc command that compiles a driver (default: this checkout's host pcc)")
    parser.add_argument("--runtime-archive", default=os.environ.get("PCC_RUNTIME_ARCHIVE"),
                        help="pcc-Python runtime archive passed as PCC_RUNTIME_ARCHIVE")
    parser.add_argument("--compile-timeout", type=float, default=900.0)
    parser.add_argument("--run-timeout", type=float, default=300.0)
    parser.add_argument("--jobs", type=int, default=1,
                        help="modules measured at the same time (each runs its own compiler)")
    parser.add_argument("--baseline-only", action="store_true",
                        help="run the CPython baseline only (checks the shim)")
    parser.add_argument("--resume", action="store_true",
                        help="keep results whose identity matches this run")
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args(argv)

    args.cpython_src = args.cpython_src.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if out == REPO or REPO in out.parents:
        parser.error("--out must be outside the repository")
    paths = {"out": out, "stage": out / "stage", "work": out / "work",
             "results": out / "results", "bin": out / "bin"}
    for name in ("results", "work", "bin"):
        paths[name].mkdir(parents=True, exist_ok=True)
    run_identity = identity(args)
    key = identity_key(run_identity)
    _write_json(out / "identity.json", {"key": key, **run_identity})
    if args.summarize_only:
        summary = summarize(paths, key)
        print(json.dumps(summary["totals"]), summary["compatibility_rate"])
        return 0
    stage(args.cpython_src, paths["stage"])
    modules = args.modules or discover_modules(args.cpython_src)
    pending = []
    for module in modules:
        existing = paths["results"] / f"{module}.json"
        if args.resume and existing.exists():
            previous = json.loads(existing.read_text(encoding="utf-8"))
            baseline_only = previous.get("native", {}).get("skipped") == "baseline only"
            if previous.get("identity") == key and (args.baseline_only or not baseline_only):
                continue
        pending.append(module)
    print(f"identity {key}: {len(pending)} of {len(modules)} modules to run", flush=True)

    def report(result: dict) -> None:
        module_score = result["score"]
        native = result.get("native", {})
        note = native.get("skipped") or native.get("compile", {}).get("error") or \
            native.get("run", {}).get("error") or ""
        print(f"{result['module']}: {module_score['native_success']}/"
              f"{module_score['baseline_success']} {note[:160]}", flush=True)

    def measure(module: str):
        try:
            return run_module(module, args, paths, key)
        except Exception as error:
            # A harness failure for one module is recorded, not fatal.
            print(f"{module}: harness error {type(error).__name__}: {error}", flush=True)
            return None

    if args.jobs <= 1:
        for module in pending:
            result = measure(module)
            if result is not None:
                report(result)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = [executor.submit(measure, module) for module in pending]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    report(result)
    summary = summarize(paths, key)
    print(json.dumps(summary["totals"]), f"rate={summary['compatibility_rate'] * 100:.2f}%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
