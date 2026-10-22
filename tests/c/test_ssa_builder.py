import pytest

from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.ssa import SSABinaryOp, SSABranch, SSABuilder, SSACall, SSACast, SSAConstant, SSAFieldAddr, SSAFieldExtract, SSAGlobalRef, SSALoad, SSAPhi, SSAReturn, SSAStackAlloc, SSAStore, SSAStringConstant
from pcc.frontends.c.ssa.builder import SSAConstructionError
from pcc.frontends.c.ssa.ir import SSAJump


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _build_function(source: str, name: str | None = None):
    ast = _PARSER.parse(source)
    builder = SSABuilder()
    builder.index_file_scope(ast)
    if name is None:
        funcdef = next(ext for ext in ast.ext if hasattr(ext, "body"))
    else:
        funcdef = next(ext for ext in ast.ext if getattr(getattr(ext, "decl", None), "name", None) == name)
    return builder.build_function(funcdef)


def test_ssa_builder_lowers_straight_line_scalar_function():
    func = _build_function(
        """
        int scale(int x) {
            int y = x + 1;
            y = y * 2;
            return y;
        }
        """
    )

    assert func.name == "scale"
    assert [param.source_name for param in func.params] == ["x"]
    assert [block.name for block in func.blocks] == ["entry"]
    assert [binding.kind for binding in func.bindings] == ["decl_init", "assign"]
    assert [binding.target_name for binding in func.bindings] == ["y", "y"]

    entry = func.block("entry")
    assert [type(instr) for instr in entry.instructions] == [SSABinaryOp, SSABinaryOp]
    assert entry.instructions[0].source_coord is not None
    assert ("y", "$t.0") in entry.instructions[1].available_bindings
    assert isinstance(entry.terminator, SSAReturn)
    assert entry.terminator.value == entry.instructions[-1]
    assert entry.terminator.source_coord is not None
    assert func.dominators["entry"] == {"entry"}
    assert func.immediate_dominators["entry"] is None


def test_ssa_builder_inserts_phi_for_branch_merge():
    func = _build_function(
        """
        int choose(int x, int a, int b) {
            int y = a;
            if (x < 0) {
                y = a + 1;
            } else {
                y = b + 2;
            }
            return y;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.terminator, SSABranch)
    assert len(func.blocks) == 4

    join_blocks = [
        block for block in func.blocks if any(isinstance(instr, SSAPhi) for instr in block.instructions)
    ]
    assert len(join_blocks) == 1

    join = join_blocks[0]
    phi = join.instructions[0]
    assert isinstance(phi, SSAPhi)
    assert phi.variable_name == "y"
    assert {pred for pred, _ in phi.incomings} == set(join.predecessors)
    assert isinstance(join.terminator, SSAReturn)
    assert join.terminator.value == phi
    assert func.dominators[join.name] == {"entry", join.name}
    assert func.immediate_dominators[join.name] == "entry"


def test_ssa_builder_handles_single_live_branch_exit_without_phi():
    func = _build_function(
        """
        int maybe_update(int x) {
            int y = 1;
            if (x < 0) {
                return y;
            }
            y = 2;
            return y;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.terminator, SSABranch)
    assert len(func.blocks) == 4

    join = [
        block for block in func.blocks if block.name.startswith("if.end.")
    ][0]
    assert join.predecessors == ["if.else.2"]
    assert not any(isinstance(instr, SSAPhi) for instr in join.instructions)
    assert isinstance(join.terminator, SSAReturn)


def test_ssa_builder_lowers_direct_function_call_expression():
    func = _build_function(
        """
        int f(int x) {
            int y = helper(x);
            return y;
        }
        """
    )

    entry = func.block("entry")
    assert len(entry.instructions) == 1
    call = entry.instructions[0]
    assert isinstance(call, SSACall)
    assert call.callee_name == "helper"
    assert [arg.name for arg in call.args] == ["x.0"]
    assert call.source_coord is not None
    assert isinstance(entry.terminator, SSAReturn)
    assert entry.terminator.value == call


def test_ssa_builder_lowers_bare_function_call_statement():
    func = _build_function(
        """
        int helper(int x);

        int f(int x) {
            helper(x);
            return x;
        }
        """
    )

    entry = func.block("entry")
    assert len(entry.instructions) == 1
    call = entry.instructions[0]
    assert isinstance(call, SSACall)
    assert call.callee_name == "helper"


def test_ssa_builder_allows_implicit_void_fallthrough_return():
    func = _build_function(
        """
        void set(int *p) {
            helper(p);
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSACall)
    assert isinstance(entry.terminator, SSAReturn)
    assert entry.terminator.value is None


def test_ssa_builder_lowers_cast_statement_as_discarded_expression():
    func = _build_function(
        """
        int helper(int x);

        void set(int x) {
            (void)helper(x);
        }
        """
    )

    entry = func.block("entry")
    assert len(entry.instructions) >= 2
    assert isinstance(entry.instructions[0], SSACall)
    assert isinstance(entry.instructions[1], SSACast)


def test_ssa_builder_folds_sizeof_to_unsigned_constant():
    func = _build_function(
        """
        int f(void) {
            return sizeof(int) != sizeof(void *);
        }
        """
    )

    entry = func.block("entry")
    cmp = entry.instructions[0]
    assert isinstance(cmp, SSABinaryOp)
    assert cmp.left.type_name == "unsigned long"
    assert cmp.right.type_name == "unsigned long"


def test_ssa_builder_lowers_string_literal_call_argument():
    func = _build_function(
        """
        int logmsg(const char *msg);

        int f(void) {
            return logmsg("hi");
        }
        """
    )

    entry = func.block("entry")
    call = entry.instructions[0]
    assert isinstance(call, SSACall)
    assert len(call.args) == 1
    assert isinstance(call.args[0], SSAStringConstant)


def test_ssa_builder_lowers_indirect_call_target():
    func = _build_function(
        """
        typedef int (*fn_t)(int);

        int apply(fn_t fn, int x) {
            return (*fn)(x);
        }
        """
    )

    entry = func.block("entry")
    call = entry.instructions[0]
    assert isinstance(call, SSACall)
    assert call.callee_name == ""
    assert call.callee is not None
    assert getattr(call.callee, "source_name", "") == "fn"


def test_ssa_builder_lowers_char_constants():
    func = _build_function(
        """
        int f(void) {
            return '9' - '0';
        }
        """
    )

    entry = func.block("entry")
    sub = entry.instructions[0]
    assert isinstance(sub, SSABinaryOp)
    assert isinstance(sub.left, SSAConstant)
    assert isinstance(sub.right, SSAConstant)
    assert sub.left.value == ord("9")
    assert sub.right.value == ord("0")


def test_ssa_builder_resolves_enum_constants():
    func = _build_function(
        """
        enum Mode {
            HEAD = 16180,
            FLAGS,
            STORED = 7
        };

        int f(void) {
            return FLAGS - HEAD + STORED;
        }
        """
    )

    entry = func.block("entry")
    first = entry.instructions[0]
    second = entry.instructions[1]
    assert isinstance(first, SSABinaryOp)
    assert isinstance(first.left, SSAConstant)
    assert isinstance(first.right, SSAConstant)
    assert first.left.value == 16181
    assert first.right.value == 16180
    assert isinstance(second, SSABinaryOp)
    assert isinstance(second.right, SSAConstant)
    assert second.right.value == 7


def test_ssa_builder_folds_sizeof_struct_with_constant_expression_array_member():
    func = _build_function(
        """
        enum { N = 4 };

        struct S {
            int data[N + 1];
        };

        int f(void) {
            return sizeof(struct S);
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.terminator, SSAReturn)
    assert isinstance(entry.terminator.value, SSAConstant)
    assert entry.terminator.value.value == 20


def test_ssa_builder_resolves_file_scope_globals():
    func = _build_function(
        """
        int g = 7;

        int f(void) {
            return g;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.terminator, SSAReturn)
    assert isinstance(entry.terminator.value, SSAGlobalRef)
    assert entry.terminator.value.symbol_name == "g"


def test_ssa_builder_resolves_known_extern_globals():
    func = _build_function(
        """
        int f(void) {
            return errno;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.terminator, SSAReturn)
    assert isinstance(entry.terminator.value, SSAGlobalRef)
    assert entry.terminator.value.symbol_name == "errno"


def test_ssa_builder_updates_env_for_assignment_expression():
    func = _build_function(
        """
        int f(void) {
            int x = 0;
            if ((x = 7) == 7) {
                return x;
            }
            return 1;
        }
        """
    )

    assert [binding.kind for binding in func.bindings] == ["decl_init", "assign"]
    then_block = next(block for block in func.blocks if block.name.startswith("if.then."))
    assert isinstance(then_block.terminator, SSAReturn)
    assert isinstance(then_block.terminator.value, SSAConstant)
    assert then_block.terminator.value.value == 7


def test_ssa_builder_uses_file_scope_unsigned_return_type_for_calls():
    func = _build_function(
        """
        unsigned int hi(void);
        unsigned int lo(void);

        int f(int x) {
            unsigned int y;
            if (x) {
                y = hi();
            } else {
                y = lo();
            }
            return y > 2U;
        }
        """,
        name="f",
    )

    calls = [
        instr
        for block in func.blocks
        for instr in block.instructions
        if isinstance(instr, SSACall)
    ]
    assert len(calls) == 2
    assert {call.type_name for call in calls} == {"unsigned int"}


def test_ssa_builder_uses_file_scope_pointer_return_type_for_calls():
    func = _build_function(
        """
        int *pick_left(int *p);
        int *pick_right(int *p);

        int *f(int c, int *a, int *b) {
            int *r;
            if (c) {
                r = pick_left(a);
            } else {
                r = pick_right(b);
            }
            return r;
        }
        """,
        name="f",
    )

    calls = [
        instr
        for block in func.blocks
        for instr in block.instructions
        if isinstance(instr, SSACall)
    ]
    assert len(calls) == 2
    assert {call.type_name for call in calls} == {"int*"}


def test_ssa_builder_lowers_explicit_integer_cast():
    func = _build_function(
        """
        int f(long x) {
            unsigned int y = (unsigned int)x;
            return y > 0U;
        }
        """
    )

    casts = [
        instr
        for block in func.blocks
        for instr in block.instructions
        if isinstance(instr, SSACast)
    ]
    assert len(casts) == 1
    assert casts[0].type_name == "unsigned int"
    assert casts[0].operand.type_name == "long"


def test_ssa_builder_lowers_pointer_arrayref_load():
    func = _build_function(
        """
        int f(unsigned char *p) {
            int x = p[0];
            return x;
        }
        """
    )

    loads = [
        instr
        for block in func.blocks
        for instr in block.instructions
        if isinstance(instr, SSALoad)
    ]
    assert len(loads) == 1
    assert loads[0].type_name == "unsigned char"
    assert loads[0].base.type_name == "unsigned char*"
    assert loads[0].index.type_name == "int"


def test_ssa_builder_lowers_pointer_deref_load():
    func = _build_function(
        """
        int f(int *p) {
            return *p;
        }
        """
    )

    entry = func.block("entry")
    assert len(entry.instructions) == 1
    load = entry.instructions[0]
    assert isinstance(load, SSALoad)
    assert load.type_name == "int"
    assert load.index is None


def test_ssa_builder_lowers_break_in_while_loop():
    func = _build_function(
        """
        int f(int n) {
            int i = 0;
            int found = 0;
            while (i < n) {
                if (i == 5) {
                    found = 1;
                    break;
                }
                i = i + 1;
            }
            return found;
        }
        """
    )

    exit_block = [b for b in func.blocks if b.name.startswith("while.end")][0]
    preds = set(exit_block.predecessors)
    # Should have the header (cond=false) plus the break site.
    assert len(preds) >= 2
    phi_instrs = [i for i in exit_block.instructions if isinstance(i, SSAPhi)]
    found_phis = [p for p in phi_instrs if p.variable_name == "found"]
    assert found_phis, "expected phi for found at the while-exit block"


def test_ssa_builder_lowers_break_in_for_loop():
    func = _build_function(
        """
        int f(int n) {
            int found = 0;
            for (int i = 0; i < n; i = i + 1) {
                if (i == 3) {
                    found = 1;
                    break;
                }
            }
            return found;
        }
        """
    )

    exit_block = [b for b in func.blocks if b.name.startswith("for.end")][0]
    preds = set(exit_block.predecessors)
    assert len(preds) >= 2
    phi_instrs = [i for i in exit_block.instructions if isinstance(i, SSAPhi)]
    found_phis = [p for p in phi_instrs if p.variable_name == "found"]
    assert found_phis, "expected phi for found at the for-exit block"


def test_ssa_builder_lowers_break_in_infinite_for_loop():
    func = _build_function(
        """
        int f(int n) {
            int i = 0;
            for (;;) {
                if (i == n) {
                    break;
                }
                i = i + 1;
            }
            return i;
        }
        """
    )

    exit_block = [b for b in func.blocks if b.name.startswith("for.end")][0]
    preds = set(exit_block.predecessors)
    # Infinite for(;;) with one break: only the break site reaches exit.
    assert len(preds) == 1


def test_ssa_builder_rejects_break_outside_loop():
    with pytest.raises(SSAConstructionError, match="break outside of loop"):
        _build_function(
            """
            int f(int n) {
                if (n) {
                    break;
                }
                return n;
            }
            """
        )


def test_ssa_builder_lowers_continue_in_while_loop():
    func = _build_function(
        """
        int f(int n) {
            int i = 0;
            int sum = 0;
            while (i < n) {
                i = i + 1;
                if (i == 2) {
                    continue;
                }
                sum = sum + i;
            }
            return sum;
        }
        """
    )

    header = [b for b in func.blocks if b.name.startswith("while.header")][0]
    assert len(header.predecessors) >= 3
    phi_i = [p for p in header.instructions if isinstance(p, SSAPhi) and p.variable_name == "i"][0]
    assert len(phi_i.incomings) == len(header.predecessors)


def test_ssa_builder_lowers_continue_in_for_loop_runs_next():
    func = _build_function(
        """
        int f(int n) {
            int sum = 0;
            for (int i = 0; i < n; i = i + 1) {
                if (i == 2) {
                    continue;
                }
                sum = sum + i;
            }
            return sum;
        }
        """
    )

    continue_blocks = [b for b in func.blocks if b.name.startswith("for.continue")]
    assert len(continue_blocks) == 1
    term = continue_blocks[0].terminator
    assert isinstance(term, SSAJump)
    assert term.target.startswith("for.header")
    assert len(continue_blocks[0].predecessors) >= 2


def test_ssa_builder_lowers_continue_in_do_while_routes_through_latch():
    func = _build_function(
        """
        int f(int n) {
            int i = 0;
            int sum = 0;
            do {
                i = i + 1;
                if (i == 2) {
                    continue;
                }
                sum = sum + i;
            } while (i < n);
            return sum;
        }
        """
    )

    latch = [b for b in func.blocks if b.name.startswith("dowhile.latch")][0]
    assert len(latch.predecessors) >= 2
    phis = [p for p in latch.instructions if isinstance(p, SSAPhi)]
    assert phis, "expected latch phi merging continue vs fall-through envs"


def test_ssa_builder_rejects_continue_outside_loop():
    with pytest.raises(SSAConstructionError, match="continue outside of loop"):
        _build_function(
            """
            int f(int n) {
                if (n) {
                    continue;
                }
                return n;
            }
            """
        )


def test_ssa_builder_lowers_postdecrement_condition_and_carries_updated_env():
    func = _build_function(
        """
        int f(unsigned int n) {
            while (n--) {
                return n;
            }
            return n;
        }
        """
    )

    header = [b for b in func.blocks if "while.header" in b.name][0]
    decs = [instr for instr in header.instructions if isinstance(instr, SSABinaryOp)]
    assert len(decs) == 1
    assert decs[0].op == "-"


def test_ssa_builder_lowers_pointer_postincrement_value_expression():
    func = _build_function(
        """
        int f(int *p) {
            int x = *p++;
            return x + *p;
        }
        """
    )

    entry = func.block("entry")
    loads = [instr for instr in entry.instructions if isinstance(instr, SSALoad)]
    assert len(loads) == 2
    assert loads[0].base.name == "p.0"
    post_inc = next(instr for instr in entry.instructions if isinstance(instr, SSABinaryOp) and instr.left.name == "p.0")
    assert loads[1].base.name == post_inc.name


def test_ssa_builder_lowers_top_level_ternary_value_via_phi():
    func = _build_function(
        """
        int f(int x) {
            return x ? 1 : 2;
        }
        """
    )

    phi_blocks = [
        block for block in func.blocks
        if any(isinstance(instr, SSAPhi) for instr in block.instructions)
    ]
    assert phi_blocks
    phi = phi_blocks[0].instructions[0]
    assert isinstance(phi, SSAPhi)
    assert {value.value for _, value in phi.incomings} == {1, 2}


def test_ssa_builder_lowers_struct_pointer_field_access():
    func = _build_function(
        """
        struct S {
            int mode;
        };

        int f(struct S *s) {
            return s->mode;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert entry.instructions[0].type_name == "int*"
    assert isinstance(entry.instructions[1], SSALoad)
    assert entry.instructions[1].type_name == "int"


def test_ssa_builder_lowers_nested_aggregate_field_chain():
    func = _build_function(
        """
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
        """
    )

    entry = func.block("entry")
    field_addrs = [instr for instr in entry.instructions if isinstance(instr, SSAFieldAddr)]
    loads = [instr for instr in entry.instructions if isinstance(instr, SSALoad)]
    assert len(field_addrs) == 3
    assert field_addrs[0].field_name == "x"
    assert field_addrs[0].type_name == "inner_t*"
    assert field_addrs[1].field_name == "have"
    assert field_addrs[1].type_name == "int*"
    assert field_addrs[2].field_name == "mode"
    assert field_addrs[2].type_name == "int*"
    assert len(loads) == 2


def test_ssa_builder_lowers_address_of_struct_field():
    func = _build_function(
        """
        struct S {
            int mode;
        };

        int *f(struct S *s) {
            return &s->mode;
        }
        """
    )

    entry = func.block("entry")
    assert len(entry.instructions) == 1
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert entry.instructions[0].type_name == "int*"


def test_ssa_builder_lowers_struct_field_store_statement():
    func = _build_function(
        """
        struct S {
            int mode;
        };

        int f(struct S *s) {
            s->mode = 7;
            return s->mode;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert isinstance(entry.instructions[1], SSAStore)
    assert isinstance(entry.instructions[2], SSAFieldAddr)
    assert isinstance(entry.instructions[3], SSALoad)


def test_ssa_builder_lowers_struct_field_assignment_expression():
    func = _build_function(
        """
        struct S {
            int mode;
        };

        int f(struct S *s) {
            return (s->mode = 7);
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert isinstance(entry.instructions[1], SSAStore)
    assert isinstance(entry.terminator, SSAReturn)
    assert isinstance(entry.terminator.value, SSAConstant)
    assert entry.terminator.value.value == 7


def test_ssa_builder_lowers_struct_field_compound_assignment():
    func = _build_function(
        """
        struct S {
            int mode;
        };

        int f(struct S *s) {
            s->mode += 2;
            return s->mode;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert isinstance(entry.instructions[1], SSALoad)
    assert isinstance(entry.instructions[2], SSABinaryOp)
    assert isinstance(entry.instructions[3], SSAStore)


def test_ssa_builder_lowers_pointer_deref_assignment_statement():
    func = _build_function(
        """
        int f(int *p) {
            *p = 7;
            return *p;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAStore)
    assert isinstance(entry.instructions[1], SSALoad)


def test_ssa_builder_lowers_array_element_assignment_statement():
    func = _build_function(
        """
        struct S {
            int data[4];
        };

        int f(struct S *s) {
            s->data[1] = 7;
            return s->data[1];
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert isinstance(entry.instructions[1], SSABinaryOp)
    assert isinstance(entry.instructions[2], SSAStore)


def test_ssa_builder_lowers_fixed_local_array_declaration():
    func = _build_function(
        """
        int f(void) {
            unsigned char buf[1];
            buf[0] = 7;
            return buf[0];
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAStackAlloc)
    assert entry.instructions[0].elem_type_name == "unsigned char"
    assert entry.instructions[0].count == 1


def test_ssa_builder_lowers_struct_field_increment_statement():
    func = _build_function(
        """
        struct S {
            int mode;
        };

        int f(struct S *s) {
            s->mode++;
            return s->mode;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSAFieldAddr)
    assert isinstance(entry.instructions[1], SSALoad)
    assert isinstance(entry.instructions[2], SSABinaryOp)
    assert isinstance(entry.instructions[3], SSAStore)


def test_ssa_builder_lowers_field_extract_from_global_array_element():
    func = _build_function(
        """
        struct Entry {
            int value;
            int other;
        };

        struct Entry table[2] = {{1, 2}, {3, 4}};

        int f(int i) {
            return table[i].other;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.instructions[0], SSALoad)
    assert isinstance(entry.instructions[1], SSAFieldExtract)


def test_ssa_builder_lowers_short_circuit_condition_via_cfg_edges():
    func = _build_function(
        """
        int f(int x) {
            if (x && helper()) {
                return 1;
            }
            return 0;
        }
        """
    )

    entry = func.block("entry")
    assert isinstance(entry.terminator, SSABranch)
    assert entry.terminator.true_target.startswith("if.cond.rhs.")
    assert entry.terminator.false_target.startswith("if.else.")

    rhs_block = func.block(entry.terminator.true_target)
    assert len(rhs_block.instructions) == 1
    assert isinstance(rhs_block.instructions[0], SSACall)
    assert isinstance(rhs_block.terminator, SSABranch)
    assert rhs_block.terminator.true_target.startswith("if.then.")
    assert rhs_block.terminator.false_target.startswith("if.else.")


def test_ssa_builder_lowers_short_circuit_value_via_phi():
    func = _build_function(
        """
        int f(int x) {
            int y = x && helper();
            return y;
        }
        """
    )

    phi_blocks = [
        block for block in func.blocks
        if any(isinstance(instr, SSAPhi) for instr in block.instructions)
    ]
    assert phi_blocks
    phi = phi_blocks[0].instructions[0]
    assert isinstance(phi, SSAPhi)
    assert {value.value for _, value in phi.incomings} == {0, 1}
    assert any(
        isinstance(instr, SSACall)
        for block in func.blocks
        for instr in block.instructions
    )


# -- While loop tests --


def test_ssa_builder_lowers_while_loop_with_phi():
    func = _build_function(
        """
        int sum(int n) {
            int s = 0;
            int i = 0;
            while (i < n) {
                s = s + i;
                i = i + 1;
            }
            return s;
        }
        """
    )

    # Should have: entry, while.header, while.body, while.end
    block_names = [b.name for b in func.blocks]
    assert any("while.header" in n for n in block_names)
    assert any("while.body" in n for n in block_names)
    assert any("while.end" in n for n in block_names)

    # Header should have phi nodes for loop-carried variables (s, i, n).
    header = [b for b in func.blocks if "while.header" in b.name][0]
    phis = [i for i in header.instructions if isinstance(i, SSAPhi)]
    phi_vars = {p.variable_name for p in phis}
    assert "s" in phi_vars
    assert "i" in phi_vars

    # Each header phi should have 2 incomings: pre-header + back-edge.
    for phi in phis:
        if phi.variable_name in {"s", "i"}:
            assert len(phi.incomings) == 2, f"phi for {phi.variable_name} has {len(phi.incomings)} incomings"

    # Header should branch on condition.
    assert isinstance(header.terminator, SSABranch)

    # Exit block should have a return.
    exit_block = [b for b in func.blocks if "while.end" in b.name][0]
    assert isinstance(exit_block.terminator, SSAReturn)


def test_ssa_builder_lowers_while_loop_with_early_return():
    func = _build_function(
        """
        int find(int n) {
            int i = 0;
            while (i < n) {
                if (i == 5) {
                    return i;
                }
                i = i + 1;
            }
            return 0;
        }
        """
    )

    # Should still produce valid SSA with dominators.
    assert func.dominators
    # There should be a return in the body (from the early exit).
    returns = [
        b for b in func.blocks
        if isinstance(b.terminator, SSAReturn)
    ]
    assert len(returns) >= 2  # early return + final return


# -- Do-while loop tests --


def test_ssa_builder_lowers_dowhile_loop():
    func = _build_function(
        """
        int count(int n) {
            int i = 0;
            do {
                i = i + 1;
            } while (i < n);
            return i;
        }
        """
    )

    block_names = [b.name for b in func.blocks]
    assert any("dowhile.body" in n for n in block_names)
    assert any("dowhile.latch" in n for n in block_names)
    assert any("dowhile.end" in n for n in block_names)

    body = [b for b in func.blocks if "dowhile.body" in b.name][0]
    phis = [i for i in body.instructions if isinstance(i, SSAPhi)]
    phi_vars = {p.variable_name for p in phis}
    assert "i" in phi_vars

    # Body phi should have 2 incomings.
    for phi in phis:
        if phi.variable_name == "i":
            assert len(phi.incomings) == 2

    latch = [b for b in func.blocks if "dowhile.latch" in b.name][0]
    assert isinstance(latch.terminator, SSABranch)


# -- For loop tests --


def test_ssa_builder_lowers_for_loop():
    func = _build_function(
        """
        int sum_to(int n) {
            int s = 0;
            int i;
            for (i = 0; i < n; i = i + 1) {
                s = s + i;
            }
            return s;
        }
        """
    )

    block_names = [b.name for b in func.blocks]
    assert any("for.header" in n for n in block_names)
    assert any("for.body" in n for n in block_names)
    assert any("for.end" in n for n in block_names)

    header = [b for b in func.blocks if "for.header" in b.name][0]
    phis = [i for i in header.instructions if isinstance(i, SSAPhi)]
    phi_vars = {p.variable_name for p in phis}
    assert "i" in phi_vars
    assert "s" in phi_vars
    assert isinstance(header.terminator, SSABranch)


def test_ssa_builder_lowers_for_loop_with_decl_init():
    func = _build_function(
        """
        int sum_to(int n) {
            int s = 0;
            for (int i = 0; i < n; i = i + 1) {
                s = s + i;
            }
            return s;
        }
        """
    )

    header = [b for b in func.blocks if "for.header" in b.name][0]
    phis = [i for i in header.instructions if isinstance(i, SSAPhi)]
    phi_vars = {p.variable_name for p in phis}
    assert "i" in phi_vars
    assert "s" in phi_vars

    # i should not be visible in the exit env (scoped to the for).
    exit_block = [b for b in func.blocks if "for.end" in b.name][0]
    assert isinstance(exit_block.terminator, SSAReturn)


def test_ssa_builder_lowers_for_loop_with_postincrement():
    func = _build_function(
        """
        int sum_to(int n) {
            int s = 0;
            for (int i = 0; i < n; i++) {
                s = s + i;
            }
            return s;
        }
        """
    )

    header = [b for b in func.blocks if "for.header" in b.name][0]
    phis = [i for i in header.instructions if isinstance(i, SSAPhi)]
    assert any(p.variable_name == "i" for p in phis)


# -- Compound assignment tests --


def test_ssa_builder_lowers_compound_assignment():
    func = _build_function(
        """
        int f(int x) {
            int y = 1;
            y += x;
            return y;
        }
        """
    )

    entry = func.block("entry")
    # Should have a binary op for the compound assignment.
    binops = [i for i in entry.instructions if isinstance(i, SSABinaryOp)]
    assert len(binops) == 1
    assert binops[0].op == "+"
    assert isinstance(entry.terminator, SSAReturn)
    assert entry.terminator.value == binops[0]


def test_ssa_builder_lowers_compound_assignment_in_loop():
    func = _build_function(
        """
        int f(int n) {
            int s = 0;
            int i = 0;
            while (i < n) {
                s += i;
                i += 1;
            }
            return s;
        }
        """
    )

    header = [b for b in func.blocks if "while.header" in b.name][0]
    phis = [i for i in header.instructions if isinstance(i, SSAPhi)]
    phi_vars = {p.variable_name for p in phis}
    assert "s" in phi_vars
    assert "i" in phi_vars

    body = [b for b in func.blocks if "while.body" in b.name][0]
    binops = [i for i in body.instructions if isinstance(i, SSABinaryOp)]
    assert len(binops) == 2  # s + i, i + 1


# -- Inc/dec statement tests --


def test_ssa_builder_lowers_standalone_increment():
    func = _build_function(
        """
        int f(int x) {
            x++;
            return x;
        }
        """
    )

    entry = func.block("entry")
    binops = [i for i in entry.instructions if isinstance(i, SSABinaryOp)]
    assert len(binops) == 1
    assert binops[0].op == "+"
    assert isinstance(entry.terminator, SSAReturn)
    assert entry.terminator.value == binops[0]


def test_ssa_builder_lowers_prefix_decrement():
    func = _build_function(
        """
        int f(int x) {
            --x;
            return x;
        }
        """
    )

    entry = func.block("entry")
    binops = [i for i in entry.instructions if isinstance(i, SSABinaryOp)]
    assert len(binops) == 1
    assert binops[0].op == "-"


# -- Edge case: non-local inc/dec rejected --


def test_ssa_builder_lowers_infinite_for_loop_with_early_return():
    func = _build_function(
        """
        int f(int n) {
            int i = 0;
            for (;;) {
                if (i >= n) return i;
                i = i + 1;
            }
        }
        """
    )

    block_names = [b.name for b in func.blocks]
    assert any("for.header" in n for n in block_names)
    assert any("for.body" in n for n in block_names)
    # The exit block exists in the block list but is unreachable.
    assert any("for.end" in n for n in block_names)
    # There should be at least one return inside the body.
    returns = [
        b for b in func.blocks
        if isinstance(b.terminator, SSAReturn)
    ]
    assert len(returns) >= 1


def test_ssa_builder_rejects_non_local_incdec():
    with pytest.raises(SSAConstructionError, match="non-local"):
        ast = _PARSER.parse(
            """
            int g;
            int f(void) {
                g++;
                return 0;
            }
            """
        )
        funcdef = ast.ext[1]  # skip the global decl
        SSABuilder().build_function(funcdef)
