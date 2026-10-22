"""PassContext — the shared state that flows through all tiers.

HighTier passes populate it, MidTier (codegen) reads it, LowTier amends IR.
"""

from __future__ import annotations

import logging
from pcc.frontends.c.c_abi_layout import c_target_triple
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pcc.frontends.c.ssa.adce import SSAADCEResult
    from pcc.frontends.c.ssa.gvn import SSAGVNResult
    from pcc.frontends.c.ssa.ir import SSAFunction
    from pcc.frontends.c.ssa.sccp import SSASCCPResult

_logger = logging.getLogger("pcc.frontends.c.passes")


class AllocStrategy(Enum):
    """How a local variable should be lowered to LLVM IR."""
    ALLOCA = auto()       # default: stack alloca + load/store
    SSA = auto()          # direct SSA value (no alloca needed)
    REGISTER_HINT = auto()  # alloca, but hint LLVM to promote


class OverflowFlag(Enum):
    NONE = auto()
    NSW = auto()    # no signed wrap
    NUW = auto()    # no unsigned wrap
    NSW_NUW = auto()  # both


@dataclass
class VarInfo:
    """Per-variable analysis results, keyed by (func_name, var_name)."""
    name: str
    func_name: str

    # Escape analysis
    address_taken: bool = False       # &x appears somewhere
    passed_to_call: bool = False      # passed by pointer to a function
    escapes: bool = False             # conservative: address_taken or passed_to_call

    # Lifetime analysis
    def_count: int = 0                # number of assignments
    use_count: int = 0                # number of reads
    single_def: bool = False          # exactly one assignment (including init)
    is_param: bool = False            # function parameter

    # Type range analysis
    type_name: str = ""               # C type name (e.g. "int", "unsigned int")
    is_unsigned: bool = False
    bit_width: int = 0
    range_min: Optional[int] = None   # proven lower bound
    range_max: Optional[int] = None   # proven upper bound

    # Allocation decision (set by HighTier, read by codegen)
    alloc_strategy: AllocStrategy = AllocStrategy.ALLOCA

    # Alignment (for noalias/align hints)
    known_align: int = 0              # 0 = unknown


@dataclass
class FuncInfo:
    """Per-function analysis results."""
    name: str
    var_infos: dict[str, VarInfo] = field(default_factory=dict)

    # Function-level properties
    is_leaf: bool = True              # no calls to other functions
    has_alloca_call: bool = False     # calls alloca() dynamically
    has_setjmp: bool = False          # uses setjmp/longjmp
    has_goto: bool = False            # uses goto
    has_var_length_array: bool = False  # VLA declarations
    max_loop_depth: int = 0

    # Restrict pointers (C99 restrict)
    restrict_params: set[str] = field(default_factory=set)


@dataclass
class TypeAliasClass:
    """TBAA (Type-Based Alias Analysis) class for IR metadata."""
    name: str
    parent: Optional[str] = None  # None = root
    is_const: bool = False


@dataclass
class PassMetric:
    tier: str = ""
    runs: int = 0
    skips: int = 0
    failures: int = 0
    total_time_ms: float = 0.0
    last_status: str = ""
    last_detail: str = ""


# Default C TBAA hierarchy (strict aliasing rule):
# root
#   ├── char (aliases everything)
#   ├── short
#   ├── int
#   ├── long
#   ├── long long
#   ├── float
#   ├── double
#   ├── pointer
#   └── struct.<name>
DEFAULT_TBAA_HIERARCHY = {
    "omnipotent char": TypeAliasClass("omnipotent char", "root"),
    "short": TypeAliasClass("short", "omnipotent char"),
    "int": TypeAliasClass("int", "omnipotent char"),
    "long": TypeAliasClass("long", "omnipotent char"),
    "long long": TypeAliasClass("long long", "omnipotent char"),
    "float": TypeAliasClass("float", "omnipotent char"),
    "double": TypeAliasClass("double", "omnipotent char"),
    "any pointer": TypeAliasClass("any pointer", "omnipotent char"),
}


class PassContext:
    """Shared mutable state flowing through the entire pass pipeline.

    HighTier passes write analysis results here.
    MidTier (codegen) reads them to generate better IR.
    LowTier passes read them to add IR metadata.
    """

    def __init__(self, opt_level: Optional[int] = None, target_triple=None):
        self.target_triple: str = c_target_triple(target_triple)
        # Explicit library/evaluation entries are observable roots even when
        # their source linkage is translation-unit private.
        self.entry_roots: set[str] = set()
        # Per-function analysis: func_name -> FuncInfo
        self.functions: dict[str, FuncInfo] = {}

        # Frontend/backend optimization level for the current compilation when
        # the caller knows it. High-tier passes can use this for
        # opt-level-specific heuristics.
        self.opt_level: Optional[int] = (
            None if opt_level is None else int(opt_level)
        )

        # Internal SSA bootstrap artifacts for eligible functions.
        self.ssa_functions: dict[str, SSAFunction] = {}
        self.ssa_adce_results: dict[str, SSAADCEResult] = {}
        self.ssa_gvn_results: dict[str, SSAGVNResult] = {}
        self.ssa_sccp_results: dict[str, SSASCCPResult] = {}

        # TBAA hierarchy (can be extended by passes)
        self.tbaa: dict[str, TypeAliasClass] = dict(DEFAULT_TBAA_HIERARCHY)

        # Pass execution log (Graal-inspired optimization log)
        self.log: list[PassLogEntry] = []

        # Global stats
        self.stats: dict[str, int] = {}

        # Per-pass execution metrics
        self.pass_metrics: dict[str, PassMetric] = {}

        # Pipeline configuration
        self.enabled: bool = True   # master switch
        self.disabled_passes: set[str] = set()
        self.fail_open: bool = True

    def get_func(self, func_name: str) -> FuncInfo:
        if func_name not in self.functions:
            self.functions[func_name] = FuncInfo(name=func_name)
        return self.functions[func_name]

    def get_var(self, func_name: str, var_name: str) -> VarInfo:
        func = self.get_func(func_name)
        if var_name not in func.var_infos:
            func.var_infos[var_name] = VarInfo(
                name=var_name, func_name=func_name,
            )
        return func.var_infos[var_name]

    def record(self, pass_name: str, action: str, target: str, detail: str = ""):
        """Log an optimization decision (Graal OptimizationLog style)."""
        entry = PassLogEntry(pass_name, action, target, detail)
        self.log.append(entry)
        _logger.debug("%s", entry)

    def bump(self, stat_name: str, count: int = 1):
        self.stats[stat_name] = self.stats.get(stat_name, 0) + count

    def disable_pass(self, pass_name: str):
        self.disabled_passes.add(pass_name)

    def is_pass_enabled(self, pass_name: str) -> bool:
        return pass_name not in self.disabled_passes

    def _metric_for(self, pass_name: str, tier: str = "") -> PassMetric:
        metric = self.pass_metrics.get(pass_name)
        if metric is None:
            metric = PassMetric(tier=tier)
            self.pass_metrics[pass_name] = metric
        elif tier and not metric.tier:
            metric.tier = tier
        return metric

    def note_pass_run(self, pass_name: str, tier: str, elapsed_ms: float):
        metric = self._metric_for(pass_name, tier)
        metric.runs += 1
        metric.total_time_ms += elapsed_ms
        metric.last_status = "ran"

    def note_pass_skip(self, pass_name: str, tier: str, reason: str):
        metric = self._metric_for(pass_name, tier)
        metric.skips += 1
        metric.last_status = "skipped"
        metric.last_detail = reason
        self.record(pass_name, "skipped", tier, reason)

    def note_pass_failure(self, pass_name: str, tier: str, exc: Exception):
        metric = self._metric_for(pass_name, tier)
        metric.failures += 1
        metric.last_status = "failed"
        metric.last_detail = f"{type(exc).__name__}: {exc}"
        self.record(pass_name, "failed", tier, metric.last_detail)

    def clear_ssa_artifacts(self, *, reason: str = "", record: bool = True):
        """Drop cached SSA artifacts after an AST rewrite.

        High-tier source rewrites happen after `ssa-bootstrap` in the default
        pipeline. If those rewrites mutate the AST, any cached SSA graph and
        derived SCCP/GVN/ADCE facts no longer describe the function body that
        codegen will lower. Clearing them forces downstream SSA consumers to
        rebuild from the rewritten AST and prevents stale SSA codegen.
        """
        had_artifacts = bool(
            self.ssa_functions
            or self.ssa_adce_results
            or self.ssa_gvn_results
            or self.ssa_sccp_results
        )
        self.ssa_functions.clear()
        self.ssa_adce_results.clear()
        self.ssa_gvn_results.clear()
        self.ssa_sccp_results.clear()
        if had_artifacts and record:
            detail = reason or "ast rewrite"
            self.record("ssa-bootstrap", "invalidate", "cache", detail)
            self.bump("ssa.bootstrap.invalidated")

    @classmethod
    def from_pass_report(cls, report: Optional[dict]):
        ctx = cls()
        if not isinstance(report, dict):
            return ctx

        raw_opt_level = report.get("opt_level")
        ctx.opt_level = None if raw_opt_level is None else int(raw_opt_level)
        ctx.enabled = bool(report.get("enabled", True))
        ctx.fail_open = bool(report.get("fail_open", True))
        ctx.disabled_passes = set(report.get("disabled_passes", ()))

        stats = report.get("stats", {})
        if isinstance(stats, dict):
            ctx.stats = dict(stats)

        passes = report.get("passes", {})
        if isinstance(passes, dict):
            for name, payload in passes.items():
                if not isinstance(payload, dict):
                    continue
                metric = ctx._metric_for(name, payload.get("tier", ""))
                metric.runs = int(payload.get("runs", 0) or 0)
                metric.skips = int(payload.get("skips", 0) or 0)
                metric.failures = int(payload.get("failures", 0) or 0)
                metric.total_time_ms = float(
                    payload.get("total_time_ms", 0.0) or 0.0
                )
                metric.last_status = str(payload.get("last_status", "") or "")
                metric.last_detail = str(payload.get("last_detail", "") or "")

        return ctx

    def pass_report(self) -> dict:
        return {
            "opt_level": self.opt_level,
            "enabled": self.enabled,
            "fail_open": self.fail_open,
            "disabled_passes": sorted(self.disabled_passes),
            "passes": {
                name: {
                    "tier": metric.tier,
                    "runs": metric.runs,
                    "skips": metric.skips,
                    "failures": metric.failures,
                    "total_time_ms": metric.total_time_ms,
                    "last_status": metric.last_status,
                    "last_detail": metric.last_detail,
                }
                for name, metric in sorted(self.pass_metrics.items())
            },
            "stats": dict(sorted(self.stats.items())),
        }

    def dump_stats(self) -> str:
        lines = ["=== Pass Statistics ==="]
        for k, v in sorted(self.stats.items()):
            lines.append(f"  {k}: {v}")
        if self.pass_metrics:
            lines.append("=== Pass Metrics ===")
            for name, metric in sorted(self.pass_metrics.items()):
                lines.append(
                    "  "
                    f"{name}: runs={metric.runs} skips={metric.skips} "
                    f"failures={metric.failures} time_ms={metric.total_time_ms}"
                )
        return "\n".join(lines)

    def dump_log(self) -> str:
        return "\n".join(str(e) for e in self.log)


@dataclass
class PassLogEntry:
    pass_name: str
    action: str
    target: str
    detail: str = ""

    def __str__(self):
        s = f"[{self.pass_name}] {self.action}: {self.target}"
        if self.detail:
            s += f" ({self.detail})"
        return s
