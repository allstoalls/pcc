import argparse
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.clang_c_cases import case_config, compile_native, compile_pcc, run_native, run_pcc


CLANG_C_TESTS_DIR = REPO_ROOT / "projects" / "clang-c-tests"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "clang_c_manifest.json"


def _classify_compile_only(case_path):
    native = compile_native(case_path, REPO_ROOT)
    pcc = compile_pcc(case_path)

    if native.returncode == 0 and pcc.returncode == 0:
        return "compile_only_success"
    if native.returncode != 0 and pcc.returncode != 0:
        return "compile_only_both_fail"
    if native.returncode == 0 and pcc.returncode != 0:
        return "compile_only_native_pass_pcc_fail"
    return "compile_only_native_fail_pcc_pass"


def _classify_runtime(case_path):
    try:
        native = run_native(case_path, REPO_ROOT)
    except subprocess.TimeoutExpired:
        return "runtime_timeout"
    except Exception:
        return "runtime_build_or_execution_failure"

    try:
        pcc = run_pcc(case_path, REPO_ROOT)
    except subprocess.TimeoutExpired:
        return "runtime_timeout"
    except Exception:
        return "runtime_build_or_execution_failure"

    # A compiler/linker/worker status is never a program exit status. Keep
    # rejected fixtures visible without promoting them to runtime matches.
    if not native.executed or not pcc.executed:
        # A completed exit 124 is legitimate; only an incomplete stage
        # carrying the timeout status identifies a timeout.
        if any(
            not result.stages[-1].completed and result.returncode == 124
            for result in (native, pcc)
        ):
            return "runtime_timeout"
        return "runtime_build_or_execution_failure"

    if native.returncode == 0 and pcc.returncode == 0:
        if pcc.stdout == native.stdout and pcc.stderr == native.stderr:
            return "runtime_exact_match"
        return "runtime_returncode_match_only"

    if native.returncode == pcc.returncode:
        if pcc.stdout == native.stdout and pcc.stderr == native.stderr:
            return "runtime_exact_match"
        return "runtime_returncode_match_only"

    if native.returncode != 0 and pcc.returncode != 0:
        return "runtime_both_fail"

    if native.returncode != 0 and pcc.returncode == 0:
        return "runtime_native_fail_pcc_pass"

    return "runtime_mismatch"


def build_manifest():
    categories = {}
    for case_path in sorted(CLANG_C_TESTS_DIR.glob("*.c")):
        config = case_config(case_path)
        if config.mode == "compile_only":
            category = _classify_compile_only(case_path)
        else:
            category = _classify_runtime(case_path)
        categories.setdefault(category, []).append(case_path.name)
    return {key: sorted(value) for key, value in sorted(categories.items())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Where to write the JSON manifest.",
    )
    args = parser.parse_args()

    manifest = build_manifest()
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
