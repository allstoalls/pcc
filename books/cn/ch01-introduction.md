# 第 1 章 导论:拥有 Python 的执行

本书讲述 pcc——一个由 Python 写成、要把 Python 编译成原生代码、并最终用自己编译自己的编译器与运行时系统——的设计与实现。第 1 章不讲任何机制细节,只回答一个问题:这个系统为什么存在,以及它用什么纪律约束自己不变质。后面十七章的每一个设计决定,几乎都能回溯到本章的三个支点:论题(把 Python 的执行变成可拥有的)、七项义务(把论题变成可检验的守则)、声明卫生(把"做到了什么"与"没做到什么"分开说)。如果读者只读一章,应该读这一章;如果读者要质疑某个后续章节的设计,也应该先回到这一章检查它是否偏离了北极星。

## 本章导读:"拥有执行"的含义

这一章是全书的契约。读的时候先不要把它当功能清单,而是把它当成后面每一章要证明的三件事:代码能不能离开 CPython 运行,语义有没有被偷偷削弱,声明有没有标清楚模式。

- "加速"只关心结果跑得快不快;"拥有执行"还关心谁生成代码、谁管理运行时、谁承担失败边界。
- 五个分水岭告诉你 pcc 和普通 Python accelerator 的区别在哪里。
- 七项义务是后文判断设计是否越界的尺子,尤其是模式标注、fallback 诚实和自举不动点。

## 1.1 问题与设计空间:为什么"加速器"不是目标

"让 Python 更快"是一个拥挤的设计空间。已有的路线大致分三类:替换解释器并附带即时编译(PyPy);把带注解的 Python 编译成 C 扩展模块、在 CPython 进程内运行(Cython、mypyc);整程序提前编译成 C、但生成代码仍调用 CPython 的对象运行时(Nuitka)。这三类路线各自成立,也各自做出了同一个隐含选择:**执行的所有权仍然留在 CPython 运行时(或一个不透明的 JIT)手里**。产物要么是必须装进 CPython 的扩展模块,要么是一个携带自身 JIT 的解释器进程;无论哪种,用户拿到的都不是一个可独立审计、可独立部署、可被同一工具链复现的原生工件。

pcc 问的是另一个问题:**谁拥有 Python 的执行?** 仓库的设计契约([AGENTS.md](../../AGENTS.md) 的 Project Intent 一节)把论题写得很直接:pcc 存在的目的,是给 Python 一条原生(native)、可审计(auditable)、可自托管(self-hostable)、no-libpython 的执行路径。目标**不是**让选定的 Python 程序更快,而是让 Python 的执行变成"可拥有的":被编译、可检视、可自举(bootstrap)、感知包生态、可在运行时层面扩展,并且对每一条回退(fallback)边界都诚实。这串形容词里每一个都承重:"被编译"指提前编译的原生工件,而非预热完毕的 JIT 进程;"可检视"指 IR、对象布局与运行时契约可以被阅读与审计;"可自举"指工具链能复现自身——1.2 节会把它精确化为三阶段判据;"对回退边界诚实"指系统无法原生证明 Python 语义时,要带着错误码响亮地说出来,而不是静默降级。

这个表述里最重要的一句话,是 pcc 对待性能的立场:**性能是已证语义的后果,决不是弱化 Python 行为的许可证。** 这句话排除了一整族常见的捷径。一个加速器可以对热门库做特判、可以在边角语义上"差不多就行"、可以在不支持的习语上静默退回慢路径而不告诉用户——因为加速器的成功标准是基准测试数字。pcc 拒绝这条路线,不是出于洁癖,而是因为它的成功标准不同:一个声称"拥有执行"的系统,如果语义靠特判拼凑、边界靠静默回退掩盖,那么它的每一条声明都不可验证,系统本身也就失去了存在的理由。仓库里与此对应的硬规则会贯穿全书:不允许 `if package == "numpy"` 这样的包名特判(本章 1.8.2 节讲它的来源),不允许 `--backend=self` 之后静默回退 LLVM,不允许为了让某个闸门(gate)转绿而弱化终结器(finalizer)、弱引用或所有权语义。

需要同样诚实地说明的是 pcc 自己的现状。[README.md](../../README.md) 的状态表写明:C 前端是仓库中最成熟的部分,经由 Lua、SQLite、PostgreSQL `libpq`、zlib、lz4、zstd、PCRE、OpenSSL、readline、nginx 等真实项目验证;而类型化 Python 前端是**实验性的**,不支持的 Python 习语默认响亮失败(fail loudly),只有显式传入 `--python-libpython=auto/on` 才会路由到 CPython 桥。pcc 不是 Clang 或 CPython 的即插即用替代品,它是一个带着真实集成测试的研究编译器。本书后续所有章节都在这个前提下展开。

## 1.2 论题与五个分水岭

如果去掉下面五样东西,pcc 就退化成"又一个加速工具";有了它们,它才是一个在重建 Python 执行所有权的系统。[AGENTS.md](../../AGENTS.md) 把这五项列为不许腐烂成装饰品的差异化核心:

```text
1. pcc1 -> pcc2 -> pcc3 自托管不动点
2. 五 GC 对比运行时(引用计数/环、增量、并发、分代、重定位)
   ——一个研究计划,不是一个收集器
3. 可选值模型——为热路径提供无身份的不可变载荷,
   不窃取普通类的语义
4. self 后端作为第一类执行根(LLVM 是 oracle,不是 owner)
5. 长跑运行时效率(随时间推移的停顿/RSS/吞吐/碎片,
   而非单次编译+运行速度)
```

**自托管不动点(fixed point)。** 自举的阶段命名是固定的:`pcc0` 指宿主 Python 运行仓库源码;`pcc1` 是它产出的第一个原生编译器二进制;`pcc1` 编译出 `pcc2`,`pcc2` 编译出 `pcc3`:

```text
pcc0/host -> pcc1    pcc 能产出一个编译器
pcc1      -> pcc2    产出的编译器能复现这个编译器
pcc2      -> pcc3    pcc2/pcc3 稳定 == 自托管不动点
```

不动点不只是一次字节比较。`pcc2` 与 `pcc3` 字节同一,意味着 pcc 的 Python 语义、运行时、代码生成、对象模型、后端和诊断已经相干到足以复现自身——任何一层有不确定性,都会在两次自编译之间放大成差异。当前的权威状态冻结在 [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json)(2026-05-01 捕获,Issue 1 关闭证据):在 macOS arm64 上,LLVM 链与 self 后端链的全部三个阶段都不链接 `libpython`;严格路径(`--backend self --python-libpython=off --ir-scaffold=on`)下 `pcc2`/`pcc3` 发射的 IR 字节同一且含 0 个 `py_cpy_*` 调用,二进制在去除 Mach-O 签名后字节同一。注意这句声明的每个限定词都是刻意的——平台、后端、模式、比较方法——这正是 1.4 节要讲的声明卫生。自举的全部细节见第 15 章。

**五 GC 对比运行时。** 运行时装载五个 GC 后端槽位,由环境变量 `PCC_GC_BACKEND` 在进程启动时选择,枚举定义在 [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 中:

```c
// pcc/runtime/include/py_runtime.h
enum {
    PCC_GC_KIND_REFCOUNT_CYCLE = 0,
    PCC_GC_KIND_INCREMENTAL_TRICOLOR = 1,
    PCC_GC_KIND_CONCURRENT_MARK_SWEEP = 2,
    PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR = 3,
    PCC_GC_KIND_COLORED_RELOCATING = 4
};
```

每个后端镜像一个真实的参照实现——CPython、Lua 5.4、Go(greentea)、OCaml、ZGC——参照源码就放在树内 [docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research) 下,移植与原文可以对照阅读。这是一个对比研究计划:同一套对象图契约上跑五种收集器,任何一个都不许靠弱化语义取胜。后端 #0 至今仍是默认与回滚参照(决策记录在 [docs/investigations/gc-backend-selection-matrix.md](../../docs/investigations/gc-backend-selection-matrix.md))。架构与逐后端的细节见第 10、11 章。

**可选值模型。** 值类(value class)是显式选择加入的、无身份的不可变载荷,服务热路径;普通类保留全部身份语义(`id`/`is`/弱引用/`__dict__`/可变性/子类化/终结器)。pcc 从 Java 的 Valhalla 项目借的是**投影(projection)模型**——语义类型与物理表示分离——而不是 Java 的定宽 `int` 回绕语义。见第 16 章。

**self 后端作为第一类执行根。** 仓库内 [pcc/backend/](../../pcc/backend) 是一个不依赖 LLVM 的原生发射路径(当前覆盖 AArch64 Darwin 与 x86_64 Linux 的子集)。它的义务是成为执行根而非演示品:`--backend=self` 之后禁止静默回退 LLVM;LLVM 的角色是 oracle——用来对照验证——而不是 owner。见第 13 章。

**长跑效率。** pcc 关心的性能指标是长时间运行下的停顿、RSS、吞吐与碎片化,而不是单次"编译+运行"的秒表数字。这直接塑造了 GC 与运行时的测量方法(见第 10、11 章)。

## 1.3 七项义务

论题靠七项义务落地。每一项在 [goal-prompt.md](../../docs/goal/goal-prompt.md) 中都有对应的轨道(track)与闸门;这里给出本书视角的速览,机制留给对应章节。

1. **兼容性声明必须模式标注(mode-labeled)。** 一条声明必须说明它在哪个模式下成立:host pcc ≠ pcc1;cpython-compat ≠ pcc-native;libpython ≠ no-libpython;LLVM 后端 ≠ self 后端;stage1 ≠ pcc1→pcc2→pcc3 不动点。1.4 节展开。

2. **性能必须被证明。** "接近 C"级别的声明需要 IR 形状证据、运行时基准,以及一条在假设失效时保持 Python 语义的慢路径。pcc 不声称任意动态 Python 能达到 C 速度——只对语义足够稳定、可被原生低层化(lowering)的部分作此声明。这条义务也要求如实记录违例:仓库曾有一个**已确认的本义务违例**——typed-int ABI 路径里,显式标注 `int` 的未装箱 `+`/`*`/`<<` 曾在 i64 溢出时静默回绕;从确认、裁定到修复(2026-06-17)的完整档案见第 16 章。

3. **生态支持必须是通用机制。** NumPy、PyTorch、pandas、Arrow、SciPy 是集成目标,永远不是编译器特例。禁止 `if package == "numpy"`;要修的是可复用机制(安装/导入/ABI/buffer/capsule/构建表面),并为通用特性添加回归测试。见第 17 章与本章 1.8.2。

4. **self 后端必须成为第一类执行根。** 不允许永久依赖 LLVM,也不允许 `--backend=self` 之后静默回退。见第 13 章。

5. **pcc1/pcc2/pcc3 不动点是契约。** 阶段间差异必须被**分类**(语义/IR 文本/类布局/对象模型/后端非确定性/链接元数据/仅性能/诊断),而不是绕过去。pcc2/pcc3 的稳定性是核心正确性信号。见第 15 章。

6. **运行时设计是研究目标的一部分。** 五个 GC 后端是对比计划,任何一个都不得靠弱化终结器、弱引用、复活(resurrection)、挂起协程帧、调度器队列、C 扩展引用或值载荷取胜。效率按长跑属性测量。见第 10、11 章。

7. **值模型是性能桥梁,不是语法噱头。** 普通类保身份;值类是可选的无身份载荷,带显式装箱/拆箱、身份逃逸诊断、含指针载荷的 GC 追踪,以及 self 后端的聚合/标量 ABI。这条义务延伸到 `int` 本身:`int` 是 Python 的任意精度**语义**类型,带一个值投影(标记小整数通道,tagged small-int lane)和一个对象投影(装箱大数);值通道溢出必须去优化/晋升,决不回绕。裸机器整数是**显式**的 `pcc.i64`/`pcc.u64` 类型(回绕/陷阱/检查/饱和写进类型里),或经证明在范围内的内部优化——决不是 `int` 的静默缺省含义。见第 16 章。

这七条不是愿景清单,而是否决权:当一个改动要拿其中一条去换局部胜利——更快的基准、更绿的闸门、更小的 diff、靠改写失败构造而"通过"的自举——契约要求停下来,把权衡摆到明面上,而不是默默接受。七项义务也解释了本书为什么用整整一章讲工程方法(第 18 章):在一个核心声明都是可复现性声明的系统里,测试与调查装置不是辅助工具,而是设计本身的一部分。

## 1.4 诚实作为体系结构:模式标注的声明

把"诚实"写进体系结构,听起来像把道德条款塞进技术文档。但 pcc 的处境使它成为一个工程必需品:这个系统同时存在太多正交的执行模式。编译器本身可以由宿主 CPython 运行(host pcc)或以自举产物运行(pcc1);Python 输入可以走严格 no-libpython 路径或显式 CPython 桥;后端可以是 `llvm`、`llvm_capi` 或 `self`;包的接受面分 cpython-compat 与 pcc-native;自举证据分 stage1 与完整不动点。一条不带模式标注的声明("pcc 能跑 NumPy 了")在这个空间里不是信息,是噪声——它会把后续的工程决策路由到错误的方向上。

因此 [goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10 把声明卫生(claim hygiene)写成一张不等式表,每条声明必须映射到一个闸门:

```text
host pcc pass          != pcc1 pass
cpython-compat pass    != pcc-native pass
libpython mode pass    != no-libpython pass
fake package pass      != real package pass
array-core pass        != import numpy pass
stage1 self-backed     != pcc1->pcc2->pcc3 self-backed
metadata exists        != runtime implementation complete
microbenchmark win     != whole-program performance win
```

同一份文档要求两句口号必须成对出现,任何只保留第一句的文档都可能在过度声明:

```text
Write Python. Run Native.
C-like speed where Python semantics can be proven.
Exact Python semantics everywhere supported.
```

声明卫生的执行机构不是文风审查,而是机器可检验的证据层级。仓库的权威次序是:**当前的聚焦测试、自举闸门与 JSON 基线 > 一切散文**。自举状态以 [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 为准,由 [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) 强制;no-libpython 回退表面以 [tests/fallback_baseline.json](../../tests/fallback_baseline.json) 为准,构成一只只许收紧的棘轮;五个 GC 后端各有自己的完整三阶段自举闸门 `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py`。[docs/current-goal-state.md](../../docs/current-goal-state.md) 顶部的审计快照逐条记录证据,且完成度本身是分级的——大量条目标注为 `DONE_WEAK`(聚焦闸门加自举验证通过,但不声称覆盖完整边界),并显式列出"不声称"的内容:不声称完整逃逸边界覆盖、不声称完整 dataclasses 支持、不声称值模型完成。

声明卫生有时表现为**故意保留一个失败**。严格 pcc-native 模式在遇到 CPython ABI 的扩展工件时,以 `PCC-PKG-004` 拒绝加载(聚焦闸门是 [tests/python/test_package_extension_abi.py](../../tests/python/test_package_extension_abi.py)):

```python
# pcc/package/linkage.py
def _diagnostic_for_cpython_extension_abi(path: str) -> dict[str, object]:
    return {
        "code": "PCC-PKG-004",
        "message": (
            "native artifact name declares a CPython extension ABI; "
            "pcc-native mode requires a pcc-native extension ABI or a source rebuild"
        ),
        "path": path,
    }
```

[README.md](../../README.md) 明说这个阻塞是有意的:它防止一个 CPython ABI 工件被误报成 pcc-native 的 NumPy 支持。一个为演示而设计的系统会把这条路悄悄打通;一个为可验证性而设计的系统把它做成显式错误码。

## 1.5 运行时分四层:让生产实现迁入 pcc-Python

no-libpython 只表示不依赖 CPython,并不自动表示 zero-libc。pcc 的长期契约比“最小 C kernel”更强:生产运行时——包括分配、线程、安全点、五 GC、libc-like substrate 与 C-API ABI shim——迁往严格 freestanding pcc-Python;编译器只保留原始内存、原子、系统调用与 host ABI 等机器内建。C 与 vendored libc 源码保留为差分 oracle,但最终退出生产链接。

```text
compiler intrinsics   保留:raw memory / atomics / syscall / machine ABI
freestanding pcc-Py   增长:allocator / threads / safepoints / GC / ABI shims
semantic pcc-Python   增长:list / dict / str / dunder / exception / import
C/libc sources        从生产依赖移除;保留为有来源标注的 oracle
```

平台边界必须单独标注:Linux 的最终目标是没有生产 C/libc 对象、`PT_INTERP`、`DT_NEEDED` 和未定义符号的受支持静态闭包;Darwin 仍通过具名 libSystem ABI 进入操作系统,不得称作 zero-libc。五个 GC 后端消费一套由生产 pcc-Python 拥有的槽位 trace/update 与 root/frame/native-handle 契约;C 版本只参与差分。no-libpython、pcc-Python-owned runtime 与 zero-libc 的完整证据阶梯见第 14 章,槽位契约见第 10 章。

## 1.6 定位:与 PyPy、Cython、Nuitka、mypyc 的差异

把 pcc 放进既有工具的坐标系,差异不在"谁更快",而在各自优化的目标函数不同。以下描述基于各项目的公开定位,不构成贬低——这些工具在自己的目标上都比今天的 pcc 成熟得多。

- **PyPy** 是一个带追踪 JIT 的替代解释器,目标是全语言兼容。执行发生在 JIT 进程内部;它出色地回答了"如何让现有 Python 程序在不修改的情况下更快",但产物不是一个可独立审计的提前编译原生工件。
- **Cython** 把带注解的 Python 超集编译为 C 扩展模块。产物在 CPython 进程内运行、链接 libpython;执行所有权完整地留在 CPython。
- **mypyc** 把带类型注解的 Python 编译为 C 扩展模块,同样以 CPython 运行时为宿主。它与 pcc 的类型化前端在输入约束上最接近,但目标产物根本不同。
- **Nuitka** 做整程序提前编译,生成的 C 代码仍调用 CPython 的对象运行时。它消除了字节码解释,但没有(也不打算)替换运行时本身。

pcc 的坐标轴是另一条:**不是"在 CPython 里更快的 Python",而是"没有 CPython 的 Python 执行"**——no-libpython 的原生工件、自托管不动点、可替换的后端(LLVM 与 self)、五 GC 对比实验室,以及把这一切声明锁进闸门的方法论。代价同样要直说:上述每个工具今天支持的 Python 表面都远大于 pcc 的类型化前端;pcc 在其原生子集之外默认响亮失败,自举路径还要求 pcc 自己的源码避开大量动态习语(运行时 `getattr`/`setattr`、生成器的部分形态、带运行时效果的装饰器、动态导入等,见 [README.md](../../README.md) 的限制清单与第 5 章)。选 pcc 的理由不是它已经赢了,而是它在押一个别人没押的方向。

## 1.7 一个使命,不是两个

pcc 同时陈述一个工业论题("在原生工件、no-libpython 部署、包感知诊断与热路径特化胜过 CPython 的场景采用 pcc")和一个学术论题("一个 Python 写成的编译器自托管进一个 no-libpython 不动点,同时暴露一个有纪律的运行时实验室")。契约坚持这是一个使命,不是两个,因为两侧互为输入:

```text
工业失败是研究数据            研究工件是工业信任
----------------------        ----------------------
导入失败    -> C-API/ABI 缺口  不动点自举   -> 可复现性
Linux 部署失败 -> self 后端    五 GC 矩阵   -> 运行时可信度
             目标缺口
长跑服务回归 -> GC/运行时基准  值类基准     -> 性能证明
性能未达    -> 值模型缺口      包 ABI 报告  -> 生态信任
```

这个结构在 1.8.2 节的案例研究里能看得很具体:一次真实 NumPy 导入的失败,被拆解成一串通用机制缺口(警告模块、typing 标记、路径操作、正则子集……),每个缺口的修复都变成一条可复用的编译器能力,而不是一个 NumPy 补丁。从一个方向读,这是工业工作:用户想要的包离可导入更近了一步;从另一个方向读,这是研究产出:一张实测的地图,标出 no-libpython 的 Python 还缺哪些导入机制语义。两种读法谁也离不开谁——所以两个论题的连接点是同一条规则:**每条声明都必须说清它证明了什么、没证明什么。**

声明式 GUI 也按同一逻辑进入这条主线。它不是把 webview 或 JavaScript 运行时带回来,而是用 pcc-Python 拥有 composition、调度、事件、样式、command 和生命周期,只把 AppKit/CoreGraphics/Metal 留在命名明确的本地 ABI 边界。它已经有可读源码和机器契约,正式验收任务仍开放;第 20 章展开其机制与声明边界。

## 1.8 历史与教训

本书每章以真实调查([docs/investigations/](../../docs/investigations))收尾。第 1 章的两个故事都关于声明卫生本身——一个讲过度声明如何被发现并被制度化地纠正,一个讲"禁止包名特判"这条规则在真实包上如何运转。

### 1.8.1 值模型"实现到 V6"的过度声明(2026-05)

**症状。** 调查文件 [docs/investigations/python-valhalla-value-model-actual-state.md](../../docs/investigations/python-valhalla-value-model-actual-state.md)(更新日期 2026-05-19 至 2026-05-24)开篇记录:值模型的计划与状态报告声称受 Valhalla 启发的轨道已"实现到 V6"。

**错误假设。** 把脚手架当成了实现。[pcc/value_model.py](../../pcc/value_model.py) 里确实存在名为 `ValuePayload`、`ValueBox`、`SpecializedArray`、`GenericSpecialization` 的宿主侧 dataclass——名字与计划中的 V1–V6 一一对应,表观上像完成了。

**证据链。** 代码检视给出了相反的结论:真正接入类型推断与类低层化的只有 V0 切片([pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py) 定义 `ValueClassType`,[pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py) 识别 `@pcc.valueclass`);V1 没有直接的 LLVM 结构 ABI、没有证明热路径避免对象分配的 IR 形状闸门;V4 的 `pcc.array[Point]` 连续载荷运行时不存在;V5 的单态化与类型元组缓存不存在;V6 的热对象迁移与分配基准不存在。计划要求的 V1–V4 测试文件在调查开启前部分缺失。

**真正根因。** 状态表面没有区分"元数据存在"与"运行时实现完成"——这正是后来 §0.10 表中 `metadata exists != runtime implementation complete` 那一行的出处。

**修复与留下的不变式。** 修复不是删掉乐观的句子,而是把诚实做成 API:`value_model_status()` 此后报告 `implemented_through`(当时为 V1 标量载荷子集)、`scaffolding_through == "V6"`、`production_runtime is False`,并以 `not_implemented` 列表枚举缺失的运行时、代码生成、GC、特化与基准工作:

```python
# pcc/value_model.py
def value_model_status() -> dict[str, object]:
    return {
        "implemented_through": (
            "V1-direct-scalar-and-nested-payload-eq-checked-marshal-"
            "v2-pointer-and-nested-dyn-boundary-partial"
        ),
        "scaffolding_through": "V6",
        "production_runtime": False,
        "marker": "@pcc.valueclass",
    }
```

状态本身变成了可断言的测试对象。这个故事还有一个递归的注脚:第一版为 V0 添加源形状诊断的补丁,自己就把 `pcc.frontends.python.type_infer` 的 no-libpython 回退计数从基线 846 推高到 951(棘轮上限 888),被 [tests/python/test_fallback_baseline.py](../../tests/python/test_fallback_baseline.py) 当场拦下——连"为了诚实而写的诊断代码"也要过同一道棘轮。修复(把诊断构造收敛到一个 `_raise_frontend_error` 辅助函数)把计数压回 851。教训:过度声明不是品德问题,是缺少装置的问题;装置到位后,它会被测试抓住,而不是被读者抓住。

### 1.8.2 真实 NumPy 的首次导入:`if package == "numpy"` 禁令的来源(2026-05-27)

**背景与症状。** [docs/investigations/numpy-first-import-libpython-fallback.md](../../docs/investigations/numpy-first-import-libpython-fallback.md) 跟踪 B-P0-PKG 轨道上的真实边界:用新鲜自举的 pcc1,在 `PCC_HOST_PYTHON=/usr/bin/false`(宿主 Python 不可用,排除任何宿主作弊)下,编译一个 `import numpy` 程序,包源是仓库本地的真实 NumPy 2.4.4。调查开篇先修正了一次声明漂移:选择加入的闸门 `tests/python/test_package_import_path.py::test_pcc1_real_numpy_first_import_boundary_opt_in` 还在断言早已过时的旧阻塞(`NoneType` marshal),而当前真实边界是 `PCC-PY-COMPILE-001`:多文件编译仍需 libpython 回退,残余 `py_cpy_*` 发射集中在 `numpy.f2py.symbolic`、`numpy.f2py.func2subr` 等模块。第一个修复(Proposal No.1)就是让闸门重新断言**当前的失败形状**——一个通过的测试,其内容是精确记录"我们还失败在哪里"。

**错误假设与诊断纪律。** 第一次短调试追踪只给出 `py_cpy_ensure_init()` 之前的两行 IR,不足以定位;完整 IR 转储才显示第一条回退边在 `user_numpy___getattr__` 里——`numpy/__init__.py` 的 `__getattr__` 导入了 `warnings`。调查据此留下一条诊断护栏:两行的 `PCC_DEBUG_BOOTSTRAP_TRACE` 上下文只是定位器,不是根因证据;动代码之前必须从完整 IR 确认所在 `define`、实际的 `py_cpy_*` 调用与参数来源。

**方法:只修通用机制。** 接下来的每一次回退收缩都是一条通用编译器能力,且在调查中逐条标注"不含 NumPy 特定分支":`import warnings` 注册为原生内建模块别名([pcc/frontends/python/codegen/import_lowering.py](../../pcc/frontends/python/codegen/import_lowering.py),镜像既有的 [pcc/stdlib/warnings.py](../../pcc/stdlib/warnings.py) 垫片);字面量 `textwrap.dedent(...)` 常量折叠(动态字符串仍走回退,不冒充完整 textwrap 兼容);`typing.TYPE_CHECKING` 在代码生成期折叠为假、`TypeVar(..., covariant=True)` 接受元数据关键字——这一项使 `numpy._typing._nested_sequence` 的 `py_cpy_*` 调用点从 10 降到 0;`os.path.getsize`、`Path(...).suffix` 的原生低层化;直接形式 `re.match/search` 与 `re.I`/`re.S` 常量的原生子集;`re.compile(...).match/search` 低层化为返回真实 `PY_TYPE_FUNC` 对象的运行时辅助(明确写道:这是绑定方法边界,不是假的真值正则对象)。受限的 `findall` 子集只接受两个被实测命中的字面量模式,并写明:不支持的模式**保留回退路径,而不是用假的空列表顶替**。每一节证据末尾重复同一句限定:这只是回退表面收缩,不证明 `import numpy` 成功。`numpy.f2py.crackfortran` 模块的回退辅助计数随之从 1555 降到 1302、1228、1220——进度以可测数字呈现,声明以不变的边界封顶。

**真实包作为研究数据。** 这条调查同时暴露了与 NumPy 毫无关系的两个通用 bug(Proposal No.3):并行导出 worker 的浅层提升器把类头关键字(`class _DTypeDict(_DTypeDictBase, total=False)`)误送进表达式提升而崩溃;跨 worker 的导出线格式把非字面量默认值(`dtype=int`、`axis=-1`、`keepdims=np._NoValue`)序列化成"无默认值",使它们变成必填参数。两者都修在 [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) 的通用路径上,并以新鲜的完整三阶段自举验证。这正是 1.7 节的闭环:工业失败(导入一个真实包)产出研究数据(前端并行化的语义等价缺陷)。

**留下的不变式。** 为什么禁止 `if package == "numpy"`?因为这条调查展示了特判的真实代价:一个包名分支可以让闸门转绿,却不会让任何机制存在——下一个包在同一个缺口上原样失败,而状态板上已经写着"支持 NumPy"。通用机制路线慢,但每一步都是单调的:回退棘轮只收紧、闸门记录当前失败形状、证据与声明逐条对齐。[AGENTS.md](../../AGENTS.md) 的 Package/NumPy Claim Hygiene 一节把这套实践固化成规则:安装成功不是导入成功;合成的同名包不算数;cpython-compat 证据不冒充 pcc-native 证据。

## 1.9 小结

pcc 的论题是把 Python 的执行变成可拥有的:原生、可审计、可自托管、no-libpython。支撑论题的是五个分水岭——自举不动点、五 GC 对比运行时、可选值模型、作为第一类执行根的 self 后端、长跑效率——和把它们变成日常守则的七项义务。性能在这个系统里是已证语义的后果;诚实不是文档礼仪,而是用不等式表、JSON 基线、回退棘轮和闸门实现的体系结构构件。生产运行时的目标实现语言是 pcc-Python:freestanding 层拥有低层设施,semantic 层拥有 Python 语义,compiler intrinsics 只表达机器边界,C/libc 退为 oracle。与既有工具相比,pcc 押注的坐标轴是"没有 CPython 的 Python 执行",并为此接受其类型化前端今天仍是实验性子集的现实。两个案例研究展示了同一件事的两面:声明卫生被违反时如何被装置捕获,被遵守时如何把一次真实失败转化为一串可复用的能力。后续各章将把本章的每个名词展开成机制——并在各自的"历史与教训"里继续检验这套纪律。

## 练习

1. **(读源码验证)** 打开 [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h),找到 `PCC_GC_KIND_*` 枚举的五个成员,并在 [docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research) 下找到每个后端对应的参照实现目录。哪个后端是默认?在哪份调查文档里记录了这个决定?

2. **(读基线验证)** 阅读 [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json)。用模式标注的语言写出它**能**证明的三条声明(注明平台、后端、比较方法),再写出两条它**不能**证明的、表面相似的声明(例如涉及 Linux 或涉及运行时性能的)。

3. **(声明卫生)** 从 §0.10 的不等式表中任选两行,各举一个具体场景:左侧成立而右侧不成立。说明把左侧证据升级为右侧证据分别需要补什么闸门。

4. **(读调查验证)** 通读 [docs/investigations/numpy-first-import-libpython-fallback.md](../../docs/investigations/numpy-first-import-libpython-fallback.md) 中 2026-05-27 的各节,列出其中至少四个"通用机制"修复,并为每个修复指出:如果当初用 NumPy 特判实现,哪些非 NumPy 程序会失去这条能力?受限 `findall` 子集为什么拒绝用空列表顶替不支持的模式?

5. **(设计权衡论证)** 假设有人提议增加 `--fast-numpy` 旗标:检测到导入 NumPy 时启用一组 NumPy 专用的低层化捷径,换取演示性的导入成功。依据本章的七项义务逐条评估这个提案,指出它违反哪几条、以什么形式违反,以及是否存在不违反义务的等价收益路径。
