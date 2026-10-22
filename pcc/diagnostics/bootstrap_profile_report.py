from __future__ import annotations

from pathlib import Path
import json
import re
from typing import Any


_STAGE_PROFILE_RE = re.compile(r"^stage([0-9]+)\.json$")
_STAGE_RESULT_JSON_RE = re.compile(r"^stage([0-9]+)\.result\.json$")
_STAGE_RESULT_RE = re.compile(
    r"^PCC_BOOTSTRAP_STAGE_RESULT\s+stage=([0-9]+)\s+"
    r"elapsed_ms=([0-9]+)\s+output=(.*)$"
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _as_int_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _profile_total_ms(profile: dict[str, Any]) -> int:
    total = _as_int_ms(profile.get("total_ms"))
    if total > 0:
        return total
    phases = profile.get("phase_totals_ms")
    if not isinstance(phases, dict):
        return 0
    for key in ("compile_python_multi_total", "compile_python_total"):
        total = _as_int_ms(phases.get(key))
        if total > 0:
            return total
    return sum(_as_int_ms(v) for v in phases.values())


def _phase_totals(profile: dict[str, Any]) -> dict[str, int]:
    phases = profile.get("phase_totals_ms")
    if not isinstance(phases, dict):
        return {}
    return {str(k): _as_int_ms(v) for k, v in phases.items()}


def _counters(profile: dict[str, Any]) -> dict[str, int]:
    counters = profile.get("counters")
    if not isinstance(counters, dict):
        return {}
    return {str(k): _as_int_ms(v) for k, v in counters.items()}


def _top_phases(phases: dict[str, int], limit: int) -> list[dict[str, Any]]:
    ordered = sorted(phases.items(), key=lambda item: item[1], reverse=True)
    return [
        {"name": name, "ms": ms}
        for name, ms in ordered[: max(0, limit)]
    ]


def _parse_stage_results(log_path: Path | None) -> dict[int, dict[str, Any]]:
    if log_path is None:
        return {}
    results: dict[int, dict[str, Any]] = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            match = _STAGE_RESULT_RE.match(line)
            if match is None:
                continue
            stage = int(match.group(1))
            results[stage] = {
                "wall_ms": int(match.group(2)),
                "output": match.group(3),
            }
    return results


def _parse_stage_result_files(root: Path) -> dict[int, dict[str, Any]]:
    results: dict[int, dict[str, Any]] = {}
    for path in root.iterdir():
        match = _STAGE_RESULT_JSON_RE.match(path.name)
        if match is None:
            continue
        data = _load_json(path)
        stage = int(match.group(1))
        payload: dict[str, Any] = {"result": str(path)}
        for key in (
            "backend",
            "output",
            "returncode",
            "publish_barrier_returncode",
            "wall_ms",
            "compile_wall_ms",
            "compile_user_ms",
            "compile_sys_ms",
            "compile_time_real_ms",
            "publish_barrier_ms",
        ):
            if key in data:
                payload[key] = data[key]
        results[stage] = payload
    return results


def build_bootstrap_profile_report(
    profile_dir: str | Path,
    *,
    log_path: str | Path | None = None,
    top: int = 6,
) -> dict[str, Any]:
    root = Path(profile_dir)
    if not root.exists():
        raise FileNotFoundError(str(root))
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    stage_results = _parse_stage_result_files(root)
    log_results = _parse_stage_results(Path(log_path) if log_path else None)
    for stage, result in log_results.items():
        stage_results.setdefault(stage, {}).update(result)
    stages: list[dict[str, Any]] = []
    totals_by_phase: dict[str, int] = {}

    profile_files: dict[int, Path] = {}
    for path in root.iterdir():
        match = _STAGE_PROFILE_RE.match(path.name)
        if match is not None:
            profile_files[int(match.group(1))] = path

    stage_numbers = sorted(set(profile_files) | set(stage_results))
    for stage in stage_numbers:
        path = profile_files.get(stage)
        profile = _load_json(path) if path is not None else {}
        phases = _phase_totals(profile)
        for name, ms in phases.items():
            totals_by_phase[name] = totals_by_phase.get(name, 0) + ms
        result = stage_results.get(stage, {})
        top_phases = _top_phases(phases, top)
        stage_payload: dict[str, Any] = {
            "stage": stage,
            "compiler_profile_ms": _profile_total_ms(profile),
            "phase_totals_ms": phases,
            "counters": _counters(profile),
            "top_phases": top_phases,
            "dominant_phase": top_phases[0] if top_phases else None,
        }
        if path is not None:
            stage_payload["profile"] = str(path)
        if "wall_ms" in result:
            stage_payload["wall_ms"] = result["wall_ms"]
        if "compile_wall_ms" in result:
            stage_payload["compile_wall_ms"] = result["compile_wall_ms"]
        if "compile_user_ms" in result:
            stage_payload["compile_user_ms"] = result["compile_user_ms"]
        if "compile_sys_ms" in result:
            stage_payload["compile_sys_ms"] = result["compile_sys_ms"]
        if "compile_time_real_ms" in result:
            stage_payload["compile_time_real_ms"] = result["compile_time_real_ms"]
        if "publish_barrier_ms" in result:
            stage_payload["publish_barrier_ms"] = result["publish_barrier_ms"]
        if "returncode" in result:
            stage_payload["returncode"] = result["returncode"]
        if "publish_barrier_returncode" in result:
            stage_payload["publish_barrier_returncode"] = result[
                "publish_barrier_returncode"
            ]
        if "backend" in result:
            stage_payload["backend"] = result["backend"]
        if "result" in result:
            stage_payload["result"] = result["result"]
        if "output" in result:
            stage_payload["output"] = result["output"]
        if "wall_ms" in stage_payload:
            stage_payload["unprofiled_wall_ms"] = max(
                0,
                _as_int_ms(stage_payload.get("wall_ms"))
                - _as_int_ms(stage_payload.get("compiler_profile_ms")),
            )
        stages.append(stage_payload)

    total_wall_ms = sum(_as_int_ms(s.get("wall_ms")) for s in stages)
    if not all("wall_ms" in s for s in stages):
        total_wall_ms = 0

    return {
        "schema": "pcc.diagnostics.bootstrap_profile_report.v1",
        "profile_dir": str(root),
        "stage_count": len(stages),
        "total_wall_ms": total_wall_ms,
        "total_compile_wall_ms": sum(
            _as_int_ms(s.get("compile_wall_ms")) for s in stages
        ),
        "total_compile_user_ms": sum(
            _as_int_ms(s.get("compile_user_ms")) for s in stages
        ),
        "total_compile_sys_ms": sum(
            _as_int_ms(s.get("compile_sys_ms")) for s in stages
        ),
        "total_publish_barrier_ms": sum(
            _as_int_ms(s.get("publish_barrier_ms")) for s in stages
        ),
        "total_unprofiled_wall_ms": sum(
            _as_int_ms(s.get("unprofiled_wall_ms")) for s in stages
        ),
        "total_compiler_profile_ms": sum(
            _as_int_ms(s.get("compiler_profile_ms")) for s in stages
        ),
        "totals_by_phase_ms": dict(sorted(totals_by_phase.items())),
        "top_phases": _top_phases(totals_by_phase, top),
        "stages": stages,
    }


def _fmt_ms(value: Any) -> str:
    ms = _as_int_ms(value)
    return "-" if ms <= 0 else str(ms)


def format_bootstrap_profile_report(report: dict[str, Any]) -> str:
    lines = [
        "pcc bootstrap profile report",
        f"profile_dir: {report.get('profile_dir', '')}",
        f"stages: {report.get('stage_count', 0)}",
        "total_wall_ms: " + _fmt_ms(report.get("total_wall_ms")),
        "total_compile_wall_ms: " + _fmt_ms(report.get("total_compile_wall_ms")),
        "total_compile_user_ms: " + _fmt_ms(report.get("total_compile_user_ms")),
        "total_compile_sys_ms: " + _fmt_ms(report.get("total_compile_sys_ms")),
        "total_publish_barrier_ms: "
        + _fmt_ms(report.get("total_publish_barrier_ms")),
        "total_unprofiled_wall_ms: "
        + _fmt_ms(report.get("total_unprofiled_wall_ms")),
        "total_compiler_profile_ms: "
        + _fmt_ms(report.get("total_compiler_profile_ms")),
        "",
        (
            "stage  wall_ms  compile_ms  compiler_ms  "
            "user_ms  sys_ms  barrier_ms  gap_ms  dominant_phase"
        ),
    ]
    for stage in report.get("stages", []):
        dominant = stage.get("dominant_phase") or {}
        dominant_text = "-"
        if dominant:
            dominant_text = (
                str(dominant.get("name", ""))
                + "="
                + _fmt_ms(dominant.get("ms"))
            )
        lines.append(
            f"{stage.get('stage', ''):>5}  "
            f"{_fmt_ms(stage.get('wall_ms')):>7}  "
            f"{_fmt_ms(stage.get('compile_wall_ms')):>10}  "
            f"{_fmt_ms(stage.get('compiler_profile_ms')):>11}  "
            f"{_fmt_ms(stage.get('compile_user_ms')):>7}  "
            f"{_fmt_ms(stage.get('compile_sys_ms')):>6}  "
            f"{_fmt_ms(stage.get('publish_barrier_ms')):>10}  "
            f"{_fmt_ms(stage.get('unprofiled_wall_ms')):>6}  "
            f"{dominant_text}"
        )
    lines.append("")
    lines.append("top phases:")
    for phase in report.get("top_phases", []):
        lines.append(f"  {phase.get('name', '')}: {_fmt_ms(phase.get('ms'))} ms")
    lines.append("")
    return "\n".join(lines)
