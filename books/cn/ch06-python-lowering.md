# 第 6 章 Python 低层化:facade 与 mixin 群

类型推断(见第 5 章)结束后,pcc 的 Python 前端拿到的是一棵带类型标注的 AST;LLVM 后端与 self 后端(见第 12、13 章)接收的是 LLVM IR。把前者变成后者的层叫 Layer-1 codegen,它是整个 Python 路径里语义密度最高的一层:Python 的下标、迭代、异常、所有权、格式化,全部在这里被翻译成对运行时函数的调用序列与基本块结构。本章讲两件事。第一,这一层的物理组织:[pcc/frontends/python/codegen/layer1.py](../../pcc/frontends/python/codegen/layer1.py) 如何从一个两万行的单文件巨石拆成一个 56 行的 facade 加 86 个 mixin,以及拆分过程中"编译器必须能编译自己"这条约束如何反过来塑造了代码形态。第二,这一层的语义纪律:为什么每个可 raise 的运行时调用之后都必须插入 `py_err_occurred()` 检查,以及当同一个 Python 语义散布在多条低层化(lowering)路径上时,会发生什么——本章"历史与教训"里的双下标路径和六条除法路径,是这个失败模式最有教育意义的两次现场。

## 本章导读:低层化把语义连接到运行时

读这一章时,不要把低层化理解成"语法树换一种格式"。它真正做的是把 Python 语义拆成 IR 控制流、runtime 调用、错误检查和引用清理,并且每一步都要保留 no-libpython 的边界。

- facade 负责调度,mixin 负责具体语义;这是一种防止单文件无限膨胀的分层。
- 每个可能抛异常的 runtime 调用之后,生成代码都要检查错误状态。
- 局部变量、返回值、临时值的 owned/borrowed 身份会直接决定对象什么时候释放。

## 6.1 问题与设计空间

### 6.1.1 巨石的终点

在 2026 年 4 月底(commit `88ee9157`,版本 0.1.2),[pcc/frontends/python/codegen/layer1.py](../../pcc/frontends/python/codegen/layer1.py) 是一个 20,195 行的单文件:表达式分发、语句分发、下标、循环、异常、类、native 模块低层化,全部塞在一个 `L1CodeGen` 类里。今天,同一个文件是 56 行,只剩下:

```python
class L1CodeGen(L1CodeGenEntrypointMixin, L1CodeGenMixinStack):
    ...
```

真正的实现散布在 [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) 目录下约一百个文件、六万余行代码里:`*_lowering.py` 形式的 mixin、`native_*.py` 形式的原生模块低层化,加上 `class_gen.py`、`hoist_lowering.py` 这样的大型专项模块。

为什么要拆?有三个原因,其中只有第一个是显然的。

**维护性。** 两万行的类没有可读的结构。每个进来修 bug 的人(或 agent)都面对同一个问题:改动的爆炸半径无法估计。仓库规则([AGENTS.md](../../AGENTS.md))把"对共享 codegen 做宽泛的投机式修改"列为制造昂贵回归的最快方式,不是修辞,是统计。

**自托管成本。** pcc 的 Python 前端必须能编译 pcc 自己(见第 15 章)。自举剖析([docs/investigations/bootstrap-self-time-after-layer1-split-2026-05-13.md](../../docs/investigations/bootstrap-self-time-after-layer1-split-2026-05-13.md),2026-05-14 数据)显示,在 pcc1 编译 [pcc/__main__.py](../../pcc/__main__.py) 的约 22.6 秒里,`multi_codegen_layer1` 这一个事件桶就占约 11.4 秒,是最大单项。codegen 层自身的形态——文件大小、嵌套函数、闭包形状——直接决定自举的墙钟时间。值得诚实记录的是同一份调查的反向教训:拆分把 `layer1.py` 降到 603 行之后,焦点剖析显示行数下降并没有等比例换来 codegen 墙钟下降;真正的热点是 `hoist_lowering.py` 的嵌套重写循环和生成 IR 的总字节数。行数是可维护性指标,不是性能指标。

**增长纪律。** 拆分完成后,仓库规则固定为一条不变式:新的 Python 低层化行为必须进入最窄的既有 mixin 或 native 模块,`layer1.py` 不允许再长回去。一个 56 行的 facade 是这条规则的物理执行机制——任何人想往里加方法,diff 本身就是告警。

### 6.1.2 为什么是 mixin

备选方案至少有两个。一是经典 visitor:每类 AST 节点一个 visit 方法,分发表显式。二是独立 pass 管线:把下标、异常、循环各自做成接收 IR、返回 IR 的纯函数 pass。

pcc 选了 mixin,核心理由是**状态共享的成本**。Layer-1 低层化的所有操作共享一个巨大的可变上下文:`self.builder`(IR 构建器及其插入点)、`self.env`(局部变量槽表)、`self.runtime`(运行时函数声明表)、`self._try_err_block`(当前 try 的错误目标块)、`self.loop_stack`、生成器上下文栈、GC 根记录,等等。visitor 与 pass 管线都要求把这套状态打包穿针引线,或者退化成一个传给所有函数的 context 对象——后者就是换了名字的 `self`。mixin 让每个关注点(异常、下标、所有权)在自己的文件里以方法形式直接读写共享上下文,代价是继承链很长。

这个代价在普通 Python 项目里只是审美问题,在 pcc 里却差点是致命问题:**pcc 的类型推断与低层化当时并不理解 mixin**。6.3 节会讲这次事故。这里先立一个本章反复出现的主题:Layer-1 的每一个架构决定都被"这段代码自己要被 pcc 编译"约束着,很多表观上奇怪的源码形态,其实是自托管约束留下的化石。

## 6.2 facade、栈与分发

### 6.2.1 三个文件构成的入口

`layer1.py` 的 facade 只做三件事:从 `layer1_mixins.py` 引入 `L1CodeGenMixinStack`,从 `layer1_entrypoints.py` 引入 `L1CodeGenEntrypointMixin`,把两者合成 `L1CodeGen`。`L1CodeGenMixinStack` 是一个 86 个基类的继承列表,从 `TypedIntAbiMixin` 一直排到 `NativeWeakrefLoweringMixin`,每行一个,顺序即 MRO。

facade 里有一段值得全文引用的注释:

```python
class L1CodeGen(L1CodeGenEntrypointMixin, L1CodeGenMixinStack):
    # Class-local copies are required for the self-hosted stage compiler:
    # several host orchestration paths in layer1.py read these attrs directly,
    # and pcc1 does not yet reliably resolve class attrs through mixin bases.
    _EXTERN_SCAFFOLD_MODULES = EXTERN_SCAFFOLD_MODULES
    ...
```

`layer1_constants.py` 里的模块级常量被逐个复制成类属性,因为 pcc1(被 pcc 自己编译出的编译器)还不能可靠地沿 mixin 基类链解析类属性。这是 6.1.2 末尾那个主题的第一个具体例证:这几行"冗余"代码不是疏忽,是 pcc1 当前能力边界在源码里的投影,删掉它们 host CPython 下一切正常、自举链断裂。

入口 mixin(`layer1_entrypoints.py`)提供 `generate()`、`_emit_stmts()`、`_emit_stmt()`、`_emit_expr()` 四个公共方法,每个都只是一层薄包装:实际工作委托给 `GenerationLoweringMixin._generate_impl`、`StmtDispatchLoweringMixin._emit_stmt_impl`、`ExprDispatchLoweringMixin._emit_expr_impl`。包装层的存在理由是**可诊断性**:当 `PCC_CODEGEN_TRACE` 类机制启用时,每次语句/表达式发射都向一个环形缓冲(`_codegen_trace_ring`)推一条面包屑(模块、函数、语句序号、节点类型、源位置);任何异常逃出低层化时,`_codegen_trace_dump()` 把 `PCC_CODEGEN_EXCEPTION` 头和最近的 `PCC_CODEGEN_BREADCRUMB` 序列打到 stderr。对一个会在编译十万行闭包的第 7 万行崩溃的编译器,这个环比栈回溯有用得多——栈回溯告诉你低层化器的哪个函数崩了,面包屑告诉你它当时在低层化**用户源码的哪一行**。

### 6.2.2 鸭子类型的分发器

`expr_dispatch_lowering.py` 的 `_emit_expr_impl` 是表达式的总分发,它的判别函数长这样:

```python
def _expr_is_subscript(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Subscript) or kind == "Subscript" or (
        _expr_has_attr(expr, "obj") and _expr_has_attr(expr, "idx")
    )
```

`isinstance` 之外还要比对类型名字符串、再退化到 `hasattr` 结构检查——三重判别。在普通 Python 项目里这是代码异味;在这里它又是一块自托管化石:这些判别必须在 pcc1 编译出的二进制里也成立,而 pcc1 对跨模块 dataclass 的 `isinstance` 支持经历过不可靠阶段(参考 6.3.3 的 `dict.get` 事故,同一族问题)。结构检查是语义上的冗余、工程上的保险。`stmt_dispatch_lowering.py` 对语句做同样的事:`_stmt_is_assign(stmt)` 用 `hasattr(stmt, "targets") and hasattr(stmt, "value")` 判定。

分发之下,mixin 群用一个统一的协作协议:**特化低层化函数返回 `Optional[ir.Value]`,`None` 意味着"这个形态不归我管",调用方继续尝试下一个候选,直到落入通用路径或抛 `NotImplementedError`。** `_maybe_emit_literal_str_format`、`_emit_native_weakref_call`、`_maybe_emit_exact_int_object` 全是这个签名。这个协议有一条不成文但严格的纪律:返回 `None` 之前不得发射任何 IR——判定必须发生在产生副作用之前,否则"不归我管"会在 IR 流里留下半截孤儿指令。6.4.5 的 format 低层化是这条纪律的教科书示例。

而严格模式哲学(见第 5 章)在分发器的兜底分支体现:不认识的表达式直接 `raise NotImplementedError("Layer 1 does not handle expression ...")`,不静默回退(fallback)。回退是否可用、可用到什么程度,由 `--python-libpython` 模式决定,而不是由分发器擅自决定。

## 6.3 拆分史:从 20,195 行到 56 行

拆分不是一次重构,是一场跨一个月、两次踩穿自托管地板的战役。完整记录在 [docs/investigations/codegen-mixin-self-cross-module-types.md](../../docs/investigations/codegen-mixin-self-cross-module-types.md) 与 [docs/investigations/layer1-host-helper-context-gap.md](../../docs/investigations/layer1-host-helper-context-gap.md),这里讲主线,因为它直接定义了今天的拆分原则。

### 6.3.1 第一次踩穿:纯重构不纯

2026-05-08,15 个 commit 把 native 模块低层化从 `layer1.py` 移入 11 个 `native_*.py` mixin。在 host CPython 下这是纯重构:同样的行为,同样的 IR。但 no-libpython 自托管闸门(gate)当场变红:`tests/test_fallback_baseline.py` 报告回退计数从基线 0 涨到 1636。

根因在类型推断:`type_infer.py` 给方法的隐式 `self` 参数指派的类型是**方法物理所在的类**。对 `class NativeTextModulesLoweringMixin:` 里的方法体,`self` 的类型就是这个 mixin——一个没有任何字段的空类。于是方法体里每一个 `self.builder`、`self.runtime[...]`、`self._fresh(...)` 都解析失败、塌缩成 `DynType`、低层化成 `py_cpy_getattr` / `py_cpy_call*`——也就是 libpython 回退。11 个 mixin × 每个约 150 个动态调用 ≈ 1636,数字严丝合缝。

调查记录了三个"显然的"解法为何全部不可行:`self: "L1CodeGen"` 前向引用注解(类表是 per-module 的,mixin 模块里查不到 `L1CodeGen`)、`if TYPE_CHECKING:` 导入(类型推断不处理该块,且目标模块构成循环导入)、直接导入(host CPython 层面就是循环)。真正的修复是两层:类型推断侧引入 `derived_class_map`——当一个 mixin 在闭世界闭包里是 `L1CodeGen` 的唯一基类时,其方法的 `self` 推断为 `L1CodeGen`;codegen 侧把 `self.attr` 读、`self.attr = value` 写、`self.method(...)` 调用全部改为**以推断出的接收者类型为准**,而不是物理上正在低层化的 `current_class`。

定位最后一块碎片的过程值得复述,因为它展示了这类 bug 的典型形状。把三个最小的 try-err-block 辅助方法移入 `ExceptionLoweringMixin` 后,pcc1 编 stage2 在某个不相关模块崩溃。决定性的线索是:`_push_try_err_block` 成功而 `_restore_try_err_block` 失败——前者存的是非空块,后者可能存回 `None`,而 `self._try_err_block = prev_err_block` 在接收者类型错误时落入了通用属性存储路径而非具体字段槽。同一个赋值语句,值的取值范围不同,走的低层化路径不同。这种"按值分岔"的失效正是 6.6 节多路径主题的微缩版。

### 6.3.2 第二次踩穿与第二种机制:contextual host param

后续拆分尝试把 `isinstance` 低层化做成普通辅助函数(第一参数 `host` 接收 `L1CodeGen` 实例)时再次踩穿同一块地板:`host` 参数被推断为 DynType,函数体整体回退([docs/investigations/layer1-host-helper-context-gap.md](../../docs/investigations/layer1-host-helper-context-gap.md))。这次的修复是一个新机制:pipeline 对 `pcc.frontends.python.codegen.*` 下首参数名为 `host` 的顶层函数自动启用 `contextual_host_params`,把 `host` 绑定到一个合成的 `L1CodeGen` host 类型;`ir_scaffold_lowering.py` 的语法识别器同步接受 `host.builder.*` 作为 IR scaffold 接收者。从此 Layer-1 有两种合法的拆分形态:mixin 方法(靠 receiver-aware 推断)与 host-param 辅助函数(靠 contextual host 类型),二者都有专门的回退棘轮测试钉住。

### 6.3.3 顺手暴露的语义哑弹:`dict.get` 缺失键

拆分 `isinstance` 低层化时还引爆了一颗与拆分无关、但只在自举链上可见的语义哑弹(记录于 `bootstrap-self-time-after-layer1-split-2026-05-13.md` 的 2026-05-14 更新):pcc1 编译出的编译器把 `_BUILTIN_TYPE_TAGS.get(class_ident)` 的缺失键结果当成整数 `0` 而非 `None`,于是所有用户类的 `isinstance` 检查走进了内建类型标签分支、统一返回 `False`。最小重现只有五行;修复是在自举路径上避开这个脆弱形态,改用显式成员判断:

```python
if class_ident not in _BUILTIN_TYPE_TAGS:
    return None
tag = _BUILTIN_TYPE_TAGS[class_ident]
```

这个案例的教训被沉淀为仓库的写码惯例:在自举关键路径上,不要依赖"缺失返回 None"这类对装箱表示敏感的习语。host 测试全部通过不构成证据——这颗哑弹只有 pcc1→pcc2 阶段能踩响。

### 6.3.4 拆分原则

三条,全部由上述事故反推而来:

1. **每一次拆分都是语义变更的候选,必须过自举闸门。** "纯重构"在自托管系统里是需要证明的命题,不是默认假设。
2. **行数不是性能。** 拆分的正当性来自可维护性与增长纪律;性能要靠剖析(profile)定位真热点(IR 字节量、hoist 重写循环),不能用文件长度代偿。
3. **新行为进最窄的 mixin。** 这条写在 [AGENTS.md](../../AGENTS.md) 里:"choose the narrowest existing mixin or native module; do not grow `layer1.py` again."

## 6.4 代表 mixin 精读

86 个 mixin 不可能逐个讲。下面选五个,每个代表一类设计问题。

### 6.4.1 exception_lowering:checked-call 模型的发射侧

pcc 没有 Itanium 式栈展开:`py_raise(exc)` 把异常存进 TLS 后**正常返回**(运行时侧见第 8 章)。这意味着异常控制流完全由 codegen 在调用点显式编织,而 `exception_lowering.py` 的 `_emit_post_call_err_check()` 是编织的针:

```python
def _emit_post_call_err_check(self, span=None) -> None:
    """After any call that could raise a Python exception, emit
    `if (py_err_occurred()) goto err_target` ..."""
```

错误目标有两级:当前 try 的 err 块(`_current_try_err_block()`),否则函数级错误出口 `_ensure_fn_err_exit()`——按返回类型发射哨兵值(指针返回 NULL、整数返回 0、`main` 特殊处理:打印未处理异常并返回 1)。`_emit_try` 把 try/except/else/finally 全部展开成基本块图:err 块里依次对每个 handler 调 `py_exc_matches` 测试,全不匹配则跳外层 err 目标;handler 绑定名(`except E as e`)需要 retain 并登记进 `_except_binding_names`,这个登记直接关系到 GC 帧根(见第 10 章的 exc-referent 案例研究)。

两个细节展示了这套模型的成本意识。其一,带源位置的 err 检查会先经过一个记录异常帧(traceback 行)的中间块,`_ensure_post_call_frame_block` 按(函数、目标、位置)做键去重——否则每个调用点都长出一个独立帧块,IR 体积失控。其二,`_emit_post_call_err_check` 在 `@c_abi_export` 标记的运行时函数体内被**抑制**:这些函数可能在 TLS 已有挂起异常时被调用(比如 except 分发期间),一个不分场合的检查会把别人的异常误判成自己的;运行时函数沿用 C 运行时的显式 NULL 返回协议。

这里产生本章最重要的低层化义务:**任何可能 raise 的运行时调用之后,发射点必须插入 err 检查(或等价的 NULL 路由)。** 漏掉的症状极具迷惑性——不是崩溃,而是异常"迟爆":跳过本应捕获它的 try/except,在下一个恰好有检查的位置、甚至进程收尾时才出现。6.6.3 的审计就是对这个义务的系统化清账。

### 6.4.2 subscript_lowering 与 exact_int_lowering:同一个 `d[k]`,两条路径

`subscript_lowering.py` 的 `_emit_subscript_load()` 是下标读取的"正路":按 `expr.obj.ty` 分支——`ListType` 调 `py_list_getitem`(IndexError)、`DictType` 调 `py_dict_getitem`(KeyError),两者都跟 `_emit_post_call_err_check`;`TupleType`、`StrType`、bytes 族、`DynType` 各有分支;结果经 `_coerce_from_object` 按静态元素类型拆箱。

但它不是唯一的路。`exact_int_lowering.py` 存在的理由是义务 7(见第 16 章):pcc 的 `int` 是任意精度**语义**类型,值投影(projection)是 i64 通道,对象投影是装箱大整数。当一个 int 表达式必须以精确对象形态参与运算——字面量超出 i64、`**` 运算、或环境标记表明变量已在对象车道——`_maybe_emit_exact_int_object()` 接管,在对象车道上用 `py_int_add` / `py_int_floordiv` 等运行时函数计算。而当这样的表达式是一个下标读取时,这条路径有**自己的**下标低层化:`_emit_subscript_load_object()`,内部同样按 List/Tuple/Dict/Dyn 分支、同样调 `py_dict_getitem` 加 err 检查。

于是同一个源码形态 `d[k]`,按消费上下文走两个文件里的两个函数:`x = d[k]` 走 `_emit_subscript_load`,而 `print(d[k])` 这类对象上下文(`print_lowering.py` 先尝试 `_maybe_emit_exact_int_object`)走 `_emit_subscript_load_object`。在 IR 里二者可以凭调用结果的名字区分:前者是 `dict.getitem`/`list.getitem`,后者是 `dict.getitem.obj`/`list.getitem.obj`。这一对名字是调试这类问题时最廉价的判别工具,而这个双路径结构本身,是 6.6.1 案例研究的舞台。

### 6.4.3 for_loop_lowering:一个语义,N 个特化

`for_loop_lowering.py` 的 `_emit_for()` 是一张特化分派表。进入分派前先做三次归一化:for-else 去糖(引入 `__forelse_broke__` 标志,重写 body 里的每个 `break` 为"置位+break",循环后用原生 i1 比较守卫 else 体——注释明确解释为什么不走 Python 级布尔比较:会装箱)、`enumerate`/`zip` 重写为索引迭代、元组目标重写为标量目标加解包赋值。然后按迭代器形态分派:`range(...)` 走 i64 归纳变量快道;CPython 值走 `_emit_for_cpython_iter`;`ListType`/`TupleType` 走长度加按索引取元素;`DictType` 物化 `py_dict_keys` 复用列表路径;`StrType` 按码点切片;带 `__next__` 的已知用户类走 `_emit_for_native_iterator`;`DynType` 走通用对象迭代协议 `_emit_for_obj_iterator`。

通用协议路径的块结构值得细看,因为它是"err 检查的等价形态"的标准样本。`py_obj_next` 返回 NULL 有两种含义:迭代正常耗尽(StopIteration)或迭代器真的抛了异常。低层化为:

```text
for.obj.next:      item = py_obj_next(it); item == NULL ? maybe_end : body
for.obj.maybe_end: py_exc_matches(cur_exc, StopIteration) ? clear : propagate
for.obj.clear:     py_clear_exception(); br end
for.obj.propagate: br <try err block 或 err.exit>
```

`maybe_end`/`propagate` 这对块名后来成了 err-check 审计(6.6.3)里"已审查等价路由"的识别签名。

特化还要与生成器(generator)语义交叉:生成器体内的 `range` 循环**禁止**走归纳变量快道,因为快道把计数器放在裸 entry-block alloca 里,不属于持久化的生成器帧,`yield` 之后 resume 会拿到一个被重置的计数器、循环一轮即终。源码注释把这个 bug 与对应回归测试(`test_generator_range_loop_resumes`)钉在分派点上。这是特化结构的固有税:每条快道都要逐一论证自己与每个语义维度(生成器、异常、GC)的交互。

### 6.4.4 ownership_lowering:拥有/借用判定的发射侧

运行时的引用契约(调用返回拥有引用,被调方为借用值 retain——见第 9 章)需要 codegen 在每个消费点做出静态判断:这个值我是否拥有、用完是否该释放。`ownership_lowering.py` 的核心是判定函数 `_expr_returns_owned_object()`:调用表达式按被调方返回类型判定;列表/字典/元组/字符串字面量与下标结果拥有;裸 `Name` 引用借用;`self.attr` 读取按字段声明类型判定。消费侧用 `_gc_release_if_owned(obj, source_expr)` 平衡——它把"值是指针、源表达式拥有、不是 CPython 桥接值"三个条件全过了才发射 `pcc_gc_release`。

另一半职责是 GC 根:`_mark_owned_local_if_object()` 把拥有对象的局部变量登记进 `_owned_local_names` 并通过 `_ensure_owned_local_gc_root()` 注册为帧根(槽位粒度的 `pcc_gc_frame_enter`,五后端共用一套契约,见第 10 章)。一个容易漏的角落:函数级错误出口也要对已根化的局部做离开处理,`_ensure_fn_err_exit` 末尾对 `_gc_rooted_local_names` 逐个调 `_patch_fn_err_exit_gc_root_leave`——异常路径上的帧根泄漏不会在 host 测试里现形,只会在追踪式 GC 后端的长跑里变成悬挂根。

发射侧错判的两个方向各有报应:把借用当拥有,多释放,use-after-free;把拥有当借用,漏释放,泄漏。[AGENTS.md](../../AGENTS.md) 的自举回归纪律第 5 条因此专门规定:所有权失效要先核对被调方/调用方契约,修复方向是**被调方 retain**,而不是让调用方停止释放。

### 6.4.5 format_lowering:编译期解析与"无副作用退出"

`format_lowering.py` 展示 `Optional` 协议最干净的用法。`_maybe_emit_literal_str_format()` 处理 `"...{}...".format(args)`:`_parse_auto_format_literal()` 在**编译期**解析格式串——花括号转义、字段引用分类(auto/index/name)、格式说明符切分,并执行 CPython 的规则"自动编号与手动编号不得混用"(混用返回 `None` 而不是猜一个语义)。解析成功则整个 format 调用低层化为 `py_obj_format` 加 `py_str_concat` 的串联,失败则返回 `None` 由调用方走通用路径。

支撑它的 `_resolve_str_literal_value()` 体现了"为真实代码模式扩特化"的方法论:格式串经常不是字面量而是常量变量(`fmt = "{x}"; fmt.format(...)`),该函数在当前函数体、再到模块体里寻找**唯一一次** `Name = StrLit` 绑定,任何其他形态的重绑定(AugAssign、for 目标、except 绑定、with-as)立即放弃。docstring 注明这个习语的动机来自 NumPy 的 `numpy/__init__.py`——特化的扩张方向由真实包驱动,而实现保持通用,不含包名特判(义务 3)。

`_emit_format_spec_builtin()` 给 `format(v, "08x")`、`","`、`".3f"` 等常见说明符开快道,直接调 `py_int_format_hex` / `py_int_format_decimal` / `py_float_format_fixed`。一个微小但典型的注释:千分位分组参数传的是分隔符**字节值**(44 是 `,`,95 是 `_`),"传裸 1 会让运行时输出 chr(1)"——这类注释存在的原因是它们都曾经是 bug。

## 6.5 native_*.py:原生模块低层化

12 个 `native_*.py` 文件(asyncio、dataclasses、files、gc、math、modules、os、system、text_modules、threading、virtual_thread、weakref)处理"import 一个标准库模块然后调用它"的低层化。统一模式以 `native_weakref.py` 为样本:

```python
def _emit_native_weakref_call(self, expr: Call) -> Optional[ir.Value]:
    attr = expr.func
    if (not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "weakref"):
        return None
    return self._emit_native_weakref_value_call("weakref." + attr.name, ...)
```

先经 alias 表确认接收者真的是该模块(import 别名、from-import 都归一到这张表),再按 `"模块.成员"` 字符串分派到具体运行时调用;不认识的成员、不支持的参数形态一律返回 `None`。`native_modules.py` 是这群文件的总调度与杂项仓库,里面还有一类纯编译期折叠:`string.ascii_lowercase` 这类常量直接内联为字符串字面量,`codecs.BOM_*` 内联为 bytes——模块根本不需要在运行时存在。

native 模块低层化与"生态支持必须通用"(义务 3)的关系需要说清楚:这里按**模块名**分派,表观上像特判,但它特判的是标准库语义的低层化目标(weakref 对应 `py_weakref_new`,gc 对应 `pcc_gc_*`),属于编译器对语言运行时的认识,而不是对某个第三方包的偏袒。禁止的是 `if package == "numpy"` 这种为通过闸门而做的包名分支;第 17 章的包机制走的是完全不同的通用路径。

`native_weakref.py` 里 `weakref.ref` / `weakref.proxy` 的构造调用后各跟一条 `_emit_post_call_err_check`,注释写明:`py_weakref_new` 可能 raise(对值类载荷抛 TypeError),漏掉检查则挂起异常跳过外层 try/except。这两行就是 6.6.3 审计的第一批真阳性修复,native 模块群正是 err-check 义务最容易被遗忘的地带——每个文件几十个发射点,作者各异,时间跨度数月。

## 6.6 历史与教训

### 6.6.1 双下标路径:改一半等于半失效(2026-05-30)

**症状。** 严格 no-libpython 模式(`--backend self --python-libpython=off`)下,`d['missing']` 不抛 KeyError 而是打印 `<null>`;`a[9]` 越界同样静默。外层 `try/except KeyError` 一无所获。CPython 对照(调试手册的参照系技法)立即确认这是语义缺陷而非未实现特性:程序"正常"跑完,输出错。

**根因。** 静态类型下标路径当时调用的是 `py_dict_get` / `py_list_get`——运行时的**非 raise**查询原语,缺失/越界返回 NULL。codegen 既不检查 NULL 也不期待异常,NULL 一路流进 print。

**修复与陷阱。** 修复([tests/python/test_native_subscript_raise.py](../../tests/python/test_native_subscript_raise.py) 的 docstring 是权威记录)分三层:运行时新增 raise 变体 `py_dict_getitem`(携带键的 KeyError,经 `py_exc_new_with_value`)与 `py_list_getitem`(IndexError),C 实现(`py_dict.c` / `py_list.c`)与 pcc-Python 端口(`py_dict.py` / `py_list.py`)双镜像(见第 14 章的镜像纪律);`py_dict_get` / `py_list_get` 保留非 raise 语义,因为 `dict.get()` / `pop()` / `setdefault()` 及一批内部调用者依赖它。陷阱在前端:下标读取有**两条**低层化路径(6.4.2)。只把 `subscript_lowering.py` 的 `_emit_subscript_load` 切到新原语,`x = d[k]` 修好了,而 `print(d[k])`——exact-int 对象上下文,走 `exact_int_lowering.py` 的 `_emit_subscript_load_object`——依旧调旧原语、依旧 `<null>`。一半的修复在表面观察下与零修复难以区分,因为最常见的验证形态(赋值后打印)恰好可能只覆盖其中一条路径。最终两个函数都切到 `py_*_getitem` 并都插 `_emit_post_call_err_check`,两处源码留下了几乎逐字相同的注释(`was py_dict_get, which returned NULL silently -> "<null>"`)——这种刻意的注释对仗就是给下一个修改者的路径耦合警示。

**复发证明。** 这不是一次性事故而是结构性税。2026-06-05 的值类(value class)键投影修复([docs/current-goal-state.md](../../docs/current-goal-state.md) 当期记录)再次同时触及两处:"`subscript_lowering.py` and the object-form helper in `exact_int_lowering.py` now route direct valueclass constructor dict/object getitem keys through ValueBox projection"。任何流经下标键或下标值的新语义,默认假设两条路径都要改。

**留下的不变式。** 回归测试 [tests/python/test_native_subscript_raise.py](../../tests/python/test_native_subscript_raise.py) 在默认运行时模式(链接 pcc-Python 端口,而非 `PCC_RUNTIME_CC=cc`)下验证两种形态;IR 调用名 `dict.getitem` 与 `dict.getitem.obj` 作为路径判别器写进了团队记忆。

### 6.6.2 六条除法低层化路径(2026-05-30)

**症状。** 同一天、同一种方法论(真实程序对 CPython 的输出 diff)发现的更深一层:严格 no-libpython 下,`10 // 0` 返回 `0`、`10 % 0` 是未定义行为、dyn 路径的 `%` 打印 `<null>`、`try/except ZeroDivisionError` 整体失效。这直接违反义务 2:静默返回错误值不是性能取舍,是语义腐蚀。

**根因不是一个,是六个。** 调查([docs/investigations/zero-division-silent-no-libpython-six-paths.md](../../docs/investigations/zero-division-silent-no-libpython-six-paths.md))枚举出整数/浮点除法在前端的全部低层化路径:① 未装箱 i64 的 `//`/`%`(ARM64 `SDIV` 除零静默得 0);② 装箱运行时双目路径;③ exact-int 装箱路径(`py_int_floordiv`/`py_int_mod` 除零返回 NULL 且不设异常——运行时注释把 raise 责任推给调用方,而没有任何调用方接);④ dyn 对象路径 `py_obj_mod`;⑤ 浮点 `fdiv`/`fmod`(得 inf/nan,不陷入);⑥ low_ir 纯叶子 scaffold——为"已证纯函数"生成的无错误出口快道(`post_call_error_check=None`),发射裸 `sdiv` 而**结构上不可能 raise**。

**修复的非对称性。** 前五条路径的修法同构:除零守卫(`_emit_zero_division_check`)或 NULL 后验(`_emit_zero_division_if_null`),分布在 `binary_op_lowering.py`、`expr_helper_lowering.py`、`exact_int_lowering.py`。第六条在结构上无法修——没有错误出口的快道发射不了 raise——所以解法是**排除**:`_low_ir_nonzero_literal()` 只在除数是可证非零的字面量(`x // 2`、`n % 256`)时保留快道,变量除数令整个函数退回有守卫的完整低层化。这是处理特化快道与语义义务冲突的一般原则:**快道不能履行义务时,收缩快道的准入条件,而不是给义务打折。** 热路径上的常量除数性能分文未损。

**教训。** 当同一语义散布在 N 条路径上,修复的单位是路径集合,不是出 bug 的那条;调查产出的第一件工件应当是路径枚举,而不是补丁。回归测试([tests/python/test_native_zero_division.py](../../tests/python/test_native_zero_division.py))按路径触发形态参数化:内联 dyn、跨函数 typed、大整数、浮点 `//`、以及验证常量除数快道仍在的反向断言。本仓的下标(两条)、除法(六条)、浮点转字符串(四条以上)反复印证:多路径是特化型编译器的常态成本,对抗手段只有路径清单加全路径回归。

### 6.6.3 从逐点修补到系统审计:emission-site err-check audit(2026-06-11)

**起因。** 一晚之内三个独立 bug 共享同一形状——运行时函数 `py_raise` 了,发射点没有 `_emit_post_call_err_check`,异常跳过 try/except 迟爆(native weakref 构造、弱字典下标存储、生成器 `throw`)。三连击之后,正确的反应不是修第四个,而是把这一类清账([docs/investigations/emission-site-err-check-audit.md](../../docs/investigations/emission-site-err-check-audit.md))。

**方法。** 两步机械扫描:收集 C 运行时里体内含 `py_raise(` 的函数(78 个);对 [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) 里每个 `self.runtime["<fn>"]` 发射点,若其后 8 行内没有 err 检查则标记——58 个嫌疑点。然后是审计的关键纪律:**嫌疑不是 bug。** 每个家族先写"红探针"(在 CPython 下确认期望行为,在 pcc 下确认错误行为),才允许动代码。结果分布很有教育意义:`py_obj_next` 家族 5 处全是假阳性(就是 6.4.3 那套 `maybe_end`/`propagate` 等价路由,8 行窗口扫不到);正则引擎家族 3 处假阳性(检查在多行参数列表之后,窗口太短);双目运算家族是真阳性×2——而且红探针顺带挖出更深的运行时洞:`py_obj_add/sub/mul` 压根没有派发用户 `__add__` dunder;生成器 `throw/close` 家族真阳性,且新插的检查立即暴露 `py_gen_close` 把注入的 GeneratorExit 留在 TLS 里的第二个洞。

**教训。** 第一,漏 err 检查的症状(迟爆)使它天然抗拒逐例发现,值得周期性机械审计;第二,审计启发式的假阳性模式要回写进审计文档(块名签名 `maybe_end`/`propagate` = 已审等价路由;扫描窗口应到语句结束而非固定行数);第三,在检查缺失的地方补上检查,经常会让下游更深的运行时洞当场现形——检查不只是修 bug,是让 bug 可见的仪器。

## 6.7 小结

Layer-1 codegen 的物理形态——56 行 facade、86 个 mixin、12 个 native 模块、鸭子类型分发、类属性复制——没有一处是凭空的审美选择,几乎全部由两个力共同塑造:语义密度(Python 的每个表达式形态都需要显式的基本块编织)与自托管约束(这段代码必须被它自己描述的那个编译器编译)。拆分史证明在自托管系统里"纯重构"是需要闸门证明的命题;双下标与六条除法路径证明特化快道的真实成本不在写,而在每一次语义演进都要乘以路径数;err-check 审计证明对"症状迟爆"的 bug 类,系统化清账优于逐例扑救。这一层留给后续章节两条线:checked-call 异常模型的运行时一半在第 8 章,所有权契约的运行时一半在第 9 章。

## 练习

1. **读源码验证。** 对比 `subscript_lowering.py::_emit_subscript_load` 与 `exact_int_lowering.py::_emit_subscript_load_object`:列出二者在 err 检查、`_gc_release_if_owned`、结果拆箱(`_coerce_from_object`)上的全部差异。两个函数的 `TupleType` 分支都没有跟 err 检查——到运行时源码([pcc/runtime/src/](../../pcc/runtime/src))里查证 `py_tuple_get` 的越界行为,判断这是等价路由、刻意豁免,还是一个待修的洞。
2. **IR 取证。** 写一个同时含 `x = d["k"]` 与 `print(d["k"])` 的小程序,用 `--emit-llvm` 在严格模式下编译,在 IR 中找出 `dict.getitem` 与 `dict.getitem.obj` 两个调用,并标出各自后随的 `py_err_occurred` 检查块。
3. **设计权衡。** 提出一个把双下标路径合并为单一共享 helper 的重构方案。指出至少三个阻力点(提示:返回值的拆箱需求不同、所有权释放点不同、exact-int 路径对键的对象化要求),并论证你的方案如何在不增加第三条路径的前提下通过五 GC 自举闸门。
4. **审计实践。** 在 `for_loop_lowering.py` 中找到 `_emit_for_obj_iterator` 的 `maybe_end`/`propagate` 块结构,解释为什么 `py_obj_next` 之后没有 `_emit_post_call_err_check` 不构成 6.4.1 义务的违反;再到 `comprehension_lowering.py` 里找出同构结构,评估两处是否可能漂移。
5. **论证题。** 6.3 节给出了两种合法拆分形态:mixin 方法(receiver-aware 推断)与 host-param 辅助函数(contextual host 类型)。从类型推断成本、可测试性、对 pcc1 能力边界的暴露面三个维度比较二者,并论证 `isinstance_lowering.py` 当初为什么适合后者。
