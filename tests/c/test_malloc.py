import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


def test_malloc():
    pcc = CEvaluator()

    out = pcc.evaluate('''
        int main(){
            int *a;
            a = malloc(8);
            *a = 4;
            return *a;
        }
        ''', llvmdump=True)
    # it seem can't work to catch llvm output
    assert out == 4;
