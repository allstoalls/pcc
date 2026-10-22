# 第 14 章 no-libpython 与 zero-libc：让运行时成为 pcc-Python

no-libpython 只排除 CPython 运行时；zero-libc 进一步排除 C 标准库和动态装载闭包；“生产运行时由 pcc-Python 撰写”则规定实现的所有权。三者相关，但不是同一句话。本章以 2026 年 8 月的源码与证据为准，说明 pcc 为什么不再把一个永久的手写 C 内核当作终点，而是把分配器、线程、安全点、五个 GC、平台包装、C-API 入口和 libc-like 基础设施迁入严格的 freestanding pcc-Python 子集。编译器只保留原始内存、原子、系统调用和 ABI 等机器内建；C 与 vendored libc 源码退为差分 oracle。Linux 的目标是没有生产 C/libc 对象、没有 `PT_INTERP`/`DT_NEEDED`、没有未定义符号的受支持静态闭包；Darwin 仍通过具名 libSystem ABI 进入操作系统，因此不得标成 zero-libc。

## 14.1 问题与设计空间：四个不能互换的声明

一个原生 Python 工件可能同时摆脱字节码解释器，却仍链接 libpython；可能不链接 libpython，却依赖 libc、系统动态装载器和一组手写 C 运行时对象；也可能在一个小 tracer 上做到静态闭包，却尚未把完整对象模型、扩展 ABI 和五 GC 都带进来。若只使用“独立二进制”一个词，这些边界会被抹平。

本书采用四级声明：

| 声明 | 精确含义 | 不能推出 |
|---|---|---|
| no-libpython | 工件不链接、不加载 CPython 运行时，严格模式没有 `py_cpy_*` 逃逸 | 不代表没有 libc 或手写 C |
| pcc-Python-owned runtime | 生产归档成员来自 `pcc/runtime/py/*.py` 经 pcc 编译的对象 | 不代表最终可执行文件没有平台动态依赖 |
| Linux zero-libc tracer | 指定 Linux x86_64 tracer 静态链接，无解释器、动态依赖和未定义符号 | 不代表完整 Python 运行时已经 zero-libc |
| Linux production zero-libc | 受支持的完整静态闭包没有生产 C/libc 对象、`PT_INTERP`、`DT_NEEDED` 或未定义符号 | 不代表 Darwin 也有同一边界 |

这四级声明形成一条证据阶梯，而不是四个别名：

```text
strict lowering
     |
     v
no py_cpy_* / no libpython
     |
     v
pcc-Python-owned runtime archive
     |
     +---- Darwin: named libSystem ABI (not zero-libc)
     |
     +---- Linux tracer: raw syscalls + static ELF (proven slice)
                    |
                    v
          full production zero-libc closure (final gate open)
```

备选设计是停在一个小型永久 C 内核：分配、线程、GC 和 ABI 全由 C 实现，pcc-Python 只拥有容器和 dunder 语义。这比迁移低层设施容易，但它不能满足“执行所有权”的强版本。编译器仍依赖另一种实现语言才能生产自身的运行时，五 GC 的生产策略还会在 C 与 Python 之间留下双重所有者，Linux 的零 libc 声明也会在归档边界之前失败。因此当前的方向不是“最小化 C 内核”，而是**消除生产 C 实现，只保留编译器拥有的机器内建与具名 OS ABI**。

## 14.2 新的运行时分层：实现层而不是文件扩展名

当前契约把运行时分为四层，每层的动词不同：

```text
semantic pcc-Python
  list / dict / str / dunder / exception / import / C-API semantics
                          |
                          v
freestanding pcc-Python
  allocator / threads / safepoints / GC / libc-like substrate / ABI shims
                          |
                          v
compiler intrinsics
  raw memory / atomics / syscall / host ABI / machine operations
                          |
                          v
OS boundary
  Linux raw syscalls             Darwin named libSystem entries

C and vendored-libc sources: differential oracle only; not production input
```

“freestanding”不是“用 Python 语法重写 C”这么简单。这里的模块本身正在实现堆、错误、线程或 GC，因而不能反过来依赖普通 Python 对象、装箱、异常分配或收集器。`__pcc_freestanding__ = True` 让构建和验证器识别这个闭包；`pcc.unsafe` 提供原始指针、固定宽度加载/存储、原子和系统调用；`pcc.extern` 的导出装饰器给产物稳定的 C ABI 名称。

`pcc/runtime/py/freestanding_mem_str.py` 中的 `memcpy` 展示了这个子集的形状。它没有创建 `bytes`，也不调用 host `memcpy`：

```python
# pcc/runtime/py/freestanding_mem_str.py
@c_abi_export("memcpy")
def pcc_memcpy(dst, src, size: int) -> c_ptr:
    i: int = 0
    while i < size:
        store_i8(dst, i, load_i8(src, i))
        i = i + 1
    return dst
```

编译器内建的边界必须按知识划分，而不是按“难不难用 Python 写”划分。`page_alloc` 可以是内建，因为它表达页映射这一机器操作；分配器的 size class、free list、统计与锁策略属于运行时政策，所以由 `freestanding_allocator.py` 拥有。类似地，原始 `syscall` 编码可以在后端，`open`/`read`/`write` 的跨平台语义和 errno 发布规则则在 freestanding 模块。这个分界防止把新的语义捷径不断塞进编译器，形成另一个不可审计的运行时。

## 14.3 生产归档：Python 源如何成为底层对象

生产目标 `libpy_runtime_pcc_py.a` 的输入是两组由 Python 源生成的对象：语义 `PY_MODULES` 和严格 `FREESTANDING_PY_MODULES`。当前 Makefile 的组装规则只归档 `PCC_PY_OBJECTS`，并为每个成员保留 provenance receipt；C 规则仍存在，是为了 host-C oracle、差分测试或其他明确模式，不是这份生产归档的成员来源。

```makefile
# pcc/runtime/Makefile
$(LIB_PCC_PY): $(PCC_PY_OBJECTS) $(PCC_PY_RECEIPTS)
	@set -eu; \
	rm -f "$@.tmp"; \
	rm -f "$@.capi_syms.nm.tmp"; \
	rm -f "$@.capi_syms.tmp"; \
	rm -f "$@.provenance.json.tmp"; \
	$(AR) rcs $@.tmp $(PCC_PY_OBJECTS); \
	$(RANLIB) "$@.tmp"; \
```

这段配方比“有同名 `.py` 文件”更强。源码所有权、对象成员和 provenance 必须闭合；把 `py_capi_shim.o` 改名成 `py_capi_compat.o` 不能使 C 对象变成 Python 产物。归档来源测试按每个成员是否能映射到 `pcc/runtime/py/<stem>.py` 判断，而不是维护一张容易被重命名绕过的黑名单。

五 GC 是这个迁移最重要的检验。当前生产收集器策略已经分拆为 `freestanding_gc_*` 模块：根注册、帧注册、对象槽访问、标记环、增量/并发调度、分代晋升、转发表和 ZPage 生命周期各有明确所有者。即使哈希索引这种过去被认为应永久留在 C kernel 的设施，也已迁到 `freestanding_gc_index_table.py`；其文件头明确把 `src/py_gc_index_table.c` 定位为差分 oracle。

```python
# pcc/runtime/py/freestanding_gc_index_table.py
@c_abi_export("pcc_gc_index_py_next_pow2")
def pcc_gc_index_py_next_pow2(value: int) -> int:
    if value < 8:
        return 8
    power: int = 1
    while power < value:
        power = power * 2
    return power
```

这改变了第 10 章所述的平等契约：五个后端不再消费“一份 C collector 加一份 Python 镜像”，而是消费**一个生产 pcc-Python 槽位/根契约**；C 实现只在差分测试里回答“相同输入是否产生相同行为”。完成迁移不是删除 C 文件。删除会损失独立 oracle；正确动作是从生产链接中移除，同时保留来源标注与差分入口。

## 14.4 Linux 与 Darwin：同一源码，不同机器边界

zero-libc 必须带目标平台。Linux x86_64 self 后端可以把受支持的系统操作降为原始 syscall，自己提供 `_start`，并静态链接。Darwin 则通过稳定、具名的 libSystem ABI 进入内核和平台框架；Mach-O 工件仍有动态系统边界。这不是落后版本的 Linux 路径，而是另一份平台契约。

`freestanding_platform_io.py` 让同一 pcc-Python API 在两个目标上保持一致：

```python
# pcc/runtime/py/freestanding_platform_io.py
@c_abi_export("pcc_platform_read")
def pcc_platform_read(fd: int, buffer, size: int) -> int:
    return read(fd, buffer, size)


@c_abi_export("pcc_platform_write")
def pcc_platform_write(fd: int, buffer, size: int) -> int:
    return write(fd, buffer, size)
```

这里的 `read`/`write` 是编译器识别的机器边界。Linux lowering 发 raw syscall；Darwin lowering 发具名 ABI 调用。上层文件、stdio、socket 和进程模块不应各自写平台分支，也不应在 Linux 上偷偷回落到 glibc。

Linux tracer 把这条路线贯通到进程入口。`freestanding_linux_start.py` 解码内核给 `_start` 的初始栈，写出固定消息并调用 `exit_group`；没有 C/汇编启动对象：

```python
# pcc/runtime/py/freestanding_linux_start.py
@c_abi_export("_start")
def pcc_linux_start(initial_stack: c_ptr) -> None:
    argc: int = load_i64(initial_stack, 0)
    argv0 = load_ptr(initial_stack, 8)
    status: int = 0
    if argc < 1 or ptr_is_null(argv0):
        status = 64

    message = cstr("pcc zero-libc ok\n")
    if write(1, message, 17) != 17:
        status = 74
    process_exit(status)
```

[2026-08-03 Linux zero-libc tracer 证据](../../docs/goal/evidence/2026-08-03-linux-zero-libc-python-start.md)记录的模式是 host-pcc0 Python 前端、x86_64 Linux、self 后端、no-libpython。产物是静态 ELF；`readelf -l` 无 `PT_INTERP`，`readelf -d` 无 `DT_NEEDED`，`nm -u` 为空，link map 只有从该 Python 源产生的对象。该证据有意不声称完整运行时、完整 C 前端、五 GC 或 pcc1 跨目标执行。

## 14.5 已落地的所有权与仍开放的最终声明

截至本章快照，多个有限切片已有 `DONE_STRONG` 证据：

- memory/string 的 15 个 ABI 由 `freestanding_mem_str.o` 独占，vendored musl 只留在 oracle；
- 分配器由 `freestanding_allocator.py` 拥有，Linux raw-syscall 运行、Darwin import ratchet 和五 GC 长跑均有记录；
- IO、文件系统、环境、进程、时间、socket、RSS 和 errno 包装由 freestanding pcc-Python 拥有；
- GC0–GC4 的生产 collector policy 已全部由 freestanding pcc-Python 拥有，生产 link map 没有 C collector 定义，并记录了五后端 fixed point；
- C 前端与 Python 前端可以共享 freestanding pcc-Python libc 的受支持链接路线；
- 当前 Makefile 的 pcc-Python 生产归档配方只接受 Python-born 对象和 provenance。

但任务 `LIBC-P3-FREESTANDING-RUNTIME-CLOSURE` 仍是 `TODO_READY`。原因不是源码里仍然公开声明一个永久 C kernel，而是最终接受面更大：必须冻结当前源身份，审计完整生产链接，跑默认、integration、五 GC 与 pcc1→pcc2→pcc3 闸门；Linux 工件还要同时通过 `file`/`readelf`/`nm`/link-map 证明，Darwin 要枚举 residual libSystem ABI。源树显示“所有成员来自 Python”不等于发布声明已通过。

因此本章采用以下状态标签：

```text
source ownership       present: production archive recipe is Python-only
bounded subsystems     proven: mem/str, allocator, wrappers, five GC, tracer
full Linux runtime     acceptance pending: final broad/link closure gates
Darwin zero-libc       inapplicable: named libSystem boundary is intentional
```

## 14.6 no-libpython 回退棘轮仍然必要

去掉 C/libc 并不会自动消灭 CPython 回退。`--python-libpython=off` 约束的是前端与链接闭包：任何需要 `py_cpy_*` 的 lowering 都必须在产生工件前失败。`tests/fallback_baseline.json` 和 `tests/python/test_fallback_baseline.py` 把这条边界做成单向棘轮；`fallback_routes.py` 与 `fallback_explainer.py` 给每次回退稳定的阶段、理由和建议。

这两个维度必须分别测量：

```text
frontend closure:  Python source -> no py_cpy_* -> no libpython
runtime closure:   runtime archive -> pcc-Python owners -> OS boundary
```

前者绿而后者红，得到的是 no-libpython 但仍依赖 C/libc 的产物；后者绿而前者红，得到的是自己写的运行时却仍需要 CPython 桥。只有两条链都闭合，Linux 的完整 zero-libc/no-libpython 声明才成立。

## 14.7 历史与教训

### 14.7.1 `PCC_RUNTIME_CC=cc` 的假信心（2026-05-30）

早期 no-libpython 工作同时维护 C 实现与 pcc-Python 端口。九个 idiom slice 在回归和自举里看似全绿，默认模式的 `bin(5)` 却链接失败。调查发现四个切片只修改了 C 文件，测试又固定 `PCC_RUNTIME_CC=cc`；默认生产模式链接的是 Python 端口，因而 C 修复要么不可见，要么保留旧的错误行为。自举也没覆盖 `bin` 和集合对称差，所以“自举绿”没有证明这些路径。

当时留下的规则是“双写镜像”。当前迁移把规则推进了一步：生产只有一个 pcc-Python 所有者，C 是独立 oracle。这样避免了“默认到底链接哪份语义”的歧义，但没有免除差分测试。教训没有变：绿色结果只证明它运行的模式，oracle 不能冒充产品路径。

### 14.7.2 从 `py_capi_shim.o` 改名为 `py_capi_compat.o` 的假关闭（2026-08-08）

最终 no-C ratchet 曾只断言归档里没有名为 `py_capi_shim.o` 的成员。对象改名为 `py_capi_compat.o` 后，断言变绿，但生产仍含手写 C；更糟的是允许符号表从原记录漂到 19 个全局符号。若把新增符号加入 allowlist，任务会在不改变实现所有权的情况下“完成”。

调查拒绝了这条路，把判断改成来源所有权：每个生产成员必须对应 `pcc/runtime/py/<stem>.py`。随后 C-API 家族被拆入 `py_capi_*_runtime.py`，当前生产归档配方不再加入 compat 对象。留下的不变式是：**终点测试应验证所需性质，而不是某个历史文件名。** 对 zero-libc 而言，同理不能只查字符串 `libc.so`；还要审计解释器段、动态依赖、未定义符号和完整 link map。

## 14.8 小结

pcc 的运行时方向已经从“永久保留一个最小 C kernel”变成“生产运行时由 pcc-Python 撰写，机器边界由编译器内建表达”。语义层和 freestanding 层都增长；C 与 vendored libc 保留为差分 oracle，却退出生产依赖。no-libpython、Python-owned runtime 和 zero-libc 是三条不同的声明轴。Linux 的 raw-syscall `_start` tracer 已证明最窄的静态闭包，memory/string、分配器、平台包装和五 GC 已有有限所有权证据；完整 Linux 运行时的最终 link/gate 验收仍开放。Darwin 的正确表述始终是“具名 libSystem ABI”，不是 zero-libc。

## 练习

1. 阅读 [pcc/runtime/Makefile](../../pcc/runtime/Makefile)，沿 `PCC_PY_OBJECTS`、`PY_MODULES`、`FREESTANDING_PY_MODULES` 和 `LIB_PCC_PY` 画出归档成员来源图。说明为什么保留的 `src/*.c` 规则不等于这些对象进入生产归档。
2. 阅读 [freestanding_linux_start.py](../../pcc/runtime/py/freestanding_linux_start.py) 与 [Linux tracer 证据](../../docs/goal/evidence/2026-08-03-linux-zero-libc-python-start.md)，为“完整运行时 zero-libc”补出 tracer 尚未覆盖的检查清单。
3. 比较 [freestanding_allocator.py](../../pcc/runtime/py/freestanding_allocator.py) 与 `pcc.unsafe.page_alloc/page_free` 的职责。证明 size-class 政策应属于 freestanding 运行时，而页映射应属于机器内建。
4. 设计一个无法被对象改名绕过的 archive provenance ratchet；要求同时检查来源、成员顺序、C-API inventory 和发布原子性。
5. 为 Darwin 写一条模式标注的发布声明，枚举它允许的 libSystem 边界，并解释为什么把它称作 zero-libc 会损害后续 Linux 验收。
