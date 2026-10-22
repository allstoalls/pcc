"""A refused compact stack-map budget check reports the sizes behind it.

The Windows and macOS Stage1 links stopped with only "compact stack-map byte
budget exceeded: <label>" (runs 38557349522 and 38567599226), so each CI round
moved one check further without showing how far an estimate was over its
limit. The exception text stays as callers and tests match it; the sizes are
kept on the budget and added to the link error.
"""

from __future__ import annotations

import pytest

from pcc.backend.precise_stackmap import CompactStackMapBudget, PreciseStackMapError

PROFILE = dict(set_header_bytes=10, set_entry_bytes=2, integer_bytes=3,
               metadata_entry_bytes=7, allocation_quantum=1)


def test_refused_check_records_label_estimate_and_limit():
    budget = CompactStackMapBudget(100, 10, **PROFILE)
    budget.check_peak(10, entries=1, label="fits")
    assert budget.last_rejection == ""
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: snapshot$"):
        budget.check_peak(90, entries=2, label="snapshot")
    assert budget.last_rejection == (
        "label=snapshot estimate=104 raw=90 records=0 entries=2 retained=0 limit=100"
    )
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: raw$"):
        budget.check_peak(101, label="raw")
    assert budget.last_rejection.startswith("label=raw estimate=-1 raw=101 ")


def test_macho_budget_error_carries_the_refused_sizes():
    from pcc.backend.macho_exec import _check_compact_copy_peak
    from pcc.backend.macho_link import LinkError

    budget = CompactStackMapBudget(100, 10, **PROFILE)
    with pytest.raises(LinkError) as caught:
        _check_compact_copy_peak(budget, 200, "Mach-O final copy")
    assert str(caught.value) == (
        "Mach-O compact stack-map budget rejected: compact stack-map byte budget"
        " exceeded: Mach-O final copy; label=Mach-O final copy estimate=-1"
        " raw=200 records=0 entries=0 retained=0 limit=100"
    )


def test_pe_link_error_carries_the_refused_sizes():
    from pcc.backend import pe_host_budget as host
    from pcc.backend.coff_x86_64 import CoffError, CoffObject, CoffSection, CoffSymbol
    from pcc.backend.pe_x86_64 import link_executable

    text = CoffSection(".text", b"\xc3" * 16, 0x60000020, 16, ())
    obj = CoffObject((text,), (CoffSymbol("pcc_windows_start", 1, 0, True, True),))
    with pytest.raises(CoffError) as caught:
        link_executable([obj], compact_budget=host.PEHostBudget(32, 7, **PROFILE))
    message = str(caught.value)
    assert message.startswith("PE compact stack-map budget rejected: compact stack-map ")
    assert "; label=" in message and message.endswith(" limit=32")
