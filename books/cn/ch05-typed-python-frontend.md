# 第 5 章 类型化 Python 前端

pcc 的 Python 路径从一个文本文件开始,到一棵每个表达式都带类型标注的 AST 结束——低层化(lowering)成 LLVM IR 是第 6 章的事。本章讲这条前端链路的四级:手写词法与递归下降解析([pcc/frontends/python/py_lex.py](../../pcc/frontends/python/py_lex.py)、`py_parse.py`)、向冻结 AST 的提升([pcc/frontends/python/py_lift.py](../../pcc/frontends/python/py_lift.py) → [pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py))、流水线装配与模式裁决([pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py))、注解驱动的类型推断([pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py))。但机制只是一半;另一半是三个必须先想清楚的设计裁决:为什么 pcc 做的是 typed-subset 编译器而不是全 Python JIT;为什么不支持的习语默认大声失败(fail loudly)而不是静默回退(fallback);以及 `--ir-scaffold` 这个三态旗标到底在裁决什么。这三个问题的答案互相锁定,共同决定了前端每一层的形态。

## 本章导读:受控的 Python 子集

这一章的关键不是 Python 语法有多全,而是前端怎样把一段 Python 程序变成编译器能信任的结构。请按四步读:词法/语法得到 AST,提升层改写成更适合编译的形状,类型推断标出对象和值,最后把不支持的动态行为明确挡在门外。

- 类型化 Python 前端服务自举,所以它宁可拒绝不清楚的代码,也不靠 CPython 偷跑。
- `ir-scaffold` 是严格路线的一部分:它让缺口暴露为编译期失败,而不是运行时 fallback。
- 看到"支持/不支持"时,要同时问它是 host pcc 支持,还是 pcc1/no-libpython 链条也支持。

## 5.1 问题与设计空间

### 5.1.1 为什么是 typed-subset 编译器,而不是全 Python JIT

"让 Python 变快"这个问题已经有成熟答案:PyPy 式的追踪 JIT,在运行时观察热路径、对观察到的类型做投机特化、失败时去优化回解释器。如果 pcc 的目标是加速任意 Python 程序,JIT 是显然更对的工程路线——它不要求用户写注解,不要求语义子集,对动态习语天然友好。

pcc 没有选这条路,因为 pcc 的论题不是加速,是**拥有执行**(见第 1 章):编译出可审计的原生工件、no-libpython 部署、以及一个能自己编译自己的编译器。这三个目标分别排除了 JIT:

第一,JIT 的产物是进程内的机器码缓存,不是可分发、可审计的二进制;pcc 要的是 `pcc hello.py -o hello` 之后那个 `hello` 文件本身。第二,JIT 是宿主运行时之上的加速层,解释器、对象模型、去优化机制都依赖一个完整的 Python 运行时;而 pcc 的 no-libpython 义务恰恰要求最终工件不依赖 CPython 运行时。第三,也是最有约束力的一条:pcc 的自举不动点(pcc1→pcc2→pcc3,见第 15 章)要求编译器能把**自己的源码**编译成原生二进制,再用那个二进制重复同样的事。这件事只有 AOT 编译器做得到,而 AOT 编译动态语言就必须回答"哪些语义子集可以静态低层化"——typed subset 不是偷懒,是这个问题的诚实答案。

子集路线有一个常被忽视的副产品:它给"子集够不够用"提供了可证伪的标尺。pcc 的前端、流水线、类型推断、codegen 自己的源码必须全部落在子集内,否则自举闸门(gate)变红。[README.md](../../README.md) 把这一点写得很直白:自托管路径比普通用户代码更严格,pcc 自己的源码仍然必须回避或隔离运行时 `getattr`/`setattr`、字符串键方法分发、运行时副作用装饰器等动态习语,并且明确说"这是自举轨道的真实当前限制,不只是文档缺口"。换句话说,子集的边界不是营销口径,是每天被一个真实编译器闭包测试的工程事实。

代价也要如实记账。typed subset 意味着 pcc 的 Python 前端今天不实现完整的 Python 数据模型;[docs/python-limitations.md](../../docs/python-limitations.md)(2026-04-20 快照)把 `eval`/`exec`、`__import__` 钩子、导入期元类列为"永不计划";README 的状态表把整个 Python 前端标注为 Experimental。本书按声明卫生(claim hygiene)的纪律转述这些标注,不替它们升级。

### 5.1.2 严格模式哲学:不支持的习语为何默认大声失败

一个子集编译器遇到子集之外的代码时有两种选择:静默桥接到 CPython(链接 libpython,把不认识的操作丢给 `PyObject_*`),或者带着诊断硬失败。pcc 的默认是后者:`pcc hello.py` 等价于 `--python-libpython=off --ir-scaffold=on`,任何需要 CPython 回退的程序直接编译失败。

这个默认乍看对用户不友好——`auto` 模式明明就能编过去。坚持它的理由是:**静默回退会毒化下游每一个声明。** 一个"编译成功"的二进制如果悄悄链接了 libpython,那么 no-libpython 部署声明是假的;一个基准测试如果热路径其实跑在 CPython 桥上,那么性能数字测的是桥而不是 pcc;一个自举阶段如果静默 import 了宿主模块,那么不动点证据是假的。pcc 的七义务第一条要求所有兼容性声明模式标注(libpython ≠ no-libpython),而模式标注只有在"回退是一个可计数的离散事件"时才可执行——这正是回退棘轮 [tests/fallback_baseline.json](../../tests/fallback_baseline.json)(见第 14 章)的前提:你只能对被显式记录的事件做棘轮,不能对弥散在代码里的默认行为做棘轮。

机制上,这个哲学落在 [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) 的 `_finalize_libpython_mode()`:当模式为 `off` 且检测到需要回退时,抛出 `PyPipelineError`,错误信息点名是哪个文件、列出原因清单,并明确告诉用户解锁方式是显式写 `--python-libpython=auto/on`。失败是大声的,出路也是显式的——回退从默认行为变成一次有据可查的用户决定。三个模式的完整语义见 5.4.3。

### 5.1.3 同一条流水线,两代解析器

前端目录里藏着一段谱系。[pcc/frontends/python/parser.py](../../pcc/frontends/python/parser.py) 是第一代解析器:用 CPython 标准库 `ast` 模块做骨干,把 `ast.AST` 节点提升成 pcc 的 AST。它实现快、覆盖全,但有一个致命属性:它本身依赖 libpython。当 pcc 开始编译自己的流水线时,这条 `import ast` 边把 libpython 拖回了 stage1 闭包。`pipeline.py` 中 `compile_python` 的注释记录了裁决:`pcc.frontends.python.py_parse` + `pcc.frontends.python.py_lift` 是自举安全(bootstrap-safe)的解析路径,"之前的 CPython-ast 逃生门在编译后的流水线里保留了一条 libpython import 边,所以自托管路径不再发射它"。`parser.py` 自己的注释则宣判了未来:一旦原生解析器成为硬默认,这个文件可以整体删除。今天它残存的价值是给若干源码形状分析测试当宿主侧工具。

这段谱系给本章定下基调:下面要讲的每一个文件——词法器、解析器、提升器——都既是 pcc 的前端,又是 pcc1 必须能编译、编译出来还必须能正确运行的**输入**。很多表观上过度防御的源码形态,都是这个双重身份留下的化石。

## 5.2 解析:py_lex 与 py_parse

整条前端链路如下,全部由 `pipeline.py` 的 `compile_python()` 编排:

```text
source.py
   │  pcc/frontends/python/py_lex.py     手写词法:INDENT/DEDENT、NAME、NUMBER、
   │                          STRING、OP、KEYWORD(最长优先匹配)
   ▼
token 流
   │  pcc/frontends/python/py_parse.py   手写递归下降:Parser._parse_stmt 关键字
   │                          分发 + 表达式优先级阶梯 → 窄 AST(_Module、
   │                          _FuncDef、_Call 等 _* dataclass)
   ▼
窄 AST
   │  pcc/frontends/python/py_lift.py    _Lifter:窄 AST → 冻结 py_ast,一切表达式
   │                          ty=DynType,哨兵编码(_yield/_list_comp/...)
   ▼
py_ast.Module
   │  pcc/frontends/python/type_infer.py   infer_module:构造新节点,填 ty
   ▼
带类型 Module  ──→  L1CodeGen.generate()(见第 6 章)──→ LLVM IR 文本
```

### 5.2.1 手写词法器

`py_lex.py` 的模块注释直接声明了设计立场:CPython 的 `Lib/tokenize.py` **不是**对齐目标,"我们只交付 pcc 需要的部分"。词法器是几百行的手写实现:缩进栈产生 `INDENT`/`DEDENT`,关键字表是一个元组常量,多字符算符按最长优先匹配。没有用解析器生成器,也没有复用 C 侧的 PLY 基础设施——因为这个文件自己要被 pcc 编译,依赖面越窄,自举闭包越小。

### 5.2.2 递归下降与窄 AST

`py_parse.py` 的 `Parser` 是教科书式的递归下降:`_parse_stmt()` 按关键字分发到 `_parse_funcdef`、`_parse_if`、`_parse_try` 等;表达式走优先级阶梯,从 `_parse_expr`(同时受理 `lambda`、`yield`、海象表达式与三目)逐级降到 `_parse_or`、`_parse_and`、比较、位运算、移位、一元、`_parse_power`,最后到 `_parse_atom_trailer`/`_parse_atom` 处理调用、属性、下标尾缀。CPython 自己的解析器(`Parser/Python.asdl` 与 `parser.c`)被注释列为参照,但实现是独立的。

解析器的输出不是 `py_ast`,而是一组只在本文件内有意义的窄 dataclass(`_Module`、`_FuncDef`、`_BinOp`……)。两层 AST 的分工是刻意的:窄 AST 允许解析器自由演化(加节点、改字段不动公共契约),冻结的 `py_ast` 才是前端各阶段之间的接口(5.3.1)。

两个设计点值得停下来看。

**软关键字 `match` 在解析器里就被脱糖。** `match` 在 Python 里仍是合法标识符,所以 `_parse_stmt` 先用 `_looks_like_match_stmt()` 做前瞻判别,确认是 `match subject:` 形态才进入 `_parse_match()`。而 `_parse_match` 不构造任何 match 专属节点——它生成临时名 `__pcc_match_N` 保存主语,把每个 `case` 的模式翻译成条件与绑定(`_match_pattern_condition_bindings`),倒序折叠成 if/elif 链返回。代价是模式匹配的语义上限就是这套条件翻译能表达的子集;收益是 `py_ast` 与其后所有阶段(推断、低层化、两个后端)完全不需要知道 `match` 存在。对一个以自举为先的编译器,这是正确的不对称:语法糖在最靠近语法的地方消化掉。

**可诊断性是为 pcc1 设计的,不是为宿主。** `parse_module()` 给每条顶层语句包了一层异常上下文(语句序号、起始 token 的 kind/text/line),`PCC_DEBUG_PY_PARSE=1` 时逐语句打印面包屑。在宿主 CPython 下这些是冗余——Python 的栈回溯本来就够;但当 pcc1(被编译出来的原生编译器)在 stage2 解析 pcc 自己的某个文件失败时,没有宿主栈回溯可看,这些手工上下文就是唯一的定位信息。同理,`_parse_float_literal()` 手工实现了浮点字面量求值(尾数/指数分离加 `_pow10f` 累乘),而不是调 `float(text)`——编译后的解析器不能假设宿主转换函数的存在形态。文件顶部把 `TK_NEWLINE` 等 token 种类字符串复制为本地常量,注释明说是为了避免"通过多文件 CPython 回退路径拉入兄弟模块常量导入"。这些都是 5.1.3 所说的化石:**解析器的源码形态由"它必须被自己编译"塑造。**

## 5.3 提升:py_lift 与冻结的 py_ast

### 5.3.1 冻结契约

[pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py) 是整个前端的枢纽,它的设计可以用三个词概括:冻结、带跨度、类型在节点上。

所有节点是 `frozen=True` 的 dataclass——构造后不可变,任何"修改"都必须用 `dataclasses.replace` 构造新节点。这条纪律的直接受益者是类型推断:`infer_module` 是一个纯函数式的 pass,输入一棵树、输出一棵新树,旧树永远有效。文件 docstring 指向权威契约 [docs/plans/python-frontend-interfaces.md](../../docs/plans/python-frontend-interfaces.md) 第 2 节,那份文档冻结于 v0.1,目的是让多个并行工作的 agent 不能单方面改接口。

每个 `Expr` 携带两个公共字段:`span: SourceSpan`(文件/行/列范围,供诊断)与 `ty: Type`。类型层级里有几处值得注意的设计:`IntType` 带 `width: int = 64` 字段,注释称之为 "tagged default"——这是 `int` 的值投影宽度,而 `int` 的语义类型仍是任意精度,溢出纪律属于值模型(见第 16 章);`DynType` 是"无法确定时的回退"类型,它不是错误,是一个会传染的标记(5.5.3);`ClassType` 把 `fields`、`bases`、`properties` 分成三个独立通道,其中 `properties` 字段的源码注释直接引用了一份调查文档——5.7.2 会讲这个字段是怎么来的;`ValueClassType` 是值类的可选标注,细节归第 16 章。

同样重要的是 `py_ast` **没有**什么:没有 set 字面量节点、没有推导式节点、没有 `yield`/`await` 节点、没有海象节点、没有 `match` 节点。这是冻结契约的另一面——节点集越小,推断与低层化要穷尽处理的形态越少,两个后端与自举链要共同支撑的表示越窄。被排除的语法去哪了?答案在提升器里。

### 5.3.2 哨兵编码

`py_lift.py` 的 `_Lifter` 把解析器的窄 AST 翻译成 `py_ast`,所有表达式的 `ty` 先填 `DynType`,等类型推断来覆盖。对于 `py_ast` 没有对应节点的语法,提升器统一采用**哨兵调用**编码——构造一个调用特殊名字的 `Call` 节点,由低层化阶段(第 6 章)识别并改写:

| 源语法 | 提升结果 |
|---|---|
| `{a, b}` | `set([a, b])` 调用 |
| 列表/字典/集合/生成器推导式 | `_list_comp` / `_dict_comp` / `_set_comp` / `_gen_comp` 调用,生成子句编码为 `_gen_clause(target, iter, (ifs,))` |
| `yield x` / `yield from x` | `_yield(x)` / `_yield_from(x)` 调用 |
| `await x` | `__await__(x)` 调用 |
| `*args` / `**kwargs` 实参 | 名为 `*` / `**` 的调用 |
| `name := expr` | `_walrus(target, expr)` 调用 |
| f-string | `format(x, spec)` 与字符串拼接的组合 |

这个编码的好处与 5.3.1 一脉相承:冻结契约不必为每个语法糖扩节点。坏处是一种特有的失败模式——**哨兵泄漏**:如果某个哨兵在低层化阶段没有被改写(因为构造它的形态不在改写器预期内),它会作为普通名字查找活到运行时,变成一个莫名其妙的 `NameError: name '_yield' is not defined`。错误从编译期搬到了运行期,且报错点离根因隔了两个阶段。5.7.1 的案例研究就是这个失败模式的现场,它给哨兵编码立下了不变式:哨兵只允许在低层化器保证改写的形态里被构造。

### 5.3.3 防御性提升:自举输入的化石层

`py_lift.py` 是全仓库防御密度最高的文件之一,而每一处防御都能指到一次真实事故:

- `lift_module()` 用显式循环逐条提升顶层语句而不是生成器表达式,捕获任何异常后把语句序号、节点类型、行号、文件名拼进 `LiftError` 再抛。源码注释指向 [docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md](../../docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md)(5.7.3)。
- `lift_stmt()` 用 `type(s) is pp._Pass` 式的具体类同一性显式分发,注释说明这优于 `getattr(self, f"_s_{...}")` 动态分发——因为 [scripts/audit_selfhost.py](../../scripts/audit_selfhost.py) 会把动态属性模式标记为自托管不合格。
- `_node_ident()`/`_node_attr_name()` 同时容忍 `ident`/`id`、`name`/`attr` 两套字段拼写,以兼容不同解析器快照。
- `_parse_float_literal_lift()` 带着一段大写 `WORKAROUND` 注释:不把参数别名进一个会被作用域退出 decref 的本地变量,"总是通过切片物化一个新的拥有字符串",指向 UAF 调查文档(5.7.3)。

把这层翻译成设计语言:**提升器是 pcc1 进程里最早执行的复杂代码**——stage2 编译的第一步就是 parse+lift pcc 自己的源码。pcc1 的 codegen 或运行时若有任何所有权、布局、分发缺陷,提升器往往是第一个受害者。于是它的源码逐渐演化成两种东西的叠加:一个 AST 翻译器,和一组被记录在案的崩溃现场加固。读这个文件时不要把防御样式当作风格选择删掉——几乎每一处都有对应的调查文档编号。

## 5.4 流水线:pipeline.py 的装配与模式裁决

### 5.4.1 单文件主线

`compile_python()` 是单文件入口,主线清晰:读源码 → `parse_and_lift()`(`py_lift.py` 的封装,把解析与提升异常统一包成 `LiftError`)→ `_module_needs_libpython()` 扫描 AST 判定回退需求 → `infer_module()` → `L1CodeGen.generate()` 产出 IR 文本(第 6 章)→ IR pass 管线 → `_finalize_libpython_mode()` 终裁 → 后端发射(第 12、13 章)。

值得注意的是回退判定做了**两次**,且第二次在 IR 层:AST 扫描只能看见 `import`,而 codegen 可能为 DynType 方法分发、`hasattr` 回退等在没有任何 `import` 的源码上发射 `py_cpy_*` 调用。所以流水线在 IR 文本上再做一次 `_ir_needs_libpython()` 扫描(匹配 `call` 指令而非裸文本,避免被无条件发射的 `declare` 桩误伤),两个来源的原因合并进 `_finalize_libpython_mode` 的原因清单:"imports still lower through CPython fallback"、"generated IR still calls py_cpy_* helpers"。大声失败时报的不是一句"需要回退",而是回退被引入的机制。

### 5.4.2 闭包收集与多文件

单文件入口并不意味着只编译一个文件。`compile_python` 先调 `_collect_relative_module_closure()` 收集相对导入闭包——`from . import sibling` 的兄弟模块会被并入同一次原生编译;当闭包大于一个文件或需要递归编译纯 Python stdlib 时,转入 `compile_python_multi()`。多文件路径在推断之前先跑一个导出预扫描(`build_closed_world_context()`):逐模块 parse+lift,提取每个模块的函数签名、类字段 schema、再导出边,装配成 `external_exports` 表传给每个模块的类型推断——这样 `from .sibling import fn` 在调用点能拿到 `FuncType` 而不是塌缩成 `DynType`。这是 5.5 的多文件输入。

导入分类是这一层的语义核心。`_classify_python_import()` 把每个导入裁决为五个**刻意稳定的字符串**之一,稳定到测试与自举诊断可以直接断言:

```text
compile_time_only        编译期擦除(typing 等)
native_user_module       同闭包原生编译的用户模块
builtin_native_dispatch  内建原生分发低层化
native_stdlib            解析到 pcc/stdlib 原生替身
cpython_fallback         无原生提供者;除非显式允许,否则触发硬失败
```

每次裁决经 `_record_import_classification()` 落到结构化日志:`PCC_LOG=import` 时发射 `pcc.import_log.v1` schema 的 JSON 行(模块、分类、是否原生、提供者、判定来源)。分类不是日志装饰——`cpython_fallback` 正是 5.1.2 那个"可计数的离散事件"。

### 5.4.3 两个旗标,六个状态

`--python-libpython` 由 `_resolve_libpython_mode()` 解析,空值默认 `off`:

- `off`(默认):任何回退需求 → `PyPipelineError` 硬失败。
- `auto`:仅当检测到回退需求时链接 libpython——兼容性实验模式,README 明确警告不得用于 no-libpython 声明。
- `on`:无条件允许并链接回退面。

`--ir-scaffold` 由 `_resolve_ir_scaffold_mode()` 解析,语义比名字深。它裁决的问题是:**当 pcc 编译的源码本身在构造 LLVM IR 时**——即 pcc 自己的 codegen 模块里的 `self.builder.call(...)`、`ir.IntType(64)` 这类调用点——这些调用如何低层化。这是自托管特有的问题:普通用户程序没有这种调用点,而 pcc1 要想脱离 libpython 运行,自己的 IR 构造层必须被封闭世界(closed-world)地编译。三态语义:

- `on`(默认,源码注释称 Path A):`IRBuilder` 与 `ir.*` 调用点由 `ir_scaffold_lowering.py` mixin 直接低层化为对外部 IR 构建符号的原生调用;`pcc.extern`、`pcc.unsafe`、`pcc.ir`、`pcc.ir.compat` 这组脚手架导入(`_SCAFFOLD_IMPORT_MODULES`)被视为编译期构造,不计入回退;`_filter_ir_scaffold_closure()` 同时改写链接闭包——剔除 `compat.py` 与 LLVM-C 绑定 `binding.py`(留着它们就会把 libpython 拖回 self 后端路径),换入真正的符号提供者 `pcc.ir.ir`。尚未迁移的 builder 方法抛 `ScaffoldUnsupportedError`,错误点名缺失的方法名。
- `off`:显式的兼容性逃生门,走旧低层化路径,builder 调用点照常动态分发(因此通常需要 libpython 允许);永不抛 `ScaffoldUnsupportedError`。`ScaffoldUnsupportedError` 的 docstring 把对比写得很直白:OFF 模式静默回退到 `py_cpy_*` 分发,错误面只存在于 ON 模式,**为的是逐文件迁移能精确看到还差哪些符号**。
- `auto`:历史遗留的混合模式。今天 `_resolve_ir_scaffold_mode` 把空值与 `auto` 都归一化为 `on`——封闭世界已经是默认现实,`auto` 只作为 CLI 兼容拼写存在。

注意 fail-loudly 哲学在这里的第二次出现:scaffold ON 的失败模式(点名方法的 `ScaffoldUnsupportedError`)与 libpython OFF 的失败模式(点名原因的 `PyPipelineError`)是同一个设计在两个层面的实例。还有一个细节暴露了它的自举本性:`_ir_scaffold_enabled()` 对 `runtime_abi`、`layer1`、`class_gen` 三个模块**无条件**启用 scaffold,旗标都拦不住——这几个模块没有封闭世界低层化就根本进不了 stage1 闭包。

## 5.5 类型推断:注解驱动,DynType 兜底

### 5.5.1 一个纯函数式的 pass

`type_infer.py` 的入口 `infer_module()` 接收 `py_ast.Module`,返回一棵全新的树:每个表达式的 `ty` 被填上"能确定的最好类型,确定不了就 `DynType`";解析期以表面 `Expr` 形式存在的注解被替换为一等 `Type` 实例。共享状态收在 `_InferCtx`(模块名、全局作用域、`func_types` 函数类型表、`class_types` 类类型表、多文件的 `external_exports` 与 `derived_class_map`);名字查找走 `_Scope` 链:局部 → 外层参数 → 模块全局 → 内建表。

推断是注解驱动的,这是与全程序类型推断(如 HM 系)的根本区别:带注解的参数取注解类型,没有注解就是 `DynType`;返回类型来自 `return_ty` 注解;局部赋值优先注解、否则取 RHS 推断结果。`_infer_funcdef()` 在走函数体**之前**就把完整 `FuncType` 注册进 `ctx.func_types`,所以递归调用能看到自己的签名;函数体走完后,若返回注解不是 `DynType`,`_check_returns()` 对每个 `return` 做相容性检查。

表达式规则集中在 `_infer_expr()` 与 `_binop_result()`。后者值得一读:`str + str` 保持 `str`,数值 `/` 恒为 `float`,位运算在 int 类操作数上保持 int,`int ** int` 为 int、沾 float 即 float;对明显的类型错误(`str + 数值`)直接经 `_raise_frontend_error()` 抛出带提示的 `PyFrontendError`;而所有规则都不命中时,返回 `TYPE_DYN`——不是错误,是降级。

### 5.5.2 有限的流敏感:isinstance 窄化

推断基本是流不敏感的,唯一的例外是 `isinstance` 窄化:`if isinstance(x, C):` 的 then 分支里,`_narrow_scope_for_isinstance()` 会压入一个把 `x` 绑定为 `C` 的子作用域(`and` 链经 `_narrow_scope_for_cond()` 递归窄化)。窄化条件很克制:当前类型必须是 `DynType`,或是候选类型的可赋值超类。更克制的是 `_type_from_isinstance_arg()` 对元组形式的处理,源码注释原文:"元组形式描述一个并集。前端还没有并集类型,所以我们刻意让它们保持未窄化,而不是去猜。"这句注释是这个文件的设计性格——**推断的诚实比推断的强大优先**:宁可让类型停在 `DynType`(从而走更慢但语义保真的路径),不引入一个可能猜错的精化。

### 5.5.3 类型如何决定低层化,DynType 如何传染

推断的输出直接决定第 6 章低层化的分层。接口契约 [docs/plans/python-frontend-interfaces.md](../../docs/plans/python-frontend-interfaces.md) 第 7 节定义了三个执行层级:L1(全原生操作数,直接 LLVM 操作)、L2(原生与 PyObject* 混合,边界编组)、L3(全动态,一切经运行时分发),并给 codegen 立下规则:"证明不了所有操作数是原生的,降到 L2;什么都证明不了,降到 L3。**决不猜测——发射运行时调用。**"需要诚实说明的是,今天的目录树与这份 v0.1 计划有漂移:不存在名为 `layer2.py`/`layer3.py` 的文件,类型化层级整体落在 `L1CodeGen` 里,而"动态层级"以 DynType 驱动的运行时分发与(在允许时的)`py_cpy_*` 路径存续。层级是语义事实,不再是文件边界。

这就解释了 `DynType` 的传染性为什么是前端最重要的性能与正确性变量:一个表达式一旦是 `DynType`,它的每个消费点都失去原生低层化资格,在 no-libpython 下还可能直接把编译推进 5.4.1 的硬失败。类型信息是一条供应链——5.7.2 的案例研究会展示链条断在哪一环时,运行时与 codegen 的既有支持如何整体作废。

类类型是这条供应链的大宗货物。`_prepopulate_module_scope()` 先把模块顶层的类与函数注册进全局作用域;类定义经 `_class_fields_from_def()`(字段 schema)、`_class_bases_from_def()`(基类)、`_class_properties_from_def()`(`@property` 声明,见 5.7.2)装配成 `ClassType`;属性访问 `obj.attr` 沿 `_class_mro_list()` 的 MRO 走 `_lookup_class_field()` → `_lookup_class_property()`。多文件场景下,`external_exports` 让跨模块的 `from .sibling import C` 在推断期拿到完整 schema;`derived_class_map` 处理 mixin 形态——当基类在闭包里有唯一派生类时,以派生类为 `self` 类型推断基类方法,这个机制的来历与代价由第 6 章的拆分史详述。`contextual_host_params` 与 `l1_codegen_host_type()` 是同一问题的另一面:为接收 `L1CodeGen` 宿主对象的辅助函数提供一个合成类型,让 `host.builder` 不至于立刻塌缩成 `DynType`——这是类型推断为"编译自己"做的定向加强。

## 5.6 错误分级:硬错误、回退路线与解释器

把前面散落的失败面收拢,前端的错误分级是一个四层结构,每层有自己的类型、阶段与受众:

**第一层:用户类型错误 → `PyFrontendError`。** 定义在 [pcc/frontends/python/types.py](../../pcc/frontends/python/types.py),dataclass 携带 `span`、`message`、可选 `hint`,`format()` 渲染 `file:line:col: error: ...` 加 hint 行。接口契约第 8 节把它定为强制约定:每个用户可见的编译失败必须是 `PyFrontendError`(或子类),不允许从用户输入冒出裸 `RuntimeError`。它表达的是"你的程序错了"。

**第二层:子集外但语义已知 → `DynType` 降级,而非错误。** 推断对不认识的形态不抛错,标 `DynType` 交给低层化;低层化对 `DynType` 发射运行时分发。这不是静默回退——是否允许由模式裁决:`--python-libpython=off` 下,若该降级最终需要 `py_cpy_*`,在 `_finalize_libpython_mode()` 处转化为**第三层:模式硬失败 → `PyPipelineError`**,带机制化的原因清单(5.4.1)。注意分层的妙处:`_binop_result` 返回 `TYPE_DYN` 时不知道也不需要知道最终模式;裁决推迟到拥有全部信息(生成的 IR、用户的模式选择)的位置。

**第三层的自托管变体:`ScaffoldUnsupportedError`。** scaffold ON 模式下未迁移的 IRBuilder 方法点名报错(5.4.3),受众不是普通用户而是做逐文件迁移的开发者。

**第四层:路线记录与解释。** [pcc/diagnostics/fallback_routes.py](../../pcc/diagnostics/fallback_routes.py) 把 5.4.2 的五个分类字符串转成用户可见事件:`FallbackRoute(module, classification, reason, native)`,`route_from_classification()` 给每个分类一句稳定的原因("no native provider found; libpython required unless disabled" 等),`explain_routes()` 渲染文本或 `pcc.diagnostics.fallback_routes.v1` schema 的 JSON。[pcc/diagnostics/fallback_explainer.py](../../pcc/diagnostics/fallback_explainer.py) 是更通用的收集器:`FallbackReason(feature, phase, reason, suggestion, source)`,`explain_import()` 对 `cpython_fallback` 生成带建议的解释("add pcc/stdlib port or enable --python-libpython=auto")。如实记录现状:这两个模块今天是带单元测试([tests/python/test_fallback_routes.py](../../tests/python/test_fallback_routes.py)、`test_fallback_explainer.py`)的稳定词汇表与渲染器,流水线的实时发射通道是 `_pcc_emit_import_log`(`PCC_LOG=import`)与 `--explain-fallback` 经 [pcc/diagnostics/compile_observability.py](../../pcc/diagnostics/compile_observability.py) 的 `ObservabilityOptions` 附到诊断注记;两侧共享同一套分类字符串,这套字符串才是真正的契约。

分级的总效果:**每个失败都落在知道"为什么失败"的那一层,且失败本身是结构化数据。** 第 14 章的回退棘轮、第 18 章的声明卫生表,都建立在这个性质上。

## 5.7 历史与教训

三个故事都取自 [docs/investigations/](../../docs/investigations) 的实时记录,按"症状 → 错误假设 → 证据链 → 根因 → 留下的不变式"复盘。

### 5.7.1 `yield a, b` 误析与哨兵泄漏(2026-05-27 修复)

**症状。** 含 `yield 1, 2` 的生成器编译零诊断,运行时崩溃:`NameError: name '_yield' is not defined`。触发源是真实包面——NumPy `numpy/distutils/misc_util.py` 的 `yield rpath, files`(调查文档:`python-yield-tuple-misparse-leaks-yield-sentinel.md`)。

**容易走错的方向。** "生成器没被识别"。证据否定:`_funcdef_has_yield_sentinel` 在嵌套位置照样找到了 `_yield` 哨兵,resume 函数正常发射——生成器机制全程在工作。

**证据链。** 双后端(self 与 llvm)同样失败、CPython 打印 `(1, 2)` → 后端无关,前端问题。读发射的 IR:resume 函数构造了一个二元组,元素 0 是对名字 `_yield` 的动态调用,元素 1 是字面量 `2`,旁边躺着 `"name '_yield' is not defined"` 字符串常量——泄漏实锤。

**根因。** CPython 里 `yield` 对 testlist 是贪婪的:`yield a, b` ≡ `yield (a, b)`。`py_parse.py` 的 `_parse_yield_expr` 只调一次 `_parse_expr()`,产出 `_Yield(value=a)`,把 `, b` 留给外层 testlist,得到 `(_yield(a), b)`。生成器低层化只改写**直接**以 `_yield(...)` 为表达式的 `ExprStmt`;被元组包裹的哨兵漏过改写,作为普通名字活到运行时。

**修复与不变式。** `_parse_yield_expr` 镜像 `_parse_return` 的隐式元组处理消费整个 testlist(表达式位置安全的终止符集合);`yield from` 按 PEP 380 刻意保持单表达式。修复注释把整个事故写进了源码。留下的不变式有两条:其一,哨兵编码把误析的代价从编译期 ParseError 推迟成运行期 NameError——所以任何产生哨兵的解析路径改动必须有端到端(编译并运行)回归,本例落在 `tests/python/test_python_generator_parity.py::test_generator_yields_implicit_tuple`;其二,`py_parse.py` 的改动必须跑完整三阶段自举闸门后才许声明修复——本次修复的调查记录里两者都有存档。

### 5.7.2 `@property` 返回类型不传播:类型供应链的断点

**症状。** 多文件封闭世界编译 [pcc/stdlib/pathlib.py](../../pcc/stdlib/pathlib.py) 触发 no-libpython 硬失败。最小形态(调查文档:`pcc-py-type-infer-property-return-type.md`):

```python
@property
def suffix(self) -> str:
    n = self.name       # n 被推断为 DynType,而非 str
    i = n.rfind(".")    # 经 py_cpy_getattr 动态分发 → 触发闸门
```

**反直觉之处。** 运行时早有 `py_str_rfind`,codegen 早有对应低层化,甚至单文件直读属性 `c.name` 的类型**就是** `str`(基线测试通过)。缺口只在一跳之外:属性结果存进局部变量再调方法时,类型已经丢了。供应链上每个下游环节都就绪,断的是上游一环——推断期 `ClassType` 没有任何 `@property` 视图,属性访问落进通用 `DynType` 路径。

**修复里的设计判断。** 调查文档的修复计划点出一个容易做错的细节:不能把 property getter 当普通方法注册——那会让 `c.prop()` 错误地通过类型检查。正确做法是给 `ClassType` 开一个独立的 `properties` 通道。今天的代码就是这个形状:`py_ast.py` 的 `ClassType.properties` 字段注释解释了为什么与 `fields`、方法表分离;`type_infer.py` 的 `_class_properties_from_def()` 收集 `@property` 声明(getter 的返回注解,缺注解回退 `DynType`),`_lookup_class_property()` 沿 MRO 查找。setter/descriptor 内省被显式划出范围。

**教训。** 类型信息是供应链:运行时函数、低层化规则、推断标注三者缺一,整条原生路径作废,且失败表现(libpython 闸门跳闸)与根因(推断少了一个查找通道)隔着两个子系统。锁定规格的回归测试写的是端到端断言——多文件 IR 里不得出现任何 `py_cpy_*` 调用——而不是推断的单元断言,因为供应链问题只有端到端才看得住。

### 5.7.3 提升器作为 pcc1 的第一个受害者:UAF 与原始值泄漏

这是两份首尾相接的调查(`pcc1-self-host-parse-float-literal-uaf.md` 与 `pcc1-stage2-lift-expr-raw-value-leak.md`),讲的是同一件事:当编译器编译自己,前端文件成为最灵敏的集成测试。

**第一幕:堆损坏。** pcc1 编译 [pcc/__main__.py](../../pcc/__main__.py) 时确定性崩溃,macOS nano 分配器报 heap corruption。探针在 `py_decref` 捕到第一次对悬垂指针的释放:`tag=2043`,不是任何合法 `PY_TYPE_*` 值——对象内存已被释放复用,转储显示原地址 48 字节后躺着一个新建的字符串对象。回溯定位到 `_parse_float_literal_lift` 的作用域退出清理;反汇编证实 `if e_idx >= 0` 的 else 路径把参数 `text` 别名存进 `mantissa` 槽位**没有 incref**,函数退出时 `pcc_gc_release(mantissa)` 多释放一次。最有方法论价值的是失败的那部分:完全相同的别名模式做成最小重现,10 万次迭代干净通过——bug 需要完整 pcc1 的堆分配序列才显形。调查如实写下"别名假设单独不充分",列出三个仍在桌上的假设,而不是宣布破案。前端源码里的对策今天还在:`_parse_float_literal_lift` 的 `WORKAROUND` 注释,所有路径一律用切片物化新的拥有字符串,绕开 codegen 的所有权盲区(所有权契约本身见第 9 章)。

**第二幕:崩溃变成谎言更小的失败。** UAF 修复(commit `18f60d6a`)之后,stage2 的下一道墙是干净的回溯:`lift_expr` 的兜底 `raise LiftError(f"no expr lifter for {t.__name__}")` 自己抛了 `AttributeError`——因为 `t` 根本没有 `__name__`。诊断插桩把兜底展开成对全部 25 个表达式类的 `isinstance` 测试加原始值探测,结论无歧义:落到 `e` 上的是原始 Python 值——一个单引号字符串、一个左花括号字符串、一个裸元组——**不是任何解析节点**。`isinstance` 全部为假,排除了"漏分发"假设;若是双重导入的类同一性问题,`isinstance` 应当仍然命中,也排除。宿主 CPython 跑同一提升器从不触发——bug 在 pcc1 编译出的解析器/运行时里,把原始值漏进了本应持有节点的子槽,泄漏点随堆布局漂移。

**留下的不变式。** `lift_module` 与 `_lift_stmt_list` 的显式循环加上下文重抛(语句序号、节点类型名、行号、文件名)就是这两幕的直接遗产:当下一个 pcc1 缺陷再把提升器选为第一受害者时,失败要自带定位坐标。更普遍的教训写进了自举回归纪律([AGENTS.md](../../AGENTS.md)):分清堆叠的失败——UAF 与原始值泄漏是两个 bug、两条证据链,合并成一个"自举坏了"的故事只会两个都修不掉;以及,最小重现不触发不等于假设错误,堆布局依赖的 bug 本来就以此为常态(调试手册 §1、§5,见第 18 章)。

## 5.8 小结

类型化 Python 前端是四个文件链成的一条供应线:手写词法/递归下降解析产出窄 AST,提升器把它翻译成冻结的 `py_ast`(语法糖统一哨兵编码,表达式一律 `DynType` 起步),流水线收集闭包、裁决导入分类与两大模式旗标,类型推断以注解为源、以 `DynType` 为诚实兜底,把"能证明的部分"标注出来供第 6 章原生低层化。三个设计裁决贯穿始终:typed subset 而非 JIT,因为论题是可拥有的执行与自举不动点,而子集的充分性每天被"编译器必须编译自己"证伪检验;不支持的习语默认大声失败,因为回退只有作为可计数的显式事件才支撑得起棘轮与模式标注的声明;`--ir-scaffold` 三态(on=封闭世界并对缺口点名报错、off=显式逃生门、auto=归一化为 on 的历史拼写)把"编译器自己的 IR 构造层如何被编译"从隐式行为提升为受控契约。而 5.7 的三个故事共同指向本章最深的一条结构性事实:这些前端文件同时是编译器的实现与编译器的输入,它们的防御形态、它们的错误分级、乃至它们的字面量求值方式,都是自托管约束在源码里的投影。

## 练习

1. **读源码验证。** `pcc hello.py` 不带任何旗标时,两个关键模式的实际取值是什么?从 [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) 的 `_resolve_libpython_mode()` 与 `_resolve_ir_scaffold_mode()` 出发,解释空值分别归一化为 `off` 与 `on` 的代码路径,并对照 [README.md](../../README.md) 状态表确认文档与代码一致。
2. **读源码验证。** 列出 [pcc/frontends/python/py_lift.py](../../pcc/frontends/python/py_lift.py) 的 `_Lifter` 可能构造的全部哨兵名字(从 `_e_Comp`、`_e_Yield`、`_e_Await`、`_e_Starred`、`_e_Assign`、`_e_Set` 入手)。任选其一,在 [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) 下找到识别并改写它的低层化代码,写出防止 5.3.2 所述哨兵泄漏的不变式在该例中的具体形式。
3. **分层论证。** `_binop_result()` 对不认识的操作数组合返回 `TYPE_DYN` 而不抛错,这与"默认大声失败"是否矛盾?描述一个 `TYPE_DYN` 最终升级为 `PyPipelineError` 硬失败的完整路径(提示:5.4.1 的两次回退检测),并论证为什么裁决放在 `_finalize_libpython_mode` 而不是 `_binop_result` 是正确的层。
4. **设计权衡。** `_type_from_isinstance_arg()` 刻意不窄化 `isinstance(x, (A, B))` 的元组形式,因为前端没有并集类型。为 `py_ast` 设计一个最小的并集类型扩展:冻结契约要加什么节点?`_narrow_scope_for_isinstance`、`_is_assignable` 与第 6 章的低层化各要承担什么?最后论证:pcc 自己的自举闭包是否真的需要它——用 `rg` 在 [pcc/](../../pcc) 下统计元组形式 `isinstance` 的实际出现密度来支撑你的结论。
5. **预测并验证。** 不看代码,先预测 `import typing`、`from . import sibling`、`import pcc.unsafe`、`import numpy` 四个导入在 5.4.2 五分类下的归属;再读 `_classify_python_import()`、`_SCAFFOLD_IMPORT_MODULES` 与 `_COMPILE_TIME_ONLY_IMPORT_MODULES` 验证,并用 `PCC_LOG=import` 实际编译一个小文件核对 JSON 日志(`pcc.import_log.v1`)与你的预测。
