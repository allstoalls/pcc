# 第 2 章 体系结构总览

第 1 章给出了 pcc 的论题与七条义务;本章给出空间地图:这些承诺在仓库里各住在哪一层,一个源文件从命令行到可执行文件要经过哪些手。pcc 是"两个编译器、一个运行时"的共居体——成熟的 C 前端、实验性的类型化 Python 前端、带五个 GC 后端的原生运行时——它们共享 CLI、项目收集、后端选择与缓存基础设施,却各自拥有完整的流水线。本章只看骨架:两条流水线各画一张图,每个部件回答"为什么在这里、边界在哪",细节交给后续各章(解析与求值器见第 3 章,C 语义低层化见第 4 章,Python 前端三级见第 5、6 章,运行时与 GC 见第 7 至 11 章,后端见第 12、13 章,自举见第 15 章)。读完本章,应当能把仓库里任何一个文件放进这张地图,并说出它属于哪条流水线的哪一段。

## 本章导读:仓库的流水线结构

这一章最容易迷路,因为它会一次出现很多目录。先按"一份源文件怎样变成可运行产物"来读:入口收集输入,前端建立语义,低层化阶段生成 IR,后端发射机器相关产物,运行时负责 Python 对象和执行语义。

- C 路径和 Python 路径不是两个孤岛:它们最终都要面对后端、链接和运行时边界。
- CLI、project、frontend、codegen、runtime、backend 是分层责任,不是随便命名的文件夹。
- 读到任何路径时,先问它站在哪一层,再问它能不能跨层替别人做决定。

## 2.1 问题与设计空间:一个仓库为什么装得下两个编译器

把一个 C 编译器和一个 Python 编译器塞进同一个代码库,初看是历史包袱,实际是结构选择。三个子系统互为测试者,构成一个闭环:

1. **C 前端是成熟参照。** 它编译并运行 Lua、SQLite、PostgreSQL `libpq`、zlib、PCRE、OpenSSL 这一级别的真实项目(README 状态表),为整个仓库提供"编译器应当是什么质量"的基线。
2. **Python 前端是自举(bootstrap)轨道。** 它是实验性的,目标不是覆盖全部 Python,而是把 pcc 自身的源码编译成原生二进制——`pcc0/host → pcc1 → pcc2 → pcc3` 的不动点(见第 15 章)。
3. **运行时被两条流水线共同消费。** Python 路径把它当链接对象(`libpy_runtime*.a`);当前生产 `libpy_runtime_pcc_py.a` 只由 pcc 编译的 semantic/freestanding Python 对象组装,C 前端继续编译 C/oracle 路径并验证共享 ABI。两条前端因此都约束运行时可信度,但不再共享一个生产 C 实现。

这个闭环解释了仓库里若干否则显得奇怪的纪律。模式标注的(mode-labeled)声明不是文档礼貌,而是结构必需:同一条命令 `pcc x.py`,跑在宿主 CPython 上是 host pcc,跑在编译产物上是 pcc1,两者的能力边界不同(README 明确:pcc1 对 C 输入目前是**委托宿主 `pcc` 的兼容外壳**,不是 pcc1 原生执行 `c_evaluator.py`);`--python-libpython=off` 与 `auto` 产出的二进制依赖面不同;`--backend llvm` 与 `--backend self` 的执行根不同。本章出现的每一个组件都会标注它属于哪个模式空间。

设计空间上,pcc 对"编译器以什么形态存在"给出了三个并存的答案,各有不可替代的理由:gcc 风格的命令行驱动(交互、项目集成)、可嵌入的 Python 库 API(`pcc.build`/`pcc.module`,给宿主 Python 程序用)、以及一个**自身被编译**的自举 CLI。第三种是 pcc 独有的约束来源:CLI 代码不只是"调用编译器的胶水",它本身是编译目标,这反过来规定了它的书写风格(见 2.2)。

## 2.2 CLI 层:三个入口与它们各自的存在理由

### 2.2.1 安装入口与 click 包装

`pip install python-cc` 安装的 `pcc` 命令由 [pyproject.toml](../../pyproject.toml) 的 `[project.scripts]` 指向 `pcc.driver.cli_launcher:main`。[pcc/driver/cli_launcher.py](../../pcc/driver/cli_launcher.py) 全文 22 行:

```python
# pcc/driver/cli_launcher.py
def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    from pcc.driver.cli_core import cli_main

    return cli_main(list(argv))
```

文档字符串直接声明立场:"The public command intentionally stays on the full CPython-hosted CLI. The native bootstrap compiler is exposed separately as `pcc1`."——公共命令是宿主 CPython 上的完整 CLI,原生自举编译器另行以 `pcc1` 暴露(轮子构建钩子 `hatch_build.py` 会自编译 [pcc/__main__.py](../../pcc/__main__.py) 产出原生 `pcc1` 随轮子发货)。launcher 只做一件事:转调 [pcc/driver/cli_core.py](../../pcc/driver/cli_core.py) 的 `cli_main`。

[pcc/pcc.py](../../pcc/pcc.py) 是另一层薄壳:`_build_click_main()` 在运行期 `__import__("click")`,把 `_click_entry` 用 click 的装饰器逐个包出带补全与帮助的命令对象;click 不可用时回落到 `_plain_main`,即同一个 `cli_main`。这个"装饰器在函数里手工套"的写法不是风格怪癖——它让 click 成为可选依赖,缺了它 CLI 照常工作。

### 2.2.2 手写参数解析器是给自举写的

真正的解析逻辑在 [pcc/driver/cli_core.py](../../pcc/driver/cli_core.py) 的 `parse_cli_args`:一个 `while i < len(argv)` 的手写循环,每个旗标写两个分支(`--flag=value` 与 `--flag value`),返回一个巨大的元组。没有 argparse,没有 click。原因在同文件的细节里可以读出来:作用域环境变量覆盖器 `_temporary_env` 是显式类而非 `@contextmanager`,注释写明"to keep the self-host audit clean";序列复制用手写的 `_copy_seq` 而非切片惯用法;字符串一律以 `(value or "") + ""` 归一。这些都是自举可编译子集的惯用法——`cli_core.py` 属于自举审计([scripts/audit_selfhost.py](../../scripts/audit_selfhost.py))覆盖的文件集,是 pcc1 将来原生执行 C 驱动路径的目标闭包的一部分,尽管今天这一步尚未完成(README 状态表把它列为未来工作)。

`cli_main` 的分派顺序本身就是架构图:`-m MODULE` 最先截获(经 `runpy` 跑宿主模块,`pip`/`pip3` 被重写为 `pcc.package.pip_shim`,见第 17 章);`-h/--help` 次之;然后 `parse_cli_args`;最后按路径后缀分流——`.py` 进 Python 流水线,其余进 C 流水线。`.py` 且未给 `-o` 时,编译产物写进临时目录并以子进程运行,退出码透传;这与 C 单文件默认的进程内 MCJIT 执行(2.3.4)形成对照:Python 路径从第一天起就只有"真实进程跑真实二进制"一种执行语义。

### 2.2.3 cli_bootstrap:被编译的 CLI

[pcc/__main__.py](../../pcc/__main__.py) 只有几行:

```python
# pcc/__main__.py
from pcc.driver.cli_bootstrap import bootstrap_cli_sys_argv_exit


if __name__ == "__main__":
    bootstrap_cli_sys_argv_exit()
```

这是自举链的入口——[scripts/bootstrap.sh](../../scripts/bootstrap.sh) 的三个阶段编译的就是 [pcc/__main__.py](../../pcc/__main__.py)。[pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py)(约七千行)是 pcc1/pcc2/pcc3 实际运行的 CLI:Python 输入由这个二进制自己编译;C 与项目输入按其帮助文本所述"delegated to the full host pcc CLI"(可用 `PCC_HOST_PCC` 覆盖宿主入口);`--pytest` 子命令让 pcc1 启动仓库测试套件(委托 `env -u LC_ALL uv run pytest` 并设置 `PCC1_BINARY`,使 pcc1 专属用例拿到当前二进制)。

为什么不让 `cli_core` 直接当自举入口?因为两者的依赖闭包不同。`cli_core` 要 import `CEvaluator`、`project.py` 等 C 路径模块,那是一个今天还编译不了自己的闭包;`cli_bootstrap` 的闭包被刻意收窄到 Python 流水线加委托逻辑。多文件自举编译则由 [scripts/pcc_multi.py](../../scripts/pcc_multi.py) 入口承担——它包装 `pipeline.compile_python_multi`,而且自身用 `pcc.extern` 写退出逻辑,同样是按"将被 pcc 编译"的标准书写的。

### 2.2.4 关键旗标

三个旗标决定一次 Python 编译落在哪个模式空间,默认值都偏向严格一侧(README 与 `pipeline.py` 的 `_resolve_libpython_mode`/`_resolve_ir_scaffold_mode` 一致):

| 旗标 | 默认 | 语义 |
|---|---|---|
| `--python-libpython` | `off` | `off`:任何需要 CPython 回退(fallback)的程序硬错误;`auto`:检测到回退才链接 `libpython`;`on`:总是允许并链接回退面。环境变量 `PCC_PYTHON_LIBPYTHON`。 |
| `--ir-scaffold` | `on` | `on`:封闭世界 IR-builder 低层化(自举主路径),未实现的方法**清晰报错而非静默回退**(`_resolve_ir_scaffold_mode` 文档字符串原话);`off`:旧低层化路径的兼容逃生门;`auto` 归一为 `on`。 |
| `--backend` | `llvm` | `llvm`、`llvm_capi`、`self` 三选一,环境变量 `PCC_BACKEND`(见 2.5.1)。 |

C 路径的旗标族围绕项目形态:`--separate-tus`、`--sources-from-make GOAL`、`--depends-on PATH[=GOAL]`、`--system-link`、`--jobs N`(显式给出时要求与多输入或 system-link 搭配,否则报错)、`--cpp-arg`/`--link-arg`、`--prepare-cmd`/`--ensure-make-goal`,以及发射族 `--emit-llvm/--emit-asm/--emit-obj` 与交叉编译的 `--target TRIPLE`(`--target` 必须与发射模式或 `--system-link` 搭配)。诊断面是两条流水线共用的:`--diagnostic-format text|json|sarif`、`--profile-json PATH`、`--explain-fallback`,经环境变量传给 `pcc.diagnostics.compile_observability` 的 `observed_compile` 包装层。

一个值得单独点名的细节:C 路径上 `--backend self` 会把 `-O2` 默认钳到 0(`cli_core._effective_self_backend_opt_level`),除非设 `PCC_SELF_BACKEND_VECTORIZE`:

```python
# pcc/driver/cli_core.py
def _effective_self_backend_opt_level(backend, opt_level: int) -> int:
    backend_name = (backend or os.environ.get("PCC_BACKEND", "") or "").strip().lower()
    if (
        backend_name == "self"
        and int(opt_level) > 0
        and not _self_backend_vectorize_requested()
    ):
        return 0
    return int(opt_level)
```

注释说明原因:self 后端尚未完整低层化 LLVM 向量化指针存储(如 Lua 的 `<4 x ptr>` strcache 广播)。这是"诚实优先于跑分"的一个微观样本:与其让向量化 IR 在 self 后端上炸掉,不如公开降级并留出显式开关。

## 2.3 C 路径:从源收集到四个执行根

```text
pcc hello.c | pcc proj/ [--separate-tus | --sources-from-make GOAL | --depends-on ...]
        |
        v
pcc/driver/cli_core.py        cli_main -> parse_cli_args -> execute_cli
        |
        v
pcc/driver/project.py         源收集(本章 2.3.1;机制详见第 3 章)
   merged:  collect_project()            -> 一份合并源,main 文件殿后
   multi :  collect_translation_units()  -> [TranslationUnit(name,path,source)...]
   flags :  collect_cpp_args()           -> make 干跑推导的 -D/-I/...
        |
        v
pcc/frontends/c/evaluator/c_evaluator.py   每 TU 一次(--jobs 进程池并行;磁盘 artifact 缓存)
   _preprocess_translation_unit_source   cc -E + 伪 libc | 内置 preprocess
   make_c_parser().parse                 -> C AST
   PassPipeline.run_high_tier            AST 分析 -> PassContext
   CCodeGenerator.generate_code       语义低层化 -> LLVM IR(第 4 章)
   postprocess_ir_text + run_low_tier    IR 文本后处理(豁免仅 va_arg,第 12 章)
        |
        +---------------+----------------+---------------------+
        v               v                v                     v
   MCJIT 进程内     MCJIT 子进程      --system-link         --backend self
   单 TU evaluate() Darwin 多 TU      emit_object 后由      self 后端发射
   CFUNCTYPE 直调   JSON 结果回传     系统 cc 链接、真实     (第 13 章),
                                      进程运行              真实进程运行
        |
   --emit-llvm / --emit-asm / --emit-obj(emit_compiled_units;可 --target 交叉)
```

### 2.3.1 源收集与四种编译模式(project.py)

[pcc/driver/project.py](../../pcc/driver/project.py) 把"一个路径"变成"要编译的东西",输出统一为不可变的 `TranslationUnit(name, path, source)`。四种模式([AGENTS.md](../../AGENTS.md) Compile Modes 一节是权威表):

1. **单文件**:`pcc hello.c`,整文件读入即一个 TU。
2. **目录合并(merged,目录输入默认)**:`_collect_directory()` 非递归收集 `*.c` 并排序,用 `// --- 文件名 ---` 注释行拼成一份大源文本,含 `main()` 的文件放最后。`main` 判定 `_has_main()` 先正则粗筛、再做真实预处理确认,避免被 `#if` 排除的 `main` 误判。
3. **`--separate-tus`**:同一组文件各自成 TU,模块层链接;强制恰好一个 `main`,且 `compile_translation_units` 会对跨 TU 的重复外部定义抛错(`_raise_if_duplicate_external_definitions`)。
4. **`--sources-from-make GOAL`**:对 make 做干跑考古,从真实编译命令行恢复参与的 `.c` 与 `-D/-U/-I/-include` 族旗标;`_scan_make_goal()` 依次尝试 `-n`、`-n clean`、`-nB`(注释写明把 `-nB` 放最后,因为强制重建所有前置会触发昂贵或脆弱的重配置规则),还有 `Makefile.in` 探测、逐目标 `make -n -W src obj.o` 探针,以及一个纯 Python 的 Makefile 解析回退。机制细节与限制见第 3 章 3.6 节;它只能恢复**构建系统真的说出口**的旗标——这条边界是 2.6.1 案例研究的主题。

`--depends-on PATH[=GOAL]` 在多输入模式上叠加约束:依赖输入一个 `main` 都不许有,主输入必须恰好一个;依赖单元排在前、主单元在后。`--prepare-cmd` 与 `--ensure-make-goal` 则在收集前执行准备命令(生成头文件、预构建库),`run_prepare_commands` 会从子进程环境里摘掉 `LC_ALL`。

### 2.3.2 为什么 merged TU 是目录默认

这是本章蓝图点名的第一个设计问题。合并模式把整个目录变成**一个**翻译单元:一次预处理、一次解析、一次代码生成、一个 LLVM 模块,不需要任何跨 TU 的符号协调或链接层——对一个从单文件求值器长出来的编译器,这是最小增量的目录支持。`main` 殿后的排序让入口函数的调用点出现在其他文件的定义之后,降低对头文件原型完备性的依赖;`#include "x.h"` 交给预处理器照常解决。[docs/system-architecture.md](../../docs/system-architecture.md) 给它的定位是"fast/simple project experiments",并把 `--separate-tus` 标注为"more realistic C semantics"——这组措辞就是权衡的全部:

- 合并模式**抹掉了 TU 边界语义**。两个文件里同名的 `static` 函数在真实 C 里互不可见,合并后变成重定义;`struct` tag 在文件间的重用会以另一种方式暴露([AGENTS.md](../../AGENTS.md) Common Pitfalls 把 tag 重用列为惯犯)。重复外部定义检查也只存在于多 TU 路径。
- 反过来,分离模式付出的是协调成本:符号去重、模块链接、(Darwin 上)多模块 MCJIT 的生命周期问题(2.3.4)。

所以默认值的逻辑是:目录输入的典型场景是"把一小撮文件当一个程序跑起来",合并最快最简单;真实项目本来就不该用裸目录模式——它们走 `--sources-from-make`(源清单由构建系统说了算)或 `--separate-tus`(语义保真)。还有一条工程纪律寄生在这个默认上:目录模式会把目录里**所有** `.c` 收进来,所以绝不能把临时探针文件丢进真实项目目录([AGENTS.md](../../AGENTS.md) 环境规则明文禁止)。

### 2.3.3 求值器:每 TU 的五段流水线与缓存

[pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) 的 `CEvaluator` 是 C 路径的总指挥。每个 TU 经过 `_compile_translation_unit_artifact_job`:预处理(`_preprocess_translation_unit_source`,借系统 `cc -E` 加伪 libc 整形,或回退内置 `preprocess`)→ 解析(`make_c_parser().parse`)→ HighTier AST 分析 pass(填 `PassContext`)→ `CCodeGenerator.generate_code` 语义低层化 → `postprocess_ir_text` 与 LowTier IR pass。产物是可序列化的 artifact 字典:`ir_text`、`return_type`、`external_defs`、`func_return_types`、pass 报告——可序列化这一点不是装饰,它同时支撑磁盘缓存与 `--jobs` 的 `ProcessPoolExecutor` 跨进程并行。

缓存有三层,键里都掺了 `backend_signature`(后端身份,见 2.5.1)与优化/pass 签名:进程内 `_jit_cache`(源文本哈希直达函数指针)、原生 `.so` 磁盘缓存(`_build_native_cache`/`_load_native_cache`,冷启动只剩 `ctypes.CDLL`)、TU artifact 磁盘缓存(`_compile_cache_key`,叠加编译器自身指纹 `_compiler_cache_fingerprint()` 防陈旧)。逐层细节见第 3 章;本章只记住:**缓存键的设计就是模式边界的设计**——换后端、换 pass 选择、换目标三元组,都必须自然失效。

### 2.3.4 四个执行根

同一份 artifact 列表有四条出路,选择逻辑集中在 `execute_cli` 与 `CEvaluator`:

1. **进程内 MCJIT**(单文件默认):`evaluate()` 把 IR 喂给 `llvm.create_mcjit_compiler`,以 `CFUNCTYPE` 取 `main`(或任意 `entry`)地址直接调用——编译器进程就是执行进程,这也是库 API `module(...)` 形态的基础。
2. **子进程 MCJIT**(Darwin 多 TU 默认):`evaluate_compiled_translation_units` 在 macOS 上改走 spawn 子进程 `_run_linked_mcjit_worker`,结果经 JSON 文件回传。原因写在进程内路径的 `finally` 注释里:"MCJIT disposal is unstable for some large multi-TU programs"——析构不稳就把整个引擎隔离进可丢弃的进程,正确输出后的 SIGSEGV 不再殃及宿主(这段历史见第 3 章的 SQLite 故事)。
3. **`--system-link`**:`run_translation_units_with_system_cc` 用仓库管理的 LLVM 对每模块做优化、`target_machine.emit_object` 直接发射原生目标文件,再交系统 `cc` 链接成真实可执行文件运行——注意 IR 文本**不**经过系统编译器之手([AGENTS.md](../../AGENTS.md) IR Fix Policy 记录了这次收紧)。
4. **`--backend self`**:`_run_compiled_translation_units_self_backend`,LLVM 完全离场,self 后端发射(第 13 章),同样以真实进程运行。

`--emit-llvm/--emit-asm/--emit-obj` 是第五条"不执行"的出路:`emit_compiled_units` 把各模块 `link_in` 合并后发射,配合 `--target` 即交叉编译。

### 2.3.5 库 API:api.py

[pcc/api.py](../../pcc/api.py) 把 C 工具链做成可嵌入库:`build(sources, ..., kind="exe"|"sharedlib"|"object")` 返回 `BuildArtifact`(产物路径、导出符号、pass 报告、IR 文本);`module(...)` 即 `build(kind="sharedlib")` 加 `ctypes.CDLL` 加属性绑定,让 `m.add(3, 4)` 直接调进编译产物。一个边角值得注意:`Module.__getattr__` 用 `self._lib[name]` 下标而非 `getattr` 取符号,注释言明这是为了不触发自举审计的动态属性规则,并标注 `Module` 是宿主 CPython 集成面——pcc 的自举 CLI 在运行期从不加载共享库。又一次,自举约束渗进了与自举无关的代码的书写方式。

## 2.4 Python 路径:从 .py 到 no-libpython 可执行文件

```text
pcc app.py [-o out] [--emit-llvm] [--backend llvm|self]
           [--python-libpython off|auto|on] [--ir-scaffold on|off|auto]
        |
        v
pcc/driver/cli_core.py(宿主)/ pcc/driver/cli_bootstrap.py(pcc1,自身即编译产物)
   observed_compile(compile_python, ...)    诊断格式/profile/回退解释包装
        |
        v
pcc/frontends/python/pipeline.py :: compile_python
   闭包收集   _collect_relative_module_closure(相对导入;入口为 __main__ 时
              收同包绝对导入;off 模式递归)+ 递归 stdlib -> 转多文件路径
   ABI 验证   _validate_package_site_no_libpython_abi(site 包的扩展 ABI 闸门)
   解析       pcc.frontends.python.py_parse + py_lift(自举安全;CPython ast 逃生门已拆除)
   类型推断   type_infer.infer_module(第 5 章)
   代码生成   codegen.layer1.L1CodeGen.generate(facade + mixin 群,第 6 章)
              -> LLVM IR 文本
   IR pass    _apply_python_ir_pass_pipeline
        |
        +-- --emit-llvm: 写出 .ll 即止
        v
   回退判定   _module_needs_libpython(AST)+ _ir_needs_libpython(扫 py_cpy_* 调用)
              -> _finalize_libpython_mode:off=硬错误(带原因);auto=按需;on=总是
        |
        v
   运行时档案 _ensure_runtime(PCC_RUNTIME_CC × PCC_RUNTIME_HIGH × needs_libpython
              选 libpy_runtime*.a;陈旧检测,Makefile 重建;第 14 章)
        |
        +-----------------------------+
        v                             v
   llvm 后端:clang 链接 .ll      self 后端:宿主 Python 子进程发射 asm/obj
   + 运行时档案                   (PCC_HOST_PYTHON;pcc.backend.* 不进闭包)
                                  系统 cc 链接;codesign -> mv -> verify -> 屏障
        |
        v
   原生可执行文件(未给 -o 时编译进临时目录,子进程运行,退出码透传)
```

### 2.4.1 入口与闭包收集

`compile_python(src_path, out_path, ...)` 是单文件入口,但"单文件"只是请求形状,不是编译形状。它先做模块闭包收集:`_collect_relative_module_closure` 追相对导入;当入口模块名以 `.__main__` 结尾时把同包绝对导入也收进来;`--python-libpython=off` 时对同包绝对导入递归。随后 `_filter_ir_scaffold_closure` 按 scaffold 模式过滤,`_validate_package_site_no_libpython_abi` 对来自 site 包的源做扩展 ABI 检查(拒绝 CPython ABI 工件混入 pcc-native 闭包,见第 17 章)。若源码使用原生 stdlib 且处于严格模式,递归 stdlib 展开被强制打开——pcc 自带的 [pcc/stdlib/](../../pcc/stdlib) 端口优先,找不到才探询宿主(2.4.5)。闭包超过一个文件就转 `compile_python_multi`,它把闭包按模块切分、用工作进程并行做代码生成(`_python_frontend_jobs` 默认自动并行,封顶 10——注释记录了实测:自举闭包上 8 到 10 个工作进程占优,12 个开始输给进程与 IO 争用)。一个反身性细节:工作进程的可执行文件由 `_python_frontend_worker_executable` 解析,在编译版 pcc1 里它就是 pcc1 自己——**编译出来的编译器把自己再 exec 成自己的代码生成工人**。

### 2.4.2 前端三级与解析器的去 libpython 化

单模块的主干是三级:`pcc.frontends.python.py_lift.parse_and_lift`(源文本 → pcc 自有 AST)、`type_infer.infer_module`(类型推断,第 5 章)、`codegen.layer1.L1CodeGen.generate`(低层化为 LLVM IR 文本;`layer1.py` 已拆成 facade 加 mixin 群,第 6 章)。`pipeline.py` 在解析调用点留了一条注释作历史界碑:`pcc.frontends.python.py_parse + py_lift` 是自举安全的解析路径,先前那个借 CPython `ast` 模块的逃生门"kept a libpython import edge alive in the compiled pipeline",已被拆除。同一个判断反复出现:任何在编译产物里残留的宿主依赖边,都是自举闭包上的洞。

### 2.4.3 回退判定:双探针与三态收尾

`--python-libpython` 的三态在 `_finalize_libpython_mode` 收口,而"是否需要回退"的判定是双探针:AST 级 `_module_needs_libpython`(导入是否仍经 CPython 桥),加 IR 级 `_ir_needs_libpython`——对生成 IR 扫 `py_cpy_*` 的**调用点**。注释解释了为什么用 `\bcall` 模式而非朴素文本搜索:运行时辅助符号的 `declare` 桩是无条件发射的,只有真实 call 才证明产物会走回退。`off` 模式下检测为真即抛 `PyPipelineError`,错误信息携带原因列表("imports still lower through CPython fallback"、"generated IR still calls py_cpy_* helpers")并提示改用 `auto/on`。**失败响亮、原因具名**,这是第 1 章义务在管线上的落点;按模块统计的回退数被 [tests/fallback_baseline.json](../../tests/fallback_baseline.json) 棘轮锁住,只许降不许升。

### 2.4.4 运行时档案与两个链接根

`_ensure_runtime` 按三个维度选择要链接的运行时档案:`PCC_RUNTIME_CC`、`PCC_RUNTIME_HIGH` 与是否需要 libpython 桥。默认组合落在 `libpy_runtime_pcc_py.a`;档案存在还要通过陈旧检测与 provenance 验证,否则经 Makefile 重建。2026-08 的生产配方比早期“高层端口替换 C”模型更强:`LIB_PCC_PY` 只归档 `PCC_PY_OBJECTS`,其中同时含 semantic `PY_MODULES` 与 `FREESTANDING_PY_MODULES`。C 源保留为 oracle,不因存在于 `src/` 就进入生产归档。no-libpython、Python-owned runtime 与 Linux zero-libc 的差别见第 14 章。

链接根有两个,由 `_link_native` 分派:`llvm` 走 `_link_with_clang`(先经 `_clang_link_compatible_python_ir` 把较新的 LLVM 内存效应属性降级成 clang 可吞的形态);`self` 走 `_link_with_self_backend`,其产物发布序列值得整段引用——临时文件上 `codesign --force -s -`,`/bin/mv -f` 原子改名,`codesign --verify`,最后一道发布屏障(`/bin/sync` 或对产物做一次完整读)。这串仪式的来历是 2.6.2 的案例研究。注意 Python 路径的发射后端只接受 `llvm` 与 `self`(`_resolve_native_backend` 对 `llvm_capi` 明确报错):`llvm_capi` 是 IR 构建层的选择,不是 Python 可执行文件的发射器。

### 2.4.5 为什么宿主查询走子进程而非进程内

这是蓝图点名的第二个设计问题。Python 流水线里所有"问宿主"的动作都是子进程:`_host_find_spec_origin` 起 `python3 -c` 探询 stdlib 模块源路径;self 后端的发射器更是整体跨进程——`_emit_self_asm_via_host_python` 把 IR 写进临时文件,用 `PCC_HOST_PYTHON`(默认探测 `.venv/bin/python3`,再回退 `python3`)运行一段内联脚本 `_SELF_BACKEND_HOST_CODE`,**在宿主进程里** import `pcc.backend.self_backend_dispatch` 并发射汇编,结果经 stdout/TSV 文件传回。

进程内调用明明更快,为什么不用?因为编译产物的 import 闭包就是自举闭包。若 pcc1 在进程内 `import pcc.backend.*`,这些模块立即成为"pcc 必须能编译的源",而它们今天不在自托管子集里——结果是 `py_cpy_*` 回退边重新进入 stage1 闭包,no-libpython 性质静默瓦解。[AGENTS.md](../../AGENTS.md) 把这条写成硬规则:`_link_with_self_backend` 不得重新引入对 `pcc.backend.*` 的编译期导入或调用;长期解法是把后端模块原生编译进来,而不是扩大进程内 CPython 回退。子进程边界的额外红利是**可证伪**:把 `PCC_HOST_PYTHON` 指向 `/bin/false`,任何宿主求助立即暴露——这正是包闸门的证据法(第 17 章)。代价当然存在:进程启动开销、文件与文本协议的脆弱性;为摊销前者,跨进程发射器自带按后端源码哈希签名的对象缓存(`PCC_SELF_BACKEND_OBJECT_CACHE`)。

## 2.5 三后端与仓库地图

### 2.5.1 后端选择:显式 opt-in 与缓存身份

[pcc/backend/__init__.py](../../pcc/backend/__init__.py) 维护后端注册表 `_BACKEND_TABLE`:`llvm`(semver 标 `llvmlite-default`,默认)、`llvm_capi`(in-repo LLVM-C 构建层,第 12 章)、`self`(`self-aarch64-asm-v0`)。`self` 在表里标 `supported: False`,但能力列表写着 `emit-asm`/`emit-object`/`run-native-via-system-cc`/`aarch64-darwin-mvp`——这对组合编码的语义是"**能用,但必须显式点名**":`resolve_backend` 默认对 unsupported 后端抛 `BackendUnavailable`,只有当用户在 CLI 或 `PCC_BACKEND` 里明确写了 `self`(`backend_request_allows_unimplemented` 判定)才放行。实验性状态被编码进了类型系统层面,而不是埋在文档里。`backend_signature` 把后端身份串进所有编译缓存键,换后端绝不会撞缓存。与之配套的是义务 4 的禁令([AGENTS.md](../../AGENTS.md)):`--backend=self` 之后不许静默回退 LLVM——后端选择是模式声明,不是性能提示。

### 2.5.2 仓库地图

权威全表在 [AGENTS.md](../../AGENTS.md) 的 Repository Map;下表是本章视角的精简版,按"哪条流水线的哪一段"组织:

| 路径 | 流水线位置 |
|---|---|
| [pcc/driver/cli_launcher.py](../../pcc/driver/cli_launcher.py)、[pcc/pcc.py](../../pcc/pcc.py)、[pcc/driver/cli_core.py](../../pcc/driver/cli_core.py) | 宿主 CLI:安装入口 → click 包装 → 手写解析与分派 |
| [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py)、[pcc/__main__.py](../../pcc/__main__.py)、[scripts/pcc_multi.py](../../scripts/pcc_multi.py) | 自举 CLI:pcc1/pcc2/pcc3 的入口与多文件编译入口 |
| [pcc/api.py](../../pcc/api.py) | C 路径库 API(`build`/`module`) |
| [pcc/driver/project.py](../../pcc/driver/project.py) | C 源收集:目录/合并/make 干跑/依赖项目 |
| [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) | C 求值器:预处理→解析→IR→优化→四执行根 |
| [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py)、[pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) | C 解析(第 3 章)与 C 语义低层化(第 4 章) |
| [pcc/frontends/python/py_parse.py](../../pcc/frontends/python/py_parse.py)、`py_lift.py`、[pcc/frontends/python/](../../pcc/frontends/python) | Python 解析/提升、类型推断、低层化(第 5、6 章) |
| [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) | Python 流水线总指挥:闭包、回退判定、链接、发布 |
| [pcc/runtime/](../../pcc/runtime) | 运行时:semantic/freestanding pcc-Python 生产 owners + C oracle + 五 GC(第 7–11、14 章) |
| GUI 框架 | 声明式 GUI kernel 与产品 canary(第 20 章);2026-09-06 迁至 [allstoalls/pcc-gui](https://github.com/allstoalls/pcc-gui) |
| [pcc/ir/](../../pcc/ir)、[pcc/backend/](../../pcc/backend) | LLVM-C 构建层(第 12 章)与 self 后端(第 13 章) |
| [pcc/extern/](../../pcc/extern)、[pcc/unsafe/](../../pcc/unsafe) | pcc-Python 写底层的两件工具(第 14 章) |
| [utils/fake_libc_include/](../../utils/fake_libc_include) | 伪 libc 头(第 3 章) |
| [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json)、[tests/fallback_baseline.json](../../tests/fallback_baseline.json) | 自举与回退的权威基线(第 15 章) |

## 2.6 历史与教训

三个故事都关于**边界**:第一个划定 CLI 与构建系统的边界,第二个划定链接器与装载器的边界,第三个证明 CLI 自身就站在被编译的边界上。

### 2.6.1 make 干跑考古的边界:旗标属于谁

(来源:[docs/investigations/make-derived-cpp-flags-vs-explicit-project-config.md](../../docs/investigations/make-derived-cpp-flags-vs-explicit-project-config.md),首次提交于 2026-03-28)

**症状与问题。** pcc 获得 `--cpp-arg` 与 make 推导旗标能力后,出现一个无法回避的不对称:`--sources-from-make` 能完整覆盖 PCRE,却覆盖不了 Lua、zlib、SQLite——后三者仍需手工旗标。这表观上像推导实现不完整。

**证据链。** 调查逐项核对四个项目的干跑输出。PCRE 的编译命令行里真有 `-DHAVE_CONFIG_H -I.`,推导"不是猜测,只是复用项目自己声明的构建输入"。Lua 的 `make -n lua` 输出有 `-DLUA_USE_LINUX`,却没有 pcc 需要的 `-DLUA_USE_JUMPTABLE=0` 与 `-DLUA_NOBUILTIN`——因为那两个根本不是 Lua 构建系统的决定,而是 pcc 用户为绕开尚不支持的编译器路径而做的**兼容性选择**。zlib 的树未配置,顶层 Makefile 只会打印 "Please use ./configure first.",压根没有可考古的编译命令。SQLite 的命令形态是 `--depends-on .../sqlite3.c`,连 make 目标都没有。

**真正根因。** 不是推导不完整,而是问题被错误地归成了一类。调查提炼出的规则成为这一层的设计公理:"Build-system-derived inference can only recover configuration that the build system has already made concrete."(构建系统推导只能恢复构建系统已经具体化的配置。)随后把显式 `-D` 旗标分成两类:**构建配置旗标**(`HAVE_CONFIG_H` 族,理想来源是配置后的构建元数据)与**编译器兼容旗标**(`LUA_NOBUILTIN` 族,属于 pcc 的当前限制,应当显式、可见、有文档)。

**留下的不变式。** 调查明文列出"什么会是设计质量的倒退":"if the path contains `sqlite`, add `-DSQLITE_THREADSAFE=0`"——按项目名注入旗标。这条禁令与义务 3 的"禁止 `if package == "numpy"`"是同一条原则在 C 侧的投影,如今的分层是:构建系统拥有项目配置,CLI 暴露显式用户选择,编译器实现 C 语义。兼容旗标的消失只有一条正路:把编译器修对,然后从命令行里删掉它。

### 2.6.2 刚链接的 Mach-O 不能立刻 exec:发布是流水线的一段

(来源:[docs/investigations/self-backend-mach-o-stage-publish-race.md](../../docs/investigations/self-backend-mach-o-stage-publish-race.md),更新至 2026-05-15;状态:仍有活跃后续)

**症状。** 五 GC 后端 #4 的开发期间,强制自举闸门间歇性回归:`bootstrap.sh` 退出码 139,stage2 刚产出 `pcc2`,stage3 立即执行它就 SIGSEGV——但同一个二进制**再跑一次就成功**,挂上 LLDB 也成功。

**错误假设与证伪。** 间歇性 + 自举链,第一直觉是 Python 前端语义错误或 GC 改动引入的堆损坏。复跑成功与 LLDB 下成功这两条证据排除了稳定语义错:问题指向"新链接的可执行文件 → 立即 exec"这个发布边界。

**根因与修复链。** self 后端链接路径原本让 `cc` 直接写最终输出路径。macOS arm64 上,文件内容已就位但装载器/签名状态尚未稳定时被 exec,就得到上述形状。修复是阶梯式逼出来的,每一级都有失败记录:仅原子改名(`mv -f`)不够;改名后补 ad-hoc 签名,失败率降低但仍复现;最终稳定边界是**签名后强制系统校验**——`_finish_self_backend_executable` 的现行序列:临时文件上 `codesign --force -s -`,`/bin/mv -f` 发布,`codesign --verify` 强制装载器一侧观察到最终 Mach-O,再加一道发布屏障。

**架构的反身性。** 中途曾尝试用 `os.replace()` 实现原子发布——被否决,因为严格自举立即报告 `pcc.frontends.python.pipeline` 出现 no-libpython 回退:`pipeline.py` 自己要被 pcc1 编译,它能用的惯用法受自己守护的闸门约束,最终实现只好走已被支持的子进程边界(`/bin/mv`)。修复手段被被修复物的架构选中,这是自举系统特有的闭环。

**诚实的结尾。** 调查的 2026-05-15 更新记录:加上 `--verify` 之后仍复现过一次 stage3 崩溃,崩溃报告指向 `py_decref` 而非装载器——发布边界修复仍然有用,但"stage3 崩溃类"未被证明关闭,后续移交另一份调查。案例研究的价值一半在修复,另一半在不把"症状消失"写成"根因关闭"。

### 2.6.3 两行入口的四个回退:CLI 也是被编译的对象

(来源:[docs/investigations/python-pcc-main-static-export-cli-bootstrap.md](../../docs/investigations/python-pcc-main-static-export-cli-bootstrap.md),2026-05-28,已解决)

[pcc/__main__.py](../../pcc/__main__.py) 只有两行:导入 `bootstrap_cli_sys_argv_exit`,调用之。它的独立编译却发射了 4 个 `py_cpy_*` 调用——`ensure_init`、`import`、`getattr`、`call_noargs`,一条完整的"经 CPython 把函数 import 进来再调用"的回退链。根因平淡得有教育意义:`pcc.driver.cli_bootstrap` 在静态原生模块的**消费者白名单**里,却没有对应的**导出表**条目,符号绑不上;`pcc.__main__` 自己则两张表都没登记。修复是给 `layer1_support.py` 加一条 `bootstrap_cli_sys_argv_exit` 的函数导出、登记 `pcc.__main__`,基线里该模块的回退数 4 → 0,并被 [tests/fallback_baseline.json](../../tests/fallback_baseline.json) 锁死。教训有二:其一,在 no-libpython 架构里,**入口脚本不是配置,是编译目标**,两行代码同样要过闭包审计;其二,回退棘轮的价值正在于让"表观上不可能有问题的文件"无处遁形——4 个回退若不被按模块计数,就会永远躲在链接成功的二进制里。

## 2.7 小结

pcc 的体系结构可以压缩成四句话。**两条流水线共享一套外壳:**CLI、项目收集、后端注册表、缓存与诊断层是公共的,C 与 Python 在 `execute_cli` 的路径后缀分流处分道扬镳。**C 路径以执行根的多样性为特征:**同一份 TU artifact 可以进程内 MCJIT 直调、隔离进子进程、由系统 cc 链接成真实程序,或交给 self 后端——模式之间的差异(合并 vs 分离、JIT vs 链接)不是实现细节,而是语义边界,bug 会在边界两侧呈现不同形状。**Python 路径以闭包与回退判定为特征:**从模块闭包收集、双探针回退检测、三态 `--python-libpython` 收口,到运行时档案矩阵与发布仪式,每一段都在回答同一个问题——这个二进制到底依赖什么。**自举把 CLI 本身拖进了编译目标:**手写参数解析器、显式上下文管理类、子进程化的宿主查询、被审计规则改写的属性访问,这些"不优雅"都是同一条约束的代价:编译器的每一行驱动代码,终将被它自己编译。后续各章将沿着这两张流水线图逐段放大。

## 练习

1. **(读源码验证)** 跟踪 `pcc hello.c -- a b` 的完整调用链:从 `cli_core.cli_main` 到 `CEvaluator.evaluate()` 里 `fptr(argc, argv)` 的那一行。`prog_args` 在哪一层从 Python 列表变成 C 的 `argv` 数组?为什么带 `prog_args` 的调用不会进入 `_jit_cache`(读 `evaluate()` 的快路径条件并给出语义理由)?
2. **(读源码验证)** `project.py::_scan_make_goal` 把三种干跑尝试排成 `-n`、`-n clean`、`-nB` 的顺序,注释给出了 `-nB` 殿后的理由。构造一个最小 Makefile,使得 `-n` 干跑不产出任何编译命令而 `-n clean` 产出(提示:目标已是最新);再说明对这个工程 `-nB` 会多付出什么。
3. **(设计权衡)** 列出两类只在合并模式暴露、分离模式正常的 bug,与两类只在分离模式暴露、合并模式被掩盖的 bug(提示:`static` 链接性;`// --- 文件名 ---` 拼接对行号诊断的影响;`_raise_if_duplicate_external_definitions` 只在哪条路径上执行)。由此论证:为什么"merged 跑通"不能作为"该项目被支持"的声明。
4. **(实验)** 写一个会触发 libpython 回退的小程序(例如使用一个未支持的动态习语),分别以 `--python-libpython=off`、`auto` 加 `--explain-fallback` 编译。对照 `_finalize_libpython_mode` 的源码,解释错误信息里 reasons 列表的两个可能来源各自由哪个探针产生;再用 `--emit-llvm` 验证 `_ir_needs_libpython` 所扫描的 `py_cpy_*` 调用点确实存在。
5. **(设计权衡论证)** 假设有人提议:pcc1 直接 `import pcc.backend.self_backend_dispatch` 在进程内发射汇编,省掉子进程开销。写出这个改动会**最先**击穿哪条闸门(给出具体测试文件名),并论证为什么"把 `pcc.backend.*` 纳入自托管子集"是正路而"扩大进程内回退"不是——你的论证应当引用第 1 章义务 4 与本章 2.4.5 的证据法(`PCC_HOST_PYTHON=/bin/false`)。
