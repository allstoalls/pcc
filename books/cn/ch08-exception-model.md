# 第 8 章 异常模型

Python 的语义允许异常从几乎任何表达式涌出:下标越界、属性缺失、迭代器耗尽、用户 `__add__` 里的一次 `raise`。解释器里这件事由求值循环统一接住;编译成原生代码之后,"控制流如何从抛出点跨过任意多个原生栈帧到达匹配的 `except`"就成了必须显式回答的设计问题。pcc 的回答表观上是所有候选中最朴素的一个:`py_raise(exc)` 把异常存进一个线程局部槽位然后**正常返回**,每个可能抛异常的调用点之后由生成代码检查 `py_err_occurred()` 并分支到错误路径。没有展开器(unwinder),没有 Itanium ABI。本章解释这个选择的理由——代价、可移植性、self 后端可实现性——以及它的真实价格:把传播正确性变成分布在几十个低层化(lowering)文件里的插入义务,漏掉一处检查的症状不是崩溃,而是"compile succeeded with no output"。本章覆盖运行时五个 C 文件与五个 pcc-Python 镜像、前端的 `ExceptionLoweringMixin`,并以两次真实调查收尾。

## 本章导读:异常传播是显式检查而非栈展开

本章先记住一个模型:抛异常只是把错误对象放进线程局部槽,真正的跳转靠 generated code 在每次可能失败的调用后手动检查。少一次检查,异常就会变成沉默的错误或错位的返回。

- runtime 保存"发生了什么异常";低层化决定"当前函数下一步跳到哪里"。
- cleanup 路径必须同时处理错误传播和已拥有引用的释放。
- traceback、诊断和 no-libpython 不是装饰,它们决定失败能不能被定位。

## 8.1 问题与设计空间

把问题摆开。函数 C 抛出 `ValueError`,匹配的 `except` 在三层调用之外的函数 A 里。设计空间里有三类经典答案:

**备选一:Itanium 式零成本展开。** C++/Rust panic 的路线:编译器为每个函数发射展开表(ELF 上是 `.eh_frame`,Mach-O 上是 compact unwind),调用点用 `invoke` + landingpad 表达异常边,运行时由 libunwind 驱动两阶段搜索(先找 handler,再逐帧清理),personality 例程仲裁每一帧。"零成本"指 happy path 不付钱:不抛异常时没有检查指令。

这不是稻草人——**pcc 最初就是这么做的**。[pcc/frontends/python/codegen/unary_call_lowering.py](../../pcc/frontends/python/codegen/unary_call_lowering.py) 中 `_call_user()` 的文档字符串保留了迁移记录:"This replaces an earlier Itanium-ABI design that used `invoke` + landingpad to route exceptions via libc++abi";[pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) 的异常节注释同样写明 "No Itanium C++ ABI symbols are exported from py_exc.c anymore"。放弃它的理由有三条,分别对应本章必须回答的三个轴:

1. *代价模型与 Python 语义不匹配。* "零成本"的前提是异常罕见。Python 不是这样:每个 `for` 循环以 `StopIteration` 终止,生成器协议、迭代器协议把异常当作正常控制流。在 Itanium 模型下,每次循环结束都要分配异常对象、进入展开器、查表、两阶段搜索——为最热的路径付最贵的钱。(pcc 今天的 for 循环低层化用 NULL 返回值加 `py_exc_matches` 对 `StopIteration` 的匹配来收尾,见 8.5——迭代终止确实是异常机制的热路径。)更要命的是第 9 章的所有权契约:每个持有 owned 局部变量的栈帧在展开经过时都必须执行引用计数清理,这意味着几乎每一帧都需要 landingpad 清理代码——"零成本"在带引用计数的语言里退化为"每帧都有着陆垫的展开",两头的钱都付。
2. *可移植性。* 展开表格式因平台而异(`.eh_frame` 与 compact unwind 的差异曾让无数编译器后端流血),且把 libc++abi/libunwind 拖进链接闭包。pcc 的北极星之一是 no-libpython 的自包含产物(见第 14 章);`_call_user()` 的注释直说收益:"keeps libc++abi out of the runtime link"。checked-call 只需要普通调用、一个 `_Thread_local` 槽和条件分支——在 macOS arm64 与 Linux x86_64 上逐指令同构。
3. *self 后端可实现性。* 第 13 章的 self 后端不经 LLVM 直接发射机器码。要支持 Itanium,它必须为每个函数发射逐字节正确的 CFI/展开元数据,实现 landingpad 语义,并对接 personality 协议——这是一个庞大且极易出错的表面。在 checked-call 模型下,异常边就是普通的条件分支:[pcc/backend/](../../pcc/backend) 整个发射器族里不存在任何展开元数据的生成路径,后端根本不需要知道"异常"这个概念存在。异常模型的全部复杂度留在前端 IR 层,后端只看见 call、icmp、br。

**备选二:setjmp/longjmp。** Lua 的路线([projects/lua-5.5.0/](../../projects/lua-5.5.0) 里就能读到):受保护调用点 `setjmp`,抛出点 `longjmp` 直达。它免去展开表,但 `longjmp` 跨帧跳过时不执行任何中间帧的代码——第 9 章的 owned-local 清理、GC 帧根注销(见第 10 章)全部被跳过,引用计数直接漏穿。要补救就得给每个 try 边界做快照式资源记账,等于重新发明一个更脆的展开器。Lua 能用它是因为 Lua 的 C 栈上没有逐帧的计数义务;pcc 有。

**备选三:checked-call(返回码风格)。** [pcc/runtime/src/py_exc_tls.c](../../pcc/runtime/src/py_exc_tls.c) 文件头把策略写成两行:`py_raise(exc)` 只把 `exc` 存进线程局部槽并正常返回;调用方在每个可能抛异常的调用之后检查 `py_err_occurred()`,为真则分支到错误传播路径。注释自标来源:"return-code style, CPython-inspired"——CPython 的 C-API 正是这个模型(函数返回 NULL/-1,`PyErr_Occurred()` 查询线程状态)。

checked-call 的得失表是诚实的:

```text
                     Itanium 展开            checked-call
happy path 开销      零指令                  每个可抛调用后:一次 TLS 读
                                            + 一次比较 + 一次分支
raise 开销           分配 + 两阶段查表展开    一次 TLS 写 + 普通返回
refcount 清理        每帧 landingpad         错误路径与正常路径共用
                                            清理代码(第 9 章)
链接依赖             libunwind/libc++abi     无
self 后端义务        CFI/展开表/personality   无(异常边 = 普通分支)
可调试性             展开器内部状态           挂起异常是可检查的内存状态
                                            (lldb 在 py_raise 下断点)
正确性风险           表错一字节 = 未定义行为   漏一处检查 = 静默错误(8.6)
```

最后一行是本章的暗线。checked-call 把传播正确性从"运行时统一机制"变成"前端在每个发射位点的插入义务"——`_emit_post_call_err_check()` 在 [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) 下有约 80 个调用位点,散布在二十多个低层化文件里(2026-06 统计)。义务是分布式的,没有中心化的强制器;漏检查不报错、不崩溃,只让异常"瞬移"到错误的上下文。8.6 与 8.7 用真实事故展示这个价格,以及仓库为此建立的审计与闸门(gate)。

## 8.2 TLS 槽与 py_raise:模型的全部运行时状态

整个异常模型的运行时状态只有一个字:[pcc/runtime/src/py_substrate.c](../../pcc/runtime/src/py_substrate.c) 里的 `static _Thread_local void *g_tls_current_exc`,经 `py_tls_exc_get()`/`py_tls_exc_set()` 两个裸访问器存取。槽位放在 substrate 而不是 `py_exc_tls.c` 自己,是镜像纪律的要求(见第 14 章):pcc-Python 端口 [pcc/runtime/py/py_exc_tls.py](../../pcc/runtime/py/py_exc_tls.py) 要整体替换 `py_exc_tls.o`,但它无法声明 C 的线程局部存储,于是存储留在"永远是 C 的底座"substrate 里,端口经 `pcc.extern.extern` 引用同一对访问器。逻辑可替换,状态不动。

在 [pcc/runtime/src/py_exc_tls.c](../../pcc/runtime/src/py_exc_tls.c) 中,抛出与查询仅需几行 C 代码:

```c
// pcc/runtime/src/py_exc_tls.c
void py_raise(PyObject *exc) {
    PyObject *cur = py_resolve_current_exception();
    if (cur != NULL && exc != NULL && cur != exc && py_type_of(exc) == PY_TYPE_EXC) {
        PyExceptionObject *new_exc = (PyExceptionObject *)exc;
        PyObject *existing_context = pcc_gc_load_ptr(exc, &new_exc->context);
        if (existing_context == NULL) {
            pcc_gc_store_ptr(exc, &new_exc->context, cur);
        }
    }
    if (exc != NULL && !exc_owned) py_incref(exc);
    if (cur != NULL) py_decref(cur);
    py_tls_exc_set(exc);
}

int64_t py_err_occurred(void) {
    return py_tls_exc_get() != NULL ? 1 : 0;
}
```

而在前端代码生成侧,[pcc/frontends/python/codegen/exception_lowering.py](../../pcc/frontends/python/codegen/exception_lowering.py) 负责在每次函数调用后发射逻辑检查:

```python
# pcc/frontends/python/codegen/exception_lowering.py
def _emit_post_call_err_check(self) -> None:
    cur_fn = self.current_function
    if cur_fn is not None and cur_fn.name in self._c_abi_export_symbols:
        return
    err_target = self._current_try_err_block()
    if err_target is None:
        err_target = self._ensure_fn_err_exit()
    err_fn = self.runtime.get("py_err_occurred")
    if err_fn is None:
        err_ty = ir.FunctionType(_I64, [])
        err_fn = ir.Function(self.module, err_ty, name="py_err_occurred")
        err_fn.linkage = "external"
        self.runtime["py_err_occurred"] = err_fn
    is_err = self.builder.call(err_fn, [])
```

`py_exc_tls.c` 在这个槽上实现四个公开入口,合计 172 行:

**`py_raise(exc)`** 做四件事。第一,规范化:静态函数 `py_raise_normalize()` 把任意被 raise 的对象收敛为可挂起的异常——`NULL` 变成 `RuntimeError("no active exception to reraise")`(裸 `raise` 而无挂起异常,与 CPython 同);`PY_TYPE_EXC` 原样保留;**用户异常子类的实例原样保留**——函数内注释记录了一段历史:早期实现把实例包装成只携带消息字符串的新 `PY_TYPE_EXC`,静默丢弃 `__init__` 设置的全部实例属性(`self.code` 之类),修复后实例保持原貌、匹配端负责投影(见 8.3);既非异常对象又非 `BaseException` 实例的,变成 `TypeError("exceptions must derive from BaseException")`。第二,重定位读:旧值经 `pcc_gc_note_relocation_read()` 解析,后端 #4 移动对象后槽里可能是旧地址(见第 11 章)。第三,隐式链接:若 TLS 已有挂起异常 `cur`,且新异常是 `PY_TYPE_EXC` 且其 `context` 槽为空,则 `pcc_gc_store_ptr()` 把 `cur` 存为新异常的 `__context__`——这是 CPython "During handling of the above exception, another exception occurred" 链的对应物。第四,所有权交接:借用的入参 incref、旧值 decref、写槽、**正常返回**。函数最后一行注释就是契约本身:"Caller is responsible for propagation via a post-call py_err_occurred() check."

**`py_err_occurred()`** 是一行:槽非空返回 1。返回类型刻意选 `int64_t`——[pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 的声明注释写明,这样 pcc-Python 端口里 `def py_err_occurred() -> int` 的默认 `int` 低层化与 C ABI 自然对齐,运行时 ABI 表(`runtime_abi.py`)与端口发射的函数签名一致。

**`py_current_exception()`** 返回借用引用(TLS 仍拥有);**`py_clear_exception()`** decref 并清空。四个入口在 `py_exc_tls.py` 里有逐行镜像,实例类判定、偏移(`context` 在偏移 40)、类型标签全部以字面量内联——端口注释解释了原因:pcc-Python 的模块级整型常量在剥离了自动 main() 的库 `.o` 构建里会归零,所以常量必须写在使用点(第 14 章详述这条构建事实)。

两个跨子系统的连接点值得点名。其一,TLS 槽是 GC 根:[pcc/runtime/src/py_gc_backend.c](../../pcc/runtime/src/py_gc_backend.c) 的 `pcc_gc_promote_tls_exception_root()` 在分代收集时晋升槽中对象并改写槽位——挂起异常可能在两次安全点之间存活任意久,不登记为根就会被追踪式后端清扫(根契约见第 10 章)。其二,可观测性内建:`py_raise`/`py_clear_exception` 等入口经 `pcc_diagnostics_runtime_log_event_code()`([pcc/runtime/src/pcc_diagnostics_runtime_log.c](../../pcc/runtime/src/pcc_diagnostics_runtime_log.c))发射 "exception" 类别事件,`PCC_LOG` 环境变量即可开启——8.6 那类"无输出"故障的第一道照明。

## 8.3 异常对象与匹配:一个标签、一张类表、一次 MRO 行走

[pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) 定义的 `PyExceptionObject` 是异常的载体:

```text
偏移  0   PyObjectHeader   (refcount@0, type_tag@8, flags — 16 字节,见第 7 章)
偏移 16   exc_class        具体类(拥有引用;稳态非空)
偏移 24   message          args[0] —— PyStrObject* 或 py_None(拥有)
偏移 32   cause            raise X from Y 的 Y(拥有;NULL = 无显式 cause)
偏移 40   context          隐式链接捕获的前一个异常(拥有)
偏移 48   traceback        PyFrameRecord 可增长数组(malloc 所有)
偏移 56   n_frames / 60 cap_frames (i32 各一)         —— 共 64 字节
```

设计决定:**所有内建异常共用一个类型标签 `PY_TYPE_EXC`,身份由 `exc_class` 字段区分**,而不是每类异常一个 C 类型。`py_internal.h` 的注释给出理由:isinstance 行走保持统一,前端发射的 handler 测试只需读 `exc_class` 做类匹配,handler 体内的属性访问走普通 getattr。指针槽全部经 `pcc_gc_store_ptr()` 写入(屏障纪律,见第 9、10 章);[pcc/runtime/src/py_obj_gc.c](../../pcc/runtime/src/py_obj_gc.c) 的 `py_obj_visit_slots()` 对 `PY_TYPE_EXC` 访问 `message`/`cause`/`context` 三个易成环的边——异常经由 `__context__` 互指成环是真实场景(handler 里再抛),循环收集器必须看得见这些边。

对象一侧在 [pcc/runtime/src/py_exc_objects.c](../../pcc/runtime/src/py_exc_objects.c):`py_exc_alloc()` 分配并清零、装配类与消息;`py_exc_new()` 按内建标签取类;`py_exc_new_with_class()` 按用户类构造;`py_exc_new_with_value()` 让 `message` 槽携带任意对象(`StopIteration(value)` 类用途);`py_dealloc_exc()` 释放四个引用槽并 `free` traceback 数组。

类一侧分两层。数据层在 `py_substrate.c`:`PY_EXC_BUILTIN_NAMES`(19 个名字)、`PY_EXC_PARENT`(父标签表,如 `PY_EXC_KEYERROR` 与 `PY_EXC_INDEXERROR` 的父亲都是 `PY_EXC_LOOKUPERROR`,`PY_EXC_ZERODIVISIONERROR` 挂在 `PY_EXC_ARITHMETICERROR` 下)与逐标签缓存 `py_exc_classes`。逻辑层在 [pcc/runtime/src/py_exc_table.c](../../pcc/runtime/src/py_exc_table.c):`py_exc_builtin_class(tag)` 缓存命中即返回,否则递归构造父类、`py_class_new()` 建类、打上 `PY_FLAG_IMMORTAL`、入缓存。数据与逻辑分层同样是镜像纪律:`py_exc_table.py` 端口替换逻辑层时,经 `pcc.unsafe.global_addr` 直读 substrate 里的同一份表与缓存——表如果留在被替换的 `.o` 里,端口就无表可读(`py_exc_table.c` 文件头注释原文如此)。

匹配在 [pcc/runtime/src/py_exc_match.c](../../pcc/runtime/src/py_exc_match.c),53 行,每个 `except` 测试都要经过,是异常机制的热路径。`exc_to_class()` 把任意对象投影为 `PyClassObject*`:类对象原样;`PY_TYPE_EXC` 读 `exc_class`;`PY_TYPE_INSTANCE` 及用户标签(`>= PY_TYPE_USER`)读实例的 `cls` 槽——这一支正是 8.2 那条"实例原样保留"修复的另一半:被 raise 的用户异常实例在匹配时投影到类,MRO 里含 `Exception` 基类,`except MyError` 与 `except Exception` 都能命中。`py_exc_matches()` 对投影后的两个类走 `ecls->mro` 数组找指针相等;无 MRO 时退化为同一性比较。没有分配,没有字符串比较,逐槽经 `pcc_gc_load_ptr()` 读(后端 #3/#4 的读侧义务,见第 10 章)。

诚实地记录一处语义粗化。前端的名字表(`exception_lowering.py` 的 `_BUILTIN_EXC_TAG`)把约五十个 Python 异常名映射到这 19 个标签:`FileNotFoundError`、`PermissionError`、`TimeoutError` 等全部映射到 `PY_EXC_OSERROR`,`UnicodeError` 一族映射到 `PY_EXC_VALUEERROR`,`ImportError` 映射到 `PY_EXC_EXCEPTION`。raise 端与 except 端走同一张表,于是 `except FileNotFoundError` 实际编译为对 OSError 类的匹配——**比 CPython 捕得更宽**。同理,`_emit_exception_class_ref()` 对解析不到的异常名与 `except json.JSONDecodeError` 这类属性形式回退(fallback)到泛化的 `Exception` 类,函数文档字符串自己写明 "catches strictly more than requested"——这是为自托管编译覆盖率付的代价,按开放问题记账,不是被掩盖的缺陷。

## 8.4 traceback:传播途中搭建的回溯

没有展开器,就没有"在抛出时刻向上扫栈"的能力——回溯必须**在传播途中增量搭建**。这是 checked-call 模型一个不显眼的推论:栈信息不是被发现的,是被沿途记录的。

机制在 [pcc/runtime/src/py_exc_traceback.c](../../pcc/runtime/src/py_exc_traceback.c)。`PyFrameRecord` 三个字段:`func_name`、`filename`(都是借用指针,指向前端发射的静态 rodata,异常对象从不释放它们——`py_internal.h` 注释明示这条借用语义)加 `line`。`py_exc_append_frame()` 把记录追加进 `PyExceptionObject` 的倍增数组(初始 8,`realloc` 失败时静默丢帧——异常处理路径上的 OOM 不允许再抛异常)。

谁在调用它?前端。`_emit_raise()` 在 `py_raise` 之后立即对当前异常追加抛出点的帧;`_emit_post_call_err_check()` 检测到挂起异常时,先跳进一个按 `(函数, 错误目标, 调用点)` 记忆化的 `err.frame` 块(`_ensure_post_call_frame_block()`),追加**调用点**所在函数的帧,再跳错误目标。异常逐层向外传播,数组逐层加帧:抛出点先入,外层调用点后入。`py_exc_print_unhandled()` 按数组下标顺序打印,因此最内层帧最先出现——从源码可直接推得,这与 CPython "most recent call last"(最内层最后)的视觉顺序相反,尽管标题沿用了 CPython 的同一句文案。按开放的呈现差异记录。

`py_exc_print_unhandled()` 的其余部分忠实复刻 CPython 的链式输出:`cause` 优先于 `context`,递归地最旧者先印,连接句逐字对应("The above exception was the direct cause of the following exception:" / "During handling of the above exception, another exception occurred:");对 NULL、标记小整数、字符串等非异常对象有防御性分支。pcc-Python 镜像 `py_exc_traceback.py` 用 `pcc.unsafe.write` 替代变参 `fprintf` 逐字节复刻同一份 stderr 文本,连 `_write_i64()` 的手写十进制都不依赖运行时格式化——镜像必须在运行时自身故障时仍可工作。

打印的触发点在前端:`_ensure_fn_err_exit()` 为 `main` 生成的错误尾声(epilogue)依次调用 `py_current_exception()`、`py_exc_print_unhandled()`、`py_clear_exception()`,然后 `ret 1`。**未处理异常的进程级语义——打印回溯、退出非零——不是运行时的内置行为,而是 main 的错误出口被显式低层化成这样。**这一点在 8.6 是关键证词。

## 8.5 前端低层化:raise、try/except 与检查的插入义务

运行时只提供算子;把 Python 的 `raise`/`try` 语义拼出来的是 [pcc/frontends/python/codegen/exception_lowering.py](../../pcc/frontends/python/codegen/exception_lowering.py) 的 `ExceptionLoweringMixin`(mixin 架构见第 6 章)。

**raise。** `_emit_raise()` 的形状:构造异常值 → 可选 `py_exc_set_cause()`(`raise X from Y`)→ `py_raise` → 追加抛出点帧 → 无条件分支到当前错误目标。错误目标由一个编译期栈决定:`_push_try_err_block()`/`_restore_try_err_block()` 维护 `_try_err_block`,在 try 体内是该 try 的 `try.err` 块,否则是 `_ensure_fn_err_exit()` 的函数错误尾声。`_build_exception_value()` 里有一段值得引述的历史:用户异常类的 `raise MyError(a, b)` 现在走真正的实例化(`class_lowering.emit_instantiate`),让用户 `__init__` 执行;注释记录旧路径 `py_exc_new_with_class(cls, args[0])` 跳过 `__init__`、只留 args[0] 当消息,丢属性还会把非消息的首参误当消息——与 8.2 的规范化修复是同一类 bug 的两端。裸 `raise` 调 `py_current_exception()`,并以 `_active_handler_excs` 栈的栈顶做 select 兜底——因为 handler 入口清空了 TLS(见下)。

**try/except。** `_emit_try()` 生成的控制流骨架:

```text
       body(err 目标 = try.err)
      /                        \
 正常出口: else → finally → done   try.err: cur = py_current_exception()
                                    ├─ except.test.0: py_exc_matches(cur, C0)?
                                    │     是 → except.body.0
                                    │     否 → except.test.1 ... 
                                    └─ 全不中 → except.propagate
                                          → 外层 try.err 或 err.exit
```

元组形式的 handler(`except (A, B):`)把多次 `py_exc_matches` 结果 or 起来;无类型的 `except:` 恒真。handler 入口的次序承载语义:先按需 retain 异常(handler 绑定了名字,或体内含裸 `raise`——`body_has_bare_raise()` 递归扫描判定),再 `py_clear_exception()` 清空 TLS,再把名字绑进 `e.addr` 槽。清空让 handler 体内的后续调用不被旧异常污染;retain 让绑定与重抛在 TLS 放手后仍有拥有引用可用。绑定名同时登记进 `_except_binding_names`,使后续 `saved = e` 的拷贝被 GC 根化——这条根缺失曾让追踪后端清扫掉异常的 message,根因在前端所有权低层化而非运行时,是第 10 章的案例研究(见 [docs/investigations/](../../docs/investigations) 的 gc-5backend-exception-referent-roots 调查)。`finally` 经 `_finally_stack` 在正常出口、无 handler 的错误路径、handler 出口三处展开;与 `return` 的交互(挂起 finally 的逐层发射)在第 9 章的返回路径里已经出现过。

从源码推得的一处语义边界,如实记录:`py_raise` 的隐式 `__context__` 链接以"TLS 仍有挂起异常"为条件,而低层化代码在 handler 入口已清空 TLS——因此 handler 体内一次普通的 `raise NewError()` 不会自动携带刚捕获的旧异常作为 `__context__`;链接生效的是异常仍挂起时再次 raise 的路径(传播途中、运行时内部的二次抛出)。CPython 在 handler 体内也会链接。这是当前实现与 CPython 的一个已知差距,按开放问题记录。

**插入义务。** 模型的重心落在 `_emit_post_call_err_check()`:任何可能抛异常的运行时调用之后,发射 `py_err_occurred()` 读取、与 0 比较、条件分支到错误目标(带源位置时先经 8.4 的 `err.frame` 块)。用户函数调用由 `_call_user()` 统一收口——调用、可选的返回值 GC 根化、然后必然的 err-check;但运行时 helper 的调用散布在各低层化文件里,每个发射位点都要自己记得插检查:2026-06 的统计是约 80 处调用,跨 25 个文件,`method_call_expression_lowering.py` 12 处、`native_text_modules.py` 7 处、`binary_op_lowering.py` 7 处……这就是 8.1 结尾说的"分布式义务"。

两处刻意的不对称完善了这个模型。其一,**抑制规则**:`@c_abi_export` 标记的运行时端口函数内部不发射 post-call 检查(`_emit_post_call_err_check()` 开头按 `_c_abi_export_symbols` 早退,`user_function_decl_lowering.py` 在声明期把这类符号登记进该集合)——异常与回溯 helper 本来就运行在"TLS 持有挂起异常"的环境里(handler 派发中、打印回溯中),在那里检查会把环境异常误判为"helper 自己抛了";运行时函数沿用 cc-C 惯例,以 NULL 返回值显式传播。其二,**等价路由**:不是所有传播都长成 post-call 检查;for 循环、推导式等对 `py_obj_next` 的消费用"NULL 返回值 → `py_exc_matches` 对 `StopIteration` → 终止或传播"的 maybe_end/propagate 形状路由,语义等价——这个形状在 8.7 的审计里成为区分真假阳性的签名。

`_ensure_fn_err_exit()` 补上最后一块:每函数惰性创建 `err.exit` 尾声,按返回类型发射哨兵——指针返回 NULL,整型返回 0(`main` 特殊:打印未处理异常并返回 1),void 直接返回;并为已根化的 owned 局部补发根注销(`_patch_fn_err_exit_gc_root_leave()`,与第 9、10 章的清理路径汇合)。注意这个哨兵:**整型错误路径的返回值是 0**。它是合法值域内的值——这一设计与漏检查叠加,正是下一节事故的形状。

另有一处编译期特判值得一提:`_maybe_emit_optional_missing_import_try()` 把"`try: import X / except ImportError: X = None`"的惯用形状识别为可选导入,直接在编译期把名字别名为缺失标记,根本不发射运行时异常路径——try 低层化不全是运行时机制,也包含对惯用法的静态识别。

## 8.6 失败模式:为什么漏一次检查 = "compile succeeded with no output"

现在回答本章第二个必答题。把 8.2 与 8.5 的两半拼起来,漏检查的故障链是机械的:

```text
1. 运行时函数内部 py_raise(exc)  → TLS 置位,函数正常返回哨兵(NULL/0)
2. 发射位点漏插 _emit_post_call_err_check
   → 生成代码拿着哨兵继续直行,仿佛调用成功
3. 哨兵是合法值:NULL 可存进槽位,0 可参与算术、可当退出码
   → 不立刻崩溃;TLS 里的异常继续挂着
4a. 后面某个"无辜"调用点恰好有检查 → 异常在错误的上下文引爆:
    跳过了本应匹配的 try/except,被更外层、语义不相干的 handler 捕获
4b. 或者再也没有检查 → 异常一路沉默;函数逐层返回哨兵 0
    → main 正常返回 0 → 进程"成功"退出,无输出文件,stderr 干净
```

4b 就是 [AGENTS.md](../../AGENTS.md) 里那句"missing the check turns into 'compile succeeded with no output'"的全部内容。[docs/investigations/python-self-host-no-libpython-runtime-holes.md](../../docs/investigations/python-self-host-no-libpython-runtime-holes.md) 记录了它在自举(bootstrap)主路径上的真实出场:no-libpython 的 pcc1 编译一个两行的 Python 文件,内部抛了编译期异常,异常传播到某个函数的错误尾声、按 8.5 的规则返回整型哨兵 0,`bootstrap_cli_sys_argv_exit()` 看到退出码 0——进程成功退出,没有产物,没有任何文字。调查原文的判词:"The runtime had the exception; the CLI did not print it or turn it into a nonzero exit."

这个失败模式昂贵在两点。第一,它**反转了调试信号**:崩溃有现场,错误信息有文本,而这里什么都没有——必须用 lldb 在 `py_raise` 下断点才能看见异常存在过(这条手法已固化进调查的推荐工作流与调试手册,见第 18 章)。第二,它**混淆了故障类别**。同一调查区分了三类必须分开报告的错误:*compile error*(用户输入不合法,pcc 应打印并非零退出)、*compiler execution error*(pcc1 自己在编译时内部抛了异常——应打印、非零退出、标注为内部错误)、*target execution error*(pcc 产出的二进制运行失败,归目标程序)。漏检查把第二类整个折叠进"静默成功",而第二类恰恰是自举调试里最需要看见的一类。

系统性的回应有三层,分别落在不同章:运行时层,8.4 的 main 错误尾声让传播到顶的异常必然打印并以 1 退出;不变式层,[AGENTS.md](../../AGENTS.md) 把"可抛调用后检查 `py_err_occurred()`"列为 Python 侧三大失败成因排查的固定一项(与类布局漂移、GC 屏障缺失并列,见第 7 章的三因检查顺序);审计层——见下一节。

## 8.7 历史与教训

### 案例研究一:链接干净、--help 正常、编译"成功"——静默失败的 pcc1(2026-04)

来源:[docs/investigations/python-self-host-no-libpython-runtime-holes.md](../../docs/investigations/python-self-host-no-libpython-runtime-holes.md)(状态快照 2026-04-29;Issue 1 此后于 2026-05-01 关闭,基线在 [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json))。

**症状。** Issue 1 的目标:自举二进制不链接 libpython。形式上的验收都过了——`--python-libpython off` 构建成功,`otool -L` 里没有 python,`pcc1 --help` 退出 0。然后让 pcc1 编译 `def main(): return 0`:退出码 0,没有输出文件,stderr 空白。

**错误假设。** "CPython 回退计数归零 + 链接干净 = 能工作的编译器。"调查的核心教训是这条推理的断裂:`py_cpy_*` 计数是必要非充分——它度量的是对 CPython 的依赖面,不度量 pcc 运行时承载编译器自身执行的正确性。"模块在 CPython 下导入成功"也不是"模块初始化在 pcc1 里执行正确"的证据。

**证据链。** 静默失败把常规手段全部废掉,只能上 lldb 在 `py_raise` 下断点:运行时确实抛了异常,而 CLI 既没打印也没把它变成非零退出——挂起异常被整型哨兵 0 吞掉,这正是 8.6 的 4b 路径。把异常浮出水面之后,后续故障逐个具象化,调查里留存了完整的 bt 指纹:`_emit_tuple_literal` 里 `ir.Constant(_I64, i)` 把原生整数经 `inttoptr` 误作对象句柄传入 `scaffold_Constant_obj`,`py_instance_set_field` 对伪指针 incref 当场崩溃——从"无输出"到"有栈、有 IR、可归因"的转变,全部依赖先让异常可见。

**留下的不变式。** 其一,错误传播闸门:编译中抛异常必须 CLI 非零、stderr 有文本、不留产物——"status 0 且无产物"应当是不可能状态。其二,测试金字塔补层:stage1-as-compiler 冒烟(构建 pcc1 → 验链接 → 跑 --help → 编译最小文件并执行产物)成为强制闸门,今天固化在 `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py`(见第 15 章)。其三,方法论一条:对这类故障,第一动作是 lldb 断 `py_raise`,不是猜。

### 案例研究二:emission-site err-check 审计——把分布式义务变成可枚举清单(2026-06-11)

来源:[docs/investigations/emission-site-err-check-audit.md](../../docs/investigations/emission-site-err-check-audit.md)。

**症状。** 一晚之内三个独立 bug 同属一类:运行时函数 `py_raise` 置位后,前端发射位点没插 `_emit_post_call_err_check`,异常跳过应匹配的 try/except、在后面某处引爆——native `weakref.ref`/`weakref.proxy`、weak-dict 下标存储、cpy 解包参数数检查,三处同形。探针形状统一:期望打印 `typeerror`,实际打印 `ok` 加一条迟到的回溯。

**方法。** 不再逐个撞,做面上的枚举:扫运行时 C 源,体内含 `py_raise(` 的函数 78 个;再扫 [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) 下所有 `self.runtime["<fn>"]` 发射点,后 8 行内无 err-check 的标记为嫌疑——58 处(2026-06-11)。关键纪律写在审计文件的 Review rules 里:**启发式产出的是候选,不是判决**;每个位点要么用最小红探针证实错误捕获行为,要么验明等价路由后记为假阳性;一族一片(slice),不打大补丁。

**裁决结果(逐族)。** `py_obj_next` 族 5 处:全部假阳性——maybe_end/propagate 等价路由在 8 行窗口之外,审计据此把"`py_exc_matches` 对 StopIteration + 该块名对"登记为"已审通过"的签名。re-engine 族 3 处:假阳性——检查确实存在,只是多行实参列表把它推出了窗口(启发式改进:扫到语句边界而非定长行数)。二元运算族:**真阳性 ×2 且复合**——`py_obj_add/sub/mul` 既缺发射端检查,又缺用户 dunder 派发(实例直落 "unsupported operand" TypeError);修复一并补上 C-only 的 `py_user_binop_dispatch()`(`py_protocol.c`,`__op__` → NotImplemented → 反射 `__rop__` → TypeError 的完整协议)与发射端检查,后又延伸出 `%`、`//`、`/` 反射与增强赋值 `__iadd__` 协议的同方修复。生成器 `send/throw/close` 族:真阳性——补检查后**又暴露一个被掩盖的运行时洞**:`py_gen_close()` 注入的 `GeneratorExit` 在生成器体不捕获时留在 TLS 里挂起,而 CPython 语义里这正是 close 的正常路径、应被吞掉;修复用与注入对象的指针同一性(`cur == exc`)做判别——`GeneratorExit` 没有专属标签(它是 `PY_EXC_BASE` 加消息),同一性是唯一精确的判据。

**留下的教训。** 第一,checked-call 的弱点可以被工程化补偿:分布式义务无法中心强制,但可以周期性枚举审计,且审计签名(等价路由形状)要沉淀给下一次。第二,漏检查会**掩盖它身后的运行时洞**——gen.close 的 GeneratorExit 处理缺陷只有在检查补上之后才可见;修一类 bug 经常是揭开第二类的盖子,两个故障要两条证据链。第三,每个真阳性修复都以"红探针 → 修复 → 双层(端口/C)验证 → 回归测试 → 五 GC 矩阵"收尾(见第 10 章的平等契约)——异常路由是对象图行为,五个后端都要作证。

## 8.8 小结

pcc 的异常模型是一次方向明确的取舍:用每个调用点一次 TLS 读加分支的恒定开销,换掉展开表、personality、libc++abi 的整个世界。换来的三样东西都对得上 pcc 的北极星——可移植(no-libpython 产物不携带 C++ 运行时)、self 后端零义务(异常边即普通分支,[pcc/backend/](../../pcc/backend) 不含一行展开代码)、可调试(挂起异常是一块可断点、可打印的内存)。与 Python 的语义脾性也对得上:`StopIteration` 级别的高频异常付不起两阶段展开,而第 9 章的逐帧引用计数清理本来就让"零成本"名不副实。

价格同样明确:传播正确性 = 分布在约 80 个发射位点的插入义务,漏一处的症状是异常瞬移或者静默成功——"compile succeeded with no output"。仓库对这个价格的回应是分层的:`main` 错误尾声兜底打印、[AGENTS.md](../../AGENTS.md) 三因排查不变式、stage1-as-compiler 闸门、以及把分布式义务变成可枚举清单的发射位点审计。运行时一侧,五个 C 文件五百余行,逐文件配 pcc-Python 镜像,TLS 存储与类表沉在 substrate 让逻辑可替换、状态不动——异常子系统同时也是镜像纪律(见第 14 章)与五 GC 平等契约(见第 10 章)的样板间。

## 练习

1. **读源码验证。** 读 `py_exc_tls.c` 的 `py_raise_normalize()` 与 `py_exc_match.c` 的 `exc_to_class()`:解释为什么被 raise 的用户异常子类**实例**必须原样保留而非包装为新的 `PY_TYPE_EXC`,两个函数各承担修复的哪一半?如果只改了前者,`except MyError` 会发生什么?
2. **追踪一次低层化。** 对 `try: f() except (A, B) as e: g()`,按 `_emit_try()` 列出生成代码依次调用的运行时函数(从 `py_current_exception` 到 handler 出口),并指出:在哪个点 TLS 被清空?`e` 的引用在哪里 retain、哪里 release?为什么 `e` 要进 `_except_binding_names`?
3. **构造故障。** 设运行时函数 `h()` 内部 `py_raise` 一个 `TypeError` 后返回 NULL,而它的发射位点漏插检查。沿 8.6 的故障链写出两种终局(4a 错位引爆 / 4b 静默成功)各自需要的后续代码形状,并解释为什么 `_ensure_fn_err_exit()` 选 0 当整型哨兵让 4b 成为可能。哨兵改成 -1 能根治吗?会破坏什么?
4. **设计权衡论证。** 假设 self 后端日后支持了 CFI 元数据发射,是否应当迁回 Itanium 展开?分别从 `StopIteration` 频率、第 9 章 owned-local 清理在展开路径上的去向、Mach-O 与 ELF 的展开表差异、以及"漏检查"故障类在两种模型下的等价物(表项错误)四个角度论证。
5. **改进审计。** 8.7 案例研究二的 8 行窗口启发式同时产出假阳性(检查在窗口外)与潜在假阴性。设计一个更好的静态审计:如何识别 maybe_end/propagate 等价路由?如何处理 `@c_abi_export` 抑制规则?它仍然证明不了什么(提示:可达性、运行时函数是否真能在该路径上 raise)?
