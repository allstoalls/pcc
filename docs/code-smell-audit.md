# pcc 代码异味深度审计报告

**范围:** `pcc/` 目录 (861 个 Python 文件 / 132 个 C 文件)
**方法:** AST 静态扫描 + 正则模式匹配 (启发式, 需人工确认)
**日期:** 2026

## 执行摘要

本次审计覆盖 14+ 个异味维度。核心结论:

| 优先级 | 类别 | 数量 | 主要风险 |
|---|---|---|---|
| **P0** | 巨型函数 (CC≥25) | 644 个 | 正确性、可测试性、回归风险 |
| **P0** | 超大文件 (>800 行) | 154 个 | 导航成本、合并冲突 |
| **P0** | 魔数 (未具名常量) | 1338 处 Python + 460 处 C | 正确性、可审查性 |
| **P0** | 超长 if/elif 链 (≥12 分支) | 356 处 | 分支覆盖困难、漏改 |
| **P1** | broad except (吞异常) | 506 处 | 掩盖 bug、诊断困难 |
| **P1** | God 类 (方法≥25) | 32 个 | 职责爆炸、耦合 |
| **P1** | 超长参数列表 (≥10) | 101 个 | 调用点脆弱、重构阻力 |
| **P1** | 空异常捕获 (except X: pass) | 186 处 | 静默失败 |
| **P2** | 缺失类型注解 | 1226 个公共函数 | 重构安全性、文档性 |
| **P2** | 局部变量爆炸 (≥25) | 323 个 | 可读性、状态追踪困难 |
| **P2** | 注释掉的代码 | 178 处 | 误导、过期逻辑 |
| **P2** | print/debug 残留 | 54 处 | 输出污染 |
| **P2** | 跨文件重复代码 | 14 组 | 维护不同步 |
| **信息** | 循环导入 | **0 个** | 架构健康 |

---

## 1. 巨型函数 (P0)

### 1.1 按行数 (Top 10)
| 行数 | 文件 | 函数 |
|---|---|---|
| 2087 | `py_frontend/codegen/method_call_expression_lowering.py` | `_emit_method_call` |
| 1811 | `py_frontend/codegen/call_expression_lowering.py` | `_emit_call` |
| 1798 | `py_frontend/codegen/hoist_lowering.py` | `_hoist_nested_funcdefs` |
| 1745 | `native_ir/instcombine.py` | `_rewrite_function` |
| 1652 | `py_runtime/py/freestanding_allocator.py` | (文件整体, 用户修改中) |
| 1046 | `py_runtime/src/py_re_engine.c` | `re_parse_atom` (C) |
| 664 | `py_runtime/src/py_re_engine.c` | `re_parse_rep` (C) |

### 1.2 按圈复杂度 (CC≥25, Top 15)
| CC | 文件 | 函数 |
|---|---|---|
| 478 | `native_ir/instcombine.py:325` | `_rewrite_function` |
| 431 | `py_frontend/codegen/method_call_expression_lowering.py:220` | `_emit_method_call` |
| 407 | `py_frontend/codegen/call_expression_lowering.py:780` | `_emit_call` |
| 346 | `py_frontend/codegen/hoist_lowering.py:200` | `_hoist_nested_funcdefs` |
| 305 | `cli_bootstrap_array_core.py:3489` | `_run_native_package_array_core_impl` |
| 280 | `cli_bootstrap.py:2781` | `_native_numpy_capi_failure_mode` |
| 272 | `backend/arm64_encode.py:1128` | `append_emitted_instruction_record` |
| 261 | `backend/self_backend_x86_64_linux.py:1798` | `_emit_compute_instruction` |
| 260 | `native_ir/simplifycfg.py:699` | `_rewrite_simple_conditional_blocks` |
| 217 | `py_frontend/type_infer.py:1339` | `_infer_expr` |
| 217 | `py_frontend/codegen/assignment_statement_lowering.py:375` | `_emit_assign` |
| 212 | `py_frontend/pipeline_context.py:69` | `build_closed_world_context` |
| 201 | `cli_bootstrap.py:2377` | `_native_numpy_capi_slot` |
| 201 | `py_frontend/codegen/hoist_lowering.py:551` | `rewrite_body` |
| 187 | `codegen/c_declaration_lowering.py:12` | `codegen_Decl` |

**总量:** 644 个函数 CC≥25。**解读:** CC>10 即难以单元测试全覆盖; CC>25 基本无法可靠推理。这些是回归缺陷的高发区。

### 1.3 控制流密度
- 411 个函数 return≥12 或 break/continue≥8
- 最高: `_native_numpy_capi_slot` return=201, `_emit_call` return=148, `_emit_method_call` return=142

### 1.4 局部变量爆炸
- 323 个函数局部变量≥25 个
- 最高: `_emit_call` 170 个, `link_prepared_executable` 160 个, `_hoist_nested_funcdefs` 148 个

**修复建议:** 对 `_emit_call` / `_emit_method_call` 按"调用类型"拆分为独立 lowering 函数 (按 method/normal/keyword/positional 路由); 对 `_rewrite_function` 按 instruction opcode 族拆分 pass。

---

## 2. 超大文件 (P0)

| 行数 | 文件 |
|---|---|
| 11766 | `pcc/cli_bootstrap.py` |
| 7414 | `pcc/codegen/c_codegen.py` |
| 7151 | `pcc/py_frontend/codegen/class_gen.py` |
| 6331 | `pcc/py_frontend/type_infer.py` |
| 6288 | `pcc/py_frontend/codegen/unsafe_lowering.py` |
| 6129 | `pcc/backend/self_backend_precise_stackmaps.py` |
| 5361 | `pcc/py_runtime/py/py_gc_backend.py` |
| 4880 | `pcc/backend/self_backend_parse.py` |
| 4648 | `pcc/cli_bootstrap_array_core.py` |
| 4370 | `pcc/capi_surface.py` |

**总量:** 154 个文件 >800 行。`cli_bootstrap.py` 同时是 broad except (506 处中占 ~40 处) 和超长 if/elif 链 (40 处) 的重灾区。

---

## 3. 魔数 (P0)

### 3.1 Python 大整数字面量 (绝对值>1000, 排除常见 2 的幂)
| 出现 | 值 | 含义推测 |
|---|---|---|
| x164 | 4294967295 | UINT32_MAX / 掩码 |
| x56 | 9223372036854775807 | INT64_MAX |
| x44 | 4095 | 页掩码 |
| x42 | 2147483647 | INT32_MAX |
| x42 | 4294967296 | UINT32_MAX+1 |
| x37 | 1048576 | 1MB |
| x12 | 16392 | **GC header 偏移家族** |
| x8 | 18446744073709551615 | UINT64_MAX |

### 3.2 关键目标文件: `freestanding_allocator.py`
- 生命周期状态魔数 `5783538902897647427/8/9` 出现 **13 次** (FREE/UNINIT/LIVE/RESERVED)
- header 偏移 `-48/-40/-32/-24/-16/-8` 硬编码
- **建议:** 提取 `GC_STATE_*` 具名常量 + `_HEADER_*_OFFSET` 常量, 全局替换 13 处

### 3.3 C 代码魔数
- 460 处, 涉及 43 个文件
- 最密集: `py_str_accessors.c` (42), `py_obj_stubs.c` (24), `py_gc_backend.c` (23), `py_json.c` (22)
- 多数为 ABI 偏移/标志位, 应迁移到 `py_abi_constants.py` 统一来源

---

## 4. 超长 if/elif 链 (P0)

**总量:** 356 处 ≥12 分支

| 文件 | 处数 | 解读 |
|---|---|---|
| `cli_bootstrap_array_core.py` | 182 | **极严重** — 可能是生成的分发/类型路由表 |
| `cli_bootstrap.py` | 40 | CLI 参数/模式路由 |
| `backend/self_backend_analysis.py` | 27 | opcode 分析 |
| `py_runtime/py/freestanding_gc_object_slots.py` | 21 | GC slot 分发 |
| `py_runtime/py/freestanding_gc_tracing_sweep_collector.py` | 18 | GC trace 分发 |

**建议:** 先确认 `cli_bootstrap_array_core.py` 是否生成代码。若是手写, 应改为查表/注册表分发 (dict dispatch)。GC 的 slot 分发适合改为基于 type tag 的函数指针表。

---

## 5. 异常处理异味 (P1)

### 5.1 broad except
- **506 处** `except Exception` / `except BaseException` / bare `except`
- 重灾区: `cli_bootstrap.py` (~40 处), 其次分散于 bootstrap/runtime
- **风险:** 掩盖真正的错误 (SyntaxError, KeyboardInterrupt, SystemExit 被吞), 使"编译成功但行为错误"类 bug 极难诊断

### 5.2 空异常捕获
- **186 处** `except X: pass`
- 多数为 `except Exception: pass`
- **风险:** 静默失败, 条件编译/能力探测场景可接受, 但需逐处确认意图

### 5.3 bare raise
- 74 处无参数 raise (部分为合法的 re-raise, 需区分)

---

## 6. God 类 (P1)

**总量:** 32 个类 方法≥25 或 属性≥20

| 方法数 | 属性数 | 类 | 文件 |
|---|---|---|---|
| 280 | 5 | `LLVMCodeGenerator` | `codegen/c_codegen.py` |
| 164 | 1 | `CParser` | `parse/c_parser.py` |
| 160 | 1 | `CParserActions` | `parse/c_parser_actions.py` |
| 136 | 1 | `IndexedFunctionKernel` | `backend/self_backend_kernel.py` |
| 113 | 1 | `ClassLowering` | `py_frontend/codegen/class_gen.py` |
| 94 | 3 | `SSABuilder` | `ssa/builder.py` |
| 80 | 0 | `NativeModuleAliasMixin` | `py_frontend/codegen/native_modules.py` |
| 78 | 1 | `IRBuilder` | `llvm_capi/ir.py` |
| 73 | 0 | `Parser` | `parse/py_parse.py` |
| 69 | 0 | `_Lifter` | `py_frontend/parser.py` |

**解读:** `LLVMCodeGenerator` (280 方法) 是架构级坏味道 — 它承载了 LLVM 后端的全部 lowering, 应与 owned self 后端对齐拆分。注意其中部分为 parser generator 生成 (CParser/CParserActions 来自 pycparser), 可排除。

---

## 7. 超长参数列表 (P1)

**总量:** 101 个函数 ≥10 参数

| 参数 | 函数 | 文件 |
|---|---|---|
| 70 | `_native_array_core_json` | `cli_bootstrap_array_core.py` |
| 64 | `array_core_report` | `package/array_core.py` |
| 43 | `build_shared_exports` | `py_frontend/pipeline_frontend_parallel.py` |
| 33 | `compile_parallel_uncached` | `py_frontend/pipeline_frontend_parallel.py` |
| 31 | `execute_cli` | `cli_core.py` |
| 31 | `link_ir_texts_run` | `py_frontend/pipeline_self_backend_link.py` |
| 30 | `_click_entry` | `pcc.py` |

**建议:** 引入参数对象 (dataclass: `CompileRequest`, `LinkConfig`, `ArrayCoreSpec`)。CLI 入口函数参数多属正常, 但内部 pipeline 函数应收敛。

---

## 8. 类型注解缺失 (P2)

- **1226 个** 公共函数无返回类型注解
- 最严重: `compile_dir` (14 参数无注解), `yacc.yacc`, `IRBuilder_call8` 等
- **影响:** 重构安全性、IDE 导航、mypy 类检查无法生效。建议在新改代码中强制执行, 对热点 lowering 函数优先补全。

---

## 9. 注释掉的代码 (P2)

- **178 处** Python 注释块含代码特征 (`def ` / `class ` / `import ` / `if ` 等)
- 分布: 大量在 `ply/` (第三方, 可忽略), 其余分散
- **建议:** 确认无意图后删除; git 已保留历史, 无需"留作参考"

---

## 10. 重复代码 (P2)

### 10.1 跨文件完全重复函数体 (14 组)
| 重复 | 函数 | 文件 |
|---|---|---|
| x6 | `_split_functions` | gvn.py / early_cse.py / adce.py / newgvn.py / instcombine.py / ssa_dse.py |
| x4 | `_collect_function_types` | ssa_dse.py / ssa_adce.py / ssa_gvn_rewrite.py / ssa_sccp_rewrite.py |
| x3 | `_utf8_next_u4` | py_capi_unicode_*_runtime.py (3 个) |
| x2 | `_iter_py_sources_under` | cli_bootstrap.py / cli_shared_paths.py |
| x2 | `tag_breaks` | control_flow_lowering.py / for_loop_lowering.py |
| x2 | `_cstr_is_dunder_class/name` | py_obj_ops_dispatch.py / py_class.py |
| x2 | `_utf8_byte_offset_for_codepoint` 等 | py_str_slice.py / py_str_accessors.py |

**建议:** `_split_functions` / `_collect_function_types` 应下沉到 `native_ir/` 公共模块; Unicode 辅助函数应收敛到单一 runtime 源。

### 10.2 单文件内重复函数名 (465 组)
多数为 AST visitor 模板生成 (`children_*`, `__init__`, `run`, `dump`) 或数据类, 属正常模式, **非异味**。

---

## 11. C 代码异味

### 11.1 超长函数 (≥80 行)
- **73 个**, 最大 `re_parse_atom` (1046 行, 正则解析器, 可接受)
- GC 相关: `pcc_gc_note_object_allocated_sized` (373), `pcc_gc_telemetry` (370), `pcc_gc_visit_object_slots_slice` (261)

### 11.2 参数过多
- 仅 2 个 ≥8 参数 (`pcc_re_engine_run_flags` 9, `pcc_re_engine_run_from` 8) — **良好**

### 11.3 goto 使用
| goto 数 | 文件 |
|---|---|
| 48 | `py_format.c` |
| 20 | `py_gc_backend.c` |
| 18 | `py_json.c` |
| 15 | `py_list.c` |

**解读:** C 的 goto 用于错误清理是惯用模式 (CPython 风格), 但 `py_format.c` 48 处值得审查是否可结构化。

### 11.4 空函数 (3 个)
`py_threading.c`: `pcc_vthread_waiter_pool_lock_acquire/release`, `py_gc_index_table.c`: `pcc_gc_ptr_index_tls_pool_drain` — 可能是桩, 需确认是否应实现或删除。

### 11.5 循环导入
- **0 个双向导入循环** — 架构健康, 应保持

---

## 12. 其他发现

| 类别 | 数量 | 备注 |
|---|---|---|
| print/debug 残留 | 54 处 | `artifact_inspect.py`, `owned_literal_driver.py`, `package/*` 等 |
| 可变默认参数 | 2 处 | `g_generator.py:_generate_type`, `ply/cpp.py:parse` |
| 布尔标志参数≥3 | 0 个 | 良好 |
| 超长行 (>120) | 75 文件 / 952 行 | `c_parsetab.py` (生成, 559 行) 占大半 |
| 模块级全局变量≥4 | 395 个 | 多数为常量表/ABI 偏移, 合理; 需区分常量与可变状态 |
| camelCase 函数 | 79 个 | 集中在 `py_stdlib/` (stdlib 镜像, 需保持与 CPython 同名, 合理) |

---

## 修复优先级建议

### 阶段 1 (P0, 立即): 魔数具名化 — **前置条件未满足, 已核实**

1. `freestanding_allocator.py`: 提取 `GC_STATE_FREE/LIVE/RESERVED` + `GRANULE_*_OFFSET`,
   替换 13 处魔数 — **当前语言层做不到**。freestanding 模块不允许任何模块作用域可执行
   语句, 所以模块级常量赋值直接被前端拒绝:

   ```
   error: PCC-PY-COMPILE-001: [python-frontend] freestanding modules do not
   support executable module-scope statements: Assign
   ```

   (`codegen/generation_lowering.py:693`。实测过一次: 加上
   `GC_STATE_LIVE: i64 = 5783538902897647428` 等 9 个常量后该模块无法编译。)

   现有的两条模块作用域豁免都不适用: `extern(...)` 是符号声明, `define_global_i64(...)`
   造的是**可变全局**, 读它要走一次内存加载 —— 而这些魔数全部位于分配器最热的
   provenance 判定路径上, 换成加载会直接吃掉 60096706 那一轮拿到的 +5.62% 配对吞吐。

   **解锁它需要的改动**: 前端支持 freestanding 模块作用域的**编译期整型常量**
   (`NAME: i64 = <整型字面量>`), 在使用点折叠为字面量, 不产生任何全局符号。判据很干净:
   改造后重新 emit 的模块 IR 应与 `build_py/freestanding_allocator.ll` **逐字节相同**,
   相同即证明纯属重命名, 无需重新测量。这项改动对全部 60+ 个 freestanding 运行时模块
   同样有效 —— 它们的魔数都源于同一个限制。

   在此之前, 三个生命周期字的含义只由 `pcc_gc_granule_object_publish` /
   `pcc_gc_granule_object_retire` 的 docstring 承载 (FREE / LIVE / RESERVED, 共享
   `PCA\xfd[\xdd\xdf` 前缀, 仅末字节 C/D/E 不同)。

2. 全局审查 `16392`、`4095`、`4294967295` 等高频魔数, 能具名则具名 — 非 freestanding
   模块不受上述限制, 可以直接做

### 阶段 2 (P0, 高价值): 巨型函数拆分
1. `_emit_call` (1811 行 / CC407) — 按调用形态拆分为 5-8 个路由函数
2. `_emit_method_call` (2087 行 / CC431) — 同上
3. `_rewrite_function` (1745 行 / CC478) — 按 opcode 族拆分

### 阶段 3 (P0/P1): 分支爆炸治理
1. 确认 `cli_bootstrap_array_core.py` 182 处长链是否生成代码; 若手写, 改为查表分发
2. `cli_bootstrap.py` broad except 逐处收窄或记录理由

### 阶段 4 (P1): 架构级
1. `LLVMCodeGenerator` (280 方法) 按 lowering 阶段拆分 mixin
2. 超长参数列表引入参数对象

### 阶段 5 (P2, 持续): 卫生
1. 删除 178 处注释代码
2. 清理 54 处 print 残留
3. 补全热点函数类型注解
4. 抽取 14 组跨文件重复代码

---

## 方法论说明

- 圈复杂度 = 1 + if/for/while/except/with/boolop/三元/推导式 计数 (标准 McCabe 近似)
- "可能未使用函数" 为调用频次启发式, 含动态调用/导出 API, **必须人工确认**
- 重复代码检测基于 AST 完全匹配, 仅捕获"复制粘贴"级重复, 不捕获结构相似
- C 函数边界用括号匹配近似, 对含宏/复杂声明的文件可能漏检
- 所有数量均为**当前工作树快照**, 随代码演进需重扫

## 后续

建议将本审计纳入 CI 门禁 (函数长度/圈复杂度阈值), 并对 P0 项建立 issue 追踪。重扫脚本可复用本次审计的扫描器。
