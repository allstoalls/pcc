"""Environment policy and diagnostics for Python IR pass scheduling."""

from __future__ import annotations

import os
from typing import Optional

from pcc.driver.paths import join_strings

PYTHON_IR_PASSES_ENV = "PCC_PYTHON_IR_PASSES"
PYTHON_IR_PASS_TRANSPORT_ENV = "PCC_PYTHON_IR_PASS_TRANSPORT"
PYTHON_IR_PASS_JOBS_ENV = "PCC_PYTHON_IR_PASS_JOBS"
PYTHON_IR_PASS_TIMEOUT_ENV = "PCC_PYTHON_IR_PASS_TIMEOUT"
PYTHON_IR_PASS_SPLIT_LARGE_MODULES_ENV = "PCC_PYTHON_IR_PASS_SPLIT_LARGE_MODULES"
PYTHON_IR_PASS_SPLIT_THRESHOLD_BYTES_ENV = (
    "PCC_PYTHON_IR_PASS_SPLIT_THRESHOLD_BYTES"
)
PYTHON_IR_PASS_SPLIT_SHARD_BYTES_ENV = "PCC_PYTHON_IR_PASS_SPLIT_SHARD_BYTES"
PYTHON_IR_PASS_SKIP_MODULE_PREFIXES_ENV = "PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES"
OUTER_PARALLELISM_ENV = "PCC_OUTER_PARALLELISM"

# This manifest is an artifact contract, not an invitation to grow the default
# optimizer tier.  PERF-P1-TIERING owns changes to the version or membership;
# PERF-P2-PASS-WIRE may only select this exact bounded tuple for self emission.
PYTHON_IR_PASS_DEFAULT_TIER_SCHEMA = "pcc.python-ir-default-tier.v1"
PYTHON_IR_PASS_DEFAULT_TIER = ("mem2reg", "sroa")
PYTHON_IR_PASS_DEFAULT_TIER_MAX_PASSES = 6
PYTHON_IR_PASS_FAST_PRESET = PYTHON_IR_PASS_DEFAULT_TIER
PYTHON_IR_PASS_UNSAFE_MODULES = frozenset(
    {
        "pcc.frontends.python.codegen.string_globals_lowering",
        "pcc.ir.ir",
    }
)
PYTHON_IR_PASS_UNSAFE_MODULE_PREFIXES = ("pcc.frontends.python.codegen",)
PYTHON_IR_PASS_PRESETS = {
    "quick": ("mem2reg", "sroa", "sccp", "dce"),
    "fast": PYTHON_IR_PASS_FAST_PRESET,
    "default": PYTHON_IR_PASS_FAST_PRESET,
    "all": ("all",),
    "full": ("all",),
}


def resolve_python_ir_pass_names(
    raw: Optional[str] = None,
    *,
    default_raw: Optional[str] = None,
) -> list[str]:
    if raw is None:
        raw = os.environ.get(PYTHON_IR_PASSES_ENV)
        if raw is None or not str(raw).strip():
            if default_raw is not None:
                raw = default_raw
            else:
                return list(PYTHON_IR_PASS_PRESETS["default"])
    elif not str(raw).strip() and default_raw is not None:
        raw = default_raw
    if raw is None:
        return list(PYTHON_IR_PASS_PRESETS["default"])
    normalized = str(raw or "").strip().lower()
    if normalized in ("off", "false", "no", "0"):
        return []
    if not normalized or normalized in ("on", "true", "yes", "1"):
        return list(PYTHON_IR_PASS_PRESETS["default"])

    pass_names: list[str] = []
    for token in normalized.split(","):
        name = token.strip()
        if not name:
            continue
        preset = PYTHON_IR_PASS_PRESETS.get(name)
        if preset is not None:
            for preset_name in preset:
                if preset_name not in pass_names:
                    pass_names.append(preset_name)
            continue
        if name not in pass_names:
            pass_names.append(name)
    return pass_names


def parallel_cpu_budget() -> int:
    cpu_count = max(1, os.cpu_count() or 1)
    raw = str(os.environ.get(OUTER_PARALLELISM_ENV, "") or "").strip()
    try:
        outer_parallelism = int(raw) if raw else 1
    except ValueError:
        outer_parallelism = 1
    return max(1, cpu_count // max(1, outer_parallelism))


def python_ir_pass_jobs(item_count: int) -> int:
    raw = str(os.environ.get(PYTHON_IR_PASS_JOBS_ENV, "") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 1
    else:
        value = min(12, parallel_cpu_budget())
    return max(1, min(max(1, item_count), value))


def parse_seconds_text(raw: str, default: float) -> float:
    text = str(raw or "").strip()
    if not text:
        return default
    sign = 1.0
    if text.startswith("-"):
        sign = -1.0
        text = text[1:]
    whole = 0.0
    fraction = 0.0
    scale = 1.0
    seen_digit = False
    seen_dot = False
    for char in text:
        if char == "." and not seen_dot:
            seen_dot = True
            continue
        if char < "0" or char > "9":
            return default
        digit = ord(char) - ord("0")
        seen_digit = True
        if seen_dot:
            scale *= 10.0
            fraction += digit / scale
        else:
            whole = whole * 10.0 + digit
    if not seen_digit:
        return default
    return sign * (whole + fraction)


def small_int_decimal(value: int) -> str:
    return str(value)


def seconds_debug_text(value) -> str:
    if value is None:
        return "disabled"
    scaled = int(value * 1000.0)
    if scaled < 0:
        return "-" + seconds_debug_text((-scaled) / 1000.0)
    whole = scaled // 1000
    fraction = scaled % 1000
    fraction_text = small_int_decimal(fraction)
    if fraction < 10:
        fraction_text = "00" + fraction_text
    elif fraction < 100:
        fraction_text = "0" + fraction_text
    return small_int_decimal(whole) + "." + fraction_text + "s"


def python_ir_pass_timeout_seconds() -> Optional[float]:
    raw = str(os.environ.get(PYTHON_IR_PASS_TIMEOUT_ENV, "") or "").strip()
    value = parse_seconds_text(raw, 120.0) if raw else 120.0
    return None if value <= 0 else value


def python_ir_pass_strict_arg(*, strict_no_libpython: bool) -> str:
    return "1" if strict_no_libpython else "0"


def python_ir_pass_batch_size_summary(
    module_ir_texts: list[tuple[str, str]],
    *,
    limit: int = 3,
) -> str:
    entries: list[tuple[int, str]] = []
    total_bytes = 0
    for module_name, ir_text in module_ir_texts:
        size = len(str(ir_text))
        total_bytes += size
        entries.append((size, str(module_name)))
    parts: list[str] = []
    index = 0
    while index < (limit if limit > 0 else 0):
        best_index = -1
        best_size = -1
        best_name = ""
        scan_index = 0
        while scan_index < len(entries):
            size, module_name = entries[scan_index]
            if size > best_size or (size == best_size and module_name > best_name):
                best_size = size
                best_name = module_name
                best_index = scan_index
            scan_index += 1
        if best_index < 0:
            break
        size, module_name = entries[best_index]
        parts.append(module_name + ":" + small_int_decimal(size))
        entries[best_index] = (-1, "")
        index += 1
    return (
        "total_bytes="
        + small_int_decimal(total_bytes)
        + " largest="
        + join_strings(parts, ",")
    )


def python_ir_pass_transport_is_memory() -> bool:
    raw = str(
        os.environ.get(PYTHON_IR_PASS_TRANSPORT_ENV, "") or ""
    ).strip().lower()
    return raw == "memory"


def default_python_ir_pass_transport(
    pass_names: list[str],
    default_raw: Optional[str],
) -> Optional[str]:
    raw = str(
        os.environ.get(PYTHON_IR_PASS_TRANSPORT_ENV, "") or ""
    ).strip().lower()
    if raw:
        return None
    normalized_names: list[str] = []
    for pass_name in pass_names:
        normalized_names.append(str(pass_name).strip().lower())
    if tuple(normalized_names) == PYTHON_IR_PASS_FAST_PRESET:
        return "memory"
    return None


def effective_python_ir_pass_transport_is_memory(
    default_transport: Optional[str],
) -> bool:
    raw = str(
        os.environ.get(PYTHON_IR_PASS_TRANSPORT_ENV, "") or ""
    ).strip().lower()
    if raw:
        return raw == "memory"
    return str(default_transport or "").strip().lower() == "memory"


def python_ir_pass_split_large_modules_enabled() -> bool:
    raw = str(
        os.environ.get(PYTHON_IR_PASS_SPLIT_LARGE_MODULES_ENV, "") or ""
    )
    normalized = raw.strip().lower()
    if normalized in ("0", "false", "no", "off", "legacy"):
        return False
    return True


def positive_int_env(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def python_ir_pass_split_threshold_bytes() -> int:
    return positive_int_env(PYTHON_IR_PASS_SPLIT_THRESHOLD_BYTES_ENV, 4_000_000)


def python_ir_pass_split_shard_bytes() -> int:
    return positive_int_env(PYTHON_IR_PASS_SPLIT_SHARD_BYTES_ENV, 1_400_000)


def python_ir_pass_names_allow_module_sharding(pass_names: list[str]) -> bool:
    normalized_names: list[str] = []
    for pass_name in pass_names:
        lowered = str(pass_name).strip().lower()
        if lowered in ("all", "full"):
            return True
        if lowered.startswith("default<o") and lowered.endswith(">"):
            return True
        normalized_names.append(lowered)
    return tuple(normalized_names) == PYTHON_IR_PASS_FAST_PRESET


def python_ir_pass_skip_prefixes() -> tuple[str, ...]:
    raw = str(
        os.environ.get(PYTHON_IR_PASS_SKIP_MODULE_PREFIXES_ENV, "") or ""
    )
    out: list[str] = []
    for part in raw.split(","):
        prefix = part.strip()
        if prefix:
            out.append(prefix)
    return tuple(out)


def python_ir_pass_should_skip_module(module_name: str) -> bool:
    name = str(module_name)
    if name in PYTHON_IR_PASS_UNSAFE_MODULES:
        return True
    for prefix in PYTHON_IR_PASS_UNSAFE_MODULE_PREFIXES:
        if name == prefix or name.startswith(prefix):
            return True
    for prefix in python_ir_pass_skip_prefixes():
        if name == prefix or name.startswith(prefix):
            return True
    return False


def python_ir_pass_skip_modules_for_batch(
    module_ir_texts: list[tuple[str, str]],
) -> tuple[str, ...]:
    out: list[str] = []
    for module_name, _text in module_ir_texts:
        if python_ir_pass_should_skip_module(module_name):
            out.append(str(module_name))
    return tuple(out)
