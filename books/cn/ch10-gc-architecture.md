# 第 10 章 五 GC 架构与平等契约

一个运行时带五个垃圾收集后端,这个决定本身需要辩护:五个收集器意味着五倍的正确性面,而 GC bug 的典型形态是难以复现的内存损坏。本章解释 pcc 为什么接受这个代价,以及用什么架构把代价控制住——一套收集器选择 ABI、一套对象图槽位规则、一套根集注册接口、一对读写屏障(write/read barrier),以及一条把五个后端钉在同一 Python 语义上的"生产平等规则"。各后端内部的算法(三色推进、并发标记、分代晋升、着色重定位)留给第 11 章;本章只讲它们共用的骨架,和这副骨架为什么决不允许出现第二套。

## 本章导读:GC 的四个基本问题

不要从"三色""分代""重定位"这些算法名开始读 GC。那样会立刻掉进后端细节。先把任何 GC 都压成四个问题:

1. **从哪里开始找活对象?** 这些入口叫根(root):当前函数的局部变量、挂起的生成器帧、调度器队列、TLS 里的当前异常、C 扩展模块状态。
2. **沿哪些边继续找?** 对象里的每个 `PyObject *` 字段都是一条边,本章叫槽位(slot):列表的元素、实例的属性字典、异常的 `message`/`cause`/`context`、任务的状态槽。
3. **程序运行时改了对象图,收集器怎么知道?** 写屏障在"把新对象存进槽"时记一笔账;读屏障在"从槽里读对象"时修正可能已经搬家的地址。
4. **什么时候、用什么策略回收?** 这才是后端算法:#0 以引用计数为主,#1 分小步标记,#2 把标记工作交给线程,#3 把对象分 young/old,#4 允许对象搬家。

用一个最小对象图看这四个问题:

```text
frame root
   │
   ▼
saved ──slot[0]──▶ exception ──message──▶ [1, 2, 3]
```

如果 `saved` 没进根集,整条链在追踪后端眼里都不存在;如果 `exception.message` 没进槽位遍历,message 会被误清;如果 `saved[0] = exception` 绕过写屏障,#3/#4 可能不知道老对象指向了新对象;如果 exception 被 #4 搬走而读路径绕过读屏障,槽里会留下旧地址。这四类错误分别对应本章后面的根集、槽位契约、写屏障、读屏障。源码名也可以按这个读法归类:`pcc_gc_frame_enter()` 负责根,`pcc_gc_trace_referents()` 负责沿槽走图,`pcc_gc_store_ptr()` 是写屏障入口,`pcc_gc_load_ptr()` 是读屏障入口,`pcc_gc_step()` 才进入后端策略。

所以本章不是让读者一次记住五个收集器。读法应该是:先确认"活对象从哪里被看见",再确认"对象之间的边是否被看全",再确认"边变化和地址变化是否被记录",最后才比较五个后端如何利用同一份信息。

## 10.1 问题与设计空间:为什么是五个收集器

先回答"为什么不是一个"。主流运行时都只押一种内存管理策略:CPython 押引用计数加环收集器,Go 押并发标记清除,OCaml 押分代小堆,ZGC 押着色指针重定位,Lua 押增量三色。每种选择都是对同一组长跑指标——暂停时间、RSS、吞吐、碎片随时间的演化——的不同取舍,而这些取舍被焊死在各自运行时的对象模型里,无法在同一程序、同一语义、同一对象图上对照测量。

pcc 的论题(见第 1 章)把"五 GC 比较运行时"列为五大差异化之一:它不是一个收集器加四个实验品,而是一个研究纲领——同一份编译产物,通过环境变量切换五种收集策略,在同一自举负载和同一语义契约下比较。[docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research/) 下保存着五个参照实现(CPython、Lua、Go、OCaml、ZGC)的源码,仓库规则要求移植前先读参照、不得重新发明。

这个纲领的最大风险不是某个算法写错,而是**五个后端各自演化出一套"什么算可达、什么算存活"的对象图规则**。一旦规则有两套,同一程序在不同后端会看到不同的对象生存期;差异以最难调试的方式呈现——只在某个后端出现的释放后使用、只在某个后端丢失的属性。[goal-prompt.md](../../docs/goal/goal-prompt.md) 的 5-GC Production Equality Rule(生产平等规则)就是对这个风险的制度化回答:

- **语义是硬性要求,五后端必须一致**:对象可达性、根安全、异常与帧的存活、容器图安全、弱引用/终结器/复活策略、扩展对象生存期、值类指针载荷安全、虚拟线程挂起帧与调度器根安全。
- **性能是可报告差异,允许不同**:暂停、吞吐、RSS、碎片画像、收集节奏都是每个后端自己的画像。
- **状态词汇有强制规则**:一个触及对象/引用/生存期的运行时特性,只有在 `PCC_GC_BACKEND=0..4` 全部通过共同契约测试后才能称 `DONE_STRONG`;只过 #0 是 `DONE_WEAK`;过一部分是 `BACKEND_PARTIAL`。`#0 是默认 ≠ #0 是唯一生产后端`;`#1–#4 可选 ≠ 实验品`。

这条规则不是文档修辞。10.7 节会展示它的第一块契约测试砖如何抓出一个让三个后端崩溃的真实 use-after-free,以及一个根因藏在前端代码生成里的根缺失。

## 10.2 一套 ABI 表面:`PCC_GC_KIND_*` 与运行时选择

五个后端在 [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 中以一个枚举存在:

```c
enum {
    PCC_GC_KIND_REFCOUNT_CYCLE = 0,
    PCC_GC_KIND_INCREMENTAL_TRICOLOR = 1,
    PCC_GC_KIND_CONCURRENT_MARK_SWEEP = 2,
    PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR = 3,
    PCC_GC_KIND_COLORED_RELOCATING = 4
};
```

pcc-Python 镜像 [pcc/runtime/py/py_gc_backend.py](../../pcc/runtime/py/py_gc_backend.py) 的文件头特意注明:名字是算法性的,不是项目品牌——refcount-cycle、incremental-tricolor、concurrent-mark-sweep、generational-minor-major、colored-relocating。选择发生在运行时:`py_gc_backend.c` 的 `pcc_gc_init_config()` 在首次进入 GC 路径时解析环境变量 `PCC_GC_BACKEND`(默认 0),`pcc_gc_set_backend()` 提供程序内切换。这意味着**同一个二进制可以在五种收集策略下运行**——这是五后端自举矩阵([tests/python/gc/](../../tests/python/gc/) 下的 `test_pcc_bootstrap_full_gc{0..4}.py`,对每个后端各跑一遍完整 stage1→stage2→stage3 自举)在工程上可行的前提:不需要五份编译产物,只需要五次运行。

更重要的设计决定写在 `py_runtime.h` 的 GC 接口注释里:`pcc_gc_alloc` / `pcc_gc_retain` / `pcc_gc_release` / `pcc_gc_load_ptr` / `pcc_gc_store_ptr` 这组函数"是代码生成应当面向的内存管理 ABI;未来的追踪/分代/移动收集器必须保持这个表面,而不是把自己的内部机制教给代码生成";`py_incref` / `py_decref` 被明确定位为引用计数形状的兼容垫片,"不应被新代码当作基础 ABI"。备选方案是让前端为每个后端发射不同的屏障序列——那会把后端数量乘进代码生成的测试矩阵,并使"同一二进制五种运行"不可能。pcc 选择把全部后端分派折叠进运行时函数内部,代价是每次槽访问多一次后端判断;10.4 节会看到这个判断的具体形态。

## 10.3 一套对象图规则:槽位 trace/update 契约

### 10.3.1 为什么对象图规则只能有一套

一个收集器对对象图需要两种能力:**遍历**(visit:给定对象,枚举它的全部指针槽)和**更新**(update:给定对象,把某个槽改写为新地址——重定位后端的需求)。每个运行时类型(list、dict、实例、生成器、任务、异常……)的指针槽集合,就是该类型在对象图中的"形状"。

这里的"槽位"不是抽象术语,就是运行时结构体里能指向另一个 Python 对象的位置。列表的 `items[i]` 是槽位,字典表项里的 key/value 是槽位,异常对象的 message 是槽位,生成器保存的堆帧也是槽位。收集器并不理解"异常消息很重要"这种语义句子;它只会问一个对象:"把你的所有指针槽交出来。"少交一个槽,被指向的对象就可能被当作垃圾;搬家后少更新一个槽,旧地址就会留在对象图里。

这份形状信息如果在两个生产实现里声明,就会漂移;漂移的后果不是编译错误,而是某个后端在标记时漏看一个槽(对象被误回收)或在重定位后漏改一个槽(悬挂指针)。AGENTS.md 把这条写成硬规则:五个后端必须消费**同一套由 pcc-Python 拥有的**槽位 trace/update 契约(实际统一入口为 `pcc_gc_visit_object_slots`,配套 root + frame + native-handle 注册),C 版本只作差分 oracle。[goal-prompt.md](../../docs/goal/goal-prompt.md) 的 G-track 把机制说得更具体:每个运行时对象类型把自己的引用槽**声明一次**(强/弱/借用/钉住/可移动/原生句柄/值载荷指针/帧局部/调度器根),五个后端各自以不同身份消费同一份声明——#0 用于可达性与环检测,#1 用于标记屏障,#2 用于工作缓冲,#3 用于记忆集(remembered set)与晋升,#4 用于转发与槽改写。

### 10.3.2 诚实的现状:生产契约已经迁入 pcc-Python

2026-08-03 的 [freestanding five-GC production closure](../../docs/goal/evidence/2026-08-03-freestanding-gc-done-strong.md)改变了本节早期快照。生产 collector policy 不再由 `src/py_gc_backend.c` 与 `py/py_gc_backend.py` 两份大实现维持镜像;它被拆入 `freestanding_gc_*` 模块。`freestanding_gc_object_slots.py` 的 `pcc_gc_visit_object_slots()` 是对象槽统一入口,依次处理核心容器、固定 owner、弱引用、continuation、class、C-extension 和 instance 槽。root/frame registry、mark cycle、backend0 collector、incremental/concurrent scheduler、generational promotion 与 relocation/ZPage mechanics 也各有 pcc-Python owner。

C collector 源仍保留,但 production link map 已证明没有 C-owned collector 定义。它的角色是差分 oracle,不是第二个运行时政策 owner。共同契约套件、weakref/finalizer/resurrection/suspended-frame/C-extension-root/relocation 闸门、五后端长跑与 GC0–GC4 的 current-source pcc1→pcc2→pcc3 fixed point 共同约束这条声明。新增带指针槽的类型时,生产必改点是统一 pcc-Python slot owner;C oracle 需要相同输入的差分更新,却不能重新进入生产归档。

### 10.3.3 分层:索引、分配器和原子也不再是永久 C kernel

早期版本把 `pcc_gc_frame_index_insert`、`pcc_gc_object_index_find` 与 `pcc_gc_forwarding_index_*` 所在的哈希表视为只有 C 实现的 kernel。当前 `freestanding_gc_index_table.py` 已导出这些 ABI,开放寻址、rehash、backward-shift deletion 和 frame/object/forwarding/ZPage 索引均由 freestanding pcc-Python 拥有;`src/py_gc_index_table.c` 留作差分 oracle。分配器同样由 `freestanding_allocator.py` 拥有。真正保留在编译器内建里的只有 raw memory、atomics、page/syscall/host-ABI 等机器操作,而非哈希策略或对象图语义。完整的去 C/libc 分层见第 14 章。

## 10.4 读写屏障:`pcc_gc_store_ptr` 与 `pcc_gc_load_ptr` 各为谁服务

代码生成访问对象指针槽只允许走两个函数([pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c)):

```c
PyObject *pcc_gc_load_ptr(PyObject *owner, PyObject **slot);
void      pcc_gc_store_ptr(PyObject *owner, PyObject **slot, PyObject *value);
```

本节要点:写屏障回答"我刚刚改了一条边,谁需要知道?",读屏障回答"我正在读一条边,它指向的对象是否已经搬家?"。引用计数需要前者来平衡新旧值;分代和重定位需要前者来记录老→新边;搬动后端需要后者把旧地址改成新地址。二者都不应该散落在调用点里,所以 pcc 把对象槽读写收口到这两个函数。

### 写侧

`pcc_gc_store_ptr()` 对**所有**后端首先承担一个与 GC 算法无关的职责:**平衡的引用计数契约**——incref 新值、写槽、decref 旧值。这意味着 `py_list_append` 等容器存储是引用平衡的;第 9 章的所有权推理依赖这一点。(2026-06-10 的一次调查更正记录了反面教材:有人只读调用点就断言"`py_list_set` 不 incref",据此设计的排序优化泄漏了引用——教训被写进调查文件:**先读屏障助手的源码,再断言它的引用计数契约**。)

在平衡存储之前,`pcc_gc_note_slot_write_barrier()`(`py_gc_backend.c`)按当前后端分派写屏障语义:

- **#1 增量三色**:若 owner 已是黑色而新值是白色,把新值染灰——经典的"存储时补染",维持三色不变式(黑色对象不得直接指向白色对象);
- **#2 并发标记清除**:标记进行期间,任何尚未变灰的新值被染灰,并进入线程本地的缓冲写屏障(`pcc_gc_cms_buffer_gray()`,满则成批刷入工作队列)——这是对 Go 缓冲写屏障的移植;
- **#3 分代**:仅当"老年代 owner 存入新生代 value"时,把 owner 记入记忆集(`pcc_gc_backend3_remember_owner_unlocked()`,对象粒度,置 `PY_FLAG_GC_REMEMBERED`);
- **#4 着色重定位**:同样的老→新条件下,把 (owner, slot, value) 入队 store buffer;
- **#0 引用计数**:写屏障部分什么都不做,只剩平衡存储本身。

`pcc_gc_store_root()` 是同一契约的全局根槽变体(owner 为 NULL 时 #1/#2/#4 在标记期直接染灰新值)。

### 读侧

`pcc_gc_load_ptr()` 只为 **#3 与 #4** 工作:这两个后端会移动对象(#3 的小堆晋升拷贝、#4 的页疏散),因此读到的槽值可能是旧地址。读屏障查询转发表(`pcc_gc_note_relocation_read()`),若对象已被搬走则返回新地址,**并把新地址写回槽位**(incref 新、decref 旧)——即自愈式(self-healing)读屏障,同一个槽只付一次转发查询。它有两个变体:`pcc_gc_load_borrowed_ptr()` 自愈但不动引用计数(借用语义),`pcc_gc_resolve_owned_ptr()` 解析一个已经握在寄存器里的拥有引用。在 #0/#1/#2 上,读屏障退化为一次普通装载。

### 为什么违例只在部分后端爆炸

AGENTS.md 警告:绕过屏障的裸写 `obj->slot = x` "在后端 #0 上工作,在 #3/#4 上崩溃"。这不是 #3/#4 更脆弱,而是 #0 恰好不需要屏障携带的信息,所以违例在 #0 上**不被发现**。一处裸写意味着:#3 看不到老→新引用(小堆收集时误回收新生对象),#4 在重定位后留下一个永不自愈的旧地址。平等契约把这类潜伏违例从"等线上崩"提前到"五后端矩阵跑不过"。每类屏障事件都计入遥测计数器(`PCC_GC_COUNTER_WRITE_BARRIERS` / `READ_BARRIERS` 等,`pcc_gc_telemetry()` 读取),使"这个后端到底付了多少屏障成本"成为可测量的研究数据而非印象。

## 10.5 根集:帧根、续延根、调度器根与扩展状态根

追踪收集器的正确性始于根集。pcc 的根集有四类来源,全部注册进 `py_gc_backend.c`,并由同一组函数消费:`pcc_gc_gray_current_roots()` 为追踪后端播种灰色,`pcc_gc_visit_runtime_roots()` 把**同一份根集**提供给后端 #0 的环收集器(`py_obj_gc.c` 的 `py_gc_recompute_reachability()` 调用它)。根集也只有一套——这是槽位契约在根维度的对应物。

根不是对象内部的字段,而是堆外世界指向堆内对象的入口。当前 C 栈上的局部变量、堆上挂起的协程帧、调度器队列和 C 扩展状态都不是普通 Python 容器,收集器不能靠"遍历对象槽位"自然发现它们。它们必须显式注册。根集漏一项时,后果表观上常常像"对象内部槽位被清坏了",但真正原因是整条对象链根本没有从入口被标记到。

### 帧根:槽粒度,非 LIFO,必须哈希

编译后的函数把局部变量根描述为一个**帧映射(frame map)**,v0 格式定义在 `py_runtime.h` 注释里:`frame_map` 指向一个带符号 int32 槽数(正数=拥有引用的根,负数=借用根),`slots` 指向连续的 `PyObject *` 数组;NULL 映射表示无根。运行时入口是 `pcc_gc_frame_enter()` / `pcc_gc_frame_leave()`,落到 `pcc_gc_note_frame_enter()` / `pcc_gc_note_frame_leave()`:进入时分配一个 `PccGcFrameNode` 挂入活动帧链表,**并以 slots 指针为键插入 `pcc_gc_frame_index` 哈希表**;离开时按键删除。哪些局部需要进入帧映射由前端的所有权低层化(lowering)决定——`_ensure_owned_local_gc_root`([pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen/) 的 ownership 路径)注册槽位;10.7.2 会展示这半边契约缺一个角时的后果。

为什么是哈希而不是栈?因为**帧根的进入/离开不是函数粒度而是槽粒度,顺序不是 LIFO**。代码生成在多个位置发射 `pcc_gc_note_frame_leave(slots)`——return 路径、元组打印、拥有局部的清理、一元调用包装——同一逻辑帧的注册与注销彼此交错。2026-06 的调查([docs/investigations/gc-frame-index-entry-pool-perf.md](../../docs/investigations/gc-frame-index-entry-pool-perf.md))用一次失败实验证明了这一点:把哈希换成 LIFO 影子栈(非栈顶退化为线性扫描)后,gc3 的 stage2 自举从约 226 秒**退化到 900 秒超时**——在约 300 层深的递归下降解析器上,"回退"线性扫描成了常态路径,复杂度变成 O(n²)。哈希无论目标在哪个深度都是 O(1),是这里**正确的数据结构**;真正的成本(每帧一次 malloc)后来用条目池解决(见 10.7.1)。`PccGcFrameNode` 的 `dup_next` 链处理同一 slots 地址被重复注册的情形;`pcc_gc_root_slot_count_from_map()` 对 INT32_MIN 与超大槽数做了防御。还有一个细节:`pcc_gc_should_track_frame_roots()` 显示后端 #0 默认不追踪帧根(它靠引用计数,不需要),仅当通过 `pcc_gc_set_backend()` 显式选回 #0 时才打开——这是少数明示的后端差异,差异在于"是否需要这份信息",不在语义。

### 续延根:挂起的协程帧

虚拟线程与生成器把被挂起函数的局部搬进堆上的续延栈块(`PyContinuationObject` 的 `stack_chunk`)。这些槽在函数不在 C 栈上时仍然是根:`pcc_gc_register_continuation_root()` 用同样的 frame map 格式注册它们,`pcc_gc_trace_continuation_roots()` 在标记时把它们染灰,`pcc_gc_rewrite_continuation_roots()` 在重定位后把其中的旧地址改写为新地址——挂起的帧不仅要"被看见",在 #4 下还要"被改写"。

### 调度器根:队列里的任务不是垃圾

一个已就绪但尚未运行的任务,可能不被任何用户可见对象引用——它只活在调度器队列里。`PccGcSchedulerQueue` 因此把根注册做进了队列操作本身:`pcc_gc_scheduler_queue_push()` 为每个入队条目的 value 槽调用 `pcc_gc_scheduler_root_register()`,`pcc_gc_scheduler_queue_pop_into()` 在弹出时解析可能的转发地址再注销。队列存续期间,每个排队值都是收集器可见的根。

### 扩展状态根与 TLS 异常根

C 扩展通过 `PyModuleDef.m_size` 持有模块状态,状态里的 `PyObject *` 是裸 C 槽——收集器既不能假设它遵守屏障,也不能在 #4 下改写它(它不是 pcc 拥有的可更新槽)。2026-05-31 的调查(`gc-5backend-extension-module-state-roots-no-libpython.md`)确定的窄策略是:通过扩展自己声明的 `m_traverse` 枚举这些引用,**先钉住(pin)再作为根访问**——宁可放弃搬动这些对象,也不冒改写裸槽的风险。`pcc_capi_visit_extension_module_state_roots()` 被根播种与根访问两条路径共同调用。类似地,TLS 中在传播的当前异常也是根(#3 还需要 `pcc_gc_promote_tls_exception_root()` 在小堆收集时晋升它)。

## 10.6 不得靠弱化取胜

平等规则有一条容易被忽视的反向约束,AGENTS.md 写为义务 6:五个后端中**任何一个都不得通过弱化终结器、弱引用、复活、挂起协程帧、调度队列、C 扩展引用或值载荷来取胜**。诱惑是真实的:终结器、复活、收集期间的重入,是每个追踪收集器最难的角落,而"这个后端不支持 `__del__` 复活"会让实现简单一个数量级、基准好看一截。pcc 把这条路堵死:语义弱化不是优化,是不合格。

这条约束的承载结构是追踪清除路径的**两阶段(实为四步)清除**,`py_gc_backend.c` 的 `pcc_gc_sweep_unreachable()`(端口镜像 `_sweep_unreachable`),每一步都对应一个曾经真实失败、后被契约测试钉住的语义:

1. **PASS 0,终结器先于一切破坏**:对每个不可达对象先跑 `py_user_del_dispatch()`——此时字段完好。这是对 CPython PEP 442 的对齐;在它落地前,#1–#4 对环成员**根本不跑** `__del__`,或在字段已被清空后才跑(`gc-5backend-cycle-finalizer-not-run-no-libpython.md`,2026-05-31 修复)。`PY_FLAG_FINALIZED` 保证 `__del__` 至多一次,后续 dealloc 不会重入(防复活环再进终结器,见第 9 章)。
2. **复活复查**:终结器可能把 self 存回可达处。`pcc_gc_recheck_reachability_after_finalizers()` 重新播种、重新标记,把复活者移出清除候选——否则就是清掉活对象(`gc-5backend-finalizer-resurrection-no-libpython.md`)。
3. **PASS 1,只清不放**:`pcc_gc_clear_unreachable()` 先 `py_weakref_invalidate()`(弱引用在对象死亡时失效,且 `_trace_referents` 对弱引用只访问 callback 槽、刻意不访问 referent——弱引用不维持存活,这本身就是契约的一部分),再 `pcc_gc_clear_referents()` 断环;`pcc_gc_clear_slot()` 对仍是清除候选的兄弟跳过 decref。
4. **PASS 2,统一释放**:`pcc_gc_finalize_unreachable()` 释放已清对象。clear 与 free 必须分相——交错执行曾让 #1/#2/#3 三个后端在最基本的两节点环上 use-after-free(10.7.3)。

同一精神覆盖其余条目:终结器里调用 `gc.collect()` 不许崩也不许禁——`pcc_gc_collect()`(`py_obj.c`)开头的重入守卫让收集中的重入成为空操作,与 CPython 的 `gc.collecting` 语义一致(`gc-5backend-reentrant-collect-during-finalizer-no-libpython.md`);被丢弃的原生文件句柄必须在释放前关闭并冲刷(`PY_TYPE_FILE` 的专用 dealloc,`gc-5backend-native-file-handle-lifetime-no-libpython.md`);值类的指针载荷在 #4 重定位下的根安全在本文写作时仍是**开放的**调查(`gc-5backend-valueclass-pointer-payload-roots-no-libpython.md`,状态 active)——按声明卫生,这里如实写明:该契约砖尚未对全部后端关闭。

## 10.7 历史与教训

### 10.7.1 帧根索引:一次被否决的"优化"与它背后的真成本(2026-06-04 至 06-10)

**症状**:五后端字节同一(byte-identical)全部成立,但 gc3 的 stage2 自举约 226 秒、gc4 约 310 秒,而 gc0 约 107 秒——纯性能差距,采样显示热点在帧根登记。

**错误假设**:`frame_index` 哈希(插入/删除加每条目 malloc)太贵,换成 LIFO 影子栈就好。**实验与否决**:换上影子栈后 gc3 直接退化到 900 秒超时——帧根槽粒度、非 LIFO 的交错让"非栈顶回退线性扫描"成为常态路径,在约 300 层递归的解析器上变成 O(n²)。提案在调查文件里被记为 DENIED 并回退。

**真根因与正确修复**:成本从来不在查找,在每帧条目的 malloc/free。`py_gc_index_table.c` 给 `PccGcPtrIndexEntry` 加自由链表池(深递归在相近深度反复进出,条目高复用),gc3 226→167 秒、gc4 310→230 秒,五后端字节同一保持。

**故事没完**:事后评审发现池引入了数据竞争——索引表的设计契约是"调用方持 GC 图锁",但 `pcc_gc_object_id()` 的身份索引插入不持锁,与持锁路径共享同一条自由链表;原先的 malloc/free 内部线程安全,池反而打破了它。而最初的"五后端字节同一"验证是单作业跑的,根本没踩到线程路径。修复:`_Thread_local` 自由链表(无共享状态)、`PCC_GC_PTR_INDEX_FREE_CAP` 上限防 RSS 钉死、线程退出时 `pcc_gc_ptr_index_tls_pool_drain()` 防长跑服务泄漏。2026-06-10 全矩阵复核通过。

**留下的不变式**:帧根是槽粒度非 LIFO 的,`frame_index` 必须支持 O(1) 任意位置删除——不许换成栈或链表;性能修复不得改动正确性结构,只许改分配策略;"正确性验证覆盖了哪种构建配置"必须随声明一起写出(单作业验证不证明线程安全)。

### 10.7.2 异常 referent 根:根因在前端,不在运行时(2026-05-31)

**症状**:被 except 捕获、存进局部 `saved` 的异常,在 `gc.collect()` 后 `str(saved)` 在 #0 上返回 `[1, 2, 3]`,在 #1–#4 上返回 `<null>`——异常壳还在(引用计数维持),message 字段被清。

**两个被否决的假设**:先猜运行时漏 trace 槽位——但 message 在 `_trace_referents` 的异常分支里,且经 `pcc_gc_store_ptr` 存储;再猜分配时漏 `py_gc_track`——补上后照样 `<null>`,且证据显示后端 1–4 在 `pcc_gc_alloc` 时本就把每个对象登记进对象索引,该编辑按"不留未经自举验证的运行时编辑"纪律回退(DENIED);第三个"传播路径瞬时减到零导致 untrack"的推断也被直接证据否决——`pcc_gc_note_object_freeing(exc)` 只在 collect 内触发,传播中从不触发。

**决定性判别**:同一个 except 处理器里绑两个局部——`s_list = [7, 8, 9]` 和 `s_exc = e`。collect 后 s_list 在五个后端全活,s_exc 只在 #0 活。同帧、同处理器:帧根机制是好的,**是 s_exc 没进帧映射**。

**真根因(前端)**:局部成为 GC 根的唯一通道是所有权低层化的 `_ensure_owned_local_gc_root`,它只对"右值是新引用(owned)"的赋值注册根。`s_exc = e` 是借用来源的拷贝赋值(赋值本身 incref 了,但右值表达式是借用),不触发注册;而来源 `e` 是 except 绑定,`exception_lowering.py` 对它只做裸 alloca 加存储、不注册根,并在处理器结束时释放保留。于是处理器之后没有任何根覆盖这个异常,追踪标记把它当不可达清掉 message。#0 不看追踪名单,靠引用计数活了下来。修复落在前端:记录 except 绑定名,对"从 except 绑定赋值出去的局部"注册根;`test_exception_roots.py` 从 xfail 翻为五后端硬闸门。

**留下的不变式**:对象图契约横跨编译器两侧——**根注册是契约的前端一半,运行时只消费它**。"#0 通过、#1–#4 失败"的形状不能默认是运行时 bug;前端低层化决定哪些槽是根,而 #0 的引用计数会掩盖根缺失。这也是调查工作流"一次一个提案、跑到 CONFIRMED/DENIED"的展示:两个貌似合理的运行时修复被证据逐个否决,才逼出真正的根因。

### 10.7.3 对象生命周期契约:一处共享路径修复关闭三个后端(2026-05-31)

平等契约的第一块测试砖(基本生存期、两节点环、嵌套容器,各跟一次 `gc.collect()`)第一次运行就证伪了"五后端生产平等"的当时状态:#0/#4 通过,#3 在**最简单的环**上崩出 `[BAD_INCREF] tag=-1`,#1/#2 在嵌套容器上 SIGABRT。LLDB 回溯指向教科书式的环收集器 use-after-free:清除路径对每个不可达对象**清完即放**,清 x 的槽把环兄弟 y 减到零并立即释放,清除循环随后又对已释放的 y 做 finalize。这正是 CPython 在清相保持不可达集合存活的原因。一个修复——两阶段 clear-then-free(PASS 1 清引用但保留清除候选标记,`pcc_gc_clear_slot` 跳过候选兄弟的 decref;PASS 2 统一释放)——同时关闭了 #1/#2/#3,因为三者共享同一条追踪清除路径;C 与端口同步镜像。这个故事的教训与前两个互补:共享路径意味着一个 bug 表现为三个后端的"各自怪病",也意味着一次根因修复的杠杆是三倍;而让这件事在自举之前、以确定性测试形态暴露出来的,正是契约套件本身。

## 10.8 小结

五 GC 架构的全部要点是:**比较收集算法,而不比较语义**。为此 pcc 把可变的与不可变的切开——算法、节奏、代价画像每后端各异(第 11 章);而以下五样东西只有一套,且被闸门钉住:

1. **选择 ABI**:`PCC_GC_KIND_*` 枚举加 `PCC_GC_BACKEND` 运行时选择;代码生成面向 `pcc_gc_*` 表面,后端内部对前端不可见。
2. **对象图规则**:每类型的槽位 trace/update 契约。目标是单点声明(`py_obj_visit_slots`);现状是多份按类型遍历器加镜像纪律加契约套件,差距如实记录。
3. **根集**:帧根(槽粒度、非 LIFO、`frame_index` 哈希)、续延根、调度器根、扩展状态根,同一份根集供五后端与 #0 环收集器共同消费;根注册的前端一半属于所有权低层化。
4. **屏障对**:`pcc_gc_store_ptr` 全后端平衡存储加按后端写屏障(#1 染灰、#2 缓冲染灰、#3 记忆集、#4 store buffer);`pcc_gc_load_ptr` 为 #3/#4 提供自愈读屏障。
5. **平等规则**:语义五后端一致是硬要求,任何后端不得靠弱化终结器/弱引用/复活/挂起帧/调度队列/扩展引用取胜;`DONE_STRONG` 必须五后端全部通过。

三个案例研究各钉一条不变式:数据结构的选择要忠于访问模式的真实形状(非 LIFO 就不许用栈);根注册是前端义务,#0 的宽容会掩盖它的缺失;共享路径的 bug 与修复都是乘法。下一章逐个打开五个后端,看同一副骨架上五种算法各自如何行走。

## 练习

1. **读源码验证**:阅读 [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c) 中的 `pcc_gc_store_ptr()`,写出它对新值与旧值各做了什么。据此论证 `py_list_append` 是引用平衡的,并设计一个带 `__del__` 计数器的小程序在 `PCC_GC_BACKEND=0` 下验证(只设计,不必运行)。
2. **读源码验证**:阅读 `py_gc_backend.c` 的 `pcc_gc_note_slot_write_barrier()`,为五个后端分别写出"该函数提前返回、什么都不做"的精确条件。解释为什么 #0 下它实际是空操作,而这不构成语义差异。
3. **格式推演**:按 `py_runtime.h` 的 frame map v0 注释,手工写出一个含两个借用根槽的帧映射的内存布局。再读 `pcc_gc_root_slot_count_from_map()`,解释它对 `INT32_MIN` 与超大槽数的两个防御分支分别防什么。
4. **镜像审计**:对照 `py_gc_backend.c` 的 `pcc_gc_trace_referents()` 与 `py_gc_backend.py` 的 `_trace_referents`,为任选三个类型标签核对两侧访问的槽集合是否一致(注意端口用裸偏移)。若要新增一个带两个指针槽的运行时类型,列出本章提到的所有必改点。
5. **设计权衡论证**:[docs/investigations/gc-frame-index-entry-pool-perf.md](../../docs/investigations/gc-frame-index-entry-pool-perf.md) 末尾记录了把索引表改为开放寻址的设计稿。读 [pcc/runtime/src/py_gc_index_table.c](../../pcc/runtime/src/py_gc_index_table.c) 中关于条目池与锁界的注释,论证:该重写必须保持哪条"谁持锁、谁不持锁"的分界?为什么字节同一(pcc2==pcc3)对这个改动不是必要闸门,而五后端契约套件是?
