"""Dynamic dict construction accepts ordinary mappings and one-shot pairs."""

import os
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python


PROTOCOL = '''import gc
events = []
class Mapping:
    @property
    def keys(self):
        events.append("keys")
        return self.list_keys
    def list_keys(self):
        events.append("call")
        return iter(["answer"])
    def __getitem__(self, key):
        gc.collect()
        events.append(key)
        return [42]
    def __iter__(self):
        raise AssertionError("mapping priority lost")
class Broken:
    def keys(self):
        raise ValueError("keys failed")
def pairs():
    gc.collect()
    yield ("first", [1])
    gc.collect()
    yield ("second", [2])
def broken_pairs():
    yield ("first", 1)
    raise ValueError("iteration failed")
def construct(source):
    return dict(source)
def mark():
    events.append("keyword")
    return [7]
def with_keyword(source):
    return dict(source, extra=mark())
def main():
    result = with_keyword(Mapping())
    assert result == {"answer": [42], "extra": [7]}
    assert events == ["keyword", "keys", "keys", "call", "answer"], events
    assert construct(pairs()) == {"first": [1], "second": [2]}
    assert construct([("list", 1)]) == {"list": 1}
    assert construct((("tuple", 2),)) == {"tuple": 2}
    assert construct({"dict": 3}) == {"dict": 3}
    for source, message in [(Broken(), "keys failed"), (broken_pairs(), "iteration failed")]:
        try:
            construct(source)
        except ValueError as error:
            assert str(error) == message
        else:
            raise AssertionError("protocol exception swallowed")
    try:
        construct([("bad", 1, 2)])
    except ValueError:
        pass
    else:
        raise AssertionError("invalid pair accepted")
    try:
        construct(42)
    except TypeError:
        pass
    else:
        raise AssertionError("non-iterable accepted")
    print("DICT_PROTOCOL_OK")
main()
'''


ENVIRON = '''import os
def main():
    source = os.environ
    assert source["PCC_DICT_MAPPING_TEST"] == "original"
    assert "PCC_DICT_MAPPING_TEST" in source.keys()
    copied = dict(source)
    assert copied["PCC_DICT_MAPPING_TEST"] == "original"
    copied["PCC_DICT_MAPPING_TEST"] = "copy"
    assert os.environ["PCC_DICT_MAPPING_TEST"] == "original"
    replacement = {"PCC_DICT_MAPPING_TEST": "replacement"}
    os.environ = replacement
    assert dict(os.environ) == replacement
    print("DICT_ENVIRON_OK")
main()
'''


@pytest.mark.parametrize("name,program", (("protocol", PROTOCOL), ("environ", ENVIRON)))
def test_dynamic_dict_protocol_executes_natively(tmp_path, pcc_runtime_archive, name, program):
    source = tmp_path / (name + ".py")
    source.write_text(program, encoding="utf-8")
    environment = dict(os.environ, PCC_DICT_MAPPING_TEST="original")
    reference = subprocess.run([sys.executable, str(source)], env=environment,
                               capture_output=True, text=True, timeout=20)
    assert reference.returncode == 0, reference.stderr
    output = tmp_path / name
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    for collector in range(5):
        environment["PCC_GC_BACKEND"] = str(collector)
        environment["PCC_GC_REFCOUNT_PROVENANCE_PROBE"] = "2"
        ran = subprocess.run([str(output)], env=environment, capture_output=True,
                             text=True, timeout=40)
        assert ran.returncode == 0, f"GC{collector}: {ran.stderr}"
        assert ran.stdout == reference.stdout
        assert "nonmanaged" not in ran.stderr and "ownership" not in ran.stderr
