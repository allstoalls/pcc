import os
import sys
import unittest

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


class TestBitfields(unittest.TestCase):
    def test_sqlite_style_bitfield_layout_matches_native_bytes(self):
        ret = CEvaluator().evaluate(
            r'''
            struct Inner {
                unsigned char jointype;
                unsigned a:1;
                unsigned b:1;
                unsigned c:1;
                unsigned d:1;
                unsigned e:1;
                unsigned f:1;
                unsigned g:1;
                unsigned h:1;
                unsigned i:1;
                unsigned j:1;
                unsigned k:1;
                unsigned l:1;
                unsigned m:1;
                unsigned n:1;
                unsigned o:1;
                unsigned p:1;
                unsigned q:1;
                unsigned r:1;
            };

            int main(void) {
                struct Inner x;
                unsigned char *p = (unsigned char *)&x;
                int i;
                for (i = 0; i < sizeof(x); i++) p[i] = 0;
                x.jointype = 0x5a;
                x.a = 1;
                x.b = 1;
                x.c = 1;
                x.p = 1;
                x.q = 1;
                x.r = 1;
                return (sizeof(x) == 4
                    && p[0] == 0x5a
                    && p[1] == 0x07
                    && p[2] == 0x80
                    && p[3] == 0x03) ? 0 : 1;
            }
            '''
        )
        assert ret == 0

    def test_sqlite_style_nested_bitfields_preserve_trailing_int(self):
        ret = CEvaluator().evaluate(
            r'''
            struct Flags {
                unsigned char jointype;
                unsigned notIndexed:1;
                unsigned isIndexedBy:1;
                unsigned isSubquery:1;
                unsigned isTabFunc:1;
                unsigned isCorrelated:1;
                unsigned isMaterialized:1;
                unsigned viaCoroutine:1;
                unsigned isRecursive:1;
                unsigned fromDDL:1;
                unsigned isCte:1;
                unsigned notCte:1;
                unsigned isUsing:1;
                unsigned isOn:1;
                unsigned isSynthUsing:1;
                unsigned isNestedFrom:1;
                unsigned rowidUsed:1;
                unsigned fixedSchema:1;
                unsigned hadSchema:1;
            };

            struct Outer {
                char *zName;
                char *zAlias;
                void *pSTab;
                struct Flags fg;
                int iCursor;
                long colUsed;
            };

            int get_cursor(struct Outer *p) {
                return p->iCursor;
            }

            int main(void) {
                struct Outer o;
                o.iCursor = 12345;
                o.fg.jointype = 0x5a;
                o.fg.isCorrelated = 1;
                o.fg.isMaterialized = 1;
                o.fg.fixedSchema = 1;
                o.fg.hadSchema = 0;
                return (sizeof(struct Flags) == 4
                    && sizeof(struct Outer) == 40
                    && get_cursor(&o) == 12345
                    && o.fg.jointype == 0x5a
                    && o.fg.isCorrelated == 1
                    && o.fg.isMaterialized == 1
                    && o.fg.fixedSchema == 1
                    && o.fg.hadSchema == 0) ? 0 : 1;
            }
            '''
        )
        assert ret == 0

    def test_bitfield_increment_updates_only_target_bits(self):
        ret = CEvaluator().evaluate(
            r'''
            struct Small {
                unsigned a:3;
                unsigned b:3;
            };

            int main(void) {
                struct Small s;
                unsigned char *p = (unsigned char *)&s;
                int i;
                for (i = 0; i < sizeof(s); i++) p[i] = 0;
                s.a = 1;
                ++s.a;
                s.a++;
                return (s.a == 3 && s.b == 0) ? 0 : 1;
            }
            '''
        )
        assert ret == 0

    def test_forward_declared_named_bitfield_struct_resolves_and_accesses(self):
        ret = CEvaluator().evaluate(
            r'''
            struct A;
            struct B;

            struct A {
                struct B *b;
                int mask;
            };

            struct B {
                struct A *a;
                int x;
                unsigned state:1;
            };

            static int nested(struct B *b) {
                return b->a->mask == 0xff;
            }

            static int same_a(struct B *b, struct A *a) {
                return b->a == a;
            }

            int main(void) {
                struct A a;
                struct B b;
                a.b = &b;
                a.mask = 0xff;
                b.a = &a;
                b.x = 0;
                b.state = 0;
                return (same_a(&b, &a) && nested(&b)) ? 0 : 1;
            }
            '''
        )
        assert ret == 0

    def test_positive_enum_bitfield_uses_zero_extension(self):
        ret = CEvaluator().evaluate(
            r'''
            enum tree_code {
                SOME_CODE = 148,
                LAST_AND_UNUSED_TREE_CODE
            };

            union tree_node;
            typedef union tree_node *tree;

            struct tree_common {
                union tree_node *chain;
                union tree_node *type;
                enum tree_code code : 8;
                unsigned side_effects_flag : 1;
            };

            union tree_node {
                struct tree_common common;
            };

            enum c_tree_code {
                C_DUMMY_TREE_CODE = LAST_AND_UNUSED_TREE_CODE,
                STMT_EXPR,
                LAST_C_TREE_CODE
            };

            enum cplus_tree_code {
                CP_DUMMY_TREE_CODE = LAST_C_TREE_CODE,
                AMBIG_CONV,
                LAST_CPLUS_TREE_CODE
            };

            int main(void) {
                union tree_node convs;
                convs.common.code = AMBIG_CONV;
                return ((enum tree_code) convs.common.code) == AMBIG_CONV ? 0 : 1;
            }
            '''
        )
        assert ret == 0

    def test_anonymous_bitfield_struct_field_assignment_reuses_layout_type(self):
        ret = CEvaluator().evaluate(
            r'''
            struct Item {
                struct {
                    unsigned a:1;
                    unsigned b:1;
                } fg;
                int x;
            };

            int main(void) {
                struct Item dst;
                struct Item src;
                src.fg.a = 1;
                src.fg.b = 0;
                dst.fg = src.fg;
                dst.fg.b = 1;
                return (dst.fg.a == 1 && dst.fg.b == 1) ? 0 : 1;
            }
            ''',
            optimize=False,
            use_system_cpp=False,
        )
        assert ret == 0


if __name__ == "__main__":
    unittest.main()
