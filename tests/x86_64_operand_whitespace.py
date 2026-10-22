"""Frozen operand-parser oracle and source-derived whitespace controls.

The preimage is the complete df386 parser (encoder SHA256
844268459ee6b78d0fedfa4c2ae6cb9a18afec552ed08334b2cdbdc2478c924e).
This module imports no PCC implementation. The native control executes only
the actual changed target-name predicate, not the complete operand parser.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path


ENCODER = Path(__file__).resolve().parents[1] / "pcc/backend/x86_64_encode.py"
PREIMAGE = '''def _parse_operand(text: str):
    reg = _parse_reg(text)
    if reg is not None:
        return reg
    mem = _parse_mem(text)
    if mem is not None:
        return mem
    try:
        return _Imm(int(text.strip(), 0))
    except ValueError:
        name = text.strip()
        if not name or any(char.isspace() for char in name):
            raise X86EncodeError(f"bad operand {text!r}")
        return _Target(name)
'''

# Include CPython's Unicode whitespace and adjacent non-whitespace characters.
# Native expectations are derived from its own original predicate, since the
# currently owned strip/split/isspace implementation recognizes ASCII six.
WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
NON_WHITESPACE = ("\x00", "\x08", "\x1b", "\x7f", "\u180e", "\u200b", "\u2060", "\ufeff", "\U0001f600")


def target_cases():
    cases = ["", "target", ".Ltarget", "target_" + "x" * 4096]
    for char in WHITESPACE + "".join(NON_WHITESPACE):
        cases.extend((char, char + "target", "target" + char,
                      "left" + char + "right", char + "left" + char + char + "right" + char))
    return cases


def _function(source):
    return next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "_parse_operand")


def _target_check(function):
    statement = function.body[-1]
    assert isinstance(statement, ast.Try) and len(statement.handlers) == 1
    handler = statement.handlers[0]
    assert isinstance(handler.type, ast.Name) and handler.type.id == "ValueError"
    assert len(handler.body) == 3 and isinstance(handler.body[1], ast.If)
    return handler.body[1]


def assert_source_contract():
    """The complete parser differs only in this exact bounded predicate."""
    source = ENCODER.read_text(encoding="utf-8")
    actual = _function(source)
    old = _function(PREIMAGE)
    expected = ast.parse(
        "not name or (len(name.split(None, 1)) > 1 if type(text) is str "
        "else any(char.isspace() for char in name))", mode="eval",
    ).body
    assert ast.dump(_target_check(actual).test) == ast.dump(expected)
    restored = copy.deepcopy(actual)
    _target_check(restored).test = copy.deepcopy(_target_check(old).test)
    assert ast.dump(restored) == ast.dump(old)
    return source, actual, old


def old_operand_parser(namespace):
    """Real frozen parser using the unchanged production records and leaves."""
    scope = dict(namespace)
    exec(compile(PREIMAGE, "<frozen-operand-whitespace-preimage>", "exec"), scope)
    return scope["_parse_operand"]


def _predicate_sources():
    source, actual, old = assert_source_contract()
    after_test = ast.get_source_segment(source, _target_check(actual).test)
    before_test = ast.get_source_segment(PREIMAGE, _target_check(old).test)
    assert after_test is not None and before_test is not None
    result = {}
    for name, predicate in (("after", after_test), ("before", before_test)):
        result[name] = (
            "\ndef _operand_name_bad_" + name + "(text: str):\n"
            "    name = text.strip()\n"
            "    return (" + predicate + ")\n"
        )
    return result


def native_whitespace_source():
    """Append actual predicate execution to the existing tiny native program.

    The split result has at most two elements, not a fixed byte bound. The
    owned runtime copies its pieces; no result replaces the original name.
    """
    cases = ["", "target", ".Ltarget", "target_" + "x" * 512]
    for char in " \t\n\r\v\f\x1c\x85\xa0\u2003\u2028\u3000\u200b\ufeff":
        cases.extend((char, char + "target" + char, "left" + char + "right"))
    sources = _predicate_sources()
    functions = sources["after"] + sources["before"]
    return functions + (
        "\ndef check_operand_whitespace():\n"
        "    # Compare within this runtime; Unicode behavior is not redefined.\n"
        "    cases = " + repr(tuple(cases)) + "\n"
        "    for text in cases:\n"
        "        assert type(text) is str\n"
        "        assert _operand_name_bad_after(text) == _operand_name_bad_before(text)\n"
        "    assert _operand_name_bad_after('left right')\n"
        "    assert not _operand_name_bad_after('target')\n"
        "    assert _operand_name_bad_after('')\n"
    )


def record_whitespace_operation_ab(tmp_path):
    """Fixed optional microcheck inside the existing normal-CI host node.

    The normal gate has already recorded complete source identity. Other test
    invocations do not start this timing. No new process, compilation, profile
    hook or performance threshold is introduced. These synthetic labels do
    not measure the actual cli_bootstrap module or Stage1 improvement.
    """
    import hashlib
    import json
    import sys
    import time

    receipts = [parent / "preflight.json" for parent in tuple(tmp_path.parents)[:5]
                if (parent / "preflight.json").is_file()]
    if not receipts:
        (tmp_path / "operand-whitespace-ab.json").write_text(json.dumps({
            "schema": "pcc.operand-whitespace-operation-ab.v1",
            "status": "NOT_COLLECTED",
            "reason": "matching normal-preflight source receipt unavailable",
            "scope": "behavior checks only; no operation measurement",
        }) + "\n", encoding="utf-8")
        return
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["phase"] == "preflight"
    source = receipt["source"]
    root = ENCODER.parents[2]
    owners = (
        "pcc/backend/x86_64_encode.py", "tests/x86_64_operand_whitespace.py",
        "tests/python/test_x86_64_x87_table_encoding.py",
    )
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
              for name in owners}
    for name, digest in hashes.items():
        assert source["test_inputs"][name] == digest
    assert source["python"]["executable"] == sys.executable
    assert source["python"]["version"] == sys.version
    with Path(sys.executable).open("rb") as executable:
        assert source["python"]["sha256"] == hashlib.file_digest(executable, "sha256").hexdigest()
    sources = _predicate_sources()
    scope = {}
    exec(compile(sources["before"] + sources["after"],
                 "<source-derived-operand-whitespace-predicates>", "exec"), scope)
    functions = {name: scope["_operand_name_bad_" + name] for name in sources}
    lengths = (16, 64, 128, 256)
    labels = tuple("target_" + "x" * (size - 7) for size in lengths)
    rounds = 8
    repetitions = 128
    rows = []
    for index in range(rounds):
        order = ("before", "after") if index % 2 == 0 else ("after", "before")
        for name in order:
            function = functions[name]
            started = time.perf_counter()
            for repeat in range(repetitions):
                for text in labels:
                    assert not function(text), "the fixed labels must remain valid"
            elapsed = time.perf_counter() - started
            rows.append({"round": index, "variant": name, "wall_s": elapsed})
    record = {
        "schema": "pcc.operand-whitespace-operation-ab.v1",
        "status": "MEASURED",
        "scope": "synthetic valid-label predicate microcheck; not cli_bootstrap or Stage1 performance",
        "interference": "existing normal six-worker CI; no isolated benchmark or performance threshold",
        "source": {key: source[key] for key in (
            "commit", "source_sha256", "source_file_count", "source_scope", "clean_commit",
        )},
        "complete_source_receipt_sha256": hashlib.sha256(json.dumps(
            source, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
        "owners": hashes,
        "frozen_preimage_parser_sha256": hashlib.sha256(PREIMAGE.encode()).hexdigest(),
        "predicate_source_sha256": {name: hashlib.sha256(value.encode()).hexdigest()
                                    for name, value in sources.items()},
        "python": source["python"],
        "executing_python_version": list(sys.version_info),
        "input_lengths": lengths,
        "input_sha256": hashlib.sha256(json.dumps(labels).encode()).hexdigest(),
        "rounds": rounds,
        "repetitions_per_batch": repetitions,
        "timed_calls_per_implementation": rounds * repetitions * len(labels),
        "characters_per_implementation": rounds * repetitions * sum(lengths),
        "rows": rows,
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    assert len(payload.encode("utf-8")) <= 4096
    # The existing artifact glob preserves **/*.json beneath this basetemp.
    (tmp_path / "operand-whitespace-ab.json").write_text(payload, encoding="utf-8")
