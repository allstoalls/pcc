# 第 13 章 self 后端:没有 LLVM 的原生发射

第 12 章里 LLVM 是 pcc 的发射引擎;本章它退为参照物。[pcc/backend/](../../pcc/backend) 下的 self 后端是一个不调用 LLVM 库的原生汇编发射器:读入 LLVM IR 文本,直接写出 AArch64 Darwin(及一个 x86_64 Linux 子集)汇编。它存在的理由写在七义务的第 4 条——self 后端必须成为第一类执行根,LLVM 是 oracle 而不是所有者。本章讲清三件事:为什么自举(bootstrap)不动点逼出了这个后端;一个刻意不做全局寄存器分配的"asm 优先"发射器如何切层(目标无关的解析/布局/栈槽层,目标特定的发射族,文本窥孔层);以及两条边界义务——`--backend=self` 之后禁止静默回退(fallback)LLVM,`_link_with_self_backend` 不得把 `pcc.backend.*` 拉回 stage1 闭包。后端的输入从哪来(Python 前端低层化,见第 6 章)与自举闸门(gate)本身(见第 15 章)不在本章。

## 本章导读:self 后端的边界与指令面

这一章不要从汇编细节开始。先抓住 self 后端的边界:它读 LLVM IR 文本,自己做指令选择和 ABI 发射,但仍把汇编/链接交给系统工具;它可以拒绝不支持的形状,但不能在 `--backend=self` 后静默绕回 LLVM。

- self 后端存在是为了自举所有权,不是为了立刻取代 LLVM 的全部优化能力。
- "asm 优先"的意思是先把正确汇编文本发出来,暂时不做完整寄存器分配和对象文件写出。
- 每个 `BackendUnavailable` 都是诚实边界:不猜、不偷跑、不伪装成已支持。

## 13.1 问题与设计空间:为什么要自己写后端

先回答"LLVM 明明能用,为什么还要写一个"。答案不在性能,在所有权链条的最后一环。pcc 的论题(见第 1 章)要求编译产物 native、auditable、self-hostable、no-libpython;自举链 pcc1→pcc2→pcc3 要求 pcc1——一个不链接 libpython 的原生二进制——能自己完成原生发射。如果发射必须经过 LLVM 库,那么每个编译阶段都要么链接一个巨大的外部 C++ 代码库,要么在运行时加载它;不动点里永远嵌着一个 pcc 不拥有、不可逐行审计的黑盒。[goal-prompt.md](../../docs/goal/goal-prompt.md) §10 把这条路堵死:"LLVM may be used as an oracle and fallback baseline, but it must not become the final dependency."

但"不依赖 LLVM"必须按声明卫生拆开说,因为 LLVM 在这里以三种身份出现,去留各不相同:

1. **LLVM 作为库**:在严格自举链上不存在。编译出的 pcc1 用 [pcc/ir/ir.py](../../pcc/ir/ir.py)——一个纯 Python 的"text-first IR builder"(其文件头自述:增量构造 IR 文本,无对象图)——生成 IR 文本,self 后端解析这份文本发射汇编,系统 `cc` 汇编与链接。整条链上没有任何 LLVM 库调用。
2. **LLVM 作为 IR 方言**:保留。self 后端的输入仍是 LLVM IR 文本格式。这是一个刻意的桥接决定:同一份 IR 既可喂给 LLVM 也可喂给 self 后端,差分对照([tests/c/test_llvm_self_vector_parity.py](../../tests/c/test_llvm_self_vector_parity.py) 把同一段 IR 分别经两条路编译运行、比较退出码与输出)因此可行;[tests/c/test_c_testsuite_self.py](../../tests/c/test_c_testsuite_self.py)、[tests/c/test_gcc_torture_self.py](../../tests/c/test_gcc_torture_self.py) 把整个 C 测试集当作 oracle 差分源。
3. **LLVM 作为优化器**:默认不在,opt-in 才在。C 路径入口 [pcc/driver/cli_core.py](../../pcc/driver/cli_core.py) 的 `_effective_self_backend_opt_level()` 在 `--backend=self` 时把优化级别压到 0,除非环境变量 `PCC_SELF_BACKEND_VECTORIZE` 显式打开——其文档注释直说原因:self 后端尚未低层化 LLVM 向量化器产物(如 Lua 的 `<4 x ptr>` strcache 广播)。打开后的组合必须按模式标注:LLVM 优化、self 发射,两种身份不混。

设计空间里被放弃的方案同样重要。(a)写一个 LLVM 式的完整后端——指令调度、图着色寄存器分配——会让这个子系统的复杂度淹没仓库的其余目标;(b)解释器或 JIT——产物形态不对,论题要的是可分发的原生二进制。选定的是(c)**asm 优先的有界忠实子集**:`self_backend_aarch64_darwin.py` 的模块文档把当下支持面逐条列出(标量整数、指针、`alloca`/`load`/`store`、直接调用、整数算术/比较/分支/phi、标量转换),并以一句话立下本后端最重要的不变式——"Unsupported shapes still raise `BackendUnavailable` instead of guessing."(不支持的形状抛 `BackendUnavailable`,而不是猜。)这一句是义务 4 在指令粒度上的落点:后端宁可拒绝编译,不产出语义存疑的代码,也绝不悄悄换路。

明示选入的机制在 [pcc/backend/__init__.py](../../pcc/backend/__init__.py):`_BACKEND_TABLE` 给 `self` 标 `supported: False`,语义版本 `self-aarch64-asm-v0`,能力面 `emit-asm` / `emit-object` / `run-native-via-system-cc` / `aarch64-darwin-mvp`。`resolve_backend()` 对不支持的后端默认抛 `BackendUnavailable`;`backend_request_allows_unimplemented()` 只在请求**点名** `self`(CLI 或 `PCC_BACKEND` 环境变量)时返回 True。也就是说,选择实验性后端这件事本身就是选入动作,没有"默认滑入"的路径;`BackendConfig.cache_signature()` 把 `kind:semver:support` 写进缓存身份,保证不同后端的产物在缓存层也不会串。

最后一条诚实边界:**self 后端拥有指令选择与 ABI,不拥有汇编器与链接器**。能力名 `run-native-via-system-cc` 说得很白——`.s` 文本交给系统 `cc -c` 变成目标文件,再由 `cc` 链接,Darwin 上还要 `codesign`。机器码编码器与 Mach-O 写出器是被委托的。"asm-first"指的正是这条边界:它让后端的正确性面收窄到"汇编文本是否正确",而把目标文件格式的字节级细节留给平台工具——这是一个 MVP 决策,不是终态宣言。

## 13.2 目标无关核心:文本解析、布局代数与栈槽分配

[pcc/backend/](../../pcc/backend) 的切层原则写在 `self_backend_parse.py` 的模块文档里:文本 LLVM IR 的处理逻辑归目标无关层所有,"解析结果应当被今天的 AArch64 Darwin 与以后的 x86_64 Linux 复用,而不是每个目标重新实现一遍 IR 文本处理"。`self_backend.py` 本身只剩一个十几行的兼容门面(老的单文件后端曾住在这里)。

**解析层**(`self_backend_parse.py`)是一组按行匹配的正则:`_ALLOCA_RE`、`_STORE_RE`、`_BINOP_RE`、`_ICMP_RE`、`_CALL_RE`、`_GEP_RE`、`_PHI_RE`……`parse_self_backend_module()` 产出 `ParsedModule`(triple、全局、函数),函数体由 `_parse_blocks()` 切成带标签的基本块,每行经 `_parse_instruction()` 归一化为 `ParsedInstr(kind, data)` 元组,终结子单独走 `_parse_terminator()`。两个细节值得点名,因为 13.7 的案例研究会回到它们:其一,`split_top_level()` 是唯一合法的逗号切分器——它跟踪花括号/方括号/尖括号/圆括号/引号深度,保证 `{ i64, i64 }` 这样的聚合类型不会被字段逗号撕开;其二,常量表达式(`getelementptr` 常量、`inttoptr` 常量、嵌套 cast)被 `decode_value_token()` 归一化成 `gepconst:base:offset`、`gep0:base`、`inttoptrconst:N` 这样的前缀小语言,发射层据此免于再碰原始文本。无法归一的形状照例抛 `BackendUnavailable`;`check_simple_symbol_name()` 把符号面限制在简单 C 标识符。

**数据模型**(`self_backend_ir.py`)围绕 `TypeDesc` 展开:一个 frozen dataclass 同时承担类型描述与布局代数——`slot_size`(内存中的存储尺寸,含结构体字段对齐折叠)、`value_slot_size` / `value_align`(SSA 值槽位用的最小 4 字节版本)、`field_offset()`、`aggregate_member_info()`(沿索引链算出成员类型与字节偏移,供 `extractvalue`/`insertvalue`/GEP 共用)。`ParsedFunction` 携带发射所需的全部账本:`value_types`、`value_slots`、`alloca_slots`、`block_map`、`hidden_sret_slot`、`frame_size`。

**分析层**(`self_backend_analysis.py`)只做轻量级数据流:`instruction_defined_value()` / `instruction_used_values()` / `terminator_used_values()` 按指令种类枚举定义与使用;`collect_used_values()` 给出全函数使用集(没人用的结果不分配槽位、不发射计算);`collect_block_local_last_uses()` 找出"只在定义块内使用"的值及其最后使用点——这是下一步槽位复用的全部依据。没有全局活跃区间,没有干涉图:这是刻意的预算分配,正确性优先,质量靠后面的窥孔挽回一部分。

**栈槽分配**(`self_backend_stackprep.py` 的 `assign_stack_slots()`)是 self 后端最重要的一个设计决定的载体:**不做寄存器分配,每个被使用的 SSA 值落一个帧内栈槽**。参数先入槽;`aggregate_returned_indirect(ret_type)` 为真时再留一个 `hidden_sret_slot` 存放调用方传来的返回缓冲指针;随后逐块逐指令为每个有使用者的结果值登记槽位。唯一的优化是块内复用:`alloc_value_slot()` 先查 `block_local_last_uses`,块内死亡的值在最后使用点之后把槽位归还 `free_slots` 自由表,后来者尺寸与对齐都不超时可以接住。帧总尺寸最后对齐到 16 字节。这个"全部落栈"的模型换来的是发射的彻底局部性:每条指令的代码生成只需"从槽位装入固定暂存寄存器→计算→存回目标槽位",既不需要跨指令的寄存器状态,也保证了输出的确定性——`tests/c/test_self_backend.py::test_self_backend_stack_slot_assignment_is_hash_seed_stable` 专门钉住槽位分配不随哈希种子漂移,因为发射顺序的任何不确定都会直接打碎 pcc2/pcc3 的字节比较(见第 15 章)。近期新增了一个不推翻"全部落栈"模型的限定项:`self_backend_aarch64_darwin_regalloc.py` 加了一层**可选的**保守块内寄存器投影——每个值仍照旧分配栈槽,但一个整数/指针 SSA 值只要其整个生命期被证明留在单个受支持的基本块内,就可以额外落在一个调用者保存寄存器(x1–x8,无调用的标量低层化本就不用它们)里;任何含调用、phi、原子或未归类指令的块整块被拒,所以栈槽模型仍是正确性底座——这依旧不是 13.1 里被放弃的图着色分配器。

**模块符号**(`self_backend_module_symbols.py`)解决多模块链接的命名冲突:`prepare_module_symbols()` 把模块公开符号集排序后取 SHA-1,得到 `__pccmod_<hash前缀>_` 作为本模块 internal 符号的前缀。两个模块各自的 `internal` 函数同名不再冲突,且前缀由内容决定而非随机数——又一次为字节级确定性让路。

## 13.3 分派骨架:目标注册表与两张分派表

目标选择曾经是散落的条件判断;`self_backend_targets.py` 的模块文档明说要止住这个趋势:"stop growing target dispatch as ad-hoc conditionals and instead expose a stable registry"。注册表是一张 dataclass 元组:

```python
SELF_BACKEND_TARGETS: tuple[SelfBackendTargetSpec, ...] = (
    SelfBackendTargetSpec(
        identity="self-aarch64-darwin-v0",
        matches_triple=is_aarch64_darwin_triple,
        emit_asm=emit_aarch64_darwin_asm,
    ),
    SelfBackendTargetSpec(
        identity="self-x86_64-linux-v0",
        matches_triple=is_x86_64_linux_triple,
        emit_asm=emit_x86_64_linux_asm,
    ),
)
```

入口 `emit_self_asm()`(`self_backend_dispatch.py`)从 IR 文本解析 target triple(没有 triple 直接 `BackendUnavailable`——self 后端不猜目标),`resolve_self_backend_target()` 匹配注册表,匹配不上同样拒绝。发射后还要过一遍 `run_self_target_pass_pipeline()`(13.4 末尾)。

函数体的发射骨架同样目标无关。`self_backend_emit.py` 的 `emit_function_blocks()` 按块序发标签、逐指令调发射回调、最后发终结子;`self_backend_instruction_dispatch.py` 把指令先递给 `emit_memory`(alloca/load/store),不认领再递给 `emit_compute`,都不认领就抛出带函数名与块名的 `BackendUnavailable`;`self_backend_terminator_dispatch.py` 对 `ret_void` / `ret` / `br` / `br_cond` / `switch` / `unreachable` 六种终结子各开一个回调参数。注意控制反转的方向:分派器是共享的,目标后端以可调用对象注入自己的发射器——AArch64 与 x86_64 两个目标消费同一副骨架,新目标按同样的形状插入。

## 13.4 AArch64 Darwin 发射族

发射族按职责拆成十余个 `self_backend_aarch64_darwin_*.py`。读它们的正确顺序是先 ABI 与寄存器约定,再单指令发射,最后看模块级的窥孔。

### 调用约定与 ABI(`_abi.py`)

`reg_name()` 按类型给寄存器名:整数 32 位以下 `w`,指针与 64 位 `x`,浮点 `s`/`d`。`assign_abi_arg_regs()` 维护 GPR 与 FPR 两个游标,各 8 个寄存器(x0–x7 / v0–v7);分不到寄存器的参数得到空元组,转入栈。聚合类型的规则收得很窄而且显式:`aggregate_reg_chunks()` 只接受纯整数/指针成员的聚合,尺寸 ≤8 字节走一个寄存器块,8–16 字节走 8+尾块两个寄存器,再大就 `aggregate_passed_indirect()` 为真——调用方把聚合落内存、传指针,被调方用一个 GPR 收指针。大聚合返回同规:`aggregate_returned_indirect()` 复用同一判定,返回缓冲指针按 AArch64 惯例走 x8,序言把它存进 `hidden_sret_slot`。栈上参数从 `[x29, #16]` 起算(越过保存的 fp/lr 对),`stack_arg_offsets()` 逐个 8 字节对齐排布。

Darwin 的变参偏离标准 AAPCS64,这里如实跟随平台:**变参一律入栈**,每个 8 字节(`variadic_stack_arg_storage_size()`);`emit_vararg_start()` 把 `va_list` 设为 x29 加"最后一个固定栈参之后"的偏移,`emit_va_arg()` 就是"读指针、装值、指针加 8、写回"四步,且只支持标量结果——越界形状照例拒绝。

### 常量与暂存寄存器(`_regs.py`)

`emit_const_to_reg()` 用 `movz`/`movk` 按 16 位块合成任意立即数,跳过零块;浮点常量优先试 `fmov` 直接立即数表(1.0、2.0),否则借 w12/x12 走整数位形再 `fmov` 过去。暂存寄存器的纪律是固定的:操作数惯用 x9/x10,结果惯用 x11,地址搬运用 x12–x15,`pick_scratch_gpr()` 在 x12–x15 里避让指定寄存器,挑不出来就抛错。这个固定分工不是美学:13.4 末尾的窥孔层要在纯文本上做活跃性推理,只有"x9–x15 是发射器自留地"这一约定成立,文本级别的"这个 mov 可以删"才是合法判断。

### 序言、返回与终结子(`_prologue.py`、`_returns.py`、`_terminators.py`)

序言是教科书形状:`stp x29, x30, [sp, #-16]!`、`mov x29, sp`、按 `frame_size` 调栈,然后把寄存器参数逐个存入各自槽位、栈参数经 `emit_fixed_stack_arg_load()` 搬入槽位、间接聚合参数按指针拷贝整块(`copy_address_to_slot()`)。`emit_return_terminator()` 分两路:间接聚合返回从 `hidden_sret_slot` 取回 x8 传来的缓冲地址,把返回值整块拷过去(零初始化与字面量各有快路);标量返回 `materialize_value()` 到 0 号寄存器(x0/w0/s0/d0)。两路共用 `emit_epilogue()`:还栈、`ldp x29, x30, [sp], #16`、`ret`。`unreachable` 发 `brk #0`。

phi 的处理值得单独一段,因为它是这个"无寄存器分配"模型里唯一的并行语义点。phi 在解析层就与普通指令分离(`ParsedBlock.phis`),发射时由**边**负责:`emit_branch_terminator()` 在跳转前调 `emit_phi_assignments()`;条件分支两条边目标不同,`emit_cond_branch_terminator()` 为假边造一个 `block_edge_label()` 标签块,各自完成各自的 phi 拷贝再跳真正的目标;`switch` 以"逐 case 比较 + 每边一个 phi 准备块"的链式形状发射——没有跳转表,这是当下子集的诚实形状。`emit_phi_assignments()` 内部处理经典的并行拷贝问题:若所有 phi 都是标量且来源不引用任何目的值,逐个直接存入;否则在 sp 下开一块临时区,先把全部来源值写进临时区、再统一搬入目的槽——两阶段隔离读与写,环形依赖(`a, b = b, a` 的 IR 形态)不会被串行覆盖破坏。

### 单指令发射(`_memory.py`、`_compute.py`、`_ops.py`、`_slots.py`、`_mem.py`、`_addr.py`)

`emit_memory_instruction()` 负责 `alloca`(编译期已折入帧布局,运行期零指令)与 `load`/`store`。每条都有一个快路:指针就是本函数 `alloca` 且类型吻合时,直接对帧槽位做 `stur`/`ldur`,免去地址物化;否则 `materialize_pointer()` 进 x9 再经 `store_value_to_address()`/`load_value_from_address()`。大聚合的存取退化为块拷贝(`copy_slot_to_slot()`、`copy_address_to_slot()`),`_mem.py` 按尺寸选 opcode(`ldurb`/`ldurh`/`ldur`,8/4/2/1 字节块逐级递减拼出任意尺寸的 `aggregate_copy_chunks()`)。

`emit_compute_instruction()` 是大分派:binop、icmp/fcmp、cast、select、freeze、extractvalue/insertvalue、向量子集(insertelement/extractelement/shufflevector 与逐 lane 的向量算术)、va_arg、GEP、call。标量路径全部遵守同一节奏——操作数 materialize 进 x9/x10,结果算进 x11,`store_reg_to_slot()` 写回。其中藏着一个与第 4 章符号性主题同源的细节:窄整数(i8/i16/i1)在槽位里**不保持规范的符号扩展形态**,而是在符号敏感的使用点重新规范化——`emit_compute_instruction()` 对 `sdiv`/`srem`/`ashr` 与有符号比较(`slt`/`sle`/`sgt`/`sge`)先对两个操作数调 `sign_extend_int_reg()`(`sxtb`/`sxth`,i1 用 `and`+`neg`)再发计算。C 代码生成层的教训(符号性元数据丢失)在这里被结构性规避:符号语义由 IR 指令种类携带,后端在用点付出重扩展的代价,换取槽位表示的单一性。

`materialize_value()`(`_materialize.py`)是全发射族的取值汇点,它按序识别:`null`/`poison`/`undef`/`zeroinitializer`、解析层归一的常量前缀(`gepconst:`/`gep0:`/`inttoptrconst:`/`ptrtointconst:`/`negconst:`/`addconst:` 与通用常量表达式前缀)、`alloca` 地址(x29 减偏移)、已有槽位的 SSA 值、浮点与整数字面量、可整体编码进 1–2 个寄存器块的聚合字面量、全局符号地址。全局地址的物化(`_addr.py` 的 `materialize_global_address()`)区分本模块定义与外部符号:前者 `adrp`+`add @PAGE/@PAGEOFF`,后者经 GOT(`@GOTPAGE/@GOTPAGEOFF` 装载)——这是 Mach-O 位置无关代码的标准形状。GEP 低层化(`emit_gep_offset()`)对常量索引直接折成立即偏移,变量索引用 `lsl` 移位或 `mul` 乘步长;结构体字段索引必须是常量,否则拒绝。

全局数据(`_data.py` 的 `emit_globals()`)按常量性落 `__DATA,__const` 或 `__DATA,__data` 节,初始化器递归展开:c 字符串 `.byte` 序列、标量 `.byte/.short/.long/.quad`、`zeroinitializer` 用 `.space`、指针初始化器可以是符号加偏移(常量 GEP 折叠成 `.quad symbol+offset`)。结构体按 `field_offset()` 补对齐空洞——布局代数与 13.2 的 `TypeDesc` 是同一份,数据节与代码对结构体偏移的理解不可能分叉。

### 文本窥孔层与目标 pass 钩子

槽位机器的代价是大量"存了马上读"的冗余。`emit_aarch64_darwin_asm()` 在拼完全部函数后,对汇编**行列表**连跑十七个窥孔 pass:相邻与隔一行的栈存/装转发(`_forward_adjacent_stack_store_load()` 等)、零常量与 mov 折叠进 store/compare/branch、`cset`+分支族的融合(`_fold_cset_zero_branch()` 把"比较、置位、存槽、零测分支"折成 `b.cond`)、死 `cset` 存储删除、蹦床分支穿透(`_thread_trampoline_branches()`)、条件分支落空翻转、落空无条件跳删除、无引用空标签清理。每个 pass 都极度保守:只动 9–15 号暂存寄存器(`_is_aarch64_scratch_reg()`),向后扫描遇到标签/分支/调用即放弃(`_can_drop_zero_mov_after_store()`),宁可漏优化不可错优化。

要把这个层与仓库的 IR Fix Policy(见第 12 章)区分开:那条政策禁止用文本重写隐藏语义 bug,因为被重写的是 IR——编译器各层共享的契约表面;而这里被重写的是 self 后端**自己刚发射的输出**,模式全部由同一发射器产生(它知道 `[x29, #-N]` 只能是自己的槽位流量),这是后端内部的窥孔优化,不是语义补丁。尽管如此,文本窥孔仍是脆弱性的来源,所以 `self_backend_target_passes.py` 已经搭好了下一步的形状:模块文档点名参照 LLVM 20.1.8 的 `PassManager.h` 与 `CodeGenPassBuilder.h`/`TargetPassConfig.h` 对 IR pass 与 codegen pass 的分离,提供 `PCC_SELF_TARGET_PASSES` / `PCC_SELF_TARGET_PASS_TRANSPORT`(`text` 或 `memory` 两种运输)与两个注册表;今天注册的只有 `strip-trailing-whitespace`(文本侧)与 `verify-prepared-module`(内存侧)两个最小 pass——这是一个钩子的诚实形态:接口先行,留给未来的目标级 IR pass 插入,而不是再长一层文本魔法。

## 13.5 x86_64 Linux 子集

`self_backend_x86_64_linux.py` 是第二个目标,模块文档同样以"intentionally narrow and explicit"开场:标量整数/指针参数与返回、`alloca`/标量 `load`/`store`、整数 binop、直接调用、`ret`。它复用 13.2–13.3 的全部目标无关层(同一解析器、同一 `prepare_module_for_target()`、同一分派骨架),证明切层不是纸面承诺。

目标侧的差异点有三个值得记。其一,语法选择:发射 `.intel_syntax noprefix`,操作数不带 `%` 前缀——这带来一个 AArch64 侧不存在的命名冲突:一个叫 `ax` 或 `and` 的全局符号会被汇编器读成寄存器或操作符。`_RESERVED_ASM_SYMBOLS` 枚举全部冲突名,`_asm_symbol()` 给撞名的本模块定义符号加 `__pcc_sym_` 前缀,并有专门回归(`test_self_backend_x86_64_linux_mangles_reserved_global_symbol_names`)。其二,ABI 形状:SysV 的 6 个 GPR 参数寄存器(rdi/rsi/rdx/rcx/r8/r9)与 8 个 xmm;聚合 ≤16 字节按 8 字节块进寄存器、更大走内存(`_is_memory_aggregate_arg()`)——与 Darwin 侧规则同源不同参。其三,暂存寄存器纪律对应收窄为 rax/rbx/r10/r11(`_reg_name()` 只接受 0/1/10/11 四个索引,越界直接 `BackendUnavailable`)。

按模式标注的要求说清现状:S-track 的目标矩阵里"x86_64 Linux parity"仍是开放扩张项,Darwin arm64 才是 supported-host 默认;这个子集靠 [tests/c/test_self_backend.py](../../tests/c/test_self_backend.py) 里数十个 `test_self_backend_x86_64_linux_*` 单测逐形状推进(phi 并行拷贝、向量 splat 子集、fp 调用与返回……),并有 [tests/c/test_self_backend_x86_64_harness.py](../../tests/c/test_self_backend_x86_64_harness.py) 作执行配套。它今天能编译的是测试钉住的形状集,不是"Linux 支持完成"。

## 13.6 工件成形与两条边界义务

### 从 asm 到可执行文件

C 路径([pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py))提供三种出口:`_emit_compiled_units_self_backend()` 支持 `--emit-llvm`(IR 文本)、`--emit-asm`(汇编文本)与 `--emit-obj`(写临时 `.s` 后 `cc -c`);`_run_compiled_translation_units_self_backend()` 把多个 TU 的汇编拼接(剥掉各模块的 `.subsections_via_symbols` 尾行,最后统一补一行)、`cc` 链接、直接运行。优化级别大于 0 时 `_prepare_self_backend_units()` 会先经仓库管理的 LLVM 优化一遍 IR——但回忆 13.1:CLI 默认把 self 后端的优化级别压到 0,所以默认路径不触发这一步。

Python 自举路径([pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py))是这条产物链的主战场。`_link_native()` 按 `_native_backend_kind()` 二选一:`llvm` 走 `_link_with_clang()`,`self` 走 `_link_with_self_backend()`,其余值抛 `PyPipelineError`——没有第三条路,也没有"self 失败就换 llvm"的代码路径存在。`_link_with_self_backend_ir_texts()` 内部分两种形态:单模块小输入走"宿主发射汇编、本进程驱动 `cc`";多模块(自举的常态)走 `_emit_self_objects_many_via_host_python()`——按 `jobs` 并行地"emit_self_asm → 写 `.s` → `cc -c` → `.o`",随后一次 `cc` 链接全部目标文件加运行时归档与 `-lm`。

这条路径上有三个为自举不动点服务的细节。第一,**内容寻址的目标文件缓存**:缓存键的 SHA-256 把所有 `self_backend*.py` 源文件的内容、`PCC_SELF_TARGET_PASSES` 等环境、`cc`、目标身份与 IR 文本全部喂进去——改任何一行后端源码,缓存自动失效,不会出现"旧后端的 `.o` 混进新自举"这种最难调试的脏状态。第二,**确定性链接旗标**:Darwin 上加 `-Wl,-no_uuid`(LC_UUID 是字节比较的天敌),`.subsections_via_symbols` 存在时加 `-Wl,-dead_strip`。第三,**发布序列**:`_finish_self_backend_executable()` 先链接到 `<out>.tmp`,Darwin 上 `codesign --force -s -` 临时签名、`codesign --verify` 强制系统校验器观察最终 Mach-O,`/bin/mv -f` 原子就位,最后做一次读回屏障(默认)或 `/bin/sync`(`PCC_SELF_BACKEND_PUBLISH_SYNC=1` 保留给可靠性二分)。这串看似偏执的动作每一步都对应一次真实失败,见 13.7.1。

### 义务一:`--backend=self` 之后禁止静默回退 LLVM

S-track([goal-prompt.md](../../docs/goal/goal-prompt.md) §10)把它写成首批 P0 闸门:S-P0-A 要求"喂给 self 后端一个不支持的 IR 形状,必须得到 self 后端的诊断、不得产出成功工件、不得回退 LLVM"。机制上这条义务分三层兑现:选择层,`resolve_backend()` 的显式选入(13.1);指令层,无处不在的 `BackendUnavailable`(参数化到函数名与块名);链接层,`_link_native()` 的封闭二分支与 `_link_with_self_backend` 失败时的 `PyPipelineError`(`"self backend native emission failed"`、`"self backend link failed"`)。为什么这条纪律值得占一个 P0?因为静默回退会让 S-track 的全部证据失真:一个"用 self 后端通过"的自举,如果其中三个模块悄悄走了 clang,那么字节比较、no-libpython 扫描、性能阈值全部测的是混合物。[AGENTS.md](../../AGENTS.md) 义务 4 的措辞——"No silent fallback to LLVM after `--backend=self`"——和声明卫生表里的 `S-CLAIM_RISK`(LLVM-backed / clang-linked / host-assisted 结果被误标成 self-backend 完成)说的是同一件事:回退本身不可耻,**未标注的**回退才是。

### 义务二:subprocess 边界——别把 `pcc.backend.*` 拉回 stage1 闭包

`_emit_self_asm_via_host_python()` 的形状乍看绕远:把 IR 写进临时 `.ll`,起一个子进程(`_host_python_command()`:`PCC_HOST_PYTHON` 指定的解释器,缺省找 `.venv` 再退 `python3`)执行一段内嵌代码 `_SELF_BACKEND_HOST_CODE`,由**那个**进程 `from pcc.backend.self_backend_dispatch import emit_self_asm` 完成发射,标准输出第一行带回目标身份、其余是汇编。为什么不在本进程直接 import?[AGENTS.md](../../AGENTS.md) 的"Subprocess vs in-process boundaries"一节给出禁令与理由:`pipeline.py` 自己会被编译进 pcc1;如果它在进程内 import `pcc.backend.*`,这些模块就进入 stage1 的编译闭包,而它们(以及解析它们所需的宿主功能)会把 `py_cpy_*` 兼容层拖回闭包——no-libpython 的边界就从根上破了。子进程把"运行后端的解释器"与"被编译的编译器"切开:stage1 闭包里只有"写文件、起子进程、读结果"这一小段,而这段在 no-libpython 下本来就被支持。

这条边界同时是一处必须模式标注的诚实点:今天 pcc1 做原生发射时,汇编逻辑是 pcc 的、运行它的解释器是宿主的——"host-emitted, self-backend-owned"。[AGENTS.md](../../AGENTS.md) 同一节写明长期目标:把这些后端模块本身编译成原生代码,而不是反向扩大进程内 CPython 回退。也就是说 subprocess 边界是一个**有方向的**过渡形态:方向是收编后端进闭包,而不是固化宿主依赖。

## 13.7 历史与教训

### 13.7.1 Mach-O 阶段发布竞态:产出二进制还不够,要拥有"发布"语义(2026-05)

**症状**([docs/investigations/self-backend-mach-o-stage-publish-race.md](../../docs/investigations/self-backend-mach-o-stage-publish-race.md)):后端 #4 GC 工作期间,长期green的自举闸门间歇性回归——stage2 刚链接出 `pcc2`,stage3 立刻执行它,约 0.2 秒后 SIGSEGV(exit 139)。同一个 `pcc2` 失败后**再跑一次就成功**,在 LLDB 下跑也成功。

**错误假设**:Python 前端语义 bug。证据链否决了它:稳定的语义错误不会"重跑即愈";失败点紧贴"新链接的 Mach-O 被立即 exec"这个边界。真根因是 macOS arm64 的装载器/签名状态:链接器直接写最终路径,文件内容已就位,但其可执行/签名状态对立即到来的 exec 尚不稳定。修复是分层叠上去的,每层都被后续失败证明不充分:先 `.tmp`+`mv` 原子改名——不够;加 `codesign --force -s -` 临时签名——降低但未消除;加 `codesign --verify` 强制系统校验器先观察最终文件——连续两轮通过。中途还有一处约束互锁:用 `os.replace()` 实现原子改名的尝试被严格自举**拒绝**,因为它在 no-libpython 闭包里引入了新回退——发布序列只能用已被支持的 subprocess 边界(`/bin/mv`、`/usr/bin/codesign`)搭。

**故事没有完**。2026-05-15 的 Update 记录:`codesign --verify` 之后 stage3 崩溃再现,崩溃报告指向 `py_decref`——可能是另一类(生成代码清理路径)问题与发布竞态叠在同一症状上;后续在 `self-bootstrap-reliability-performance-2026-05-15.md` 里继续:加 `/bin/sync` 后两轮通过,但全局 sync 引入主机级 I/O 方差(闸门从约 55 秒漂到 80 秒以上的一部分嫌疑),最终默认换成对签名后文件的读回屏障,保留 `PCC_SELF_BACKEND_PUBLISH_SYNC=1` 供二分。该调查同时确认了一个真实的性能根因:生成代码在每个循环/函数门上直接调用 `pcc_thread_safepoint()`,改为装载导出标志加慢路调用后,闸门从 82.30 秒降到约 71 秒。

**留下的不变式**:一个产出可执行文件的后端必须拥有**发布**语义,不只是字节正确;对非确定性失败类,"连跑两次通过"不构成关闭证据(调查原文把这条写进了 Next Steps:先有计时与崩溃率测量,再谈优化);堆叠修复时每一层都要单独记录它否决了什么。

### 13.7.2 `{ i64` ——一个逗号的切分政策与值模型推着后端长大(2026-06-04)

**症状**([docs/investigations/self-backend-valueclass-aggregate-call-signature.md](../../docs/investigations/self-backend-valueclass-aggregate-call-signature.md)):值类(value class)V2 边界工作中,一个装箱的 `Point` 经容器下标取回再传入类型化函数,严格 no-libpython 自举发射失败:

```text
BackendUnavailable: self backend does not understand LLVM type '{ i64'
```

**证据链**:三个假设逐一跑到判定。"valuebox 拆箱产出畸形 IR"——否决,IR 是合法文本;"self 后端聚合 ABI 传不了小载荷"——否决,直接的嵌套值类聚合冒烟早已通过,失败发生在目标 ABI 低层化之前;"`_parse_call_signature()` 按裸逗号切分签名"——确认。显式调用签名 `call i64 ({ i64, i64 }) @...` 的聚合参数被字段逗号撕成 `{ i64` 与 `i64 }`,前半截喂进 `_parse_type()` 当场拒绝。修复一行:改用 13.2 提到的 `split_top_level()`,与解析器其余路径的既有政策对齐;两个聚焦回归(解析器级与端到端值类级)加上完整五 GC 自举(458.24 秒,5 passed)封板。

同日的姊妹调查(`self-backend-nested-valueclass-payload-aggregate-return.md`)展示了同一股推力的更大画面:前端开始把非递归嵌套值类载荷低层化为字面嵌套结构 `{ { i64, i64 }, { i64, i64 } }` 后,self 后端解析器在函数头返回类型、`ret`、`extractvalue`、聚合返回调用四个边界上接连失败;修复把窄正则升级为"保留正则快路 + 结构感知回退"的解析。调查的 Report 一节专门强调:解析器改动**对字面嵌套聚合是通用的,没有为值类开任何特例**——这是义务 3(generic-mechanism)在后端的体现。

**留下的不变式**:文本 IR 解析器的切分纪律是一条全局政策,不是每个调用点的局部选择——`split_top_level()` 存在的意义就是不允许第二种逗号语义;self 后端的子集不是随机扩张的,**值模型的 ABI 义务(义务 7:self 后端的聚合/标量 ABI)是推着它长大的具体力**;以及,后端边界的失败要先定位"第一个失败的边界"(解析?ABI?运行时?)再动手——这次三个假设的判定顺序正是这个纪律的执行记录。

### 13.7.3 self 后端的二进制崩了,不等于 self 后端错了(2026-05-09,活跃)

一条简短但必要的对照([docs/investigations/stage1-self-backend-ir-scaffold-segfault.md](../../docs/investigations/stage1-self-backend-ir-scaffold-segfault.md),状态 active):`--backend self` 构建出的 pcc1 在编译 stage2 时段错误。最小化后的根因目前指向**前端**方法分派——`_emit_method_call` 的闭世界"任何声明了该方法的类"回退,把 `DynType` 接收者上的 `.append(...)` 错误绑定到 `pcc.ir.ir.Block.append`,把 `ir.Value` 传给了期望字符串指令行的函数。症状署名是"self 后端的产物崩了",根因不在 [pcc/backend/](../../pcc/backend) 的任何一行。这正是 [AGENTS.md](../../AGENTS.md) 自举回归纪律第 1 条的用武之地:先用模式标注的语言确定第一个失败边界(`pcc0→pcc1` 回退?`pcc1→pcc2` 运行期崩溃?),再列嫌疑子系统。在自托管链条里,后端是所有上游语义错误的最终显影液——它显影的多数照片,拍的都不是它自己。

## 13.8 小结

self 后端回答的问题是:不动点的最后一环——原生发射——能否也被 pcc 拥有。它的答案刻意地窄而诚实:

1. **身份**:LLVM 三种身份分开处置——库,自举链上不存在;IR 方言,保留作 oracle 桥;优化器,默认关闭、opt-in 须标注。`self` 在 `_BACKEND_TABLE` 里标 `supported: False`,选它本身就是选入实验性。
2. **结构**:目标无关层(正则解析、`TypeDesc` 布局代数、轻量活跃性、全落栈的槽位分配、内容哈希的符号前缀)+ 目标注册表与两张分派表 + 目标发射族(AArch64 Darwin 全家,x86_64 Linux 窄子集)+ 后端内部的文本窥孔 + 目标 pass 钩子。每个 SSA 值一个栈槽换来发射的局部性与确定性;质量损失由十七个保守窥孔挽回一部分。
3. **拒绝**:不支持的形状抛 `BackendUnavailable`,从类型 token 到 icmp 条件码无一例外。这不是未完成的歉意,是 S-P0-A 闸门的实现材料:静默猜测与静默回退是同一种谎言的两个面。
4. **边界**:工件经系统 `cc` 汇编链接、Darwin 上签名校验读回后才算发布;`--backend=self` 后没有任何通往 `_link_with_clang()` 的代码路径;pcc1 经 subprocess 调用宿主解释器运行后端,使 `pcc.backend.*` 不进 stage1 闭包——一个方向明确的过渡形态。

三个案例研究各钉一条边界上的不变式:发布语义属于后端的正确性面;文本解析的切分政策只能有一条,而推动子集扩张的是值模型的 ABI 义务;自托管链上的崩溃署名不可信,第一失败边界必须用模式标注的语言确立。

## 练习

1. **读源码验证**:阅读 `self_backend_stackprep.py` 的 `assign_stack_slots()`,写出一个值的槽位可被复用的全部条件(提示:`collect_block_local_last_uses()` 的"仅定义块内使用"判定、`free_slots` 的尺寸与对齐匹配)。解释为什么跨块存活的值被排除在复用之外,以及放开这个限制需要哪种分析升级。
2. **读源码验证**:沿 `i8` 的 `icmp slt` 走一遍 `emit_compute_instruction()`:操作数槽位里的字节何时不是符号扩展形态?`sign_extend_int_reg()` 在哪些指令前被插入,哪些指令(如 `udiv`、`eq` 比较)刻意不插?把这个"用点重规范化"策略与第 4 章 C 代码生成的符号性元数据策略做一次对比:各自的失败模式是什么?
3. **ABI 推演**:给定签名 `void f(double, { i64, i64 }, [3 x i64], i32)`,用 `assign_abi_arg_regs()` 与 `stack_arg_offsets()` 的规则手工推出每个参数的位置(寄存器名或 `[x29, #N]` 偏移),并指出 `[3 x i64]`(24 字节)走哪条路、调用方多做了什么。
4. **窥孔正确性论证**:阅读 `_can_drop_zero_mov_after_store()` 与 `_is_aarch64_scratch_reg()`。列出该函数放弃优化的全部触发条件,并论证:若把暂存寄存器范围从 x9–x15 扩到 x0–x8,哪个具体窥孔会产生错误代码?为什么"固定暂存寄存器纪律"是文本级活跃性推理合法性的前提?
5. **设计权衡论证**:subprocess 边界让 pcc1 的每次发射多付一次进程启动与(多模块时的)结果文件往返。替代方案是把 `pcc.backend.*` 编译进 pcc1 本体。结合 [AGENTS.md](../../AGENTS.md) 的"Subprocess vs in-process boundaries"与 13.6 的闭包论证,写出切换前必须先变绿的闸门清单(至少包括:这些模块在严格模式下的零回退编译、五 GC 自举矩阵、[tests/fallback_baseline.json](../../tests/fallback_baseline.json) 棘轮),并论证为什么"先收编、后撤边界"的顺序不可颠倒。
