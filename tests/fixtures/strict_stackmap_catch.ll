activation.entry.5717:
  %activation.entered.129 = call i64 () @py_recursion_enter()
  %.130 = icmp ne i64 %activation.entered.129, 0
  br i1 %.130, label %entry, label %activation.failure.5718

entry:
  %except.match.slot.5573.11 = alloca ptr
  %except.match.owner.5572.owned.15 = alloca i1
  %handled.context.5578.17 = alloca [2 x ptr]
  %except.class.slot.5584.21 = alloca ptr
  %except.class.owner.5583.owned.25 = alloca i1
  %except.class.replacement.slot.5611.45 = alloca ptr
  %except.class.replacement.owner.5610.owned.49 = alloca i1
  %err.empty.owner.5629.61 = alloca ptr
  store ptr null, ptr %err.empty.owner.5629.61
  %err.empty.owner.5635.67 = alloca ptr
  store ptr null, ptr %err.empty.owner.5635.67
  %err.empty.owner.5641.73 = alloca ptr
  store ptr null, ptr %err.empty.owner.5641.73
  %except.handler.slot.5652.82 = alloca ptr
  %except.handler.owner.5651.owned.87 = alloca i1
  %err.empty.owner.5657.88 = alloca ptr
  store ptr null, ptr %err.empty.owner.5657.88
  %handled.context.5663.94 = alloca [2 x ptr]
  %gc.safepoint.pending.5557.1 = load atomic i32, ptr @pcc_gc_backend4_auto_step_pending acquire, align 4
  %gc.safepoint.needed.5558.2 = icmp ne i32 %gc.safepoint.pending.5557.1, 0
  store ptr null, ptr %except.match.slot.5573.11
  %gc.frame.map.ptr.5574.12 = bitcast ptr @.pcc.gc.frame.map.1 to ptr
  %gc.frame.slots.ptr.5575.13 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @pcc_gc_frame_enter(ptr %gc.frame.map.ptr.5574.12, ptr %gc.frame.slots.ptr.5575.13)
  store i1 0, ptr %except.match.owner.5572.owned.15
  store ptr null, ptr %except.class.slot.5584.21
  %gc.frame.map.ptr.5585.22 = bitcast ptr @.pcc.gc.frame.map.1 to ptr
  %gc.frame.slots.ptr.5586.23 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr, ptr) @pcc_gc_frame_enter(ptr %gc.frame.map.ptr.5585.22, ptr %gc.frame.slots.ptr.5586.23)
  store i1 0, ptr %except.class.owner.5583.owned.25
  store ptr null, ptr %except.class.replacement.slot.5611.45
  %gc.frame.map.ptr.5612.46 = bitcast ptr @.pcc.gc.frame.map.1 to ptr
  %gc.frame.slots.ptr.5613.47 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr, ptr) @pcc_gc_frame_enter(ptr %gc.frame.map.ptr.5612.46, ptr %gc.frame.slots.ptr.5613.47)
  store i1 0, ptr %except.class.replacement.owner.5610.owned.49
  store ptr null, ptr %except.handler.slot.5652.82
  %gc.frame.map.ptr.5653.83 = bitcast ptr @.pcc.gc.frame.map.1 to ptr
  %gc.frame.slots.ptr.5654.84 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @pcc_gc_frame_enter(ptr %gc.frame.map.ptr.5653.83, ptr %gc.frame.slots.ptr.5654.84)
  store i1 0, ptr %except.handler.owner.5651.owned.87
  br i1 %gc.safepoint.needed.5558.2, label %gc.safepoint.slow.5559, label %gc.safepoint.cont.5560

gc.safepoint.slow.5559:
  call void () @pcc_gc_safepoint()
  br label %gc.safepoint.cont.5560

gc.safepoint.cont.5560:
  %unsafe.addr.int.5563.3 = ptrtoint ptr null to i64
  %.4 = inttoptr i64 %unsafe.addr.int.5563.3 to ptr
  %unsafe.ptr_is_null.i.5564.5 = ptrtoint ptr %.4 to i64
  %unsafe.ptr.null.5565.6 = icmp eq i64 %unsafe.ptr_is_null.i.5564.5, 0
  %b2i64.5566.7 = zext i1 %unsafe.ptr.null.5565.6 to i64
  %icmp.5567.8 = icmp ne i64 %b2i64.5566.7, 0
  %gc.frame.leave.ptr.5576.14 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5576.14)
  %gc.frame.leave.ptr.5587.24 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5587.24)
  %gc.frame.leave.ptr.5614.48 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5614.48)
  %gc.frame.leave.ptr.5656.86 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5656.86)
  call void (i64) @py_recursion_leave(i64 %activation.entered.129)
  ret i1 %icmp.5567.8

try.done.5561:
  %except.class.owner.5583.owned.load.5697.117 = load i1, ptr %except.class.owner.5583.owned.25
  br i1 %except.class.owner.5583.owned.load.5697.117, label %except.class.owner.5583.owned.release.5698, label %except.class.owner.5583.owned.cont.5699

try.err.5562:
  %try.cur_exc.5568.9 = call ptr () @py_current_exception()
  br label %except.test.0.5569

except.test.0.5569:
  %except.match.exception.5571.10 = call ptr () @py_current_exception()
  store i1 1, ptr %except.match.owner.5572.owned.15
  %gc.ptr.5577.16 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5577.16, ptr %except.match.exception.5571.10)
  %gc.ptr.5579.18 = bitcast ptr %handled.context.5578.17 to ptr
  %gc.ptr.5580.19 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @py_handled_context_push(ptr %gc.ptr.5579.18, ptr %gc.ptr.5580.19)
  call void () @py_clear_exception()
  %exc.cls.NotImplementedError.5582.20 = call ptr (i64) @py_exc_builtin_class(i64 11)
  store i1 1, ptr %except.class.owner.5583.owned.25
  %gc.ptr.5588.26 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5588.26, ptr %exc.cls.NotImplementedError.5582.20)
  %gc.ptr.5589.27 = bitcast ptr %except.class.slot.5584.21 to ptr
  %except.class.live.5590.28 = call ptr (ptr, ptr) @pcc_gc_load_ptr(ptr null, ptr %gc.ptr.5589.27)
  %handler.exception.root.5591.29 = bitcast ptr %except.match.slot.5573.11 to ptr
  %handler.exception.active.5592.30 = call ptr (ptr, ptr) @pcc_gc_load_ptr(ptr null, ptr %handler.exception.root.5591.29)
  %gc.ptr.5593.31 = bitcast ptr %except.class.slot.5584.21 to ptr
  %except.class.match.5594.32 = call ptr (ptr, ptr) @pcc_gc_load_ptr(ptr null, ptr %gc.ptr.5593.31)
  %exc.matches.5595.33 = call i64 (ptr, ptr) @py_exc_match_handler(ptr %handler.exception.active.5592.30, ptr %except.class.match.5594.32)
  %gc.ptr.5596.34 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5596.34, ptr null)
  %err.flag.5597.35 = call i64 () @py_err_occurred()
  %err.cmp.5598.36 = icmp ne i64 %err.flag.5597.35, 0
  br i1 %err.cmp.5598.36, label %err.frame.5600, label %call.cont.5599

except.body.0.5570:
  %except.handler.exception.5650.81 = call ptr () @py_current_exception()
  store i1 1, ptr %except.handler.owner.5651.owned.87
  %gc.ptr.5662.93 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5662.93, ptr %except.handler.exception.5650.81)
  %gc.ptr.5664.95 = bitcast ptr %handled.context.5663.94 to ptr
  %gc.ptr.5665.96 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @py_handled_context_push(ptr %gc.ptr.5664.95, ptr %gc.ptr.5665.96)
  call void () @py_clear_exception()
  %gc.ptr.5667.97 = bitcast ptr %handled.context.5663.94 to ptr
  %handled.context.popped.5668.98 = call i64 (ptr) @py_handled_context_pop(ptr %gc.ptr.5667.97)
  %.99 = icmp ne i64 %handled.context.popped.5668.98, 0
  br i1 %.99, label %handled.context.clear.5669, label %handled.context.done.5670

except.class.error.5581:
  %except.class.exception.5609.44 = call ptr () @py_current_exception()
  store i1 1, ptr %except.class.replacement.owner.5610.owned.49
  %gc.ptr.5615.50 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5615.50, ptr %except.class.exception.5609.44)
  %gc.ptr.5616.51 = bitcast ptr %except.match.slot.5573.11 to ptr
  %except.class.original.5617.52 = call ptr (ptr, ptr) @pcc_gc_load_ptr(ptr null, ptr %gc.ptr.5616.51)
  %except.class.distinct.5618.53 = icmp ne ptr %except.class.exception.5609.44, %except.class.original.5617.52
  br i1 %except.class.distinct.5618.53, label %except.class.context.5619, label %except.class.release.5620

call.cont.5599:
  %gc.ptr.5602.38 = bitcast ptr %except.match.slot.5573.11 to ptr
  %except.match.restore.5603.39 = call ptr (ptr, ptr) @pcc_gc_load_ptr(ptr null, ptr %gc.ptr.5602.38)
  call void (ptr) @py_raise(ptr %except.match.restore.5603.39)
  %gc.ptr.5604.40 = bitcast ptr %handled.context.5578.17 to ptr
  %handled.context.popped.5605.41 = call i64 (ptr) @py_handled_context_pop(ptr %gc.ptr.5604.40)
  %.42 = icmp ne i64 %handled.context.popped.5605.41, 0
  br i1 %.42, label %handled.context.clear.5606, label %handled.context.done.5607

err.frame.5600:
  %err.frame.exc.5601.37 = call ptr () @py_current_exception()
  call void (ptr, ptr, ptr, ptr, i32) @py_exc_append_frame_source(ptr %err.frame.exc.5601.37, ptr @.tb.func.54, ptr @.tb.file.2, ptr @.tb.source.55, i32 206)
  br label %except.class.error.5581

handled.context.clear.5606:
  %gc.ptr.5608.43 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5608.43, ptr null)
  br label %handled.context.done.5607

handled.context.done.5607:
  %exc.matches.i1.5647.79 = icmp ne i64 %exc.matches.5595.33, 0
  br i1 %exc.matches.i1.5647.79, label %except.body.0.5570, label %except.propagate.5648

except.class.context.5619:
  %gc.ptr.5621.54 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  %gc.ptr.5622.55 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @py_exc_set_implicit_context_slots(ptr %gc.ptr.5621.54, ptr %gc.ptr.5622.55)
  br label %except.class.release.5620

except.class.release.5620:
  %gc.ptr.5623.56 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5623.56, ptr null)
  %gc.ptr.5624.57 = bitcast ptr %handled.context.5578.17 to ptr
  %handled.context.popped.5625.58 = call i64 (ptr) @py_handled_context_pop(ptr %gc.ptr.5624.57)
  %.59 = icmp ne i64 %handled.context.popped.5625.58, 0
  br i1 %.59, label %handled.context.clear.5626, label %handled.context.done.5627

handled.context.clear.5626:
  %gc.ptr.5628.60 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5628.60, ptr null)
  br label %handled.context.done.5627

handled.context.done.5627:
  br label %err.exit

err.exit:
  %except.match.owner.5572.err.owned.5630.62 = load i1, ptr %except.match.owner.5572.owned.15
  %except.match.owner.5572.err.slot.5631.63 = bitcast ptr %except.match.slot.5573.11 to ptr
  %except.match.owner.5572.err.empty.5632.64 = bitcast ptr %err.empty.owner.5629.61 to ptr
  %except.match.owner.5572.err.active.slot.5633.65 = select i1 %except.match.owner.5572.err.owned.5630.62, ptr %except.match.owner.5572.err.slot.5631.63, ptr %except.match.owner.5572.err.empty.5632.64
  store i1 0, ptr %except.match.owner.5572.owned.15
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.match.owner.5572.err.active.slot.5633.65, ptr null)
  %except.class.owner.5583.err.owned.5636.68 = load i1, ptr %except.class.owner.5583.owned.25
  %except.class.owner.5583.err.slot.5637.69 = bitcast ptr %except.class.slot.5584.21 to ptr
  %except.class.owner.5583.err.empty.5638.70 = bitcast ptr %err.empty.owner.5635.67 to ptr
  %except.class.owner.5583.err.active.slot.5639.71 = select i1 %except.class.owner.5583.err.owned.5636.68, ptr %except.class.owner.5583.err.slot.5637.69, ptr %except.class.owner.5583.err.empty.5638.70
  store i1 0, ptr %except.class.owner.5583.owned.25
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.class.owner.5583.err.active.slot.5639.71, ptr null)
  %except.class.replacement.owner.5610.err.owned.5642.74 = load i1, ptr %except.class.replacement.owner.5610.owned.49
  %except.class.replacement.owner.5610.err.slot.5643.75 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  %except.class.replacement.owner.5610.err.empty.5644.76 = bitcast ptr %err.empty.owner.5641.73 to ptr
  %except.class.replacement.owner.5610.err.active.slot.5645.77 = select i1 %except.class.replacement.owner.5610.err.owned.5642.74, ptr %except.class.replacement.owner.5610.err.slot.5643.75, ptr %except.class.replacement.owner.5610.err.empty.5644.76
  store i1 0, ptr %except.class.replacement.owner.5610.owned.49
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.class.replacement.owner.5610.err.active.slot.5645.77, ptr null)
  %except.handler.owner.5651.err.owned.5658.89 = load i1, ptr %except.handler.owner.5651.owned.87
  %except.handler.owner.5651.err.slot.5659.90 = bitcast ptr %except.handler.slot.5652.82 to ptr
  %except.handler.owner.5651.err.empty.5660.91 = bitcast ptr %err.empty.owner.5657.88 to ptr
  %except.handler.owner.5651.err.active.slot.5661.92 = select i1 %except.handler.owner.5651.err.owned.5658.89, ptr %except.handler.owner.5651.err.slot.5659.90, ptr %except.handler.owner.5651.err.empty.5660.91
  store i1 0, ptr %except.handler.owner.5651.owned.87
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.handler.owner.5651.err.active.slot.5661.92, ptr null)
  br label %err.finish

err.finish:
  %gc.frame.leave.ptr.5634.66 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5634.66)
  %gc.frame.leave.ptr.5640.72 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5640.72)
  %gc.frame.leave.ptr.5646.78 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5646.78)
  %gc.frame.leave.ptr.5655.85 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5655.85)
  call void (i64) @py_recursion_leave(i64 %activation.entered.129)
  ret i1 0

except.propagate.5648:
  %except.unmatched.exception.5649.80 = call ptr () @py_current_exception()
  br label %err.exit

except.handler.error.5666:
  %gc.ptr.5692.113 = bitcast ptr %handled.context.5663.94 to ptr
  %handled.context.popped.5693.114 = call i64 (ptr) @py_handled_context_pop(ptr %gc.ptr.5692.113)
  %.115 = icmp ne i64 %handled.context.popped.5693.114, 0
  br i1 %.115, label %handled.context.clear.5694, label %handled.context.done.5695

handled.context.clear.5669:
  %gc.ptr.5671.100 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5671.100, ptr null)
  br label %handled.context.done.5670

handled.context.done.5670:
  %except.class.owner.5583.owned.load.5672.101 = load i1, ptr %except.class.owner.5583.owned.25
  br i1 %except.class.owner.5583.owned.load.5672.101, label %except.class.owner.5583.owned.release.5673, label %except.class.owner.5583.owned.cont.5674

except.class.owner.5583.owned.release.5673:
  store i1 0, ptr %except.class.owner.5583.owned.25
  %except.class.owner.5583.release.gc.slot.5675.102 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.class.owner.5583.release.gc.slot.5675.102, ptr null)
  br label %except.class.owner.5583.owned.cont.5674

except.class.owner.5583.owned.cont.5674:
  %except.class.replacement.owner.5610.owned.load.5676.103 = load i1, ptr %except.class.replacement.owner.5610.owned.49
  br i1 %except.class.replacement.owner.5610.owned.load.5676.103, label %except.class.replacement.owner.5610.owned.release.5677, label %except.class.replacement.owner.5610.owned.cont.5678

except.class.replacement.owner.5610.owned.release.5677:
  store i1 0, ptr %except.class.replacement.owner.5610.owned.49
  %except.class.replacement.owner.5610.release.gc.slot.5679.104 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.class.replacement.owner.5610.release.gc.slot.5679.104, ptr null)
  br label %except.class.replacement.owner.5610.owned.cont.5678

except.class.replacement.owner.5610.owned.cont.5678:
  %except.handler.owner.5651.owned.load.5680.105 = load i1, ptr %except.handler.owner.5651.owned.87
  br i1 %except.handler.owner.5651.owned.load.5680.105, label %except.handler.owner.5651.owned.release.5681, label %except.handler.owner.5651.owned.cont.5682

except.handler.owner.5651.owned.release.5681:
  store i1 0, ptr %except.handler.owner.5651.owned.87
  %except.handler.owner.5651.release.gc.slot.5683.106 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.handler.owner.5651.release.gc.slot.5683.106, ptr null)
  br label %except.handler.owner.5651.owned.cont.5682

except.handler.owner.5651.owned.cont.5682:
  %except.match.owner.5572.owned.load.5684.107 = load i1, ptr %except.match.owner.5572.owned.15
  br i1 %except.match.owner.5572.owned.load.5684.107, label %except.match.owner.5572.owned.release.5685, label %except.match.owner.5572.owned.cont.5686

except.match.owner.5572.owned.release.5685:
  store i1 0, ptr %except.match.owner.5572.owned.15
  %except.match.owner.5572.release.gc.slot.5687.108 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.match.owner.5572.release.gc.slot.5687.108, ptr null)
  br label %except.match.owner.5572.owned.cont.5686

except.match.owner.5572.owned.cont.5686:
  %gc.frame.leave.ptr.5688.109 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5688.109)
  %gc.frame.leave.ptr.5689.110 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5689.110)
  %gc.frame.leave.ptr.5690.111 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5690.111)
  %gc.frame.leave.ptr.5691.112 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5691.112)
  call void (i64) @py_recursion_leave(i64 %activation.entered.129)
  ret i1 0

handled.context.clear.5694:
  %gc.ptr.5696.116 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %gc.ptr.5696.116, ptr null)
  br label %handled.context.done.5695

handled.context.done.5695:
  br label %err.exit

except.class.owner.5583.owned.release.5698:
  store i1 0, ptr %except.class.owner.5583.owned.25
  %except.class.owner.5583.release.gc.slot.5700.118 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.class.owner.5583.release.gc.slot.5700.118, ptr null)
  br label %except.class.owner.5583.owned.cont.5699

except.class.owner.5583.owned.cont.5699:
  %except.class.replacement.owner.5610.owned.load.5701.119 = load i1, ptr %except.class.replacement.owner.5610.owned.49
  br i1 %except.class.replacement.owner.5610.owned.load.5701.119, label %except.class.replacement.owner.5610.owned.release.5702, label %except.class.replacement.owner.5610.owned.cont.5703

except.class.replacement.owner.5610.owned.release.5702:
  store i1 0, ptr %except.class.replacement.owner.5610.owned.49
  %except.class.replacement.owner.5610.release.gc.slot.5704.120 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.class.replacement.owner.5610.release.gc.slot.5704.120, ptr null)
  br label %except.class.replacement.owner.5610.owned.cont.5703

except.class.replacement.owner.5610.owned.cont.5703:
  %except.handler.owner.5651.owned.load.5705.121 = load i1, ptr %except.handler.owner.5651.owned.87
  br i1 %except.handler.owner.5651.owned.load.5705.121, label %except.handler.owner.5651.owned.release.5706, label %except.handler.owner.5651.owned.cont.5707

except.handler.owner.5651.owned.release.5706:
  store i1 0, ptr %except.handler.owner.5651.owned.87
  %except.handler.owner.5651.release.gc.slot.5708.122 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.handler.owner.5651.release.gc.slot.5708.122, ptr null)
  br label %except.handler.owner.5651.owned.cont.5707

except.handler.owner.5651.owned.cont.5707:
  %except.match.owner.5572.owned.load.5709.123 = load i1, ptr %except.match.owner.5572.owned.15
  br i1 %except.match.owner.5572.owned.load.5709.123, label %except.match.owner.5572.owned.release.5710, label %except.match.owner.5572.owned.cont.5711

except.match.owner.5572.owned.release.5710:
  store i1 0, ptr %except.match.owner.5572.owned.15
  %except.match.owner.5572.release.gc.slot.5712.124 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr, ptr) @pcc_gc_store_root(ptr %except.match.owner.5572.release.gc.slot.5712.124, ptr null)
  br label %except.match.owner.5572.owned.cont.5711

except.match.owner.5572.owned.cont.5711:
  %gc.frame.leave.ptr.5713.125 = bitcast ptr %except.handler.slot.5652.82 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5713.125)
  %gc.frame.leave.ptr.5714.126 = bitcast ptr %except.class.replacement.slot.5611.45 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5714.126)
  %gc.frame.leave.ptr.5715.127 = bitcast ptr %except.class.slot.5584.21 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5715.127)
  %gc.frame.leave.ptr.5716.128 = bitcast ptr %except.match.slot.5573.11 to ptr
  call void (ptr) @pcc_gc_frame_leave(ptr %gc.frame.leave.ptr.5716.128)
  call void (i64) @py_recursion_leave(i64 %activation.entered.129)
  ret i1 0

activation.failure.5718:
  call void (i64) @py_recursion_leave(i64 %activation.entered.129)
  ret i1 0
