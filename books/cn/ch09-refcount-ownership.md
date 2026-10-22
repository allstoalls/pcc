# 第 9 章 引用计数与所有权

解释器执行 Python 时,引用计数由一个中心化的求值循环代管:每条字节码知道自己取走了什么、归还了什么。pcc 把 Python 编译成原生代码之后,这个中心消失了——每一次 incref、每一次 decref、每一次槽位覆写,都必须由前端在编译期逐条发射成 IR。发射依据不可能是运行期观察,只能是一份编译期可判定的契约:哪个表达式产生拥有引用(owned reference),哪个名字只是借用(borrowed reference),引用在函数边界上向哪个方向转移。本章讲这份契约的三层实现:运行时基元(`py_incref`/`py_decref`/`pcc_gc_store_ptr`)、前端的 owned-local 低层化(lowering)、以及函数返回路径上的被调方 retain 规则;并以两次真实的自举回归收尾。GC 算法本身——循环回收、追踪、分代、重定位——留给第 10、11 章;本章只讨论引用计数语义与所有权契约,以及它们为什么不允许为了过闸门(gate)而被弱化。

## 本章导读:引用的所有权归属

引用计数章节不用从 `incref`/`decref` 细节背起。先把每个值都看成一张借条:谁拿到 owned reference,谁就负责在合适的位置释放;borrowed reference 只是临时看一眼,不能当成自己拥有。

- 函数调用返回值默认要按 owned 结果处理,除非 helper 明确声明不是。
- 局部变量、参数、模块全局、字段和 singleton 经常是 borrowed 来源,返回它们时要在 callee 侧补 retain。
- 异常路径和正常路径一样重要:正常路径不泄漏,错误路径也不能提前释放或漏释放。

## 9.1 问题与设计空间

先把问题说清楚:pcc 生成的原生代码里,一个 `PyObject*` 从函数 A 流到函数 B,再被存进列表 C。谁负责在最后一次使用之后释放它?三类候选答案都在设计空间里出现过:

**备选一:不要引用计数,全部交给追踪式 GC。** Go 风格:生成代码只管分配和写屏障,回收全靠追踪。pcc 拒绝这个方案,原因有二。第一,五 GC 平等契约(见第 10 章)要求同一份生成代码在 `PCC_GC_BACKEND=0..4` 全部后端上正确运行,而后端 #0(refcount+cycle,默认且是回滚参照)的根基就是精确的引用计数——生成代码必须发射计数操作,这是不可协商的下限。第二,pcc 的义务之一是长跑效率(长时间运行下的 RSS、停顿、碎片),引用计数提供确定性的即时回收与确定性的终结器时机,这在比较实验里是有价值的参照系,不是历史包袱。

**备选二:调用方借用约定。** 让函数调用返回借用引用,调用方需要长期持有时自己拷贝。这个方向在 2026-06-01 的自举回归调查中被明确否决(见 9.7):它会让构造器、容器字面量、拥有的局部变量这些天然产生新引用的返回路径发生泄漏,并且把"这个返回值要不要释放"的判断从有信息的一方(被调方知道自己返回的是什么)推给无信息的一方(调用方只看到一个指针)。CPython C-API 数十年的实践也站在另一边:函数结果是 new reference。

**备选三:纯静态所有权(Rust 路线)。** 在编译期完全证明每个引用的生命周期,运行期零计数。Python 的语义不配合:变量可以在任意控制流分支重绑定,异常可以从几乎任何调用点抛出(见第 8 章),同一个名字在控制流汇合点上可能一条边是拥有、另一条边是借用。要做纯静态证明就得改语言,而 pcc 的北极星是不弱化 Python 语义。

pcc 的答案是一个混合体:**静态分类 + 运行时旗标 + 单向的边界契约**。静态部分由 [pcc/frontends/python/codegen/ownership_lowering.py](../../pcc/frontends/python/codegen/ownership_lowering.py) 的表达式分类承担;运行时部分是每个 owned 局部变量配一个 `i1` 旗标;边界契约只有一句话,值得抄写仓库规则([AGENTS.md](../../AGENTS.md) 自举回归纪律第 5 条)的原文大意:

> 函数调用返回拥有引用;被调方返回借用的 local、参数、模块全局、字段或单例时,必须在**被调方** retain,而不是让调用方停止 release 拥有的结果。

这句话是本章的主轴。它的两个方向性选择——"返回即拥有"和"修被调方不修调用方"——各有一节展开(9.5)。先把契约的全貌画出来,后面各节逐块落实:

```text
调用方一侧
  r = f(x)                      收到拥有引用;存储自由,用毕 release
被调方一侧(return_lowering.py)
  return <owned 表达式>          天然拥有,原样交出
  return <owned local>           所有权转移:不 retain、清理时 skip
  return <param/global/借用名>   先 pcc_gc_retain 提升,再交出
槽位一侧(py_obj.c)
  obj.f = v / lst[i] = v         pcc_gc_store_ptr 平衡写入:
                                 incref 新值 → 覆写 → decref 旧值
                                 (不消费调用方手中的引用)
```

图里的三块各有归属:槽位写入是运行时基元(9.3),owned 表达式与 owned local 的判定是前端低层化(9.4),返回路径的三分支是边界规则(9.5)。任何一块单独看都简单;所有权 bug 几乎全部发生在两块的接缝上——某一侧假设了另一侧没有兑现的义务。

## 9.2 运行时基元:py_incref、py_decref 与对象之死

引用计数住在对象头里。[pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 定义的 `PyObjectHeader` 是每个堆对象的前缀:`int64_t refcount`(偏移 0)、`int32_t type_tag`(偏移 8)、`int32_t flags`。对象出生即被拥有:`pcc_gc_alloc()`([pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c))在分配后写入 `h->refcount = 1`,分配者就是第一个拥有者。

`py_incref()` 和 `py_decref()` 同在 `py_obj.c`,两者开头的快速路径揭示了三类不参与计数的"对象":

1. **NULL**:两个函数都直接返回,这让生成代码不必在每次释放前判空。
2. **标记小整数(tagged small int)**:`PY_IS_TAGGED_INT` 命中的值是 `int` 的值投影(见第 16 章),没有对象头,自然没有计数。
3. **不朽(immortal)单例**:`py_None`/`py_True`/`py_False` 在 [pcc/runtime/src/py_substrate.c](../../pcc/runtime/src/py_substrate.c) 里以 `.flags = PY_FLAG_IMMORTAL` 静态定义,`py_incref`/`py_decref` 检查到 `PY_FLAG_IMMORTAL` 即返回。这意味着生成代码可以对单例统一执行 retain/release 而无任何效果——契约的统一性比省掉几条指令重要。

`py_decref()` 把计数减到 0 时,执行一个固定的死亡序列:`pcc_refcount_forget()` 注销计数、`py_weakref_invalidate(o)` 失效弱引用、`pcc_gc_note_object_freeing(o)` 通知活动 GC 后端、`py_gc_untrack(o)` 摘出循环收集器名册,然后才进入类型分派的析构。这个顺序不是随意的:弱引用必须在任何析构副作用(包括用户 `__del__`)之前失效,否则终结器可以通过弱引用看到一个半死对象。

两个函数还各自内建了一道调试防线。`py_decref` 在入口调用 `pcc_debug_maybe_abort_bad_decref()`:仅当环境变量 `PCC_DEBUG_RUNTIME` 置位时启用,校验指针形状(`py_pointer_can_have_header()`——非空、非标记整数、地址不低于 0x1000、8 字节对齐、高 16 位为零)与 `type_tag` 的合法性(`py_type_tag_is_valid()`),任一不合法即打印指针与标签并 `abort()`;此外还有计数下穿断言(`refcount underflow`)。生产构建里同样的检查会让坏指针**静默通过**(防御性返回,不计数)——这是刻意的不对称:所有权 bug 的第一现场几乎总是某次多余的 decref,生产模式选择不让单次计数错误立即变成崩溃,调试模式选择把沉默的腐败变成立刻的、带现场的 abort。第 18 章的调试手册多次依赖这道防线缩短定位链。

析构本身有一个值得讲的机制:**垃圾延迟队列**。设想 `list -> list -> list -> ...` 嵌套十万层,朴素实现里 `py_dealloc_list()` 释放子元素会递归调用 `py_decref` 再进 `py_dealloc_list`,栈直接打穿。`py_decref` 用线程局部的 `pcc_trash_dealloc_depth` 计数器检测嵌套析构:深度大于 0 时,容器与用户实例类型(`pcc_trash_should_defer()`)不立即析构,而是挂入 `pcc_trash_enqueue()` 的链表;最外层析构者负责 `pcc_trash_drain()`,把递归摊平成迭代。这是 CPython "trashcan" 机制在 pcc 里的对应物。

最后是文件组织本身承载的设计决定。`py_obj.c` 只留计数与分派;类型专属析构器全部拆到 [pcc/runtime/src/py_obj_dealloc.c](../../pcc/runtime/src/py_obj_dealloc.c)。拆分注释写明了理由:计数逻辑要被 pcc-Python 端口([pcc/runtime/py/py_obj.py](../../pcc/runtime/py/py_obj.py))整体替换以推进自托管,而析构器要摸柔性数组成员和裸结构体字段,当前 pcc-Python 表面表达不了,留在 C。端口侧的 `py_obj.py` 用 `@c_abi_export("pcc_gc_store_ptr")` 等装饰器导出同名 C ABI 符号,镜像 C 实现的每一步——这是第 14 章详述的 C↔pcc-Python 镜像纪律在引用计数上的实例:同一份对象图规则,两种作者语言,不允许语义漂移。

## 9.3 平衡的槽位写入:pcc_gc_store_ptr

把引用存进另一个对象的槽位(列表元素、实例字段、字典表项)是所有权最容易出错的瞬间。pcc 把它收敛成一个函数,`py_obj.c` 中的 `pcc_gc_store_ptr()`,其核心四行:

```c
PyObject *old = *slot;
py_incref(value);
*slot = value;
py_decref(old);
```

三个设计点。

**第一,顺序。** 先 incref 新值,后 decref 旧值。如果反过来,自我存储(`x.f = x.f`,或新旧值经由别的路径是同一对象)时 decref 旧值可能把对象计数减到 0 并析构,随后 incref 的是悬垂指针。先加后减让这条路径天然安全,不需要调用方判同。

**第二,平衡。** 这个函数对调用方手里的引用是**中性**的:槽位为自己 incref 新值、为自己 decref 旧值,不消费调用方传入的引用。推论:调用方若传入一个拥有的临时对象(比如刚构造的字符串),存完之后仍然要释放自己那一份。容器存入是"复制所有权"而非"转移所有权"。读这个函数的源码而不是凭对 CPython `PyList_SET_ITEM`(窃取引用)的记忆来推断契约,本身就是仓库的一条教训——pcc 的槽位存储与 CPython 的宏不同向。

**第三,屏障挂载点。** 在四行核心之前,函数依据 `pcc_gc_backend()` 通知活动后端:`pcc_gc_note_store()` 计数、后端 #3/#4 的重定位读解析、以及 `pcc_gc_note_slot_write_barrier()`。本章只确认一件事:所有 GC 侧的写时义务都搭载在这同一个函数上,所以"直写槽位"(`obj->slot = x`)在后端 #0 上碰巧能跑、在 #3/#4 上必坏。屏障语义本身见第 10、11 章。

全局根槽位有对应的 `pcc_gc_store_root()`,在根槽位锁内做同样的平衡写入。两个函数在 `py_obj.py` 端口里有逐行镜像。

## 9.4 前端:谁拥有这个引用?

运行时基元只是算盘;打算盘的是前端。[pcc/frontends/python/codegen/ownership_lowering.py](../../pcc/frontends/python/codegen/ownership_lowering.py) 的 `OwnershipLoweringMixin` 回答编译期的核心问题:**这个表达式的求值结果,当前函数拥有吗?**

判定函数是 `_expr_returns_owned_object()`。它的分类学值得列举,因为每一条都对应运行时的一个事实:

- **调用**:用户函数调用按被调方声明的返回类型判定(`_return_type_is_owned_object()`——`None`/`bool`/`int`/`float` 是非装箱标量,不是对象);类构造(`ClassType` 或注册过的类名)恒为拥有;`list`/`dict`/`set`/`tuple`/`str`/`bytes`/`frozenset` 等内建构造恒为拥有。
- **字面量与下标**:`ListExpr`/`DictExpr`/`TupleExpr`/`StrLit`/`BytesLit`/`Subscript` 恒为拥有——容器取项的运行时 API 返回新引用。
- **二元运算**:结果类型为 `str`/`list`/`tuple`/dyn 的 `BinOp`(拼接、重复)产生新对象。
- **属性读取**:`_attr_expr_returns_owned_object()` 只对声明了对象类型的已知字段返回真——字段读取经由产生新引用的访问器。
- **反例**:[pcc/unsafe/](../../pcc/unsafe) 的裸指针内建(`_UNSAFE_RAW_POINTER_RETURNS` 集合里的 `malloc`、`cstr`、`global_addr` 等)返回的不是 `PyObject*`,永远不参与计数。

静态分类不够,还需要运行时旗标。原因是控制流:`if c: s = build()`——汇合点之后 `s` 在一条边上拥有新对象、在另一条边上根本没绑定。pcc 给每个 owned 局部配一个入口块上的 `i1` alloca(`_ensure_owned_local_flag()`,IR 名形如 `name.owned`),赋值存入拥有值时置 1,释放后清 0。释放点(`_emit_release_owned_local_if_flagged()`)发射条件分支:旗标为真才经 `pcc_gc_load_ptr` 读出当前值并 `pcc_gc_release`。静态集合(`_owned_local_names`)决定**哪些名字**参与管理,运行时旗标决定**此刻是否真的持有**。

围绕这对机制是一组生命周期规则:

- **重绑定**:[pcc/frontends/python/codegen/assignment_statement_lowering.py](../../pcc/frontends/python/codegen/assignment_statement_lowering.py) 的赋值路径先对旧值执行 `_emit_release_owned_local_if_flagged()`,再存新值、再置旗标。漏掉第一步就是每次循环迭代泄漏一个对象。
- **作用域退出**:`_emit_owned_local_cleanup()` 在每个 `return` 之前释放所有带值旗标的 owned 局部,然后按注册的**逆序**(`_gc_rooted_local_order`)注销 GC 帧根。它接受 `skip_name` 参数——9.5 解释为什么。
- **GC 根注册**:每个 owned 局部同时经 `_ensure_owned_local_gc_root()` 注册为帧根(`pcc_gc_frame_enter`/`pcc_gc_frame_leave`),让追踪式后端能看见栈上引用。根的机制属于第 10 章;本章只指出所有权与根注册在同一处低层化中耦合,这个耦合在 9.7 的第二个案例研究里会咬人。
- **表达式临时的释放**:owned 不只属于具名局部。`a.b.c` 求值产生中间接收者临时、二元运算产生操作数临时,这些拥有的中间值用毕即应释放。统一入口是 `_gc_release_if_owned()`:它先经 `_raw_scaffold_object_rhs_is_owned()` 与 `_expr_returns_owned_object()` 双重确认来源表达式确实产生拥有引用、再排除 CPython 桥接标记值(`_cpy_values`),才发射 release。调用点散布在 `attr_load_lowering.py`、`expr_dispatch_lowering.py`、`exact_int_lowering.py`、`assignment_statement_lowering.py` 等十余处低层化位点。每次 release 都携带上下文标签(`_release_expr_label()` 编入函数名、表达式类型与源位置),调试构建里 `pcc_debug_check_release` 用它回答"是哪一行的哪一次 release 打穿了计数"。
- **丢弃赋值**:`_ = expr` 由 `_maybe_emit_discard_assignment()` 特殊处理——求值、立即释放拥有结果、把 `_` 从 env 与各 hint 表中除名,不留下任何可悬垂的绑定。
- **诚实的保守主义**:元组解包目标经 `_unpack_target_value_is_owned()` 判定,`DynType` 的解包结果**不**按拥有对象管理——动态解包位点可能携带指针形状的原生值(自举编译器里的 dataclass/AST 字符串字段),错按对象释放就是段错误。同理,带 C-ABI 导出的 raw-int-scaffold 模块(运行时端口自己)按"手工管理引用"处理,前端只追踪它能高置信识别的对象生成表达式(`_raw_scaffold_object_rhs_is_owned()`)。分类器宁可漏管(泄漏,可测量)不可错管(双重释放,崩溃)。

## 9.5 返回路径:被调方 retain

现在到契约的边界条款。规则的前半句——**函数调用返回拥有引用**——确定了调用方的行为:它可以存储结果、可以在用完后释放,不需要知道被调方内部发生了什么。这半句一旦固定,义务就全落在被调方:**无论返回表达式是什么,跨出函数边界的那个值必须是拥有的。**

大多数返回天然满足:构造器、字面量、owned 局部,本来就拥有。问题出在借用:

```python
def common_type(a, b):
    if ...:
        return a        # 参数——借用
    return TYPE_DYN     # 模块全局——借用
```

参数是调用方借给被调方的;模块全局归模块所有。直接返回它们,调用方按契约视之为拥有并最终释放,就释放了一个自己从未拥有的引用——计数下穿,双重释放。

修复在 [pcc/frontends/python/codegen/return_lowering.py](../../pcc/frontends/python/codegen/return_lowering.py)。`_return_value_needs_retain()` 判定返回值在被调方是否为借用:返回表达式是 `Name` 且命中 `_current_param_names`(参数)、`_module_globals`(模块全局)或 env 中非 owned 的局部,即为借用;`_expr_returns_owned_object()` 为真则不是。判定为借用时,`_retain_borrowed_return_value()` 发射一次 `pcc_gc_retain`(IR 名 `ret.retain`),把借用提升为拥有再交出去。`pcc_gc_retain()` 在运行时侧就是 `py_incref` 加返回原指针——单例与标记整数经过它是无害的空操作,这正是 9.2 统一契约的回报:[AGENTS.md](../../AGENTS.md) 第 5 条里"字段或单例"也在借用之列,而生成代码不需要为它们特判。

返回 owned 局部走的是另一条对称路径:**所有权转移**。`_emit_return()` 把返回名作为 `skip_name` 传给 `_emit_owned_local_cleanup()`——清理释放其余 owned 局部,唯独跳过正在返回的那个;它不被 retain 也不被 release,引用原样移交调用方。一次转移,计数净变化为零。

`_emit_return()` 把这些步骤排成固定次序:求值返回表达式 → 类型 coerce → **retain(借用提升)** → `_emit_pending_finally_blocks()`(逐层发射挂起的 `finally` 体)→ **owned-local 清理(带 skip_name)** → `ret`。次序里有两个不显眼的正确性决定。retain 在 finally 之前:`finally` 体可以重绑定甚至释放局部变量,但已提升为拥有的返回值不再依赖任何局部绑定,不受影响。清理在 finally 之后:`finally` 体本身还可能读写 owned 局部,提前释放就是 use-after-free。

返回路径上还有一个细节:retain 之后、清理之前,`_enter_return_cleanup_root()` 把返回值存进一个临时 GC 根(`ret.tmp.root`),清理完成后 `_leave_return_cleanup_root()` 再读回。原因是清理本身会调用运行时函数,在可重定位的后端上对象可能在这期间移动;读回的值经由 `pcc_gc_load_ptr`,拿到的是搬迁后的地址。细节见第 11 章。

最后回到契约的后半句:**为什么修被调方,不修调用方?** 出错时表面上有两个对称的修法:被调方补 retain,或者调用方停止 release。仓库规则把后者明文禁止,理由是信息位置。被调方在编译自己的返回语句时确切知道"这是我的参数""这是模块全局";调用方看到的只是一个函数调用结果,对所有调用一视同仁。让调用方区别对待某些被调方,要么需要全程序分析,要么退化成按函数名特判——后者正是 pcc 在包支持上明文禁止的 `if package == "numpy"` 式特殊情形的所有权版本。而且"调用方停止 release"会同时作用于真正拥有的返回(构造器、字面量),把双重释放修成了泄漏。契约的不对称是刻意的:谁有信息,谁负责。

## 9.6 终结器与复活:PY_FLAG_FINALIZED

引用计数归零触发析构,而用户实例的析构可能运行 `__del__`——一段任意 Python 代码,可以把 `self` 存到任何地方。这是引用计数语义里最阴的角落:**复活(resurrection)**。

pcc 的处理在两个函数的配合里。[pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c) 的 `py_instance_dealloc()` 序列:先 `py_weakref_invalidate()`,再 `py_user_del_dispatch(o)` 运行终结器,然后检查——

```c
if (py_header(o)->refcount > 0) {
    py_gc_track(o);
    return;
}
```

终结器若把 `self` 存进了某个活结构,`pcc_gc_store_ptr` 的平衡写入已经把计数从 0 加了回去。析构器看到非零计数就放弃释放,把对象重新挂回循环收集器名册,正常退出。对象合法复活。

防线在 [pcc/runtime/src/py_dunder.c](../../pcc/runtime/src/py_dunder.c) 的 `py_user_del_dispatch()` 里:

```c
if ((h->flags & PY_FLAG_FINALIZED) != 0) {
    return;                          /* 已终结,跳过 */
}
...
h->flags |= PY_FLAG_FINALIZED;       /* 先置位 */
meth(o);                             /* 再调用 __del__ */
```

`PY_FLAG_FINALIZED`([pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h),位 0x4)保证 `__del__` 至多运行一次——对应 CPython PEP 442 的语义。置位在调用**之前**:如果 `__del__` 内部的操作再次把计数推过生死线引发重入析构,重入方查旗标即返回,不会出现终结器套终结器。复活的对象将来第二次死亡时,同一旗标让它直接走释放路径。函数查找 `__del__` 时还顺手把结果缓存进 `PyClassObject` 的 `del_method` 槽(该槽位于类对象 120 字节布局的偏移 96,见第 7 章),后续实例析构免于重复的方法解析。

一处如实记录的不完整:`py_user_del_dispatch()` 在终结器返回后调用 `py_clear_exception()`,源码注释承认这是对 CPython "unraisable exception" 通道的占位——异常被吞掉以保证 TLS 异常状态不污染调用方(见第 8 章),但警告报告通道还是后续的诊断任务。这是开放问题,不是设计。

循环中的死亡(对象在引用环里,计数永不归零)由收集器处理:后端 #0 的 `py_gc_maybe_finalize_unreachable()`([pcc/runtime/src/py_obj_gc.c](../../pcc/runtime/src/py_obj_gc.c))与追踪后端的清扫前终结器阶段都调用同一个 `py_user_del_dispatch()`,同一个旗标保证跨路径的"至多一次"。值得注意的是旗标在收集器侧还多承担了一个角色:`py_gc_maybe_finalize_unreachable()` 比较调用前后的 `PY_FLAG_FINALIZED` 位来判断本轮是否**真的新运行了**终结器——只要有,收集器就必须重算可达性,因为任意 `__del__` 都可能改写对象图。多阶段清扫如何与终结器交错,见第 10 章。

## 9.7 历史与教训

### 案例研究一:借用返回打穿自举(2026-06-01)

(来源:[docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md](../../docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md))

长期全部通过的三阶段自举闸门(`--backend self --python-libpython=off`)突然双重失败。第一道边界是严格模式下的 libpython 回退,与所有权无关,修掉后暴露第二道:pcc0 产出的 pcc1 在编译 [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) 生成 pcc2 时崩溃,LLDB 回溯落在生成代码 `user_pcc_frontends_python_type_infer__infer_expr` 经 `pcc_gc_release` → `py_decref` → `pcc_gc_free_object_memory` 的双重释放,现场紧挨 `replace(expr, ..., ty=ty)`。

错误假设排了一排。元组自动 GC 追踪被怀疑过——**用户明确否决**了禁用它的提案,因为那是用弱化运行时语义换闸门变绿,恰是仓库规则禁止的方向。`replace(...)` 的 dataclass 字段拷贝被怀疑过——源码检查否决:字段写入走 `pcc_gc_store_ptr`,平衡无误;`replace` 只是欠拥有的对象**变得可见**的地方,不是所有权丢失的地方。"把用户函数调用结果当借用"的方案也被否决——理由即 9.5 的方向性论证。

真正的证据链:被调方 `common_type(...)` 的生成 IR 直接返回 `%a`、`%b`(参数)和 `TYPE_STR`、`TYPE_DYN`(模块全局),没有任何 retain;调用方按契约把结果当拥有的释放。根因是返回低层化从未实现契约的被调方义务。修复即现在的 `_return_value_needs_retain()`/`_retain_borrowed_return_value()`(9.5),最小回归测试 [tests/python/test_return_ownership.py](../../tests/python/test_return_ownership.py) 锁定"返回借用参数必须在被调方发射 `pcc_gc_retain`",修后单后端全自举闸门通过。

留下的不变式有两层。机制层:被调方 retain 规则进入 `return_lowering.py` 并有专属回归测试。流程层:这次调查被写进 [AGENTS.md](../../AGENTS.md) 自举回归纪律——所有权失败先验证调用方/被调方契约再动清理代码,且永远不许靠调用方停止 release 来"修"。

### 案例研究二:跨函数泄漏的 owned 旗标缓存

(来源:[docs/investigations/python-generator-owned-flag-cache-leaks-across-sibling-generators.md](../../docs/investigations/python-generator-owned-flag-cache-leaks-across-sibling-generators.md))

症状离所有权很远:self 后端在编译真实 numpy 包时,固定在第 149 个 IR 模块后拒绝——`BackendUnavailable: self backend expected pointer value 'pruned_directories.owned.33'`,位置在某个生成器恢复函数里。这个失败被此前多轮调查当作"已知的 self 后端生成器发射失败"长期挂账,从未最小化。

最小化之后真相很小:同一编译单元里两个兄弟生成器函数共享同名局部(`numpy.distutils.misc_util` 里两个函数都有 `pruned_directories`)。`_ensure_owned_local_flag()` 是按名缓存:名字已有旗标 alloca 就直接返回缓存。`user_function_lowering.py` 的 `_emit_user_function` 在**普通函数**路径上会把 `_owned_local_flag_slots` 与 `_gc_rooted_local_names` 重置为空再发射函数体,但**生成器**分支直接调用包装器发射并提前返回,跳过了重置。于是生成器 B 的恢复函数向缓存要 `pruned_directories` 的旗标,拿到的是生成器 A 函数体里的 alloca——一个在 B 中根本不存在的 SSA 值。self 后端的 `materialize_pointer` 查不到该值,**正确地**拒绝了非法 IR。

修复是两行:在生成器分支镜像普通路径已有的缓存重置。修后最小复现与 CPython 输出一致,numpy 的 149 个模块全部发射(随即撞上下一个独立的链接期 bug),强制自举闸门通过。

教训有三。其一,per-function 编译器状态的生命周期必须与函数发射严格对齐,任何提前 `return` 的分支都是泄漏点;`finally` 里恢复保存的引用救不了**就地变异**的字典。其二,后端报错是定位器不是病灶:症状在 self 后端的指针物化,根因在前端 ownership lowering 的缓存纪律。其三,长期挂账的 known-unknown 值得花一次最小化的成本——这个"已知失败"卡住 numpy 路线很多轮,最终是一个两行修复。

## 9.8 小结

pcc 的引用计数与所有权是一份三层契约。运行时层提供基元:对象出生计数为 1;`py_incref`/`py_decref` 对 NULL、标记小整数、不朽单例统一免疫;死亡序列固定(弱引用失效 → GC 通知 → 摘除 → 延迟析构);`pcc_gc_store_ptr` 以"先 incref 新值、后 decref 旧值"的平衡写入垄断所有槽位覆写。前端层做判定:`_expr_returns_owned_object()` 静态分类拥有表达式,运行时 `i1` 旗标解决控制流汇合的不可判定,重绑定先释放、作用域退出统一清理,对动态与 raw-scaffold 位点保持"宁漏勿错"的保守。边界层是一句话契约:调用返回拥有引用,借用的返回在被调方 retain,owned 局部的返回是免计数的所有权转移。终结器一侧,`PY_FLAG_FINALIZED` 先置位后调用,保证 `__del__` 至多一次,复活由析构器的计数复查合法承认。两个案例研究共同指向同一条仓库纪律:所有权失败先审契约,不弱化语义换绿灯——这份纪律本身就是契约的一部分。

## 练习

1. **读源码验证**:在 [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c) 的 `pcc_gc_store_ptr()` 中,把四行核心改写成"先 decref 旧值、后 incref 新值"的顺序,构造一个会因此产生悬垂指针的 Python 赋值语句,并解释为什么现行顺序不需要调用方做新旧判同。

2. **读源码验证**:`_expr_returns_owned_object()`([pcc/frontends/python/codegen/ownership_lowering.py](../../pcc/frontends/python/codegen/ownership_lowering.py))对返回类型为 `IntType` 的用户函数调用返回假。结合第 16 章的值投影,解释为什么"非对象"与"借用"是两个不同概念,以及把 `int` 返回值误判为拥有对象会在 `py_decref` 的哪个检查上被(侥幸)挡住。

3. **追踪契约**:[tests/python/test_return_ownership.py](../../tests/python/test_return_ownership.py) 里有 `test_returning_borrowed_parameter_retains_for_owned_call_result`。不运行测试,仅读 `return_lowering.py`,写出 `identity(xs)`(直接 `return xs`,`xs` 为参数)的返回路径会依次经过哪些函数,以及生成 IR 中应出现的 retain 调用名。

4. **设计权衡论证**:9.5 论证了"修被调方而非调用方"。构造一个反方立场:在何种全程序信息可得的前提下,调用方侧的所有权决策可以比被调方侧产生更少的 retain/release 对?这种优化在 pcc 当前的逐模块编译与五 GC 平等约束下为什么不可行?

5. **设计权衡论证**:`PY_FLAG_FINALIZED` 在 `__del__` 调用之前置位。讨论另一种设计——调用成功返回后再置位:它在终结器抛异常、终结器内重入析构、终结器复活对象三种场景下分别产生什么行为差异?哪种差异违反 PEP 442 语义?
