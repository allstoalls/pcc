# 第 11 章 五个后端:从引用计数到重定位

第 10 章讲完了五个收集器共用的骨架:一套选择 ABI、一套槽位契约、一套根集、一对读写屏障、一条生产平等规则。本章逐个打开五个后端,看五种算法如何在同一副骨架上行走。每个后端用同一套问题展开:参照系是谁、核心算法是什么、移植到 pcc 时做了哪些关键决定、有哪些只属于它的不变式、今天的诚实状态如何——以及至少一个真实的案例研究。所有性能数字只作相对量级使用,并标注测量日期与含义;自举(bootstrap)负载下的秒数衡量的是"收集器对编译器型负载收的税",不是暂停画像。

> **2026 年 8 月状态说明。** 五个生产 collector 的 policy 现已由严格 freestanding pcc-Python 编写并进入生产归档,C collector 不再拥有生产符号;证据边界见 [freestanding five-GC production closure](../../docs/goal/evidence/2026-08-03-freestanding-gc-done-strong.md)。本章保留的 C→pcc-Python 对照和失败故事是迁移过程的差分证据,不是“今天仍有两套生产实现”的陈述。算法等价性、长期暂停/RSS/碎片和完整固定点仍须按各自 gate 单独声明。

## 本章导读:五个后端概览

第 10 章回答的是"五个后端共用什么信息";本章回答的是"每个后端拿同一份信息做什么事"。先不要背算法名,先用下面这张表建立直觉:

| 后端 | 核心思想 | 它最关心哪类账 |
|---|---|---|
| #0 refcount-cycle | 主要靠引用计数即时释放,再用环收集补上互相引用的死对象。 | 每个对象现在还有多少拥有者;追踪名单里的容器是否只被环内对象引用。 |
| #1 incremental-tricolor | 不一次性扫描全堆,而是按分配债务分小步把对象染成白/灰/黑。 | 标记进度、颜色位、黑对象是否新指向白对象。 |
| #2 concurrent-mark-sweep | 把 #1 的标记工作排给后台线程和队列,但今天仍用 STW 切片保证正确性。 | 工作票据、写屏障缓冲、图锁和线程安全。 |
| #3 generational | 假设多数对象短命:先放 young 区,活下来再晋升 old 区。 | 老对象是否指向新对象;晋升后哪些槽必须立刻改写。 |
| #4 colored-relocating | 允许对象搬家,用转发表和读屏障把旧地址修成新地址。 | 谁待搬、谁已搬、哪些槽还没自愈、`id()` 是否稳定。 |

每节都可以按三个问题读:

1. **分配和存储时多记了什么账?** 例如 #1 记颜色,#3 记老→新,#4 记转发表和 store buffer。
2. **一次 `pcc_gc_step()` 推进了哪种工作?** 例如 #1/#2 推标记,#3 推晋升,#4 推排空、年龄推进、页疏散。
3. **漏记这笔账会坏在哪里?** #0 会双重释放,#1 会漏标,#3 会晋升后悬挂,#4 会释放旧地址。

这样读,第 11 章不是五套互不相干的算法清单,而是同一张对象图在五种代价结构下的五种维护方式。

## 11.1 一副骨架,五种行走

先把声明卫生立在前面。[pcc/runtime/src/py_gc_backend.c](../../pcc/runtime/src/py_gc_backend.c) 的文件头注释如实记录了出身:非引用计数的后端"以可选择的骨架起步:复用引用计数语义,同时暴露真实 Lua/Go/OCaml/ZGC 实现将驱动的屏障/安全点计数器"。两年的切片工作让这些骨架长出了真实算法——三色推进、并发工作线程、小堆晋升、转发与搬动——但 [docs/refs_docs/gc-research/README.md](../../docs/refs_docs/gc-research/README.md) 的状态表仍然写明:它们**不是** Lua、Go、OCaml、ZGC 的等价算法移植,而是朝各自参照系收敛的、逐切片验证的方向。本章按这个口径写:每节先说参照系要求什么,再说 pcc 今天实现到哪里、刻意没实现什么。

五个参照实现的源码快照就在仓库里([docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research/) 下的 `<lang>/`),仓库规则要求移植前先读参照、不得重新发明:

```text
后端  算法                      参照              快照内容(节选)
#0    refcount + STW 环收集    python/           gcmodule.c, gc_free_threading.c
#1    增量三色标记清除          lua/              lgc.c, lobject.h, lstate.h
#2    并发标记清除              go-greentea/      mgcmark.go, mgcsweep.go, mwbbuf.go
#3    分代 young/old           ocaml/            minor_gc.c, major_gc.h, gc.h
#4    着色重定位 / GenZGC      zgc/              jdk-27+21 钉定包(2026-05-14 取样,
                                                 MANIFEST.json 记录哈希)
```

ZGC 快照特意钉在 OpenJDK `jdk-27+21`,因为 JDK 23(JEP 474)起分代模式成为默认、JDK 24(JEP 490)删除了非分代模式——后端 #4 必须对照**分代** ZGC 评估,不是已被删除的单代模式。

进入各后端之前,先看一眼它们共用的入口,后面各节就不必重复了。`gc.collect()` 落到 [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c) 的 `pcc_gc_collect()`,它对 #0 与追踪后端走两条完全不同的管线:

```text
pcc_gc_collect(reason)
  ├─ #0:           py_gc_collect()            (py_obj_gc.c,STW 环收集)
  └─ #1/#2/#3/#4:  pcc_stop_the_world()
                    pcc_gc_begin_explicit_tracing_collect()
                    loop { pcc_gc_step(1024) } 直到步进无工作
                    pcc_gc_collect_tracing()   (有候选才清除)
                    pcc_gc_end_explicit_tracing_collect()
                    pcc_resume_world()
```

也就是说,追踪后端的"标记"藏在 `pcc_gc_step()` 的循环里,"清除"在 `pcc_gc_collect_tracing()` 里——调试时在错误的一半下断点会让你以为收集没有发生。`pcc_gc_step()` 内部按 `pcc_gc_selected_backend` 分派:#1/#2 走三色追踪步进,#3 走晋升步进,#4 走 store-buffer 排空→年龄推进→页疏散→追踪的流水线。各后端的差异全部长在这一个分派点之后;分派点之前的世界(分配、屏障入口、根注册)是第 10 章的内容。

还有一个常被误解的架构事实值得先说破:**引用计数在五个后端下都活着**。`pcc_gc_retain()` 就是 `py_incref()`,`pcc_gc_release()` 的主体就是 `py_decref()`(`py_obj.c`;#3/#4 在前面垫了转发解析,#3 对 arena 出身的对象另有专门分支,见 11.5),计数到零即时释放是所有后端的常规路径。#1–#4 的追踪机器叠加在引用计数之上,负责引用计数收不掉的部分——环。这个选择不是偷懒:确定性的终结器时序与 C 扩展引用契约的兼容都依赖即时回收,把引用计数整个换掉等于换一种对象生命周期语言。代价是每个追踪后端都要与一个并行的回收者共存——11.2 的案例研究会展示这种共存最尖锐的一个角。

每个后端的硬性闸门是同一个:[tests/python/gc/](../../tests/python/gc/) 下的 `test_pcc_bootstrap_full_gc{0..4}.py`,对每个 `PCC_GC_BACKEND` 值各跑一遍完整 stage1→stage2→stage3 自举,并要求 pcc2 与 pcc3 规范化后字节同一。语义由第 10 章的平等契约钉死;本章谈的全部差异都落在算法、节奏与代价上。

## 11.2 后端 #0:引用计数加环收集——参照 CPython

**参照系。** `gc-research/python/gcmodule.c` 是 CPython 3.13 的分代环收集器(`gc_collect_main`、`visit_decref`、`move_unreachable`),`gc_free_threading.c` 是 PEP 703 无 GIL 变体,留作未来自由线程路径的参照。

**核心算法。** 引用计数是第一收集器:`py_decref` 到零即释放,绝大多数对象从不进入任何追踪名单。环收集器只对被追踪的容器工作(`PY_FLAG_GC_TRACKED`,见第 9 章),实现于 [pcc/runtime/src/py_obj_gc.c](../../pcc/runtime/src/py_obj_gc.c):

```c
// pcc/runtime/src/py_obj_gc.c
int64_t py_gc_collect(void) {
    if (py_gc_collecting) return 0;
    py_gc_collecting = 1;
    py_gc_visit_runtime_roots(py_gc_visit_root_cb, NULL);
    py_gc_subtract_child_refs();
    py_gc_mark_reachable();
    int64_t freed = py_gc_sweep_unreachable();
    py_gc_collecting = 0;
    return freed;
}
```

被追踪对象挂在 `py_gc_head` 链表上,辅以 `py_gc_node_index` 指针哈希;阈值采用 CPython 形状(`py_gc_threshold0 = 700` 等)。`py_gc_collect()` 复刻 CPython 算法的骨架:把每个节点的 `gc_refs` 初始化为引用计数,`py_gc_subtract_child` 减去对象图内部边,`gc_refs > 0` 者即有外部引用,从它们出发 `py_gc_mark_reachable` 传播可达性;运行时根(帧根、续延根、调度器根、扩展状态根)经 `pcc_gc_visit_runtime_roots()` 注入同一次标记——这是第 10 章"根集只有一套"的 #0 侧消费端。之后是终结器、复活复查、断环、释放的序列(第 10 章 10.6 节)。

同一文件还承载 `gc` 模块的反射表面——`py_gc_get_objects()`、`py_gc_get_referents()`、`py_gc_get_referrers()`——它们建立在收集器自己使用的同一套按类型 referent 访问之上,用户可见的对象图与收集器的对象图因此不会各自漂移。

**移植关键决定。** 两条值得点名。其一,#0 默认不追踪帧根(`pcc_gc_should_track_frame_roots()`):引用计数已经维持局部变量存活,给每帧付登记成本没有收益——这是少数明示的后端差异,差异在"是否需要这份信息",不在语义。其二,环收集器与追踪后端消费**同一份**运行时根集而非各建一套,这让 10.7.2 那类"根注册缺失"在 #0 与 #1–#4 上至少是同一个缺口,而不是两个。

**专属不变式。** 环收集器不得收集裸引用计数已为 0 的被追踪对象——这种对象归一个正在执行中的 `py_decref` 所有(见下面的案例研究);`py_gc_collecting` 重入守卫使收集中的 `gc.collect()` 成为空操作。

**已知状态。** 默认后端、回滚参照、覆盖面最广的生产路径(`gc-backend-selection-matrix.md` 的明确决定);五后端自举矩阵下也是最快的一格。它不是终点:选择矩阵给 #0 留的积压清单包括环收集的自动配速(今天的阈值是静态的 CPython 形状)与更深的弱引用/终结器/复活策略对齐,且每一次共享运行时编辑都必须以 #0 为非回归参照——参照后端的首要义务是无聊地保持绿色。

**案例研究:rc==0 窗口与一个连环死锁(2026-05-29,`pcc1-threaded-explicit-gc-backend0-double-free-highscale.md`)。** 把 pcc1 线程闸门加压到"4 工作线程 × 200 迭代、主线程连发 100 次 `gc.collect()`"后,作为参照后端的 #0 红了:约 10/12 的运行以 SIGABRT 结束,签名是 `py_decref: refcount underflow` 断言或 malloc 双重释放。LLDB 显示崩溃线程停在帧槽重赋值的 `pcc_gc_release` 里,而主线程还在 `pcc_stop_the_world` 等待——崩溃瞬间收集器根本没在跑,说明对象死于更早的窗口。环收集器调试开关给出判决:`PCC_GC_DEBUG_LEAK_UNREACHABLE=1`(只算可达性、不释放)让 12 次运行零崩溃,`PCC_GC_DEBUG_FREE_TRACE=1` 抓到被释放对象的指纹 `rc=0, in_root=0`。根因:一个工作线程在 `py_decref` 把引用计数减到 0 之后、`py_gc_untrack` 之前停在安全点,对象以 rc==0 的状态留在追踪名单里;另一线程的 STW 收集把它判为不可达并释放;工作线程恢复后完成自己的释放——双重释放。修复是一行不变式:重算可达性时跳过 `refcount <= 0` 的节点(环垃圾的引用计数必然大于零,因为环内互指)。修完双重释放又暴露第二个 bug:`Lock.acquire` 的无界 `pcc_cond_wait` 不经过安全点,锁的释放者却停在安全点上,STW 永远凑不齐——修复为"timedwait;解锁;安全点;重加锁"的循环,且顺序一个都不能换(第一版把安全点放在持锁状态下,死锁换了个姿势回来)。两修复后独立复现 25/25 通过。这个故事也是调查纪律的展示:还有一个教训藏在中途——编辑 `py_runtime/src/*.c` 不会自动重建运行时归档,强制重建之前的所有插桩实验全部作废。

## 11.3 后端 #1:增量三色——参照 Lua 5.4

**参照系。** `gc-research/lua/lgc.c` 是 Lua 5.4 的单线程增量三色收集器:mark/atomic/sweep 状态机、`GCdebt` 步进债务、`gcpause`/`gcstepmul` 配速器(pacer);`lobject.h` 给出头部颜色位,`lstate.h` 给出每状态的配速字段。

**核心算法。** 三色直接放在对象头 flags 里(`PY_FLAG_GC_WHITE`/`GRAY`/`BLACK`,`py_internal.h`)。写屏障是 Dijkstra 前向式:黑色 owner 存入白色 value 时把 value 染灰:

```c
// pcc/runtime/src/py_gc_backend.c
void pcc_gc_store_ptr(PyObject *owner, PyObject **slot, PyObject *value) {
    if (pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR) {
        if (owner != NULL && (owner->flags & PY_FLAG_GC_BLACK) &&
            value != NULL && !(value->flags & (PY_FLAG_GC_BLACK | PY_FLAG_GC_GRAY))) {
            value->flags |= PY_FLAG_GC_GRAY;
            pcc_gc_gray_count++;
        }
    }
    PyObject *old = *slot;
    *slot = value;
    if (value != NULL) py_incref(value);
    if (old != NULL) py_decref(old);
}
```

配速器是 Lua 模型的直接移植:`pcc_gc_note_alloc()` 把分配字节累进 `pcc_gc_debt_bytes`;债务越过阈值(`PCC_GC_DEFAULT_DEBT_THRESHOLD` 为 64KiB,或 `live_bytes × (gcpause − 100) / 100`,`PCC_GC_PAUSE` 默认 1000)时,`pcc_gc_maybe_auto_step()` 用 `pcc_gc_budget_from_debt()` 换算出的预算执行一次有界步进(债务除以 `PCC_GC_WORK_BYTES = 64`,乘 `PCC_GC_STEPMUL`,上限 65536);步进完成后 `pcc_gc_discharge_debt()` 按处理量冲销债务。标记步进 `pcc_gc_step_trace_cycle_unlocked()`:游标沿对象注册表推进,遇灰对象就 `pcc_gc_trace_referents()` 染灰其子并把自己转黑;游标走完且灰计数为零时进入终局——`pcc_gc_finish_tracing_cycle()` 在 STW 边界下重扫当前根集、排空新灰,然后把仍为白色的对象打上 `PY_FLAG_GC_SWEEP_CANDIDATE`。这对应 Lua 的 atomic 阶段:增量标记期间根可以变,最终的白色裁决必须原子。

```c
// pcc/runtime/src/py_gc_backend.c
void pcc_gc_step(int64_t work_limit) {
    switch (pcc_gc_selected_backend) {
    case PCC_GC_KIND_INCREMENTAL_TRICOLOR:
        pcc_gc_step_incremental(work_limit);
        break;
    case PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR:
        pcc_gc_step_generational(work_limit);
        break;
    case PCC_GC_KIND_COLORED_RELOCATING:
        pcc_gc_step_relocating(work_limit);
        break;
    default:
        break;
    }
}
```

**移植关键决定。** 三处与 Lua 有意不同。其一,没有显式灰色链表(Lua 的 `gray`/`grayagain`):灰色只是 flags 位,步进靠游标反复扫对象注册表直到灰计数归零——实现简单、避免链表侵入对象头,代价是重扫;`grayagain` 的角色由终局 STW 重扫承担。其二,Lua 对表用回向屏障(`luaC_barrierback_`,把黑表重新染灰),pcc 统一用前向屏障(染灰新值),因为 pcc 的全部容器存储已经收口在 `pcc_gc_store_ptr()` 一个函数里,前向屏障在那里是一行判断。其三,自动步进**只标记不清除**:在自动步进里顺手清除既有候选的提案被调查否决(`gc-backend1-auto-step-sweep-debt.md`)——当时生成代码还没有把循环局部容器证明给追踪根栈,清了就是清活对象;直到 `gc-backend1-owned-local-frame-roots.md` 给拥有局部接上单槽帧映射根之后,清除仍只发生在显式收集的 STW 窗口里。另一个诚实的限制:`pcc_gc_maybe_auto_step()` 在线程模式下直接关闭,多线程下 #1 只在显式收集时推进。

**专属不变式。** 新分配对象带 `PY_FLAG_GC_FRESH_ALLOC`,在自动步进里按黑色对待一个周期(分配时序与未注册临时量的竞争防护),但显式 `gc.collect()` 拥有 STW 与稳定根集,新鲜对象必须按白色参与;周期不活跃且无请求时债务清零,防止债务在静默期堆积成一次巨步。

**已知状态。** README 状态行:production-test-green——颜色位、帧/模块/类根、写屏障、有界步进、清除、债务配速、显式收集、终结器/复活修复与对象注册表性能闸门全部通过;自举矩阵五格全过。选择矩阵把它列为"最现实的近期非默认挑战者"。

**案例研究一:端口独有的双重释放(`gc-backend1-pcc-py-runtime-collect-abort.md`,2026-05-07 审计系列)。** 第一次五后端自举矩阵审计(`bootstrap-five-gc-matrix.md`)给出一张难堪的表:15 个格子全部编译通过,运行时 #1 一列全部 SIGABRT——pcc0/pcc1/pcc2 三个编译器阶段、同一个分配翻腾加 `gc.collect()` 探针,全在打印收集结果之前死亡。而 C 运行时的 #1 闸门全部通过。根因在 pcc-Python 端口的一行多余代码:C 运行时只在 `pcc_gc_alloc()` 注册对象进追踪名单,端口却在 `py_gc_track()` 里又注册了一次,被追踪容器在对象名单里有两个条目;#1 清除释放对象时只摘掉一个,另一个悬挂条目随后把已释放内存当对象再释放一次。删掉端口的重复注册即修复。教训直接喂给了第 14 章的镜像纪律:**C 闸门绿不等于端口绿**,而能把这种差异在自举之前暴露出来的,正是逐后端全矩阵。

**案例研究二:保护机制吃掉了显式收集(2026-05-14,`gc-backend1-explicit-collect-sweep.md` 的 Update)。** `PY_FLAG_GC_FRESH_ALLOC` 落地后,#1 的生产闸门以另一种姿势回归:显式收集探针打印 `False 0`,所有 G1 环/终结器测试报零次终结器调用。新鲜对象保护对自动步进是对的,对显式收集是错的——刚分配就不可达的对象在 `gc.collect()` 里必须能死。修复引入显式追踪收集模式(`pcc_gc_begin_explicit_tracing_collect()` / `pcc_gc_end_explicit_tracing_collect()`):显式收集期间根播种把新鲜对象当白色。这个模式位后来还兼职了第 10 章讲过的重入守卫。

## 11.4 后端 #2:并发标记清除——参照 Go

**参照系。** `gc-research/go-greentea/`:`mgcmark.go`(并发标记工作线程、工作队列、工作窃取)、`mgcsweep.go`(并发清除、span 缓存)、`mwbbuf.go`(每 P 的 512 条目写屏障缓冲,批量刷入 GC 工作队列)。

**核心算法。** #2 与 #1 共享同一个三色追踪核心,差异全在"谁在什么时候推进"。`pcc_gc_cms_maybe_start_worker()` 启动一个分离的 pthread 工作线程(`pcc_gc_cms_worker_main`),工作通过一个 256 槽的有界全局队列传递,票据有两种:正数票据是分配字节数,工作线程按 `PCC_GC_WORK_BYTES` 换算成预算执行一段有界标记;负数票据编码一个灰对象指针,工作线程直接追踪它。写屏障是 `mwbbuf.go` 模式的缩尺移植:标记进行期间,存入的未染灰值先染灰,灰票据记入线程本地的 32 条目缓冲(`pcc_gc_cms_buffer_gray()`),缓冲满或步进开始时在**图锁之外**成批刷入队列(`pcc_gc_cms_flush_wb_buffer()`)——避免持图锁时再抢队列锁。分配方在债务越限时执行 mutator 协助(`pcc_gc_cms_maybe_assist()`,对应 Go 的 assist 机制,计入 `pcc_gc_cms_mutator_assists`)。

**移植关键决定。** 用一句诚实的话概括:**并发在调度维度,不在标记的每一刻**。今天的工作线程对每张票据先 `pcc_stop_the_world()` 再拿图锁,标记切片实际跑在 STW 窗口里;Go 式的"mutator 与标记真正并行"需要逐对象原子染色与终止协议,pcc 选择先用全局图锁加 STW 切片换取一个 TSan 干净的正确基线,再逐切片逼近参照。同理,屏障只染灰**新值**(Dijkstra 插入屏障),没有移植 Go 混合屏障的"同时染灰被覆盖旧值"——终止正确性由 #1 同款的终局 STW 根重扫兜底。工作线程拿到图锁后还要复查当前后端仍是 #2:进程可以在工作线程存活期间切换后端,过期票据必须被丢弃而不是去摸对象图。清除维持 STW,是否长成 Go 式并发 span/对象清除被记录为一个显式的待决策项,不是默认承诺。

**专属不变式。** 不持图锁不得触碰对象图;等锁路径必须调用 `pcc_thread_safepoint()`(等锁的工作线程必须能配合 STW 握手);后端切换时 `pcc_gc_cms_stop_worker()` 停止并 join 工作线程。

**已知状态。** README:correctness-green threaded prototype;`gc-backend2-3-production-verdict.md` 的结论是"对 pcc 当前 GIL 形状的线程运行时是生产可用的——保守、TSan 干净、带缓冲写屏障,但不是 Go 工作缓冲/span 清除的等价克隆",并用公开的裁决遥测(`pcc_gc_backend2_worker_buffer_score()`、`pcc_gc_backend2_production_score()`)背书——原生压力探针必须让这些计数器动起来,闸门才算过。选择矩阵把它定位为线程正确性候选,非默认候选;缺口——完整的 Go 式工作缓冲/排空模型与并发清除的决策——被如实写进积压清单,而不是被措辞掩盖。

**案例研究:分离工作线程撞上无锁对象图(`gc-backend2-cms-worker-instability.md`)。** 全量 GC 闸门反复在同一处失败:`cms_probe.out` 以 SIGSEGV(-11)退出;聚焦重跑又暴露出第二个症状——工作线程追踪遥测恒为零。根因:分离的工作线程在遍历与染色对象图的同时,mutator 正在插入与删除对象节点,两者之间没有任何同步。修复引入了后来全体追踪后端共用的运行时图锁:对象注册、对象释放、屏障染色、mutator 步进、工作线程追踪全部收口到同一把锁下,等锁循环带安全点。值得记下的克制:修复**没有**改队列算法——工作线程在锁竞争时不重排队票据,保持单生产者/单消费者假设不被悄悄破坏。修后全量闸门从崩溃变为 168 passed, 17 xfailed。这个 bug 的形状(共享可变结构 + "应该没事吧"的无锁并发)是教科书级的,值得写进书里的原因是它的修复路径:先用锁买正确性,把无锁优化留给有了 TSan 闸门之后的未来。

## 11.5 后端 #3:分代小堆——参照 OCaml

**参照系。** `gc-research/ocaml/minor_gc.c`:域本地小堆的指针碰撞分配器、`oldify_one` 的转发式晋升(对象头写转发指针,`Field(v, 0)` 跟随)、`caml_ref_table` 记忆集表。`gc.h` 与 `major_gc.h` 给出接口与大堆结构。

**核心算法。** 对象头 flags 带 `PY_FLAG_GC_YOUNG` / `PY_FLAG_GC_OLD` 两代标记,分配时默认 young。整条数据通路长这样:

```text
分配:  pcc_gc_alloc ─→ pcc_gc_try_minor_alloc ─→ [minor arena 块,线程私有,碰撞分配]
                          (仅 #3,size ≤ MINOR_ALLOC_MAX)        │ 块满
写入:  old owner ← young value ─→ 记忆集(owner 粒度,            ▼
                  PY_FLAG_GC_REMEMBERED)        pcc_gc_minor_collect_reset
                                                       │
晋升:  标量白名单 ──拷出 arena──→ malloc 老年代 + 转发条目 + 急切槽改写
        指针承载类型 ──原地翻 flags──→ OLD(不搬动)
回收:  块 live_objects 归零 ──→ pcc_gc_minor_release_block(整块释放)
```

小堆是真实的碰撞分配 arena:`pcc_gc_alloc()`(`py_obj.c`)先尝试 `pcc_gc_try_minor_alloc()`——仅后端 #3、仅尺寸不超过 `PCC_GC_MINOR_ALLOC_MAX`(默认 16 字节,16 字节对齐)的对象走 arena,块大小 `PCC_GC_MINOR_HEAP_SIZE` 默认 32 MiB,当前块线程私有(`owner_thread_id`)。块写满触发 `pcc_gc_minor_collect_reset()`,执行一次有界晋升步进。写屏障是分代经典款:老年代 owner 存入新生代 value 时,owner 进入记忆集(`pcc_gc_backend3_remember_owner_unlocked()`,置 `PY_FLAG_GC_REMEMBERED`)。晋升步进 `pcc_gc_step_generational_promotion()` 的顺序:先晋升四类根(帧、调度器、TLS 异常、扩展模块状态),再排空记忆集 owner,最后扫表晋升全部 young。晋升本身有两条路:`pcc_gc_generational_oldify_copy()` 对**标量白名单**类型(`pcc_gc_relocate_copy_supported_tag()`:int/float/str/complex/bytes/bytearray/CpyHandle——全部无 pcc 指针槽)把对象从 arena 拷出到 malloc 老年代、安装转发条目并把源标记为不活动;指针承载类型不拷贝,原地翻 flags 晋升。拷贝晋升后,`pcc_gc_promote_owner_referents()` 按类型**立即改写** owner 的槽指向新址(incref 新、decref 旧),读屏障只兜没赶上的懒路径。arena 内存按块回收:`pcc_gc_note_object_freeing()` 对 `minor_block != NULL` 的节点不单独 free,块的 `live_objects` 归零时 `pcc_gc_minor_release_block()` 整块释放;`pcc_gc_release()` 对"arena 出身、已晋升、rc 已归零"的对象有一个专门的跳过分支——arena 内存不属于 malloc,不能走普通释放。

**移植关键决定。** 三个。其一,记忆集是 **owner 粒度**而非 OCaml 的槽粒度表:记住"这个老对象写过新生引用",扫描时重访它的全部槽——条目更少、去重靠一个 flags 位,代价是重扫宽度;`gc-backend3-remembered-slot-rewrite.md` 后来证明扫描必须顺手改写它刚追踪过的槽,否则直接读裸槽的运行时代码会在读屏障来不及自愈的窗口里看到旧地址。其二,**急切槽改写优先于懒读屏障**,原因同上:pcc 的 C 运行时大量直接读容器内存,不是每次读都过 `pcc_gc_load_ptr()`。AGENTS.md 把这条固化为规则:急切改写代码必须长在 `py_gc_backend.c` 的每类型晋升代码旁边,不许另起炉灶。其三,**只拷贝标量**:指针承载对象的搬动牵出全部容器/根/挂起帧的改写义务,#3 把这份义务留给 #4,自己用"原地晋升"绕开——这是两个后端之间清晰的复杂度分界。

**专属不变式。** 构造函数必须保全 young/minor 头 flags(`gc-backend3-pcc-py-constructor-header-flags.md`);类元数据的借用槽(`methods[i].func`、`del_method`)必须参与晋升,但不得为此把它们塞进 #4 的通用追踪面(借用槽对追踪意味着重复计数);挂起的生成器帧槽(`PyGenObject.frame` 指向的堆帧列表)同样参与记忆集与改写(`gc-backend3-suspended-generator-frame-slot-rewrite.md`)。

**已知状态。** 2026 年 8 月的生产所有权由 freestanding pcc-Python 单轨承担,C 路径保留为差分 oracle;线程本地 arena 的有限所有权闸门已绿。开放项仍包括跨域记忆集共享、更广的线程化对象索引同步和长期性能证明。选择矩阵列它为中期吞吐候选;2026-05-07 审计的分配翻腾探针里它是唯一真正动了分代遥测的非默认后端(`minor_collections=18`)。

**案例研究:四个字节的字符串,两个不变式(`gc-backend-selection-matrix.md` 闭合段,闸门日期 2026-05-17)。** 选择矩阵收尾时,#3 的 pcc1 矩阵格子崩在 `IRBuilder.call` 里:`_opname_of()` 每次调用都新切一个 `"call"` 短字符串,这个 16 字节级的对象正好落进 minor arena,却被存进长生命周期的 IR 元数据——一条教科书式的老→新边,在当时的记忆集覆盖之外。记录的修复是让 `_opname_of()` 返回稳定的操作码字面量:编译器侧消灭这条边,而不是当场为它扩展运行时覆盖。同一轮闭合还抓到第二个缺口:类元数据晋升漏掉了借用的 `methods[i].func` 与 `del_method` 槽——C 与端口随后同步补上(`gc-backend3-class-metadata-slot-rewrite.md`)。两个 bug 同一形状:**分代正确性的边界恰好是"谁可能持有新生引用"这张清单**,清单漏一行,就有一类对象在晋升后悬挂。

## 11.6 后端 #4:着色重定位——参照 GenZGC

**参照系。** `gc-research/zgc/` 是 OpenJDK `jdk-27+21` 的钉定参照包(2026-05-14 取样,`MANIFEST.json` 记录上游路径与 SHA-256):`zForwarding*`/`zRelocationSet*`(转发表与重定位集)、`zBarrier*`(装载屏障)、`zStoreBarrierBuffer.*`/`zRemembered*`(分代存储屏障与记忆集)、`zPage*`(页分配)、`zGeneration*`(年轻/老年代)。

**核心算法。** ZGC 的目标是移动对象而不长暂停;pcc #4 的移植围绕四张并行的账本。**重定位集**:被选中疏散的对象进入 `pcc_gc_relocation_set` 并打上 `PY_FLAG_GC_RELOCATION_CANDIDATE`。**转发表**:`pcc_gc_relocate_copy()` 按类型拷贝载荷(`pcc_gc_relocate_copy_payload()`,受 `pcc_gc_colored_relocate_copy_supported_tag()` 白名单约束——list/tuple/dict/set、实例与用户类、函数/迭代器/生成器/协程/续延、异常/类/弱引用/线程/任务等,每个类型的拷贝代码显式处理自己的指针槽与记忆集重定向),成功后 `pcc_gc_install_forwarding()` 写入 from→to 侧表;读屏障(第 10 章 10.4 节)据此自愈槽位。**稳定身份侧表**:`pcc_gc_object_id()` 在首次取 `id()` 时分配一个与地址解耦的标识,搬动后 `id()` 不变——身份语义不许被搬动出卖。**分代切片**:分配默认 young(`pcc_gc_note_object_allocated_sized()`),老→新存储经 store buffer 入队(条目持有 value 的引用),`pcc_gc_step_colored_remembered_roots()` 有界批量排空,`pcc_gc_step_colored_generation_aging()` 把存活 young 翻成 old——对应 GenZGC 的年龄推进。承载这一切的是合成 ZPage:`pcc_gc_backend4_try_zpage_alloc()` 在按代与尺寸级(small ≤ 4 KiB、medium ≤ 64 KiB、large)组织的页内碰撞分配,页有记忆槽位图与 512 字节跨度卡(card),疏散按页选择候选(`pcc_gc_backend4_select_relocation_pages()`)并排空(`pcc_gc_backend4_evacuation_page_drain()`)。`pcc_gc_step()` 里 #4 的一步是这套流水线的串联:排空 store buffer→年龄推进→页疏散→必要时 STW 追踪周期。

**移植关键决定。** 最大的一条:**没有着色指针,没有多重映射**。ZGC 把颜色编进指针元数据位并靠多重映射实现"同页多视图";pcc 的指针要原样穿过 C 扩展 ABI、要支撑 `is` 与 `id()`,不能动指针位。颜色与候选状态于是落在头 flags 与侧表里,`pcc_gc_step()` 中的注释直说:正因为用侧表候选位替代多重映射,追踪周期的相位切换保持 STW。第二条:**白名单宁缺毋滥**——只有载荷拷贝代码被逐类型写出并测过的类型才可搬动,不在白名单上的类型永远不进重定位集;`PY_FLAG_GC_PINNED` 对象拒绝安装转发(`pcc_gc_relocation_pin_rejects` 计数)。第三条:**重定位拷贝单次有效**(`gc-backend4-relocate-copy-single-forward.md`)——已转发的源拒绝再拷贝,成功拷贝即从重定位集摘除,否则同一源的第二份拷贝会顶掉转发目标并复制稳定 ID。

**专属不变式。** 任何释放路径在释放前必须先经读屏障自愈(见案例研究);`pcc_gc_backend4_verify_no_old_addresses()` 提供"无旧地址残留"的可验证断言;碎片分数(`pcc_gc_backend4_fragmentation_score()`)定义为活跃疏散债务——待搬条目加待自愈的转发条目,稳定 ID 条目明确排除在外(它们是身份元数据,不是债务)。

**已知状态。** README 的状态行最长也最诚实:转发/读屏障/容器搬动/调度器根自愈/分代切片/页类遥测是 production-facing 的,但真正的 GenZGC 年轻老年代策略、真实页疏散驱动的碎片策略、原生句柄(Thread/File)搬动协议、端口线程化镜像刷新仍然开放。选择矩阵的定位:长期低暂停候选,因复杂且未完而非默认。它也是自举矩阵里最慢的一格(见 11.7)。

**案例研究一:队列销毁路径绕过了读屏障(`gc-backend4-scheduler-queue-free-read-barrier.md`)。** 调度器队列的 pop 路径规规矩矩地经 `pcc_gc_load_ptr()` 读出条目值;但**销毁**一个还有未弹出条目的队列时,释放路径直接 `pcc_gc_store_root(..., NULL)` 清槽——如果队列里躺着一个已被搬走的对象,释放的是过期的源指针。探针打印 `1, 1, 0`:pop 自愈正常,free 路径零次屏障转发。修复让 `pcc_gc_scheduler_queue_entry_free()` 镜像 pop:条目还在调度器根上时先经读屏障装载,再注销、再清槽。教训是一条可推广的不变式:**对搬动收集器,"释放"也是一次读**——每条 teardown 路径都要像热路径一样过屏障,而 teardown 路径恰恰是测试最少走到的。

**案例研究二:被采样否决的"读屏障税"(2026-06-10,`gc-frame-index-entry-pool-perf.md` 的 Update)。** gc4 长期是自举矩阵最慢的一格,流传的解释是"读屏障税压在前端工作上"。对 gc4 stage2 代码生成窗口做 70 次采样捕获后,这个假设被正式记为 DENIED:`pcc_gc_load_ptr` 自身样本只占非等待 CPU 的约 3.7%,而 GC 辅助函数合计约 41%,其中**索引表维护**(对象索引、ptr 索引、帧索引的插入/查找)约 27%——为"随时可搬"维持的每分配/每追踪记账,才是 gc4 的真实税基。随后落地的低风险子集(插入路径单次哈希加装载因子 3/4→1/2)让 gc4 的单文件自举闸门同日背靠背从 148.31s 降到 121.07s(−18.4%,2026-06-10)。教训写在调查文件里:五百毫秒以上的优化决策必须建立在采样证据而不是直觉叙事上——这一章引用的每个性能数字都附带日期与测量含义,原因即在此。

## 11.7 选择矩阵:为什么默认仍是 #0

`gc-backend-selection-matrix.md` 把"选默认"作为一个显式决定关闭,而不是悬置:**#0 维持默认**。排序的全文是:#0 默认(参照路径、最广的真实自举与语言覆盖、最少的策略不确定性);#1 最佳近期非默认候选(最简单的非引用计数算法面);#3 中期吞吐候选(arena、改写、根覆盖都已双轨);#2 线程正确性候选而非默认;#4 长期低暂停候选,因复杂未完而非默认。回滚策略就是不设 `PCC_GC_BACKEND` 或设 0;任何后端改动共享运行时代码都必须保持 #0 闸门绿;未来若换默认,CI 必须双跑新默认与 #0 至少一个发布周期。

数字按声明卫生给出,只作相对量级:

- **分配翻腾探针**(2026-05-07 审计,pcc2 编译的 20 万次分配加显式 `gc.collect()`,三次取中位):#0 0.146s 基线;#1 1.11×;#4 1.08×;#3 1.60×;#2 1.79×。含义有限:默认阈值下 #4 根本没触发搬动(1.08× 是"记账但不干活"的成本),#2 的 1.79× 里有协助与队列开销;这是单探针,不是吞吐画像。
- **自举矩阵**(单作业 stage2,2026-06-04 基线):gc0 约 107s;gc3 226s;gc4 310s。索引条目池化后(同一调查,2026-06-04 验证):gc3 167s、gc4 230s(各约 −26%);插入单遍化与装载因子调整后(2026-06-10,同日背靠背):gc4 单文件闸门 148.31s→121.07s。全矩阵(五后端 × 三阶段)的当日墙钟在 426–520s 之间波动,跨日比较无意义——调查文件明确把"目录跑进 200s"这种含混目标作废,换成"矩阵墙钟 + 各后端 stage2 `compile_python_total`,同日背靠背"两个可比指标。
- 自举负载的**含义**:深递归、海量短命对象、帧根登记密集——它度量"收集器让 mutator 付了多少税",不度量暂停分布或碎片演化;后者是 G-track 长跑基准的职责,本文写作时仍在建设。

矩阵的另一个产出容易被忽略:它本身是台仪器。15 格(3 编译器阶段 × 5 后端)的第一次全跑当场抓出 #1 的端口独有崩溃(11.3 节);每个后端单独一个自举文件意味着任何运行时改动的回归会精确指认是哪个后端的哪一阶段先红。2026-05-07 审计的遥测列还展示了"同一探针、五个后端"在诊断上的价值:同一个 2 万次分配的矩阵探针下,#0 报零次追踪步进(设计如此),#1 七次有界步进、债务 120,#2 八十一次步进且协助工作可见,#3 二十次步进且 arena 遥测在动,#4 一次步进、零次搬动——一个程序给出五份代价签名,每份都能对照"该后端的算法在默认阈值下应该做什么"核验。当某个后端的签名在没有对应代码改动的情况下变形,那就是回归在自首。

最后把状态词汇与后端对上号。平等契约的词汇(第 10 章 10.1 节)规定:触及对象/引用/生存期的特性,五后端全过共同契约测试才是 `DONE_STRONG`,只过 #0 是 `DONE_WEAK`,过一部分是 `BACKEND_PARTIAL`。这套词汇与本章的后端状态是两个正交的轴:前者描述**某个特性**横跨五后端的覆盖,后者描述**某个后端**纵向离参照系的距离。一个特性可以 `DONE_STRONG`(比如两节点环的终结器顺序,五后端契约套件全部通过)而 #2 仍是 prototype;反过来 #0 是生产默认,但"值类指针载荷在 #4 下的根安全"这块契约砖在本文写作时仍是 active 的开放调查。把两个轴混为一谈,是 GC 状态讨论里最常见的声明卫生事故。

## 11.8 历史与教训

各后端小节已经承载本地案例研究。为了让本章的"历史与教训"仍保留明确案例研究结构,这里把两条跨后端调查重新抽成命名案例研究。

### 案例研究一:矩阵作为差分探测器

矩阵暴露的是"差异",根因常在共享层之外的单侧。11.3 的端口双重释放(C 绿、端口红)、第 10 章 10.7.2 的异常根缺失(#0 绿、#1–#4 红)、11.5 的 `_opname_of` 短串(只有 #3 红)——三个故事的失败形状都是"某些后端红",而根因分别在端口镜像、前端所有权低层化、编译器元数据生命周期,没有一个在"出红的那个后端"的算法里。这正是平等契约的诊断价值:五后端是五个不同灵敏度的探测器,哪几格红、哪几格绿的组合本身就是根因的指纹。调查纪律(一次一个提案、跑到 CONFIRMED/DENIED)负责把指纹读对。

### 案例研究二:性能假设必须带证据等级

性能工作的每一步都要有证据等级。这一章引用的三次性能干预给出一条完整的方法论弧线:LIFO 影子栈凭直觉替换帧索引哈希——DENIED,gc3 退化到 900s 超时(访问模式是槽粒度非 LIFO 的,第 10 章 10.7.1);"读屏障是 gc4 主要成本"凭印象流传——DENIED,采样显示真实税基是索引维护(11.6);索引条目池与插入单遍化凭采样证据落地——CONFIRMED,且每次都用五后端字节同一(pcc2==pcc3)与同日背靠背计时验收。同一周的另一条更正(2026-06-10,同一调查文件)把这条纪律延伸到了正确性断言:有人只读调用点就推断 `py_list_set` 不做 incref,据此设计的"零引用计数流量"排序在 `__del__` 探针下漏出每元素一个引用——**先读屏障助手的源码,再断言它的契约**。性能与正确性共用同一条规则:证据,然后才是结论。

## 11.9 小结

五个后端是同一份语义契约下的五种代价结构:

1. **#0** 引用计数加 STW 环收集(CPython 形状):确定性回收、最低记账、默认与回滚参照;代价是环依赖收集器、暂停随追踪集增长。
2. **#1** 增量三色(Lua 形状):分配债务配速的有界步进,前向写屏障,STW 终局裁决;自动步进只标记,清除留给显式收集。
3. **#2** 并发标记清除(Go 方向):分离工作线程、有界票据队列、线程本地缓冲写屏障、mutator 协助;今天的标记切片仍在 STW 窗口加图锁下——并发在调度维度,参照系的逐刻并发是未竟方向。
4. **#3** 分代小堆(OCaml 形状):线程私有碰撞 arena、owner 粒度记忆集、标量拷贝晋升加指针类型原地晋升、急切槽改写;arena 按块回收。
5. **#4** 着色重定位(GenZGC 方向):重定位集、转发侧表、自愈读屏障、稳定 ID 侧表、store buffer 分代切片、合成 ZPage 页疏散;以侧表换着色指针,以白名单换搬动覆盖,以 STW 相位切换换多重映射。

默认是 #0,这是一个有记录、有回滚策略、有挑战者排序的显式决定。五个后端的状态词汇全部模式标注:production-test-green、threaded prototype、production-facing focused gates、advanced surface——每个词组都对应 README 与选择矩阵里一行可复核的闸门清单,这正是第 1 章承诺的"每个声明说清它证明了什么、没证明什么"。

## 练习

1. **读源码验证**:通读 `py_gc_backend.c` 的 `pcc_gc_step()`,为五个后端各写出"一次步进做了什么"的清单(#0 在此函数里做什么?为什么?)。对照 11.2–11.6 节核对你的清单。
2. **参照对照**:读 `gc-research/lua/lgc.c` 的 `luaC_step` 与 `lgc.c` 中 `GCdebt` 的换算,对照 `pcc_gc_budget_from_debt()` 与 `pcc_gc_discharge_debt()`,指出 pcc 配速器相对 Lua 的两处刻意简化,并论证各自的代价在自举负载下是否可见。
3. **读源码验证**:读 `pcc_gc_generational_oldify_copy()`,解释:(a) 为什么 `to_h->refcount` 先置 1、载荷拷贝完成后又置 0;(b) 为什么 `PY_FLAG_GC_PINNED` 直接拒绝;(c) 源对象被 `pcc_gc_mark_forwarded_source_inactive()` 标记后,它的 arena 内存何时、由谁真正释放。
4. **白名单审计**:对比 `pcc_gc_relocate_copy_supported_tag()` 与 `pcc_gc_colored_relocate_copy_supported_tag()` 的集合差,选三个只在后者中的类型,在 `pcc_gc_relocate_copy_payload()` 里找到它们的拷贝分支,列出每个分支为"指针槽搬家"额外做的事(引用计数、记忆集重定向、索引更新)。
5. **设计权衡论证**:11.4 节说 #2 的"并发在调度维度,不在标记的每一刻"。假设要把工作线程的每票据 STW 去掉:列出至少三个必须新增的机制(提示:染色的原子性、终止协议、与 #1 共享的 `pcc_gc_trace_cursor` 的归属),并论证 Go 混合屏障与 pcc 现有的"终局 STW 根重扫"哪个更适合作为第一步,为什么。
