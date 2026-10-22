# 第 7 章 对象模型

运行时的一切都从"一个 Python 值在内存里长什么样"开始。pcc 的对象模型回答三件事:每个堆对象共享什么样的对象头(object header);类与实例如何布局、属性如何查找;以及最特殊的一条——生产运行时里的 pcc-Python 实现为什么仍须与 C ABI 布局和差分 oracle 逐字节一致。本章只讲对象的静态结构与属性协议:引用计数与所有权契约见第 9 章,异常协议见第 8 章,五个 GC 后端如何遍历和移动这些对象见第 10、11 章。读完本章,应当能对照 [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) 画出 `PyClassObject` 的 120 字节,并解释为什么"对象明明有这个属性却报 AttributeError"的排查顺序是布局、屏障、错误检查——而不是先怀疑前端。

## 本章导读:从对象头开始

这一章细节很多,但入口很简单:所有堆对象先有同一类对象头,再由类型标签和具体布局决定后面的身体是什么。只要对象头、类型标签、slot 访问以及 pcc-Python 与 C ABI oracle 的布局一致,后面的属性、方法和 GC 才有共同地基。

- 遇到对象 bug,先问指针指向的到底是哪种 type tag。
- 遇到类/实例 bug,先核对 pcc-Python 生产实现和 C ABI/oracle 布局是否逐字段一致。
- 遇到 backend #3/#4 专属 bug,先看读写对象槽有没有走 GC barrier。

## 7.1 问题与设计空间

一个 Python 运行时的对象模型要同时服务四个客户:解释/编译出来的代码(读写字段、调方法)、内存管理器(找到对象里的指针)、诊断系统(从一个裸地址判断"这是什么"),以及——pcc 特有的——自举链条(pcc-Python 必须能重述同一布局)。CPython 的答案是众所周知的 `ob_refcnt` + `ob_type` 指针:每个对象头部带一个指向 `PyTypeObject` 的指针,类型的全部行为(方法表、分配器、buffer 协议)挂在那个类型对象上。

pcc 没有照搬这个模型。[pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) 中的对象头是:

```c
typedef struct {
    int64_t refcount;
    int32_t  type_tag;
    int32_t  flags;        /* bit 0 = immortal, bit 1 = gc-tracked, ... */
} PyObjectHeader;
```

类型信息是一个 32 位整数标签(type tag),不是指针。这个选择的理由值得逐条说清,因为它约束了后面所有章节:

1. **标签可以在不解引用第二个对象的前提下被消费。** 运行时大量的分派(dealloc、比较、格式化、`py_obj_getattr`)是对 `type_tag` 的 `switch`。崩溃现场拿到一个可疑指针,读 `obj + 8` 处的 4 字节就能判断它像不像一个对象——[pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c) 中的 `py_type_tag_is_valid()` 与 `py_pointer_can_have_header()` 正是这样做防御性验证的。若类型是指针,验证一个对象先要验证另一个对象,诊断的地基就软了。
2. **五个 GC 后端共用一个头。** `flags` 的低位留给对象语义(immortal / gc-tracked / finalized),其余位留给五个 GC 后端的颜色、代龄、重定位状态(见第 10 章)。一个 16 字节的头是五后端"生产平等规则"的物理公分母。
3. **镜像义务。** pcc-Python 端口要用 `load_i32(o, 8)` 这样的原始访存重述同一布局(7.5 节)。整数标签是平的,镜像起来是一行;类型指针图意味着端口要镜像第二张对象图。
4. **self 后端可发射性。** 标签比较是一条整数指令,不需要 LLVM 帮忙做任何聪明事。

代价同样要写明:标签空间是手工管理的(7.2 节会看到用户类标签从 104 起单调分配,而 `PY_TYPE_VALUEBOX = 200` 嵌在同一空间里——这是一个真实的锐边,见练习 2);类型的行为不能像 CPython 那样挂在类型对象的槽上,而是散在运行时的 `switch` 里,新增一个内建类型要改多处分派(本书作者在 `CpyHandle` 标签 32 的引入记录中数过:两个 C dealloc switch、重定位白名单、端口窗口)。pcc 接受这个代价,因为它的目标不是"最大可扩展的类型系统",而是**可审计、可镜像、可自举的最小对象内核**。

第二个大的设计决定是**标记小整数通道(tagged small-int lane)**。`PyObject *` 的 bit 0 被征用:为 1 表示这不是指针,而是一个左移一位的 63 位有符号整数;为 0 表示真堆指针——`py_internal.h` 的注释给出了依据:malloc 在所有目标平台上至少 8 字节对齐,真指针的 bit 0 恒为 0。这是第 16 章值模型的物理面:`int` 的语义类型是任意精度,值投影是标记通道,对象投影是 `PyIntObject` 大数;溢出标记范围就装箱,决不回绕。本章只需要记住它对对象模型的两条影响:任何接受 `PyObject *` 的运行时函数都必须先问 `PY_IS_TAGGED_INT`,而 `py_incref`/`py_decref` 对标记值直接返回——标记整数没有对象头,没有引用计数,没有身份。

第三个决定:**类的元数据用裸 C 数组,实例字段用静态槽位**。`PyClassObject` 里方法表是线性数组而非哈希表,[pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c) 的注释直接给了理由:"Classes have small method tables so this is faster than a dict in the common case. A future phase can swap to a hashmap."(类的方法表很小,常见情形下线性扫描比字典快;以后可以换。)实例字段则在编译期由代码生成确定槽索引,`self.field` 低层化(lowering)为 `py_instance_get_field(self, idx)` 而不是字典查找——[pcc/frontends/python/codegen/class_gen.py](../../pcc/frontends/python/codegen/class_gen.py) 的模块头明确写着这条契约。动态性没有被取消,而是被排到后面:声明字段走槽,未声明的属性走每实例一个的隐藏字典槽(7.4 节)。这正是"性能是已证语义的后果"在对象模型上的体现:静态化只发生在语义可证明的地方,所有其余路径保留完整的 Python 行为。

## 7.2 对象头、标记整数与类型标签空间

### 对象头

```text
字节:   0               8       12      16
        +---------------+-------+-------+
        |   refcount    |type_  |flags  |    PyObjectHeader,16 字节
        |   (int64)     |tag i32|  i32  |
        +---------------+-------+-------+
        |  类型特定字段从这里开始 ...    |
```

`refcount` 在偏移 0,`type_tag` 在偏移 8(int32),`flags` 在偏移 12(int32)。这三个数字是仓库里的硬契约——[AGENTS.md](../../AGENTS.md) 把它们写进了启动必读,pcc-Python 端口用字面量直接读写它们。

`flags` 的对象语义位定义在 [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h):

```c
#define PY_FLAG_IMMORTAL    0x1
#define PY_FLAG_GC_TRACKED  0x2
#define PY_FLAG_FINALIZED   0x4
```

- `PY_FLAG_IMMORTAL`:`py_incref`/`py_decref` 对带此位的对象直接返回。`py_None`、`py_True`、`py_False` 以及惰性构造的根类 `object`(`py_class.c` 中的 `object_root()`)都是 immortal 的。
- `PY_FLAG_GC_TRACKED`:对象已被登记进循环收集器的侧表(`py_obj_gc.c` 中的 `py_gc_track()` 设置)。哪些类型在何时登记属于第 10 章;本章只需要知道实例在 `py_instance_new()` 末尾登记,而**类对象从不登记**——这个事实在 7.3 节会变成一个有意思的位重用。
- `PY_FLAG_FINALIZED`:终结器(finalizer)`__del__` 已经派发过。[pcc/runtime/src/py_dunder.c](../../pcc/runtime/src/py_dunder.c) 中的 `py_user_del_dispatch()` 在调用 `__del__` 之前置位;此后即使对象在终结器里复活(resurrection)、引用计数再次归零,第二次 dealloc 也会跳过终结器。这是对象模型为"终结器至多跑一次"付出的一个 bit。

从 `0x8` 开始的位(`PY_FLAG_GC_WHITE/GRAY/BLACK/PINNED/GC_YOUNG/GC_OLD/...` 直到 `0x10000`)全部属于 GC 后端,留给第 10、11 章。

`flags` 的读写一律通过 `py_internal.h` 的内联原子访问器:`py_header_flags_load/store/or/and`(`__atomic_*`,acquire/release 序)以及 CAS 循环的 `py_header_flags_update()`。`refcount` 则经由 `pcc_refcount_incref/decref`,其策略(`PCC_REFCOUNT_KIND_NONATOMIC/ATOMIC/BIASED/DEFERRED`)由线程基底选择——细节属于第 9 章。

### 标记整数与防御性指针判定

```text
一个 PyObject* 的两种解读(由 bit 0 区分):

  ...xxxx xxx1    标记小整数:算术右移 1 位 = 63 位有符号载荷
                  (PY_TAGGED_INT_MIN = INT64_MIN>>1, MAX = INT64_MAX>>1)
  ...xxxx x000    真堆指针:malloc ≥ 8 字节对齐保证低 3 位为 0,
                  指向一个以 PyObjectHeader 开头的分配块
```

编解码就是 `py_internal.h` 里的 `py_tag_int()`(左移一位置低位)与 `py_untag_int()`(算术右移保符号)。`py_type_of()` 对标记值返回 `PY_TYPE_INT`,对真指针读头部标签——所以"标签分派"对调用者是统一的,标记性只在边界检查一次。

运行时不信任任何传进来的指针。`py_obj.c` 的 `py_pointer_can_have_header()`(以及 `py_class.c` 中独立实现的 `pointer_can_have_header()`)做四重排除:NULL、bit 0 为 1(标记整数)、低于 0x1000(空页)、未 8 字节对齐、高 16 位非零(非规范地址)。`py_incref`/`py_decref` 进一步校验 `type_tag` 是否落在合法集合(`py_type_tag_is_valid()`),在 `PCC_DEBUG_RUNTIME` 环境变量打开时,坏指针或坏标签直接 `abort()` 并打印现场。这些检查不是洁癖:自举链上 pcc1 的代码生成 bug 第一时间表现为"某处递减了一个不是对象的东西",让运行时在第一现场喊出来,比让堆腐烂三步后再崩便宜得多(调试手册 §8)。

### 类型标签空间

`py_runtime.h` 顶部的匿名枚举给出内建标签:`PY_TYPE_NONE = 0`、`PY_TYPE_BOOL = 1`、`PY_TYPE_INT = 2`(大数形态)、`PY_TYPE_FLOAT = 3`、`PY_TYPE_STR = 4`、`PY_TYPE_LIST = 5`、`PY_TYPE_DICT = 6`、`PY_TYPE_TUPLE = 7`、`PY_TYPE_SET = 8`、`PY_TYPE_FUNC = 9`、`PY_TYPE_CLASS = 10`、`PY_TYPE_INSTANCE = 11`、`PY_TYPE_EXC = 12`……一路排到 `PY_TYPE_CPY_HANDLE = 32`(外来 CPython 引用的拥有句柄,第 17 章)。然后是一段刻意的留白:

```text
 0 .. 32      内建类型标签
100           PY_TYPE_USER        用户域起点(>= 100 即"用户类实例")
101           PY_TYPE_PROPERTY    ┐
102           PY_TYPE_CLASSMETHOD │ 描述符包装对象(py_internal.h)
103           PY_TYPE_STATICMETHOD┘
104           PY_TYPE_USER_CLASS_START   第一个用户类标签;
              py_class.c 的 g_next_user_tag 从这里单调 ++
200           PY_TYPE_VALUEBOX    值类装箱对象——嵌在用户域里
```

每个用户类在 `py_class_new()` 里领取一个唯一标签存进 `type_tag_alloc`,它的实例把这个标签写进对象头。于是 `isinstance` 的快路径、dealloc 分派(`pcc_dealloc_dispatch()` 对 `>= PY_TYPE_USER` 统一走 `py_instance_dealloc`)都不需要追指针。注释如实记录了取舍:"Using a single allocator keeps tags unique across modules. In a future phase this can be per-module."——单进程单调分配换跨模块唯一性。`PY_TYPE_VALUEBOX = 200` 落在同一空间且分配器没有避让逻辑,这是一个值得读者自己审计的开放锐边(练习 2)。

对象死亡时,`py_obj.c` 的 `pcc_dealloc_dispatch()` 按标签把对象交给类型专属的 deallocator(`py_dealloc_list`、`py_class_dealloc`、`py_instance_dealloc`……)。为防容器链过深导致 C 栈溢出,`py_decref` 维护一个线程局部的延迟队列(`PccTrashNode`,CPython "trashcan" 的对应物):嵌套 dealloc 时容器类对象先入队,最外层统一排干。所有权语义见第 9 章;此处只需记住 dealloc 分派的钥匙是 `type_tag`。

## 7.3 类对象:`PyClassObject` 的 120 字节

用户类在模块初始化时由代码生成发射:每个 `ClassDef` 对应一个全局变量 `.class.<module>.<name>`,模块 init 函数收集基类指针与字段名数组,调用 `py_class_new()`,再对每个方法调用 `py_class_add_method()`(`class_gen.py` 模块头的契约)。运行期动态建类(`type(name, bases, ns)`)走 `py_class_attrs.c` 的 `py_class_new_from_objects()`,最终落到同一个 `py_class_new()`。

`py_internal.h` 中的布局,加上手工算出的偏移:

```text
PyClassObject —— 共 120 字节(LP64)
offset size 字段              语义
  0     16  h                 PyObjectHeader(type_tag = PY_TYPE_CLASS = 10)
 16      8  name              借用的 C 字符串(指向发射模块的只读段)
 24      4  n_bases           直接基类数            (+4 填充)
 32      8  bases             PyClassObject** 声明序直接基类
 40      4  n_mro             MRO 长度              (+4 填充)
 48      8  mro               C3 线性化;mro[0] == 本类
 56      4  n_methods                                (+4 填充)
 64      8  methods           PyClassMethod[]:{name, func},每项 16 字节
 72      4  n_fields          本类声明的实例字段数   (+4 填充)
 80      8  field_names       const char** 槽序字段名
 88      4  instance_size     实例总字节数
 92      4  type_tag_alloc    本类实例携带的类型标签
 96      8  del_method        __del__ 缓存(借用)
104      8  attrs             类级变量字典(拥有)
112      8  metaclass         元类(借用)
```

四个 `int32` 字段每个后面跟 4 字节填充(下一个 8 字节字段要对齐),而 `instance_size`/`type_tag_alloc` 两个 `int32` 恰好挤进一个 8 字节,使 `del_method` 落在 96——这就是 [AGENTS.md](../../AGENTS.md) 反复强调的三个数:`del_method@96`、`attrs@104`、`metaclass@112`,总 120 字节。pcc-Python 端口 [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) 的模块文档头逐行写着同一张表;那段 docstring 就是这个结构体事实上的跨语言规范(7.5 节)。

逐字段的设计要点:

**`bases` 与 `mro`。** `py_class_new()` 浅拷贝基类数组(`copy_class_array()`),然后用 `c3_linearize()` 做 PEP 3119 的 merge 算法:候选头是"不出现在任何其他序列尾部的头",取不出候选即 MRO 不一致,返回 -1,调用方以 TypeError 呈现。零基类且自身不是根类时,MRO 末尾追加惰性构造的 `object_root()`——一个 calloc 出来的、immortal 的、`type_tag_alloc = PY_TYPE_INSTANCE` 的极简类。注意这些数组持有的都是**借用引用**:类的生命周期与进程相当(代码生成把它存进全局),所以基类、方法、字段名都不参与引用计数。但"借用"不等于"GC 不可见"——这些槽仍然是对象图的边,移动型后端必须能改写它们,这正是 7.7 节第一个案例研究的主题。

**`methods` 与方法值的双重形态。** `PyClassMethod` 是 `{const char *name; PyObject *func}`,`func` 上的注释毫不掩饰:"borrowed — points at a user_* LLVM function"。方法表里存的多数不是堆上的函数对象,而是代码生成发射的裸函数指针 cast 成 `PyObject *`。调用辅助函数(`py_class.c` 的 `class_call_binary_method()` 等)先做头嗅探——指针能承载对象头且标签是 `PY_TYPE_FUNC` 才按 `PyFuncObject` 走 `py_func_call()`,否则直接 cast 回函数指针调用。这换来了零分配的静态方法表(模块 init 只做 realloc 追加),代价是运行时必须能可靠区分"堆对象"与"代码地址"——前述防御性指针判定在此不是诊断手段而是正确性依赖。`py_class_lookup()` 沿 MRO 线性扫每个类的方法表,字符串比较前先做指针相等短路(字段名/方法名通常是同一份 rodata 字面量);`__name__` 与 `__mro__` 在这里特判合成。

**`del_method`。** 终结器查找在 dealloc 热路径上,所以缓存到固定偏移。历史 C oracle 的 `py_class_new()` 在结尾预填 `py_class_lookup(c, "__del__")`(继承的也能拿到),`py_class_add_method()` 看到 `"__del__"` 时同步更新,而 `py_dunder.c` 的 `py_user_del_dispatch()` 发现槽为空时懒补一次。当前 pcc-Python 生产所有者的 `py_class_new` 不做预填(memset 留 NULL),而是依赖懒补得到同一可观察行为。这是"布局必须逐字节相同,oracle 与生产实现的行为允许不同步调、但必须收敛"的一个干净例子(练习 3)。

**`attrs`:类级变量字典。** 这是布局里最年轻的槽,它的来历是一段三幕剧(7.7 节)。今天的形态:`attrs` 是类**拥有**的字典,存放 `class C: x = 1` 这类类变量以及 `type()` 三参形式的命名空间;[pcc/runtime/src/py_class_attrs.c](../../pcc/runtime/src/py_class_attrs.c) 顶部注释言明,旧的指针键侧表(`PccClassAttrsNode` 链)已退化为索引,不再拥有字典——把边放进对象本体,移动型收集器才能直接追踪与改写它。类属性读取 `py_class_getattr()` 的顺序是:`__dict__` 特判 → 元类的数据描述符 → 沿 MRO 查每个类的 `attrs` 字典(命中 classmethod 则绑定、命中描述符则调 `__get__`)→ 退到 `py_class_lookup()` 方法表。写入 `py_class_setattr()` 先问元类数据描述符的 `__set__`,否则进本类 `attrs`。

**`metaclass`。** 借用指针,`py_class_set_metaclass()` 设置。pcc 的元类支持是窄的:元类参与类属性的 get/set/delete 协议(上一段的查找顺序),不参与类创建协议——这是一个如实的"实现到哪了"的边界,不要把它读成完整的 CPython 元类语义。

**一个值得知道的位重用。** `py_class.c` 定义 `PY_CLASS_FLAG_SLOTS_ONLY` 为 2——数值上与 `PY_FLAG_GC_TRACKED` 是同一个 bit。`py_class_mark_slots_only()` 把它置到**类对象**的头上,含义是"本类实例没有动态属性字典"(7.4 节);这与 GC 位不冲突,仅仅因为运行时从不对 `PY_TYPE_CLASS` 对象调用 `py_gc_track()`(grep 可证:`py_gc_track` 的全部调用点都在实例、容器、函数等构造器里)。这是一个依赖"用法不交叠"的 bit 复用,不是依赖类型系统的隔离——读 flags 转储时如果不知道这条,会把一个 slots-only 的类误读成"被 GC 跟踪的类"。

## 7.4 实例与属性协议

### 实例布局

```text
PyInstanceObject(instance_size = 24 + 8*(n_fields+1) 字节)
offset  0   PyObjectHeader     type_tag = cls->type_tag_alloc
offset 16   cls                → PyClassObject(借用语义,但经屏障读)
offset 24   fields[0]          ┐ 声明字段槽:拥有引用,
            ...                │ NULL = "尚未赋值"哨兵
            fields[n_fields-1] ┘
            fields[n_fields]   隐藏槽:动态属性字典(__dict__),懒建
```

`py_instance_new()` 按 `cls->n_fields + 1` 分配槽——多出来的一个是**隐藏的动态属性字典槽**,这就是 `py_class_new()` 计算 `instance_size` 时那个 `+ 1` 的去向。全部槽位清零:NULL 既是"未赋值"哨兵,也让 dealloc 可以无条件地"非空才递减"。实例头部写的是类领到的 `type_tag_alloc`,因此从任何一个实例指针出发,一次 `load` 就能知道它属于哪个类标签;`py_isinstance()` 先比类指针,再线性扫 MRO。

**字段槽的索引在编译期定死。** `py_class.c` 中 `lookup_field_index()` 的注释交代了一个容易误解的点:字段槽只查最派生类自己的 `field_names`,不沿 MRO 聚合——因为"the codegen merges the declaration sets",代码生成在发射类元数据时已经把继承链上的字段声明合并进叶子类的字段表。运行时的 MRO 字段聚合于是不必存在。这也意味着字段索引是**合并后表的属性,而不是任何单个源文件的属性**——7.7 节第三个故事里,一个按 AST 源序推断索引的测试正是死在这条上。`py_instance_get_field()`/`py_instance_set_field()` 仍保留防御性的界检查(索引越界返回 NULL/无操作),注释写明这是为了"malformed IR cannot segfault us"。

### 属性查找的七层

[pcc/runtime/src/py_obj_ops_dispatch.c](../../pcc/runtime/src/py_obj_ops_dispatch.c) 的 `py_obj_getattr()` 是统一入口:按标签分派,实例标签进 `py_instance_getattr()`,类标签进 `py_class_getattr()`,函数/弱引用/复数/异常各有小特判;全部失败且 TLS 无挂起异常时,`py_obj_missing_attr()` 构造 `AttributeError`——**这就是"object has no attribute X"消息的出生地**。实例路径展开后:

```text
py_instance_getattr(inst, name)                    py_class.c
 ├─ 类定义了 __getattribute__?
 │    调用之;若抛 AttributeError 且类有 __getattr__,清异常改调后者
 └─ 默认路径 py_instance_getattr_default:
      1. "__class__" / "__dict__" 特判(后者懒建隐藏槽字典)
      2. MRO attrs 字典命中【数据描述符】?
         (PY_TYPE_PROPERTY,或类定义 __set__/__delete__)→ __get__
      3. 声明字段槽:lookup_field_index → fields[idx]; 非 NULL 即返回
      4. 动态属性字典(隐藏槽)查 name
      5. 步骤 2 命中的普通类属性:函数 → 绑定;非数据描述符 → __get__;
         否则原值返回
      6. MRO 方法表 py_class_lookup → py_instance_bind_method 绑定
      7. __getattr__ 兜底
 返回 NULL 且无挂起异常 → py_obj_missing_attr → AttributeError(name)
```

这个顺序是 CPython 描述符协议的同构移植:数据描述符压过实例存储,实例存储压过非数据描述符与普通类属性。pcc 的"实例存储"只是分裂成了两层——静态字段槽(3)与动态字典(4)。写路径 `py_instance_setattr()` 是同一逻辑的三层缩影:带 `__set__` 的描述符 → 字段槽(经 `pcc_gc_store_ptr()`,旧值递减新值递增的平衡契约属于第 9 章)→ 动态字典(懒建;若类被标记 slots-only 则此层不存在,返回 -1)。

**绑定方法是合成的跳板。** 第 6 步拿到的方法多半是裸函数指针,不能直接作为值返回。`py_class_attrs.c` 的 `py_instance_bind_method()` 把 `{method, self}` 装进 captures 元组,套上统一入口 `pcc_instance_bound_method_entry()`,造出一个真正的 `PyFuncObject`。入口按实参个数把 `self` 前插后分发:0、1、2 个实参各有直调分支,3 实参分支的注释记录了它的来历——`__exit__(self, exc_type, exc, tb)` 曾因缺这个分支而让上下文管理器"exit returned NULL";这是"对象模型的洞以真实程序的崩溃形态浮出"的又一例。`classmethod` 走平行的 `pcc_classmethod_bind()`,把类对象而非实例前插。

### 终结与复活(对象模型侧)

`py_instance_dealloc()`(`py_class.c`)的开头两行决定了 Python 终结语义的对象模型部分:先 `py_weakref_invalidate()` 清弱引用,再 `py_user_del_dispatch()` 派发 `__del__`;随后检查 `refcount > 0`——终结器可能把 `self` 存到别处使对象**复活**,此时重新 `py_gc_track()` 并返回,不释放。因为 `py_user_del_dispatch()` 在派发时已置 `PY_FLAG_FINALIZED`,复活对象的下一次死亡不会再进终结器。终结器抛出的异常被 `py_clear_exception()` 吞掉(CPython 的 unraisable 语义,告警通道是后续诊断任务)。引用计数为何在这一刻可信、跨后端如何保证,见第 9、10 章。

## 7.5 一套布局,一个生产所有者:pcc-Python 与 C oracle 的镜像纪律

pcc 的运行时分层(第 1、14 章)已经把这段生产所有权迁进 pcc-Python:[pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) 以 `@c_abi_export("py_class_lookup")` 等修饰导出**同名同 ABI** 的符号,当前生产归档链接 pcc-Python 对象,不把 [pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c) 当作第二份生产实现。C 结构声明和历史实现仍有两项职责:定义外部 ABI 布局,以及充当迁移期差分 oracle。于是 `PyClassObject` 仍是双方的**公共契约**,但生产所有者只有 pcc-Python。

端口没有结构体可用,它用原始访存重述布局:

```python
n_fields_i32: int = load_i32(cls, 72)
field_names = load_ptr(cls, 80)
...
store_ptr(cls, 96, func)          # del_method
store_ptr(cls, 112, metaclass)    # metaclass
```

每个数字字面量都是对 C 结构体的一次盲信。这就是 [AGENTS.md](../../AGENTS.md) 写成铁律的原因:"The pcc-Python mirror in [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) must match the C `PyClassObject` in [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) exactly. Layout drift between them is a recurring class of bug."(两者必须精确一致;布局漂移是反复出现的 bug 类。)改 C 结构体而不改端口,不会有编译错误、不会有链接错误——只有运行期某个偏移读出来的"字段"变成了邻居的字节。

读端口还能看到镜像不是转写,而是**同一谓词在另一坐标系里的重述**。C 的 `pointer_can_have_header()` 检查 `bits < 0x1000`、`bits & 0x7`、`bits >> 48`;端口的 `_ptr_can_have_header()` 拿到的是 `untag_int(o)`(指针算术右移一位),于是同样的检查变成 `bits < 2048`、`bits & 3`、`bits >= 2**47`——每个常数都除以二,因为坐标系移了一位。抄错任何一个,谓词就静默放过(或拒绝)一类指针。同样,端口把 `PY_TYPE_CLASS = 10`、120、偏移等常量**内联在使用点**而不是读模块级常量,并在 docstring 里记下 i32/i64 的 ABI 细节:C ABI 的 `int32` 参数在 pcc-Python 里靠 `: int` 注解强制为 i32,函数体内则按 pcc 默认 i64 运算——为避免调用边界的宽度错配,端口宁可内联逻辑也不调带 int 参数的辅助函数。这些都是自举链(第 15 章)真实踩出来的纹理。

纪律因此有三条可操作的形态:

1. **改布局 = 一次双边提交。** C 结构体与端口 docstring/字面量在同一变更里走;`py_class.py` 文档头那张偏移表是事实规范,先改表再改码。
2. **默认模式测试,而非 `PCC_RUNTIME_CC=cc`。** 修在 C 文件里的 bug,若该文件属于 `PY_MODULES`(默认链接端口),默认模式根本不会执行你的 C 修复——仓库记录过四个切片在 cc 模式下给出假信心的教训(第 14 章)。
3. **行为允许收敛式差异,布局不允许任何差异。** `del_method` 预填(C oracle)对懒补(pcc-Python 生产实现)是合法差异,因为可观察 ABI 行为相同;偏移 96 对偏移 88 不是差异,是腐坏。

## 7.6 "明明定义了 X 却说没有":三因检查顺序

把 7.2–7.5 节的机制叠起来,就能解释 [AGENTS.md](../../AGENTS.md) 里那条排查口诀。症状:真实程序报 `AttributeError: object has no attribute X`,而类源码明明定义了 `X`。由 7.4 节可知,这个消息只在 `py_obj_missing_attr()` 处产生,意味着七层查找**全部**走空。按命中概率与验证成本排序的三因:

1. **布局漂移(C ABI `PyClassObject` vs `py_class.py`)。** 若 ABI 声明与生产实现对 `n_fields@72`、`field_names@80`、`methods@64` 中任何一个的理解不同,`lookup_field_index()` 会在垃圾上扫描,`_class_lookup_in_mro()` 会读错方法表——查找不崩溃,只是永远落空。验证:对照 `py_internal.h` 与 `py_class.py` docstring 的偏移表逐行核;看最近的 diff 是否单边动过布局。
2. **缺失 `pcc_gc_load_ptr()` 屏障(后端 #3/#4)。** 指针槽必须经 `pcc_gc_load_ptr()` 读、`pcc_gc_store_ptr()` 写;裸读 `obj->slot` 在默认后端 #0 上完全正常,在分代 #3 / 重定位 #4 上可能拿到搬迁前的旧地址——旧地址上的"类"读出来的方法表是噪声。验证:`PCC_GC_BACKEND=0` 复跑,若症状消失,几乎可以锁定某条新加的裸槽访问;然后在涉事路径上找没走屏障的读写。(屏障语义本身见第 10 章。)
3. **缺失 `py_err_occurred()` 检查。** pcc 的异常是"存 TLS、正常返回"(第 8 章):一个更早的调用失败后,若生成代码或运行时漏检 `py_err_occurred()`,NULL 会被当成"没找到"继续传播,最终在不相干的属性上报错——或者反过来,挂起的旧异常让 `py_obj_missing_attr()` 放弃报错,症状漂移到更远处。验证:在症状点回溯最近一次可抛调用,检查每个调用点之后是否有 err-check 分支。

口诀的最后一句同样重要:"Check in that order before suspecting frontend codegen."——这三因都是对象模型/运行时层的,而且都比"前端把属性名低层化错了"常见。先排掉便宜且高概率的,再去翻代码生成。

## 7.7 历史与教训

本节的三个故事都来自 [docs/investigations/](../../docs/investigations) 的实录,按 STYLE 的格式:症状 → 错误假设 → 证据链 → 真正根因 → 留下的不变式。

### 故事一:类变量的三幕剧——`attrs` 槽是怎么长出来的

类级变量(`class C: x = 1`)的运行时存储经历了三个形态,完整体现镜像纪律如何约束设计。

第一幕([docs/investigations/goal-data-model-b3-classvar-v2-0416-0425.md](../../docs/investigations/goal-data-model-b3-classvar-v2-0416-0425.md) 的 "Why v2" 一节):最初的尝试直接给 `PyClassObject` 加一个 `attrs` 字段。被否——理由原文写得很白:"That was wrong for pcc because `py_class.py` mirrors `PyClassObject` using hard-coded offsets. Changing the C layout would silently desynchronize the pcc-Python runtime mirror."(端口用硬编码偏移镜像布局,单边改 C 会静默解同步。)注意这不是"不能改布局",而是那一刻的变更没有准备好支付双边代价。

第二幕(同文件):v2 改为 C 侧表 `PccClassAttrsNode{cls, attrs, next}`,字典 `pcc_gc_pin()` 钉住,给追踪式后端一个稳定根,"without adding a new class-layout trace edge"——布局不动,代价是对象图里多了一条藏在侧表里的边。

第三幕(今天的 `py_class_attrs.c` 头注释 + `py_internal.h`):`attrs` 最终还是进了 `PyClassObject`(offset 104,拥有引用),侧表退化为不拥有字典的指针索引;动机也写在注释里——"so moving collectors can trace and update the edge directly",移动型收集器需要这条边长在对象本体上。这一次双边一起改:端口的 docstring 与全部字面量同步到 120 字节。配套的 Backend #3 生产化调查([docs/investigations/gc-backend3-class-metadata-slot-rewrite.md](../../docs/investigations/gc-backend3-class-metadata-slot-rewrite.md))则补上了另一半:`bases[]`、`mro[]`、`methods[].func`、`del_method` 这些**借用**槽虽不参与引用计数,但在分代提升时必须被改写到搬迁后的地址——焦点测试先以 `['1','0','0','0']` 实证失败(方法装上了,槽没改写),修复后 C 与端口两个运行时各自过闸(gate)。

**留下的不变式:** 布局是镜像间的公共财产,改它是一次双边事务;"借用引用"描述所有权,不描述 GC 可见性——类元数据槽全部都是对象图的边。

### 故事二:pcc1 丢失 `_generator_ctx`——default-None 槽与 setattr 的陷阱(2026-05-11)

症状:自举回归。`pcc1`(stage1 自产编译器)编译任何含生成器的文件都报 `Layer 1 unknown function _yield`;同一份源码 stage0(宿主 CPython 跑 pcc)编译正常。基线 2026-05-01 还是绿的,中间约 30 个提交([docs/investigations/pcc1-self-host-generator-ctx-slot.md](../../docs/investigations/pcc1-self-host-generator-ctx-slot.md))。

错误假设一(提案 No.1,DENIED):怀疑新加的 yield-sentinel 缓存腐坏。短路缓存后症状不变,且探针证明检测函数每次都返回正确结果——bug 在发射路径下游。

错误假设二(提案 No.2,DENIED):在 `L1CodeGen.__init__` 里预声明 `self._generator_ctx = None`,让属性"存在"。重建 pcc1 后用 stderr 探针观测:`_emit_generator_resume_function` 明明执行了 `self._generator_ctx = {...}`,随后每条语句发射处读回的却仍是 `None`——**赋值不持久**。调查把它对上了已记录的模式:default-None 的 dataclass 风格槽,之后用 `obj.attr = value` 改写,在 pcc1 的运行时 setattr 路径下不可靠;预填 `None` 不是预订了一个可写槽,只是把陷阱埋得更早。

修复(提案 No.3,CONFIRMED):换成 `self._generator_ctx_stack: list = []`,构造时就给槽一个**真实容器对象**,此后只 `append`/`pop` 原位变异,从不重赋属性——槽的值身份(那个 list)终生不变,完全绕开了 setattr 路径。pcc1 随即能编译生成器;接着暴露的 `Value.bitcast` 实参数错误被证明是**第二个独立失败**(同文件 Update 段:局部 `tmp_builder = ir.IRBuilder(entry)` 别名的赋值期注册在 pcc1 下不生效,workaround 是改用 `self.builder` 直写),两条证据链分开记录——这正是自举回归纪律第 3 条"分离堆叠失败"的范本。

诚实的部分必须写全:**底层根因至今未被隔离**。调查明确标注 list-as-stack 与 builder 直写都是 workaround,`_emit_assign` 在 pcc1 下的分歧仍是开放问题,后续审计指向所有"方法内首次 `self.X = ...` 而 `__init__` 未声明"的属性。

**留下的不变式:** 在自举敏感的代码里,实例槽在构造时就绑定最终的容器对象,用原位变异代替属性重赋;一个 bootstrap 回归 = 一条边界 + 一条证据链,叠加失败拆开记。

### 故事三:字段索引属于谁——schema 测试的漂移

症状:`tests/python/test_py_class_export_schema.py::test_pcc_cross_module_class_schema_matches_local_layout` 在 layer1 拆分(第 6 章)后失败:`L1CodeGen.__init__.self.env not found`([docs/investigations/python-class-export-schema-test-mixin-init-drift.md](../../docs/investigations/python-class-export-schema-test-mixin-init-drift.md))。

测试原本 AST 解析 `layer1.py`,按 `__init__` 里 `self.env` 出现的源序推断字段索引,再断言 IR 里 `py_instance_get_field` 用同一索引。mixin 重构后 `__init__` 移进了 `layer1_entrypoints.py`/`layer1_init.py`,把解析目标指过去只是部分修复(PARTIAL):AST 源序给出 38,IR 实际是 94。根因:pcc 的类布局来自**整个 mixin 栈合并后的 `ClassInfo.field_names`**(7.4 节"代码生成合并声明集"的另一面),不来自任何单个 `__init__` 的语句顺序。最终修复(CONFIRMED)是把断言改写为跨发射一致性:抓出所有 `%self.env.* = @py_instance_get_field(...)` 行,断言**所有读者用同一个索引**——保住真正要守的不变式(跨模块一致),丢掉已失效的耦合(源位置)。

**留下的不变式:** 字段索引是合并后字段表的属性;任何"从源码位置推槽位"的工具(测试、调试脚本、人脑)在继承与 mixin 面前都会漂移。守恒的是"全体发射点一致",不是"等于某个源序"。

## 7.8 小结

pcc 的对象模型由四个相互咬合的决定构成。16 字节对象头(`refcount@0`、`type_tag@8`、`flags@12`)用整数标签而非类型指针,换来无解引用的分派、可防御的指针验证、五 GC 共用的头格式与可镜像性。标记小整数通道征用指针 bit 0,让 `int` 的值投影零分配,代价是全运行时的"先问 tagged"纪律。`PyClassObject` 的 120 字节把 MRO、线性方法表、静态字段名表、`del_method`/`attrs`/`metaclass` 三个尾槽压进一个 ABI 布局,实例则是"类指针 + 静态槽 + 一个隐藏字典槽",属性协议按描述符优先级分七层,`AttributeError` 只有一个出生地。最后,当前生产实现归 pcc-Python 所有,C 保留为 ABI 声明与差分 oracle;逐字节一致仍是用纪律保证的契约。于是排查属性丢失的顺序永远是:布局、屏障、错误检查,然后才轮到前端。

对象怎么死、引用怎么算,翻到第 9 章;这些槽位如何被五个收集器遍历、改写、搬迁,翻到第 10、11 章。

## 练习

1. **(读源码)** 对照 [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) 手算 `PyClassObject` 全部 15 个字段的偏移,标出四处 4 字节填充的位置,验证 120 字节与 `del_method@96/attrs@104/metaclass@112`;再对照 [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) 的 docstring 与代码中的字面量,找出端口读写每个槽的所有位置。
2. **(审计)** `PY_TYPE_VALUEBOX = 200` 落在用户类标签空间内,而 `py_class.c` 的 `g_next_user_tag` 从 104 起单调递增且无避让。第 97 个领到标签的用户类会与之相撞。读 `py_obj_ops_compare.c`、`py_weakref.c`、`py_format.c` 中所有消费 `PY_TYPE_VALUEBOX` 的判断,描述相撞后的可观察症状;给出两种修复(分配器跳号 / 迁移 VALUEBOX 标签)并论证各自对镜像与已发射代码的代价。
3. **(行为收敛证明)** C oracle 的 `py_class_new()` 预填 `del_method`,pcc-Python 生产实现不预填。借助 `py_user_del_dispatch()` 的懒补逻辑,论证两者对任何用户程序不可区分;再构造一个(只能用运行时内部探针观察到的)差异点,说明为什么"可观察 ABI 等价"是比"逐语句等价"更合理的差分标准。
4. **(设计权衡)** `PY_CLASS_FLAG_SLOTS_ONLY` 复用了 `PY_FLAG_GC_TRACKED` 的 bit 0x2,安全前提是类对象从不进入 `py_gc_track()`。假设未来要让类对象参与循环收集(例如支持运行期类的卸载),列出这个 bit 复用会以什么症状暴露,并提出迁移方案(提示:`py_internal.h` 的 flags 空间还剩哪些位?端口里有多少处 `flags & 2` 需要同步?)。
5. **(测量设计)** `py_class_lookup()` 是线性扫描,注释承诺"future phase can swap to a hashmap"。设计一个实验回答换哈希是否值得:应测量哪些真实负载(提示:自举 stage2 编译、[tests/python/](../../tests/python) 下的类密集用例)、统计什么分布(每类方法数、查找命中深度)、以及在什么阈值下结论成立。说明为什么微基准在这里会误导。
