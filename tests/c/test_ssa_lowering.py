"""Test Phase 2: SSA → LLVM IR lowering.

Verifies that eligible functions are lowered directly from internal SSA
to LLVM IR values and phi nodes, bypassing alloca/load/store for
promotable scalar locals.

LLVM reference boundary:
  PromoteMemoryToRegister.cpp achieves the same result bottom-up.
  We go top-down via the SSA builder's structured phi placement.
"""

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit


def _compile_and_run(source: str, prog_args=None):
    ev = CEvaluator()
    unit = TranslationUnit("test.c", "/dev/null", source)
    compiled = ev.compile_translation_units(
        [unit], base_dir="/tmp", use_compile_cache=False,
    )
    return ev.run_compiled_translation_units_with_system_cc(
        compiled, optimize=False, prog_args=prog_args or [],
    )


def _compile_and_eval(source: str, prog_args=None):
    ev = CEvaluator()
    unit = TranslationUnit("test.c", "/dev/null", source)
    compiled = ev.compile_translation_units(
        [unit], base_dir="/tmp", use_compile_cache=False,
    )
    return ev.evaluate_compiled_translation_units(
        compiled, optimize=False, prog_args=prog_args or [],
    )


def test_ssa_lowering_multi_def_scalar_if_else():
    """Multi-def local scalar through if/else produces correct result."""
    result = _compile_and_eval("""
    int f(int x) {
        int y = 0;
        if (x > 0) {
            y = x + 1;
        } else {
            y = x - 1;
        }
        return y;
    }
    int main(void) {
        return f(5);
    }
    """)
    assert result == 6


def test_ssa_lowering_multi_def_scalar_nested_if():
    """Multi-def local through nested branches."""
    result = _compile_and_eval("""
    int f(int x) {
        int r = 0;
        if (x > 10) {
            r = 1;
        } else {
            if (x > 0) {
                r = 2;
            } else {
                r = 3;
            }
        }
        return r;
    }
    int main(void) {
        return f(5);
    }
    """)
    assert result == 2


def test_ssa_lowering_constant_propagation_through_join():
    """Constant value through both branches should work."""
    result = _compile_and_eval("""
    int f(int c) {
        int x = 0;
        if (c) {
            x = 42;
        } else {
            x = 42;
        }
        return x;
    }
    int main(void) {
        return f(1);
    }
    """)
    assert result == 42


def test_ssa_lowering_while_loop_simple():
    """Simple while loop with multi-def scalar."""
    result = _compile_and_eval("""
    int f(int n) {
        int sum = 0;
        int i = 0;
        while (i < n) {
            sum = sum + i;
            i = i + 1;
        }
        return sum;
    }
    int main(void) {
        return f(5);
    }
    """)
    # 0+1+2+3+4 = 10
    assert result == 10


def test_ssa_lowering_break_in_while():
    """`break` inside a while loop carries merged env to exit block."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        int found = -1;
        while (i < n) {
            if (i == 3) {
                found = i * 10;
                break;
            }
            i = i + 1;
        }
        return found;
    }
    int main(void) {
        return f(10);
    }
    """)
    assert result == 30


def test_ssa_lowering_break_in_for_loop():
    """`break` inside a for loop with decl-list init."""
    result = _compile_and_eval("""
    int f(int n) {
        int sum = 0;
        for (int i = 0; i < n; i = i + 1) {
            if (i == 4) {
                break;
            }
            sum = sum + i;
        }
        return sum;
    }
    int main(void) {
        return f(100);
    }
    """)
    # 0+1+2+3 = 6
    assert result == 6


def test_ssa_lowering_break_in_infinite_for():
    """`break` is the only exit from for(;;) — env comes from break site only."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        for (;;) {
            if (i >= n) {
                break;
            }
            i = i + 2;
        }
        return i;
    }
    int main(void) {
        return f(7);
    }
    """)
    # i: 0 → 2 → 4 → 6 → 8 (>= 7, break)
    assert result == 8


def test_ssa_lowering_break_in_do_while():
    """`break` inside a do-while."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        do {
            if (i == n) {
                break;
            }
            i = i + 1;
        } while (i < 100);
        return i;
    }
    int main(void) {
        return f(5);
    }
    """)
    assert result == 5


def test_ssa_lowering_continue_in_while():
    """`continue` in a while loop skips rest of body, jumps to header."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        int sum = 0;
        while (i < n) {
            i = i + 1;
            if (i == 3) {
                continue;
            }
            sum = sum + i;
        }
        return sum;
    }
    int main(void) {
        return f(5);
    }
    """)
    # 1+2+4+5 = 12 (skip 3)
    assert result == 12


def test_ssa_lowering_continue_in_for_runs_next_expression():
    """`continue` in a for loop must still execute the `next` expression."""
    result = _compile_and_eval("""
    int f(int n) {
        int sum = 0;
        int i;
        for (i = 0; i < n; i = i + 1) {
            if (i == 2) {
                continue;
            }
            sum = sum + i;
        }
        return sum + i;
    }
    int main(void) {
        return f(5);
    }
    """)
    # 0+1+3+4=8, plus i=5 → 13
    assert result == 13


def test_ssa_lowering_continue_in_do_while():
    """`continue` in a do-while jumps to the latch (cond eval)."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        int sum = 0;
        do {
            i = i + 1;
            if (i == 3) {
                continue;
            }
            sum = sum + i;
        } while (i < n);
        return sum;
    }
    int main(void) {
        return f(5);
    }
    """)
    # 1+2+4+5=12
    assert result == 12


def test_ssa_lowering_continue_and_break_interleaved():
    """Mixed continue + break in one loop."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        int sum = 0;
        while (i < n) {
            i = i + 1;
            if (i == 3) {
                continue;
            }
            if (i == 7) {
                break;
            }
            sum = sum + i;
        }
        return sum;
    }
    int main(void) {
        return f(20);
    }
    """)
    # i: 1,2,[skip 3],4,5,6,[break at 7] → sum = 1+2+4+5+6 = 18
    assert result == 18


def test_ssa_lowering_comma_expr_in_for_next():
    """Comma expression in a for-loop next executes each in order."""
    result = _compile_and_eval("""
    int f(int n) {
        int a = 0;
        int b = 0;
        int i;
        for (i = 0; i < n; a = a + 1, b = b + 2) {
            if (i + 1 >= n) {
                break;
            }
            i = i + 1;
        }
        return a * 100 + b;
    }
    int main(void) {
        return f(4);
    }
    """)
    # iterations complete i=0,1,2 (i+1 < 4 first three iterations, break on i=3's check)
    # Wait: initial i=0, body runs, i becomes 1, a=1,b=2. Next iter i=1<4: i becomes 2, a=2,b=4. i=2<4: i becomes 3, a=3,b=6. i=3<4: i+1>=4 → break.
    # So a=3, b=6 → 306
    assert result == 306


def test_ssa_lowering_comma_expr_in_value_position():
    """Comma expression in initializer evaluates each, returns last."""
    result = _compile_and_eval("""
    int f(int n) {
        int y;
        int x = (y = n + 1, y * 2);
        return x + y;
    }
    int main(void) {
        return f(5);
    }
    """)
    # y = 6, x = 12, return 18
    assert result == 18


def test_ssa_lowering_side_effecting_short_circuit_condition():
    """Short-circuit conditions with side-effecting subexprs (`++i`) must
    carry each cond-chain block's own env snapshot through the exit phi.
    Reproduces the zlib deflate.c `longest_match` do-while regression
    where `*++scan == *++match && ...` used the same env for every
    predecessor of the exit block, corrupting scan's value after the loop.
    """
    result = _compile_and_eval("""
    int f(void) {
        int i = 0;
        int j = 0;
        do {
            /* nothing */
        } while (++i < 3 && ++j < 3 && i + j < 100);
        return i * 10 + j;
    }
    int main(void) {
        return f();
    }
    """)
    # First iteration: i=1, j=1, 1+1<100 → true, continue.
    # Second iteration: i=2, j=2, 2+2<100 → true, continue.
    # Third iteration: i=3, NOT (< 3) → short-circuit exits. j stays at 2.
    # Expected: 3*10 + 2 = 32
    assert result == 32


def test_ssa_lowering_break_with_short_circuit_condition():
    """Break inside a loop whose condition uses short-circuit `&&`.

    The `&&` decomposition creates an intermediate `if.cond.rhs` block
    that also branches to the loop exit. The phi merge at exit must
    include both the latch block and that rhs block, or LLVM's phi
    verifier asserts "PHINode should have one entry for each predecessor".
    Reproduces the zlib deflate.c fill_window regression.
    """
    result = _compile_and_eval("""
    int f(int n, int limit) {
        int i = 0;
        int hits = 0;
        do {
            if (i == 7) {
                hits = i * 100;
                break;
            }
            i = i + 1;
        } while (i < n && i < limit);
        return hits + i;
    }
    int main(void) {
        return f(20, 20);
    }
    """)
    # i reaches 7, hits=700, break → 707
    assert result == 707


def test_ssa_lowering_local_struct_value_scalar_fields():
    """Phase 4 MVP: local struct value becomes stack alloca so `s.field` works
    on a struct that has no initializer and is not passed by value."""
    result = _compile_and_eval("""
    struct S { int x; int y; };
    int f(int n) {
        struct S s;
        s.x = n;
        s.y = n * 2;
        return s.x + s.y;
    }
    int main(void) { return f(10); }
    """)
    assert result == 30


def test_ssa_lowering_local_struct_value_array_field():
    """Phase 4 MVP: array field on a local struct value (zlib deflate-state
    shape)."""
    result = _compile_and_eval("""
    struct S { int arr[10]; };
    int f(int n) {
        struct S s;
        int i;
        int sum = 0;
        for (i = 0; i < 10; i = i + 1) s.arr[i] = i * n;
        for (i = 0; i < 10; i = i + 1) sum = sum + s.arr[i];
        return sum;
    }
    int main(void) { return f(3); }
    """)
    # sum = 3 * (0+1+...+9) = 135
    assert result == 135


def test_ssa_lowering_local_struct_value_nested_field():
    """Phase 4 MVP: nested struct/inner-field access on a local struct value."""
    result = _compile_and_eval("""
    struct Inner { int v; };
    struct Outer { struct Inner inner; int tag; };
    int f(int n) {
        struct Outer o;
        o.inner.v = n * 2;
        o.tag = n;
        return o.inner.v + o.tag;
    }
    int main(void) { return f(5); }
    """)
    assert result == 15


def test_ssa_lowering_address_of_local_struct():
    """Phase 4 MVP: `&s` on a struct-alloca local yields the alloca pointer
    directly, so `helper(&s)` and `p = &s; p->field` both work."""
    result = _compile_and_eval("""
    struct S { int v; };
    int helper(struct S *p) { return p->v + 100; }
    int f(int n) {
        struct S s;
        s.v = n;
        return helper(&s);
    }
    int main(void) { return f(7); }
    """)
    assert result == 107


def test_ssa_lowering_struct_positional_init_list():
    """Phase 4 MVP: positional scalar-only InitList like `struct S s = {1, 2};`
    emits element-wise stores to each field."""
    result = _compile_and_eval("""
    struct S { int x; int y; };
    int f(int n) {
        struct S s = {1, 2};
        return s.x + s.y + n;
    }
    int main(void) { return f(100); }
    """)
    assert result == 103


def test_ssa_lowering_struct_partial_init_list_zero_fills():
    """Phase 4 MVP: partial InitList zero-initializes remaining fields per
    C99 6.7.8p19. Covers the `struct S s = {0};` memset-like pattern that
    is ubiquitous in C code."""
    result = _compile_and_eval("""
    struct S { int x; int y; int z; };
    int f(void) {
        struct S s = {0};
        s.y = 42;
        return s.x + s.y + s.z;
    }
    int main(void) { return f(); }
    """)
    assert result == 42


def test_ssa_lowering_scalar_struct_copy_assignment():
    """Phase 4 MVP: `s2 = s1` where both are local structs with scalar
    fields is lowered as a field-by-field load/store chain."""
    result = _compile_and_eval("""
    struct S { int x; int y; };
    int f(int n) {
        struct S s1;
        struct S s2;
        s1.x = n;
        s1.y = n * 2;
        s2 = s1;
        return s2.x + s2.y;
    }
    int main(void) { return f(10); }
    """)
    assert result == 30


def test_ssa_lowering_array_of_struct_field_access():
    """Phase 4 MVP: `arr[i].field` on a local array of structs — both write
    and read — works through the synthetic `(&arr[i])->field` path in
    `_lower_struct_ref_addr_with_decl`."""
    result = _compile_and_eval("""
    struct E { int k; int v; };
    int f(int n) {
        struct E table[3];
        table[0].k = 1; table[0].v = 100;
        table[1].k = 2; table[1].v = 200;
        table[2].k = 3; table[2].v = 300;
        for (int i = 0; i < 3; i = i + 1) {
            if (table[i].k == n) return table[i].v;
        }
        return -1;
    }
    int main(void) { return f(2); }
    """)
    assert result == 200


def test_ssa_lowering_local_array_positional_init():
    """Phase 4 MVP: `int a[3] = {1, 2, 3};` — positional scalar-only
    array initializer emits alloca + indexed stores; partial initializers
    zero-fill remaining elements per C99."""
    full = _compile_and_eval("""
    int main(void) {
        int a[3] = {1, 2, 3};
        return a[0] + a[1] + a[2];
    }
    """)
    assert full == 6

    partial = _compile_and_eval("""
    int main(void) {
        int a[5] = {10, 20};
        return a[0] + a[1] + a[2] + a[3] + a[4];
    }
    """)
    assert partial == 30


def test_ssa_lowering_struct_copy_with_array_field():
    """Phase 4 MVP: struct-to-struct copy where field is an array —
    copied via unrolled indexed load/store."""
    result = _compile_and_eval("""
    struct S { int a; int arr[3]; };
    int main(void) {
        struct S s1 = {1, {2, 3, 4}};
        struct S s2;
        s2 = s1;
        return s2.a + s2.arr[0] + s2.arr[1] + s2.arr[2];
    }
    """)
    assert result == 10


def test_ssa_lowering_struct_copy_with_nested_struct():
    """Phase 4 MVP: struct-to-struct copy where field is itself a struct —
    copied via recursive field-by-field copy through the inner struct's
    address."""
    result = _compile_and_eval("""
    struct Inner { int a; int b; };
    struct Outer { struct Inner inner; int tag; };
    int main(void) {
        struct Outer o1 = {{10, 20}, 5};
        struct Outer o2;
        o2 = o1;
        return o2.inner.a + o2.inner.b + o2.tag;
    }
    """)
    assert result == 35


def test_ssa_lowering_nested_struct_field_init():
    """Phase 4 MVP: initializing a struct-typed field inside a struct
    via an inner InitList, both positional and designated forms."""
    pos = _compile_and_eval("""
    struct Inner { int a; int b; };
    struct Outer { struct Inner inner; int tag; };
    int main(void) {
        struct Outer o = {{1, 2}, 5};
        return o.inner.a + o.inner.b + o.tag;
    }
    """)
    assert pos == 8

    designated = _compile_and_eval("""
    struct Inner { int a; int b; };
    struct Outer { struct Inner inner; int tag; };
    int main(void) {
        struct Outer o = {.tag = 100, .inner = {.a = 10, .b = 20}};
        return o.inner.a + o.inner.b + o.tag;
    }
    """)
    assert designated == 130


def test_ssa_lowering_2d_array_basic():
    """Phase 4 MVP: 2D array `int mat[N][M]` with nested loops for
    write + read. Alloca is flat (N*M), index is computed as
    `i*M + j`."""
    result = _compile_and_eval("""
    int main(void) {
        int mat[3][3];
        for (int i = 0; i < 3; i = i + 1) {
            for (int j = 0; j < 3; j = j + 1) {
                mat[i][j] = i * 10 + j;
            }
        }
        int sum = 0;
        for (int i = 0; i < 3; i = i + 1) {
            for (int j = 0; j < 3; j = j + 1) {
                sum = sum + mat[i][j];
            }
        }
        return sum;
    }
    """)
    # 0+1+2 + 10+11+12 + 20+21+22 = 99
    assert result == 99


def test_ssa_lowering_2d_array_compound_assign():
    """Phase 4 MVP: 2D array with compound assignment `mat[i][j] += X`."""
    result = _compile_and_eval("""
    int main(void) {
        int mat[2][3];
        for (int i = 0; i < 2; i = i + 1)
            for (int j = 0; j < 3; j = j + 1)
                mat[i][j] = 0;
        mat[1][2] = 10;
        mat[1][2] += 5;
        return mat[1][2];
    }
    """)
    assert result == 15


def test_ssa_lowering_array_designator_init():
    """Phase 4 MVP: array designators `[N] = value` support, including
    out-of-order with zero-fill and mixed positional + designated."""
    simple = _compile_and_eval("""
    int main(void) {
        int a[5] = {[0] = 1, [2] = 3};
        return a[0] + a[1] + a[2] + a[3] + a[4];
    }
    """)
    assert simple == 4

    ooo = _compile_and_eval("""
    int main(void) {
        int a[5] = {[4] = 40, [1] = 10, [2] = 20};
        return a[0] + a[1] + a[2] + a[3] + a[4];
    }
    """)
    assert ooo == 70


def test_ssa_lowering_struct_with_array_field_nested_init():
    """Phase 4 MVP: nested InitList for struct with array field like
    `struct S { int a; int arr[3]; }; struct S s = {1, {2, 3, 4}};`."""
    full = _compile_and_eval("""
    struct S { int a; int arr[3]; };
    int main(void) {
        struct S s = {1, {2, 3, 4}};
        return s.a + s.arr[0] + s.arr[1] + s.arr[2];
    }
    """)
    assert full == 10

    # Partial array in struct init — remaining elements zero-filled.
    partial = _compile_and_eval("""
    struct S { int a; int arr[5]; };
    int main(void) {
        struct S s = {100, {1, 2}};
        return s.a + s.arr[0] + s.arr[1] + s.arr[2] + s.arr[3] + s.arr[4];
    }
    """)
    assert partial == 103


def test_ssa_lowering_string_literal_char_array_init():
    """Phase 4 MVP: `char s[] = "hi";` decodes the string literal
    (including escapes) into a positional init list and emits per-byte
    stores. Unsized arrays infer count from the literal (including NUL);
    sized arrays truncate or zero-fill trailing elements."""
    # Unsized: "hi" -> 'h'(104) + 'i'(105) + NUL(0) = 209
    r1 = _compile_and_eval("""
    int main(void) {
        char s[] = "hi";
        return s[0] + s[1] + s[2];
    }
    """)
    assert r1 == 209

    # Sized with zero fill: "abc" -> 97+98+99+0+0 = 294
    r2 = _compile_and_eval("""
    int main(void) {
        char s[5] = "abc";
        return s[0] + s[1] + s[2] + s[3] + s[4];
    }
    """)
    assert r2 == 294

    # Loop over NUL-terminated string
    r3 = _compile_and_eval("""
    int main(void) {
        char s[] = "hello";
        int sum = 0;
        for (int i = 0; s[i] != 0; i = i + 1) sum = sum + s[i];
        return sum;
    }
    """)
    # 'h'(104)+'e'(101)+'l'(108)+'l'(108)+'o'(111) = 532
    assert r3 == 532


def test_ssa_lowering_struct_designated_init():
    """Phase 4 MVP: designated initializers `.field = value` including
    out-of-order designators and zero-fill of omitted fields."""
    # Full designated
    full = _compile_and_eval("""
    struct S { int x; int y; int z; };
    int main(void) {
        struct S s = {.x = 10, .y = 20, .z = 30};
        return s.x + s.y + s.z;
    }
    """)
    assert full == 60

    # Out-of-order with zero fill
    ooo = _compile_and_eval("""
    struct S { int x; int y; int z; };
    int main(void) {
        struct S s = {.z = 3, .x = 1};
        return s.x * 100 + s.y * 10 + s.z;
    }
    """)
    assert ooo == 103


def test_ssa_lowering_for_loop():
    """For loop with multi-def scalars."""
    result = _compile_and_eval("""
    int f(int n) {
        int sum = 0;
        int i;
        for (i = 0; i < n; i = i + 1) {
            sum = sum + i;
        }
        return sum;
    }
    int main(void) {
        return f(6);
    }
    """)
    # 0+1+2+3+4+5 = 15
    assert result == 15


def test_ssa_lowering_arithmetic_ops():
    """Various arithmetic operations."""
    result = _compile_and_eval("""
    int f(int a, int b) {
        int sum = a + b;
        int diff = a - b;
        int prod = a * b;
        return sum + diff + prod;
    }
    int main(void) {
        return f(7, 3);
    }
    """)
    # sum=10, diff=4, prod=21 → 35
    assert result == 35


def test_ssa_lowering_with_direct_call():
    """Function with a direct call to another function."""
    result = _compile_and_eval("""
    int add(int a, int b) {
        return a + b;
    }
    int f(int x) {
        int y = add(x, 10);
        return y + 1;
    }
    int main(void) {
        return f(5);
    }
    """)
    # add(5,10)=15, 15+1=16
    assert result == 16


def test_ssa_lowering_with_call_in_branch():
    """Call inside an if/else branch with multi-def scalar."""
    result = _compile_and_eval("""
    int double_it(int x) {
        return x + x;
    }
    int negate(int x) {
        return 0 - x;
    }
    int f(int x) {
        int r = 0;
        if (x > 0) {
            r = double_it(x);
        } else {
            r = negate(x);
        }
        return r;
    }
    int main(void) {
        return f(7);
    }
    """)
    assert result == 14


def test_ssa_lowering_with_bare_call_statement():
    """Statement-position direct calls should stay inside SSA lowering."""
    result = _compile_and_eval("""
    int g = 0;

    void bump(int x) {
        g += x;
    }

    int f(int x) {
        bump(x);
        return g;
    }

    int main(void) {
        return f(5) == 5 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_with_string_literal_call_argument():
    """String literals should flow through SSA calls into existing codegen."""
    result = _compile_and_eval("""
    int starts_with_h(const char *msg) {
        return msg[0] == 104;
    }

    int main(void) {
        return starts_with_h("hi") ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_string_literal_pointer_phi():
    """String literals flowing through SSA phis should stay pointer-typed."""
    result = _compile_and_eval("""
    char *pick(int flag, char *fallback) {
        return flag ? "" : fallback;
    }

    int main(void) {
        char buf[2];
        buf[0] = 'a';
        buf[1] = 0;
        return pick(1, buf)[0] == 0 && pick(0, buf)[0] == 'a' ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_call_in_loop():
    """Call inside a loop."""
    result = _compile_and_eval("""
    int inc(int x) {
        return x + 1;
    }
    int f(int n) {
        int sum = 0;
        int i = 0;
        while (i < n) {
            sum = sum + inc(i);
            i = i + 1;
        }
        return sum;
    }
    int main(void) {
        return f(5);
    }
    """)
    # inc(0)+inc(1)+inc(2)+inc(3)+inc(4) = 1+2+3+4+5 = 15
    assert result == 15


def test_ssa_lowering_sccp_constant_fold_through_join():
    """SCCP-proven constant at join point is folded during lowering."""
    result = _compile_and_eval("""
    int f(int c) {
        int x = 0;
        if (c) {
            x = 42;
        } else {
            x = 42;
        }
        return x + 1;
    }
    int main(void) {
        return f(1);
    }
    """)
    # x is always 42, so x+1 = 43
    assert result == 43


def test_ssa_lowering_sccp_dead_branch_elimination():
    """SCCP-proven dead branch is eliminated during lowering."""
    result = _compile_and_eval("""
    int f(void) {
        int x = 1;
        if (x) {
            return 10;
        } else {
            return 20;
        }
    }
    int main(void) {
        return f();
    }
    """)
    assert result == 10


def test_ssa_lowering_compound_assignment():
    """Compound assignments (+=, -=) work through SSA lowering."""
    result = _compile_and_eval("""
    int f(int n) {
        int sum = 0;
        int i = 0;
        while (i < n) {
            sum += i;
            i += 1;
        }
        return sum;
    }
    int main(void) {
        return f(5);
    }
    """)
    assert result == 10


def test_ssa_lowering_do_while():
    """Do-while loop through SSA lowering."""
    result = _compile_and_eval("""
    int f(int n) {
        int i = 0;
        int sum = 0;
        do {
            sum += i;
            i += 1;
        } while (i < n);
        return sum;
    }
    int main(void) {
        return f(4);
    }
    """)
    # 0+1+2+3 = 6
    assert result == 6


def test_ssa_lowering_compare_result_behaves_like_int():
    """C comparisons produce int results, not i1-sized temporaries."""
    result = _compile_and_eval("""
    int f(int x) {
        int y = (x > 0);
        return y + 41;
    }
    int main(void) {
        return f(1);
    }
    """)
    assert result == 42


def test_ssa_lowering_logical_not_result_behaves_like_int():
    """Logical not should also produce an int-typed result."""
    result = _compile_and_eval("""
    int f(int x) {
        int y = !x;
        return y + 41;
    }
    int main(void) {
        return f(0);
    }
    """)
    assert result == 42


def test_ssa_lowering_unsigned_compare_uses_unsigned_predicate():
    """Unsigned comparisons must not be lowered with signed icmp."""
    result = _compile_and_eval("""
    int f(unsigned int x) {
        unsigned int y = 1U;
        return x > y;
    }
    int main(void) {
        return f(4294967295U) ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_preserves_unsigned_call_result_through_join():
    """Unsigned-returning calls must keep unsigned SSA type metadata."""
    result = _compile_and_eval("""
    unsigned int hi(void) {
        return 4294967295U;
    }
    unsigned int lo(void) {
        return 1U;
    }
    int f(int c) {
        unsigned int x;
        if (c) {
            x = hi();
        } else {
            x = lo();
        }
        return x > 2U;
    }
    int main(void) {
        return f(1) ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_preserves_pointer_call_result_through_join():
    """Pointer-returning calls must not be forced through int SSA types."""
    result = _compile_and_eval("""
    int g_left = 7;
    int g_right = 9;

    int *pick_left(void) {
        return &g_left;
    }

    int *pick_right(void) {
        return &g_right;
    }

    int *f(int c) {
        int *p;
        if (c) {
            p = pick_left();
        } else {
            p = pick_right();
        }
        return p;
    }

    int main(void) {
        return *f(1) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_explicit_integer_cast_in_realistic_path():
    """Explicit integer casts should stay inside the direct SSA lowering path."""
    result = _compile_and_eval("""
    unsigned long combine(unsigned long adler1, unsigned long adler2, long long len2) {
        unsigned long sum1;
        unsigned long sum2;
        unsigned rem;

        rem = (unsigned)len2;
        sum1 = adler1 & 0xffffUL;
        sum2 = rem * sum1;
        sum1 += (adler2 & 0xffffUL);
        return sum1 + sum2;
    }

    int main(void) {
        return combine(1UL, 2UL, 3LL) == 6UL ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_pointer_arrayref_load():
    """Pointer-indexed loads should stay inside the direct SSA lowering slice."""
    result = _compile_and_eval("""
    int f(unsigned char *p) {
        int x = p[0];
        return x == 200 ? 0 : 1;
    }

    int main(void) {
        unsigned char buf[1];
        buf[0] = 200;
        return f(buf);
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_pointer_deref_load():
    """Simple pointer dereference should lower through SSA load nodes."""
    result = _compile_and_eval("""
    int g = 7;

    int f(int *p) {
        return *p;
    }

    int main(void) {
        return f(&g) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_postdecrement_condition():
    """Condition-side postdecrement should update the loop-carried SSA value."""
    result = _compile_and_eval("""
    int f(unsigned int n) {
        int count = 0;
        while (n--) {
            count += 1;
        }
        return count;
    }

    int main(void) {
        return f(3U) == 3 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_pointer_postincrement_value_expression():
    """Value-position pointer postincrement should preserve old and new pointer values."""
    result = _compile_and_eval("""
    int f(int *p) {
        int x = *p++;
        int y = *p;
        return x + y;
    }

    int main(void) {
        int buf[2];
        buf[0] = 3;
        buf[1] = 4;
        return f(buf) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_pointer_compare_against_null_constant():
    """Pointer-vs-zero comparisons should coerce the zero to a null pointer."""
    result = _compile_and_eval("""
    int f(unsigned char *p) {
        if (p == 0) {
            return 1;
        }
        return 0;
    }

    int main(void) {
        return f((unsigned char *)0) == 1 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_top_level_ternary_value():
    """Top-level ternary values should lower through CFG + phi."""
    result = _compile_and_eval("""
    int f(int x) {
        return x ? 11 : 22;
    }

    int main(void) {
        return f(0) == 22 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_struct_pointer_field_access():
    """Named-struct pointer field loads should stay inside direct SSA lowering."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int f(struct S *s) {
        return s->mode;
    }

    int main(void) {
        struct S s;
        s.mode = 7;
        return f(&s) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_nested_aggregate_field_chain():
    """Nested state->x.have-style field chains should stay inside SSA lowering."""
    result = _compile_and_eval("""
    typedef struct {
        int have;
    } inner_t;

    typedef struct {
        inner_t x;
        int mode;
    } state_t;

    int f(state_t *s) {
        return s->x.have + s->mode;
    }

    int main(void) {
        state_t s;
        s.x.have = 3;
        s.mode = 4;
        return f(&s) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_address_of_struct_field():
    """Address-of on a struct field should reuse the SSA field-address path."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int *pick(struct S *s) {
        return &s->mode;
    }

    int main(void) {
        struct S s;
        s.mode = 9;
        return *pick(&s) == 9 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_struct_field_store_statement():
    """Struct field stores should stay inside the direct SSA slice."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int f(struct S *s) {
        s->mode = 7;
        return s->mode;
    }

    int main(void) {
        struct S s;
        s.mode = 0;
        return f(&s) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_nested_struct_field_store_chain():
    """Nested state->x.have stores should reuse field-address lowering."""
    result = _compile_and_eval("""
    typedef struct {
        int have;
    } inner_t;

    typedef struct {
        inner_t x;
        int mode;
    } state_t;

    int f(state_t *s) {
        s->x.have = 3;
        s->mode = 4;
        return s->x.have + s->mode;
    }

    int main(void) {
        state_t s;
        s.x.have = 0;
        s.mode = 0;
        return f(&s) == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_implicit_void_fallthrough_return():
    """Void SSA-lowered functions should not require an explicit return."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    void set(struct S *s) {
        s->mode = 7;
    }

    int main(void) {
        struct S s;
        s.mode = 0;
        set(&s);
        return s.mode == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_cast_statement_as_discarded_expression():
    """(void)call; should stay inside the direct SSA lowering slice."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int helper(struct S *s) {
        s->mode = 7;
        return 0;
    }

    void set(struct S *s) {
        (void)helper(s);
    }

    int main(void) {
        struct S s;
        s.mode = 0;
        set(&s);
        return s.mode == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_sizeof_typename_constants():
    """sizeof(type) should fold inside the SSA builder path."""
    result = _compile_and_eval("""
    int f(void) {
        return sizeof(int) != sizeof(void *);
    }

    int main(void) {
        return f() == 1 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_sizeof_deref_named_struct():
    """sizeof(*p) should infer the pointed-to named aggregate size."""
    result = _compile_and_eval("""
    struct S {
        int a;
        int b;
    };

    int f(struct S *s) {
        return sizeof(*s);
    }

    int main(void) {
        struct S s;
        return f(&s) == 8 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_sizeof_struct_with_constant_expression_array_member():
    """sizeof(struct ...) should handle array members sized by constant expressions."""
    result = _compile_and_eval("""
    enum { N = 4 };

    struct S {
        int data[N + 1];
    };

    int main(void) {
        return sizeof(struct S) == 20 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_char_constants_in_ssa_conditions():
    """Character constants should lower through the direct SSA path."""
    result = _compile_and_eval("""
    int is_digit(const char *mode) {
        return *mode >= '0' && *mode <= '9';
    }

    int main(void) {
        if (is_digit("7") != 1) {
            return 1;
        }
        if (is_digit("x") != 0) {
            return 2;
        }
        return 0;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_enum_constants():
    """File-scope enum constants should lower as plain SSA constants."""
    result = _compile_and_eval("""
    enum Mode {
        HEAD = 16180,
        FLAGS,
        DICT = 9
    };

    int f(int mode) {
        return mode == HEAD || mode == FLAGS || mode == DICT;
    }

    int main(void) {
        return f(HEAD) == 1 && f(FLAGS) == 1 && f(DICT) == 1 && f(0) == 0 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_keeps_local_decl_and_assignment_types_stable():
    """Local SSA variables should retain their declared type across joins."""
    result = _compile_and_eval("""
    typedef unsigned long ulg;
    typedef unsigned int uInt;

    struct S {
        ulg pending;
        uInt avail_out;
    };

    int f(struct S *s) {
        unsigned len = s->pending;
        if (len > s->avail_out) {
            len = s->avail_out;
        }
        return len == 3 ? 0 : 1;
    }

    int main(void) {
        struct S s;
        s.pending = 5;
        s.avail_out = 3;
        return f(&s);
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_pointer_difference_feeding_pointer_arithmetic():
    """Pointer subtraction should produce an integer result usable in later gep arithmetic."""
    result = _compile_and_eval("""
    int f(char *base) {
        char *p = base + 3;
        char *q = base + (p - base);
        return q == base + 3 ? 0 : 1;
    }

    int main(void) {
        char buf[4];
        return f(buf);
    }
    """)
    assert result == 0


def test_ssa_lowering_filters_phi_incomings_after_constant_branch_pruning():
    """Phi lowering should skip predecessors pruned away by SCCP/codegen branch folding."""
    result = _compile_and_eval("""
    int f(void) {
        unsigned len = 5;
        do {
            len = 3;
        } while (0);
        return len == 3 ? 0 : 1;
    }

    int main(void) {
        return f();
    }
    """)
    assert result == 0


def test_ssa_lowering_merges_ternary_arms_to_a_common_integer_type():
    """Ternary arms with narrower integer loads should be widened before phi creation."""
    result = _compile_and_eval("""
    int f(char *buf, int flag) {
        return flag ? -1 : buf[0];
    }

    int main(void) {
        char buf[2];
        buf[0] = 'a';
        buf[1] = 0;
        return f(buf, 0) == 'a' && f(buf, 1) == -1 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_file_scope_globals():
    """Direct SSA lowering should read scalar globals and decay global arrays."""
    result = _compile_and_eval("""
    int g = 7;
    int table[3] = {4, 9, 12};

    int pick(int i) {
        return g + table[i];
    }

    int main(void) {
        return pick(1) == 16 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_field_extract_from_global_array_elements():
    """Aggregate values loaded from global arrays should support scalar field extracts."""
    result = _compile_and_eval("""
    struct Entry {
        int value;
        int other;
    };

    struct Entry table[2] = {{1, 2}, {3, 4}};

    int pick(int i) {
        return table[i].other;
    }

    int main(void) {
        return pick(1) == 4 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_indirect_function_pointer_calls():
    """SSA calls should support function-pointer callees, not just direct IDs."""
    result = _compile_and_eval("""
    typedef int (*fn_t)(int);

    int inc(int x) {
        return x + 1;
    }

    int apply(fn_t fn, int x) {
        return (*fn)(x);
    }

    int main(void) {
        return apply(inc, 4) == 5 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_assignment_expression_for_locals():
    """Value-position local assignments should update the live SSA env."""
    result = _compile_and_eval("""
    int f(int y) {
        int x = 0;
        if ((x = y + 1) == 4) {
            return x;
        }
        return 0;
    }

    int main(void) {
        return f(3) == 4 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_assignment_expression_for_struct_fields():
    """Value-position field assignments should store and yield the assigned value."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int set(struct S *s) {
        return (s->mode = 7);
    }

    int main(void) {
        struct S s;
        s.mode = 0;
        return set(&s) == 7 && s.mode == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_struct_field_compound_assignment():
    """Compound field stores should stay inside the direct SSA slice."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int bump(struct S *s) {
        s->mode += 2;
        return s->mode;
    }

    int main(void) {
        struct S s;
        s.mode = 5;
        return bump(&s) == 7 && s.mode == 7 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_pointer_deref_assignment():
    """Pointer-deref stores should lower through SSAStore directly."""
    result = _compile_and_eval("""
    int write_and_read(int *p) {
        *p = 7;
        *p += 2;
        return *p;
    }

    int main(void) {
        int value = 0;
        return write_and_read(&value) == 9 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_array_element_assignment():
    """Array element stores on pointer-like bases should lower via gep+store."""
    result = _compile_and_eval("""
    struct S {
        int data[4];
    };

    int write_slot(struct S *s) {
        s->data[1] = 7;
        s->data[1] += 2;
        return s->data[1];
    }

    int main(void) {
        struct S s;
        s.data[1] = 0;
        return write_slot(&s) == 9 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_fixed_local_arrays():
    """Fixed-size local arrays should lower as explicit stack allocations."""
    result = _compile_and_eval("""
    int f(void) {
        unsigned char buf[1];
        buf[0] = 7;
        buf[0] += 2;
        return buf[0];
    }

    int main(void) {
        return f() == 9 ? 0 : 1;
    }
    """)
    assert result == 0


def test_ssa_lowering_handles_struct_field_increment():
    """Non-ID ++/-- targets should lower for aggregate fields."""
    result = _compile_and_eval("""
    struct S {
        int mode;
    };

    int bump(struct S *s) {
        s->mode++;
        s->mode++;
        return s->mode;
    }

    int main(void) {
        struct S s;
        s.mode = 5;
        return bump(&s) == 7 ? 0 : 1;
    }
    """)
    assert result == 0
