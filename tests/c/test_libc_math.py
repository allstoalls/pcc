"""Tests for math.h libc functions."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestMathBasic(unittest.TestCase):
    def test_sqrt(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return (int)sqrt(144.0); }
        ''')
        assert ret == 12

    def test_pow(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return (int)pow(2.0, 10.0); }
        ''')
        assert ret == 1024

    def test_fabs(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return (int)fabs(-42.5); }
        ''')
        assert ret == 42

    def test_ceil_floor(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int c = (int)ceil(3.2);
                int f = (int)floor(3.8);
                return c * 10 + f;
            }
        ''')
        assert ret == 43  # ceil(3.2)=4, floor(3.8)=3

    def test_round(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                return (int)round(3.5) + (int)round(2.4);
            }
        ''')
        assert ret == 6  # round(3.5)=4, round(2.4)=2

    def test_log_exp(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                double x = exp(0.0);
                double y = log(1.0);
                return (int)(x * 10 + y);
            }
        ''')
        assert ret == 10  # exp(0)=1.0, log(1)=0.0

    def test_sin_cos(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                double s = sin(0.0);
                double c = cos(0.0);
                return (int)(s * 100 + c * 100);
            }
        ''')
        assert ret == 100  # sin(0)=0, cos(0)=1

    def test_fmod(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return (int)(fmod(10.5, 3.0) * 10); }
        ''')
        assert ret == 15  # fmod(10.5, 3.0) = 1.5

    def test_hypot(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return (int)hypot(3.0, 4.0); }
        ''')
        assert ret == 5


class TestMathComplex(unittest.TestCase):
    def test_distance(self):
        """Euclidean distance between two points."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                double x1 = 1.0; double y1 = 2.0;
                double x2 = 4.0; double y2 = 6.0;
                double dx = x2 - x1;
                double dy = y2 - y1;
                return (int)(sqrt(dx*dx + dy*dy) * 100);
            }
        ''')
        assert ret == 500  # sqrt(9+16)=5.0

    def test_quadratic(self):
        """Solve x^2 - 5x + 6 = 0 -> x=2, x=3."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                double a = 1.0; double b = -5.0; double c = 6.0;
                double disc = b*b - 4.0*a*c;
                double x1 = (-b + sqrt(disc)) / (2.0 * a);
                double x2 = (-b - sqrt(disc)) / (2.0 * a);
                return (int)(x1 * 10 + x2 * 10);
            }
        ''')
        assert ret == 50  # x1=3, x2=2 -> 30+20=50


if __name__ == '__main__':
    unittest.main()
