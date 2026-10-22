import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.c_testsuite_cases import DEFAULT_TIMEOUT, read_expected_output, run_native, run_pcc


C_TESTSUITE_DIR = REPO_ROOT / "projects" / "c-testsuite"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "c_testsuite_manifest.json"


def _classify_runtime(case_path, timeout):
    expected_output = read_expected_output(case_path)

    try:
        native = run_native(case_path, REPO_ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "runtime_timeout", case_path.name
    except Exception:
        return "runtime_build_or_execution_failure", case_path.name

    try:
        pcc = run_pcc(case_path, REPO_ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "runtime_timeout", case_path.name
    except Exception:
        return "runtime_build_or_execution_failure", case_path.name

    # Build/worker statuses are not program exits. A completed exit 124 is
    # legitimate; only an incomplete stage record identifies a timeout.
    if not native.executed or not pcc.executed:
        if any(
            result.stages
            and not result.stages[-1].completed
            and result.returncode == 124
            for result in (native, pcc)
        ):
            return "runtime_timeout", case_path.name
        return "runtime_build_or_execution_failure", case_path.name

    if native.returncode == 0 and pcc.returncode == 0:
        if pcc.stdout == native.stdout and pcc.stderr == native.stderr:
            if expected_output and pcc.stdout == expected_output:
                return "runtime_exact_match", case_path.name
            if not expected_output:
                return "runtime_exact_match", case_path.name
            return "runtime_returncode_match_only", case_path.name
        return "runtime_returncode_match_only", case_path.name

    if native.returncode == pcc.returncode:
        if pcc.stdout == native.stdout and pcc.stderr == native.stderr:
            return "runtime_exact_match", case_path.name
        return "runtime_returncode_match_only", case_path.name

    if native.returncode != 0 and pcc.returncode != 0:
        return "runtime_both_fail", case_path.name

    if native.returncode != 0 and pcc.returncode == 0:
        return "runtime_native_fail_pcc_pass", case_path.name

    return "runtime_native_pass_pcc_fail", case_path.name


def _classify_case_paths(case_paths, jobs, retry_jobs, timeout):
    categories = {}
    classifier = partial(_classify_runtime, timeout=timeout)
    timeout_cases = []

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        for category, filename in executor.map(classifier, case_paths):
            if category == "runtime_timeout":
                timeout_cases.append(filename)
                continue
            categories.setdefault(category, []).append(filename)

    if timeout_cases:
        with ThreadPoolExecutor(max_workers=retry_jobs) as executor:
            retry_paths = [C_TESTSUITE_DIR / filename for filename in timeout_cases]
            for category, filename in executor.map(classifier, retry_paths):
                categories.setdefault(category, []).append(filename)

    return {key: sorted(value) for key, value in sorted(categories.items())}


def build_manifest(jobs, retry_jobs, timeout):
    case_paths = sorted(C_TESTSUITE_DIR.glob("*.c"))
    return _classify_case_paths(case_paths, jobs, retry_jobs, timeout)


def refresh_manifest_category(existing_manifest, category, jobs, retry_jobs, timeout):
    case_paths = [
        C_TESTSUITE_DIR / filename
        for filename in existing_manifest.get(category, [])
    ]
    refreshed = _classify_case_paths(case_paths, jobs, retry_jobs, timeout)
    merged = {
        key: list(value) for key, value in existing_manifest.items() if key != category
    }
    for key, cases in refreshed.items():
        merged.setdefault(key, []).extend(cases)

    for key in list(merged):
        merged[key] = sorted(set(merged[key]))
        if not merged[key]:
            del merged[key]
    return merged


def main():
    parser = argparse.ArgumentParser(
        description="Classify c-testsuite cases and write a JSON manifest."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Where to write the JSON manifest.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=8,
        help="Maximum parallel workers while classifying cases.",
    )
    parser.add_argument(
        "--retry-jobs",
        type=int,
        default=2,
        help="Lower-concurrency retry workers for cases that initially time out.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Per-side timeout budget in seconds.",
    )
    parser.add_argument(
        "--only-category",
        help="Reclassify only the cases currently listed in one manifest category.",
    )
    args = parser.parse_args()

    if args.only_category:
        existing_manifest = json.loads(args.output.read_text())
        manifest = refresh_manifest_category(
            existing_manifest,
            args.only_category,
            args.jobs,
            args.retry_jobs,
            args.timeout,
        )
    else:
        manifest = build_manifest(args.jobs, args.retry_jobs, args.timeout)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    total = sum(len(v) for v in manifest.values())
    print(f"Wrote {total} cases across {len(manifest)} categories to {args.output}")
    for cat, cases in sorted(manifest.items()):
        print(f"  {cat}: {len(cases)}")


if __name__ == "__main__":
    main()
