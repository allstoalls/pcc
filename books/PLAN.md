# 《pcc 的设计与实现》/ The Design and Implementation of pcc — 全书蓝图

十一部二十章 + 前言 + 附录。每章蓝图含:主题边界、必读素材、必须回答的设计问题、
必讲的案例研究来源。写作 agent 不得越出本章边界(相邻章节会覆盖)。

跨章引用写法:「见第 N 章」/ "see Chapter N",不要复述他章内容。

---

## 第 I 部 总览 (Part I: Overview)

### 第 1 章 导论:拥有 Python 的执行 (Introduction: Owning Python Execution)
文件:`ch01-introduction.md`
- 边界:pcc 的论题(thesis)——native / auditable / self-hostable / no-libpython;
  五大差异化(自举不动点、五 GC、值模型、self 后端、长跑效率);七义务;
  "一个使命,不是两个"(工业↔学术);"性能是已证语义的后果";
  运行时四层(compiler intrinsics / freestanding pcc-Python / semantic pcc-Python /
  C/libc oracle)总览;区分 no-libpython、Python-owned runtime、Linux zero-libc 与
  Darwin named-libSystem boundary。
- 素材:`AGENTS.md`(Project Intent 全文)、`README.md`、`codex-goal-prompt.md` 开头部分、
  `docs/current-goal-state.md` 顶部审计快照。
- 设计问题:为什么"加速器"不是目标?为什么 honesty(mode-labeled claims)是架构的一部分?
  与 PyPy/Cython/Nuitka/mypyc 的定位差异(基于事实,不贬低)。
- 案例研究:claim hygiene 的来源——为什么仓库规则禁止 `if package == "numpy"`。

### 第 2 章 体系结构总览 (Architecture Overview)
文件:`ch02-architecture.md`
- 边界:两个编译器一个运行时;从源文件到二进制的完整流水线(两条:C 路径、Python 路径);
  仓库地图;编译模式(single-file / merged / --separate-tus / --sources-from-make);
  CLI 层(`pcc/pcc.py`、`cli_core.py`、`cli_bootstrap.py`)、`api.py`、`project.py`;
  三后端选择(llvm / llvm_capi / self)与关键旗标(`--python-libpython`、`--ir-scaffold`)。
- 素材:`pcc/pcc.py`、`pcc/driver/cli_core.py`、`pcc/api.py`、`pcc/driver/project.py`、
  `pcc/frontends/c/evaluator/c_evaluator.py`(只看流水线骨架)、`pcc/frontends/python/pipeline.py`(同)、
  `README.md`、`AGENTS.md` Repository Map / Compile Modes。
- 设计问题:为什么 merged TU 是目录默认?为什么 host 查询走 subprocess 而非 in-process?

## 第 II 部 C 前端 (Part II: The C Frontend)

### 第 3 章 C 前端:解析、伪 libc 与求值器 (The C Frontend: Parsing, fake-libc, and the Evaluator)
文件:`ch03-c-frontend.md`
- 边界:`pcc/frontends/c/parse/c_parser.py`(PLY、解析器缓存与版本号)、`pcc/frontends/c/preprocessor.py`、
  `utils/fake_libc_include/` 的设计(为什么伪造 libc 头;host ABI 失配在哪暴露)、
  `pcc/frontends/c/evaluator/c_evaluator.py` 流水线(preprocess/parse/IR/optimize/execute)、
  `pcc/driver/project.py` 源收集与 `--sources-from-make` 的原理及限制。
- 素材:上列文件 + `AGENTS.md` Common Pitfalls 里 stale parser cache、目录探针误编译。
- 案例研究:从 `docs/investigations/INDEX.md` 选 C 侧条目(struct/union tag 重用、
  static/incomplete array、casted function-pointer globals 类)。

### 第 4 章 C 语义降低与符号性 (C Semantic Lowering and Signedness)
文件:`ch04-c-lowering-signedness.md`
- 边界:`pcc/frontends/c/codegen/c_codegen.py` 的组织;核心不变式——int 与 unsigned 同为 i32、
  符号性单独跟踪(`_tag_unsigned`/`_clear_unsigned`/`_is_unsigned_val`/
  `_convert_int_value`/`_usual_arithmetic_conversion`/`_shift_operand_conversion`);
  usual arithmetic conversions 与 C 标准的对应;经典失败模式(位形对、签名丢 →
  sdiv/srem/ashr/signed compare);常量折叠是语义子系统;数据布局 bug vs 表达式语义 bug。
- 素材:`pcc/frontends/c/codegen/c_codegen.py`(读上述 helper 与调用点)、`AGENTS.md` 符号性一节、
  `docs/debugging-playbook.md` §10/§12、`tests/c/test_unsigned_loads.py`。
- 案例研究:Lua/libc-heavy 程序暴露符号性丢失的真实案例(查 INDEX)。

## 第 III 部 Python 前端 (Part III: The Python Frontend)

### 第 5 章 类型化 Python 前端 (The Typed-Python Frontend)
文件:`ch05-typed-python-frontend.md`
- 边界:`pcc/frontends/python/py_parse.py`/`py_lift.py` → `pcc/frontends/python/py_ast.py` →
  `pipeline.py` → `type_infer.py`;类型推断对原生降低的角色;严格模式哲学
  (不支持的习语默认 fail loudly,而非静默回退);`--ir-scaffold` 三态的含义。
- 素材:上列文件、`README.md` Python 状态表、`docs/python-limitations.md`(若在)。
- 设计问题:为什么是"typed subset 编译器"而不是"全 Python JIT"?类型推断失败时
  错误如何分级(hard error vs fallback route,联系 `fallback_routes.py`/`fallback_explainer.py`)。

### 第 6 章 Python 降低:facade 与 mixin 群 (Python Lowering: the Facade and the Mixins)
文件:`ch06-python-lowering.md`
- 边界:layer1.py 从巨石到 56 行 facade 的拆分史与拆分原则;`*_lowering.py` mixin 架构
  (挑 4–6 个代表深讲:exception_lowering、ownership 相关、subscript/exact_int 双路径、
  for_loop/comprehension、format/fstring);`native_*.py` 原生模块降低;
  生成代码必须在可 raise 调用后插 `py_err_occurred()` 检查的降低义务。
- 素材:`pcc/frontends/python/codegen/` 目录、`AGENTS.md` layer1 split 段。
- 案例研究:双下标路径(subscript_lowering vs exact_int_lowering 改一半 = 半失效)、
  六条除法降低路径——同一语义散布多路径的维护代价(查 INDEX 对应调查)。

## 第 IV 部 运行时 (Part IV: The Runtime)

### 第 7 章 对象模型 (The Object Model)
文件:`ch07-object-model.md`
- 边界:`PyObjectHeader`(refcount@0、type_tag@8、flags:PY_FLAG_FINALIZED/GC_TRACKED/
  IMMORTAL,线程下 `__atomic_*`);`enum PyTypeTag`;`PyClassObject` 120 字节布局
  (del_method@96、attrs@104、metaclass@112);C 与 pcc-Python 镜像(`py_internal.h` vs
  `py_runtime/py/py_class.py`)必须逐字节一致的纪律;实例、attr 存取、metaclass;
  "object has no attribute X 但类明明定义了 X"的三因检查顺序。
- 素材:`pcc/runtime/include/py_runtime.h`、`src/py_internal.h`、`src/py_class.c`、
  `src/py_obj.c`、`py/py_class.py`、`AGENTS.md` 对象头/布局节。
- 案例研究:布局漂移类 bug(INDEX 检索 layout/class);dataclass default-None setattr
  clobber 邻槽问题。

### 第 8 章 异常模型 (The Exception Model)
文件:`ch08-exception-model.md`
- 边界:`py_raise()` 存 TLS 后正常返回;无 Itanium 展开——为什么选 checked-call 而非
  unwinding(代价、可移植性、self 后端可实现性);`py_err_occurred()` 检查的插入义务;
  exc 对象/匹配/traceback(`py_exc_objects.c`、`py_exc_match.c`、`py_exc_table.c`、
  `py_exc_tls.c`、`py_exc_traceback.c`);失败模式"compile succeeded with no output"。
- 素材:上列 C 文件 + `py/py_exc_*.py` 镜像、`exception_lowering.py`、
  `docs/investigations/python-self-host-no-libpython-runtime-holes.md`。
- 案例研究:漏 err-check 的 emission-site 审计(INDEX: emission-site-err-check-audit)。

### 第 9 章 引用计数与所有权 (Reference Counting and Ownership)
文件:`ch09-refcount-ownership.md`
- 边界:拥有/借用引用契约——函数调用返回拥有引用;被调方返回借用的
  local/param/global/field/singleton 时必须在被调方 retain(而非让调用方停止 release);
  `pcc_gc_store_ptr()` 的平衡契约(incref 新值 / decref 旧值);owned-local 清理与
  GC root(ownership 相关 lowering);`py_user_del_dispatch()` 与 PY_FLAG_FINALIZED
  防复活再入;为什么"禁止为了过 gate 而弱化所有权/清理"。
- 素材:`pcc/frontends/python/codegen/ownership*_lowering.py`(以实际文件名为准,rg 查)、
  `src/py_obj.c`、`src/py_obj_dealloc.c`、`AGENTS.md` 自举回归纪律第 5 条。
- 案例研究:bootstrap 所有权回归案例(INDEX: ownership / owned-local / return)。

## 第 V 部 垃圾收集:五后端实验室 (Part V: GC — the Five-Backend Laboratory)

### 第 10 章 五 GC 架构与平等契约 (The Five-GC Architecture and the Equality Contract)
文件:`ch10-gc-architecture.md`
- 边界:`PCC_GC_KIND_*` 枚举与 `PCC_GC_BACKEND` 运行时选择;**一套槽位契约**
  (`py_obj_visit_slots`/`py_obj_update_slot`/root+frame+native-handle 注册)供五后端、
  freestanding/semantic pcc-Python 共用——生产平等规则,为什么决不允许第二套对象图规则;
  读写屏障 API(`pcc_gc_load_ptr`/`pcc_gc_store_ptr`)及哪些后端依赖哪侧;
  帧根:槽粒度、非 LIFO,为什么 frame_index 必须是哈希(换链表曾把 gc3 退化到 900s);
  终结器/弱引用/复活/挂起协程帧/调度队列/C 扩展引用——任何后端不得靠弱化这些取胜。
- 素材:`pcc/runtime/src/py_gc_backend.c`、`src/py_obj_gc.c`、`include/py_runtime.h`、
  `py/py_gc_backend.py`、`AGENTS.md` 5-GC equality、
  `docs/investigations/gc-5backend-*`(对象生命周期契约、异常 referent roots 等)。
- 案例研究:exc-referent 根缺失的根因在前端 ownership lowering 而非运行时
  (gc-5backend-exception-referent-roots);frame-root 哈希退化案例。

### 第 11 章 五个后端:从引用计数到重定位 (The Five Backends: from Refcounting to Relocation)
文件:`ch11-gc-backends.md`
- 边界:逐后端一节,各讲:参照系(reference implementation)、核心算法、pcc 移植中的
  关键决定、专属不变式、已知状态——
  #0 refcount+cycle(CPython;默认与回滚参照)
  #1 incremental tricolor(Lua 5.4 lgc.c;pacer、explicit collect sweep)
  #2 concurrent mark-sweep(Go;buffered write barrier、worker 稳定性)
  #3 generational(OCaml;promotion、eager slot rewrite、forwarding)
  #4 colored-relocating(ZGC;colored pointers、load barrier、relocation whitelist)
  以及 selection matrix(为什么 #0 仍是默认)。基线量级(gc0~107s/gc3~226s/gc4~310s
  bootstrap)只作相对比较用,标注测量日期与含义。
- 素材:`src/py_gc_backend.c`、`docs/refs_docs/gc-research/<lang>/`、
  `docs/investigations/gc-backend{1,2,3,4}-*`、`gc-backend-selection-matrix.md`、
  `bootstrap-five-gc-*`。
- 案例研究:每个后端至少一个(从对应 investigation 取)。

## 第 VI 部 后端与链接 (Part VI: Backends and Linking)

### 第 12 章 LLVM 后端与 llvm_capi 对齐 (The LLVM Backends and llvm_capi Parity)
文件:`ch12-llvm-backends.md`
- 边界:llvmlite 路径与 in-repo `pcc/ir/` C-API builder;llvmlite 作为 oracle 的
  parity 测试法(`tests/c/test_llvm_capi_ir_parity.py`);IR Fix Policy——为什么文本级
  重写只剩 va_arg 一个豁免;`postprocess_ir_text()` 与属性剥离
  (nuw/nneg/range()/initializes()/dead_on_unwind);system-link 路径直接发 native object。
- 素材:`pcc/ir/`、`AGENTS.md` IR Fix Policy、上述测试。
- 案例研究:capi/llvmlite 不一致案例(INDEX 检索 llvm_capi / parity)。

### 第 13 章 self 后端:没有 LLVM 的原生发射 (The Self Backend: Native Emission without LLVM)
文件:`ch13-self-backend.md`
- 边界:`pcc/backend/` 全家——IR 解析(self_backend_parse/ir)、分析与准备
  (analysis/prepare/stackprep)、指令/终结子分派、AArch64 Darwin 发射族
  (regs/abi/calls/prologue/addr/mem/materialize/returns/...)、x86_64 Linux 子集、
  目标 pass、Mach-O 产物;"第一类执行根"义务——`--backend=self` 后禁止静默回退 LLVM;
  `_link_with_self_backend` 不得把 `pcc.backend.*` 拉回 stage1 闭包(subprocess 边界)。
- 素材:`pcc/backend/` 文件群、`AGENTS.md` S-track 义务与 subprocess 边界节。
- 案例研究:self 后端 bootstrap 相关调查(INDEX 检索 self-backend / backend)。

## 第 VII 部 自举与 no-libpython (Part VII: Self-Hosting)

### 第 14 章 no-libpython 与 zero-libc:让运行时成为 pcc-Python (No-libpython and Zero-libc: Making the Runtime pcc-Python)
文件:`ch14-no-libpython.md`
- 边界:四层模型(compiler intrinsics KEEP / freestanding pcc-Python GROW /
  semantic pcc-Python GROW / C-libc source REMOVE-from-production);严格区分
  no-libpython、pcc-Python-owned runtime、Linux tracer zero-libc、完整生产 zero-libc;
  Darwin 只允许具名 libSystem ABI,绝不标 zero-libc;`pcc/extern/` 与 `pcc/unsafe/`
  如何让 pcc-Python 写分配器、线程、GC、libc-like substrate 与 ABI shim;
  `PCC_PY_OBJECTS`/provenance 归档构建;fallback 棘轮与 link-map 闭包的正交关系;
  当前有限 DONE_STRONG 切片与 `LIBC-P3-FREESTANDING-RUNTIME-CLOSURE` 开放边界。
- 素材:`AGENTS.md` Runtime layering、`pcc/runtime/Makefile`、
  `pcc/runtime/py/freestanding_{mem_str,allocator,linux_start,gc_index_table,platform_io}.py`、
  `docs/goal/evidence/2026-08-03-{linux-zero-libc-python-start,freestanding-gc-done-strong,freestanding-mem-str-done-strong,freestanding-allocator-done-strong,freestanding-platform-wrappers-done-strong}.md`、
  `tests/fallback_baseline.json`。
- 案例研究:`PCC_RUNTIME_CC=cc` 的 oracle 假信心;`py_capi_shim.o` 改名
  `py_capi_compat.o` 绕过文件名棘轮,最终改成 source-ownership 断言。

### 第 15 章 自举:pcc1→pcc2→pcc3 不动点 (Bootstrap: the pcc1→pcc2→pcc3 Fixed Point)
文件:`ch15-bootstrap-fixed-point.md`
- 边界:阶段命名(pcc0 host → pcc1 → pcc2 → pcc3);`scripts/bootstrap.sh` 与
  `pcc/driver/cli_bootstrap.py`;不动点的内涵——字节同一不只是 diff,是语义/运行时/codegen/
  对象模型/后端/诊断的相干性证据;差异分类学(semantic/IR-text/class-layout/
  object-model/backend nondeterminism/link metadata/perf-only/diagnostic);
  权威基线 `tests/bootstrap_gate_baseline.json` 与五 GC 全自举闸
  (`tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py`);自举回归纪律(七步);
  Thompson "Trusting Trust" 的关联与边界。
- 素材:上列脚本/测试/基线、`AGENTS.md` Bootstrap 全节、README Status 表。
- 案例研究:一次真实 bootstrap 回归的因果审计(INDEX 检索 bootstrap-)。

## 第 VIII 部 值模型与生态 (Part VIII: The Value Model and the Ecosystem)

### 第 16 章 值模型:投影而非定宽 (The Value Model: Projection, not Fixed Width)
文件:`ch16-value-model.md`
- 边界:语义类型 vs 物理表示;value/object 投影与装箱桥;`int` = 任意精度语义类型 +
  值投影(标记小整数通道)+ 对象投影(boxed bignum);值通道溢出必须 deopt/promote
  决不回绕;显式机器整数 `pcc.i64`/`pcc.u64`(wrap/trap/checked/saturating 写进类型);
  普通类身份不可窃取(id/is/weakref/__dict__/mutation/subclass/finalizer);
  值类 = 可选、无身份、显式装/拆箱、身份逃逸诊断、指针载荷 GC 追踪、self 后端 ABI;
  与 Valhalla 的关系(借投影模型,不借 Java 定宽 int)。
- 边界(诚实部分):**已确认开放问题**——typed-int 未装箱 +/-/* 在 i64 溢出时静默回绕
  (违反义务 2),设计张力(unboxed vs bignum)与候选方案,如实呈现。
- 素材:`pcc/value_model.py`、`pcc/runtime/src/py_int_*.c` 群、`AGENTS.md` 义务 7、
  INDEX 检索 valueclass / typed-int / overflow 的调查。

### 第 17 章 包、C-API shim 与扩展 ABI (Packages, the C-API Shim, and Extension ABI)
文件:`ch17-packages-capi.md`
- 边界:真实 `pip install` / `import` 双闸及其证据标准;generic-mechanism 原则
  (修可复用机制:install/import/ABI/buffer/capsule/build-surface,禁包名特判);
  `pcc/package/`、`capi_abi.py`/`capi_surface.py`、`src/py_capi_shim.c`、
  `src/py_extension_loader.c`、buffer protocol;cpython-compat vs pcc-native 两种接受面;
  CpyHandle(tag 32)装箱外来 cpy 引用的机制;`PCC_HOST_PYTHON=/bin/false` 证据法。
- 素材:上列文件、`AGENTS.md` Package/NumPy Claim Hygiene、`codex-goal-prompt.md` §0.10、
  INDEX 检索 package / numpy / capi / extension。
- 案例研究:stale capi header 给出假"gap 0"的测量教训。

## 第 IX 部 工程方法 (Part IX: Engineering Method)

### 第 18 章 工程方法论:测试、调查与声明卫生 (Method: Tests, Investigations, and Claim Hygiene)
文件:`ch18-engineering-method.md`
- 边界:为什么方法论是设计的一部分(本书立场:不动点与五 GC 矩阵本身就是方法论装置);
  调试手册 12 技法(`docs/debugging-playbook.md`)逐条精讲+案例;
  调查工作流(一调查一文件、CONFIRMED/DENIED、一次一个 proposal、INDEX 再生);
  声明卫生表(§0.10);闸门体系(focused gates / bootstrap gates / fallback ratchet /
  Definition of Done);回归纪律(先最小重现、共享 codegen 不堆叠未验证编辑);
  测量纪律(假信心案例:stale cache、PCC_RUNTIME_CC、mid-run 读 buffered log)。
- 素材:`AGENTS.md` 全部方法节、`docs/debugging-playbook.md`、
  `docs/investigation-workflow.md`、`codex-goal-prompt.md` §0.10、INDEX 任选案例。

## 第 X 部 加速器 (Part X: Accelerators)

### 第 19 章 GPU 内核 IR、Metal 与加速器执行 (GPU Kernel IR, Metal, and Accelerator Execution)
文件:`ch19-gpu-kernel-ir.md`
- 边界:加速器线属"拥有执行"论题的延伸(非第六使命,不挤占脊柱);三子系统按成熟度——
  内核 IR(`pcc/kernel_ir/`:host/device 分裂、`validate_kernel()` 拒 PyObject、CPU 参照 oracle、
  `@gpu.kernel`→Metal 规范路径 Kernel IR→validate→TIRx freeze→Metal 定案→启动包、真机执行、
  GPU claim level 0..6、硬件门控/本机 only)、GPU-GC(`pcc/gpu_gc/` CPU-only oracle、未接五后端)、
  分布式(`pcc/dist/` 本机单进程无 socket 元数据);**TVM/TIRx/TileLang 作 oracle 非 owner**——
  不 import/link/执行,`import_tilelang_source`(解析严格 DSL 子集 fail-closed)、
  `lower_to_plain_tir`(TIRx freeze + 拒 CUDA-only)、`project_to_tir_shape`(TVM golden oracle)。
- 素材:`docs/design/pcc-gpu-next-work.md`、`pcc/kernel_ir/gpu_claims.py`、
  `pcc/kernel_ir/{tilelang_import,tirx_adapter,tvm_oracle}.py`、`README.md` GPU 段、
  `AGENTS.md`"加速器执行是所有权论题的延伸"段。
- 设计问题:为什么"oracle,不是 owner"必须写进架构(与 self 后端对 LLVM、值模型对 Valhalla 同律)?
  为什么"有 TVM/tilelang 支持"是要防的 overclaim?
- 案例研究:import 上游帮你跑通 kernel ≠ 执行所有权;claim level 阶梯为何存在。

## 第 XI 部 应用执行 (Part XI: Application Execution)

### 第 20 章 声明式 GUI:组件、调度与无 WebView 应用边界 (Declarative GUI: Components, Scheduling, and a Webview-Free Application Boundary)
文件:`ch20-declarative-gui.md`
- 边界:GUI 作为执行所有权的产品压力测试;canonical `pcc_gui_kit` 的 generation-id
  可回收节点池、layout/clip/scroll/paint-order hit path;机器可读 v1 ABI 的 bounded
  render context/descriptor/state/update/effect/listener/command 记录;key+type reconcile 与
  atomic commit;SET/reducer queue、四 lane、aging、yield/restart/replay;唯一 event dispatch
  owner 与 effect phases;namespaced token、bounded utility candidate compiler/cache;
  managed state、invoke/result/error/cancellation;无 WebView 的 app lifecycle;
  CoreGraphics/Metal/AppKit 边界与 `mac_diff_app` canary。明确“吸收机制”不等于
  React/Tailwind/Tauri API/wire compatibility。
- 素材(2026-09-06 已迁至 allstoalls/pcc-gui,核心最后版本 `977ad074`):`docs/design/gui-declarative-absorption.md`、
  `pcc/runtime/gui_declarative_contract_v1.json`、
  `pcc/runtime/py/pcc_gui_{kit,components,scheduler,events,style,commands,app_lifecycle}.py`、
  `projects/mac_diff_app/{declarative_app,declarative_headless,app}.py`、
  `tests/python/test_pcc_gui_*.py`、`tests/python/test_mac_diff_app_declarative.py`。
- 设计问题:为什么 committed tree 只能有一个 mutation owner?为什么 priority lane 需要
  base-queue replay 而非 dirty bit?为什么 native bridge acknowledgement 不等于 pixel correctness?
  为什么 managed Python references 必须先加入五 GC slot contract?
- 案例研究:runtime/project/app 三份 kernel 导致证据无归属;GUI 暴露的 class-method
  argument tagging 与 raw module-pointer rooting 边界。状态必须写成 source-present、
  gate-defined、task-board TODO_READY 三层,不得把未运行闸门写成完成。

---

## 前言与附录(主笔统一撰写,agent 勿写)

- `ch00-preface.md`:前言(成书缘起、读者假设、如何读)。
- `SUMMARY.md`:目录。
- 附录 A 仓库地图;附录 B 术语对照(从 STYLE.md 扩展)。
