"""Python syntax edge case parity tests."""
from __future__ import annotations
import subprocess
import textwrap
import pytest

def _compile_and_run(tmp_path, monkeypatch, source: str) -> str:
    from pcc.frontends.python.pipeline import compile_python
    src = tmp_path / "case.py"
    exe = tmp_path / "case.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30.0)
    assert result.returncode == 0
    return result.stdout

def test_walrus_operator(tmp_path, monkeypatch):
    source = """
        def main():
            xs = [1, 2, 3]
            if (n := len(xs)) > 2: print(n)
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).strip() == "3"

def test_complex_comprehensions(tmp_path, monkeypatch):
    source = """
        def main():
            print([x for row in [[1,2],[3,4]] for x in row if x % 2 == 0])
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).strip() == "[2, 4]"

def test_pattern_matching_basic(tmp_path, monkeypatch):
    source = """
        def main():
            match [1, 2]:
                case [a, b]: print(a, b)
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).strip() == "1 2"


def test_pattern_matching_class_and_as_patterns(tmp_path, monkeypatch):
    source = """
        def classify(width):
            match width:
                case int(both):
                    return both + both
                case tuple((int(before), int(after))):
                    return before * 10 + after
                case _ as invalid:
                    return len(invalid)
        def main():
            print(classify(4))
            print(classify((2, 7)))
            print(classify("bad"))
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).splitlines() == [
        "8",
        "27",
        "3",
    ]


def test_fstring_expressions(tmp_path, monkeypatch):
    source = """
        def main():
            x = True
            print(f"{x}")
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).strip() == "True"


def test_fstring_debug_expression(tmp_path, monkeypatch):
    source = """
        def main():
            width = 3
            binwidth = 5
            print(f"Insufficient bit {width=} provided for {binwidth=}")
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).strip() == (
        "Insufficient bit width=3 provided for binwidth=5"
    )


def test_fstring_ascii_conversion(tmp_path, monkeypatch):
    source = """
        def main():
            value = "\\u00e9"
            face = chr(0x1f600)
            print(f"{value!a}")
            print(f"{face!a}")
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).splitlines() == [
        "'\\xe9'",
        "'\\U0001f600'",
    ]


def test_parenthesized_with_item_as(tmp_path, monkeypatch):
    source = """
        class Ctx:
            def __enter__(self):
                return 7
            def __exit__(self, exc_type, exc, tb):
                return False
        def main():
            with (Ctx()
                  as value):
                print(value)
            with (Ctx()) as value2:
                print(value2 + 1)
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).splitlines() == [
        "7",
        "8",
    ]


def test_decorator_comment_before_class(tmp_path, monkeypatch):
    source = """
        def runtime_checkable(cls):
            return cls
        @runtime_checkable
        # comment between decorator and class is accepted by CPython
        class Box:
            pass
        def main():
            print(1)
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).strip() == "1"


def test_starred_rhs_tuple_display_assignment(tmp_path, monkeypatch):
    source = """
        def make_pair():
            return (10, 20)

        def main():
            first, second, third = [], *make_pair()
            print(first)
            print(second)
            print(third)
            print((*[1, 2], 3))
            print([0, *[1, 2], 3])
        if __name__ == "__main__": main()
    """
    assert _compile_and_run(tmp_path, monkeypatch, source).splitlines() == [
        "[]",
        "10",
        "20",
        "(1, 2, 3)",
        "[0, 1, 2, 3]",
    ]
