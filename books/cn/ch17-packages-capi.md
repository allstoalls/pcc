# 第 17 章 包、C-API shim 与扩展 ABI

一个 Python 编译器最容易在包生态上自欺。"支持 NumPy"这四个字可以指十几种强度完全不同的事实:从"安装命令没报错",到"纯 Python 子集能 import",再到"C 扩展在 pcc 自己的对象模型上初始化并正确运行"。pcc 对这条战线的回答有两层:一层是机制——[pcc/package/](../../pcc/package) 的通用安装管道、`py_capi_shim.c` 的 C-API shim、`py_extension_loader.c` 的扩展加载器、CpyHandle 装箱;另一层是纪律——pip install 闸与 import 闸是两个独立声明,各自有证据标准,且任何修复都必须落在可复用机制上,禁止 `if package == "numpy"` 式特判。本章两层都讲,因为在这个子系统里,纪律不是机制的注脚,而是机制能否长期成立的前提。

## 本章导读:包兼容是梯度而非开关

这一章先把"支持某个包"拆成多级问题:能安装,能解析 metadata,能 import 纯 Python,能加载扩展,能通过 C-API shim 暴露需要的 ABI。任何一级通过,都不能自动宣称全包生态兼容。

- `pip install` 通过和 `import` 通过是两条不同声明。
- C 扩展兼容要看 ABI、对象协议、buffer、capsule、错误处理和引用所有权。
- 不能为单个热门包写编译器特例;要补的是可复用机制和对应回归测试。

## 17.1 问题与设计空间:兼容性是梯度,不是开关

### 17.1.1 四级梯度

[goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10 把"替代 CPython"拆成一个必须显式区分的梯度:

```text
source compatibility          用户 .py、stdlib、包、import、异常、描述符、
                              元类、async、GC、weakref、线程按 CPython 语义运行
pcc-native extension ABI      面向 pcc 运行时的原生 C 扩展 ABI,不依赖 libpython
CPython C-API compatibility   逐步实现公共 C-API 表面:PyModuleDef、PyMethodDef、
                              capsule、buffer、dict/list/tuple/unicode/int 助手
CPython binary ABI            任意 .so/.pyd 假设 CPython 对象布局、PyObject_HEAD、
                              GIL、私有 API——必须是显式兼容模式,不得伪装成 pcc-native
```

这个梯度回答了第一个设计问题:为什么不直接做二进制 ABI 兼容?因为 pcc 的对象头是 16 字节的 `PyObjectHeader`(refcount + type_tag + flags,见第 7 章),与 CPython 的 `ob_refcnt + ob_type` 布局根本不同;假装布局相同的"兼容"会在第一个直接读字段的扩展上变成内存损坏。pcc 选择把 C-API 兼容(源码级,扩展对着 pcc 的 `Python.h` 重新编译)与二进制 ABI 兼容(链接 CPython 自己的 libpython)拆成两条路,分别命名、分别测量。

### 17.1.2 两种接受面

由此产生两种"接受面"(acceptance surface),它们接受的工件集合不同,失败方式也不同:

- **pcc-native**:扩展必须针对 pcc 的窄 `Python.h`/C-API shim 编译,导出返回 pcc `PyObject*` 的 `PyInit_<leaf>()`,由 [pcc/runtime/src/py_extension_loader.c](../../pcc/runtime/src/py_extension_loader.c) 在无 libpython 的进程里 dlopen。任何名字里带 CPython 扩展 ABI 标记(`cpython-NN`、`cpNN-cpNN`、`abi3`)的原生工件都被拒绝。
- **cpython-compat / libpython**:编译产物链接宿主 CPython 的 libpython,第三方包的 import 蹦床到 [pcc/runtime/src/py_libpython.c](../../pcc/runtime/src/py_libpython.c) 的 `py_cpy_*` 包装层。该文件的设计注释写明关键决定:CPython 的 `PyObject*` 与 pcc 自己的 `PyObject*` 是**两个不相交的指针命名空间**,前者对代码生成只暴露为不透明 `void*`,二者决不混叠。

两种面各自诚实:pcc-native 拒绝它跑不了的东西并给出诊断码;cpython-compat 接受 CPython ABI 工件但明说自己依赖 libpython。声明卫生表(§0.10)里的 `cpython-compat pass != pcc-native pass` 就是禁止把一种面的通过说成另一种面的能力。

### 17.1.3 generic-mechanism 原则

第三个设计决定写在 AGENTS.md 北极星义务 3 里:**生态支持必须是通用的**。NumPy、PyTorch、pandas、Arrow、SciPy 是集成目标,决不是编译器特例;禁止 `if package == "numpy"`,要修的是可复用机制——install、import、ABI、buffer、capsule、build-surface——并对通用特性回归。这条原则有工程上的硬理由:包名特判不可扩展(每个包一坨私有逻辑)、不可测试(测的是包名不是机制)、且制造假声明("支持 NumPy"实为"硬编码了 NumPy 的路径")。本章后面每个模块的文档字符串都能看到这条原则的回声:[pcc/package/install.py](../../pcc/package/install.py) 开头写着 "This is a real local/cache install skeleton, not a NumPy-specific shortcut";`build_exec.py` 的 include 重定向注释强调 "Generic — no package-specific rules"。

## 17.2 双闸:pip install 与 import 是两个声明

### 17.2.1 为什么必须拆成两个闸门

AGENTS.md "Package / NumPy Claim Hygiene" 一节把规则写死:

- `pcc1 -m pip install numpy ...` 成功,**仅当**真实包工件被安装进目标 site、且其包元数据可用;
- `import numpy` 是**另一个闸门(gate)**。不得从安装成功、从 array-core-only 测试、或从一个恰好叫 `numpy` 的合成包推出"支持 NumPy"。

拆分的理由是两个闸门失败的子系统完全不同。install 闸考验的是工件解析、wheel 标签兼容、解包、清单写入、链接扫描——全部在 [pcc/package/](../../pcc/package) 里;import 闸考验的是模块解析、低层化(lowering)、扩展加载、C-API shim、对象模型——分布在 `pipeline.py`、`import_lowering.py`、运行时 C 代码里。一个"NumPy 支持"的合并声明会把两组证据搅成一团,而 §0.10 的声明卫生表正是为防止这种搅拌而存在:

```text
fake package pass      != real package pass
array-core pass        != import numpy pass
metadata exists        != runtime implementation complete
```

[pcc/package/array_core.py](../../pcc/package/array_core.py) 是 `array-core pass != import numpy pass` 这行的具体注脚:它是一套通用数组核心语义的报告前门(arange/reshape/matmul 等布局语义),无论它多完整,都不构成 `import numpy` 证据——它根本没有碰 NumPy 的 C 代码。

### 17.2.2 install 闸的证据标准

install 闸的可执行形态在 [tests/python/test_package_import_path.py](../../tests/python/test_package_import_path.py)。以 `test_pcc1_pip_install_numpy_name_from_find_links_command_shape` 为例,它的断言层层递进:`pcc1 -m pip install numpy --no-index --find-links <dir> --target <site>` 的 JSON 输出 `ok` 为真、解析出的包名正确、`installs[0]["source_path"]` 指向真实 wheel 文件、site 目录下真的出现了包文件。注意这个测试的命名诚实:`command_shape`——它安装的是一个**按 wheel 命名规范造的合成包**,证明的是命令形状与安装管道,不是真 NumPy。真 NumPy 的 install 证据是另一个测试 `test_pcc1_pip_install_real_numpy_artifact_opt_in`,用 `PCC_RUN_REAL_NUMPY_INSTALL=1` 与 `PCC_NUMPY_ARTIFACT` 显式选入。同一个文件里,假包与真包是两个测试、两份证据。

### 17.2.3 `PCC_HOST_PYTHON=/bin/false` 证据法

install 闸还有一个细节决定证据强度:`pcc1` 是编译出来的原生二进制,但仓库的若干自举宿主查询允许通过 `PCC_HOST_PYTHON` 环境变量逃逸到一个宿主 Python 子进程。于是"pcc1 安装了包"有一个隐蔽的弱化形态——pcc1 只是壳,实际工作偷偷由宿主 Python 完成。证据法是把逃逸通道指向一个必定失败的程序:上述测试统一设置 `env["PCC_HOST_PYTHON"] = "/usr/bin/false"`(AGENTS.md 行文用 `/bin/false`,原理相同)。此后任何对宿主 Python 的静默求助都立即变成硬失败,测试通过就证明整条 install 链确实跑在 pcc1 自己的原生代码里。这是一种值得推广的证据构造:**不是断言"没有用宿主",而是让"用宿主"必然失败**——把否定性声明转换成可执行闸门。

### 17.2.4 import 闸的证据标准

import 闸同样分层。纯 Python 包的 import 证据是端到端的:安装后用 `pcc1 --python-libpython=off --ir-scaffold=on` 编译一个 `import demo_pkg` 的主程序、运行、断言输出(`test_pcc1_pip_install_wheel_participates_in_import_site` 断言打印 `43`)。涉及 **CPython-ABI** C 扩展时,no-libpython 模式的当前诚实证据是**拒绝**:真实 CPython 扩展 wheel 在 import 边界被 `PCC-PKG-004` 挡下(17.4 节),而不是默默生成成千上万个 `py_cpy_*` 回退调用。

而在前端包扫描与链接层,[pcc/package/linkage.py](../../pcc/package/linkage.py) 对 CPython ABI 扩展工件给出拒绝诊断:

```python
# pcc/package/linkage.py
def _diagnostic_for_cpython_extension_abi(path: str) -> dict[str, object]:
    return {
        "code": "PCC-PKG-004",
        "message": (
            "native artifact name declares a CPython extension ABI; "
            "pcc-native mode requires a pcc-native extension ABI or a source rebuild"
        ),
        "path": path,
    }
```

## 17.3 包管道:[pcc/package/](../../pcc/package) 的通用机制

### 17.3.1 pip 前门

在 C-API shim 侧,[pcc/runtime/src/py_capi_shim.c](../../pcc/runtime/src/py_capi_shim.c) 实现了标准的 C-API 函数代理:

```c
// pcc/runtime/src/py_capi_shim.c
PyObject *py_capi_PyObject_CallObject(PyObject *callable, PyObject *args) {
    if (callable == NULL) return NULL;
    return py_call_callable(callable, args, NULL);
}

void *py_capi_PyCapsule_GetPointer(PyObject *capsule, const char *name) {
    if (capsule == NULL || py_type_of(capsule) != PY_TYPE_CAPSULE) return NULL;
    return py_capsule_pointer(capsule, name);
}
```

在原生扩展加载器中,[pcc/runtime/src/py_extension_loader.c](../../pcc/runtime/src/py_extension_loader.c) 负责通过 `dlopen` 加载 pcc-native `.so` 并唤醒 `PyInit_<mod>`:

```c
// pcc/runtime/src/py_extension_loader.c
PyObject *py_extension_load_native_so(const char *so_path, const char *mod_name) {
    void *handle = dlopen(so_path, RTLD_NOW | RTLD_GLOBAL);
    if (handle == NULL) return NULL;
    char init_name[256];
    snprintf(init_name, sizeof(init_name), "PyInit_%s", mod_name);
    PyInitFunc init_fn = (PyInitFunc)dlsym(handle, init_name);
    if (init_fn == NULL) return NULL;
    return init_fn();
}
```

[pcc/package/pip_shim.py](../../pcc/package/pip_shim.py) 是 `pcc -m pip` 的前门,文档字符串先把边界说清:接受常见的 `pip install ... --dry-run` 形态并报告计划而不调用 pip 的安装器;非 dry-run 的本地安装走 pcc 自己的安装器而非上游 pip。`_parse_install_args` 识别 `--target`、`--cache-dir`、`--find-links`、`--index-url`、`--no-index`、`--report`,以及一个 pip 没有的旗标:`--abi`(默认 `pcc-native`)——ABI 模式从命令行第一站就开始流动,后面的兼容判断、链接扫描、构建重定向全部按它分派。

### 17.3.2 工件解析与 wheel 标签

[pcc/package/install.py](../../pcc/package/install.py) 的 `_artifact_compatibility_reason_from_name()` 是 pcc-native 模式下工件准入的单一判定点。源码工件(`.tar.gz` 等)放行,理由 `source_artifact`——源码总可以用 pcc 的工具链重建;`py3-none-any` 纯 Python wheel 放行,理由 `pure_python_wheel`;wheel 标签等于 `pcc_native_wheel_tag()` 的放行,理由 `pcc_native_wheel`;其余 wheel 拒绝,理由 `wheel_tag_not_pcc_native_compatible`。[pcc/package/metadata.py](../../pcc/package/metadata.py) 的 `pcc_native_wheel_tag()` 生成 `pcc{major}-pcc_native-{platform}` 形态的标签——pcc 把自己注册成 wheel 命名规范里的一等平台,而不是冒用 CPython 的 `cp3xx` 标签。这又是 generic-mechanism:兼容性判断全部基于工件命名规范与 ABI 模式,没有任何包名出现在逻辑里。本地 wheel 仓库(`pcc-wheel-repository.json` 清单,见 `_repository_manifest_candidates()`)同样按 `pcc_native_compatible` 与 `links_libpython` 字段过滤,而非按包名。

### 17.3.3 链接扫描:PCC-PKG-003 与 PCC-PKG-004

[pcc/package/linkage.py](../../pcc/package/linkage.py) 在安装边界执行 no-libpython 声明。`_LIBPYTHON_PATTERNS` 在链接命令与原生工件字节里扫 `libpythonX.Y`、`-lpython`、`Python.framework`、`pythonXY.dll` 四类证据,命中即产出诊断码 `PCC-PKG-003`;`_CPYTHON_EXTENSION_ABI_RE` 按名字识别 CPython 扩展 ABI(`cpython-\d+`、`cp\d+-cp\d+`、`abi3`),命中产出 `PCC-PKG-004`。`linkage_report()` 的判定逻辑把两种接受面写成布尔代数:`links_libpython` 仅在 `abi_mode == "libpython"` 时可接受;`uses_cpython_extension_abi` 在 `libpython` 与 `cpython-compat` 两种模式下可接受;`no_libpython_runtime` 只有在零 libpython 边、零 CPython ABI、且模式为 `pcc-native` 时才为真。`install_package()` 把这份报告整体写进每个安装根目录的 `pcc-package.json` 清单——后续的 import 闸不必重新发明判定,读清单即可,但它仍会重扫(17.4 节会讲为什么)。

### 17.3.4 构建表面:include 重定向

源码工件要变成 pcc-native 扩展,必须对着 pcc 的 `Python.h` 编译,而真实包的构建系统(NumPy 用 meson)在 `compile_commands.json` 里烤死了 CPython 的 `-I` 路径。[pcc/package/build_exec.py](../../pcc/package/build_exec.py) 的解法是两个通用函数:`_materialize_pcc_capi_include()` 把 [utils/fake_libc_include/](../../utils/fake_libc_include) 中**仅限** `_PCC_CAPI_HEADERS` 列出的八个 C-API 头(`Python.h`、`structmember.h`、`pymem.h`、`frameobject.h`、`pythread.h`、`pyerrors.h`、`abstract.h`、`datetime.h`)物化到 `<build>/pcc-package/pcc-capi-include`;`_redirect_pcc_native_includes()` 按 `_CPYTHON_INCLUDE_DIR_RE` 丢弃 CPython 头目录的 `-I`/`-isystem`,把 pcc 的 C-API 目录与 [pcc/runtime/include](../../pcc/runtime/include) **追加在末尾**——包自己的头永远优先,pcc 只填补被丢弃的 `Python.h` 空缺。

"仅限八个头"不是吝啬,是一次真实教训的固化:[utils/fake_libc_include/](../../utils/fake_libc_include) 整目录里有桩版 `math.h`/`complex.h`,整目录上 include 路径会遮蔽 NumPy C 核心需要的真实系统 libm。重定向只在 `abi_mode == "pcc-native"` 且语言为 C 时生效,头文件定位失败时发出 `PCC-PKG-CAPI-INCLUDE-MISSING` 诊断并跳过,而不是带病构建。

## 17.4 import 闸:两个拒绝点与一条回退路

### 17.4.1 包边界的早失败

[pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) 的 `_validate_package_site_no_libpython_abi()` 在 `--python-libpython=off` 下对参与编译的安装包根目录重新扫描,任何名字带 CPython ABI 的原生扩展都让编译当场失败,错误信息携带 `PCC-PKG-004` 与修复指引("reinstall with --abi=pcc-native from source, or choose an explicit --abi=libpython / --abi=cpython-compat mode")。它的文档字符串解释了为什么不信任安装时清单而要重扫:旧安装可能产生于 ABI 闸存在之前;与其让 codegen 在后面生成"thousands of opaque `py_cpy_*` fallback calls",不如在包边界给一个可操作的失败。这是 pcc 错误哲学的缩影:回退(fallback)边界必须诚实,失败要发生在最能解释自己的位置。

### 17.4.2 pcc-native 扩展的解析与低层化

通过了 ABI 闸的 pcc-native 扩展,由 [pcc/frontends/python/codegen/import_lowering.py](../../pcc/frontends/python/codegen/import_lowering.py) 的 `_resolve_pcc_native_extension_path()` 在 `PCC_PACKAGE_SITE` 各 site 根下按 `模块点路径 → 目录路径 + {.so,.dylib,.pyd,.dll}` 搜索,候选名带 CPython ABI 标记的同样跳过——拒绝逻辑在低层化阶段重复出现,两道闸互为冗余。命中后 `_emit_native_extension_import()` 发射对运行时 `py_native_extension_import` 的调用,并立即跟一个 `_emit_post_call_err_check()`:第 8 章讲过 pcc 的异常模型没有栈展开,任何可能 raise 的运行时调用之后必须显式检查 `py_err_occurred()`,扩展导入不例外。

### 17.4.3 cpython-compat 的蹦床

在 libpython 模式下,第三方 import 低层化为 `py_cpy_import` 等调用,进入 `py_libpython.c` 的包装层:`Py_Initialize` 在首次 import 时惰性调用,`atexit` 注册 `Py_Finalize`,全部 CPython API 调用持 GIL。两个指针命名空间的纪律在这里变成可执行约束——pcc 侧拿到的 CPython 引用是 `void*`,要进入 pcc 对象图必须经过显式转换(`py_cpy_to_pcc_obj()` 递归转换 None/bool/int/float/str/list/tuple/dict/set,不支持的退化为 `str(obj)`)或装箱(17.6 节的 CpyHandle)。

## 17.5 C-API shim:从符号目录到对象模型桥

**2026-08 当前实现注。** 本章初稿中的 `src/py_capi_shim.c` 叙述保留下来作为机制来源与 host-C oracle 说明,但它不再是生产 pcc-Python 归档的 owner。当前生产实现分拆在 `pcc/runtime/py/py_capi_*_runtime.py`:exception/data symbols、dict/object/type/unicode/capsule/buffer、module state、descriptor、variadic call 与 visit surface 各有 Python owner;`py_extension_loader_runtime.py` 拥有原生扩展加载;CpyHandle ABI 由 `py_obj_dealloc.py` 拥有。`pcc/runtime/Makefile` 的 `LIB_PCC_PY` 只归档 `PCC_PY_OBJECTS`。因此下列 C shim 细节应读作 ABI 语义与迁移历史,不是“当前生产仍链接一个手写 C shim”的声明;第 14 章给出 source-ownership 与最终 no-C/zero-libc 验收边界。

### 17.5.1 可执行的优先级地图

[pcc/frontends/c/capi_surface.py](../../pcc/frontends/c/capi_surface.py) 的文档字符串先声明自己不是什么:"This is not an implementation of every C-API symbol. It is the executable priority map used by extension-loader work so gaps are explicit and tested." 每个符号是一条 `CApiSymbol(name, header, priority, implemented, notes)` 记录,优先级枚举 `CApiPriority` 从 `IMPORT_BLOCKER`(0)经 `RUNTIME_CORE`、`ARRAY_CORE`、`NUMPY_CAPI` 到 `ACCELERATION`(5)。这个目录的价值在于它把"缺口"变成数据:`extension_abi_plan()` 接受一组需求符号(可用 `require_capsule`/`require_buffer`/`require_memoryview`/`require_numpy_capi` 批量展开),输出结构化诊断——`PCC-EXT-MISSING-CAPI-SYMBOL`(在目录里但未实现)、`PCC-EXT-UNKNOWN-CAPI-SYMBOL`(不在目录里)、`PCC-EXT-MISSING-CAPI-HEADER`(头文件缺失)、`PCC-EXT-ABI-VERSION-MISMATCH`(版本不符)。值得注意的是 NumPy C-API 符号(`PyArray_*`/`PyUFunc_*`)在目录里被显式标记 `implemented=False`,并带 `_NUMPY_CAPI_TABLE_SLOTS` 元数据(capsule 表名、槽号、失败模式)——未实现的部分不是被省略,而是被精确登记。[pcc/frontends/c/capi_abi.py](../../pcc/frontends/c/capi_abi.py) 则是一份七个符号的最小核心表,`extension_import_blockers()` 直接回答"还差什么才能 import"。

### 17.5.2 shim 的自我设限与 PyModuleDef

[pcc/runtime/src/py_capi_shim.c](../../pcc/runtime/src/py_capi_shim.c) 开头的注释是这份五千余行文件的契约:"deliberately narrow ... It does not claim CPython binary object-layout parity."。它自带一套 C-API 类型定义(`Py_buffer`、`PyMethodDef`、`PyModuleDef`),其中 `PyModuleDef` 上方的注释点出一条布局不变式:**必须与 [utils/fake_libc_include/Python.h](../../utils/fake_libc_include/Python.h) 的 `PyModuleDef` 完全一致**——扩展对着后者编译,shim 对着前者读 `m_slots`,两边漂移就是越界读。这与第 7 章 C/pcc-Python 镜像布局纪律同构,只是这次镜像的两端是"扩展看到的头文件"与"运行时自己的结构体"。

多阶段初始化(PEP 489)的处理是一个朴素而有效的标记技巧:`PyModuleDef_Init()` 把静态变量 `pcc_capi_moduledef_marker` 的地址盖进 `def->m_base.ob_base`;加载器拿到 `PyInit_*` 返回值后用 `pcc_capi_is_moduledef()` 检查这个标记——真模块的头 8 字节是 refcount,不可能等于该地址。命中则走 `pcc_capi_module_exec()`:先 `PyModule_Create2()` 建模块,再遍历 `m_slots` 执行每个 `Py_mod_exec` 槽(NumPy 正是在这里注册类型与 `PyArray_API` capsule)。

`PyModule_Create2()` 揭示了 shim 的总策略:**用 pcc 自己的对象扮演 C-API 概念**。模块就是一个 `py_instance_new()` 出来的 pcc 实例,`__name__` 是实例属性,`METH_VARARGS` 方法经 `py_func_new()` 变成实例上的可调用属性。`m_size > 0` 的模块状态用 `calloc` 分配并登记进一张状态表;`pcc_capi_visit_extension_module_state_roots()` 让五个 GC 后端把模块对象与 `m_traverse` 报告的状态引用当作根并钉住(pin)——扩展模块状态里的 pcc 对象不因 GC 而消失,这是 2026-05-31 一次真实调查(`gc-5backend-extension-module-state-roots-no-libpython.md`)固化的不变式。capsule 同样是 pcc 实例:指针、名字、析构器分别以 `__pcc_capsule_pointer__` 等属性存放(指针经 `PyLong_FromVoidPtr` 装箱),`PyCapsule_GetPointer`/`PyCapsule_IsValid` 按 CPython 语义做名字匹配。

边界语义的小处见真章:`PyModule_GetDict()` 在 `py_obj_getattr` 返回拥有引用后立即 `py_decref` 再返回——因为 CPython 契约规定它返回借用引用。C-API shim 的每个函数都站在两套引用计数约定的交界上,这类显式的所有权调整是 shim 正确性的主要内容。

### 17.5.3 buffer 协议

buffer 协议有两份实现,各司其职。[pcc/library/buffer_protocol.py](../../pcc/library/buffer_protocol.py) 是 Python 侧的规划模型:`PyBUF_*` 旗标常量与一个 `BufferView` 数据类,`check_flags()` 按 CPython 语义抛 `BufferError`(可写性、shape、strides 的请求校验)——它服务于包规划与测试,不碰内存。真正给扩展用的在 shim 里:`pcc_capi_buffer_data()` 认 pcc 的 bytes(只读)、bytearray(可写)、memoryview(经 `pcc_gc_load_ptr()` 读 base 递归——注意即使在 C-API shim 内部,指针槽读取也走 GC 读屏障,第 10 章的屏障纪律没有豁免区);`PyObject_GetBuffer()` 填出一维连续、`itemsize` 为 1、格式 `"B"` 的 `Py_buffer`,需要 shape/strides 时把一个 `PccBufferMeta` 挂在 `view->internal` 上,并对导出对象 `py_incref`;`PyBuffer_Release()` 对称地 decref 并释放。这是诚实的窄实现:足够 `bytes`/`bytearray`/`memoryview` 的 SIMPLE/一维场景,不假装支持多维 strided 视图。

### 17.5.4 类型桥与 ob_type:最硬的边界

最深的问题是对象模型本身。扩展的静态 `PyTypeObject` 经 `PyObject_HEAD_INIT` 初始化后,在 pcc 的头布局下 `type_tag` 恰好是 0(`PY_TYPE_NONE`)——pcc 无法把一个 C 扩展类型对象与 `None` 区分;扩展实例的结构体(`PyObject_HEAD` 后紧跟自己的字段)也与 pcc 的 `PyInstanceObject` 布局冲突。调查文档 `python-no-libpython-numpy-build-pcc-capi-include-redirect.md` 记录了桥的设计与落地:`PyType_Ready()` 给类型分配**动态 type_tag**(基址 0x10000,高于内建枚举),登记进 tag→`PyTypeObject*` 注册表;`PyType_GenericAlloc/New` 按 `tp_basicsize` 用 `pcc_gc_alloc` 分配并打上动态 tag,扩展实例保持自己的布局;`Py_TYPE` 对动态 tag 查注册表、对内建 tag 映射到 shim 里定义的内建类型识别令牌(`PyLong_Type` 等,标记小整数直接映射 `&PyLong_Type`);子类型检查沿 `tp_base` 链行走。

而 NumPy 直接读 `((PyObject*)x)->ob_type` 的代码暴露了 tag 方案的天花板:pcc 头里没有那个指针,这是头文件无法伪装的字段访问。最终的解法是一次七处协同的布局变更——`PyObject_HEAD`/`PyVarObject` 增长出 `ob_type` 槽、初始化宏与 `struct PyObject` 同步、shim 里手工铺设的 `PyTypeObject` 布局镜像随之移位、`PyType_GenericAlloc` 负责填槽。这个变更的验证标准值得记住:不是玩具扩展测试通过,而是完整 `scripts/bootstrap.sh --backend self` 三阶段自举打出 pcc2/pcc3 字节同一——共享运行时布局的改动,只有不动点才有资格说"没破坏"。

## 17.6 扩展加载器与 CpyHandle

### 17.6.1 加载器

`py_extension_loader.c` 是 pcc-native 面的 dlopen 入口,逻辑刻意简单:按模块名与路径查缓存链表,未命中则 `dlopen(path, RTLD_NOW | RTLD_GLOBAL)`、`dlsym` 出 `PyInit_<leaf>`(leaf 取点路径最后一段)、调用之;返回值若被 `pcc_capi_is_moduledef()` 识别为模块定义,则转 `pcc_capi_module_exec()` 跑多阶段初始化。成功的模块进入缓存节点并被 `pcc_gc_pin()`——模块在进程生命周期内不参与回收,这与 CPython 模块的事实不朽一致。`py_native_extension_import_by_name()` 提供按名查找:在 `PCC_PACKAGE_SITE`(冒号分隔,Windows 用分号)各根下尝试 `.so`/`.dylib`/`.pyd`/`.dll` 四种后缀。错误路径统一经 `pcc_extension_runtime_error()` 转成携带 dlerror 文本的 RuntimeError——dlopen 失败对用户可见、可诊断,而不是静默回退。

### 17.6.2 CpyHandle:外来引用的装箱

cpython-compat 模式有一个对象图难题:挂起的生成器帧只能持有 pcc 对象——帧保存走 `py_list` 的存储屏障,帧析构按 pcc 对象头解引用——但生成器局部变量可能是 `py_cpy_*` 拿回的 CPython 引用。[pcc/runtime/src/py_cpy_handle.c](../../pcc/runtime/src/py_cpy_handle.c)(类型标签 `PY_TYPE_CPY_HANDLE = 32`,定义于 `py_runtime.h`)给出装箱方案:`PyCpyHandleObject` 是一个 pcc 对象头加一个 `void *cpy_ref` 字段,文件注释强调该字段"**不是** pcc 槽"——GC 永远不解释这个外来指针。`py_cpy_handle_new()` 取得外来引用的所有权,`py_cpy_handle_get()` 借用,`py_dealloc_cpy_handle()` 在析构时通过注册的释放钩子归还外来引用——于是丢弃一个挂起的生成器,会**结构性地**释放它持有的活 CPython 迭代器,无需任何特判清理代码。

两个细节展示了运行时分层与五 GC 平等契约如何约束一个小文件。其一,释放钩子 `py_cpy_handle_set_release_fn()` 存在的原因是归档边界:`py_cpy_handle.c` 在主运行时归档里,而 `py_cpy_decref` 在独立的 libpython 归档里;不初始化 libpython 桥的进程不可能产生过外来引用,所以 NULL 钩子安全——依赖方向只从桥指向主归档,决不反向。其二,新类型标签必须接入对象生命周期的全部分派点:`py_obj.c` 与 `py_gc_backend.c` 的两处析构 switch 都登记了 `py_dealloc_cpy_handle`,后端 #4 的 `pcc_gc_relocate_copy_supported_tag()` 白名单也加入了它,旁注解释"CpyHandle 没有 pcc 指针槽——浅拷贝重定位与 str 一样安全"。一个 58 行的 C 文件,接口却横跨析构分派、重定位白名单与归档链接拓扑——这正是"新增运行时类型"在 pcc 里的真实成本。

## 17.7 历史与教训

### 17.7.1 过期的 C-API 头给出假"gap 0"(2026-05-29)

`python-no-libpython-numpy-build-pcc-capi-include-redirect.md` 记录的这场测量事故,是本仓库最干净的"测量基底腐烂"标本。背景:为回答"NumPy 还差 pcc 多少宿主 C-API 符号",工作流是把 NumPy `_core` 的 C 文件对着 `/tmp/pcc_capi`(从 [utils/fake_libc_include/](../../utils/fake_libc_include) 拷出的精选头)编译成 `.o`,再用 `nm` 对照运行时归档统计未提供符号。经过十几个批次的符号实现,batch 17 打出里程碑:"FULL-MODULE host C-API LINK gap = 0",随后的扩展测量进一步得出"剩余 506 个符号全部是 NumPy 内部符号,宿主侧零缺口"。

两个结论都错了。调查里题为 CORRECTION 的小节记录了根因:测量用的 60 个 `.o` 是对着一份**过期的** `/tmp/pcc_capi/Python.h` 编译的——其 mtime 早于当个会话后来追加的一批声明。过期头编不过 38 个文件,这 38 个文件就**静默地**缺席了 `.o` 集合,它们引用的宿主符号从未进入统计。错误假设是"编译基底不变";证据链是头文件 mtime 与文件计数(60/98 对 95/98)。刷新 `/tmp/pcc_capi` 重测:95 个文件可编译,真实缺口是 **10 个符号,不是 0**——`PyArg_UnpackTuple`、`PySlice_New`、`PyUnicode_Format` 等,全部"声明了所以编译过,但没有运行时实现所以链接缺"。

修复本身平淡:batch 18/19 用真实 pcc 原语补齐 10 个符号,再次到达 gap 0——这次调查特意写明"(CORRECTLY verified this time)",并附测量基底(95 个新鲜 `.o`、当前头);batch 20/21 再关掉最后三个编译缺口后,以"refreshed /tmp/pcc_capi per the stale-header lesson"复测出 98/98 编译、链接缺口 0。留下的不变式有三条:测量前必须从 [utils/fake_libc_include/](../../utils/fake_libc_include) 刷新精选头目录(已固化为工作流规则);"gap 0"类声明必须随附测量基底描述;以及一条元教训——**假里程碑被以书面 CORRECTION 公开撤销,而不是悄悄覆盖**。调查文件里 batch 17 的错误结论原文保留,后接更正,这使下一个读到"gap 0"的 agent 能看到它曾经怎样错过。声明卫生不只约束怎么说成功,也约束怎么撤回成功。

### 17.7.2 看似 NumPy 的 bug,其实谁都跑不了(2026-05-29)

第二个故事来自另一条接受面。`python-cpython-compat-import-numpy-multiarray-init-fails.md` 记录:cpython-compat 模式(`--python-libpython=on`)下 `import numpy` 一路走过纯 Python 加载,死在核心 C 扩展上——`SystemError: execution of module numpy._core._multiarray_umath failed without setting an exception`。同一份 NumPy 在同一个 CPython 下直接运行正常。

最顺手的错误假设是:NumPy 的 C-API 需求太大,pcc 缺了哪个符号。调查明确抵制了这个方向——符号表面当时已 384/406,import 也正确低层化到了 `cpy.import.numpy`;"without setting an exception"的签名同样可能指向嵌入式解释器的运行时状态不符。决定性的一步是隔离实验:换一个最小的目标,`import unicodedata`——宿主 libpython **自带**的平凡 stdlib C 扩展,连 PYTHONPATH 都不用设。它以完全相同的方式失败:同样的 SystemError,外加模块属性缺失(`AttributeError: unidata_version`)——说明 `Py_mod_exec` 槽的模块体根本没有执行完成。

结论重写了问题本身:这不是 NumPy bug,而是 pcc 的 libpython 嵌入层执行 C 扩展多阶段初始化的**通用** bug;调查原文承认"this file's title is therefore narrower than the root cause; numpy is just the motivating case"。教训与 generic-mechanism 原则镜像对称:原则禁止把通用机制写成包特判,而这次调查表明**诊断同样不能包特判**——在为大目标的失败立项之前,先用一个最小的同类目标做隔离;如果最小目标同样失败,修复点就该上移到通用机制(此处是 `_imp.create_dynamic`/`exec_dynamic` 一线的扩展加载执行路径),一次修复解锁所有 `.so`,而不是给 NumPy 修一个、再给 pandas 修一个。

## 17.8 小结

包与扩展子系统是 pcc 的诚实边界最受考验的地方,本章的机制与纪律可以收拢为五条:

1. **兼容是梯度。** 源码兼容、pcc-native 扩展 ABI、CPython C-API 兼容、CPython 二进制 ABI 兼容是四级不同的声明;pcc-native 与 cpython-compat 是两种接受面,`py_libpython.c` 用两个不相交的指针命名空间把后者与 pcc 对象图隔开。
2. **双闸独立。** pip install 闸的证据是真实工件进入 site 且元数据可用(`PCC_HOST_PYTHON=/usr/bin/false` 封死宿主逃逸);import 闸另证,合成包、array-core、命令形状都不外推。
3. **机制必须通用。** 准入看 wheel 标签与 ABI 模式(`pcc_native_wheel_tag()`),拒绝看命名规范(`PCC-PKG-003`/`PCC-PKG-004`),构建重定向看 include 目录模式——没有任何判定以包名为输入。
4. **shim 用 pcc 对象扮演 C-API 概念,并明说天花板。** 模块、capsule 是 pcc 实例;buffer 是一维窄实现;类型桥用动态 type_tag,直到 `ob_type` 字段访问逼出一次以自举不动点验证的头布局变更。
5. **测量与撤回同受声明卫生约束。** 过期头给出的假 gap 0 以书面 CORRECTION 撤销;看似包特定的失败用最小隔离实验上移为通用 bug。

## 练习

1. **读源码验证。** 在 [pcc/package/linkage.py](../../pcc/package/linkage.py) 中找出 `no_libpython_runtime` 为真的全部三个必要条件,并解释为什么 `abi_mode == "cpython-compat"` 时 `uses_cpython_extension_abi` 不阻断 `ok`,却仍使 `no_libpython_runtime` 为假。这两个字段分别服务于哪类声明?
2. **追一条路径。** 从 `pcc -m pip install demo.whl --target site` 出发,沿 `pip_shim.py::pip_install_plan` → `install.py::install_package` → `linkage.py::linkage_report` 列出 `pcc-package.json` 清单中 `links_libpython`、`pcc_native_wheel_tag`、`diagnostics` 三个字段各自的来源函数。
3. **边界语义。** `py_capi_shim.c` 的 `PyModule_GetDict()` 在返回前对结果 `py_decref`。结合第 9 章的拥有/借用引用契约,说明这一行为什么必要、删掉它会产生哪类 bug、以及为什么这类 bug 在玩具扩展上很难被发现。
4. **设计权衡论证。** `PyType_Ready` 的动态 type_tag 注册表(基址 0x10000)与后来落地的 `ob_type` 头字段是两种共存的机制。各自回答了什么问题?如果当初直接给每个 pcc 对象头加 `ob_type` 指针而不做 tag 注册表,会在哪些子系统(分配、GC 重定位、pcc-Python 镜像、自举)付出什么代价?
5. **证据构造。** 仿照 `PCC_HOST_PYTHON=/usr/bin/false` 的手法,为"pcc1 的扩展加载不静默回退到 LLVM 后端"设计一个把否定性声明转换为必然失败的可执行闸门,并说明它与单纯断言日志里没有回退记录相比强在哪里。
