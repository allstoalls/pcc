from pcc.diagnostics.self_backend_profile import SelfBackendPhase, summarize_self_backend


def test_self_backend_profile_totals_spills():
    assert summarize_self_backend([SelfBackendPhase("ra", spills=3)])["total_spills"] == 3
