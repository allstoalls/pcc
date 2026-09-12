#!/usr/bin/env bash
#
# scripts/bootstrap.sh — P6C.6 three-stage bootstrap for pcc.
#
# Per docs/plans/python-frontend-plan.md §Phase 6C.6, the gate for
# declaring pcc self-hosted is:
#
#   stage1: CPython runs pcc, compiles pcc/pcc.py  → pcc1 (native)
#   stage2: ./pcc1 compiles pcc/pcc.py             → pcc2 (native)
#   stage3: ./pcc2 compiles pcc/pcc.py             → pcc3 (native)
#   verify: cmp pcc2 pcc3   (byte-identical; structural-equivalent OK
#                            if build metadata is stripped)
#
# Status (2026-04-23): the script completes stage1/stage2/stage3 on the
# supported macOS arm64 development host. Direct cmp still differs on
# Mach-O code-signature / LC_UUID metadata, so verification normalizes
# temporary comparison copies before declaring success.
#
# Usage:
#   scripts/bootstrap.sh               # full three-stage + cmp
#                                      # defaults to self on macOS arm64
#   scripts/bootstrap.sh --stage 1     # stop after stage1
#   scripts/bootstrap.sh --backend self --stage 1
#   scripts/bootstrap.sh --backend llvm --stage 1
#   scripts/bootstrap.sh --out-dir build/bootstrap-self --backend self --stage 1
#   scripts/bootstrap.sh --stage 3 --reuse-stage1
#                                      # reuse an existing OUT_DIR/pcc1 and run
#                                      # only stage2/stage3 (pcc1 is backend-
#                                      # agnostic; build it once, reuse it).
#   scripts/bootstrap.sh --from-stage 3 --stage 3 --reuse-stage1
#                                      # run only stage3 + pcc2/pcc3 verify
#                                      # against an existing OUT_DIR/pcc2.
#   scripts/bootstrap.sh --clean       # remove all stage artifacts
#
# Runtime defaults for every stage:
#   PCC_BOOTSTRAP_RUNTIME_CC=pcc
#   PCC_BOOTSTRAP_RUNTIME_HIGH=py
#   PCC_BOOTSTRAP_PYTHON_LIBPYTHON=off
#   PCC_BOOTSTRAP_PYTHON_IR_PASSES=${PCC_PYTHON_IR_PASSES:-off}
#   PCC_BOOTSTRAP_PY_FRONTEND_JOBS=${PCC_PY_FRONTEND_JOBS:-auto} for stage2+
#   PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS=${PCC_PY_FRONTEND_JOBS:-2}
#   PCC_BOOTSTRAP_SELF_BACKEND_JOBS=${PCC_SELF_BACKEND_JOBS:-2}
#   PCC_BOOTSTRAP_MACHO_LINK_JOBS=${PCC_MACHO_LINK_JOBS:-8}
#   PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES=17179869184
#   PCC_BOOTSTRAP_STAGE_TIMEOUT=600
#
# The Stage2+ auto default is a host-safety contract: it runs oversized modules
# serially and caps the safe codegen pool at two workers.  A numeric override
# above two requires PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS=1; ordinary agents,
# tests and performance runners must never set that escape hatch.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${PCC_BOOTSTRAP_OUT_DIR:-${REPO_ROOT}/build/bootstrap}"
MAIN_PY="${REPO_ROOT}/pcc/__main__.py"
BOOTSTRAP_RUNTIME_CC="${PCC_BOOTSTRAP_RUNTIME_CC:-pcc}"
BOOTSTRAP_RUNTIME_HIGH="${PCC_BOOTSTRAP_RUNTIME_HIGH:-py}"
BOOTSTRAP_PYTHON_LIBPYTHON="${PCC_BOOTSTRAP_PYTHON_LIBPYTHON:-off}"
BOOTSTRAP_PYTHON_IR_PASSES="${PCC_BOOTSTRAP_PYTHON_IR_PASSES:-${PCC_PYTHON_IR_PASSES:-off}}"
_BOOTSTRAP_SAFE_MAX_JOBS=2
_BOOTSTRAP_SAFE_MAX_LINK_JOBS=8
_BOOTSTRAP_SAFE_MAX_TREE_RSS_BYTES=17179869184
# A cold stage1 measured 383s, 391s, 400s, 505s and 546s across runs on
# 2026-09-11; a 600s cap leaves under 20% margin over the observed spread
# and fired as a false MEMORY/TIMEOUT failure twice in one evening, once
# purely from CPU contention with a concurrent test suite.  The watchdog
# is there to stop a runaway, not to race a healthy build.
_BOOTSTRAP_SAFE_MAX_STAGE_TIMEOUT=2400
BOOTSTRAP_PY_FRONTEND_JOBS="${PCC_BOOTSTRAP_PY_FRONTEND_JOBS:-${PCC_PY_FRONTEND_JOBS:-auto}}"
BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS="${PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS:-${PCC_PY_FRONTEND_JOBS:-2}}"
BOOTSTRAP_SELF_BACKEND_JOBS="${PCC_BOOTSTRAP_SELF_BACKEND_JOBS:-${PCC_SELF_BACKEND_JOBS:-2}}"
BOOTSTRAP_MACHO_LINK_JOBS="${PCC_BOOTSTRAP_MACHO_LINK_JOBS:-${PCC_MACHO_LINK_JOBS:-8}}"
BOOTSTRAP_MAX_TREE_RSS_BYTES="${PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES:-17179869184}"
BOOTSTRAP_STAGE_TIMEOUT="${PCC_BOOTSTRAP_STAGE_TIMEOUT:-600}"
BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES="${PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES:-8589934592}"
BOOTSTRAP_EXTERNAL_MEMORY_GUARD="${PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD:-0}"
BOOTSTRAP_IN_PROCESS_CODEGEN="${PCC_BOOTSTRAP_IN_PROCESS_CODEGEN:-0}"
BOOTSTRAP_DEFER_FRONTEND_CODEGEN="${PCC_BOOTSTRAP_DEFER_FRONTEND_CODEGEN:-1}"
BOOTSTRAP_DEFER_SELF_LINK="${PCC_BOOTSTRAP_DEFER_SELF_LINK:-1}"
BOOTSTRAP_PROFILE_DIR="${PCC_BOOTSTRAP_PROFILE_DIR:-}"
BOOTSTRAP_STAGE_EXEC_DELAY="${PCC_BOOTSTRAP_STAGE_EXEC_DELAY:-0.10}"

if [[ "${BOOTSTRAP_SELF_BACKEND_JOBS}" == "auto" ]]; then
    BOOTSTRAP_SELF_BACKEND_JOBS=2
fi
if [[ "${BOOTSTRAP_MACHO_LINK_JOBS}" == "auto" ]]; then
    BOOTSTRAP_MACHO_LINK_JOBS=8
fi

validate_bootstrap_worker_budget() {
    local label="$1"
    local value="$2"
    local safe_max="$3"
    if [[ "${value}" =~ ^[0-9]+$ ]] && (( value > safe_max )); then
        if [[ "${PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS:-0}" != "1" ]]; then
            echo "unsafe bootstrap worker budget: ${label}=${value} exceeds ${safe_max}; set PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS=1 only for an explicitly isolated machine" >&2
            exit 2
        fi
    fi
}

validate_bootstrap_worker_budget \
    "PCC_BOOTSTRAP_PY_FRONTEND_JOBS" "${BOOTSTRAP_PY_FRONTEND_JOBS}" "${_BOOTSTRAP_SAFE_MAX_JOBS}"
validate_bootstrap_worker_budget \
    "PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS" "${BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS}" "${_BOOTSTRAP_SAFE_MAX_JOBS}"
validate_bootstrap_worker_budget \
    "PCC_SELF_BACKEND_JOBS" "${BOOTSTRAP_SELF_BACKEND_JOBS}" "${_BOOTSTRAP_SAFE_MAX_JOBS}"
validate_bootstrap_worker_budget \
    "PCC_MACHO_LINK_JOBS" "${BOOTSTRAP_MACHO_LINK_JOBS}" "${_BOOTSTRAP_SAFE_MAX_LINK_JOBS}"

validate_bootstrap_resource_limit() {
    local label="$1"
    local value="$2"
    local safe_max="$3"
    if [[ ! "${value}" =~ ^[0-9]+$ ]] || (( value < 1 )); then
        echo "invalid bootstrap resource limit: ${label}=${value}" >&2
        exit 2
    fi
    if (( value > safe_max )) && [[ "${PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS:-0}" != "1" ]]; then
        echo "unsafe bootstrap resource limit: ${label}=${value} exceeds ${safe_max}" >&2
        exit 2
    fi
}

validate_bootstrap_resource_limit \
    "PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES" "${BOOTSTRAP_MAX_TREE_RSS_BYTES}" "${_BOOTSTRAP_SAFE_MAX_TREE_RSS_BYTES}"
validate_bootstrap_resource_limit \
    "PCC_BOOTSTRAP_STAGE_TIMEOUT" "${BOOTSTRAP_STAGE_TIMEOUT}" "${_BOOTSTRAP_SAFE_MAX_STAGE_TIMEOUT}"
validate_bootstrap_resource_limit \
    "PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES" "${BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES}" 8589934592

STAGE_LIMIT=3
START_STAGE=1
CLEAN=0
BACKEND=""
BACKEND_EXPLICIT=0
# When 1, skip the (backend-agnostic, CPython-hosted) stage1 build if a usable
# pcc1 already exists in OUT_DIR and start from stage2. Lets callers build pcc1
# once and reuse it across many stage2/stage3 runs (e.g. one bootstrap per GC
# backend), since PCC_GC_BACKEND only affects stage2+ runtime, not pcc1.
REUSE_STAGE1="${PCC_BOOTSTRAP_REUSE_STAGE1:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage)   STAGE_LIMIT="$2"; shift 2 ;;
        --from-stage|--start-stage) START_STAGE="$2"; shift 2 ;;
        --reuse-stage1) REUSE_STAGE1=1; shift ;;
        --backend) BACKEND="$2"; BACKEND_EXPLICIT=1; shift 2 ;;
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        --clean)   CLEAN=1; shift ;;
        -h|--help) sed -n '3,27p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "${BACKEND}" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        machine="$(uname -m)"
        if [[ "${machine}" == "arm64" || "${machine}" == "aarch64" ]]; then
            BACKEND="self"
        else
            BACKEND="llvm"
        fi
    else
        BACKEND="llvm"
    fi
fi

if [[ $CLEAN -eq 1 ]]; then
    rm -rf "${OUT_DIR}"
    echo "cleaned ${OUT_DIR}"
    exit 0
fi

mkdir -p "${OUT_DIR}"

# Content-addressed compiler caches: reuse GC-invariant frontend IR bundles
# and self-backend objects across equivalent invocations. The caches only
# activate when the host supplies identity namespaces (a compiled stage
# binary cannot hash its own implementation sources), so derive them here
# when the caller has not already namespaced the run (the pytest bootstrap
# helper sets its own). PCC_SELF_BACKEND_OBJECT_CACHE=0 disables both.
if [[ -z "${PCC_PY_FRONTEND_IR_CACHE_IDENTITY:-}" || -z "${PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY:-}" ]]; then
    if command -v uv >/dev/null 2>&1; then
        bootstrap_identities="$(cd "${REPO_ROOT}" && env -u LC_ALL uv run python -m pcc.bootstrap_cache_identity 2>/dev/null || true)"
        frontend_identity="$(printf '%s\n' "${bootstrap_identities}" | sed -n '1p')"
        object_identity="$(printf '%s\n' "${bootstrap_identities}" | sed -n '2p')"
        if [[ -n "${frontend_identity}" && -n "${object_identity}" ]]; then
            export PCC_PY_FRONTEND_IR_CACHE_IDENTITY="${PCC_PY_FRONTEND_IR_CACHE_IDENTITY:-${frontend_identity}}"
            export PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY="${PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY:-${object_identity}}"
        fi
    fi
fi
# Share one cache namespace with the pytest bootstrap helper so suite
# provisioning, gate chains, and manual bootstraps reuse each other's work.
export PCC_SELF_BACKEND_OBJECT_CACHE_DIR="${PCC_SELF_BACKEND_OBJECT_CACHE_DIR:-${REPO_ROOT}/build/bootstrap-pytest-object-cache}"

# NOTE (2026-08-27): a physical-memory-derived oversized admission cap
# (hw.memsize/2700 -> one wide wave on a 96 GB Mac) was tried here and
# REMOVED: physical RAM is not available RAM.  The widened wave was run on
# a machine that had drifted to 23.7/24 GB swap used and the stage died
# with silently killed workers; a control with the conservative cap failed
# identically, so the machine state — not the cap — decided, and the safe
# in-compiler default (7 MB pairs, receipted at -58 s) is the one shape
# proven green with a fixed point.  If a wider default is wanted, derive
# it from AVAILABLE memory at launch time, not hw.memsize.
# PCC_SELF_BACKEND_OVERSIZED_BYTE_CAP remains an explicit-user knob only.

banner() {
    echo ""
    echo "=============================================================="
    echo " $*"
    echo "=============================================================="
}

now_ms() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time; print(int(time.monotonic() * 1000))'
    else
        echo "$(($(date +%s) * 1000))"
    fi
}

stage_exec_barrier() {
    local out_exe="$1"
    if [[ "${BACKEND}" != "self" ]]; then
        return
    fi
    if [[ "$(uname)" != "Darwin" ]]; then
        return
    fi
    if command -v codesign >/dev/null 2>&1; then
        codesign --verify "${out_exe}" >/dev/null 2>&1 || true
    fi
    cat "${out_exe}" >/dev/null
    if [[ -n "${BOOTSTRAP_STAGE_EXEC_DELAY}" && "${BOOTSTRAP_STAGE_EXEC_DELAY}" != "0" ]]; then
        sleep "${BOOTSTRAP_STAGE_EXEC_DELAY}"
    fi
    "${out_exe}" --help >/dev/null 2>&1
    local smoke_dir
    smoke_dir="$(mktemp -d "${OUT_DIR}/stage-smoke.XXXXXX")"
    local smoke_src="${smoke_dir}/smoke.py"
    local smoke_out="${smoke_dir}/smoke"
    printf 'def main() -> int:\n    return 0\n\nmain()\n' > "${smoke_src}"
    env \
        "PCC_RUNTIME_CC=${BOOTSTRAP_RUNTIME_CC}" \
        "PCC_RUNTIME_HIGH=${BOOTSTRAP_RUNTIME_HIGH}" \
        "${out_exe}" \
        --ir-scaffold=on \
        --backend "${BACKEND}" \
        --python-libpython "${BOOTSTRAP_PYTHON_LIBPYTHON}" \
        "${smoke_src}" -o "${smoke_out}" >/dev/null 2>&1
    local smoke_returncode=$?
    rm -rf "${smoke_dir}"
    return "${smoke_returncode}"
}

write_stage_result_json() {
    local stage="$1"
    local out_exe="$2"
    local compile_elapsed_ms="$3"
    local barrier_elapsed_ms="$4"
    local stage_elapsed_ms="$5"
    local returncode="$6"
    local time_file="$7"
    local barrier_returncode="$8"
    if [[ -z "${BOOTSTRAP_PROFILE_DIR}" ]]; then
        return
    fi
    python3 - "$BOOTSTRAP_PROFILE_DIR/stage${stage}.result.json" \
        "$stage" "$out_exe" "$BACKEND" "$compile_elapsed_ms" \
        "$barrier_elapsed_ms" "$stage_elapsed_ms" "$returncode" \
        "$time_file" "$barrier_returncode" <<'PY'
from __future__ import annotations

import json
import sys

path = sys.argv[1]
stage, output, backend = sys.argv[2:5]
compile_ms, barrier_ms, wall_ms, returncode, time_file = sys.argv[5:10]
barrier_returncode = sys.argv[10]
payload = {
    "schema": "pcc.bootstrap_stage_result.v1",
    "stage": int(stage),
    "output": output,
    "backend": backend,
    "compile_wall_ms": int(compile_ms),
    "publish_barrier_ms": int(barrier_ms),
    "wall_ms": int(wall_ms),
    "returncode": int(returncode),
    "publish_barrier_returncode": int(barrier_returncode),
    "metric_scopes": {
        "compile_wall_ms": "end_to_end_elapsed",
        "compile_time_real_ms": "end_to_end_elapsed",
        "compile_user_ms": "timed_command_plus_waited_children_cpu",
        "compile_sys_ms": "timed_command_plus_waited_children_cpu",
        "publish_barrier_ms": "end_to_end_elapsed",
        "wall_ms": "end_to_end_elapsed_including_publish_barrier",
    },
    "comparison_contract": {
        "primary_compute_metrics": ["compile_user_ms", "compile_sys_ms"],
        "wall_metric_role": "paired_end_to_end_observation",
        "required_comparison": "adjacent_alternating_same_environment_pairs",
        "single_wall_verdict_allowed": False,
    },
}
if time_file:
    try:
        with open(time_file, "r", encoding="utf-8") as f:
            for raw in f:
                key, sep, value = raw.strip().partition("=")
                if sep != "=":
                    continue
                if key == "user_s":
                    payload["compile_user_ms"] = int(float(value) * 1000)
                elif key == "sys_s":
                    payload["compile_sys_ms"] = int(float(value) * 1000)
                elif key == "real_s":
                    payload["compile_time_real_ms"] = int(float(value) * 1000)
    except OSError:
        pass
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, sort_keys=True)
    f.write("\n")
PY
}

run_stage() {
    local stage="$1"
    local out_exe="$2"
    shift 2
    # Remaining positional args are the compiler invocation (as an
    # array so ``python3 -m pcc`` vs a single executable path both
    # work without shell-quoting gymnastics).
    local cmd=("$@")
    local backend_args=()
    if [[ -n "${BACKEND}" ]]; then
        backend_args=(--backend "${BACKEND}")
    fi
    if [[ -n "${BOOTSTRAP_PROFILE_DIR}" ]]; then
        mkdir -p "${BOOTSTRAP_PROFILE_DIR}"
    fi
    # Never let a failed/short-circuited compile leave a previous run's stage
    # binary in place. Without this, stage3 can accidentally execute a stale
    # pcc2 from the shared pytest/bootstrap directory.
    rm -f "${out_exe}" "${out_exe}.tmp"

    local deferred_plan=""
    local codegen_plan=""
    if [[ "${stage}" != "1" && "${BACKEND}" == "self" ]]; then
        if [[ "${BOOTSTRAP_DEFER_FRONTEND_CODEGEN}" == "1" ]]; then
            codegen_plan="${out_exe}.pcc-codegen-plan"
            rm -f \
                "${codegen_plan}" \
                "${codegen_plan}.internal-inputs" \
                "${codegen_plan}.link-profile.json" \
                "${codegen_plan}.result.json"
        fi
        if [[ "${BOOTSTRAP_DEFER_SELF_LINK}" == "1" ]]; then
            deferred_plan="${out_exe}.pcc-link-plan"
            rm -f \
                "${deferred_plan}" \
                "${deferred_plan}.inputs" \
                "${deferred_plan}.profile.json" \
                "${deferred_plan}.result.json"
        fi
    fi

    local backend_label="${BACKEND}"
    local frontend_jobs="${BOOTSTRAP_PY_FRONTEND_JOBS}"
    if [[ "${stage}" == "1" ]]; then
        frontend_jobs="${BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS}"
    fi
    if [[ "${BACKEND_EXPLICIT}" -eq 0 ]]; then
        backend_label="${BACKEND} (default)"
    fi
    local full_cmd=(
        env
        "PCC_RUNTIME_CC=${BOOTSTRAP_RUNTIME_CC}"
        "PCC_RUNTIME_HIGH=${BOOTSTRAP_RUNTIME_HIGH}"
        "PCC_PYTHON_IR_PASSES=${BOOTSTRAP_PYTHON_IR_PASSES}"
        "PCC_PY_FRONTEND_JOBS=${frontend_jobs}"
        "PCC_SELF_BACKEND_JOBS=${BOOTSTRAP_SELF_BACKEND_JOBS}"
        "PCC_MACHO_LINK_JOBS=${BOOTSTRAP_MACHO_LINK_JOBS}"
        "PCC_WORKER_TREE_BUDGET_BYTES=${BOOTSTRAP_MAX_TREE_RSS_BYTES}"
    )
    if [[ "${stage}" != "1" && "${BACKEND}" == "self" ]]; then
        if [[ "${BOOTSTRAP_IN_PROCESS_CODEGEN}" == "1" ]]; then
            full_cmd+=("PCC_PY_FRONTEND_IN_PROCESS_CODEGEN=1")
        fi
        if [[ -n "${codegen_plan}" ]]; then
            full_cmd+=(
                "PCC_DEFER_FRONTEND_CODEGEN_PLAN=${codegen_plan}"
                "PCC_DEFER_FRONTEND_OUTPUT=${out_exe}"
            )
        fi
        if [[ -n "${deferred_plan}" ]]; then
            full_cmd+=("PCC_DEFER_SELF_LINK_PLAN=${deferred_plan}")
        fi
    fi
    full_cmd+=("${cmd[@]}")
    if [[ -n "${BOOTSTRAP_PROFILE_DIR}" ]]; then
        full_cmd+=(--profile-json "${BOOTSTRAP_PROFILE_DIR}/stage${stage}.json")
    fi
    full_cmd+=(
        "${backend_args[@]}"
        --python-libpython "${BOOTSTRAP_PYTHON_LIBPYTHON}"
        "${MAIN_PY}" -o "${out_exe}"
    )
    banner "stage ${stage}: backend ${backend_label}: ${cmd[*]}"
    echo "input: ${MAIN_PY}"
    echo "output: ${out_exe}"
    echo "runtime: PCC_RUNTIME_CC=${BOOTSTRAP_RUNTIME_CC} PCC_RUNTIME_HIGH=${BOOTSTRAP_RUNTIME_HIGH} PCC_PYTHON_IR_PASSES=${BOOTSTRAP_PYTHON_IR_PASSES} PCC_PY_FRONTEND_JOBS=${frontend_jobs} PCC_SELF_BACKEND_JOBS=${BOOTSTRAP_SELF_BACKEND_JOBS} PCC_MACHO_LINK_JOBS=${BOOTSTRAP_MACHO_LINK_JOBS} --python-libpython ${BOOTSTRAP_PYTHON_LIBPYTHON}"
    if [[ -n "${BOOTSTRAP_PROFILE_DIR}" ]]; then
        echo "profile: ${BOOTSTRAP_PROFILE_DIR}/stage${stage}.json"
    fi
    local stage_start_ms
    local compile_start_ms
    local compile_end_ms
    local compile_elapsed_ms
    local barrier_start_ms
    local barrier_end_ms
    local barrier_elapsed_ms
    local stage_end_ms
    local stage_elapsed_ms
    local stage_returncode
    local barrier_returncode=0
    local stage_time_file=""
    local process_guard_dir=""
    local process_guard_stdout=""
    local process_guard_stderr=""
    local target_cmd=("${full_cmd[@]}")
    if [[ -n "${deferred_plan}" ]]; then
        local deferred_runner=()
        if [[ -n "${PCC_HOST_PYTHON:-}" && -x "${PCC_HOST_PYTHON}" ]]; then
            deferred_runner=("${PCC_HOST_PYTHON}")
        elif command -v uv >/dev/null 2>&1; then
            deferred_runner=(env -u LC_ALL uv run python)
        else
            deferred_runner=(python3)
        fi
        target_cmd=(
            "${deferred_runner[@]}"
            "${REPO_ROOT}/scripts/run_pcc_deferred_link.py"
            --timeout "${BOOTSTRAP_STAGE_TIMEOUT}"
        )
        if [[ -n "${codegen_plan}" ]]; then
            target_cmd+=(--codegen-plan "${codegen_plan}")
        fi
        target_cmd+=("${deferred_plan}" -- "${full_cmd[@]}")
    fi
    local execution_cmd=("${target_cmd[@]}")
    if [[ -n "${BOOTSTRAP_PROFILE_DIR}" ]]; then
        stage_time_file="${BOOTSTRAP_PROFILE_DIR}/stage${stage}.time"
    fi
    if [[ "${BOOTSTRAP_EXTERNAL_MEMORY_GUARD}" != "1" ]]; then
        process_guard_dir="$(mktemp -d "${OUT_DIR}/stage${stage}.process.XXXXXX")"
        process_guard_stdout="${process_guard_dir}/target.stdout"
        process_guard_stderr="${process_guard_dir}/target.stderr"
        local sampler_python=()
        if command -v uv >/dev/null 2>&1; then
            sampler_python=(env -u LC_ALL uv run python)
        else
            sampler_python=(python3)
        fi
        execution_cmd=(
            "${sampler_python[@]}"
            "${REPO_ROOT}/scripts/run_process_tree_sample.py"
            --result "${process_guard_dir}/result.json"
            --samples "${process_guard_dir}/samples.tsv"
            --stdout "${process_guard_stdout}"
            --stderr "${process_guard_stderr}"
            --cwd "${REPO_ROOT}"
            --timeout "${BOOTSTRAP_STAGE_TIMEOUT}"
            --interval 0.25
            --progress-interval 30
            --max-tree-rss-bytes "${BOOTSTRAP_MAX_TREE_RSS_BYTES}"
            --no-performance-lock
        )
        if [[ "$(uname)" == "Darwin" ]]; then
            execution_cmd+=(
                --darwin-preflight-reserve-bytes
                "${BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES}"
            )
        fi
        execution_cmd+=(-- "${target_cmd[@]}")
        echo "process-tree guard: ${process_guard_dir}/result.json cap=${BOOTSTRAP_MAX_TREE_RSS_BYTES} timeout=${BOOTSTRAP_STAGE_TIMEOUT}s"
    fi
    stage_start_ms="$(now_ms)"
    compile_start_ms="${stage_start_ms}"
    set +e
    if command -v time >/dev/null 2>&1; then
        if [[ -n "${stage_time_file}" ]]; then
            { TIMEFORMAT=$'real_s=%3R\nuser_s=%3U\nsys_s=%3S'; time "${execution_cmd[@]}" 2>&3; } 3>&2 2> "${stage_time_file}"
        else
            time "${execution_cmd[@]}"
        fi
        stage_returncode=$?
    else
        "${execution_cmd[@]}"
        stage_returncode=$?
    fi
    set -e
    compile_end_ms="$(now_ms)"
    if [[ -n "${process_guard_stdout}" && -s "${process_guard_stdout}" ]]; then
        sed -n '1,$p' "${process_guard_stdout}"
    fi
    if [[ -n "${process_guard_stderr}" && -s "${process_guard_stderr}" ]]; then
        sed -n '1,$p' "${process_guard_stderr}" >&2
    fi
    compile_elapsed_ms=$((compile_end_ms - compile_start_ms))
    barrier_start_ms="${compile_end_ms}"
    if [[ ${stage_returncode} -eq 0 ]]; then
        if [[ ! -s "${out_exe}" || ! -x "${out_exe}" ]]; then
            echo "FAIL — stage ${stage} did not produce executable ${out_exe}; refusing stale stage artifact." >&2
            stage_returncode=127
        fi
    fi
    if [[ ${stage_returncode} -eq 0 ]]; then
        set +e
        stage_exec_barrier "${out_exe}"
        barrier_returncode=$?
        set -e
        if [[ ${barrier_returncode} -ne 0 ]]; then
            stage_returncode="${barrier_returncode}"
        fi
    fi
    barrier_end_ms="$(now_ms)"
    barrier_elapsed_ms=$((barrier_end_ms - barrier_start_ms))
    stage_end_ms="$(now_ms)"
    stage_elapsed_ms=$((stage_end_ms - stage_start_ms))
    write_stage_result_json \
        "${stage}" "${out_exe}" "${compile_elapsed_ms}" \
        "${barrier_elapsed_ms}" "${stage_elapsed_ms}" "${stage_returncode}" \
        "${stage_time_file}" "${barrier_returncode}"
    # A failed stage must not emit a success-shaped result line.  This printed
    # `PCC_BOOTSTRAP_STAGE_RESULT stage=2 elapsed_ms=334749 output=.../pcc2`
    # for a stage that crashed and produced no pcc2 at all; anyone grepping for
    # that line -- which is exactly how these runs get measured -- reads a
    # timing and an output path for work that never happened.
    if [[ ${stage_returncode} -ne 0 ]]; then
        # Keep the process-tree sample on failure: result.json/samples.tsv are
        # the only record of what the killed stage was doing.
        echo "PCC_BOOTSTRAP_STAGE_FAILED stage=${stage} elapsed_ms=${stage_elapsed_ms} rc=${stage_returncode} output=<none>"
        exit "${stage_returncode}"
    fi
    # Successful stage: drop the sampler scratch directory, the way
    # stage_exec_barrier already drops its smoke directory.  Without this every
    # stage run left a `stage<N>.process.XXXXXX` behind for good -- 84 of them
    # had accumulated in one out-dir.  Set
    # PCC_BOOTSTRAP_KEEP_PROCESS_SAMPLES=1 to retain them for profiling.
    if [[ -n "${process_guard_dir}" && -z "${PCC_BOOTSTRAP_KEEP_PROCESS_SAMPLES:-}" ]]; then
        rm -rf "${process_guard_dir}"
    fi
    if [[ ! -s "${out_exe}" ]]; then
        echo "PCC_BOOTSTRAP_STAGE_FAILED stage=${stage} elapsed_ms=${stage_elapsed_ms} rc=0 output=<missing:${out_exe}>"
        exit 1
    fi
    echo "PCC_BOOTSTRAP_STAGE_RESULT stage=${stage} elapsed_ms=${stage_elapsed_ms} output=${out_exe} rc=0"
}

# stage 1: CPython-hosted pcc produces pcc1.
if [[ ${START_STAGE} -le 1 && ${STAGE_LIMIT} -ge 1 ]]; then
    if [[ "${REUSE_STAGE1}" -eq 1 && -s "${OUT_DIR}/pcc1" && -x "${OUT_DIR}/pcc1" ]]; then
        banner "stage 1: reuse existing ${OUT_DIR}/pcc1 (--reuse-stage1)"
        echo "PCC_BOOTSTRAP_STAGE_RESULT stage=1 elapsed_ms=0 output=${OUT_DIR}/pcc1 reused=1"
    elif command -v uv >/dev/null 2>&1; then
        if [[ -n "${LC_ALL:-}" ]]; then
            run_stage 1 "${OUT_DIR}/pcc1" env -u LC_ALL uv run python -m pcc
        else
            run_stage 1 "${OUT_DIR}/pcc1" uv run python -m pcc
        fi
    else
        PYTHON="${PYTHON:-python3}"
        run_stage 1 "${OUT_DIR}/pcc1" "${PYTHON}" -m pcc
    fi
fi

# stage 2: pcc1 compiles pcc.py.
if [[ ${START_STAGE} -le 2 && ${STAGE_LIMIT} -ge 2 ]]; then
    run_stage 2 "${OUT_DIR}/pcc2" "${OUT_DIR}/pcc1"
fi

# stage 3: pcc2 compiles pcc.py.
if [[ ${START_STAGE} -le 3 && ${STAGE_LIMIT} -ge 3 ]]; then
    run_stage 3 "${OUT_DIR}/pcc3" "${OUT_DIR}/pcc2"

    banner "verify: cmp pcc2 pcc3"
    if cmp -s "${OUT_DIR}/pcc2" "${OUT_DIR}/pcc3"; then
        echo "OK — pcc2 and pcc3 are byte-identical. Self-host gate passed."
        exit 0
    fi
    if [[ "$(uname)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
        tmp2="$(mktemp "${OUT_DIR}/pcc2.compare.XXXXXX")"
        tmp3="$(mktemp "${OUT_DIR}/pcc3.compare.XXXXXX")"
        cp "${OUT_DIR}/pcc2" "${tmp2}"
        cp "${OUT_DIR}/pcc3" "${tmp3}"
        codesign --remove-signature "${tmp2}" >/dev/null 2>&1 || true
        codesign --remove-signature "${tmp3}" >/dev/null 2>&1 || true
        PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
            python3 -m pcc.macho_normalize "${tmp2}" "${tmp3}" >/dev/null 2>&1 || true
        if cmp -s "${tmp2}" "${tmp3}"; then
            rm -f "${tmp2}" "${tmp3}"
            echo "OK — pcc2 and pcc3 differ only by Mach-O code-signature / LC_UUID metadata."
            echo "     Metadata-normalized copies are byte-identical."
            exit 0
        fi
        rm -f "${tmp2}" "${tmp3}"
    fi
    # Fall back to size + md5 structural comparison so nondeterministic
    # build metadata (e.g. embedded build-ids) doesn't break the
    # structural equivalence claim outright.
    s2=$(stat -f%z "${OUT_DIR}/pcc2")
    s3=$(stat -f%z "${OUT_DIR}/pcc3")
    echo "pcc2 size: ${s2}"
    echo "pcc3 size: ${s3}"
    if [[ "${s2}" != "${s3}" ]]; then
        echo "FAIL — pcc2 / pcc3 differ in size, not just metadata." >&2
        exit 1
    fi
    echo "WARN — bytes differ but sizes match; metadata noise suspected."
    echo "       Once the build is deterministic (no timestamps / paths"
    echo "       / uuids embedded), cmp should succeed."
    exit 2
fi
