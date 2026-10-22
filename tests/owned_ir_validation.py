"""Validate test IR through the same owned parser and verifier as emission."""

import re

from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module


def verify_ir_text(text, *, progress=None):
    """Keep the public parse/verify route; optionally report phase boundaries."""
    text = str(text)
    if not re.search(r'^\s*target\s+triple\s*=', text, re.MULTILINE):
        from pcc.frontends.python.pipeline_targets import host_target_triple

        text = 'target triple = "' + host_target_triple() + '"\n' + text
    if progress is not None:
        progress("parse", "started")
    module = parse_self_backend_module(text)
    if progress is not None:
        progress("parse", "complete")
        progress("verify", "started")
    verify_parsed_module(module)
    if progress is not None:
        progress("verify", "complete")
    return module
