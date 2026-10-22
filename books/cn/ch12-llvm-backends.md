# 第 12 章 LLVM 后端与 llvm_capi 对齐

pcc 对 LLVM 的立场写在七义务的第四条里:LLVM 是 oracle,不是 owner。义务的后一半——self 后端如何成为第一类执行根——留给第 13 章;本章讲前一半:pcc 如何使用 LLVM,以及为了不被 LLVM 的某一个 Python 绑定锁死,pcc 在仓库内重写了一层 llvmlite 形状的 IR 构建器与 LLVM-C 绑定([pcc/ir/](../../pcc/ir)),并用一套对齐(parity)测试法把新旧两条通路钉在同一语义上。本章回答三个必须回答的设计问题:为什么 llvmlite 当 oracle、llvm_capi 当受测方;为什么文本级 IR 重写收缩到只剩 `va_arg` 一个豁免;system-link 路径为什么不再把 IR 文本交给系统编译器。这三个答案背后是同一条原则:每一段 LLVM IR 文本,只允许被生成它的那套 LLVM 解析。

## 本章导读:LLVM 的两种角色

这一章先分清两个身份:LLVM 可以当可靠参照,但不能永远当 pcc 的所有者。llvmlite 先做 oracle,仓库内的 llvm_capi 做受测替身;两条路长期并存,靠 parity 测试证明它们生成和解析的是同一类语义。

- `--backend` 选择执行路线,compat 门选择哪套 IR builder,这两根轴不要混。
- parity 测试不是"输出差不多",而是把新旧路径的 IR、验证、运行结果和边界豁免钉住。
- 文本 IR 只能交给生成它的 LLVM 解析,否则版本差异会伪装成编译器 bug。

## 12.1 问题与设计空间:为什么要自己写一层 LLVM 绑定

pcc 最初站在 llvmlite 上。llvmlite 是 Numba 项目维护的 LLVM Python 绑定,提供两层 API:`llvmlite.ir`(纯 Python 的 IR 构建器,产出 IR 文本)和 `llvmlite.binding`(C 扩展,封装解析、验证、优化、MCJIT、目标机发射)。pcc 的 C 代码生成([pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py))与 Python 前端低层化(lowering)都是面向这套 API 写成的,仓库在 [pyproject.toml](../../pyproject.toml) 中锁定 `llvmlite==0.46.0`。

问题出在自托管上。llvmlite 的 binding 层是 CPython C 扩展:一个想脱离 libpython 的自举(bootstrap)二进制,不可能在运行时 `import llvmlite`。[pcc/ir/__init__.py](../../pcc/ir/__init__.py) 的模块注释把这个推论写得很直接:选定的自举策略"要求用一个手写的、pcc 可编译的 LLVM-C-API 绑定替换 llvmlite,使自托管二进制没有 Python-C-extension 运行时依赖"。

设计空间有三个选项:

1. **永远依赖 llvmlite。** 否决:与自托管不动点(见第 15 章)直接冲突,也违反义务 4——LLVM 永远是 owner。
2. **直接跳到 self 后端。** 否决:在 self 后端覆盖完整指令选择、ABI、重定位之前砍掉 LLVM,等于把成熟的优化器和多目标支持一起扔掉,且没有任何过渡期的对照参照。
3. **在仓库内写一个 llvmlite 形状的替身**:同名类、同签名方法的 IR 构建器,加一个走 LLVM-C API 的窄绑定;新旧可切换,默认行为不变。

pcc 选了第三条,而且在动手前先做了测量。[docs/plans/llvmcapi-beta4-backlog.md](../../docs/plans/llvmcapi-beta4-backlog.md) 记录了 β4.0 阶段(2026-04-21 标记完成)对真实负载的 API 调用追踪:在 py_corpus、内联 C、ir_passes 测试加一次 `-g` 运行上,pcc 对 llvmlite 的**外部**使用只有约 40 个 API——34 个 `ir.*` 构建器调用点加 5 个 `binding` 热 API(`parse_assembly` 与 `verify` 占绑定调用的全部);追踪里另有约 31 万次的"长尾"调用,全部是 llvmlite 在 `str(module)` 序列化时调用自己的内部实现,文本直出的替代品根本不需要复刻。这次测量把"重写 llvmlite"从听上去的无底洞,变成 backlog 里估算 800–1000 行的分层任务(Tier 1 十二个类撑起编译、Tier 2 常用指令、Tier 3 异常处理、Tier 4 长尾算术共享一个 binop 助手)。先测量使用面、再决定重写边界,是这一章最值得带走的方法论。

过渡的工程原则写在 [docs/plans/dual-llvm-backend-compat-plan.md](../../docs/plans/dual-llvm-backend-compat-plan.md):默认行为不变、每个新特性显式旗标门控、每个新后端必须有"退回 llvmlite"的策略、每阶段只动少数模块。这不是保守主义修辞——12.5 节的对齐测试法之所以成立,前提就是新旧两条通路长期共存、随时可切。

## 12.2 两根开关轴:`--backend` 与 compat 门

讲机制前必须先把两根经常被混为一谈的开关轴分开,否则后文的模式标注会失真。

**轴一:后端种类。** [pcc/backend/__init__.py](../../pcc/backend/__init__.py) 的 `_BACKEND_TABLE` 登记三种后端:`llvm`、`llvm_capi`、`self`,由 `--backend` 旗标或 `PCC_BACKEND` 环境变量经 `resolve_backend()` 解析。`llvm` 是公开默认;`self` 标记为 unsupported,未显式选择时 `resolve_backend()` 抛 `BackendUnavailable`(见第 13 章)。每个 `BackendConfig` 的 `cache_signature()` 进入编译缓存键,保证切换后端必然缓存失效。值得诚实标注的是:在 [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) 的运行路径里,只有 `self.backend == "self"` 有专属分支;`llvm` 与 `llvm_capi` 两种取值共享同一条求值器代码路径——这根轴今天主要区分的是 self 与非 self,`llvm_capi` 作为后端种类在表里注明是"占位选择",影响缓存签名而非执行路径。

**轴二:构建器库。** 真正决定"哪份代码在构造 IR"的是 [pcc/ir/compat.py](../../pcc/ir/compat.py)。代码生成模块不直接 import llvmlite,而是:

```python
from pcc.ir.compat import ir_c as ir      # C codegen
from pcc.ir.compat import ir_py as ir     # Python codegen
from pcc.ir.compat import ir_passes as ir # IR passes
```

compat 在 import 期读一组环境旗标:`PCC_USE_LLVMLITE` 全局退回 llvmlite,`PCC_USE_LLVMLITE_PY` / `PCC_USE_LLVMLITE_C` / `PCC_USE_LLVMLITE_PASSES` 按子系统退回。**默认(无旗标)走 `pcc.ir`**——这是已经发生的"默认翻转":更早的 opt-in 旗标 `PCC_USE_LLVMCAPI*` 在翻转时退役为 no-op,旗标语义从"敢不敢开新的"反转为"要不要退回旧的"。按子系统分门的理由是回归隔离:Python 前端构建器出回归,不必把 C 前端和 pass 层一起回滚。

调用点零改动是这层设计的硬指标。[pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) 首行 `from pcc.ir.compat import ir_c as ir` 之后,全文件不知道自己用的是哪个构建器。唯一一处形状差异由 compat 的 `set_struct_body()` 适配器吸收:llvmlite 的 `IdentifiedStructType.set_body(*elements)` 是可变位置参数且没有 `packed` 关键字,pcc 侧是单可迭代加 `packed=`;适配器按类的 `__module__` 前缀分派。

还有一处必须如实写明的不对称:[pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) 文件顶部至今是 `import llvmlite.binding as llvm`——宿主求值器的解析、验证、优化、MCJIT、对象发射主路径,今天仍由 llvmlite 的 binding 承担。`pcc.ir.binding` 的当前消费者是内存 pass 管线传输(12.7 节)、Python 前端的 `ir_pass_pipeline.py`,以及自托管闭包;它对 parse/verify/JIT/emit_object 全链路的胜任由独立闸门(gate)[tests/c/test_llvm_capi_end_to_end.py](../../tests/c/test_llvm_capi_end_to_end.py) 证明(12.5 节)。"构建器默认已翻转"与"求值器 binding 已替换"是两个不同的声明,本书只做前一个。

## 12.3 文本优先的构建器:[pcc/ir/ir.py](../../pcc/ir/ir.py)

[pcc/ir/ir.py](../../pcc/ir/ir.py) 的第一句话就是设计立场:"text-first IR builder……增量地以文本构造 LLVM IR(没有对象图),`str(module)` 产出完整 IR 字符串"。llvmlite 的 `ir` 层维护一张真正的对象图,序列化时再遍历它;pcc 的替身在每次 builder 调用时就把指令渲染成最终文本行。

这个决定有两个独立的理由。第一个来自 β4.0 的测量:那 31 万次长尾调用是 llvmlite 序列化自身对象图的内部机制,文本直出根本不需要它们存在。第二个更根本:**这个文件要被 pcc 自己编译**。模块注释写明实现哲学是"静态类型 Python、无重度反射、为自托管编译就绪";正文里处处是这条约束的痕迹——`ModuleRef.functions` 的注释解释为什么返回 list 而不是生成器("自托管审计把 `yield` 标为阻塞项"),`Module.__str__` 用 `while` 循环加 `_join_text` 拼接而非生成器表达式。[pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) 里的闭包映射函数把这条线收紧到链接层:`--ir-scaffold=on` 模式下,stage 二进制的源闭包包含 `pcc.ir.ir`(以 `user_pcc_ir_ir_*` 符号形态被原生编译进去),同时**剔除** `compat.py` 与 `binding.py`——注释直说,把 LLVM-C/JIT 绑定留在闭包里"就是把 libpython 拖回 self 后端路径的那只手"。于是分层很干净:自托管的 pcc1 用编译进自己的 ir.py 构造 IR 文本,消费这段文本的是 self 后端(见第 13 章),而不是进程内的 LLVM。

文本优先不等于丢弃结构。`Block` 持有 `InstructionRecord` 列表,每条记录是"文本行 + opname + 所属块"三元组;`_opname_of()` 从文本行首 token 提取操作名,使 codegen 仍能问"这是不是 alloca / 终结子"。`Value` 携带类型与引用形(`%tmp3`、`@glob` 或常量字面量),并保留一个指回定义指令记录的 `_instr`:llvmlite 允许事后改写指令的 fast-math 旗标列表,pcc 用 `_refresh_flags()` 重写记录文本来兑现同一可变语义。`IRBuilder` 用整数插入位 `_pos` 模拟 llvmlite 的插入锚,并刻意暴露 `_anchor` 属性别名——因为 pcc 自己的某些 codegen 助手直接保存/恢复 llvmlite 的内部名 `_anchor`。兼容做到这个深度说明一件事:对齐的对象不是文档化 API,而是**调用方实际碰过的一切**。

两处与 llvmlite 的语义对齐值得单独点名,因为它们都在后来的回归里付过学费(12.8 节):

- **整型驻留。** `IntType` 按位宽驻留,`IntType(64) is IntType(64)` 成立——codegen 里大量 `value.type is _I64` 形式的同一性检查依赖它,文本相等不够。
- **不透明指针下的 pointee 跟踪。** LLVM 15+ 的指针文本形态一律是 `ptr`,但 `PointerType` 仍保存 `pointee` 并让 `__eq__` 比较地址空间与 pointee;`_same_tracked_type()` 同样递归比较 pointee。教训写在调查文档里:不要"因为文本都是 `ptr` 就把所有不透明指针判等"——GEP 结果类型、装载宽度全靠被跟踪的 pointee 推导。

builder 还做防御式省略:同型 `sext`/`zext`/`trunc`/`fpext`/`fptrunc`/`bitcast` 直接返回原值不发指令(回归测试 `test_pcc_ir_builder_elides_same_type_casts` 钉住此行为)。`Value` 上挂着 `_is_unsigned`、`_pcc_unsigned_pointee`、`_pcc_unsigned_return` 三个 pcc 私有槽——第 4 章讲过符号性在 C 低层化阶段是与 IR 类型分离跟踪的元数据,这里是该元数据在构建器层的落点。文件尾部还有一族定元数(monomorphized)包装:`FunctionType___init__2`、`IRBuilder_call4_i32`、`scaffold_Constant_i64` 等,把可变参数构造拍平成定 arity 入口,服务于受限的类型化前端。最后,文本字节级与 llvmlite 完全一致(匿名临时名编号等)被显式排除在 β4.1 范围外,定级为 β4.3 polish——语义等价先于字节等价,这个分级在 12.5 节的测试设计里复现。

## 12.4 `binding.py`:ctypes 上的窄表面

[pcc/ir/binding.py](../../pcc/ir/binding.py) 自述为"`llvmlite.binding` 的 β4.2 drop-in",实现的正是 β4.0 追踪划出的窄表面:`parse_assembly(text)`、`ModuleRef.verify()`、`ModuleRef.functions` 迭代、`Target.from_triple` / `from_default_triple` / `create_target_machine`、`create_mcjit_compiler`、`ExecutionEngine.get_function_address`、`TargetMachine.emit_object` / `emit_assembly`、目标初始化,以及新增的 `run_passes` / `run_passes_on_ir`。宿主形态用 ctypes 打开 `libLLVM-C`:`_find_libllvm()` 依次试 `PCC_LIBLLVM_PATH` 环境变量、一张 Homebrew/Linux 候选路径表、`ctypes.util.find_library`;自托管形态下同一组函数声明改由 `pcc.extern` 在 AOT 编译期绑定([pcc/ir/__init__.py](../../pcc/ir/__init__.py) 就是那份 extern 声明清单)。

几处实现细节体现了"窄但正确"的取向:

- `_configure_bindings()` 为每个用到的 LLVM-C 函数显式设置 `argtypes`/`restype`,注释点明这是在 64 位平台上防指针截断;`LLVMInitializeNativeTarget` 是头文件宏而非导出符号,所以初始化函数对 x86 / AArch64 两族符号逐个 `_call_if_present`。
- 所有 LLVM 返回的 `char*` 经 `_consume_msg()` 复制后立刻 `LLVMDisposeMessage`;`LLVMParseIRInContext` 拿走 memory buffer 所有权,调用后不得重复释放;`create_mcjit_compiler()` 成功后置 `mod._owns = False`——执行引擎接管模块所有权。这些所有权注释与第 9 章的拥有/借用引用契约是同一种纪律,只是对象换成了 LLVM 句柄。
- `parse_assembly()` 把构建器默认的占位 triple `unknown-unknown-unknown` 改写为宿主 triple——MCJIT 对占位 triple 报 "No available targets",而 llvmlite 在未设 triple 时也是应用宿主 triple,行为对齐到这一层。
- `run_passes()` 走 LLVM 新 pass manager 的 C 接口 `LLVMRunPasses`,管线串语法与 `opt -passes=` 相同(支持 `default<O2>` 这类 profile),跑完后强制 `mod.verify()`。这个入口是 12.7 节内存管线传输的承载者。

## 12.5 对齐测试法:llvmlite 作 oracle,llvm_capi 作受测方

必答题之一:为什么角色是这个方向,而不是反过来?三个互相独立的理由。

**第一,历史不对称。** pcc 的两套 codegen 是面向 llvmlite 的 API 与行为写成的;llvmlite 路径编码了全部"既定意图"——每个调用点期望的返回类型、可变性、命名行为。`pcc.ir` 是后来为自托管写的替身,它的正确性定义就是"与既定意图一致"。受测方只能是新来者。

**第二,成熟度不对称。** llvmlite 经 Numba 生态多年生产负载检验;[pcc/ir/ir.py](../../pcc/ir/ir.py) 是单仓库新代码。把久经检验的一方当参照系,是第 3 章"与已知良好参照对比"调试原则(AGENTS.md 把它列为仓库三大调试支柱之一:C 用原生编译器、Python 用 CPython、`llvm_capi` 用 llvmlite)在后端层的实例。

**第三,可控变量。** compat 门(12.2 节)使同一份 codegen、同一份源文件、同一个测试,仅凭一个环境变量在两个构建器之间切换。差异出现时,变量只有构建器本身——这是一个受控实验,而不是两套系统的笼统对比。

但要说清 oracle 的边界:llvmlite 是**参照系**,不是真值来源。最终仲裁者是 LLVM 自己——两边的输出都必须过 LLVM 的解析器与验证器,执行对齐由 MCJIT 跑出的数值裁决。llvmlite 答错的地方(它毕竟也只是绑定),LLVM verifier 与运行结果会同时否决两边。

[tests/c/test_llvm_capi_ir_parity.py](../../tests/c/test_llvm_capi_ir_parity.py) 把这套思想编成三级闸门,每个测试用同一个 `_build(ir_mod)` 函数分别喂入 `llvmlite.ir` 与 `pcc.ir.ir`:

1. **可解析、可验证。** 两边的 `str(module)` 都必须通过 `llvm.parse_assembly` 加 `verify()`;受测方失败时把完整 IR 源文本打进断言消息。
2. **结构签名一致。** `_structural_signature()` 在**解析后的** ModuleRef 上提取(函数名、签名文本、每块指令数、每块终结子 opcode、全局变量名与类型)的元组并比较。签名刻意跳过匿名临时名编号——那是两个构建器之间无害的发散,被显式归入 β4.3 文本打磨而非 Level-1 硬闸。先在解析后的模块上比较,意味着比较的是 LLVM 眼中的结构,而不是字符串。
3. **执行对齐。** 最强的一级:`_jit_call_int_int()` 把两边的模块分别交给 MCJIT,调用函数比较返回值——`test_execution_parity_recursive` 用递归 fib 要求两边都算出 55。测试文件注释说得直白:LLVM 把两个模块都 JIT 成行为一致的机器码,语义等价才算被证明。

[tests/c/test_llvm_capi_end_to_end.py](../../tests/c/test_llvm_capi_end_to_end.py) 补上另一半闭环:完全不 import llvmlite,IR 由 `pcc_ir` 构造、由 `pcc_bind.parse_assembly` 解析、`verify`、MCJIT 执行(递归 fib、带 phi 的循环求和)、`TargetMachine.emit_object` 发射对象并检查 Mach-O/ELF/COFF 魔数,以及一个负向测试:坏 IR 必须被拒绝(`test_verify_rejects_bad_ir`)。对齐测试证明"新构建器与旧构建器等价",端到端测试证明"新构建器加新绑定自身成环"——两类证据缺一不可,因为前者依赖 llvmlite 在场,而自托管恰恰要求 llvmlite 退场。

pass 层也有同构的对齐:[tests/c/test_llvm_capi_pass_pipeline.py](../../tests/c/test_llvm_capi_pass_pipeline.py) 用 `run_passes_on_ir("mem2reg,instcombine")` 验证栈槽提升真的发生(`alloca`/`store`/`load` 消失、`ret i32 42` 出现),再把内存管线输出与外部 `opt` 文本管线输出经 [pcc/ir_passes/parity.py](../../pcc/ir_passes/parity.py) 的 `normalize_ir()`(剥 ModuleID/`attributes #N`、按函数稠密重编号匿名 SSA 名、折叠空行)归一后逐字比较。

方法论的最后一块是适用边界,[docs/debugging-playbook.md](../../docs/debugging-playbook.md) §3 与 12.8 节的调查文档都强调:llvmlite 是 builder 缺指令、常量 GEP/bitcast、不透明指针类型语义、函数指针退化这类问题的 oracle;它**不是**预处理器行为、fake-libc 头策略、解析器接受性、编译期诊断的 oracle——两个构建器同样失败,bug 就在构建器层之上,硬把失败塞进 backend-parity 故事只会浪费证据链。

## 12.6 IR Fix Policy:文本重写的棘轮

AGENTS.md 的 IR Fix Policy 一节是这条规则的现行宪法:语义 bug 属于解析器/代码生成的源逻辑;`postprocess_ir_text()` 仅可用于 IR 构建器无法直接表达的窄低层化缺口;**当前可接受的文本级低层化只剩 `va_arg` 一条**,其余任何文本修复都是应当在序列化前修掉的编译器源 bug;不得用文本重写遮掩 CFG、类型或符号性 bug。

这是一个棘轮收紧的终点,而非一开始就有的洁癖。git 历史里 2026-03-23 的 AGENTS.md(提交 `bc1f1018`)记录了当时 `postprocess_ir_text` 的全部在册修复:`bitcast` 整数到指针改写为 `inttoptr`;Python `<ir.Constant>` repr 泄漏进 IR 改写为 `zeroinitializer`;删除 void 的 `alloca/load/store`;switch 重复 case 去重;终结子后死代码删除;连续空标签补 `br`/`unreachable`。六类修复,每一类都是 codegen 源头 bug 的文本绷带——repr 泄漏意味着常量低层化在某条路径上把 Python 对象直接 `str()` 进了 IR;空标签意味着 CFG 构造在某处漏发终结子。当时的政策只能要求"文本修复集中在一处、别散进测试助手"(那份文档已经点破:只存在于测试代码里的修复救不了 `uv run pcc`)。后来的工作把六类绷带逐一变成源头修复,棘轮收紧到今天的形态。

今天的 `postprocess_ir_text()`([pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py))是一行委托,真正的实现搬进了 [pcc/frontends/c/codegen/c_varargs.py](../../pcc/frontends/c/codegen/c_varargs.py) 的 `postprocess_varargs_ir()`。豁免的理由是构建器表达力,不是语义:llvmlite 的 IR 层在 pcc 使用的子集里不能直接发射 LLVM 的 `va_arg` 指令,于是 C codegen 先发射对 `__pcc_va_arg_N` 助手的普通调用占位,文本期用两个正则(`_PCC_VAARG_DECL_RE` 删助手声明、`_PCC_VAARG_CALL_RE` 把调用改写为真 `va_arg` 指令)完成低层化。这正是政策定义的"builder 无法直接表达的窄缺口"——改写不改变任何已表达语义,只是补一条构建器发不出的指令。

且这条豁免不是默许,是被审计的:`postprocess_ir_text_with_report()` 返回结构化的 `VarargsRewriteReport`(schema 字符串 `pcc.c.varargs_rewrite.v1`,逐条记录助手名、左值、参数类型与值、结果类型)。一个被迫保留的文本重写,至少要可计数、可序列化、可被工具看见。

为什么对文本重写如此严苛?因为正则看不见 CFG 和类型系统。一条在 verify 之前改动文本的规则,把 codegen bug 搬到了整条管线中最难归因的位置:IR 文本是 codegen 的输出、LLVM 的输入,在这里打补丁等于同时弄脏两侧的证据。第 4 章的符号性教训(位形正确但符号元数据丢失)在文本层会以更隐蔽的方式复发——文本重写根本看不到"这个值应当无符号"这件事。

## 12.7 system-link 路径:优化进内存,对象直发

`--system-link` 模式(多翻译单元编译后由系统工具链链接成真实可执行文件)是版本错位问题的历史高发区。现行实现([pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py))的链路是:

```text
.c 文件 ──compile_translation_units()──► (unit_name, ir_text, ...) 列表
                                              │  每单元
                                              ▼
                          _prepare_llvm_module(unit_name, ir_text, tm)
                            ├─ 仓库管理的 LLVM 优化(三选一,见下)
                            └─ llvm.parse_assembly + 可选 PCC_DUMP_BAD_IR 转储
                                              │
                                              ▼
                          target_machine.emit_object(llvmmod) ──► .o 文件
                                              │
                                              ▼
                      系统 cc:仅链接 .o(+平台旗标,Linux 加 -no-pie)
```

`run_translation_units_with_system_cc()` 先走前端得到各 TU 的 IR 文本,随后 `run_compiled_translation_units_with_system_cc()` 对每个单元调用 `_prepare_llvm_module()` 解析并优化,再用 `target_machine.emit_object()` 直接发射原生对象字节写成 `.o`,最后拼一条只含对象文件的 `cc` 命令链接。**系统编译器从头到尾没有见过一个字节的 LLVM IR 文本**——它只做它最不挑版本的事:链接。

优化发生在哪套 LLVM 里,有三条受控路由,全部由仓库管理:

- 默认:`_apply_llvm_optimizations()` 经 `PassPipeline.run_backend_tier` 在 llvmlite 进程内优化已解析的模块;
- `PCC_LLVM_PIPELINE` 显式文本管线:[pcc/frontends/c/passes/llvm_text_pipeline.py](../../pcc/frontends/c/passes/llvm_text_pipeline.py) 用 `opt --print-pipeline-passes` 展开 `default<O2>` 之类的 profile、剪除禁用 pass、再经 `opt -S -passes=...` 执行——关键在 `find_opt_binary()`:候选 `opt` 的版本必须与 llvmlite 的 LLVM 版本**相等**,否则宁可返回 None 也不用;
- `PCC_LLVM_PIPELINE_TRANSPORT=memory`:同一管线串改经 `pcc.ir.binding.run_passes_on_ir()`(`LLVMRunPasses`)在内存中执行,免去 subprocess 与两次文本往返。

必答题之三在此有了完整答案:system-link 不再交 IR 文本给系统编译器,因为那是唯一能让"两套不同版本的 LLVM 解析同一段 IR 文本"的场景,而 12.8 节的事故证明该场景结构性不可靠。现行设计的不变式是:IR 文本只在"生成它的那套 LLVM"内部流动(llvmlite 自己、版本核对过的同版本 `opt`、或同一 libLLVM 的 `LLVMRunPasses`),跨工具链边界只传递原生对象。AGENTS.md 为未来留了一条防御条款:若有朝一日重新引入文本 IR 交接,属性剥离(`nuw`、`nneg`、`range()`、`initializes()`、`dead_on_unwind`)必须集中在 `postprocess_ir_text()`——这是对事故的制度记忆,不是对现状的描述:在今天的代码里没有任何属性剥离逻辑,因为没有需要它的交接。

## 12.8 历史与教训

### 12.8.1 LLVM 版本错位与五个属性(2026-03-23)

**症状。** pcc 把 IR 文本写到文件、再用 `cc -c` 编译时,凡是经过 O2 优化的 IR 会被系统 clang 的解析器拒绝;同样的 IR 在 JIT 路径上运行良好。

**根因。** llvmlite 捆绑自己的 LLVM,版本通常比系统 clang 新。新版本优化器会在输出里发射老解析器不认识的属性:`nuw`(getelementptr 上的新用法)、`nneg`、`range()`、`initializes()`、`dead_on_unwind`。JIT 路径无恙的原因毫无神秘感——llvmlite 自己解析自己的输出,版本天然一致;只有"文本写出、另一套 LLVM 读入"的交接会爆炸。

**当时的均衡与代价。** 2026-03-23 的 AGENTS.md(提交 `bc1f1018`)记录了当时的工程妥协:测试编译路径干脆**不跑** LLVM 优化以绕开属性问题,文档建议"若必须用系统编译器编优化后的 IR,就用正则后处理剥掉这些属性"。两个选项都是亏损:前者让 system-link 产物失去 O2,后者是在追逐一个随 LLVM 版本持续漂移的属性集——正则永远落后一个版本。

**真正的修复与留下的不变式。** 消灭交接本身:`run_translation_units_with_system_cc()` 重写为"仓库 LLVM 在内存中优化 → `emit_object()` 直发原生对象 → 系统 cc 只链接";显式文本管线的 `find_opt_binary()` 以版本相等为硬条件。不变式由此确立:**LLVM IR 文本不跨 LLVM 版本边界**。AGENTS.md 现行 IR Fix Policy 里那句"若重新引入文本 IR 交接,把属性剥离集中到 `postprocess_ir_text()`"就是这场事故的墓志铭——它存在的意义是让重蹈覆辙者至少把伤害集中在一处。

### 12.8.2 oracle 调试法的成形(2026-04-18)

**背景。** 构建器默认翻转到 `pcc.ir` 之后,回归开始以新形态出现:同一份 C 源,默认构建器下编译失败或运行错,而没有人能立刻说清是 codegen 的语义 bug 还是替身构建器的对齐缺口。[docs/investigations/llvm-capi-vs-llvmlite-oracle-debugging.md](../../docs/investigations/llvm-capi-vs-llvmlite-oracle-debugging.md)(2026-04-18 入库)记录了从这批回归里沉淀出的标准流程。

**方法。** 五步:最小化重现 → 默认环境跑一次 → `PCC_USE_LLVMLITE_C=1` 再跑同一重现 → 按三种结果分诊(仅 `llvm_capi` 失败 = 对齐缺口;两边同败 = bug 在构建器层之上;两边都编译但运行不同 = 转去 dump 前端未优化 IR 做最小结构 diff)→ 只修最小语义缺口,加一个聚焦回归再跑一个真实确认。调查文档特意给出 dump IR 的代码模板:同一段源经 `_compile_translation_unit_artifact_job` 取 `ir_text`,两种环境各跑一遍,diff 最小结构差异。

**真实修出的缺口**(每一个都是这套流程的产出,也都进了回归测试):builder 缺 `uitofp`/`fptoui`/`fpext`/`fneg` 四个指令方法;常量表达式形态的 `Value.gep()` / `Value.bitcast()` 不工作(全局初始化器里的常量 GEP 变成 `null` 或 `0`);`int ()` 形参应按 C 语义退化为函数指针、而不是固定零参函数类型;以及 12.3 节点过名的——不得仅凭文本同为 `ptr` 就把所有不透明指针类型判等。

**教训。** 两条。其一,对照实验先于猜测:在有内建 oracle 的子系统里,任何"我觉得是 X"都应当先被一次 `PCC_USE_LLVMLITE_C=1` 复跑廉价地证实或证伪——这正是 compat 门按子系统设旗标的回报。其二,oracle 有边界:调查文档用整节列出 llvmlite **不是**哪些问题的答案(系统预处理器、头文件 shim 策略、编译期诊断、解析器接受性),并给出判据"两边同败,bug 在上层"。把每个失败都塞进 backend-parity 叙事,和从不做对照一样,都是在浪费证据。

## 12.9 小结

本章的 LLVM 侧故事由一条原则贯穿:**IR 文本只被生成它的那套 LLVM 解析**。由它推出三个机制:llvmlite 形状、文本优先、pcc 可自编译的替身构建器 [pcc/ir/ir.py](../../pcc/ir/ir.py) 加窄绑定 `binding.py`(为 no-libpython 自托管准备,见第 13、15 章);以 llvmlite 为参照系、以 LLVM parser/verifier/MCJIT 为仲裁的三级对齐闸门加端到端闭环闸门;以及"优化进内存、对象直发"的 system-link 链路。配套的政策面同样收敛于诚实:文本级 IR 重写收缩到唯一的、带结构化报告的 `va_arg` 豁免;compat 门按子系统提供受控回退;声明保持模式标注——构建器默认已是 `pcc.ir`,而宿主求值器的 binding 主路径仍是 llvmlite,两者不可混为一谈。方法论上,本章给出的可迁移件有三:重写前先追踪真实 API 使用面;给每个替身配一个一键切换的 oracle;把不得不留的脏豁免变成可审计的结构化报告。

## 练习

1. **读源码验证。** 在 [pcc/ir/ir.py](../../pcc/ir/ir.py) 中找到 `PointerType.__eq__` 与 `_same_tracked_type()`,再找到 `IRBuilder` 中至少一处依赖 pointee 推导结果类型的指令发射(提示:GEP 或 load)。解释:若把 `__eq__` 改成只比较 `str(self) == str(other)`,哪一类 IR 会先出错,错误会在 parse、verify 还是运行期暴露?
2. **读源码验证。** 跟踪 `postprocess_ir_text_with_report()` 从 [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) 到 [pcc/frontends/c/codegen/c_varargs.py](../../pcc/frontends/c/codegen/c_varargs.py) 的完整调用链,写出 `_PCC_VAARG_CALL_RE` 改写前后的指令文本各一行(可对照正则各分组构造),并说明为什么删除助手**声明**与改写助手**调用**必须配对发生。
3. **实验设计。** 给定一个"默认构建器下 Lua 编译失败"的报告,写出你按 12.8.2 流程要执行的前四条命令(含环境变量),并为三种分诊结果各写一句下一步行动。不要实际运行。
4. **设计权衡论证。** β4.1 选择了"文本优先、无对象图"的构建器。列出这个选择放弃的至少两种能力(提示:构建后变换、跨引用一致性维护),再论证为什么在 pcc 的架构里这两种能力的缺失是可接受的——你的论证应当引用 pass 层与 self 后端各自消费 IR 的方式。
5. **设计权衡论证。** AGENTS.md 允许在重新引入文本 IR 交接时把属性剥离集中到 `postprocess_ir_text()`。假设某个部署场景确实只有系统 clang 可用作后端(没有 llvmlite、没有同版本 `opt`),请对比"剥离属性后交文本"与"降级为 O0 交文本"两个方案在正确性风险、性能损失、维护成本三个维度上的差异,并给出选择结论、以及要求附带什么闸门。
