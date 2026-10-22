"""Auto-discover and run all .c files and project directories in tests/c_cases/.

Single .c files: first-line comment // EXPECT: <return_value>
Project directories: must contain a main.c with // EXPECT: <return_value>
"""
import os
import re
import pytest

this_dir = os.path.dirname(os.path.abspath(__file__))
c_tests_dir = os.path.join(os.path.dirname(this_dir), "c_cases")


def _read_expect(fpath):
    """Read // EXPECT: N from first line of a file."""
    with open(fpath) as f:
        first_line = f.readline().strip()
    m = re.match(r'//\s*EXPECT:\s*(-?\d+)', first_line)
    return int(m.group(1)) if m else None


def collect_c_tests():
    """Collect single .c files and project directories."""
    tests = []
    if not os.path.isdir(c_tests_dir):
        return tests
    for entry in sorted(os.listdir(c_tests_dir)):
        full = os.path.join(c_tests_dir, entry)
        if entry.endswith('.c') and os.path.isfile(full):
            expected = _read_expect(full)
            if expected is not None:
                tests.append((entry, full, expected, False))
        elif os.path.isdir(full):
            main_c = os.path.join(full, 'main.c')
            if os.path.isfile(main_c):
                expected = _read_expect(main_c)
                if expected is not None:
                    tests.append((entry + "/", full, expected, True))
    return tests


C_TEST_CASES = collect_c_tests()


@pytest.mark.parametrize(
    "name,path,expected,is_project",
    C_TEST_CASES,
    ids=[t[0] for t in C_TEST_CASES],
)
def test_c_file(name, path, expected, is_project):
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    from pcc.driver.project import collect_project
    source, base_dir = collect_project(path)
    pcc = CEvaluator()
    ret = pcc.evaluate(source, optimize=False, base_dir=base_dir)
    assert ret == expected, f"{name}: expected {expected}, got {ret}"
