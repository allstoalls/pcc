from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.memory_opt import MemoryOptIRPass


def test_memory_opt_records_store_load_forward_and_load_load_elim():
    ir_text = """
define i32 @main(i32* %p) {
entry:
  store i32 7, i32* %p
  %x = load i32, i32* %p
  %y = load i32, i32* %p
  ret i32 %y
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%x = load i32, i32* %p" not in out
    assert "%y = load i32, i32* %p" not in out
    assert "ret i32 7" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 2
    assert "memory_opt.load_load_elim" not in ctx.stats


def test_memory_opt_eliminates_redundant_overwritten_store():
    ir_text = """
define i32 @main(i32* %p, i32 %v) {
entry:
  store i32 1, i32* %p
  store i32 %v, i32* %p
  %x = load i32, i32* %p
  ret i32 %x
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "store i32 1, i32* %p" not in out
    assert "store i32 %v, i32* %p" in out
    assert "%x = load i32, i32* %p" not in out
    assert "ret i32 %v" in out
    assert ctx.stats["memory_opt.redundant_store_elim"] == 1
    assert ctx.stats["memory_opt.store_load_forward"] == 1


def test_memory_opt_reuses_repeated_load_after_kept_load():
    ir_text = """
define i32 @main(i32* %p) {
entry:
  %x = load i32, i32* %p
  %y = load i32, i32* %p
  %z = add i32 %x, %y
  ret i32 %z
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%y = load i32, i32* %p" not in out
    assert "%z = add i32 %x, %x" in out
    assert ctx.stats["memory_opt.load_load_elim"] == 1


def test_memory_opt_preserves_independent_alloca_store_facts():
    ir_text = """
define i32 @main(i32 %a, i32 %b) {
entry:
  %x = alloca i32
  %y = alloca i32
  store i32 %a, i32* %x
  store i32 %b, i32* %y
  %vx = load i32, i32* %x
  ret i32 %vx
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%vx = load i32, i32* %x" not in out
    assert "ret i32 %a" in out
    assert "store i32 %b, i32* %y" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 1


def test_memory_opt_tracks_bitcast_alias_of_exact_alloca_slot():
    ir_text = """
define i32 @main(i32 %a) {
entry:
  %x = alloca i32
  %alias = bitcast i32* %x to i32*
  store i32 %a, i32* %alias
  %v = load i32, i32* %x
  ret i32 %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%v = load i32, i32* %x" not in out
    assert "ret i32 %a" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 1


def test_memory_opt_tracks_bitcast_alias_after_forwarded_pointer_load():
    ir_text = """
define i32 @main(i32 %a) {
entry:
  %x = alloca i32
  %p = alloca i8*
  %cast = bitcast i32* %x to i8*
  store i8* %cast, i8** %p
  %loaded = load i8*, i8** %p
  %alias = bitcast i8* %loaded to i32*
  store i32 %a, i32* %alias
  %v = load i32, i32* %x
  ret i32 %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%loaded = load i8*, i8** %p" not in out
    assert "%v = load i32, i32* %x" not in out
    assert "ret i32 %a" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 2


def test_memory_opt_tracks_zero_gep_alias_of_exact_alloca_slot():
    ir_text = """
define i32 @main(i32 %a) {
entry:
  %x = alloca i32
  %alias = getelementptr inbounds i32, i32* %x, i64 0
  store i32 %a, i32* %alias
  %v = load i32, i32* %x
  ret i32 %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%v = load i32, i32* %x" not in out
    assert "ret i32 %a" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 1


def test_memory_opt_tracks_zero_gep_alias_after_forwarded_pointer_load():
    ir_text = """
define i32 @main(i32 %a) {
entry:
  %x = alloca i32
  %slot = alloca i32*
  store i32* %x, i32** %slot
  %loaded = load i32*, i32** %slot
  %alias = getelementptr inbounds i32, i32* %loaded, i64 0
  store i32 %a, i32* %alias
  %v = load i32, i32* %x
  ret i32 %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%loaded = load i32*, i32** %slot" not in out
    assert "%v = load i32, i32* %x" not in out
    assert "ret i32 %a" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 2


def test_memory_opt_tracks_zero_gep_alias_through_zero_cast_index():
    ir_text = """
define i32 @main(i32 %a) {
entry:
  %x = alloca [1 x i32]
  %idx = sext i32 0 to i64
  %alias = getelementptr [1 x i32], [1 x i32]* %x, i64 0, i64 %idx
  store i32 %a, i32* %alias
  %idx2 = sext i32 0 to i64
  %alias2 = getelementptr [1 x i32], [1 x i32]* %x, i64 0, i64 %idx2
  %v = load i32, i32* %alias2
  ret i32 %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%v = load i32, i32* %alias2" not in out
    assert "ret i32 %a" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 1


def test_memory_opt_does_not_treat_zero_gep_from_bitcast_aggregate_view_as_exact_slot():
    ir_text = """
define double @main(void) {
entry:
  %u = alloca { i64 }
  %bits = bitcast { i64 }* %u to [2 x i32]*
  %idx = sext i32 0 to i64
  %word0 = getelementptr [2 x i32], [2 x i32]* %bits, i64 0, i64 %idx
  store i32 1, i32* %word0
  %as_double = bitcast { i64 }* %u to double*
  %v = load double, double* %as_double
  ret double %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%v = load double, double* %as_double" in out
    assert "memory_opt.store_load_forward" not in ctx.stats


def test_memory_opt_does_not_forward_bitcast_alias_across_type_mismatch():
    ir_text = """
define i32 @main(void) {
entry:
  %x = alloca i32
  %alias = bitcast i32* %x to i8*
  store i8 7, i8* %alias
  %v = load i32, i32* %x
  ret i32 %v
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%v = load i32, i32* %x" in out
    assert "memory_opt.store_load_forward" not in ctx.stats


def test_memory_opt_unknown_load_keeps_prior_exact_store_live():
    ir_text = """
define void @main(i32* %alias) {
entry:
  %x = alloca i32
  store i32 1, i32* %x
  %seen = load i32, i32* %alias
  store i32 2, i32* %x
  ret void
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "store i32 1, i32* %x" in out
    assert "store i32 2, i32* %x" in out
    assert "memory_opt.redundant_store_elim" not in ctx.stats


def test_memory_opt_clears_memory_facts_across_call_boundary():
    ir_text = """
declare void @touch(i32* %p)
define i32 @main(i32* %p) {
entry:
  store i32 7, i32* %p
  call void @touch(i32* %p)
  %x = load i32, i32* %p
  ret i32 %x
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%x = load i32, i32* %p" in out
    assert "ret i32 7" not in out
    assert "memory_opt.store_load_forward" not in ctx.stats


def test_memory_opt_does_not_leak_aliases_across_function_boundaries():
    ir_text = """
define i32 @first(i32* %p) {
entry:
  store i32 0, i32* %p
  %x = load i32, i32* %p
  ret i32 %x
}

define void @second(i32* %p, i32 %v) {
entry:
  store i32 %v, i32* %p
  ret void
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "define void @second(i32* %p, i32 %v)" in out
    assert "store i32 %v, i32* %p" in out


def test_memory_opt_records_memcpy_like_call_boundary():
    ir_text = """
define void @copy(ptr %dst, ptr %src) {
entry:
  call void @memcpy(ptr %dst, ptr %src, i64 4)
  ret void
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert out == ir_text
    assert ctx.stats["memory_opt.memcpy_like_calls"] == 1


def test_memory_opt_treats_va_arg_as_memory_barrier():
    ir_text = """
declare void @sink(i8* %p)
define void @consume(i8* %incoming) {
entry:
  %ap = alloca i8*
  store i8* %incoming, i8** %ap
  %arg = va_arg i8** %ap, i64
  %next = load i8*, i8** %ap
  call void @sink(i8* %next)
  ret void
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%next = load i8*, i8** %ap" in out
    assert "call void @sink(i8* %incoming)" not in out
    assert "memory_opt.store_load_forward" not in ctx.stats


def test_memory_opt_rewrites_forwarded_cursor_at_va_arg_barrier():
    ir_text = """
define i32 @consume(ptr %incoming) {
entry:
  %ap = alloca ptr
  store ptr %incoming, ptr %ap
  %cursor = load ptr, ptr %ap
  %arg = va_arg ptr %cursor, i32
  %next = load ptr, ptr %ap
  %same = icmp eq ptr %next, %incoming
  %result = select i1 %same, i32 %arg, i32 0
  ret i32 %result
}
""".strip()
    ctx = PassContext()

    out = MemoryOptIRPass().run(ir_text, ctx)

    assert "%cursor = load" not in out
    assert "%arg = va_arg ptr %incoming, i32" in out
    assert "%next = load ptr, ptr %ap" in out
    assert ctx.stats["memory_opt.store_load_forward"] == 1
