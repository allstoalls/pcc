import sys
import os
this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest

class TestStruct(unittest.TestCase):

    def test_struct(self):
        # Evaluate some code.
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct {
                int x;
                int y;
            } Point;

            int main() {
                struct Point p;
                p.x = 4;
                p.y = 4;
                int distance_squared = p.x * p.x + p.y * p.y;
                return distance_squared;
            }
            ''', llvmdump=True)

        assert (ret == 32)

    def test_incomplete_struct_member_access_errors(self):
        pcc = CEvaluator()
        with self.assertRaisesRegex(ValueError, "incomplete struct"):
            pcc.evaluate(
                '''
                struct Opaque;

                int read_x(struct Opaque *p) {
                    return p->x;
                }
                '''
            )

    def test_block_scope_tag_shadowing_does_not_mutate_outer_struct(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct T;

            struct T {
                int x;
            };

            int main() {
                struct T v;
                { struct T { int z; }; }
                v.x = 2;
                return v.x;
            }
            """
        )
        assert ret == 2

    def test_anonymous_struct_union_members_flatten_into_parent_scope(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef struct {
                int a;
                union {
                    int b1;
                    int b2;
                };
                struct { union { struct { int c; }; }; };
                struct {
                    int d;
                };
            } s;

            int main() {
                s v;
                v.a = 1;
                v.b1 = 2;
                v.c = 3;
                v.d = 4;
                return v.a + v.b2 + v.c + v.d;
            }
            """
        )
        assert ret == 10

    def test_anonymous_union_member_in_initializer_and_access(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct S1 {
                int a;
                int b;
            };

            struct S2 {
                int a;
                int b;
                union {
                    int c;
                    int d;
                };
                struct S1 s;
            };

            struct S2 v = {1, 2, 3, {4, 5}};

            int main() {
                return v.a + v.b + v.c + v.d + v.s.a + v.s.b;
            }
            """
        )
        assert ret == 18

    def test_flat_array_initializer_for_struct_elements_without_inner_braces(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef struct {
                long c[4];
                long b, e, k;
            } PT;

            PT cases[] = {
                1, 2, 3, 4, 5, 6, 7,
                8, 9, 10, 11, 12, 13, 14
            };

            int main() {
                return cases[0].c[0] == 1 &&
                       cases[0].c[3] == 4 &&
                       cases[0].b == 5 &&
                       cases[1].c[0] == 8 &&
                       cases[1].k == 14 ? 0 : 1;
            }
            """
        )
        assert ret == 0

    def test_file_scope_pointer_to_compound_literal_struct(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct S {
                int a;
                int b;
            };

            struct S *s = &(struct S){1, 2};

            int main() {
                return s->a == 1 && s->b == 2 ? 0 : 1;
            }
            """
        )
        assert ret == 0

    def test_file_scope_pointer_to_nested_compound_literal_struct(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct S1 {
                int a;
                int b;
            };

            struct S2 {
                struct S1 s1;
                struct S1 *ps1;
                int arr[2];
            };

            struct S1 gs1 = { .a = 1, 2 };
            struct S2 *s = &(struct S2){
                { .b = 2, .a = 1 },
                &gs1,
                { [0] = 1, 1 + 1 }
            };

            int main() {
                return s->s1.a == 1 &&
                       s->s1.b == 2 &&
                       s->ps1->a == 1 &&
                       s->ps1->b == 2 &&
                       s->arr[0] == 1 &&
                       s->arr[1] == 2 ? 0 : 1;
            }
            """
        )
        assert ret == 0

if __name__ == '__main__':
    unittest.main()
