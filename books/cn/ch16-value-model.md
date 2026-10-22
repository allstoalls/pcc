# 第 16 章 值模型:投影而非定宽

前面的章节建立了 pcc 的对象世界:每个值是一个带头部的堆对象,引用计数与五个 GC 后端管理它的生死(第 7、9、10 章)。这个世界语义完备,但对热路径而言代价高昂——一次 `Point(1, 2)` 是一次分配、一个对象头、两个装箱的字段、若干次间接寻址。值模型(value model)是 pcc 对这笔身份税的回答,也是七义务中第 7 条的落点。它的核心立场可以压缩成本章标题:**借投影(projection),不借定宽**。从 Java Project Valhalla 借来的是"语义类型与物理表示分离"的投影模型;明确拒绝借的,是 Java 把 `int` 定为 32 位回绕整数的那个历史决定。本章讲 `int` 的双投影(标记小整数通道 + boxed bignum)、显式机器整数类型的契约、`@pcc.valueclass` 的实现现状,以及——按本书的诚实义务——一个已确认缺陷从发现、裁定到修复(2026-06-17)的完整档案:typed-int 未装箱算术曾在 i64 溢出时静默回绕。

## 本章导读:投影而非语义替换

这一章最重要的防误解点是:值模型不是把普通 Python 类偷偷改成没有 identity 的结构体,也不是把 Python `int` 改成会溢出回绕的机器整数。它只是给一部分明确 opt-in 的热路径一个更紧凑的物理表示。

- 普通类保留 identity、`__dict__`、weakref、finalizer、动态属性和继承。
- value class 是显式选择的 identity-free payload,需要清楚的 boxing/unboxing 边界。
- Python `int` 的语义仍是任意精度;小整数通道只是物理投影,溢出必须提升或 deopt。

## 16.1 问题与设计空间

把问题摆正:Python 的 `int` 在语义上是任意精度整数,`2**40 * 2**40` 就是 `2**80`,没有商量余地。Python 的对象在语义上有身份:`id()` 稳定、`is` 可判、可以挂弱引用、可以塞 `__dict__`、可以被子类化、死时可以跑 `__del__`。一个想把 Python 编译成原生代码的编译器,面对这两条语义,设计空间里有三类答案。

**备选一:把 `int` 直接定为机器整数。** Cython 的 `cdef long`、mypyc 的原生 int 走的方向:`int` 类型的值就是 i64,加法就是一条 `add` 指令。快,且实现简单。代价是语义被换掉了——i64 溢出时回绕,`mul(2**40, 2**40)` 得 0,与 CPython 静默分歧。这正是 Java 的 `int`:为性能把定宽回绕写进语言语义。pcc 的北极星禁止这个方向:义务 2 写明"性能必须被证明……当快路径假设失效时,慢路径必须保持 Python 语义";义务 7 进一步点名"值通道溢出必须 deopt/promote(去优化/提升),决不回绕"。

**备选二:一切装箱。** 每个 `int` 都是堆上的 bignum 对象,每次加法都走运行时调用。语义无懈可击,性能退回解释器量级,义务 7 承诺的"热路径性能桥"落空。

**备选三:投影模型。** 这是 pcc 从 Valhalla 蒸馏出的答案,写在 [goal-prompt.md](../../docs/goal/goal-prompt.md) 的 V-track 一节:**语义类型与物理表示分离**。一个语义类型可以有两个物理投影——值投影(value projection)与对象投影(object projection)——由编译器与运行时在显式的接缝处切换;优化只许更换表示,不许更换语义。具体到三个类型:

```text
Python int        语义 = 任意精度,永远
  ├── 值投影:   标记小整数通道(tagged small-int lane,约 i63)
  └── 对象投影: boxed bignum(PyIntObject)
      值投影溢出 → deopt/promote 到对象投影,决不回绕

pcc.i64 / pcc.u64  语义 = 显式机器整数(契约,尚未实现,见 16.4)
  └── 裸 i64/u64 投影;wrap/trap/checked/saturating 写进类型
      ——定宽回绕唯一合法的住所

@pcc.valueclass C  语义 = 无身份的不可变载荷
  ├── 值投影:   LLVM 聚合载荷(如 {i64, i64})
  └── 对象投影: ValueBox(PY_TYPE_VALUEBOX)
      字段语义跟随字段类型:int 字段 = Python bigint
```

投影模型有一个不显眼但重要的推论,Valhalla 的 JEP 402 也承认同样的事实:**接缝是真实存在的,不要假装没有**。一个 `int` 值可能此处内联、彼处装箱;一个值类载荷可能此处是寄存器里的聚合、彼处是堆上的 ValueBox。pcc 不假装两者不可区分,而是把切换点做成显式的、可审计的:溢出提升是显式分支,装箱/拆箱是显式发射(emission),身份观察是显式诊断(16.5)。本章其余部分就是沿着这三条接缝走一遍源码,最后在 16.3 停在那条没有兑现接缝义务的路径上。

## 16.2 `int` 的双投影:标记小整数通道与 boxed bignum

### 16.2.1 编码:一个低位换一个对象头

值投影的运行时编码在 [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h)。`PY_IS_TAGGED_INT(p)` 检查指针低位:低位为 1 是值,为 0 是真正的 `PyObject*`——`malloc` 在所有目标平台上至少 8 字节对齐,真实指针的 bit 0 恒为 0,这一位是白捡的。编码与解码各一行:`py_tag_int()` 左移一位再置低位;`py_untag_int()` 经 `intptr_t` 做算术右移,保符号。于是标记整数的载荷是 63 位:

```c
#define PY_TAGGED_INT_MIN  ((int64_t)INT64_MIN >> 1)   /* -2^62 */
#define PY_TAGGED_INT_MAX  ((int64_t)INT64_MAX >> 1)   /*  2^62 - 1 */
```

这一个低位买到的东西值得列全:无分配(值就在指针位形里)、无对象头、无引用计数(第 9 章里 `py_incref`/`py_decref` 的快速路径之一就是 `PY_IS_TAGGED_INT` 直接返回)、无 GC 参与。代价是每个消费 `PyObject*` 的运行时入口都必须先问一句"这是指针还是值"。

对象投影是 `PyIntObject`,同文件定义:符号-数值(sign-magnitude)表示的 bignum,基 2^32 的数字数组按小端存储,`sign` 取 -1/0/+1,柔性数组 `digits[]` 长 `ndigits`。注释写明两条规范不变式:`sign == 0` 当且仅当 `ndigits == 0`(零没有数字);`sign != 0` 时最高位数字非零。第三条不变式更关键,直接写在结构体注释里:**落在标记范围内的值应当存为标记整数,不该存为 `PyIntObject`**。表示是规范化的——同一个数学值只有一种合法编码,等值比较与哈希因此不必处理双表示。

规范化由两个函数执行,都在 [pcc/runtime/src/py_int_core.c](../../pcc/runtime/src/py_int_core.c)。`py_int_from_i64()` 是构造侧的选择器:在标记范围内就 `py_tag_int`,否则 `py_bigint_from_i64`(最坏两个数字,`INT64_MIN` 经无符号路径安全取负)。`py_bigint_to_pyobject()` 是计算侧的坍缩器:bignum 算出来的结果若能放回标记范围,就释放 bignum、返回标记值。提升与坍缩双向都有,值不会单向漂去对象投影。

### 16.2.2 运行时算术:溢出即提升

[pcc/runtime/src/py_int_ops.c](../../pcc/runtime/src/py_int_ops.c) 是对象层算术分派,每个操作都是同一个形状——快路径试值投影,失败就提升到对象投影:

```c
PyObject *py_int_add(PyObject *a, PyObject *b) {
    if (is_tagged_both(a, b)) {
        int64_t av = py_untag_int(a);
        int64_t bv = py_untag_int(b);
        int64_t r;
        if (!__builtin_add_overflow(av, bv, &r)) {
            return py_int_from_i64(r);
        }
    }
    PyIntObject *ba = promote_any(a);
    PyIntObject *bb = promote_any(b);
    ...
    PyIntObject *br = py_bigint_add(ba, bb);
    ...
    return wrap_bigint(br);
}
```

逐行就是投影模型:两操作数都是标记值时用 `__builtin_add_overflow` 做带检查的 i64 加法——注意检查的是 i64 溢出,而结果经 `py_int_from_i64` 还会再做标记范围判定,所以 i63 与 i64 之间的"夹层"值也正确落到堆上;任一检查失败,`promote_any()`(即 `py_bigint_from_any`)把两边都提升为 bignum,`py_bigint_add`([pcc/runtime/src/py_int_addsub.c](../../pcc/runtime/src/py_int_addsub.c) 的符号-数值加减)算出精确结果,`wrap_bigint()` 经 `py_bigint_to_pyobject` 坍缩回去。`py_int_sub`/`py_int_mul` 同构;`py_int_neg` 单独防 `INT64_MIN`(它的相反数恰好放不进 i64)。

几个操作的快路径里藏着语义修正,值得点名。`py_int_floordiv`/`py_int_mod`:C 除法向零截断而 Python 向下取整,余号随除数,快路径里有显式的商减一/余加除数修正——操作数已知在标记范围内,修正本身不会再溢出,注释证明了这一点。`py_int_shl`:左移用"乘 2^n 带溢出检查"实现,溢出则走 `py_bigint_shl`——16.3 会回头看它的未装箱镜像是怎么把这条语义丢掉的。`py_int_truediv` 返回 `PyFloatObject`,除零返回 NULL 留给调用方升 `ZeroDivisionError`。

文件群的划分本身是个设计决定。`py_int_core.c`、`py_int_ops.c`、`py_int_addsub.c`、`py_int_mul.c`、`py_int_convert.c`、`py_int_bigint_convert.c`、`py_int_parse.c`、`py_int_decimal.c` 各自的文件头注释都写着同一句话的变体:"split from py_int.c so the pcc-Python runtime can replace it independently"。每个 C 文件在 [pcc/runtime/py/](../../pcc/runtime/py) 下有同名 pcc-Python 端口(`py_int_core.py`、`py_int_ops.py`……),这是第 14 章讲的"C 语义运行时收缩、pcc-Python 运行时增长"迁移在整数子系统上的切片:拆得越细,可独立替换、独立验证的单元就越小。`py_int_mul.c` 的头注释还留下一条诚实的边界记录:教科书乘法的 `uint32*uint32` 中间值需要完整的无符号 64 位行为,这是当时 pcc-Python 表面不易表达的,所以乘法比加减晚拆出去。

### 16.2.3 生成代码里的值通道:内联标记快路径

运行时层的双投影解决了正确性;性能要求把值投影内联进生成代码,省掉函数调用。这一步在 [pcc/frontends/python/codegen/binary_op_lowering.py](../../pcc/frontends/python/codegen/binary_op_lowering.py) 的 `_emit_inline_tagged_int_binop_or_call()`:当 `int` 表达式按装箱表示流动时(`_int_exprs_are_boxed()` 为真,此时 `IntType` 的存储类型是 `PyObject*`),`+`/`-`/`&`/`|`/`^` 不直接发射运行时调用,而是发射一段内联 CFG:

```text
ptrtoint 两操作数 → 各测低位 → and → cbranch
fast 块:  ashr 1 拆标记 → add/sub/and/or/xor
          (+/- 再测结果 ∈ [-2^62, 2^62-1],不合中途跳 slow)
          shl 1 | 1 重新打标 → inttoptr → join
slow 块:  call py_int_add/...(bignum 能力齐全)→ join
join 块:  phi 合流
```

这段 IR 就是值投影的编译器形态:快路径完全在寄存器里,没有分配、没有调用;`+`/`-` 的结果范围检查是 deopt 点——放不回 63 位就把**原始的装箱操作数**交给慢路径重算,慢路径产出合法的标记值或 bignum,语义与 CPython 逐位一致。位运算 `&`/`|`/`^` 在 63 位上封闭,无需检查。`*` 不在内联名单里:两个 63 位数的乘积需要 126 位中间值,内联检查的代价结构不同,当前留给运行时调用(`py_int_mul` 的 `__builtin_mul_overflow` 路径)。

慢路径调用之后还有两道善后,都在 `_emit_runtime_int_binop_value()`:可升异常的操作(移位)发射 `py_err_occurred()` 检查(第 8 章的低层化义务);`//` 与 `%` 的 NULL 结果经 `_emit_zero_division_if_null()` 升 `ZeroDivisionError`——运行时注释明确把除零的 raise 推迟给调用方,前端必须接住。

到此为止,装箱表示一侧的 `int` 投影是完整且诚实的:值通道有,溢出提升有,慢路径语义完备。问题出在另一侧。

## 16.3 一个缺陷的完整档案:typed-int 未装箱算术曾在 i64 溢出时静默回绕

本节是这一章的诚实义务。以下缺陷于 2026-05-30 由外部审计标记、当日在仓库内复现确认,调查记录在 [docs/investigations/typed-int-unboxed-overflow-silent-wraparound.md](../../docs/investigations/typed-int-unboxed-overflow-silent-wraparound.md),**并于 2026-06-17 修复**(16.3.4)。本节保留从症状到修复的全程,作为声明卫生的工作范例。

### 16.3.1 症状与精确触发面

先说什么**不**触发。大字面量与增长的累加器不触发:`x = 2**63 - 1; x = x + 1` 在 strict no-libpython、self 后端下输出与 CPython 逐字节相同——类型推断把这些值路由到了 16.2 的装箱路径,IR 里是 `call @py_int_add`。

触发的是**显式 `int` 注解的函数参数**:

```python
def mul(a: int, b: int) -> int:
    return a * b

print(mul(1099511627776, 1099511627776))   # 2^40 * 2^40
# pcc:     0
# CPython: 1208925819614629174706176
```

pcc 打印 0——2^80 mod 2^64。同样确认回绕的还有 `+`(`addf(2**63 - 1, 5)` 得负数)、溢出值穿过函数返回 ABI、穿过局部槽位,以及 `<<`(裸 i64 `shl` 对移位数取模,`1 << 100` 算成了 `1 << 36`)。`-` 与 `a*b > <大字面量>` 比较在 2026-05-31 的探测中已走装箱路径,是正确的——缺陷面是 `+`/`*`/`<<` 这三个操作穿过 typed-int ABI 的路径,不是整数算术的全部。

### 16.3.2 根因:把语义类型钉死在机器表示上

因果链在源码里是三段,全部确认:

1. [pcc/frontends/python/codegen/typed_int_abi.py](../../pcc/frontends/python/codegen/typed_int_abi.py) 的 `_type_is_typed_int_abi_param()` 对 `IntType` 无条件返回真——`a: int` 注解使参数获得 i64 原生 ABI,函数签名定型为 `define external i64 @user_..._mul(i64 %a, i64 %b)`。
2. `binary_op_lowering.py` 的 `_emit_binop_value()` 整数尾部:`lv = _to_int64(lhs); rv = _to_int64(rhs); return self._emit_binop_int(op, lv, rv)`。
3. `_emit_binop_int()` 对 `+`/`-`/`*` 直接发射 `builder.add`/`builder.sub`/`builder.mul`——裸 i64 指令,无溢出检查,无慢路径。

用投影模型的语言说:这条路径把 Python 的**语义类型** `int` 等同于**机器表示** i64,值投影没有 deopt 点,溢出即回绕。这恰好是 16.1 备选一——被北极星明文禁止的 Java `int` 方向——经由 typed-ABI 的后门溜了进来。[goal-prompt.md](../../docs/goal/goal-prompt.md) 的 V-track 把这条路径点名为"投影模型禁止的那种混淆"。

值得强调它为什么不是局部补丁能修的,因为调查里有两次失败实验把这一点钉死(2026-05-31):把 `*`/`<<` 从 `_typed_int_expr_is_i64_safe` 的安全集合里剔除,无效果;把 `*` 从 `_expr_is_native_typed_int_shape` 剔除,也无效果。原因是表示约束:一个正确的 bignum 结果**装不进 i64 返回寄存器**。只要 `mul` 的签名还是 `i64(i64, i64)`,任何分析层的收紧都改变不了结果无处安放的事实。修复必须是表示/ABI 级的:`int` 参数、返回值与槽位从 i64 改为可携带标记值或 bignum 的 `PyObject*`。

### 16.3.3 设计张力:两个候选方案与各自的真实代价

调查记录了两个候选与一次代价反转,本书如实转述。

**方案一:带溢出检查的快路径 + 装箱提升。** `_emit_binop_int` 改用 `llvm.sadd/ssub/smul.with.overflow`,溢出位起跳到 `py_int_*` 慢路径;结果表示改为标记整数(i63 内联,溢出装箱),与 16.2.3 的内联快路径同构。语义正确,且保住未装箱快通道——溢出分支在常见情形下是预测不跳转的。代价是实施面:typed-int 的结果表示、局部槽位、返回 ABI、调用方约定全要动,是共享代码生成 + 自举关键路径上的子工程。

**方案二:保守装箱。** 只在可证明不溢出的上下文(有界循环计数器、range 索引)保留裸 i64,`int` 参数等任意来源的值一律走装箱路径。最初被选为立即修复——更简单、确定正确。随后的可行性勘探(2026-05-31 (b))把它的真实代价翻了出来:[tests/python/test_py_typed_int_unboxed.py](../../tests/python/test_py_typed_int_unboxed.py) 是义务 7 的活闸门(gate),14 个用例断言累加器循环的 IR **不含** `@py_int_add`、函数签名是 `define i64 @user_*_bench(i64 %n)`。而累加器(`total = total + step(i)`)本质上不可证界——在"`int` = bignum 除非证明安全"的规则下它必须装箱,闸门断言必须**反转**。也就是说方案二不是"接受一点性能损失",而是拆掉非 range 循环的 typed-int 未装箱快通道——挖空义务 7 的性能桥本身。

这次代价反转把推荐结论改写为:方案一(标记整数快通道)不只是长期答案,也是更合理的立即答案——闸门改**形状**(未装箱 add + 溢出分支)而非反转为全装箱。CPython 的 int 本来就永远带溢出检查(任意精度的内在成本),所以带检查的快通道仍然严格快于 CPython;性能从来不是保留这个 bug 的理由。

### 16.3.4 裁定、修复与约束语义规则

三样东西先于修复落地并持久化。第一,**类型语义规则**(2026-05-31 用户裁定,现为约束契约):Python 注解 `int` 意指任意精度整数;裸机器整数需要显式的 pcc 自有类型(如 `pcc.i64`);未装箱 i64 是优化,永远不是 `int` 的用户可见含义。第二,**5 个 xfail 回归**:[tests/python/test_native_typed_int_overflow.py](../../tests/python/test_native_typed_int_overflow.py) 以 `xfail(strict=False)` 固定了 `+`/`*` 参数溢出、链式 `a*b+c`、返回 ABI 携带、局部槽位携带、`<<` 提升五个验收判据——它们以 XFAIL 形态记录缺陷,等待修复之日翻绿摘标。第三,**优先级裁定**:P0 正确性 > 性能 > 包扩展;`def f(a: int, b: int)` 静默算错在 strict-native 可信度上打的洞,排在包回退收缩工作之前。

修复于 2026-06-17 落地,五个验收判据全部翻绿、xfail 标记摘除。在 [pcc/frontends/python/codegen/typed_int_abi.py](../../pcc/frontends/python/codegen/typed_int_abi.py) 中,`int` 参数的默认 ABI 规则被修正:

```python
# pcc/frontends/python/codegen/typed_int_abi.py
def _type_is_typed_int_abi_param(self, type_obj: Type) -> bool:
    # int defaults to boxed/tagged PyObject* ABI; raw i64 is opt-in only
    if isinstance(type_obj, IntType):
        return False
    return False
```

落地的形状综合了 16.3.3 的两个方案:准入端收紧——`int` 注解默认采用 boxed/tagged Python-int ABI,裸 i64 函数 ABI 降级为显式的、模式标注的逃生口,不再是 `int` 的默认含义(该回归文件的模块 docstring 即这条契约的原文);算术端保速——标记通道上的运算以 `llvm.smul.with.overflow.i64` 一类内联快路径实现溢出即提升(`test_tagged_int_mul_uses_inline_overflow_fast_path`)。义务 7 的活闸门 [tests/python/test_py_typed_int_unboxed.py](../../tests/python/test_py_typed_int_unboxed.py) 的断言随之按新契约重写:一般累加器循环如今断言 IR 中**出现** `@py_int_add`(装箱/标记语义),只有可证明安全的形状保留纯未装箱通道。今天,`mul(2**40, 2**40)`、`2**62 * 4`、`1 << 100` 在 strict self 后端 no-libpython 模式下与 CPython 逐位一致,并在 `PCC_GC_BACKEND=0..4` 下保持一致。

这一节的存在本身是风格契约的执行:已知缺陷写成开放问题,讲清张力,不粉饰。

## 16.4 显式机器整数:`pcc.i64` / `pcc.u64` 的契约

投影模型把任意精度判给 `int` 之后,定宽语义需要一个合法的家。契约写在 [goal-prompt.md](../../docs/goal/goal-prompt.md) 的 V-track:`pcc.i64` / `pcc.u64` 是显式的机器整数语义类型,只有裸 i64/u64 一个投影;**溢出策略——wrap、trap、checked、saturating——写进类型本身**,在源码里可见。Java/C 风格的定宽行为只许住在这里,决不许是 `int` 的默认含义。

诚实标注:截至写作,`pcc.i64`/`pcc.u64` 是设计契约,**尚未实现**——在 [pcc/](../../pcc) 源码树里检索不到对应的类型实现,仅 [goal-prompt.md](../../docs/goal/goal-prompt.md) 载有规格。它不是 16.3 那个缺陷修复的一部分(修复是让 `int` 停止意指 i64),而是 V-track 上独立的类型系统增项:给确实想要机器语义的代码(位操作、哈希、与 C ABI 对话的运行时内核)一个不必撒谎的去处。

把溢出策略写进类型,与第 4 章 C 前端的符号性教训是同一个论证的两面。C 代码生成里 `int` 与 `unsigned` 同为 i32,符号性作为带外元数据单独跟踪(`_tag_unsigned`/`_is_unsigned_val`),经典失败模式就是元数据在某个表达式形态上丢失、下游静默选错 `sdiv`/`ashr`。带外语义会腐烂;写进类型的语义不会。`pcc.i64` 的设计直接吸收了这条教训:回绕还是陷阱不是某个 pass 的心照不宣,是类型签名的一部分。

## 16.5 值类:可选的无身份载荷

### 16.5.1 标记与宿主助手:[pcc/value_model.py](../../pcc/value_model.py) 是什么、不是什么

`@pcc.valueclass` 装饰器定义在 [pcc/value_model.py](../../pcc/value_model.py)(经 [pcc/__init__.py](../../pcc/__init__.py) 惰性导出)。宿主 Python 端它做三件事:把类变成 `frozen=True` 的 dataclass(不可变性的宿主近似)、打上 `__pcc_valueclass__` 标记、经 `value_payload_layout()` 记录字段布局描述符。编译时,[pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py) 识别这个装饰器并生成 `ValueClassType`(定义于 [pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py))。

必须先把这个文件的边界说清楚,因为它曾经被夸大,而纠偏本身成了一份调查([docs/investigations/python-valhalla-value-model-actual-state.md](../../docs/investigations/python-valhalla-value-model-actual-state.md)):文件里的 `ValuePayload`、`ValueBox`、`SpecializedArray`、`GenericSpecialization` 这些 dataclass 是**宿主侧投影助手,供规划测试使用,不是生产 C 运行时**。文件 docstring 与 `value_model_status()` 都写明了这一点——后者维护三张诚实清单:`implemented`(V1 标量载荷、V2 选定指针字段边界等)、`not_implemented`(完整 marshal 覆盖、扁平化布局元数据、`pcc.array[ValueClass]` 连续存储、单态化等)、以及 `production_runtime: False`。状态曾声称"实现到 V6",代码审视证明 V1–V6 多数只是元数据脚手架,状态面随即被改写为区分 implemented 与 scaffolding。一个把声明卫生(claim hygiene)当架构组件的项目,连自己的状态函数都要接受审计。

### 16.5.2 值投影:标量载荷的 LLVM 聚合

当前实现的核心切片(V1)是标量字段值类的直接载荷低层化:`p = Point(1, 2)` 不再调用 `py_instance_new`,而是构造 LLVM 聚合 `{i64, i64}`;`p.x` 是一条 `extractvalue`;函数参数、构造器返回、方法接收者都可以走载荷 ABI(`def norm2(p: Point) -> int` 的签名携带聚合);`p == q` 在类 dunder 分派之前按字段低层化为 `icmp`/`fcmp` 链。V2 扩展到选定的指针字段(`Bag(items: list, count: int)` 的 `list` 字段以指针形式进载荷)与非递归嵌套。IR 形状闸门 [tests/python/test_py_value_class_unboxed.py](../../tests/python/test_py_value_class_unboxed.py) 断言热路径不出现 `py_instance_new`——这是义务 7"性能桥"的证据形式:不是基准数字,是分配点从 IR 里消失。

载荷能成立,前提是形状受限,而限制以编译期诊断的形式强制。`type_infer.py` 的 `_validate_valueclass_shape()` 拒绝:子类化(V0 子集禁止)、定义 `__del__`(无身份者无终结时机,提示移给拥有它的身份对象)、声明 `__dict__`/`__weakref__`(包括藏进 `__slots__` 元组里的,`_slots_contains_identity_slot()` 专门扫)、无类型注解的字段。`_validate_valueclass_recursion()` 拒绝递归与互递归载荷图(直接自含、互含、经容器自含)——扁平化布局放不下无穷展开,与其静默装箱不如显式拒绝。每条诊断都带修复提示,其中反复出现的一句就是本章的立场:"use a normal identity class"——要身份,用普通类,值类不偷。

### 16.5.3 装箱桥:ValueBox 与对象边界

载荷一旦流向动态上下文(`Any` 参数、容器、`print`),就跨过对象投影的接缝。运行时侧的桥是 `py_valuebox_new()`([pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c)):按类的字段数分配 `PyValueBoxObject`,类型标签 `PY_TYPE_VALUEBOX = 200`([pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 的公开枚举)。设计上它刻意复用实例兼容的布局——`py_valuebox_get_field`/`py_valuebox_set_field` 直接委托给 `py_instance_get_field`/`py_instance_set_field`,后者经 `pcc_gc_load_ptr()`/`pcc_gc_store_ptr()` 读写槽位。这一行委托买到的是第 10 章的全部基础设施:ValueBox 的指针载荷自动落入五后端共用的槽位追踪/更新契约,`py_gc_track` 注册、写屏障、重定位更新一个都不缺。**指针载荷的 GC 追踪不是值类的附加特性,是它寄生在统一对象图规则上的自然结果。**等值与哈希同样跨过桥:`py_obj_eq`/`py_obj_hash` 各有 `PY_TYPE_VALUEBOX` 分支(C 与 pcc-Python 两个运行时层都有),先比类、再经 GC 感知的槽位读取逐字段比较/混合,使分别装箱的等值载荷在字典里命中同一个键。

接缝的另一半是诚实声明:每次装箱产生**新的** box。两次把同一个 `Point(1, 2)` 递给 `Any` 边界,得到两个不同的堆对象。这正是身份观察必须被拒绝的原因——下一小节的对照表与 16.7 的第一个案例研究都从这里出发。

### 16.5.4 自举与 self 后端:载荷 ABI 一路到底

值投影不是 LLVM 后端的私有优化。聚合载荷出现在函数签名里,意味着 self 后端(第 13 章)的 IR 文本解析器、ABI 低层化、寄存器分配都要理解 `{ i64, i64 }`;16.7 的第三个案例研究就是这条链上最薄的一环断掉的记录。同时,值类工作的每个切片都按第 15 章的纪律带全量自举闸门——调查文件里反复出现的收尾句是"five-GC bootstrap matrix → 5 passed",模式标注为 strict no-libpython、`--backend self`。

## 16.6 身份不可窃取:普通类与值类的对照

义务 7 的前半句常被忽略:"**不窃取普通类语义**"。值模型的全部性能收益必须来自用户显式让渡身份,而不是编译器悄悄替用户做决定。对照表如下,右列的每一行都能在源码里指到拒绝点:

| 语义能力 | 普通类(第 7 章) | `@pcc.valueclass` |
|---|---|---|
| `id(x)` 稳定 | 保留 | 编译期拒绝(`type_infer.py` 内建调用分支:"id() is not supported for valueclass payloads in strict mode") |
| `x is y` | 保留 | 编译期拒绝(Compare 分支:"identity comparison is not supported...",提示用 `==` 比字段) |
| `weakref.ref(x)` | 保留 | 编译期拒绝(Call 分支)+ 运行时拒绝(`py_weakref.c` 对 `PY_TYPE_VALUEBOX` 升 TypeError;CPython 类比:`weakref.ref(3)`) |
| `__dict__` / 动态属性 | 保留 | 编译期拒绝(`_validate_valueclass_shape`,含 `__slots__` 扫描) |
| 字段可变 | 保留 | 不可变(frozen dataclass 语义;载荷按值复制) |
| 子类化 | 保留 | 编译期拒绝(V0 子集) |
| `__del__` 终结器 | 保留(第 9 章 `py_user_del_dispatch`) | 编译期拒绝(无身份即无可终结的生命周期) |
| `==` / `hash` | 默认按身份,可自定义 | 按字段值(载荷直比或 `PY_TYPE_VALUEBOX` 分支) |

三层防线的纵深是刻意的:静态可知的载荷在编译期被 `type_infer.py` 拦下,带源位置与修复提示;静态逃逸到 Dyn 的 box 在运行时被 `py_weakref_new` 拦下,行为对齐 CPython 对无身份值的既有先例。普通类一侧没有任何一行为值模型让步——`id`、`is`、弱引用、`__dict__`、可变性、子类、终结器全部原样保留,这是"可选"二字的实义。

与 Valhalla 的关系到此可以收口:pcc 借的是投影模型——语义类型与物理表示分离、身份是一种语义成本、对象/值边界由显式装箱桥管理、优化决不改语义;不借的是 Java 的定宽 `int` 回绕(16.3 的缺陷恰是这条红线被穿透的现行案例),也不把 "Valhalla" 当作品牌或设计约束——[AGENTS.md](../../AGENTS.md) 义务 7 写明它只是概念蒸馏的出处。

## 16.7 历史与教训

值模型的接缝在哪里,调查档案比设计文档诚实。[docs/investigations/](../../docs/investigations) 下以 `valuebox-valueclass-*-projection` 命名的文件有二十余份——属性存储、成员资格探针、推导式、异常参数、条件表达式、短路、`dataclasses.replace`、`super` 方法参数……每一份都是同一个事实的一次取样:**值/对象接缝出现在每一个能把值带进动态上下文的发射位点,漏掉一个,就在那里物化出一个带身份的实例(或直接崩溃)**。下面三个案例研究从这张地图上取最有教学价值的三段。

### 案例研究一:`weakref.ref(Pt(1, 2))` 未被拒绝(2026-06-10)

V-track 的"weak-dict 键策略"设计问题被归约成一个探针:对值类载荷取弱引用,今天会发生什么?预期是被拒绝;观察到的是**成功**——构造器投影成载荷,对象边界投影装箱,运行时对那个 ValueBox 建了弱引用,探针打印 `weakref-ok 1`(strict no-libpython、self 后端)。这是教科书级的身份语义窃取:弱引用观察的是身份的**生命周期**,而 16.5.3 说过每次装箱产生新 box——这个弱引用指向的对象会在某个无法预测的时刻死去,`r()` 的返回值是表示细节的函数。有趣的对照是,同一探针的第一版还写了 `r() is p`,被**既有的** `is` 诊断正确拦下——`is` 的栅栏立着,弱引用的洞就开在它旁边。

修复分两层落地,与对照表的纵深一致:`type_infer.py` 在 Call 分支加编译期诊断(与 `is` 诊断同一机制、同一位置族);随后的动态路径切片让 `py_weakref_new` 对 `PY_TYPE_VALUEBOX` 升 TypeError,C 与 pcc-Python 端口镜像落地,`WeakKeyDictionary`/`WeakValueDictionary` 经由同一构造函数自动继承拒绝。

教训有三。第一,身份逃逸面是**枚举出来的,不是推导出来的**——`is` 被堵了不等于 `id()` 被堵,更不等于弱引用被堵;每个观察身份的 API 都要单独探测。第二,调查在修复过程中又挖出两个伏笔:port 侧的陈旧 `.o` 让第一次验证差点给出假阴性(删档案不够,缓存的目标文件也要失效——延伸了仓库既有的 stale-archive 教训);`native_weakref.py` 的低层化位点漏发 `_emit_post_call_err_check`,运行时正确升起的异常"瞬移"过了 try/except——第 8 章那条"没有 Itanium 展开,漏检查点异常就瞬移"的失败类在值模型工地上原样复发。第三,from-import 形态(`from weakref import ref`)刻意未覆盖并如实记录——裸名 `ref` 太泛,宁可留下已记录的窄洞,不做不可靠的宽匹配。

### 案例研究二:后端 #4 重定位下指针载荷"失忆"(2026-06-01)

五 GC 生产平等契约(第 10 章)新增了一个针对性测试:装箱一个带指针字段的值类,强制后端 #4 重定位那个 ValueBox,再经 Python 对象路径改写、读回载荷字段。初始证据说后端 #0 过、#1–#4 全挂 `AttributeError: items`;两轮收窄后只剩 #4:程序打印完重定位前导就静默返回,五行载荷读回一行都没有。

这个调查(`gc-5backend-valueclass-pointer-payload-roots-no-libpython.md`)的提案清单本身就是一堂方法论课——十七个提案,第一个就标着 `[REJECTED as implemented]`。最有教学价值的三步:

第一步,剥掉红鲱鱼。`AttributeError: items` 根本不是属性系统的错——动态值类 getattr 的低层化**投机地**先发射了 `py_obj_getattr(box, "field")` 再做选择,回退分支的异常副作用污染了现场。改成先判值类匹配、回退块里才调用,假症状消失,真失败裸露:#4 在第一次 `gc.collect()` 后拿着陈旧指针走了错误出口。

第二步,替换法定位(第 18 章调试手册的"用替换检验假设,不只用观察")。只改一处:把 #4 分支的最后一个调用从 `check_payload(box)` 换成 `check_payload(loaded)`,其中 `loaded` 是从注册过的根槽位经 `pcc_gc_load_ptr()` 读回的对象。五行载荷读回全部打印。这一个替换同时证明了两件事:ValueBox 的重定位副本**完好保留了载荷**(运行时无罪),坏的是 `check_payload` 的参数槽 `%box.addr` 里那个没人更新的旧指针(前端有罪)。根因:借用的对象参数没有注册为可更新的 GC 帧根,重定位后转发源被清,槽里只剩悬垂。修复是把用户函数的对象参数注册为借用帧根——可被 #4 更新,但不改所有权,清理时不多释放。

第三步,反方向的教训。pcc-Python 运行时镜像里曾试图加一个 `_resolve_instance()` 助手统一解析转发指针,结果它对借用接收者走了普通对象返回所有权、发射了多余的 `pcc_gc_retain`,把后端 #0 的循环成员引用计数撑高,`gc.collect()` 报告零回收、终结器不再跑。提案一被标记 REJECTED,助手删除。值模型这一侧的结论:**值载荷不绕过对象图契约**——指针载荷要被追踪,载荷的携带者要被根住,而修根的人自己也要遵守第 9 章的所有权规则,否则修了 #4 坏 #0。

### 案例研究三:self 后端不认识 `{ i64`(2026-06-04)

V2 边界工作里一个普通的程序形状——装箱的 `Point` 从元组/列表下标里取回、传给 `def total(p: Point) -> int`——在 strict no-libpython、self 后端下编译失败:`BackendUnavailable: self backend does not understand LLVM type '{ i64'`。

错误消息里那半个聚合类型就是根因的全部线索。值类低层化发射了带显式聚合签名的调用:`call i64 ({ i64, i64 }) @...`;而 [pcc/backend/self_backend_parse.py](../../pcc/backend/self_backend_parse.py) 的 `_parse_call_signature()` 用裸 `inner.split(",")` 切签名参数,把 `{ i64, i64 }` 从字段逗号处劈成 `{ i64` 和 `i64 }`,前半截送进 `_parse_type()` 即爆。修复一行:换用解析器其余路径早就在用的 `split_top_level()`,嵌套聚合、数组、向量、括号常量、带引号值都保持完整。

教训不在修复的难度,在它暴露的依赖链长度:16.1 那张投影图里"值投影:LLVM 聚合载荷"一行,落地时意味着**从类型推断、低层化、IR 文本,一路到 self 后端手写解析器的每个字符串切分点**都要理解聚合。第一个假设("valuebox 拆箱发射了坏 IR")被否决——IR 是合法的,是解析器跟不上;第三个假设("self 后端聚合 ABI 传不了小载荷")也被否决——直接的嵌套聚合冒烟早就能跑。失败发生在最不被怀疑的一层:一个 `split(",")`。值模型每往前推一格,整条工具链就被重新审计一格——这也是为什么每个值类切片的定义里都含五 GC 全自举闸门。

## 16.8 小结

值模型是 pcc 对"Python 能不能既保语义又拿到扁平数据性能"这个问题的结构化回答,回答的形状是投影:语义类型恒定,物理表示二选一,接缝显式且受审计。`int` 的接缝在运行时已经完整——`py_internal.h` 的一个指针低位换来无分配的 63 位值通道,`py_int_ops.c` 的每条算术在溢出处提升、决不回绕,`binary_op_lowering.py` 把同一形状内联进生成代码;typed-int ABI 一侧的接缝义务也已兑现(2026-06-17):`a: int` 参数默认走 boxed/tagged ABI,标记通道溢出即提升、决不回绕,当初以五个 xfail 固定的验收判据已全部翻绿摘标(16.3.4)。值类的接缝是三层防线:编译期形状诊断与身份逃逸诊断、运行时 ValueBox 对统一槽位契约的复用、以及对每个身份观察 API 的显式拒绝;它的实现是窄而诚实的切片(`value_model_status()` 自己报告 `production_runtime: False`),它的边界由二十余份投影调查逐点测绘。普通类不为这一切付任何代价——身份不可窃取,值语义只能由用户显式选入。这就是"投影而非定宽"的全部含义:性能来自合法的表示,永远不来自被偷换的语义。

## 练习

1. **读源码验证。** [pcc/runtime/src/py_int_ops.c](../../pcc/runtime/src/py_int_ops.c) 的 `py_int_add()` 在双标记快路径里用 `__builtin_add_overflow` 检查 i64 溢出,而标记载荷只有 63 位。解释为什么这不是错误:追踪一个和落在 `[2^62, 2^63)` 区间的加法,说明它经过哪些函数、最终以什么表示返回。再对照 `py_int_neg()`,解释为什么它单独防 `INT64_MIN`。
2. **读 IR 形状。** `binary_op_lowering.py` 的 `_emit_inline_tagged_int_binop_or_call()` 只内联 `+`/`-`/`&`/`|`/`^`。论证为什么 `&`/`|`/`^` 的快路径不需要范围检查而 `+`/`-` 需要;再论证把 `*` 加入内联名单需要哪些额外的 IR(提示:126 位中间值、`llvm.smul.with.overflow` 与两次范围判定的关系)。
3. **复现因果链(纸上)。** 不运行任何命令,仅凭 `typed_int_abi.py` 的 `_type_is_typed_int_abi_param()`、`binary_op_lowering.py` 的 `_emit_binop_value()` 整数尾部与 `_emit_binop_int()`,写出 `def mul(a: int, b: int) -> int: return a * b` 的函数签名与乘法指令的 IR 形状,并解释为什么调查中两次"分析层收紧"实验注定无效。
4. **设计权衡论证。** 基于 16.3.3 的代价数据(方案二将反转 `test_py_typed_int_unboxed.py` 的 14 个未装箱断言、拆掉累加器快通道;方案一保住快通道但要求 typed-int 结果表示改为标记值并波及返回 ABI 与槽位存储),为两个方案各写一段最强辩护,然后给出你的裁定,并明确说明你的方案落地后义务 7 的 IR 形状闸门应当断言什么。
5. **对照表审计。** 16.6 的表声称值类的每条身份能力都有源码可指的拒绝点。逐行核对:在 [pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py) 中找到 `id()`、`is`、`weakref.ref`、子类化、`__del__`、`__dict__`(含 `__slots__` 形态)各自的诊断;在 [pcc/runtime/src/py_weakref.c](../../pcc/runtime/src/py_weakref.c) 中找到运行时层的拒绝。哪一条防线只有编译期一层?构造一个绕过它的程序形状(提示:案例研究一的 from-import 记录),并说明仓库为什么选择记录而非封堵。
