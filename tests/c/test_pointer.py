import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest

   # int *c=&a ;  It is still too complex to fix
   # *c = 5;
   # return *c - a ;
##
# need one line by one line test

# define double @"main"()
# {
# main_entry:
#   %"a" = alloca double
#   store double 0x4008000000000000, double* %"a"
#   %"a.1" = load double, double* %"a"
#   ret double %"a.1"
# }
#
# The answer is 3

# define double @"main"()
# {
# main_entry:
#   %"c" = alloca double*
#   %"a" = alloca double
#   %"a.1" = load double, double* %"a"
#   ret double %"a.1"
#   store double 0x4008000000000000, double* %"a"
# }
# first fix this little simple


class TestSimpleFunc(unittest.TestCase):

    def test_simple(self):
        pcc = CEvaluator()

        ret = pcc.evaluate('''
                int main(){
                    int a = 33;
                    int *c;
                    c = &a;
                    *c = 34;
                    return a;
                }
            ''', llvmdump=True)

        assert ret == 34;
