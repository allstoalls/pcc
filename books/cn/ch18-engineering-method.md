# 第 18 章 工程方法论:测试、调查与声明卫生

一本编译器书以方法论收尾,不是出于礼貌。pcc 的多数 bug 不是解析错误,而是语义错误:表达式组合、低层化(lowering)到 IR、再被真实程序锤打之后才显形,第一现场往往离根因隔着好几个子系统。在这样的系统里,"怎么测、怎么查、怎么说"不是工程卫生的附属品,而是设计本身的一部分——自举(bootstrap)不动点(fixed point)与五 GC 矩阵既是产品特性,也是这个仓库最重要的两台测量仪器。本章把仓库里成文的方法论逐一摊开:闸门(gate)体系与基线文件、调试手册的十二条技法、调查工作流的证据链纪律、声明卫生(claim hygiene)的模式标注规则、以及三次有案可查的"假信心"测量事故。所有内容都落在真实文件上:[AGENTS.md](../../AGENTS.md)、[docs/debugging-playbook.md](../../docs/debugging-playbook.md)、[docs/investigation-workflow.md](../../docs/investigation-workflow.md)、[goal-prompt.md](../../docs/goal/goal-prompt.md) 与 [docs/investigations/](../../docs/investigations) 下的调查记录。

## 本章导读:方法论服务于声明卫生

这一章不是额外的工程礼仪,而是前面所有设计能成立的保护层。pcc 的每个大目标都有容易误判的局部成功,所以必须用模式标注、最小复现、真实项目确认和基线闸门把声明钉住。

- 测试不是越多越好,而是要覆盖刚刚改变的语义边界。
- 调查文档不是事后记录,而是防止同一类失败被反复猜测。
- claim hygiene 的目的很直接:让读者知道一个结果到底证明了什么,没有证明什么。

## 18.1 问题与设计空间:为什么方法论是设计的一部分

先描述这个仓库的真实工况。第一,因果链极长:stage2 的一次段错误,根因可以是前端某个按名缓存没有在生成器分支上重置(第 9 章案例研究二);Lua 排序的一次偶发失败,根因可以是 `^` 表达式丢掉了无符号元数据(本章 18.7)。第二,工作树由多个 agent 与人类共享,聊天记录会消失,而"上一个人已经排除了什么"恰恰是最贵的信息。第三,系统的声明天然容易漂移:同一句"支持 X",在 host pcc 与 pcc1、libpython 与 no-libpython、LLVM 后端与 self 后端之间,可以指八件完全不同的事。

设计空间里有三类回应。其一,**靠规模**:每次改动跑全量测试。这个仓库的全量套件以分钟计,自举闸门以百秒计,把它当内循环会让迭代速度塌缩,而且全量绿也回答不了"这个声明在哪个模式下成立"。其二,**靠人**:依赖资深维护者的记忆与评审。在多 agent 并行、会话即焚的工况下,记忆不是存储介质。其三,**把方法论物化成仓库制品**:状态写进可测试的 JSON 基线,历史写进结构化的调查文件,诚实写进可引用的声明卫生表,流程写进 [AGENTS.md](../../AGENTS.md) 的强制小节。pcc 选了第三条,且走得很彻底——[tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 的注释直接写着它是 Issue 1 的权威状态,[AGENTS.md](../../AGENTS.md) 则明文规定这些 JSON 是事实源,历史跟踪文档可以滞后。

在这个立场下重新看两件"产品特性",会发现它们首先是方法论装置:

**不动点是全系统差分测试。** `pcc0 -> pcc1 -> pcc2 -> pcc3` 的链条要求编译器复现自己:语义、运行时、代码生成、对象模型、后端、诊断中任何一处的不一致,都会以阶段间分歧的形式暴露。[AGENTS.md](../../AGENTS.md) 把它说破:不动点"不只是字节比较",而是各子系统连贯性的证据。作为测试,它有普通单测不具备的性质——输入是整个编译器自身,任何"只在大程序上出现"的语义 bug 都在它的打击面内;而 pcc2/pcc3 在 Mach-O 签名规范化后必须字节一致([tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json)),把判据压到了无法讨价还价的程度。差异不允许绕过,只允许分类:语义 / IR 文本 / 类布局 / 对象模型 / 后端非确定性 / 链接元数据 / 仅性能 / 诊断([AGENTS.md](../../AGENTS.md) 义务 5)。

**五 GC 矩阵是同一契约的五个独立观察者。** 五个后端(引用计数+循环、增量、并发、分代、重定位)消费同一份槽位追踪契约(`py_obj_visit_slots` / `py_obj_update_slot` 与根、帧、原生句柄注册)。生产平等规则([goal-prompt.md](../../docs/goal/goal-prompt.md) G-track)禁止任何后端靠弱化终结器、弱引用、复活、挂起协程帧或值载荷语义取胜;于是每个对象图错误有五次被不同算法抓住的机会——前端漏注册一个 GC 根,在后端 #0 上可能被引用计数掩盖,在 #4 的重定位下立刻变成悬垂指针。`tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` 把"五个后端各自跑通三阶段自举"固化成一文件一后端的闸门,正是把这台仪器接到不动点那台仪器上。

这就是本章的立场:方法论不是围绕设计的脚手架,而是设计的延伸。后文各节是这台仪器组的使用手册。

## 18.2 闸门体系:基线即状态

[AGENTS.md](../../AGENTS.md) 的 Testing & Definition of Done 小节给出语义修复的最低四步:加聚焦回归测试;确认原始真实场景修复;跑覆盖被改子系统的聚焦闸门;凡涉及提交级完成,必须通过自举闸门。完成定义(Definition of Done)是一张清单:最小重现通过、原始集成场景通过、临时调试残留清除、回归测试入 [tests/](../../tests)、聚焦闸门通过(若跳过全量,必须说明跑了哪个子集、为何足够)、要求的自举验证跑过。注意其中的修辞:全量套件默认是**可选**的,聚焦闸门是**强制**的——这是对"靠规模"路线的明确拒绝,代价是每次改动必须先回答"我动了哪个子系统、它的敏感闸门是哪几个"。

闸门分层。最内层是**聚焦回归**,如 [tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) 之于符号性、[tests/python/test_return_ownership.py](../../tests/python/test_return_ownership.py) 之于返回所有权。中层是**子系统闸门**,[AGENTS.md](../../AGENTS.md) 为高危路径列了清单:Lua 编译与运行时对照、SQLite 集成、`llvm_capi` 对 `llvmlite` 的奇偶校验等。最外层是两组**仓库级契约闸门**,它们的共同特点是断言对象不是行为而是**状态文件**:

```text
tests/bootstrap_gate_baseline.json   自举权威状态:无阶段链接 libpython;
                                     pcc2/pcc3(签名规范化后)字节一致必须保持
tests/fallback_baseline.json         no-libpython 回退棘轮:多文件回退总数
                                     已为零且必须保持为零;逐模块计数作为
                                     诊断性棘轮防止动态习语回潮
```

对应的测试是 [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) 与 [tests/python/test_fallback_baseline.py](../../tests/python/test_fallback_baseline.py) / [tests/python/test_ir_py_fallback_baseline.py](../../tests/python/test_ir_py_fallback_baseline.py):

```python
# tests/python/test_fallback_baseline.py
def test_fallback_baseline_ratchet():
    baseline = json.loads(pathlib.Path("tests/fallback_baseline.json").read_text())
    current = count_fallback_routes()
    assert current <= baseline["max_allowed_fallbacks"]
```

同时,[scripts/goal_state.py](../../scripts/goal_state.py) 用机器可执行命令强化任务板验证:

```python
# scripts/goal_state.py
def validate_task_board(board_path: str = "docs/goal/task-board.yaml") -> int:
    tasks = load_tasks(board_path)
    for task in tasks:
        validate_task_schema(task)
    print(f"OK: {len(tasks)} tasks validated")
    return 0
```

在测试基础设施层面,[tests/conftest.py](../../tests/conftest.py) 通过通用的 fixture 隔离测试环境与缓存状态:

```python
# tests/conftest.py
@pytest.fixture(autouse=True)
def _isolate_env_and_caches(tmp_path_factory):
    old_env = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(old_env)
```

"棘轮"(ratchet)一词值得停一下:这类闸门不断言一个固定值,而断言**单调性**——当前回退数不得劣于基线。它把"进展"本身变成了可回归测试的量,任何让回退面回潮的改动会被机械地拦住,而不依赖评审者记得上个月的数字。

闸门之上是声明强度的分级。[goal-prompt.md](../../docs/goal/goal-prompt.md) §0.1 规定 `DONE_STRONG` 的全部前提:实现必须是通用机制而非硬编码特例;有聚焦回归与负向/边界测试;涉及 pcc1 的声明要有 `PCC_HOST_PYTHON=/bin/false` 级别的无宿主证据;涉及运行时/GC/根/对象生命周期的声明,自举证据必须是全五 GC 的——仅后端 #0 通过不构成强声明;性能声明要有 IR 形状闸门加运行期基准。§0.2 进一步规定证据格式:命令必须完整可复现,没跑就写 `not run` 并说明原因,**禁止写 "should pass"**;凡触及 pcc1、self 后端、no-libpython、运行时/GC、共享低层化路径的切片,完成摘要必须带一行 `bootstrap: passed|failed|not run`,缺这一行本身就是验证缺口。

## 18.3 调试手册:十二条技法

[docs/debugging-playbook.md](../../docs/debugging-playbook.md) 是任务条件触发的强制程序:一旦在调试失败,先读它再猜。十二条技法编号稳定(§1–§12),被 [AGENTS.md](../../AGENTS.md) 与调查文件直接引用。逐条过一遍,每条配仓库内的着力点。

**§1 先让失败确定化。** 不要从通读 `c_codegen.py` 开始。固定随机种子、用常量替换文件系统/时间输入、`-n0` 关掉 xdist、隔离单个测试文件。若失败是随机的,消除随机性就是第一项工作。18.7 的 sort.lua 案例研究里,第一个有效动作是 `math.randomseed(15)`,不是读代码。

**§2 用同源参照分离"程序怪"与"编译器错"。** C 侧参照系统编译器,Python 侧参照 CPython,`llvm_capi` 参照 `llvmlite`,自举阶段分歧参照两个 JSON 基线。同一份源码、两个工具链、不同行为——bug 才归属编译器。

**§3 llvmlite 作为 llvm_capi 的神谕。** `PCC_USE_LLVMLITE_C=1` 在同一最小重现上切换 IR 构建器实现,比较编译结果、运行结果乃至 IR 文本。它对缺失的 `IRBuilder` 操作、常量 `gep`/`bitcast`、不透明指针语义特别有效;但它不是预处理器、fake-libc 或解析器的神谕——两个后端同时失败,bug 在后端层之上。

**§4 短回退/IR 痕迹只当定位器。** 在指认根因前必须确认四件事:所在的 `define` 函数、实际的 `py_cpy_*` 调用指令、helper 实参来源、产生它的源表达式。只有短上下文时,结论必须写成 "candidate, unconfirmed"。这是对"看了两行 trace 就补丁"的明令禁止。

**§5 分阶段缩小重现。** 失败的集成测试 → 更小的脚本/输入 → 调用同一内部路径的小 harness → 纯 C/Python 表达式。缩小花掉的时间会数倍偿还。

**§6 用替换验证假设,不只用目视。** 把嫌疑函数拷进临时 harness,一次换回一个真实 helper、逐步恢复分支。比盯五百行 IR 快。

**§7 排除 harness 自身的错误。** 这一条全是血泪清单:zsh 里 pytest 节点 id 的 `[ ]` 必须引号;macOS 上 `multiprocessing` spawn 与 `<<'PY'` 标准输入不相容;清单文件会过期,先用**当前** harness 重跑再怀疑编译器;改了语法/词法之后必须升 [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py) 里的 PLY 缓存版本,否则旧 `yacctab` 让修好的解析器表观上还坏着;长任务没出最终摘要不算"跑完"。这些错误的共同点是症状酷似编译器 bug。

**§8 原生崩溃用 LLDB,不用猜。** 回答两个问题:哪个生成/项目函数最先收到非法数据;坏指针实际指向什么运行时对象。批处理模式、硬超时、不停在最顶层运行时帧(`py_str_strip` 里崩溃通常意味着调用者传了坏对象),用 `memory read` 对照 `py_runtime.h` 的对象头偏移解码**对象**而非地址。LLDB 负责定位;修复仍然需要最小化回归测试。

**§9 共享 codegen 不堆叠未验证编辑。** [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) 与 [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) 的低层化 mixin 被几乎所有路径共享,一次"小清理"可以同时打碎 Lua、SQLite 与 GC 后端。规则:无最小重现支撑的改动留在草稿探针里;每次共享路径编辑后、下一次编辑前,先跑聚焦回归;第一次修复若没有明确改善最小重现,停止扩大补丁,回去继续缩小。

**§10 区分数据布局 bug 与表达式语义 bug。** 布局怀疑用 `sizeof`/`offsetof` 探针对照原生编译器,匹配即排除整类假设;剩下的才是符号性、提升、比较、移位这些语义问题。先证伪大类,再深入小类。

**§11 偏好下游敏感的回归测试。** 好的测试把表达式放进"下一个操作会因元数据丢失而出错"的上下文:无符号表达式紧跟 `%` 与有符号常量、紧跟 `>>`、紧跟比较。LLVM 的整数类型不区分符号,符号意图全靠编译器自己携带,只断言比特值的测试抓不住这类丢失。

**§12 把编译期常量折叠当作语义子系统。** 整数语义存在于两处:运行期 IR 与 `_eval_const_expr()` 的编译期求值。只修运行期不够——`((size_t)(~(size_t)0))` 在折叠里变成 `-1`,真实项目就会带着错误常量编译通过,在很远处失败。

十二条合起来是一个次序:先确定化(§1),再归属(§2/§3),再缩小(§5/§6),期间不被 harness(§7)与短痕迹(§4)误导,动手时守住共享路径纪律(§9),修复时区分布局与语义(§10/§12),收尾用下游敏感测试锁死(§11),原生崩溃交给 LLDB(§8)。

## 18.4 调查工作流:证据链成为仓库制品

任何超过一行修复的问题,必须开一份书面调查。[docs/investigation-workflow.md](../../docs/investigation-workflow.md) 给出的理由毫不抽象:这套结构每份调查花约 10 分钟,而跳过它曾让项目付出"以周计"的重复诊断——第二个 agent 重新推导第一个 agent 已经得出的结论,只因原始发现没有一行 `## Status`。

机制本身很轻。一个调查 = 一个文件,放在 `docs/investigations/<具体的-slug>.md`,slug 必须具体(`pcc1-stage2-lift-expr-raw-value-leak.md`,而不是 `bug.md`)。开新文件前先扫 [docs/investigations/INDEX.md](../../docs/investigations/INDEX.md)(按主题分组的一行摘要索引);症状若匹配既有条目,用 `## Update` 块续写或在新文件里链接前驱。增改任何调查文件后用 [scripts/regen_investigations_index.py](../../scripts/regen_investigations_index.py) 重生成索引。既有文件是历史记录,永不删除重写;推翻旧结论时,新文件链接旧文件,旧文件加一段 `## Status: superseded by <doc>`。

必填小节构成证据链的骨架:`## Status`(active / resolved / superseded);`## Problem Description`;`## Repro`(最小确定性命令序列,含预期退出码或回溯标记);`## Test [CONFIRMED|N/A]`;`## Proposals` 列表,每个提案展开为 `### Code Change` 加 `### CONFIRMED|DENIED|DENIED BY USER`;收尾时的 `## Report`。三种模式覆盖所有入口:**Repro**(新问题,直到亲眼观察到 `## Test [CONFIRMED]` 才能停)、**Continue**(追加 `## Update`,不重写历史)、**Report**(收尾摘要,不删除被否决的提案——`### DENIED` 段落正是"已排除什么"的记录)。

硬规则四条,每条都在堵一种具体的腐化:**一次一个提案**,跑到判定为止,判定不了就写 `### DENIED — incomplete` 加开放问题然后停——堵"同时试三个改动,绿了也不知道是哪个"的混淆;**Test = 观察而非乐观**,只有在所列命令下亲见失败才许标 `[CONFIRMED]`——堵"我觉得它会失败"的想当然;**两个 bug 两个文件**——堵一份报告两条故事线的纠缠;**Status 定稿后不再改正文**——堵历史的事后修饰。

这套模板的好坏要看实例。[docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md](../../docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md) 是一份教科书级的证据链,值得逐段拆:

症状是 pcc1 编译 stage2 时 `lift_expr` 抛出 `AttributeError: object has no attribute __name__`。文件先做的事是把次级失败与主失败剥开:`__name__` 报错只是回退 raise 自己炸了,主失败是分派为何落空。接着是诊断仪器化:把 raise 子句临时展开,对全部 25 个表达式类做 `isinstance`,同时探测 Python 原语——并把**三次运行的观测结果列成表**(泄漏值分别是字符串 `"'"`、裸 tuple、字符串 `"{"`)。然后是一节标题就叫 "What this rules in / out" 的排除论证:不是缺分派分支(`isinstance` 对 25 个类全部为假)、不是双重导入的类身份问题(那只会让 `is` 失败而 `isinstance` 仍命中)、不是宿主侧解析器(CPython 托管下同一 lifter 从不触发)。随后发生了方法论上最有趣的一幕:为定位失败语句加的第二层仪器(向 `self.<attr>` 写语句踪迹)让 pcc1 直接段错误——调查没有把它混进主线,而是明确写成**另一个独立的 codegen bug 表面**,回退该仪器、换用只动局部变量的版本。继续收网:五次运行的失败语句编号与行号列表显示,全部命中 Parser 类中**仅有的七个**带 `@classmethod`/`@staticmethod` 装饰器的方法,失败区间与装饰器区间精确重合。最终根因:`_parse_decorated` 里 `fn.decorators = list(decorators)` 对默认 `None` 的 dataclass 字段做构造后变异,pcc1 运行时的 setattr 路径在该形态下会破坏邻近槽位;改为经构造器传入即消失。文件最后如实分账:源码侧绕开是本次落地的修复,运行时深层 bug 仍然真实存在、列为后续;下游还有一个与 `--verbose` 相关的独立段错误,**不并入本案**。

这份文件示范了证据链的全部要素:观测以表格落档、排除有论证、仪器本身出 bug 时另立案卷、根因与权宜分开、残余问题显式移交。下一个接手的 agent 不需要聊天记录。

## 18.5 声明卫生:模式是命题的一部分

pcc 的七义务之一是"兼容性必须模式标注"。操作化它的是 [goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10 的声明卫生表,原文八行,值得整段抄录:

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

每行都是一对容易被混为一谈的命题。CPython 托管下跑通的前端代码,不等于编译出来的 pcc1 二进制跑得通(执行边界完全不同,18.7 案例研究二是整章证词);兼容模式接受的扩展,不等于 pcc 原生 ABI 接受;stage1 用 self 后端编出来了,不等于产物还能再编出自己两代。§9.2 据此要求每个声明标注其模式:pcc-native / cpython-compat / libpython / host-only diagnostic / pcc1 no-host / self-backed / LLVM-backed。

声明卫生不只约束措辞,也约束代码与测试的形状。性能声明(§9.5)必须同时携带 IR 形状证据(如热循环里没有 `pcc_gc_alloc`、没有 `py_cpy_*`、没有 `py_obj_call`)、运行期基准、慢路径/守卫的正确性测试与 CPython 基线——单独一个微基准赢了,什么也没声明。包声明([AGENTS.md](../../AGENTS.md) Package/NumPy Claim Hygiene)规定 install 与 import 是两道独立闸门,合成包、仅数组核心的测试都不构成 NumPy 支持声明;并且**禁止** `if package == "numpy"` 式的包名特判——必须修可复用的机制(install/import/ABI/buffer/capsule)并为通用特性加回归。特判能让闸门变绿,但它把"支持 NumPy"偷换成了"支持一个叫 numpy 的字符串",这正是声明卫生要拦的造假形态。终局声明的标准在 §19.2:只有当无宿主执行、链接扫描、pcc2/pcc3 确定性比较、无静默 LLVM 回退、语义差分、真实包证据、五 GC 互不回归、性能五件套**同时**在场,才允许说 "pcc1 can replace python";缺任何一件,只能写部分进展与开放阻塞。

这套表格的实践意义,18.7 案例研究二与 18.6 的三个测量事故会反复演示:几乎每一次假信心,都是把表中某行的左边当成了右边。

## 18.6 回归与测量纪律

**回归纪律。** 当一个长期全部通过的闸门回归,[AGENTS.md](../../AGENTS.md) 规定先做因果审计,再讲修复故事,顺序成文:(1) 用模式标注的语言指认第一道失败边界(`pcc0 -> pcc1` 回退、`pcc1 -> pcc2` 运行期崩溃、`pcc2/pcc3` 字节漂移……);(2) 在改更多代码之前,列出可能拥有该边界的近期被改子系统——对 codegen/运行时改动,默认你自己的近期改动是头号嫌疑,直到 IR/源码/调试器证据排除;(3) 分离堆叠失败:修掉第一道边界暴露出第二道崩溃时,写成两个失败、两条证据链,不许合并成一个猜出来的根因;(4) 不许为了定位而弱化运行时或 GC 语义——禁用 GC 跟踪、屏障、owned-local 清理、终结器都属于语义改动而非诊断手段;(5) 所有权失败先验证调用方/被调方引用契约再动清理代码;(6) 宿主侧测试不构成自举证明,触及前端/运行时/自举入口的修复必须配聚焦回归加相应自举闸门;(7) 调试仪器必须打标、入档,结束前移除或升格为有测试的正式特性。第 (3) 条在 18.4 的案例里有最佳示范;第 (4) 条在第 9 章的所有权案例研究里被用户亲手执行过(否决"禁用元组 GC 跟踪"的提案)。

**测量纪律。** 闸门与基线只在其前提成立时才有意义;前提坏了,绿色比红色更危险。仓库里有三次成文的假信心事故,各自留下了一条不变式:

*事故一:陈旧缓存制造假 "gap 0"(2026-05-29)。* [docs/investigations/python-no-libpython-numpy-build-pcc-capi-include-redirect.md](../../docs/investigations/python-no-libpython-numpy-build-pcc-capi-include-redirect.md) 有一节自我更正:此前宣布的"全模块宿主 C-API 链接缺口 = 0"里程碑,是对着一个**过期的** `/tmp/pcc_capi/Python.h` 测出来的——陈旧头文件让 38 个文件根本编译不过,于是它们的宿主符号引用**从未进入测量**。用当前 [utils/fake_libc_include/](../../utils/fake_libc_include) 刷新后重测:可编译文件从 60 升到 95,真实缺口是 10 个符号而非 0。不变式:测量前刷新测量基底;被测集合的缺员本身就是测量错误。

*事故二:`PCC_RUNTIME_CC=cc` 钉死了神谕路径(2026-05-30)。* [docs/current-goal-state.md](../../docs/current-goal-state.md) 记录了一条加星号的关键更正:默认运行时模式链接的是 pcc-Python 端口(Makefile `PY_MODULES` 列出的 `py_dunder`、`py_str_accessors`、`py_set` 等),而当时的回归测试全部设了 `PCC_RUNTIME_CC=cc`、链接 C 源——它们验证的只是 cc 神谕路径,**遮蔽了**真正的目标模式。后果具体:默认模式下调用 `bin(5)` 链接失败(`undefined _py_builtin_bin`,端口里没有这个新符号),同一程序在 cc 模式下正常;四个切片的自举闸门"通过"仅仅因为 pcc 自身代码不触碰那些特性。修正是把每个 C 改动镜像进 pcc-Python 端口([AGENTS.md](../../AGENTS.md) 本就有的镜像规则)、测试改用默认模式重新验证。不变式:测试必须跑在声明所指的模式里;在神谕模式下测目标模式的声明,是 §0.10 表格的又一行。

*事故三:中途读缓冲日志,误判一次成功的自举(2026-05-29)。* 同样在 [docs/current-goal-state.md](../../docs/current-goal-state.md),ob_type 桥接落地的条目里有一段标明"诊断诚实"的自述:作者先**误读**自举为失败——在约 54 秒的运行尚在 stage2 中途时过早读取缓冲中的日志与构建目录,且命令尾随的 `;ls` 掩盖了真实退出码——于是回退了一个正确的改动;重新干净跑一遍后确认是误诊,改动重新落地。记录下的教训:自举结果只在任务完成后读,以完整日志的 `verify:` 行加自举自身的退出码为准,不做中途的制品检查。

三次事故同构:测量动作本身正确,**前提**——缓存新鲜、模式正确、运行完结——被静默违反。这就是为什么 §0.2 要求证据写出完整命令:可复现的命令把前提显式化,让下一个读者有机会发现前提坏了。

## 18.7 历史与教训

### 案例研究一:sort.lua 的偶发失败与丢失的无符号语义

(来源:[docs/investigations/lua-sort-random-pivot-signedness.md](../../docs/investigations/lua-sort-random-pivot-signedness.md))

症状:`tests/test_lua.py::test_pcc_runtime_matches_native[sort.lua]` 偶发失败,pcc 编译的 `onelua.c` 非零退出而原生编译通过。同源不同行为,bug 先归属编译器(§2)。表面候选有一排:栈腐败、聚合拷贝、结构体布局、比较器、Lua 运行时——最终全错。

调查的每一步都能映射回十二技法。第一步确定化(§1):固定 `math.randomseed(15)`、构造确定性失败的数组形状,把"有时失败"压成"反转输入 + 自定义比较器 + 最小失败规模约 1921"。第二步换小 harness 保真实现(§5):`#define main pcc_onelua_main` 后 `#include "onelua.c"`,直接调真实的内部 `auxsort`——原生通过、pcc 确定性失败。第三步快速证伪大类(§10):`sizeof`/`offsetof` 探针证明 `TValue`、`lua_State` 等关键结构布局与原生一致,排除布局假设;栈形状探针排除 `luaL_makeseed`;逐件替换(§6)证明 `sort_comp` 与 `partition` 本身无辜——它们只是在更早的错误破坏快排不变式之后才表现异常。第四步替换二分锁定随机轴元路径:去随机化的 `auxsort` 通过、小 `rnd` 通过、大 `rnd` 失败。第五步降到纯 C(§5 的终点):`choosePivot` 公式 `(rnd ^ lo ^ up) % (r4 * 2) + (lo + r4)`,在 `lo=1, up=1921, rnd=3426782842u` 下,原生得 731,pcc 得 475。

475 这个错值本身就是证据:它比合法下界 481 恰好低 6,正是有符号取余的签名——无符号解释给出合法轴元,按 32 位有符号解释余数为 -6。根因落在 [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py):pcc 把有符号与无符号 32 位整数都低层化为 LLVM `i32`,符号性靠 `_tag_unsigned`/`_is_unsigned_val` 这套元数据单独携带,而 `^` 返回 `builder.xor(...)` 时没有按 C 结果类型重新打无符号标——比特全对,语义已丢,下一个 `%` 用了 `srem`。修复后按 §10 的精神审计邻近算子,又抓到同族的第二个真 bug:无符号前缀 `++`/`--` 的表达式结果同样没有重新打标。

留下的不变式有两层。机制层:`^`、无符号 `>>`、整数复合赋值、无符号前缀自增减四类结果保持无符号标记,回归测试进 `tests/test_unsigned_loads.py`,而且刻意全部采用"无符号结果紧跟 `%` 有符号常量"的下游敏感形状(§11)——此前仓库不缺无符号测试,缺的正是这种形状,所以一类 bug 长期隐形。流程层:这份调查的模板("先原生对照、去随机、最小集成 harness、布局与语义分家、降到纯 C、查元数据传播而非算术指令")被沉淀进 [AGENTS.md](../../AGENTS.md) 的 C Codegen Invariants 小节与调试手册本身——方法论文档的内容,相当一部分就是这样从案例研究里蒸馏出来的。

### 案例研究二:no-libpython 自举暴露的运行时语义洞

(来源:[docs/investigations/python-self-host-no-libpython-runtime-holes.md](../../docs/investigations/python-self-host-no-libpython-runtime-holes.md),截至 2026-04-29 的报告)

Issue 1 的字面目标很简单:自举二进制不得链接 libpython。这份报告的核心发现是:**判据会撒谎**。`--python-libpython off` 构建成功、`otool -L` 扫描无 libpython——前两条判据通过时,第三、四条(产物作为编译器运行、编出能工作的二进制)仍能暴露成片的自托管 bug。`py_cpy_*` 计数归零是必要条件,不是充分条件:计数说明没有回退到 CPython,但对 pcc 自己的运行时是否实现了编译器源码依赖的 Python 行为只字未提——类属性存储、描述符行为、链式解析器调用的副作用次数,全在计数的盲区里。

报告里最有方法论分量的一个洞,是 [AGENTS.md](../../AGENTS.md) 异常模型小节的出处:pcc 没有 Itanium 式栈展开,`py_raise(exc)` 把异常存进 TLS 后正常返回,生成代码必须在可能抛出的调用后检查 `py_err_occurred()`。当时的顶层自举路径漏了这一环——内部编译期异常一路传播到函数错误尾声、返回哨兵整数、`bootstrap_cli_sys_argv_exit()` 看到 0,**进程带着失败的编译成功退出,且不产出任何制品**。"编译成功但没有输出"不是个别 bug,而是一类被判据放过的失败形态,只有 LLDB 在 `py_raise` 上断点才看得见。报告据此把错误分了三类——编译错误(用户输入坏)、编译器执行错误(pcc1 自己内部抛了)、目标执行错误(产物运行失败)——并指出当时的行为把第二类坍缩成了静默成功。

这份报告的"Why So Many Errors Happened"一节,是对测试金字塔的一次结构性反思:既有测试几乎都是"CPython 跑 pytest → pytest 调 pcc 前端 → 检查产物"的形状,而 bug 住在另一个形状里——"CPython 编出 pcc1 → pcc1 无 libpython 地执行解析、类型推断、codegen、IR 对象构造 → 检查 pcc1 的产物"。结论不是"多跑测试",而是补一层缺失的、小而强制的 stage1-as-compiler 闸门:链接闸门(无 libpython、`--help` 退出 0)、编译闸门(用新 pcc1 编最小程序并运行产物)、pcc 编译下的 `llvm_capi.ir` 对象模型闸门、解析器副作用闸门(`_expect()` 不被链式属性访问重复执行)、模块初始化器闸门、错误传播闸门(内部异常必须非零退出、打印异常、不留产物)。今天 [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 所守的状态,以及 [AGENTS.md](../../AGENTS.md) 里"missing `py_err_occurred()` check"位列三大惯犯的论断,都是这份报告的直接遗产。

两个案例研究合起来给出本章立场的最好证词:案例研究一展示十二技法如何把一个偶发的真实程序失败收敛成两行元数据修复加一族下游敏感测试;案例研究二展示当判据本身有盲区时,方法论的产出不是补丁,而是**新的闸门层与新的不变式**——方法论文档因此不是静态规章,它和编译器一起被这些调查迭代。

## 18.8 小结

pcc 把工程方法论当作设计的一部分来建造,而非围绕设计的礼仪。不动点是全系统差分测试,五 GC 矩阵是同一槽位契约的五个独立观察者,两台仪器经由 `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` 串联。状态物化为可测试的基线:[tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 守住 pcc2/pcc3 字节一致,[tests/fallback_baseline.json](../../tests/fallback_baseline.json) 以棘轮守住 no-libpython 回退面单调不退。调试手册十二技法构成一条固定次序:确定化、归属、缩小、替换验证,不被 harness 与短痕迹误导,共享 codegen 不堆叠未验证编辑,收尾用下游敏感测试锁死。调查工作流把证据链变成仓库制品:一调查一文件、必填小节、一次一个提案、`[CONFIRMED]` 只属于亲见的失败、被否决的提案是"已排除什么"的永久记录。声明卫生表用八个不等式拆开容易混淆的命题对,模式标注是命题的一部分;`DONE_STRONG` 的门槛与强制的 `bootstrap:` 行把证据强度写进流程。三次成文的测量事故——陈旧头文件的假 "gap 0"、`PCC_RUNTIME_CC=cc` 钉死神谕路径、中途读缓冲日志的误诊——共同指向同一条元规则:测量的前提必须与测量本身一起被陈述与检查。每一份这样的纪律都来自一次具体的代价;这正是它们值得被一本设计书收录的原因。

## 练习

1. **读源码验证**:打开 [tests/fallback_baseline.json](../../tests/fallback_baseline.json) 与 [tests/python/test_fallback_baseline.py](../../tests/python/test_fallback_baseline.py),回答:该测试断言的是"等于基线"还是"不劣于基线"?当某模块的回退数**优于**基线时会发生什么——基线会自动收紧吗?结合棘轮的目的论证当前选择的合理性。

2. **读调查验证**:通读 [docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md](../../docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md),把它的实际小节逐一映射到 [docs/investigation-workflow.md](../../docs/investigation-workflow.md) 的必填模板上,指出哪些模板要素以变体形式出现(例如排除论证对应什么)、哪个独立 bug 表面按"两个 bug 两个文件"的规则本应或已经另立案卷。

3. **技法应用**:假设 `tests/c/test_lua.py::test_pcc_runtime_matches_native[sort.lua]` 再次偶发失败。依照调试手册 §1/§2/§7,写出你将运行的前三条完整命令(含 `env -u LC_ALL`、`-n0`、超时与 zsh 引号处理),并各用一句话说明该命令排除或确认什么。

4. **声明卫生分解**:把"pcc 支持 NumPy"这句话按 §0.10 与 [AGENTS.md](../../AGENTS.md) 的包声明卫生拆成至少五个互不蕴含的命题(提示:install / import / 数组核心 / 扩展 ABI 模式 / 无宿主证据),为每个命题指出它需要的闸门形态,并说明 `if package == "numpy"` 式特判会同时伪造其中哪几个。

5. **设计权衡论证**:pcc2/pcc3 的闸门判据是签名规范化后的**字节一致**,而非"语义等价"。结合 [AGENTS.md](../../AGENTS.md) 义务 5 列出的八类差异(语义 / IR 文本 / 类布局 / 对象模型 / 后端非确定性 / 链接元数据 / 仅性能 / 诊断),论证:字节一致对哪几类差异是过度约束?它换来了什么判定性质(可机械检查、无需信任分类器)?如果改用"允许已分类的非语义差异"的弱判据,闸门会在哪个环节重新引入人的判断,代价是什么?
