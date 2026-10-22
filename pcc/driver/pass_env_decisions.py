from __future__ import annotations

from dataclasses import dataclass

from pcc.diagnostics.pass_explain import PassDecision, format_pass_explain


@dataclass(frozen=True)
class PassEnvExplain:
    requested: tuple[str, ...]
    disabled: tuple[str, ...]
    opt_level: int
    decisions: tuple[PassDecision, ...]

    def format(self, fmt: str = "text") -> str:
        return format_pass_explain(list(self.decisions), fmt=fmt)


def explain_pass_selection(default_passes: list[str], enabled: list[str], disabled: list[str], opt_level: int) -> PassEnvExplain:
    enabled_set = set(enabled)
    disabled_set = set(disabled)
    decisions = []
    for name in default_passes:
        if name in disabled_set:
            decisions.append(PassDecision(name, False, "disabled by --disable-pass"))
        elif enabled_set and name not in enabled_set:
            decisions.append(PassDecision(name, False, "not named by --pass allowlist"))
        else:
            decisions.append(PassDecision(name, True, "selected by optimization preset"))
    for name in enabled:
        if name not in default_passes:
            decisions.append(PassDecision(name, True, "explicit extra pass"))
    return PassEnvExplain(tuple(enabled), tuple(disabled), opt_level, tuple(decisions))
