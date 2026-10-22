# 第 4 章 C 语义低层化与符号性

第 3 章把 C 源码送到了"AST 进入代码生成器"这条线;本章讲线的另一侧:[pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) 如何把 C 表达式低层化(lowering)为 LLVM IR。仓库的 [AGENTS.md](../../AGENTS.md) 对这份约 1.1 万行的文件有一句定性——"大多数 C 侧 bug 落在这里",而其中最高产的一族 bug 只围绕一个事实:LLVM 的整数类型没有符号,C 的整数类型有。本章以符号性跟踪为主线,讲清三件事:为什么 `int` 与 `unsigned int` 同为 `i32` 而符号性要单独跟踪;C 标准的 usual arithmetic conversions 如何落在 `_usual_arithmetic_conversion` 等六个 helper 上;以及这个设计的经典失败模式——位形正确、签名标记丢失,下游悄悄选了 `sdiv`/`srem`/`ashr`/有符号比较。压轴的案例研究来自 Lua:一次"排序偶尔出错"的诡异失败,最终缩减成一行丢了无符号标记的 XOR。

## 本章导读:LLVM 整数类型不携带符号信息

本章的核心结论是:位宽相同不代表语义相同。`int` 和 `unsigned int` 都可能降成 `i32`,但后续除法、取模、右移、比较要用 signed 还是 unsigned,取决于 pcc 自己保存的元数据。

- 低层化的难点不是生成一条 LLVM 指令,而是让后面的指令还能知道这条值的 C 语义。
- 符号性会在表达式链里丢失,所以测试不能只看单个操作的结果位。
- 本章的三个元数据层可以当排错清单:值标签、绑定标签、常量值哪一层断了。

## 4.1 问题与设计空间:LLVM 整数没有符号

LLVM IR 的设计立场是:**整数值无符号性,符号性属于操作**。`i32` 只是 32 个比特;有符号与无符号的区别被推到指令选择上,同一对操作数可以喂给两套指令:

```text
C 语义            有符号指令          无符号指令
除法 /            sdiv               udiv
取余 %            srem               urem
右移 >>           ashr(算术)         lshr(逻辑)
比较 < <= > >=    icmp slt/sle/...   icmp ult/ule/...
加宽转换          sext(符号扩展)     zext(零扩展)
整数→浮点         sitofp             uitofp
浮点→整数         fptosi             fptoui
```

这个立场对两补码机器是诚实的:`+`、`-`、`*`、`&`、`|`、`^`、`<<` 在两补码下本来就不区分符号,位形完全相同,LLVM 没必要为它们准备两套指令。但它把一个责任完整地推给了前端:**C 类型系统里的符号性信息,必须由编译器自己从"产生值的表达式"携带到"消费值的运算符"**。[pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) 的类型映射表 `get_ir_type_from_names()` 写得很直白:`"int"` 与 `"int unsigned"` 都映到 `int32_t`,`"long"` 与 `"long unsigned"` 都映到 `int64_t`,`signed` 关键字在进表前就被过滤掉。IR 类型层面,符号性已经不存在了。

设计空间有三个候选。其一,把 C 类型全程钉在每个表达式值上——每个 codegen 方法不再返回裸 IR 值,而是返回"值 + 完整 C 类型"的包装对象,类似 clang 在 AST 上携带完整类型信息的做法。这最严密,但 pcc 的代码生成器架构是 `codegen_<节点类名>` 方法族经 `codegen()` 的 MRO 扫描分派,每个方法返回 `(值, 地址)` 二元组;包装方案要求一次性改写全部表达式路径,且包装对象会渗进所有与 llvmlite builder 交互的代码。其二,用不同 IR 宽度区分符号——直接违背 LLVM 模型,不成立。其三,pcc 的实际选择:**IR 值对象上的旁挂元数据,加一组纪律化的 helper**。值还是 llvmlite 的值,但可能带一个 `_is_unsigned` 属性;六个 helper(`_tag_unsigned`、`_clear_unsigned`、`_is_unsigned_val`、`_convert_int_value`、`_usual_arithmetic_conversion`、`_shift_operand_conversion`)构成读写与转换的全部合法入口。

这个选择的代价必须诚实写出:旁挂元数据是**可选的、可丢失的**。包装方案里"忘了携带类型"是类型错误,编译器自己会拒绝;旁挂方案里"忘了打标签"静默通过,产出的 IR 合法、可执行、位形在多数输入下正确——直到某个输入让 `srem` 和 `urem` 给出不同答案。`_tag_unsigned` 的实现甚至自带一层静默:它用 `try/except (AttributeError, TypeError)` 给值设属性,设不上就算了。整个机制的安全网不在类型系统里,而在 4.4 节讲的测试纪律里。这是一个真实的工程权衡:渐进可改造、与现有架构同构,换来的是把"完备性"从机器检查降级为人工不变式。[AGENTS.md](../../AGENTS.md) 专设"C Codegen Invariants — Signedness"一节、调试手册专设 §10/§11/§12 三条技法,正是为这个降级支付的持续利息。

## 4.2 三层元数据:值标签、绑定标签、常量值

符号性信息在 `CCodeGenerator` 里以三种形态存在,对应值的三种生命阶段。

### 4.2.1 值标签:三种"味道"

第一层挂在 IR 值对象上,有三个互相独立的标签:

- `_is_unsigned`(经 `_tag_unsigned`/`_clear_unsigned`/`_is_unsigned_val` 存取):这个整数值本身按无符号解释;
- `_pcc_unsigned_pointee`(经 `_tag_unsigned_pointee`/`_is_unsigned_pointee`):这是一个指针,**从它 load 出来的值**应当是无符号的;
- `_pcc_unsigned_return`(经 `_tag_unsigned_return`/`_is_unsigned_return`):这是一个函数或函数指针,**调用它的返回值**应当是无符号的。

后两个标签的存在揭示了问题的递归性:指针值自己无所谓符号,但它携带着"将来某次解引用的符号性";函数指针更进一层,携带着"将来某次调用结果的符号性"。`unsigned char *p` 的解引用路径(`codegen_UnaryOp` 的 `*` 分支)在 `_safe_load` 之后检查 `_is_unsigned_pointee` 并给结果打 `_tag_unsigned`;数组下标(`codegen_ArrayRef`)的每一条出口——指针下标、数组下标、字节偏移回退——都重复同一动作。这种"每个 load 点各自补标签"的重复正是脆弱性所在:新加一条取值路径,就新增一个可能漏标的点。

### 4.2.2 绑定标签:从声明到使用

第二层挂在存储绑定(alloca、全局变量、函数)上:`_mark_unsigned`/`_mark_unsigned_pointee`/`_mark_unsigned_return` 优先给绑定对象设属性,设不上就退到 `__init__` 里的三个集合(`_unsigned_bindings` 等)。绑定标签在声明处诞生:`codegen_Decl` 解析声明类型后对 unsigned 标量调 `_mark_unsigned(var_addr)`;函数定义的序言(`codegen_FuncDef` 路径)对每个参数做同样判定,还会识别"指向 unsigned 标量的指针参数"(`_has_unsigned_scalar_pointee`)与"返回 unsigned 的函数指针参数"(`_func_decl_returns_unsigned`);函数声明与定义则给函数对象本身 `_mark_unsigned_return`。

判定依据是 `_is_unsigned_type_names()`:它先沿 `__typedef_` 链解析 typedef,再查冻结集合 `_UNSIGNED_TYPE_NAMES`——除了 `"int unsigned"`、`"long unsigned"` 这些排序后的类型名组合,还显式收录 `size_t` 与 `uint8_t`..`uint64_t`。这意味着 `typedef unsigned char lu_byte;`(Lua 的字节类型)经一次链解析就能命中。

绑定标签到值标签的桥是 `codegen_ID` 末尾的 `_propagate_binding_tags(result, var)`:从绑定身上把三种标签按味道拷到刚 load 出的值上。于是一个 `unsigned int x` 的读取链是:声明时 `_mark_unsigned(alloca)` → 使用时 load → `_propagate_binding_tags` → 值带 `_is_unsigned` → 进入表达式。

### 4.2.3 标签的其他诞生点

除声明外,值标签还有五个诞生点,每一个都对应 C 标准里一条"此表达式的类型是无符号的"规则:

1. **字面量**(`codegen_Constant`):`u`/`U` 后缀直接无符号;十六进制与八进制字面量超过 `0x7FFFFFFF` 时落成无符号 `i32`——这是 C 字面量类型阶梯的忠实复刻,十进制字面量的阶梯跳过无符号类型(超界进 `i64` 仍有符号),非十进制字面量则会途经 `unsigned int`。
2. **`sizeof` 与 `_Alignof`**(`_codegen_sizeof`/`_codegen_alignof`):结果固定为 `i64` 常量并 `_tag_unsigned`——`size_t` 永远无符号。
3. **结构体字段访问**(`codegen_StructRef` 一族):字段布局对象 `StructFieldLayout` 携带 `is_unsigned` 与 `decl_type`,load 出的值经 `_tag_value_from_decl_type` 按声明类型打标;位域走 `BitFieldRef`,其 `is_unsigned` 同时决定取值方式(掩码截断 vs `trunc`+`sext`)和结果标签——位域是数据布局与表达式语义的交汇点,4.4 节再回到这一点。
4. **函数调用返回**(`_extend_call_result`):按被调函数绑定上的 `_is_unsigned_return` 标记结果,普通调用、函数指针调用两条路径都过这个口。
5. **强制转换**(`codegen_Cast`):按目标类型名判 `_is_unsigned_type_names`,浮点→整数时直接决定 `fptoui` 还是 `fptosi`。

`codegen_Cast` 里藏着本章最值得端详的一个细节。当源与目标的 IR 类型完全相同、只有符号性要翻转时——`(unsigned)x` 而 `x` 是 `int`——代码不是就地改标签,而是发射一条 `add x, 0` 制造一个**新的值身份**,再给新值打标。原因在元数据的存放方式:标签挂在 Python 层的 IR 值对象上,而同一个值对象可能已被其他表达式持有。就地 `_tag_unsigned(x)` 会追溯性地改写 `x` 在所有已生成与将生成代码里的含义——一次 cast 污染整个函数。`add 0` 在 IR 层是噪音(优化器随手消除),在元数据层却是必要的:它把"同一个比特模式、两种类型解释"拆成两个可以各自打标的对象。这是旁挂元数据方案必须支付的一笔小额但深刻的税。

第三层元数据 `ConstIntValue` 属于编译期常量求值,留到 4.5 节。

## 4.3 usual arithmetic conversions:从规约到代码

C 标准把二元运算前的类型统一规则称为 usual arithmetic conversions(C11 6.3.1.8),其前置步骤是整数提升(integer promotions,6.3.1.1)。pcc 把这两层分别落在 `_integer_promotion` 与 `_usual_arithmetic_conversion` 上,移位单独走 `_shift_operand_conversion`。

### 4.3.1 整数提升:不变式是双向的

```python
def _integer_promotion(self, val):
    if not isinstance(getattr(val, "type", None), ir.IntType):
        return val
    if val.type.width == 1:
        return self._clear_unsigned(self.builder.zext(val, int32_t))
    if val.type.width < int32_t.width:
        return self._convert_int_value(val, int32_t, result_unsigned=False)
    return val
```

注意 `result_unsigned=False`:`unsigned char` 与 `unsigned short` 提升后是**有符号的** `int`。这是 C 的原文规则——只要 `int` 能表示原类型的全部值,提升目标就是 `int` 而非 `unsigned int`。[tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) 里的 `test_unsigned_char_promotes_to_signed_int_for_compare` 钉死了这个行为:`reg >= nvarstack`,其中 `int reg = -1`、`lu_byte nvarstack = 1`,两者都提升为有符号 `int`,`-1 >= 1` 为假。如果 pcc 望文生义地认为"unsigned char 是无符号的,所以比较也无符号",`-1` 会被解释成 `0xFFFFFFFF`,比较结果反转。

这揭示了本章不变式的完整形态:它是**双向的**。丢失无符号标记是 bug(4.4 节的主角),但把无符号标记保持得太久同样是 bug。符号性跟踪的目标不是"尽量无符号",而是**在每个运算点精确复刻 C 标准指定的那个类型**。

另一处细节:`i1`(比较结果)提升为 `i32` 并清除标签——C 里关系运算的结果类型是 `int`,有符号。`codegen_BinaryOp` 的比较分支后有一行注释直说了对齐对象:"clang CodeGen: comparison results are i32 (C int)"。

### 4.3.2 三分支:rank 规则在宽度上的坍缩

C 标准的转换规则用"整数转换 rank"与"能否表示全部值"来表述。pcc 的实现只比较宽度:

```python
if lhs_unsigned == rhs_unsigned:
    target_type = lhs.type if lhs_width >= rhs_width else rhs.type
    result_unsigned = lhs_unsigned
elif lhs_unsigned:
    if lhs_width >= rhs_width:
        target_type, result_unsigned = lhs.type, True
    else:
        target_type, result_unsigned = rhs.type, False
else:
    ...  # 对称分支
```

这不是偷工减料,而是一次合法的坍缩:在 pcc 的类型映射下(`char`=i8、`short`=i16、`int`=i32、`long`/`long long`=i64),标准整数类型的 rank 顺序与 IR 宽度严格单调对应,而"有符号类型能否表示无符号类型的全部值"在两补码定宽表示下恰好等价于"有符号类型是否严格更宽"。于是标准的三句话——同符号取高 rank;无符号侧 rank 不低则无符号胜;有符号侧能装下则有符号胜——精确翻译为三个宽度分支。

[tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) 用一对镜像测试钉住坍缩的两个临界面:`test_unsigned_int_converts_to_signed_long_when_long_can_hold_it` 验证 `unsigned int`(i32)遇 `long`(i64)时转向**有符号** long——`(long)-2 < (unsigned)1` 为真;`test_size_t_still_uses_unsigned_comparison_at_same_rank` 验证 `size_t`(i64 无符号)遇 `long`(i64)时**无符号**获胜——`-2` 被重新解释为巨大正数,`x < u` 为假。同一对操作数形状,宽度差一档,语义整个翻面。这两个测试本身就是对 6.3.1.8 最好的注释。

统一目标类型后,两侧操作数经 `_convert_int_value` 转换。这个 helper 的关键约定是:**加宽时按"源"的符号性选 `zext`/`sext`,结果标签按"目标"语义打**。C 的转换是按值定义的,在两补码下"按源符号扩展"恰好实现按值转换;而结果是什么类型由调用方(转换规则)说了算,两件事必须分开。`test_unsigned_char_return_is_zero_extended` 守的就是前半句:返回 `lu_byte` 200 的函数,其返回值进 `int` 时必须 `zext`——若按目标(有符号 int)选了 `sext`,200 的最高位为 1 时会变成负数。

### 4.3.3 移位:独立的小通道

C11 6.5.7 给移位开了特例:**不做 usual arithmetic conversions**,两侧各自做整数提升,结果类型是提升后的左操作数类型。`_shift_operand_conversion` 忠实复刻:两侧各自提升;为满足 LLVM 同型要求把右操作数转到左操作数宽度(转换时保留右操作数自己的符号性,保证扩展方向正确);返回的符号性只看左侧。于是 `u >> 1` 与 `1 >> u` 的符号性由各自的左操作数决定,与另一侧无关。消费端在 `codegen_BinaryOp`:`>>` 按 `is_unsigned` 选 `lshr` 或 `ashr`,且无符号结果要 `_tag_unsigned`——`test_unsigned_right_shift_result_stays_unsigned_for_modulo` 验证 `(x >> 31) % 2` 这种"移位喂取余"的链。

### 4.3.4 消费端:运算符选择与 no-wrap 立场

转换完成后,`codegen_BinaryOp` 与 `codegen_Assignment`(复合赋值)按 `is_unsigned` 做最终指令选择:`/` `%` 选 `udiv/urem` 或 `sdiv/srem`,比较选 `icmp_unsigned` 或 `icmp_signed`,`>>` 选 `lshr` 或 `ashr`;`+ - * & | ^ <<` 不分指令,但**结果必须重新打标**——它们正是"位形相同、类型不同"的那一族,也因此是标签最容易丢的地方。

`codegen_BinaryOp` 的算术分支里有一段立场注释值得抄录:默认**不**给整数运算挂 `nsw`/`nuw` no-wrap 旗标——即使是有符号算术,前端也需要先有"不会回绕"的证明才有资格加 `nsw`,否则 LLVM 有权对回绕敏感的代码做出错误优化。`test_unsigned_long_long_subtraction_wraps_without_nuw` 从行为面钉住它:`0 - 1000ULL` 必须按模回绕成 `0xfffffffffffffc18`。这与第 12 章 IR Fix Policy 里"文本层剥离 `nuw`/`nneg` 等属性"的规定同根:pcc 对未经证明的优化承诺一律说不。这也是第 1 章义务 2("性能必须被证明")在最底层的一次具体化。

顺带一笔对照:C 的 `unsigned` 回绕是**语言定义的语义**,pcc 必须精确复刻;而 Python 前端的 `int` 是任意精度语义类型,值投影(标记小整数通道)溢出时必须 deopt/promote、决不回绕(见第 16 章)。同一个代码库,两种相反的溢出契约,各自忠于各自的语言——这正是"语义先于性能"立场的最好注脚。

## 4.4 经典失败模式:位形正确、签名丢失

现在可以完整刻画本章的核心失败模式了。它的解剖结构是三段:

```text
生产者          xor / shl / add / ++ / 复合赋值 / phi 合流
                位形正确(这些运算两补码下不分符号)
   │            但忘了 _tag_unsigned(result)
   ▼
传播链          值在临时变量、phi、赋值间流动,标签一路缺席
   ▼
消费者          % → srem(应为 urem)
                / → sdiv(应为 udiv)
                >> → ashr(应为 lshr)
                < → icmp_signed(应为 icmp_unsigned)
```

它的险恶之处在于错误的**条件性**:只要值的最高位为 0,有符号与无符号指令给出相同结果。`(rnd ^ lo ^ up) % m` 在 `rnd` 较小时年复一年地正确,直到某个随机种子让 XOR 结果落进高位区。toy 测试几乎不可能撞上它,真实程序(Lua、libc 重度代码、控制流密集的程序)却天天用满 32 位。

[AGENTS.md](../../AGENTS.md) 把防御纪律压缩成三个问题,任何人在新增或修改表达式形态时必须自问:

1. 这个表达式产生整数结果吗?
2. 如果是,结果应该保持无符号吗?
3. 这个结果以后会喂给 `%`、`/`、`>>`、比较,或另一次算术转换吗?

第三问对应的测试方法论是调试手册 §11 的"下游敏感回归测试":好的符号性测试不是断言一个常量,而是**让无符号生产者的结果立即流进一个符号敏感的消费者,且尽量让另一操作数是普通有符号常量**。[tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) 整个文件就是这个形状的标本库:XOR 喂取余、复合赋值喂取余、前缀自增/自减喂取余、右移喂取余、三目喂取余。`% 960` 这个看似随意的常数是刻意的——960 是有符号字面量,若左侧标签丢失,usual arithmetic conversion 双签名分支会把整个运算拖回有符号世界。

调试手册 §10 补上定位学的另一半:面对真实程序失败,先把**数据布局假设**与**表达式语义假设**拆开。布局假设(`sizeof`、`offsetof`、伪 libc 声明、结构体布局)用一个对照原生编译器的探针程序就能整族证伪,成本极低;证伪之后,剩下的嫌疑就集中在符号性、提升、比较、移位、除法这些表达式语义上。4.6.1 的案例研究会展示这个二分法在实战里如何砍掉一半搜索空间。两族在一处交汇:位域。`BitFieldRef` 同时携带布局信息(容器类型、位偏移、位宽)与符号性(`is_unsigned`),`_load_bitfield` 对无符号位域走掩码+截断/零扩展,对有符号位域走 `trunc` 到位宽再 `sext`——布局错一个比特或符号错一个判断,症状几乎相同,只有探针能分辨。

还有一处值得诚实标注的开放角落:`codegen_TernaryOp` 的合流规则。三目两臂先各自转换到 `pick_target_type` 选出的较宽类型,phi 节点的符号性取"任一入边无符号则无符号"(`any(self._is_unsigned_val(...))`)。同宽两臂下这与标准一致(无符号胜);但"较宽的有符号臂遇上较窄的无符号臂"时,标准要求结果为有符号(宽类型装得下),而 any() 规则会给 phi 错打无符号标签。`test_unsigned_ternary_result_stays_unsigned_for_modulo` 只覆盖了同宽情形;混宽情形目前没有回归测试。这是旁挂方案"近似规则散布在各合流点"的又一例——完整的格点规则在 `_usual_arithmetic_conversion` 里,而 phi 合流用了一个更粗的近似。练习 3 请读者把这个角落变成一个可运行的反例。

## 4.5 常量折叠是第二个语义子系统

到目前为止讲的都是运行期路径:表达式变成 IR 指令,符号性决定指令选择。但 C 还要求编译器在**编译期**求值一整类常量表达式:枚举值、数组维度、位域宽度、初始化器、`case` 标签、`_Static_assert` 条件。这条路径在 `_eval_const_expr()` 里,而调试手册 §12 把它定性为**独立的语义子系统**:运行期符号性全对,编译期折叠仍可能全错,因为它是同一套规则的第二份实现。

`_eval_const_expr` 的载体是 `ConstIntValue`——一个携带 `width` 与 `is_unsigned` 的 `int` 子类。围绕它,运行期的每个 helper 都有一个编译期孪生:

```text
运行期(IR 值 + 标签)                 编译期(ConstIntValue)
_integer_promotion                    integer_promotion(宽度<32 → 32 位有符号)
_usual_arithmetic_conversion          usual_arithmetic_conversion(同样的三分支)
_convert_int_value                    convert_int_value / cast_int_value
codegen_Constant 的字面量阶梯          parse_int_constant(逐行同构)
udiv/urem vs sdiv/srem                raw_bits(a) // raw_bits(b) vs c_int_div
icmp_unsigned vs icmp_signed          raw_bits 比较 vs 带符号 int 比较
```

两个孪生细节最能说明"这是语义,不是算术"。其一,`cast_int_value` 在每次转换后按目标宽度掩码、再按符号位回折——Python 的 `int` 是无限精度的,不主动回折就永远不会回绕,而 C 的无符号回绕语义恰恰依赖定宽。其二,`c_int_div`/`c_int_mod` 手工实现**向零截断**:Python 的 `//` 向负无穷取整,`-7 // 2 == -4`,而 C 要求 `-7 / 2 == -3`。用宿主语言的运算符直接折叠 C 表达式,等于把宿主语义偷渡进目标语言——这正是 §12 要防的事故族。

无符号语义在折叠层的实现靠 `raw_bits()`:取值的低 `width` 位作无符号解释。于是 `(size_t)(~(size_t)0)` 的折叠链是:`0` 转 `size_t`(宽 64、无符号)→ `~` 经 `raw_bits` 翻转再掩码,得 `2^64 - 1` 而非 Python 直觉的 `-1` → 后续 `/ sizeof(t)` 走无符号除法 → 比较走 `raw_bits` 比较。链上任何一环忘了宽度或符号性,整条常量就错。

折叠层与运行层的**双重实现**是一份必须持续支付的同步成本:每修一个符号性 bug,都要自问它在另一层是否有孪生。§12 的原话值得抄进工程记忆:"如果一个真实程序在'简单常量'上失败,先检查 `_eval_const_expr()` 与宏展开后的源码,再怀疑运行期 IR。"

## 4.6 历史与教训

### 4.6.1 Lua sort.lua:一次 XOR 丢标,整个快排塌方

(来源:[docs/investigations/lua-sort-random-pivot-signedness.md](../../docs/investigations/lua-sort-random-pivot-signedness.md))

症状最初是侮辱性的模糊:Lua 集成测试的 `sort.lua` 用例**偶尔**失败——pcc 编译的 `onelua.c` 非零退出,原生 `cc` 编译的同一份源码通过。同源、同 Lua 版本、不同编译器,嫌疑被第一时间钉在编译器侧;但失败依赖随机种子,表面证据指向一堆吓人的方向:栈损坏?聚合拷贝错误?结构体布局漂移?比较器 bug?

调查的第一步不是读代码,是消灭随机性(调试手册 §1):固定 `math.randomseed`,构造确定性失败的数组形状,最终缩到"逆序输入、自定义比较器、最小失败规模约 1921"。第二步是绕开 Lua 测试套件、保留真实实现:一个 C 辅助程序 `#define main pcc_onelua_main` 后 `#include "onelua.c"`,直接构造逆序表调用内部 `auxsort`——原生通过、pcc 确定性失败,证明 bug 与测试框架无关。第三步是 §10 的二分:`sizeof`/`offsetof` 探针对照原生编译器,`TValue`、`CallInfo`、`Table` 等关键结构全部一致,**布局假设整族出局**;`luaL_makeseed` 前后的栈形状探针证明栈未被破坏;比较器与 `partition` 逐个替换证明它们只是受害者。

第四步替换法(§6)把范围收缩到随机基准点路径:去掉随机化的 `auxsort` 通过,小 `rnd` 值通过,大 `rnd` 值失败。于是 Lua 被整个移出现场,只剩一个纯 C 复现:

```c
typedef unsigned int IdxT;
static IdxT choosePivot(IdxT lo, IdxT up, unsigned int rnd) {
  IdxT r4 = (up - lo) / 4;
  IdxT p = (rnd ^ lo ^ up) % (r4 * 2) + (lo + r4);
  return p;
}
```

`lo=1, up=1921, rnd=3426782842u`:原生给 `p=731`,pcc 给 `p=475`。决定性的推理来自错误值本身:合法基准点区间是 `[481, 1441]`,475 比下界**恰好小 6**——这是有符号取余的指纹。`rnd ^ lo ^ up` 的位形最高位为 1,按无符号取余得正确余数,按有符号解释则 `srem` 给出 `-6`,加上 `lo + r4 = 481` 正好是 475。三段式解剖完整现形:XOR 算出了正确的位(生产者无辜),`builder.xor` 的结果没有被 `_tag_unsigned` 重新打标(标签丢失),下游 `%` 于是选了 `srem`(消费者无辜)。

根因确认后,调查没有止步于单点修复,而是按"同族审计"原则横扫相邻表达式形态,又抓出一个独立 bug:无符号前缀 `++`/`--` 的表达式结果未重新打标(存回变量的值是对的,表达式值的标签丢了)。最终修复覆盖四条路径:`^` 结果、无符号 `>>` 结果、整数复合赋值结果、无符号前缀自增自减结果;回归测试全部采用"无符号生产者 % 有符号常量"的下游敏感形状,沉淀为今天 [tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) 的后半个文件。

这个故事留下的不变式已写进 [AGENTS.md](../../AGENTS.md):**任何制造新整数 IR 值的表达式节点,都必须显式回答"这个结果在 C 语义下是有符号还是无符号"**。而它的方法论遗产同样重要:错误值与正确值的差(475 对 731,差出一个 `-6` 对 `+954` 的余数)往往直接拼出错误指令的名字——读懂错误值,比读一千行 IR 快。

### 4.6.2 编译期孪生:`MAX_SIZET` 折叠成 `-1`

(来源:[docs/debugging-playbook.md](../../docs/debugging-playbook.md) §12 与回归测试 `test_constexpr_cast_to_size_t_keeps_unsigned_range_in_ternary`;这一课没有独立的调查文件,以下按这两份留存物重述。)

4.6.1 修复的是运行期路径,而同族问题在编译期路径上还有一个孪生。调试手册 §12 记录了它的形态:运行期无符号比较已经正确,编译期的 cast 与三目折叠却忽略宽度与符号性——`((size_t)(~(size_t)0))` 被折叠成 `-1`,真实项目于是带着错误常量编译通过,在很远的地方失败。

这个表达式不是构造出来的刁钻样例,它是 Lua 源码里的 `MAX_SIZET`,并经宏链一路参与 Lua 表的尺寸上限计算。回归测试完整保留了宏链:

```c
#define MAX_SIZET ((size_t)(~(size_t)0))
#define luaM_limitN(n,t) \
  ((cast_sizet(n) <= MAX_SIZET/sizeof(t)) ? (n) : cast_int((MAX_SIZET/sizeof(t))))
enum { MAXHSIZE = luaM_limitN(1 << MAXHBITS, Node) };
```

按 Python 直觉折叠,`~0` 是 `-1`,`-1 / sizeof(Node)` 还是负数,`cast_sizet(n) <= 负数` 为假,三目选错分支,枚举常量 `MAXHSIZE` 拿到一个截断后的错误值——而枚举值是 4.5 节那条编译期管线的入口,没有任何运行期检查能拦住它。正确折叠要求每一步都带着 `ConstIntValue` 的宽度与符号:`~` 经 `raw_bits` 得 `2^64-1`,除法走无符号位形除法,比较走 `raw_bits` 比较,三目才能选对分支,`MAXHSIZE == 1 << MAXHBITS` 才成立。

两个故事合在一起,正好覆盖同一语义的两份实现:4.6.1 是 IR 指令选择层丢符号,4.6.2 是常量折叠层丢宽度与符号。这就是 §12 那句话的全部分量——**修符号性 bug 时,问一遍它的编译期/运行期孪生在哪**。一个只在运行期修复的编译器,会把同一个 bug 以枚举常量的形态重新带回来。

## 4.7 小结

本章的全部内容可以折叠回一个三层结构。底层是一个外部事实:LLVM 整数无符号,`int` 与 `unsigned int` 同为 `i32`,符号性只存在于指令选择(`sdiv/udiv`、`srem/urem`、`ashr/lshr`、`icmp_signed/icmp_unsigned`、`sext/zext`、`sitofp/uitofp`)。中层是 pcc 的设计回应:旁挂元数据——值标签(`_is_unsigned` 及指针味、返回味两个变体)、绑定标签(声明处诞生,`_propagate_binding_tags` 桥接)、编译期 `ConstIntValue`;转换逻辑收口在 `_convert_int_value`(按源选扩展、按目标打标)、`_integer_promotion`(小于 int 一律提升为**有符号** int)、`_usual_arithmetic_conversion`(C 的 rank 规则在定宽两补码映射下坍缩为三个宽度分支)、`_shift_operand_conversion`(移位不做统一转换,结果随左操作数)。顶层是为这个设计的固有弱点——标签可静默丢失——支付的纪律:制造新整数值必答三问;回归测试必须下游敏感(无符号生产者直喂 `%`/`>>`/比较);布局假设与表达式语义假设先二分;每个修复自查编译期孪生。Lua 的 475 与 `MAX_SIZET` 的 `-1` 是这套纪律的两份出生证明:前者证明丢一个标签足以放倒一个真实解释器,后者证明同一条语义必须在两份实现里各修一次。

## 练习

1. **读源码验证。** 在 [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) 的 `codegen_BinaryOp` 中追踪表达式 `(x ^ y) % m`(`x`、`y` 为 `unsigned int`,`m` 为 `int`)的完整低层化路径:`^` 的结果在哪一行被打标?`%` 之前的 `_usual_arithmetic_conversion` 走哪个分支、`result_unsigned` 是什么?最终选择 `urem` 的判定条件是哪一句?再对照 `test_unsigned_xor_result_stays_unsigned_for_modulo`,解释 `% 960` 中 960 不写成 `960u` 的用意。
2. **双向不变式。** `_integer_promotion` 对宽度小于 32 的整数固定传 `result_unsigned=False`。假设有人"修复"为保留源符号性(`unsigned char` 提升后仍无符号),[tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) 中哪个测试会立即失败?写出该测试里比较运算两侧的提升后类型与比较指令,分别在正确实现与"修复"后实现下的版本。
3. **开放角落实证。** 4.4 节指出 `codegen_TernaryOp` 的 phi 合流用 any() 近似符号性。构造一个最小 C 程序,让"较宽有符号臂 + 较窄无符号臂"的三目结果流入一个符号敏感的消费者,按 C 标准与按 any() 规则分别手推结果;说明为什么现有测试 `test_unsigned_ternary_result_stays_unsigned_for_modulo` 捕不到它,并按 §11 的形状为它写一个下游敏感回归测试(纸面即可)。
4. **编译期孪生推演。** 不运行代码,分别按 `_eval_const_expr` 的 `ConstIntValue` 语义与"直接用 Python int"的朴素语义,手推 `((size_t)(~(size_t)0)) / sizeof(Node)`(设 `sizeof(Node) == 16`)的折叠值,以及它使 `luaM_limitN` 三目各选哪个分支;再解释 `c_int_div` 为什么不能写成 Python 的 `//`(给出一个两者结果不同的具体常量表达式)。
5. **设计权衡论证。** 对比"旁挂元数据 + helper 纪律"(pcc)与"每个表达式值强制携带完整 C 类型"两种方案:各自在改造成本、漏标可检测性、与 llvmlite builder 的耦合度上的得失。然后设计一个机械检查来缩小 pcc 方案的弱点——例如一个 IR 后验 pass,扫描所有 `sdiv/srem/ashr/icmp_signed`,当其操作数携带 `_is_unsigned` 标签时报警——并论证它能抓住与抓不住 4.6 节两个案例研究中的哪一个,为什么。
