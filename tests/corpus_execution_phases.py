"""Distinguish corpus builds, worker stops and completed program executions."""
from __future__ import annotations

from dataclasses import dataclass
import subprocess

from tests.clang_c_cases import CStageResult


@dataclass(frozen=True)
class PccCompileResult:
    returncode: int
    stdout: str
    stderr: str
    stages: tuple[CStageResult, ...] = ()

    @classmethod
    def from_stages(cls, stages):
        stages = tuple(stages)
        last = stages[-1]
        return cls(last.returncode, last.stdout, last.stderr, stages)

    @property
    def executed(self):
        return bool(self.stages and self.stages[-1].stage == "run"
                    and self.stages[-1].completed)

    def require_execution(self, label):
        last = self.stages[-1] if self.stages else CStageResult(
            "unverified", self.returncode, self.stdout, self.stderr, False
        )
        assert self.executed, (
            f"{label} did not execute: {last.stage} "
            f"returncode={last.returncode}, completed={last.completed}:\n{last.stderr}"
        )


class ExecutionPhases:
    """Record exact owned-corpus stage callbacks and their terminal outcome."""

    def __init__(self):
        self.stages = []
        self.current = "prepare"

    def begin(self, stage):
        if self.current in {"compile", "link"}:
            self.stages.append(CStageResult(self.current, 0))
        self.current = stage

    def complete(self, result=None):
        self.stages.append(CStageResult(
            self.current,
            0 if result is None else result.returncode,
            "" if result is None else result.stdout,
            "" if result is None else result.stderr,
        ))
        return PccCompileResult.from_stages(self.stages)

    def fail(self, exc):
        code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 1
        self.stages.append(CStageResult(self.current, code, stderr=str(exc), completed=False))
        return PccCompileResult.from_stages(self.stages)


def run_reference_stages(commands, *, cwd, env, timeout):
    stages = []
    for stage, command in commands:
        try:
            result = subprocess.run(command, cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 1
            stages.append(CStageResult(stage, code, stderr=str(exc), completed=False))
            break
        stages.append(CStageResult(stage, result.returncode, result.stdout, result.stderr))
        if result.returncode != 0:
            break
    return PccCompileResult.from_stages(stages)
