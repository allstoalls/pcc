# 附录 A 仓库地图

本附录是全书源码引用的总索引。按子系统组织;路径相对仓库根。

## 入口与驱动

| 路径 | 角色 |
|---|---|
| [pcc/pcc.py](../../pcc/pcc.py)、[pcc/driver/cli_core.py](../../pcc/driver/cli_core.py) | CLI 入口 |
| [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) | 自举各阶段(`pcc1`/`pcc2`/`pcc3`)使用的 bootstrap CLI |
| [pcc/api.py](../../pcc/api.py) | C 路径的 `build(...)` / `module(...)` Python API |
| [pcc/driver/project.py](../../pcc/driver/project.py) | 目录源收集、`--sources-from-make`、TU 选择 |
| [scripts/bootstrap.sh](../../scripts/bootstrap.sh) | macOS arm64 三阶段自举入口 |
| [scripts/pcc_multi.py](../../scripts/pcc_multi.py) | 实验性多文件 Python 入口 |

## C 前端(第 3–4 章)

| 路径 | 角色 |
|---|---|
| [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py) | C 解析器(PLY;改语法/词法须升缓存版本号) |
| [pcc/frontends/c/preprocessor.py](../../pcc/frontends/c/preprocessor.py) | 预处理 |
| [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) | C 预处理/解析/IR/优化/执行流水线 |
| [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) | C 语义低层化主体(符号性不变式所在) |
| [utils/fake_libc_include/](../../utils/fake_libc_include) | 伪 libc 头(host ABI 失配在此暴露) |

## Python 前端(第 5–6 章)

| 路径 | 角色 |
|---|---|
| [pcc/frontends/python/py_parse.py](../../pcc/frontends/python/py_parse.py)、[pcc/frontends/python/py_lift.py](../../pcc/frontends/python/py_lift.py) | Python 解析与提升 |
| [pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py)、`pipeline.py`、`type_infer.py` | AST、流水线、类型推断 |
| [pcc/frontends/python/codegen/layer1.py](../../pcc/frontends/python/codegen/layer1.py) | Layer-1 低层化 facade |
| `pcc/frontends/python/codegen/*_lowering.py` | 低层化 mixin 群(行为主体) |
| `pcc/frontends/python/codegen/native_*.py` | 原生模块低层化(gc、threading、asyncio、os、math…) |
| [pcc/diagnostics/fallback_routes.py](../../pcc/diagnostics/fallback_routes.py)、[pcc/diagnostics/fallback_explainer.py](../../pcc/diagnostics/fallback_explainer.py) | 回退路由与解释 |

## 运行时(第 7–11、14 章)

| 路径 | 角色 |
|---|---|
| [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) | 公共头:对象头、类型标签、`PCC_GC_KIND_*` |
| [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) | 运行时内部布局(如 `PyClassObject`) |
| `pcc/runtime/src/*.c` | 过渡/host-C 实现与差分 oracle;最终生产 pcc-Python 归档不以其为成员来源 |
| [pcc/runtime/Makefile](../../pcc/runtime/Makefile) | `PY_MODULES`、`FREESTANDING_PY_MODULES`、provenance 与生产归档组装 |
| `pcc/runtime/py/py_*.py` | semantic pcc-Python:对象、容器、异常、C-API 等运行时语义 |
| `pcc/runtime/py/freestanding_*.py` | freestanding pcc-Python:分配器、线程、平台、libc-like substrate 与五 GC 政策 |
| [pcc/runtime/py/freestanding_gc_object_slots.py](../../pcc/runtime/py/freestanding_gc_object_slots.py) | 生产对象槽统一访问契约 |
| [pcc/extern/](../../pcc/extern)、[pcc/unsafe/](../../pcc/unsafe) | Python→C extern 声明;编译器识别的内建 |
| [docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research) | 五 GC 的参照实现(Lua、Go、OCaml、ZGC、CPython) |

## 后端(第 12–13 章)

| 路径 | 角色 |
|---|---|
| [pcc/ir/](../../pcc/ir) | 仓库内 LLVM-C builder(llvmlite 为回退与 oracle) |
| [pcc/backend/](../../pcc/backend) | self 后端(AArch64 Darwin、x86_64 Linux 子集) |

## 自举与基线(第 15 章)

| 路径 | 角色 |
|---|---|
| [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) | **自举状态权威基线** |
| [tests/fallback_baseline.json](../../tests/fallback_baseline.json) | **no-libpython 回退棘轮权威基线** |
| `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` | 五 GC 全自举闸(stage1→2→3) |
| [tests/python/test_self_host_oracle_diff.py](../../tests/python/test_self_host_oracle_diff.py) | Python 语义 oracle / pcc1-pcc2 对齐棘轮 |

## 包与扩展(第 17 章)

| 路径 | 角色 |
|---|---|
| [pcc/package/](../../pcc/package)、[pcc/frontends/c/capi_abi.py](../../pcc/frontends/c/capi_abi.py)、[pcc/frontends/c/capi_surface.py](../../pcc/frontends/c/capi_surface.py) | 包路径与 C-API 面 |
| `pcc/runtime/py/py_capi_*_runtime.py`、`py_extension_loader_runtime.py` | 生产 pcc-Python C-API ABI 与扩展装载 owners |
| [pcc/runtime/src/py_capi_shim.c](../../pcc/runtime/src/py_capi_shim.c)、`py_extension_loader.c` | host-C oracle/过渡实现,不是生产 pcc-Python 归档 owner |

## GUI 与应用执行(第 20 章)

GUI 框架、v1 契约、`mac_diff_app` canary、Metal/AppKit 桥接及其测试已于 2026-09-06
迁至 [allstoalls/pcc-gui](https://github.com/allstoalls/pcc-gui)。核心提交 `977ad074`
是最后一个包含它们的版本。

## 方法论文档(第 18 章)

| 路径 | 角色 |
|---|---|
| [AGENTS.md](../../AGENTS.md) | 仓库规则与北极星(Project Intent) |
| [docs/goal/goal-prompt.md](../../docs/goal/goal-prompt.md) | 目标契约与工作协议(§0.10 声明卫生表) |
| [docs/goal/task-board.yaml](../../docs/goal/task-board.yaml) | 结构化任务执行队列(`scripts/goal_state.py next` 选任务) |
| [docs/current-goal-state.md](../../docs/current-goal-state.md) | 当前目标审计与路由 |
| [docs/debugging-playbook.md](../../docs/debugging-playbook.md) | 调试手册(12 技法) |
| [docs/investigation-workflow.md](../../docs/investigation-workflow.md) | 调查工作流(三模式与模板) |
| [docs/investigations/INDEX.md](../../docs/investigations/INDEX.md) | 两百余篇调查的索引 |
| [tests/](../../tests) | 单元、对齐、集成回归 |
| [projects/lua-5.5.0/](../../projects/lua-5.5.0) | 真实程序压力目标 |
