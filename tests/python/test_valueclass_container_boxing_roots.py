"""Native payload boxing must unwind the surrounding literal's live roots."""
from __future__ import annotations

import re

import pytest

from pcc.backend.self_backend_aarch64_darwin_abi import (
    aggregate_returned_indirect as darwin_aggregate_returned_indirect,
)
from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_x86_64_linux import (
    _aggregate_returned_indirect as linux_aggregate_returned_indirect,
)
from tests.python.test_shared_call_binding import _emit


_PREFIX = '''import pcc
@pcc.valueclass
class Leaf:
    items: list
@pcc.valueclass
class Packet:
    leaf: Leaf
    flag: bool
'''


def _verify_roots(text, target):
    aggregate = (darwin_aggregate_returned_indirect if target == "aarch64-darwin"
                 else linux_aggregate_returned_indirect)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=aggregate)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target=target)
    assert len(plans) == len(prepared.functions)


@pytest.mark.parametrize("target", ("x86_64-linux", "aarch64-darwin"))
@pytest.mark.parametrize("threads", (False, True), ids=("lexical", "threaded"))
@pytest.mark.parametrize("literal,conversion_cleanup", (
    ("[Packet(Leaf([7]), True)]", True),
    ("(Packet(Leaf([7]), True),)", True),
    ("{'value': Packet(Leaf([7]), True)}", False),
), ids=("list", "tuple", "dict-value"))
def test_payload_boxing_retires_literal_roots_on_all_error_edges(
    literal, conversion_cleanup, threads, target, monkeypatch,
):
    monkeypatch.setenv("PCC_WITH_THREADS", "1" if threads else "0")
    text = _emit(_PREFIX + "def probe():\n    result = " + literal + "\n    return result\n")
    assert "@py_valuebox_new(" in text
    # Native dicts already scope operand evaluation around their container;
    # they are the unchanged control for the pre-evaluated list/tuple path.
    has_conversion_cleanup = "value.box.unwind.cleanup" in text
    assert has_conversion_cleanup is conversion_cleanup
    # Run the real emitter verifier and precise root analysis. Before the fix,
    # an internal boxing failure left list/tuple/dict.tmp.root active at the
    # statement handler while the allocation-failure edge had no such frame.
    _verify_roots(text, target)


@pytest.mark.parametrize("target", ("x86_64-linux", "aarch64-darwin"))
@pytest.mark.parametrize("threads", (False, True), ids=("lexical", "threaded"))
def test_boxing_unwind_root_balances_early_return_loop_and_handler(
    threads, target, monkeypatch,
):
    monkeypatch.setenv("PCC_WITH_THREADS", "1" if threads else "0")
    text = _emit(_PREFIX + '''
def probe(stop):
    if stop:
        return None
    for index in range(2):
        try:
            result = [Packet(Leaf([index]), True)]
            if stop:
                break
            continue
        except ValueError:
            return None
    return result
''')
    _verify_roots(text, target)
    bodies = re.findall(r"^define[^\n]*\{\n(.*?)^}", text, re.M | re.S)
    body = next(body for body in bodies if "value.box.unwind.exception" in body)
    blocks = re.split(r"^([\w.]+):\n", body, flags=re.M)
    cleanups = [block for label, block in zip(blocks[1::2], blocks[2::2])
                if label.startswith("value.box.unwind.cleanup")]
    assert cleanups
    for block in cleanups:
        swaps = [match.start() for match in re.finditer(r"@py_tls_exc_swap_slot\(", block)]
        assert len(swaps) == 2
        clear = block.index("@py_clear_exception(")
        unpin = block.index("@pcc_gc_unpin(")
        release = block.index("@pcc_gc_release(")
        assert swaps[0] < unpin < release < clear < swaps[1]
        if threads:
            # The persistent frame leaves at function exit; this edge only
            # clears the container owner and pin before terminal decref.
            assert "@pcc_gc_frame_leave_lifo(" not in block
            assert swaps[0] < block.index("@pcc_gc_store_root(") < unpin
        else:
            assert swaps[0] < block.index("@pcc_gc_frame_leave_lifo(") < unpin


@pytest.mark.parametrize("target", ("x86_64-linux", "aarch64-darwin"))
def test_module_payload_boxing_keeps_its_error_owner_registered(target, monkeypatch):
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    text = _emit(_PREFIX + "result = [Packet(Leaf([7]), True)]\n")
    assert "value.box.unwind.cleanup" in text
    _verify_roots(text, target)
