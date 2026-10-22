# 第 3 章 C 前端:解析、伪 libc 与求值器

pcc 的 C 前端是仓库中最成熟的子系统:它编译并运行过 Lua、SQLite、PostgreSQL `libpq`、zlib、lz4、zstd、PCRE、OpenSSL 这一级别的真实项目。本章讲它如何把一份(或一个目录的)C 源码变成可解析的翻译单元(translation unit,TU),再送进求值器流水线。具体来说:解析器从 PLY 到原生 LR 驱动的双轨结构、两条预处理路径、[utils/fake_libc_include/](../../utils/fake_libc_include) 这套"只有声明没有实现"的伪 libc、[pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) 的 preprocess→parse→IR→optimize→execute 流水线,以及 [pcc/driver/project.py](../../pcc/driver/project.py) 的源收集与 `--sources-from-make`。表达式如何低层化(lowering)为 LLVM IR、符号性如何跟踪,是第 4 章的内容;本章止步于"AST 进了代码生成器"这条线。

## 本章导读:C 前端的四个阶段

本章不用从 C 语法细节读起。先抓住四道关:预处理把宿主头文件问题收窄,fake libc 提供可控声明,解析器把 token 变成 AST,求值器把 AST 接到 LLVM 与真实项目测试上。

- 如果一个 C 项目编译失败,先分清它卡在源文件收集、头文件/声明、解析,还是语义低层化。
- fake libc 不是 libc 实现,它是给编译器看的声明边界。
- 真实项目测试的价值在组合语义:很多 bug 只有在预处理、解析、低层化和链接连起来时才出现。

## 3.1 问题与设计空间

C 前端要回答的问题不是"如何解析 C 语言"——这在教科书里早有答案——而是"如何解析**真实世界交付的** C 代码"。两者的差距构成了本章全部的设计空间:

1. **真实 C 文件以预处理后的形态才是 C。** `#include <stdio.h>` 展开后,宿主系统头会带来数万行充满编译器扩展的声明:`__attribute__`、嵌套 `__builtin_*`、平台条件编译。一个不打算复刻 GCC 的前端必须决定:这些东西在哪一层被挡住。
2. **声明与实现可以分离。** 解析和类型检查只需要 `printf` 的原型,不需要它的实现——实现在链接期由真实的 libc 提供。这个观察是伪 libc 设计的根。
3. **真实项目没有"源文件列表"这个输入。** 它们有 Makefile、configure 脚本、amalgamation、条件编译进出的 TU。前端的入口不是 `parse(file)`,而是"从一个目录和一个构建系统里恢复出参与编译的 `.c` 集合与预处理旗标"。

在解析器本体上,设计空间有三个候选:自写完整 C 前端、绑 clang 的 AST、复用 pycparser。pcc 选择了第三条:[pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py) 的文件头仍保留着 pycparser 的版权声明(Eli Bendersky, BSD),文法实现的注释明说是 K&R2 附录 A.13 的 BNF。理由是务实的:pycparser 的文法和 AST 经过十几年真实代码的打磨,而绑 clang 会把"前端"变成对外部 C++ 巨型依赖的封装——这与第 1 章的自托管目标直接冲突。但复用不是终点:pycparser 依赖 PLY,而 PLY 在运行期动态构造解析表、依赖 Python 反射机制,这对"pcc 编译 pcc 自身"的自举(bootstrap)路线是负担。于是 C 解析器走出了一条双轨演化路径——PLY 版保留为参照,默认路径换成无 PLY 的原生 LR 驱动(见 3.2)。

预处理的设计同样是两轨:一个纯 Python 的内置预处理器([pcc/frontends/c/preprocessor.py](../../pcc/frontends/c/preprocessor.py))处理无系统编译器的环境,一条"借系统 `cc -E` 之力、用伪 libc 头替换真实系统头"的主路径处理真实项目。两轨的共同立场是:**文本级整形在预处理边界是合法的,在 IR 层不是。** 仓库的 IR Fix Policy 规定 IR 文本重写只剩 `va_arg` 一个豁免(见第 12 章);相对地,本章会出现大量正则与字符扫描——因为预处理层的职责就是把宿主世界整形成解析器能接受的 C 子集,这是边界层的本职,不是 hack 的遮羞布。

## 3.2 解析器:从 PLY 到原生 LR 驱动

### 3.2.1 pycparser 遗产与 lexer hack

C 不是上下文无关语言。`A * b;` 是声明还是乘法,取决于 `A` 是否被 typedef 过——这就是经典的 lexer hack:词法器必须查询解析器的符号表才能决定返回 `TYPEID` 还是 `ID`。`CParser` 用一个作用域栈实现它:`_scope_stack` 是字典的栈,`_add_typedef_name()` 在当前作用域把名字标记为类型,`_add_identifier()` 标记为对象,`_is_type_in_scope()` 从内向外查找;词法器拿到回调 `_lex_type_lookup_func()`,并在 `{`/`}` 处通过 `_lex_on_lbrace_func`/`_lex_on_rbrace_func` 推入弹出作用域。

这个机制里藏着两个值得读的时序细节,都是注释里写明的真实陷阱:

- **声明规则被拆成 `decl_body SEMI` 两段。** 若 `declaration` 是单条规则,LALR 解析器在归约前会向前看 `SEMI` 之后的下一个 token——那可能正是下一行里使用刚 typedef 的类型名,而此刻符号表还没更新。拆分后,`decl_body` 在 `SEMI` 处先归约、先写符号表,词法器在读下一行之前就能看到新类型。
- **函数定义的参数必须在看到 `{` 的瞬间入作用域。** `p_direct_declarator_6` 里的注释给了例子:`typedef char TT; void foo(int TT) { TT = 10; }`——函数体内 `TT` 是参数而非类型。等 yacc 的规则触发已经太晚(向前看 token 已被错误词法化),所以解析器用 `_get_yacc_lookahead_token()` 直接探查词法器的 `last_token`,在它是 `LBRACE` 时立即注册参数名。

在 pycparser 的基线之上,pcc 的文法做了真实项目逼出来的扩展(均可在 `c_parser.py` 的产生式中找到):`INT128` 与 `_FLOAT16` 类型说明符、计算 goto(`goto *expr;` → `c_ast.ComputedGoto`)、GNU 语句表达式(`({ ... })` → `c_ast.StmtExpr`)、`BUILTIN_VA_ARG` 的两种调用形态、`_Static_assert`、`_Generic`、`OFFSETOF`、`nullptr`、`_Alignas`/`_Alignof`、`_Thread_local`,以及 GNU 范围指示符 `[0 ... 5] =`(→ `c_ast.RangeDesignator`)。

### 3.2.2 PLY 表缓存与版本号纪律

PLY 在首次运行时构造 LALR 表,代价不小,所以 `CParser.__init__` 把表写盘复用。缓存模块名是带版本号的常量:

```python
_DEFAULT_PLY_LEXTAB = "pcc_frontends_c_lextab_v14"
_DEFAULT_PLY_YACCTAB = "pcc_yacctab_v19"
```

缓存目录默认在 `tempfile.gettempdir()` 下的 `pcc-ply-cache`,可用 `PCC_PLY_CACHE_DIR` 覆盖;目录被插进 `sys.path` 以便 PLY 以 import 方式加载表。并发构建由 `_ply_table_build_lock` 守护——一个用 `fcntl.flock` 排它锁实现的上下文管理器(刻意写成显式类而非 `@contextmanager`,注释说明是为了不让自举审计误报 `yield`)。

版本号是手工纪律:**改了文法或词法就必须 bump**,否则 PLY 看到旧表文件存在就直接复用,新规则静默失效。这是 [AGENTS.md](../../AGENTS.md) Common Pitfalls 里 "stale parser caches" 条目的来源——症状是"我明明改了文法,行为却没变",根因是磁盘上躺着上一个版本的 `pcc_yacctab_v19.py`。把版本号编进模块名,等于把"缓存失效"从运行时判断变成了命名约定,简单,但依赖人记得改;这正是 3.2.3 中原生路径用内容哈希取代它的动机之一。

### 3.2.3 原生 LR 驱动:把 PLY 移出闭包

[pcc/frontends/c/parse/__init__.py](../../pcc/frontends/c/parse/__init__.py) 的 `make_c_parser()` 工厂是现在唯一正确的解析器入口:

```python
def make_c_parser():
    if os.environ.get("PCC_USE_PLY_C_PARSER") == "1":
        from pcc.frontends.c.parse.c_parser import CParser
        return CParser()
    from pcc.frontends.c.parse.c_parse_driver import CParseDriver
    return CParseDriver()
```

默认路径是原生驱动,PLY 版退为环境变量门控的参照实现。原生路径由三件套组成:

```text
源文本 ──► c_lex.CLexer(原生词法)──► CParseDriver ──► c_ast.FileAST
                                          │
                                          ├── ACTION/GOTO 表(c_parsetab,冻结数据)
                                          └── 文法动作(c_parser_actions)
```

- [pcc/frontends/c/parse/c_parsetab.py](../../pcc/frontends/c/parse/c_parsetab.py) 是**冻结的** LR 表:由 [scripts/freeze_c_parser_tables.py](../../scripts/freeze_c_parser_tables.py) 从 PLY 文法离线生成的纯数据 Python 字面量,加载时不 import PLY。文件头带 `GRAMMAR_SHA256`——对 `c_parser.py` 全部 `p_*` 方法源码拼接后的 SHA-256,CI 用它对照活文法,检测"改了文法忘了重新冻结"。这就是对 3.2.2 手工版本号纪律的机器化替代:从"人记得 bump"变成"哈希不匹配就报警"。
- [pcc/frontends/c/parse/c_parse_driver.py](../../pcc/frontends/c/parse/c_parse_driver.py) 是约 250 行的标准移进/归约状态机,通过 `_PSlot` 类向动作函数复刻 PLY 的最小接口(`p[i]`、`p.lineno(i)`、`p.slice`),使两套驱动共享同一份文法动作语义。
- [pcc/frontends/c/parse/c_lex.py](../../pcc/frontends/c/parse/c_lex.py) 是手写的逐字符扫描器,热路径不用正则(正则只留给整数后缀、浮点指数这类天然多字符模式),与 `pcc.frontends.c.lex.c_lexer.CLexer` 构造器签名、token 名完全兼容。

两条路径的行为等价由 [tests/c/test_c_parse_driver_parity.py](../../tests/c/test_c_parse_driver_parity.py) 闸门(gate)保证。诚实声明一条边界:`c_parse_driver.py` 的文档自己写明,驱动层源码级无 PLY,但整个 pcc 包仍经由 [pcc/__init__.py](../../pcc/__init__.py) 传递性加载 PLY——这是尚未完成的表面清理,不是已达成的"零 PLY"。

为什么费这个劲?因为解析器在自举闭包里。pcc1(由 pcc 编译出的第一个原生编译器,见第 15 章)必须能解析 C 运行时与它自己;一个在运行期反射构表的解析器框架,远比"冻结表 + 纯函数动作 + 手写扫描器"难以被 typed-Python 前端原生编译。原生化不是性能洁癖,是自举不动点(fixed point)的前置工程。

## 3.3 预处理:两条路径,一个边界

### 3.3.1 内置预处理器

[pcc/frontends/c/preprocessor.py](../../pcc/frontends/c/preprocessor.py) 的 `Preprocessor` 是纯 Python 实现,模块 docstring 列出支持面:`#include "..."`(读入内联)、`#include <...>`(**静默忽略**)、对象宏/函数宏/标志宏、`#undef`、完整的 `#ifdef`/`#ifndef`/`#if`/`#elif`/`#else`/`#endif` 与 `defined()` 求值、`##` 粘接、`__VA_ARGS__`。系统头被忽略后,常用类型由 `TYPE_PREAMBLE`(`size_t`、`va_list`、`FILE` 等 typedef 文本)注入,常用宏由 `BUILTIN_DEFINES`(`NULL`、`INT_MAX`、`__STDC_VERSION__` 等)预载。

最值得一读的是 `#if` 表达式求值器。直觉写法是把展开后的表达式丢给 Python 的 `eval()`——但 `eval` 在自举审计([scripts/audit_selfhost.py](../../scripts/audit_selfhost.py))的禁用内建列表上,源码注释明说了这一点。于是 `_eval_cpp_expr()` 配了一个完整的递归下降解析器 `_CppExprParser`,产出带标签元组树,由 `_eval_tree()` 按 **C 语义**求值:`&&`/`||` 与 `?:` 未取分支短路(死分支里的 `1/0` 不会炸,与 C 一致)、整数除法向零截断(`int(l / r) if (l < 0) ^ (r < 0) else l // r`)、`!0 == 1`。求值失败抛 `_CppExprError`,上层 `_eval_condition()` 发 warning 并按假处理。这是一个缩影:**自举约束会一路渗透到看似无关的工具代码里**。

宏展开走逐行定点迭代:`_expand_line()` 反复调用 `_expand_once()` 直到稳定,上限 30 轮防自引用宏死循环;`_expand_once` 先用 `IDENTIFIER_RE` 提取行内标识符集与宏表求交,按名字长度降序替换,对象宏用预编译的 `\b...\b` 模式配回调替换(回调是为了防止宏体里的 `\xNN`、`\n` 被正则引擎当转义解释)。函数宏的实参收集(`_find_macro_args`)做了括号配平和字符串/字符字面量跳过。这不是逐 token 的标准预处理器,`##` 也只是简单删除空白拼接;它的定位是受控场景与无 `cc` 环境的兜底,不是一致性实现——这一点必须如实写下。

### 3.3.2 系统 cpp 路径:借力但不失控

主路径在 `CEvaluator._system_cpp()`([pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py)):有系统编译器时(`_has_system_cpp()` 探测 `cc`/`gcc`),预处理交给真家伙,但用三个手段保证输出仍落在 pcc 可消化的子集内:

```text
cc -E -P -nostdinc -isystem utils/fake_libc_include  -I <用户目录>...  <大量 -D>  file.c
```

1. **`-nostdinc` + `-isystem fake_libc`**:掐断宿主系统头,换上伪 libc(3.4 节)。
2. **`compat_defs` 大表**:伪头刻意不提供的宏在命令行补齐——`limits.h`/`stdint.h` 的全套极值(按 LP64 模型,`INT64_MAX=...L`)、`inttypes.h` 的全套 `PRI*` 格式宏、`offsetof` 的指针算术定义,以及一组"把扩展定义成无害形态"的宏:`__attribute__(x)=`、`asm(...)=`、`__extension__=`、`__builtin_memcpy=memcpy`、`_Static_assert(x,...)=`(宿主头的静态断言里可能嵌着 `__builtin_types_compatible_p` 这类解析不了的内建,直接消去;用户代码的 `_Static_assert` 仍由内置预处理路径支持——文法里有 `p_static_assert`)。
3. **平台定义**:Darwin 上 `stdin=__stdinp` 等 stdio 全局重映射、`-U__ARM_NEON` 关掉向量内建路径。

这条命令失败时有第二段回退:改用**宿主真头**重跑,叠加 `system_header_compat_defs`——其中最关键的是把宿主形态的 `__builtin_va_arg(ap, type)` 重写成 pcc 已支持的 `(*((t*)__builtin_va_arg(&(ap),sizeof(t))))` 形态,以及 OpenSSL 的原子操作回退宏。对 zstd、OpenSSL、PostgreSQL 的 include 目录则反转顺序、直接首选宿主头(`prefer_system_headers`),并对 PostgreSQL 的输出做 `__int128` → `long long` 收窄(注释写明:libpq 前端源码不依赖 128 位语义,这是定界过的妥协,不是普适规则)。

预处理输出还要过一道归一化才进解析器(`_preprocess_translation_unit_source` → `_normalize_preprocessed_source`):`_VA_TYPEDEF_NORMALIZE` 把 `typedef __builtin_va_list X;` 链规整为 `typedef char * X;`,`_SELF_TYPEDEF` 删除自指 typedef,`_strip_gnu_asm_statements` 用字符扫描(非正则——需要括号配平和字符串跳过)把语句位置的 `asm(...)` 整体替换为 `;`,`_CPP11_ATTRIBUTE` 删 `[[...]]`,`_expand_simple_gnu_range_designators` 把 `{ [0 ... N] = v }` 展成不超过 4096 个元素的显式列表,`_inject_system_cpp_keyword_compat` 在 `bool`/`wchar_t`/`true`/`false` 被使用却无定义时注入兜底 typedef/enum。每一条都对应某个真实项目曾经的解析失败;它们留在预处理边界层,正是为了让解析器文法和 IR 层保持干净。

## 3.4 伪 libc:声明即接口

[utils/fake_libc_include/](../../utils/fake_libc_include) 沿用 pycparser 的 fake-libc 思路并大幅扩展,现有 83 个条目(含 `sys/`、`arpa/`、`netinet/`、`linux/`、`openssl/`、`numpy/` 子目录)。其设计原则可以概括为:**编译期只需要声明和布局,实现属于链接期。**

结构上一切收敛到两个根文件。多数头只有两行:

```c
#include "_fake_defines.h"
#include "_fake_typedefs.h"
```

`_fake_defines.h` 提供宏常量(`NULL`、`EOF`、`SEEK_*`、各类极值)和 va_arg 宏族——后者把 `va_start`/`va_arg`/`va_end`/`va_copy` 统一改写为取址形态:

```c
#define va_arg(_ap, _type) (*((_type *)__builtin_va_arg(&(_ap), sizeof(_type))))
```

这是前端与代码生成器之间的一个**契约**:文法里 `BUILTIN_VA_ARG` 的产生式恰好识别这两种形态,IR 层那唯一的文本级低层化豁免(见第 12 章)也建立在这个形状上。伪头在这里不只是声明的替身,而是把"可变参数"这种 ABI 深水区规整成编译器约定形态的转换器。

`_fake_typedefs.h` 则是一份**对宿主 ABI 的显式断言清单**:`size_t` = `unsigned long`、`va_list` = `char *`、`int64_t` = `long`(LP64)、`time_t`/`clock_t` 按 `__LP64__` 条件分支、`FILE` = `char`(只需可指向)、全部 `pthread_*` 类型 = `int`(只过类型检查,真实布局由链接进来的实现持有,前提是这些类型只以指针或不透明值方式穿过 pcc 编译的代码)。[AGENTS.md](../../AGENTS.md) 仓库地图对这个目录的注释是"host ABI / decl mismatches surface here"——当 pcc 编译的代码与真实 libc 在结构体布局或整型宽度上失配,第一个该怀疑的就是这里的某行 typedef。一个已经存在的失配例子:`mode_t` 在 `_fake_typedefs.h` 里是 `unsigned short`,而内置预处理器的 `TYPE_PREAMBLE` 里是 `unsigned int`——两条预处理路径对同一类型的断言并不一致,只是尚未有真实项目踩中。

少数头带真实内容。`stdio.h` 在 `__APPLE__` 下声明 `__stdinp`/`__stdoutp`/`__stderrp` 并宏映射 `stdin` 等(与 3.3.2 的 `-Dstdin=__stdinp` 互为补充),再补 `fileno`、`popen` 这类 POSIX 函数;`stdlib.h` 补 `getenv`/`mkdtemp`/`system`。这些都是真实项目逐个逼出来的增量——zlib 的 `gz*.c` 曾要求补 `read`/`write`/`open` 与 `O_CREAT` 族旗标([docs/investigations/zlib-integration-static-local-arrays-and-layout.md](../../docs/investigations/zlib-integration-static-local-arrays-and-layout.md))。最大的一个伪头是 1256 行的 `Python.h`:pcc 给 C 扩展编译用的伪 CPython 头,定义 `struct PyObject` 与 pcc 16 字节对象头的对应、伪造 `PY_VERSION_HEX` 让 numpy 的兼容垫片跳过反向填充。它属于第 17 章的故事,这里只指出:伪 libc 的思想被复用成了 C-API shim 的载体。

## 3.5 求值器流水线

[pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) 的 `CEvaluator` 是 C 路径的发动机舱。核心流水线在模块级函数 `_compile_translation_unit_artifact_job` 与 `_compile_preprocessed_translation_unit_artifact` 里,形状是:

```text
TranslationUnit(name, path, source)
  → _preprocess_translation_unit_source        # 3.3 的两条路径之一 + 归一化
  → make_c_parser().parse(codestr)             # 3.2 的双轨解析器
  → PassPipeline.run_high_tier(ast, ctx)       # AST 分析 pass,填 PassContext
  → CCodeGenerator(...).generate_code(ast)  # 语义低层化(第 4 章)
  → postprocess_ir_text(str(module))           # IR 文本后处理(豁免仅 va_arg,第 12 章)
  → PassPipeline.run_low_tier(ir_text, ctx)    # IR 级 pass
  → artifact 字典                               # ir_text / return_type / external_defs /
                                               # func_return_types / pass 统计与报告
```

产物刻意做成可 JSON 序列化的字典而非对象:返回类型经 `_serialize_ir_type()` 变成 `("int", 32)` 这类元组。这服务于两件事:进程池并行(`_compile_translation_units` 在多 TU 时用 `ProcessPoolExecutor` 分发,字典天然可跨进程)和磁盘缓存。

**缓存是三层的,各管一段冷启动成本。** 最外层是编译产物 JSON 缓存(默认 `~/.cache/pcc/compile-cache`,尊重 `XDG_CACHE_HOME`,`PCC_COMPILE_CACHE_DIR` 覆盖,`PCC_DISABLE_COMPILE_CACHE` 关闭):键由 `_compile_cache_key()` 计算,混入 `_COMPILE_CACHE_VERSION`(现为 `"v4"`)、**编译器自身指纹**、pass 选择签名、后端签名、目标三元组与预处理后的源文本。指纹 `_compiler_cache_fingerprint()` 对 `c_codegen.py`、`c_parser.py`、`c_lexer.py`、`preprocessor.py` 及 `passes`/`ssa`/`llvm_capi`/`backend` 四个包的全部 `.py` 文件哈希 mtime/size,再混入 Python 版本与平台——**改编译器源码自动废缓存**,把 3.2.2 那类"陈旧缓存"问题在这一层用机制而非纪律解决。中层是原生 `.so` 缓存:`_build_native_cache()` 把优化后的 IR 经 `emit_object` 加 `cc -dynamiclib`/`-shared` 编成共享库落盘,下次 `_load_native_cache()` 直接 `ctypes.CDLL` 取函数指针,跳过预处理、解析、代码生成和 LLVM 全部环节。最内层是进程内 `_jit_cache`,键为(源文本 SHA-256、入口名、优化签名、pass 签名、后端签名),值里持有执行引擎本体——注释明说:留着引擎是为了函数指针不失效。

**执行有四种出口。** (1) `evaluate()` 的进程内 MCJIT:`llvm.parse_assembly` → 优化 → `create_mcjit_compiler` → `get_function_address` → `CFUNCTYPE` 调用,带 `prog_args` 时构造 argc/argv。(2) 多 TU 链接执行:`_prepare_linked_llvm_module()` 逐模块 `link_in` 合并,执行前 `_raise_if_duplicate_external_definitions()` 用各 TU 上报的 `external_defs` 拒绝跨 TU 重复定义。(3) 系统链接:`run_compiled_translation_units_with_system_cc()` 对每个 TU 直接 `target_machine.emit_object()` 发原生对象,交系统 `cc` 链接成二进制后子进程运行——注意这条路**不把 IR 文本递给系统编译器**,这是 IR Fix Policy 的执行面;Linux 上补 `-no-pie`(pcc 发非 PIC 对象码,`_platform_link_flags` 的注释解释了 `__stack_chk_guard` 的绝对重定位问题)。(4) self 后端:`_run_compiled_translation_units_self_backend()` 经 `emit_self_asm` 出汇编、`cc` 汇编链接运行(第 13 章)。

执行面还有一个 Darwin 特有的生命周期防御。某些大型多 TU 程序在 MCJIT 上**正确跑完之后**,llvmlite/LLVM 的析构会让宿主 Python 进程崩溃。求值器的对策有二:`_detach_execution_engine()` 把引擎/模块/目标机的包装对象 detach 后存进进程级列表 `_DETACHED_MCJIT_WRAPPERS`,让它们只在解释器关闭时被丢弃;更重的场景用 `_run_linked_mcjit_worker()` 把整个执行隔离进子进程,结果写 JSON 文件,然后 `os._exit()` 直接退出、根本不走析构。这两手都来自 SQLite 集成的真实崩溃(见 3.7)。

调试钩子值得记住一个:IR 解析失败时,设 `PCC_DUMP_BAD_IR=<dir>` 会把坏 IR 按内容哈希落盘,便于定位是哪个 TU、哪个函数的产物。

## 3.6 项目收集与 --sources-from-make

[pcc/driver/project.py](../../pcc/driver/project.py) 负责把"一个路径"变成"一组 `TranslationUnit`"。第 2 章已概述四种编译模式,这里讲机制与限制。

**目录默认合并(merged)。** `_collect_directory()` 非递归收集 `*.c`(`os.listdir` + 排序),把含 `main()` 的文件放到最后,用 `// --- 文件名 ---` 注释行拼接成单一大 TU。`main` 的判定 `_has_main()` 是两阶段的:先用正则 `\b(?:int|void)\s+main\s*\([^;{}]*\)\s*\{` 粗筛,命中后再做**真实预处理**(`CEvaluator._system_cpp`)并对预处理结果重新匹配——这样被 `#if` 条件编译排除掉的 `main` 不会造成误判;预处理失败则回退正则并发 warning。`--separate-tus` 模式(`_collect_directory_units`)收集同一组文件但各自成 TU,并强制恰好一个 `main`;`--depends-on` 的依赖输入则一个 `main` 都不许有。

这个"目录里所有 `.c` 都算数"的语义有一个著名陷阱,[AGENTS.md](../../AGENTS.md) 把它写进了环境规则与 Common Pitfalls:**调试时随手丢在项目目录里的探针 `.c` 文件会被静默收编进编译**。症状千奇百怪(重复符号、行为漂移),根因只是目录收集忠实地做了它的工作。规矩是探针放 `/tmp` 或测试产物目录,不进真实项目目录。

**`--sources-from-make` 用 make 的干跑(dry run)恢复源清单。** `_scan_make_goal()` 的尝试序列编码了对真实构建系统的经验:

```python
attempts = [
    (("-n",), ()),          # 最便宜:纯 dry-run
    (("-n",), ("clean",)),  # goal 已是最新时 -n 不输出任何编译命令;
                            # 干跑 clean 不真删文件,却能逼出全量重建命令
    (("-nB",), ()),         # 放最后:-B 强制全部前置目标,可能触发
                            # 昂贵或脆弱的重新配置规则
]
```

每个 makefile 候选(顶层 Makefile 之外还会尝试 `Makefile.in`——zlib 的顶层 Makefile 只是"先跑 ./configure"的桩,真实规则在 `Makefile.in`,这个回退就是 zlib 集成时加的)按此序列跑,对 stdout 逐行做两类提取:`_extract_c_sources_from_make_line()` 用 `shlex` 切词,认领存在于磁盘的 `.c` token,并经 `realpath`/`commonpath` 判断是否在项目树内;`.o`/`.lo` token 走 `_infer_c_source_from_object_token()` 反推同名 `.c`(还认识 libtool 的 `xxx_la-` 前缀)。`_extract_cpp_args_from_make_line()` 只回收 CPP 级旗标——`-D/-U/-I/-include/-isystem/-iquote/-idirafter` 的双 token 与前缀两种形态,路径参数归一化为绝对路径。这些旗标随后喂回 3.3.2 的 `_system_cpp`,也用于 `main` 检测,保证检测与真实编译同一宏环境(`_main_detection_cpp_args`)。

干跑全失败时还有一层**纯文本 Makefile 解析回退**:`_parse_makefile_tree()` 处理续行、`include`/`-include`、`ifdef`/`ifeq` 条件栈、`=`/`:=`/`+=`/`?=` 的赋值语义差异,`_make_expand()` 做 `$(...)` 变量展开(支持 `firstword`/`dir`/`strip`,`wildcard` 一律展开为空,带自引用防护);`_fallback_goal_sources()` 沿规则图从 goal 走到 `.c` 前置,兜底再扫 `OBJS`/`OBJECTS`/`SRCS`/`SOURCES` 变量。未配置树里的 autoconf 占位符(`@CFLAGS@` 这类)被 `_AUTOCONF_PLACEHOLDER_RE` 过滤,`PTHREAD_CFLAGS` 还有专门的 configure 文件回溯推断。甚至当某行编译命令完全没带旗标时,`_probe_make_cpp_arg_groups()` 会用 `make -n -W <src> <obj>.o` 假装源文件变新,逼 make 打印那条编译命令。

限制必须说清([AGENTS.md](../../AGENTS.md) Compile Modes 同样强调):这套机制**只能恢复构建系统真正发出的旗标**。只写在头文件注释或文档里、靠人肉 `-DXXX` 传入的配置宏,任何干跑都看不见。zlib 的 `HAVE_UNISTD_H` 这类 configure 注入宏,最终是在预处理层按项目特征补的,而不是 make 扫描能解决的。

## 3.7 历史与教训

本节取材 [docs/investigations/](../../docs/investigations) 的三份 C 侧调查报告。它们有同一个叙事骨架:真实项目失败 → 第一直觉是"规模/链接器/MCJIT 问题" → 直觉错了 → 逐级缩减后发现是编译器语义 bug。这骨架本身就是教训。

### 3.7.1 SQLite:前向声明的位域结构体分裂了类型图

(来源:[docs/investigations/sqlite-forward-declared-bitfield-struct-tags.md](../../docs/investigations/sqlite-forward-declared-bitfield-struct-tags.md))

症状离根因极远:`sqlite3_step()` 对一条 `INSERT` 返回 `0` 而非 `SQLITE_DONE`,`db->errMask` 变成 `0` 把后续错误码全部掩没。第一嫌疑人照例是 MCJIT 扩展性。决定性的缩减是一个使用**真实 SQLite 类型**的最小辅助函数:

```c
static int same_db(Vdbe *p, sqlite3 *db) { return p->db == db; }
```

原生编译器下返回真,pcc 下返回假。第一字段的指针读取都错了,问题显然在类型低层化,与 SQLite 逻辑无关。

结构性根因:SQLite 的 `sqlite3` 与 `Vdbe` 互相递归引用,且 `Vdbe` 含位域,走 pcc 的自定义布局路径。两个 bug 在此叠加:其一,`struct Vdbe { ... };` 这种**独立 tag 定义**(无对象声明)在 `codegen_Decl()` 里漏进了普通对象声明路径,没有被当作纯类型定义注册;其二,前向声明过的命名位域结构体在定义时可能**新建**一个 `layout_*` 的 LLVM identified type,而不是复用已存在的 `struct_*` 前向 tag 类型。结果是类型图分裂:一部分代码引用不透明的 `struct_Vdbe`(IR 里赫然一行 `type opaque`),另一部分用独立的布局类型,递归图上这是致命的。修复后留下两条不变式:**同一源级 struct tag 必须对应同一 LLVM identified type,任何分裂都是编译器 bug**;独立 tag 定义在 AST 里长得像声明,但不声明存储,必须特判。回归测试沉淀在 [tests/c/test_bitfields.py](../../tests/c/test_bitfields.py)。

### 3.7.2 PCRE:挂起的循环,病根在零长全局

(来源:[docs/investigations/pcre-op-lengths-incomplete-array-binding.md](../../docs/investigations/pcre-op-lengths-incomplete-array-binding.md))

PCRE 编译通过,运行时在 `pcre_compile("hello", ...)` 里永久挂起。当时的工作重心恰好是给分离 TU 加系统链接路径,所以第一理论又是"22 个模块对 MCJIT 太多了"。证伪干净利落:换成系统 `cc` 链接,挂起依旧——**链接策略实验排除了链接器,指针转向被编译的程序本身**。

随后是教科书式的缩减链:LLDB 附上看到活栈停在 `auto_possessify()`;循环体内插桩打印出 `c = 131`(`OP_BRA`)而 `PRIV(OP_lengths)[c]` 恒为 `0`,于是 `code += OP_lengths[c]` 原地踏步;一个只打印 `_pcre_OP_lengths[129..133]` 的微型程序输出 `0 0 0 0 0`——挂起问题就此变成常量数据问题。预处理后的源码完全正确,决定性证据在 IR 里:

```llvm
@"_pcre_OP_lengths" = global [0 x i8] zeroinitializer
@"_pcre_OP_lengths.1" = global [162 x i8] [i8 1, i8 1, ...]
```

真实数据在,但挂在改名后的符号下;外部引用全部解析到零长占位符。根因有二:旧的数组声明路径在从初始化器推出真实长度**之前**就创建了文件作用域全局,事后只能另起 `.1` 后缀的新符号;同时 `extern const pcre_uint8 _pcre_OP_lengths[];`(不完整数组声明)与后续完整定义没有被按 C 标准视为同一对象统一。修复对应两条:先推长度再建符号,彻底消灭 `[0 x T]` 占位路径;文件作用域把"不完整数组声明 + 同元素类型的完整定义"判为兼容。报告留下的调试格言值得整段抄录在工程记忆里:**表驱动的循环挂起时,先打印表项再怀疑控制流;预处理源正确不代表符号绑定正确,必须看 IR**。

### 3.7.3 一次集成,三类 bug:SQLite VFS 与 zlib 的并发教训

(来源:[docs/investigations/sqlite-integration-vfs-init-and-mcjit-lifecycle.md](../../docs/investigations/sqlite-integration-vfs-init-and-mcjit-lifecycle.md)、[docs/investigations/zlib-integration-static-local-arrays-and-layout.md](../../docs/investigations/zlib-integration-static-local-arrays-and-layout.md))

SQLite 集成报告的标题就是结论:它**不是一个 bug**。`(sqlite3_syscall_ptr)lstat` 这类**带强制转换的函数指针常量**在常量指针路径上没有拆开 `c_ast.Cast`,整张 Unix VFS 系统调用表被低层化成空指针;`struct sqlite3 db = {0};` 的函数作用域聚合初始化没有真正清零;最后,程序打印 `OK` 之后宿主 Python 进程 SIGSEGV——这才是真正的 Darwin MCJIT 析构问题,3.5 节那套 detach 列表与 `os._exit()` 子进程隔离就是它的产物。zlib 那份报告同样一次暴露三层:`enum` 被低层化成 64 位整数破坏 inflate 状态布局、块作用域 `static` 数组被当自动局部变量、`static const char my_version[] = "1.3.1";` 不从初始化器推长度变成零长全局(与 PCRE 同族,版本检查直接 `Z_VERSION_ERROR`)。教训是方法论性的:**堆叠的失败必须拆成各自带证据链的独立 bug**,一个修复让症状位移之后,新症状要重新立案,而不是并进同一个"根因"叙事——这条纪律后来被写进 [AGENTS.md](../../AGENTS.md) 的自举回归规程第 3 条。

## 3.8 小结

C 前端的每一层都是同一个判断的不同投影:**复用成熟件起步,但把"可被 pcc 自己编译、可被审计"作为演化方向**。解析器从 pycparser/PLY 出发,冻结表 + 原生驱动 + 手写词法逐步替换运行时魔法,行为等价由 parity 闸门钉住;预处理借系统 `cc -E` 之力,用 `-nostdinc` + 伪 libc + 大表兼容宏把宿主世界挡在边界外,文本整形被明确圈定在这一层;伪 libc 用"声明即接口"换得跨机器可重复的解析输入,代价是一份必须与宿主 ABI 对齐的 typedef 断言清单;求值器用三层缓存(编译器指纹废止、`.so` 复用、进程内 JIT)摊销冷启动,用四条执行出口覆盖从交互求值到 self 后端的谱系;源收集对 make 做干跑考古,并诚实声明边界——构建系统没说出口的旗标,谁也恢复不了。而 3.7 的三个故事反复验证同一条经验:真实项目的失败极少是它表观上的那个问题,缩减到带真实类型的最小复现、读 IR 而不是猜,才是这套前端长出今天形状的方式。

## 练习

1. **读源码验证。** 在 [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py) 中找到 `p_declaration` 与 `p_decl_body` 的拆分注释,解释:如果合并为单条规则,`typedef int T; T x;` 两行连写为什么会解析失败?yacc 的向前看 token 在其中扮演什么角色?
2. **缓存考古。** 对照 `_DEFAULT_PLY_YACCTAB` 的手工版本号与 [pcc/frontends/c/parse/c_parsetab.py](../../pcc/frontends/c/parse/c_parsetab.py) 的 `GRAMMAR_SHA256` 机制,各写出一种它们能/不能捕获的陈旧场景;再看 `_compiler_cache_fingerprint()`,说明编译产物缓存为什么不需要任何手工版本号(提示:`_COMPILE_CACHE_VERSION` 仍然存在,它防的是哪类变化?)。
3. **伪 libc 失配实验(纸面)。** `_fake_typedefs.h` 断言 `mode_t` 为 `unsigned short`,内置预处理器 `TYPE_PREAMBLE` 断言 `unsigned int`。构造一个最小 C 程序,使它在两条预处理路径下产生不同的 `sizeof` 行为;再论证:什么样的真实 libc 调用会把这个失配变成运行时错误?
4. **设计权衡。** 内置预处理器为实现 `#if` 求值专门写了 `_CppExprParser`,而不是调用 `eval()`。除了自举审计的禁令,再给出至少两个独立于自举的理由(提示:C 语义 vs Python 语义;攻击面)。反方向论证一次:如果 pcc 永远不自举,`eval()` 方案是否就是正确的工程选择?
5. **案例研究重演。** 仅凭 3.7.2 的信息,写出你在拿到"PCRE 在 `pcre_compile` 挂起"这个报告后的前四个动作,并为每个动作标注它要证伪的假设。然后对照报告原文 [docs/investigations/pcre-op-lengths-incomplete-array-binding.md](../../docs/investigations/pcre-op-lengths-incomplete-array-binding.md) 的实际顺序,找出你的方案中最昂贵的多余步骤。
