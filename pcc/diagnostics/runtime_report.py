"""Runtime capability reporting for pcc.

This is the first concrete slice of ``pcc --runtime-report`` from the
roadmap.  It is deliberately separated from the CLI so tests and bootstrap
helpers can consume it directly.  The report is truth-preserving: it
    distinguishes production, production-gated, partial, experimental, and
    planned surfaces instead of claiming that selectable backends are complete
    collectors without matching gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any


@dataclass(frozen=True)
class Capability:
    name: str
    status: str
    summary: str
    evidence: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class RuntimeReport:
    schema: str = "pcc.diagnostics.runtime_report.v1"
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)
    environment: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "environment": dict(sorted(self.environment.items())),
            "capabilities": [cap.to_json() for cap in self.capabilities],
        }

    def format_json(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True)

    def format_text(self) -> str:
        lines = ["pcc runtime report", "==================", ""]
        for cap in self.capabilities:
            lines.append(f"[{cap.status}] {cap.name}: {cap.summary}")
            for item in cap.evidence:
                lines.append(f"  evidence: {item}")
            for item in cap.missing:
                lines.append(f"  missing: {item}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def build_runtime_report() -> RuntimeReport:
    env = {
        "PCC_WITH_THREADS": os.environ.get("PCC_WITH_THREADS", "1"),
        "PCC_REFCOUNT_KIND": os.environ.get("PCC_REFCOUNT_KIND", "default"),
        "PCC_PYTHON_LIBPYTHON": os.environ.get("PCC_PYTHON_LIBPYTHON", "off"),
    }
    capabilities = (
        Capability(
            name="gc.backend.0.refcount-cycle",
            status="production",
            summary="default CPython-style refcount plus cycle collector path",
            evidence=(
                "pcc_gc_* ABI",
                "py_gc_collect STW wrapper",
                "tests/test_gc_effectiveness.py",
            ),
        ),
        Capability(
            name="gc.backend.1.incremental-tricolor",
            status="production-gated",
            summary="Lua-style incremental tracing path with bounded debt, root tracing, and explicit sweep gates",
            evidence=(
                "tests/python/test_gc_backend_incremental.py",
                "tests/python/test_gc_g1_cycle_collector.py",
                "tests/python/test_gc_g2_finalizers.py",
                "pcc1 threaded explicit gc.collect matrix",
            ),
        ),
        Capability(
            name="gc.backend.2.concurrent-mark-sweep",
            status="production-gated",
            summary="CMS worker path with assist, buffered barrier work, mark termination, lifecycle, and TSan gates",
            evidence=(
                "tests/python/test_gc_backend_concurrent.py",
                "tests/python/test_gc_concurrent_collection.py CMS TSan probes",
                "tests/python/test_gc_backend23_production.py",
                "pcc1 threaded explicit gc.collect matrix",
            ),
        ),
        Capability(
            name="gc.backend.3.generational-minor-major",
            status="production-gated",
            summary="minor arena, remembered-set promotion, eager reference update, and cross-domain slot rewrite gates",
            evidence=(
                "tests/python/test_gc_backend_generational.py",
                "tests/python/test_gc_backend23_production.py",
                "cross-domain C and pcc-Python remembered-slot gates",
                "pcc1 threaded explicit gc.collect matrix",
            ),
        ),
        Capability(
            name="gc.backend.4.colored-relocating",
            status="production-gated",
            summary="colored relocating path with forwarding table, read-barrier repair, stable IDs, and evacuation-debt telemetry",
            evidence=(
                "tests/python/test_gc_backend_relocating.py",
                "tests/python/test_gc_backend4_production.py",
                "fragmentation score tracks live evacuation debt",
                "pcc1 threaded explicit gc.collect matrix",
            ),
        ),
        Capability(
            name="refcount.nonatomic.atomic",
            status="production",
            summary="NONATOMIC default and ATOMIC build strategy are available",
            evidence=("pcc_refcount_incref/decref", "tests/test_gc_refcount_strategies.py"),
        ),
        Capability(
            name="refcount.biased.deferred",
            status="bridge",
            summary="side-table bridge builds; PEP 703 header migration remains separate",
            evidence=("PCC_REFCOUNT_KIND=2/3 side-table metadata"),
            missing=("ob_tid", "ob_ref_local", "ob_ref_shared", "deferred queue flush policy"),
        ),
        Capability(
            name="threading.native",
            status="partial",
            summary="Thread/Lock/RLock/Event/Condition/Semaphore route through native ABI",
            evidence=("pcc_threads.c", "py_threading.c", "tests/test_threading_module_native.py"),
            missing=("threading.local TLS", "timeouts", "container synchronization"),
        ),
        Capability(
            name="no-libpython",
            status="production-gated",
            summary="bootstrap direction is no-libpython with explicit linkage gates",
            evidence=("tests/test_self_host_oracle_diff.py", "scripts/verify_nolibpython.py"),
        ),
    )
    return RuntimeReport(capabilities=capabilities, environment=env)


def format_runtime_report(fmt: str = "text") -> str:
    report = build_runtime_report()
    normalized = fmt.strip().lower()
    if normalized == "text":
        return report.format_text()
    if normalized == "json":
        return report.format_json()
    raise ValueError(f"unknown runtime report format {fmt!r}")
