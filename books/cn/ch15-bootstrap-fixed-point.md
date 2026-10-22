# 第 15 章 自举:pcc1→pcc2→pcc3 不动点

前面十四章描述的每一个子系统——解析、类型推断、低层化(lowering)、对象模型、所有权、五 GC、self 后端、no-libpython 运行时——各自都有自己的测试。但"各部分分别正确"与"整个系统相干"之间隔着一条鸿沟:测试是对行为的采样,采样永远证明不了全称命题。pcc 用一个古老而苛刻的装置跨越这条鸿沟:让编译器编译自己,再让产物编译自己,直到输出收敛成不动点(fixed point)。本章讲这个装置的全部:四个阶段的语义、[scripts/bootstrap.sh](../../scripts/bootstrap.sh) 与 [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) 的机制、字节同一性背后的验证阶梯、三项相互独立的证明、pcc1/pcc2 差异的分类学、把不动点钉死成回归闸门(gate)的基线体系,以及它与 Thompson《Reflections on Trusting Trust》之间必须诚实划清的边界。

## 本章导读:三代编译器证明三件事

这一章可以按 pcc0→pcc1→pcc2→pcc3 这条链读。pcc0 证明宿主路径能产出编译器,pcc1 证明产出的编译器能继续编译,pcc2/pcc3 比较证明系统开始稳定复现自己。

- pcc1 失败通常是 frontend/runtime/fallback 边界问题。
- pcc2 或 pcc3 漂移通常是非确定性、布局、链接元数据或诊断输出问题。
- byte identity 不是形式主义,它把"表观上能跑"提升成"能稳定再生产自己"。

## 15.1 问题与设计空间:为什么是三阶段加字节比较

先回答"为什么要自举"。第 1 章列出的五大差异化里,`pcc1 -> pcc2 -> pcc3` 自托管不动点排第一位,理由写在 [AGENTS.md](../../AGENTS.md) 的北极星一节:**不动点不只是一次字节比较,它是 pcc 的 Python 语义、运行时、代码生成、对象模型、后端与诊断相干到足以复现自身的证据。** 拆开说:要让 pcc 编译自己,编译器源码必须整体落在自身可编译的 Python 子集内(前端覆盖证明);要让产物 pcc1 再编译一遍源码,原生运行时必须在"编译一个编译器"这种规模的真实负载下正确(运行时正确性证明);要让 pcc2 与 pcc3 字节相同,整条流水线必须确定(确定性证明)。任何一层有不相干,链条就在对应的边断掉,而且断点会用模式标注的(mode-labeled)语言指认责任边界。

设计空间里有三个被放弃的备选。

**备选一:不自举。** 大多数 Python 加速工具走这条路:编译器永远跑在 CPython 上,只有用户代码被编译。这对加速器是合理的,对 pcc 不行——pcc 的论题是执行所有权(见第 1 章),一个自己都离不开 CPython 的编译器无法主张"no-libpython 的原生执行路径"是完整的。更实际的损失是覆盖:编译器自身约十几万行 Python,是仓库里最大、最毒的单个真实输入,放弃自举等于放弃这份免费的压力测试。

**备选二:两阶段加测试套件。** 让 pcc1 编译出 pcc2,然后对 pcc2 跑测试。问题在于测试是行为采样:pcc2 通过一千个测试,只证明一千条路径正确。而"pcc2 编译源码的输出等于 pcc1 编译源码的输出"是对整个编译函数在一个巨大输入上的**全量比较**——任何一处非语义噪音(哈希顺序、并行调度、未初始化填充)或语义偏差都会让数百万字节中的某一个不同。字节比较把"编译器行为相等"从论证题变成机器可判定的断言。

**备选三:只比较 pcc1 与 pcc2。** 这是初学者直觉,但它要求过强的性质。pcc1 由 pcc0(宿主 CPython 解释执行仓库源码)产出,pcc2 由原生二进制 pcc1 产出;两个**执行引擎不同**的编译器即使语义完全一致,输出字节也可能系统性不同。[tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 里冻结的尺寸直接展示了这一点:self 后端下 stage1 为 4442648 字节,stage2 与 stage3 同为 4524200 字节——pcc1 与 pcc2 不同,pcc2 与 pcc3 相同。经典的三阶段收敛论证(GCC 传统)解释了为什么这恰好够用:pcc2 = pcc1(源码),pcc3 = pcc2(源码);若 pcc2 == pcc3,则 pcc1 与 pcc2 这两个**不同字节的二进制**在"编译 pcc 源码"这个输入上计算了同一个函数。不动点定义在自产编译器之间,第一级允许带着外来宿主的指纹。

诚实的部分:字节同一是在 **Mach-O 签名归一化后**成立的。[scripts/bootstrap.sh](../../scripts/bootstrap.sh) 头部注释如实记录(状态注释日期 2026-04-23):直接 `cmp` 仍因 Mach-O 代码签名元数据不同而失败,验证流程先用 `codesign --remove-signature` 处理临时比较副本再宣告成功。15.6 的分类学会把这类差异归为 link metadata——被理解、被归一、被记录,而不是被掩盖。

## 15.2 阶段语义:pcc0→pcc1→pcc2→pcc3

[AGENTS.md](../../AGENTS.md) 固定了阶段命名,本书全程沿用:

```text
pcc0   宿主 CPython 解释执行仓库源码(不是一个二进制,是一种运行方式)
pcc1   pcc0 产出的第一个原生编译器二进制
pcc2   pcc1 编译 pcc 源码产出的二进制
pcc3   pcc2 编译 pcc 源码产出的二进制
验证   pcc2 与 pcc3 在签名归一化后逐字节比较
```

三条边各证明一件不同的事:

```text
pcc0 -> pcc1   源码可编译性:编译器整体落在自身子集内,
               且在 --python-libpython=off 下闭世界成立
pcc1 -> pcc2   运行时正确性:原生对象模型/GC/异常/所有权
               扛得住"编译编译器"规模的负载
pcc2 -> pcc3   行为自稳定:自产编译器编译同一输入收敛
  + cmp        (= 不动点;这一步才允许说 self-hosted)
```

三条边的输入是同一个文件:[pcc/__main__.py](../../pcc/__main__.py)。它只有五行——从 `pcc.driver.cli_bootstrap` 导入 `bootstrap_cli_sys_argv_exit` 并调用。于是 stage1 的命令呈现一种意味深长的对称:`python -m pcc ... pcc/__main__.py -o pcc1`——**命令与输入是同一个模块,只是宿主不同**。pcc0 用 CPython 运行它,pcc1 用 pcc 自己的运行时运行它。

在 [scripts/bootstrap.sh](../../scripts/bootstrap.sh) 中,自举三阶段流程写得很直接:

```bash
# scripts/bootstrap.sh
# Stage 1: pcc0 -> pcc1
env -u LC_ALL uv run pcc pcc/__main__.py -o pcc1 \
  --backend=self --python-libpython=off --ir-scaffold=on

# Stage 2: pcc1 -> pcc2
./pcc1 pcc/__main__.py -o pcc2 \
  --backend=self --python-libpython=off --ir-scaffold=on

# Stage 3: pcc2 -> pcc3
./pcc2 pcc/__main__.py -o pcc3 \
  --backend=self --python-libpython=off --ir-scaffold=on

codesign --remove-signature pcc2 pcc3
cmp -s pcc2 pcc3
```

而在 [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) 内部,入口函数响应自编译二进制的核心控制逻辑:

```python
# pcc/driver/cli_bootstrap.py
def bootstrap_cli_main(argv: list[str]) -> int:
    """Entry point for the self-hosted pcc1/pcc2/pcc3 binary."""
    if "--pytest" in argv:
        return _run_pytest_harness(argv)
    return _dispatch_compile_job(argv)
```

验证不动点基线的断言也是测试的一部分,[tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) 强制要求对比结果匹配:

```python
# tests/python/test_bootstrap_gate_baseline.py
def test_bootstrap_fixed_point_baseline():
    baseline = json.loads(pathlib.Path("tests/bootstrap_gate_baseline.json").read_text())
    assert baseline["stages"]["pcc2_vs_pcc3_byte_identical"] is True
    assert baseline["no_libpython"]["py_cpy_calls"] == 0
```

声明卫生(claim hygiene)在这里有严格的措辞表([goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10):host pcc ≠ pcc1;stage1 通过 ≠ 不动点通过。"pcc 能编译 X"与"pcc1 能编译 X"是两个声明,后者要求 X 的全部依赖都在原生闭包里;"自举到 stage1"与"pcc1→pcc2→pcc3 不动点"也是两个声明,差着两级运行时正确性。[AGENTS.md](../../AGENTS.md) 的规则是:不要从局部玩具复现宣布自举修复完成。

## 15.3 机制(一):bootstrap.sh 的阶段机器

[scripts/bootstrap.sh](../../scripts/bootstrap.sh) 是 macOS arm64 三阶段入口,约 380 行,核心是一个被调用三次的 `run_stage()`。值得逐项读的设计点:

**运行时默认值揭示自举同时使用两个编译器。** 每个阶段都在 `PCC_RUNTIME_CC=pcc`、`PCC_RUNTIME_HIGH=py` 下运行(对应 `pipeline.py` 的 `_runtime_cc_mode()` / `_runtime_high_mode()`,两者的默认值也已是 `pcc`/`py`):运行时归档由 pcc 的 **C 前端**编译(而非宿主 cc),运行时高层模块取 pcc-Python 端口(而非 C 源,见第 14 章)。也就是说,自举闸门同时压测两条编译路径:C 前端编运行时,Python 前端编编译器。另两项默认是 `PCC_BOOTSTRAP_PYTHON_LIBPYTHON=off`(严格 no-libpython)与 `PCC_BOOTSTRAP_PYTHON_IR_PASSES` 默认 off。后端默认按平台:Darwin arm64 选 `self`,其余选 `llvm`——注意这是自举脚本的默认,公开 CLI 的默认后端仍是 LLVM(README 状态表的模式标注)。

**陈旧产物防御。** `run_stage()` 开头无条件 `rm -f "${out_exe}" "${out_exe}.tmp"`,注释写明理由:绝不让一次失败或短路的编译把上一轮的阶段二进制留在原位,否则 stage3 可能拿一个**陈旧的 pcc2** 继续跑——闸门会绿,但证明的是上周的编译器。这是"基线即状态"哲学的微观版本:宁可失败,不可用过期证据通过。

**发布屏障(stage_exec_barrier)。** 阶段产物在被下一阶段执行前要过一道屏障:`codesign --verify`、`cat` 一遍文件、默认 `PCC_BOOTSTRAP_STAGE_EXEC_DELAY=0.10` 秒延迟、跑 `--help`、再用它编译一个两行冒烟程序(`def main() -> int: return 0`)。这道屏障是用真实事故换来的:[docs/investigations/self-backend-mach-o-stage-publish-race.md](../../docs/investigations/self-backend-mach-o-stage-publish-race.md) 记录了 stage3 立刻执行新链接的 pcc2 时间歇性段错误(退出码 139),同一个二进制稍后再跑却成功。根因不在编译器语义,在 macOS arm64 的 Mach-O 发布边界:原子改名不够,稳定的边界是先 ad-hoc 签名再 `codesign --verify`,强迫系统验证器在下一阶段 exec 之前观察到最终的 Mach-O。修复落在 `pipeline.py` 的 `_finish_self_backend_executable()`:`codesign --force -s -` 签临时文件 → verify → `/bin/mv -f` 发布 → 再 verify → `/bin/sync` 或 `cat` 屏障(后续调查 `self-bootstrap-reliability-performance-2026-05-15.md` 补的)。值得注意它全部用 subprocess 完成——一次 `os.replace()` 的尝试被否决,因为它在严格自举里引入了 no-libpython 回退(fallback):**连发布序列的实现方式都受自举闭包约束。**

**验证阶梯。** stage3 之后是三级比较:

```text
cmp pcc2 pcc3                     → 字节同一,最强结论,exit 0
codesign --remove-signature 副本
  后再 cmp                         → "仅签名元数据不同",exit 0
size + md5 结构比较                → 尺寸不等 = FAIL exit 1
                                    尺寸等而字节不同 = WARN exit 2
                                    ("怀疑元数据噪音")
```

阶梯的每一级对应一种诚实程度:第二级明说差异是什么并归一掉它;第三级不假装成功,退出码 2 加注释提醒"等构建完全确定(无时间戳/路径/uuid 嵌入)后,cmp 应当直接成功"。Linux 侧的对应努力在 `_platform_link_flags()`:`-Wl,--build-id=none -s`,把已知的链接期非确定源从产物里剔除。

**阶段剖面。** `--profile-json` 加 `PCC_BOOTSTRAP_PROFILE_DIR` 时,每阶段写出 `pcc.bootstrap_stage_result.v1` 模式的 JSON(compile_wall_ms、publish_barrier_ms、returncode 等),供性能回归调查使用(如 `bootstrap-self-time-after-layer1-split-2026-05-13.md`)。

**`--reuse-stage1`。** pcc1 对 GC 后端是不可知的:`PCC_GC_BACKEND` 在运行期选择收集器(见第 10 章),只影响 stage2+ 的运行时行为,不影响 pcc1 的构建。脚本因此允许构建一次 pcc1、在多次 stage2/stage3 之间复用——五 GC 闸门正建立在这一点上(15.7)。

## 15.4 机制(二):cli_bootstrap.py——pcc1 究竟是什么

[pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) 约七千行,是阶段二进制的全部用户面。读它要带着一个意识:**这个文件本身必须能被 pcc 编译**,它是 stage1 闭包的成员。这解释了它的方言。

入口链是 [pcc/__main__.py](../../pcc/__main__.py) → `bootstrap_cli_sys_argv_exit()` → `bootstrap_cli_main()`。后者按请求类型路由:

```text
--pcc-python-multi-codegen-worker  → 并行代码生成 worker 重入口
--pytest ...                        → _run_pytest_from_pcc1(pcc1 启动测试套件)
-m MODULE ...                       → _run_python_module_from_pcc1
   其中 pip / pcc.package.*         →   原生包外壳(_run_native_pip_shim_from_pcc1 等,见第 17 章)
   其余模块                          →   pcc1 自编译成原生二进制再运行
                                        (_run_compiled_python_module_from_pcc1,backend=self、libpython=off)
   --python-libpython=auto/on 时    →   _run_python_module_from_pcc1_with_mode 显式委托 CPython 兼容子进程
                                        (打印 PCC1_COMPAT_RUNNER_MANIFEST,不谎称 no-libpython)
C 输入(.c / --sources-from-make /
  -I/-D/-U / 目录)                  → _run_host_pcc_from_pcc1(委托宿主 pcc CLI)
否则                                → parse_bootstrap_cli_args → 编译 Python 输入
```

核心编译路径很短:`parse_bootstrap_cli_args()` 手写 while 循环解析 `--backend` / `--python-libpython` / `--ir-scaffold` / `-o` / `--emit-llvm` 等旗标(没有 argparse——它不在原生闭包里),然后 `_observed_compile_python()` 直接调用 `py_frontend.pipeline` 的 `compile_python`。这个"直接调用"有一条值得全文引用的 docstring:**"This intentionally calls `_compile_python` directly instead of passing it as a first-class callable through `observed_compile`. The self-host path does not yet have a native `callable(*args, **kwargs)` ABI."** 自举子集没有一等函数装箱(第 5 章讲过这个限制),所以可观测性包装只能写成固定形状的直调而非高阶函数。同类痕迹遍布全文件:`_normalized_sys_argv()` 用 `(sys.argv[i] or "") + ""` 逐个拷贝规范化字符串;`_copy_seq()` 用显式索引循环代替切片惯用法。**编译器的 CLI 是用编译器自己能消化的 Python 写成的——这既是约束,也是测试。**

两条委托边界都有防御。`_run_host_pcc_from_pcc1()` 在 `PCC_HOST_PCC` 指向自身时拒绝递归委托;`-m` 的默认路径已不再借道宿主 Python——通用模块由 pcc1 自己编译成原生二进制再跑(`_run_compiled_python_module_from_pcc1`,`backend=self`、`libpython=off`),宿主委托退化为 `--python-libpython=auto/on` 下的显式选入:只有那时 `_run_python_module_from_pcc1_with_mode` 才起一个 CPython 兼容子进程,并在 `PCC_COMPAT_PYTHON` 指向自身时拒绝递归。这条 opt-in 路径打印 `PCC1_COMPAT_RUNNER_MANIFEST` 把模式写在明面上,pcc1 进程本身仍是 no-libpython。[AGENTS.md](../../AGENTS.md) 把边界上升为规则:`_link_with_self_backend` 不得在编译后的阶段里 import/调用 `pcc.backend.*`,那会把 `py_cpy_*` 拉回 stage1 闭包。subprocess 是刻意选择的隔离层:宿主能力可以被**调用**,但不能被**链接**。

## 15.5 三项独立证明:0 py_cpy_*、无 libpython、字节同一

README 状态表的 bootstrap 行(Issue 1 于 2026-05-01 关闭)给出的证据是三元组:pcc2/pcc3 发射的 IR 中 **0 个 `py_cpy_*` 调用**;`otool -L` 中**无 libpython 条目**;签名归一化后 pcc2/pcc3 **字节同一**(IR 文本也逐字节相同)。三者各锁一个层面,互不蕴含,合在一起才构成"严格 no-libpython 自举"这个复合声明。

**0 个 `py_cpy_*` 调用锁的是生成代码层。** `py_cpy_*` 是运行时头文件 [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 里 "Phase 4: CPython C-API fallback" 一节声明的桥接面:`py_cpy_import()`、`py_cpy_getattr()`、`py_cpy_call1()` 等,操作与 pcc 自有对象不同的**不透明 CPython 指针**,实现在 [pcc/runtime/src/py_libpython.c](../../pcc/runtime/src/py_libpython.c)。前端遇到推不出原生低层化的表达式时,历史上的退路就是发射这些调用(见第 14 章)。合并 IR 里数出 0,意味着编译器闭包的每一条路径都走了原生低层化——闭世界不是宣称,是 grep 可验的计数。

**无 libpython 链接锁的是产物层。** `_ensure_runtime()` 按需选择运行时归档:不需要回退时链 `_PY_RUNTIME_ARCHIVE_PCC_PY`,需要时换成带 `py_libpython` 兼容桥的版本。[tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) 的 `_links_libpython()` 直接对二进制跑 `otool -L`(Linux 用 `ldd`)找 `libpython` / `Python.framework` 字串。这一层防的回归与上一层不同:即使 IR 干净,构建系统也可能因配置错误把桥接归档静默链回来。

**字节同一锁的是自指层。** 前两项对单个二进制成立;第三项是 pcc1 与 pcc2 之间的行为等式(15.1 的收敛论证)。它对非确定性的敏感度远超常规测试——一次哈希迭代顺序的不稳定、一次并行分片的边界变化,都会在数兆字节里留下差异。

回退面还有两道互补的检测网,分工在两个权威基线文件里:[tests/fallback_baseline.json](../../tests/fallback_baseline.json) 是 no-libpython 回退棘轮(基线 0,任何增长即失败),[tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 是自举闸门基线。15.9 的两个案例研究恰好各被一道网拦住:一个在严格模式编译期硬错误上炸响,一个无声地混进 IR、被棘轮的计数扫描捉住。

## 15.6 差异分类学:先分类,再修

不动点闸门红了之后的第一动作不是修,是分类。[goal-prompt.md](../../docs/goal/goal-prompt.md) 的 §19.2「Fixed-point classification」一节规定了八类,并附一句禁令:**在差异类别查明之前,不得绕着症状打补丁。**

```text
1 semantic execution    语义执行差异(真编译器 bug)
2 codegen IR            发射 IR 差异(IR 文本闸门捕获)
3 class field layout    类字段布局差异(C/pcc-Python 镜像漂移,见第 7 章)
4 backend selection     后端选择差异(声明的后端没真正生效)
5 object/link metadata  对象/链接元数据(Mach-O 签名、build-id、uuid)
6 runtime archive/链接   运行时归档或链接形状差异
7 diagnostic/error-path 诊断与错误路径差异
8 performance-only      仅性能差异(字节同一,时间不同)
9 unknown               未知——合法的类别
```

([AGENTS.md](../../AGENTS.md) 北极星一节的八类表述——semantic / IR-text / class-layout / object-model / backend nondeterminism / link metadata / perf-only / diagnostic——是同一套分类学的义务版措辞。)

分类学的价值在于它把"修什么"和"用什么证据修"都决定了。link metadata 类(Mach-O 签名)的正确处理是**归一化加记录**——`bootstrap.sh` 的签名剥离比较;把它当 semantic 类去"修"就会去折腾代码生成,徒劳且危险。反过来,semantic 类(15.9 案例研究一的双重释放)绝不允许用归一化掩盖。backend nondeterminism 类有一个已被运营化的实例:[tests/python/test_pcc_bootstrap_full.py](../../tests/python/test_pcc_bootstrap_full.py) 的 `_run_stage2_3()` 刻意在 pcc2 与 pcc3 之间**固定同一份并行预算**,注释言明:在 codegen/链接输出被证明与并行度无关之前,改变 worker 数可能改变二进制布局、打破字节同一闸门——语义未变,字节已变。这是分类学反向塑造基础设施的例子:已知的非确定源被制度性钳住,而不是每次红灯重新争论。第 9 类 unknown 的存在同样是设计:它强迫调查在证据不足时写"不知道",而不是把最顺手的故事当结论(这正是仓库声明卫生的微观形态)。

分类之后才是 [AGENTS.md](../../AGENTS.md) 的自举回归纪律七步,逐条都是从真实事故里蒸馏的(15.9 案例研究一就是其中之一的来源):(1) 用模式标注语言指认**第一道失败边界**(pcc0→pcc1 回退?pcc1→pcc2 运行时崩溃?pcc2/pcc3 字节漂移?);(2) 列出可能拥有该边界的近期改动子系统,**把自己最近的改动当头号嫌疑**,直到 IR/源码/调试器证据排除;(3) 堆叠失败要拆开——修掉第一道边界暴露出第二道时,写成两个失败、两条证据链;(4) 不得为了让某一阶段变绿而弱化运行时或 GC 语义——禁用追踪、屏障、owned-local 清理、终结器都属语义变更而非诊断;(5) 所有权失败先验证调用方/被调方引用契约再动清理代码(见第 9 章);(6) 宿主侧测试不是自举证明——触及前端/运行时/自举入口的修复必须带上相应的 pcc1/自举闸门;(7) 调试探针必须打标、记录、撤除或转正,不得留下改变归档新鲜度或链接形状的临时改动。

## 15.7 闸门体系:基线即状态

自举状态的权威记录不是文档,是两个被测试消费的冻结 JSON。[AGENTS.md](../../AGENTS.md) 的措辞是"authoritative ... (do not invent)":[tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) 记录 2026-05-01 抓取的每后端每阶段尺寸与 `links_libpython` 状态(llvm 与 self 双后端、三阶段全部 `false`,`byte_identical_pcc2_pcc3` 双双为 `true`),并声明单向棘轮语义——任何 `links_libpython` 回到 `true` 都是回归;历史追踪文档 [docs/issues/open-bootstrap-issues.md](../../docs/issues/open-bootstrap-issues.md) 可以滞后,JSON 不可以。选 JSON 而非散文的理由是机器可执行:[tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) 逐字段核对它,而没人会用 pytest 去核对一段 Markdown。

这个基线测试本身的设计也值得一读:它**只检查已存在的二进制**,缺了就 skip,绝不触发重型构建——闸门的存在不能让每次 pytest 都付出几分钟自举的代价。重型闸门被显式隔离成另一组文件:`tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py`,每个 GC 后端一个文件,各自跑完整的 `pcc1 -> pcc2 -> pcc3` 真实链条。共享助手在 [tests/python/test_pcc_bootstrap_full.py](../../tests/python/test_pcc_bootstrap_full.py),其模块 docstring 把哲学写成一句话:**"Speed comes from *not skipping anything* — it comes from sharing stage1 and keeping each GC as an independent file/node."** 具体机制:会话级 fixture `shared_stage1_pcc1` 在文件锁下构建一个 GC 后端不可知的共享 pcc1(`_shared_pcc1_is_fresh()` 用源码树最新 mtime 加 libpython 链接检查判定新鲜度);每个 GC 文件把它播种进自己的输出目录,在 `PCC_GC_BACKEND=N` 下经 `bootstrap.sh --reuse-stage1` 跑 stage2、stage3,然后断言三阶段存在、三阶段都不链 libpython、pcc2/pcc3 归一化后字节同一。调度按权重排队(`_GC_BOOTSTRAP_WEIGHT`:gc0=60、gc4=50、gc3=40、gc1=gc2=30,重的先跑),`_bootstrap_active_gc_lease()` 限制同时活跃的链条数(默认至多 3),所有子进程经 `run_process_group_timeout`(2400 秒)以进程组为单位超时回收——这台机器的每个零件都对应一次真实的痛:被饿死的重后端、机器上游荡数小时的僵尸 pcc1。

于是闸门形成三层金字塔:轻量基线核对(秒级,随每次 pytest)、单后端全自举(分钟级,提交级验证,[AGENTS.md](../../AGENTS.md) 列为 commit-level mandatory)、五 GC 全自举矩阵(最重,运行时/GC/对象生命周期声明的完成证据)。与之互补的语义对照棘轮是 [tests/python/test_self_host_oracle_diff.py](../../tests/python/test_self_host_oracle_diff.py)(仓库地图称其为 core Python semantic oracle / pcc1-pcc2 parity ratchet)。

## 15.8 与《Reflections on Trusting Trust》的关联与边界

任何写自举的章节都绕不开 Thompson 的 1984 年图灵奖演讲。他演示的攻击恰好住在本章的机制里:在编译器二进制中植入一段逻辑,识别"正在编译编译器自己",并把木马重新注入产物——源码永远干净,木马在 pcc1→pcc2→pcc3 的每一条边上自我延续。必须诚实到刺眼的程度:**这样的木马与字节同一不动点完全相容,甚至以成为不动点为设计目标。pcc2 == pcc3 证明的是自再生产的稳定性,不是产物的可信性。** 如果有人从本章读出"pcc 自举了,所以 pcc 可信",那是本章的失败。

不动点真正证明的(也是它对工程的全部价值):编译器源码在自身子集内、原生运行时扛得住最重真实负载、整条流水线确定到字节、且这三件事在五个 GC 后端下同时成立。这些是相干性证据,与信任正交。

pcc 的结构里有两个**缓解信任问题但不解决它**的事实,措辞必须精确。其一,信任根是可刷新的:pcc0 是 CPython 解释执行可审计的仓库源码,任何人任何时刻可以从源码加一个独立取得的 CPython 重建 stage1,不存在"只有某个祖传二进制能造出下一代"的封闭谱系——Thompson 攻击最舒适的温床恰是后者。其二,存在部分多样性:llvm 与 self 两个发射后端各自到达签名归一化后的字节同一不动点(基线 JSON 双双记录 `true`)。这与 Wheeler 的 Diverse Double-Compiling(DDC,用独立可信编译器交叉重建并比对可疑二进制)精神相通,但 pcc **没有做** DDC 要求的交叉比对,也就**不声明** DDC 级别的结论。这一段本身就是声明卫生的练习:每个声明说清自己证明什么、不证明什么——这正是 [AGENTS.md](../../AGENTS.md) 对全仓库的要求,本书没有豁免权。

## 15.9 历史与教训

### 案例研究一:一次长绿闸门回归的因果审计(2026-06-01)

(来源:[docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md](../../docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md);其所有权机制面已在第 9 章讲过,本节讲审计方法面。)

长期全部通过的单后端全自举闸门突然失败。报告形态不是"某个功能坏了",而是 stage1 在构建 pcc2 时硬错误:`PCC-PY-COMPILE-001 ... Python pipeline requires libpython fallback for multi-file compile (modules: pcc.frontends.python.codegen.user_function_lowering)`。审计的第一步是把这句话读成模式标注的边界指认:失败在 **pcc0→pcc1 的严格 no-libpython 编译期**,不是运行时,错误自己点名了模块。

第二步是嫌疑排序而非代码阅读:失败模块属于最近的 LowIR/layer1 拆分提交范围(相对 v0.1.2 的 `fe1de470` 范围)——"近期改动是头号嫌疑"由 git 范围证据确认,而非感觉。根因是递归 LowIR 助手(`_low_ir_expr_to_value()` 等)缺少返回类型标注,类型推断给出 DynType,于是 `operand.ty == _LOW_F64` 这类比较从原生整数字段读取退化为动态属性操作,发射出 `py_cpy_getattr`、`py_cpy_call1` 等调用——**严格模式正确地拒绝了这份 IR,闸门按设计工作**。证据是量化的:上下文回退计数从 80 降到 0,而不是"表观上好了"。

第三步是纪律里最反直觉的一条:修掉第一道边界后闸门仍然红——pcc0 产出的 pcc1 在编译 [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) 时双重释放崩溃。调查没有把两件事捏成一个故事,而是开了第二条证据链(LLDB 回溯、生成 IR 比对),定位到与第一道边界完全无关的通用返回所有权 bug(机制见第 9 章)。两道边界、两个根因、两个修复、两组回归测试。

被否决的提案与被采纳的同样重要,调查里逐条留档:禁用元组自动 GC 追踪——**用户明确否决**,那是用弱化运行时语义换绿灯(纪律第 4 条);把调用结果改为借用——否决,等于改掉全局所有权契约;单文件裸编译探针报出的 `Function._fresh` 错误——标记为误导性踪迹,裸探针给了 mixin 错误的宿主上下文,只能当定位器。收尾是制度化:为 `user_function_lowering` 加上专属的 ON 模式回退金丝雀测试,并把整个审计方法写回 [AGENTS.md](../../AGENTS.md)——今天读到的"自举回归纪律七步",相当一部分就是这次调查的蒸馏物。

### 案例研究二:一个 rsplit 重新引入回退,棘轮拦截(日期早于案例研究一)

(来源:[docs/investigations/bootstrap-types-rsplit-libpython-fallback.md](../../docs/investigations/bootstrap-types-rsplit-libpython-fallback.md))

这个案例研究小而锋利,展示的是 15.5 里"第二道网"的工作方式。一次改动后,stage1 闭包**编译成功**——但回退棘轮测试失败:合并 IR 里出现 9 个 `py_cpy_*` 调用,基线是 0(报错原文:`fallback total grew past ratchet: 9 vs baseline 0`)。全部 9 个调用聚在生成函数 `user_pcc_frontends_python_types__class_type_from_dotted` 里,源头是一行 `name.rsplit(".", 1)`:`rsplit` 当时没有原生低层化,静默走了 libpython 桥。

修复选了最小的源码级形状:[pcc/frontends/python/types.py](../../pcc/frontends/python/types.py) 的 `_class_type_from_dotted` 改为单遍扫描记录最后一个 `.` 再显式切片——因为闭包已经原生支持字符串索引、切片、长度与相等,不支持的只是 `rsplit` 这一个方法。回归测试把不变式钉进 IR 层:以 `ir_scaffold_mode="on"`、`libpython_mode="off"` 多文件编译 `py_ast` 加 `types`,断言该生成函数体内不含任何 `py_cpy_*` 调用。

教训浓缩成一句:**"编译成功"不是声明,闭包扫描才是。** 9 个调用没有触发案例研究一那样的编译期硬错误,但棘轮基线为 0 意味着任何重新引入都是硬失败。两个案例研究合起来证明回退检测必须是多层的——硬错误拦截结构性的回退需求,IR 计数棘轮拦截无声渗漏;只有任何一层,另一类回归就会溜进不动点。

## 15.10 小结

自举不动点是 pcc 把"系统相干"变成机器可判定命题的装置。四个阶段各有语义:pcc0(CPython 宿主)→ pcc1 证明源码落在自身子集且闭世界成立;pcc1 → pcc2 证明原生运行时扛得住编译编译器的负载;pcc2 → pcc3 加字节比较证明自产编译器行为自稳定——不动点定义在自产编译器之间,允许 pcc1 带外来宿主指纹。机制层,`bootstrap.sh` 提供阶段机器(陈旧产物防御、发布屏障、三级验证阶梯),`cli_bootstrap.py` 是一个必须能被自己编译的 CLI,其方言处处是自举子集的指纹。证据是三项独立声明:IR 中 0 `py_cpy_*`(生成代码层闭世界)、无 libpython 链接(产物层独立)、签名归一化后字节同一(自指层确定性),各锁一层、互不蕴含。pcc1/pcc2 差异先分类后修,八类分类学连 unknown 都是合法答案;闸门体系三层金字塔以冻结 JSON 为权威状态、以单向棘轮防回退、以五 GC 矩阵为最重完成证据。与 Thompson 的边界必须诚实:不动点证明相干与确定,不证明可信;可刷新的信任根与双后端多样性缓解而不解决信任问题。两个案例研究从两侧夹住同一条纪律:回归先做因果审计、堆叠失败拆成两条证据链、永不弱化语义换绿灯;无声的回退渗漏靠基线为零的棘轮拦截。

## 练习

1. **读源码验证**:[scripts/bootstrap.sh](../../scripts/bootstrap.sh) 的 `run_stage()` 在编译返回 0 后仍可能把 `stage_returncode` 改成 127 或屏障的返回码。读出这两条改写路径各自防御什么失败形态,并解释为什么"输出文件存在且可执行"的检查必须放在 `stage_exec_barrier` 之前。

2. **读源码验证**:[tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) 的 `_byte_identical_after_normalize()` 在临时目录里对**副本**做 `codesign --remove-signature` 再比较。结合 15.3 的发布屏障,解释为什么绝不能对 `build/bootstrap-*/pcc{2,3}` 原件做签名剥离。

3. **追踪闭包**:[pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) 的 `-m MODULE` 默认路径 `_run_compiled_python_module_from_pcc1()` 把通用模块用 `backend=self`、`libpython=off` 编译成原生二进制再运行,而不委托宿主 Python。结合第 14 章的回退路由,说明这条默认路径为什么天然闭包安全;再说明 `--python-libpython=auto/on` 下的兼容子进程 `_run_python_module_from_pcc1_with_mode()` 靠 `PCC1_COMPAT_RUNNER_MANIFEST` 守住了哪条声明边界——为什么它必须显式声明模式,而不能默默借道 CPython。

4. **设计权衡论证**:15.1 论证了 pcc1 ≠ pcc2 是被允许的。假设要把闸门加强为"pcc1 == pcc2(签名归一化后)",列出至少三类必须先消除的 pcc0/pcc1 执行环境差异,并论证这笔投入对相干性证据的边际收益为什么低于(或高于)把同样投入花在五 GC 矩阵上。

5. **设计权衡论证**:案例研究一里,为 `user_function_lowering` 增加专属回退金丝雀被当作系统改进;但每个模块一个金丝雀会让测试数量随闭包模块数线性增长。设计一个替代方案(例如按生成函数前缀自动断言 0 `py_cpy_*` 的全闭包扫描),分析它相对金丝雀的检测粒度、失败定位速度与误报风险,并说明 [tests/fallback_baseline.json](../../tests/fallback_baseline.json) 棘轮已经覆盖了其中哪部分。
