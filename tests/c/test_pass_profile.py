
from __future__ import annotations

from pcc.diagnostics.pass_profile import PassEvent, PassProfile


def test_pass_profile_explains_slow_and_skipped_passes():
    prof = PassProfile()
    prof.add(PassEvent("mem2reg", 1.0, True))
    prof.add(PassEvent("adce", 0.0, False, skipped=True, skip_reason="budget"))
    assert prof.total_ms() == 1.0
    assert prof.slowest()[0].name == "mem2reg"
    assert prof.explain() == [
        "mem2reg: 1.000ms changed",
        "adce: skipped (budget)",
    ]
    assert prof.to_json()["passes"][1]["skip_reason"] == "budget"
