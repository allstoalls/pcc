# 第 20 章 声明式 GUI：组件、调度与无 WebView 应用边界

> GUI 框架及其 canary 已于 2026-09-06 迁至
> [allstoalls/pcc-gui](https://github.com/allstoalls/pcc-gui)，核心仓库不再包含 GUI
> 代码。下文路径描述的是核心提交 `977ad074`(最后一个包含它的版本)时的设计。

pcc 的 GUI 不是把浏览器嵌进原生窗口，也不是把 React、Tailwind 或 Tauri 的 API 复制进 Python。它的目标是让 pcc 编译的 Python 程序拥有从状态到像素命令的执行链：有界组件记录产生描述符，keyed reconcile 生成效果，原子 commit 更新一棵可回收的组合树，事件路径进入状态队列，优先级调度触发局部重渲染，样式字符串编译成缓存的定型操作，命令与生命周期通过明确的 request/result/error 状态机连接应用和原生窗口。本章同时写两类事实：仓库当前已有的 pcc-Python GUI 源码与 canary；以及尚未被结构化任务板标成 `DONE_STRONG` 的正式接受面。源码存在不等于所有闸门已经通过，设计吸收也不等于上游 API 兼容。

## 20.1 问题与设计空间：为什么 GUI 属于执行所有权

GUI 表面上像一个外围库，实际上会迫使编译器和运行时面对长期状态、回调 ABI、原生句柄、事件排序、增量更新与退出清理。若这些能力只能通过 CPython、Electron/WebView 或外部 UI runtime 提供，那么 pcc 仍未拥有应用的执行根。GUI 因而是第 1 章论题的产品级压力测试，而不是第六个研究使命。

设计空间有三条常见路线：

1. 直接包一层平台控件。实现快，但状态、布局与事件语义分散在 Objective-C/AppKit 等宿主里，难以在 headless 模式下做确定性差分。
2. 嵌入 WebView。组件与 CSS 生态现成，但应用执行所有权转移到浏览器 runtime；no-libpython 并不等于 no-web-runtime。
3. 由 pcc-Python 拥有组合树、组件、调度、样式与命令状态机，只把窗口、绘制和 OS 事件留在具名平台 ABI。实现面最大，却能在无窗口 canary 中验证完整状态转换。

pcc 选择第三条。它吸收的是三组**机制**：React 风格的 queued reducer、lane、keyed render/commit 与 effect phase；Tailwind 风格的 namespace token、utility generator、candidate parse/compile/cache；Tauri 后端风格的 target listener、managed state、command resolver 和 run lifecycle。它不声称 wire compatibility、完整 hook/CSS 语义或 WebView API。

## 20.2 分层：描述、提交与绘制各有一个所有者

当前源码的层次如下：

```text
application: components + state + command intent
            |
            v
pcc_gui_components / scheduler / events
  descriptors -> reconcile -> effects -> atomic commit
            |
            +------ pcc_gui_style / theme_anim
            +------ pcc_gui_commands / app_lifecycle
            |
            v
pcc_gui_kit
  reclaimable node tree / layout / clip / hit path / command walk
            |
            v
pcc_gui_cg or Metal/AppKit bridge
  native draw/present/event acknowledgement
```

这份所有权划分解决一个关键问题：谁有权修改已提交的树。应用组件只能写 caller-owned descriptor arena；scheduler 只能选择待处理 state update；style 只能产生定型操作；只有 component commit 能调用 kernel 的结构变更；kernel 只负责节点、布局、命中路径和绘制命令，不调用组件回调。这样一次失败的 render 不会把半棵树暴露给事件或绘制线程。

`pcc_gui_kit.py` 的节点池是有界、可回收的。节点 id 把 generation 与 slot index 组合，销毁后复用槽位会产生新 id，旧 id fail closed。208 字节记录保存 parent/child/sibling、几何、布局、文本、padding/gap、clip、scroll 与 event flag。与 append-only 树相比，可回收树更难，因为 detach/reorder/destroy 必须同时维护 focus、hover、owner route 和 stale-id 检测；但 keyed component commit 不可能建立在永久泄漏的池上。

事件路由的 v2 接口只返回完整的 leaf-to-root 路径，不在 kernel 中执行 bubble：

```python
# pcc/runtime/py/pcc_gui_kit.py
def pcc_kit_hit_path_v1(root: int, x: int, y: int, path_out, capacity: int) -> int:
    """Write the complete leaf-to-root path; never return a partial path."""
    hit = pcc_kit_hit(root, x, y)
    if hit < 0:
        return 0
    needed = 0
    cur = hit
    while cur >= 0 and _valid(cur) != 0:
        needed = needed + 1
        cur = _n8(cur, 0)
    if capacity < needed or ptr_is_null(path_out):
        return -needed
```

完整路径不足容量时返回负的所需长度，而不是写一条部分路径。这条选择使事件 dispatch 具有事务边界：listener owner 要么看到完整 ancestry，要么不 dispatch。

## 20.3 冻结 ABI：为什么先限定记录，再谈语法便利

[gui_declarative_contract_v1.json](../../pcc/runtime/gui_declarative_contract_v1.json) 是机器可读的 ABI 权威。它冻结容量、字节序、对齐、记录字段、owner、lifetime、错误码、lane aging、effect phase、command completion 和 app transition。`PccGuiRenderContextV1` 是 caller-owned 80 字节记录，`PccGuiDescriptorV1` 为 72 字节，组件 child identity 是 `(parent_component_id, key, node_kind)`。同 key 但不同 node kind 必须 replace，不能错误复用。

先冻结原始记录而不是让任意 Python 对象穿过回调，有两个理由。第一，自举/self 后端只需实现一份固定 ABI；第二，GC 所有权可审计。v1 state slot 只允许 `i64` 与显式 retain/release 的 opaque handle。`managed_ref` 虽在 contract 中保留类型位，却必须先加入 root、write barrier、trace 和 relocation update，并过 GC0–GC4，才能进入生产记录。把语义对象伪装在 `i64` 中会逃过收集器，属于明确禁止的做法。

component callback 写 descriptor arena 后，`pcc_gui_component_render_commit()` 先验证 ABI、容量、key、node owner 和资源预算，再 staging 新节点；完整 sibling order 通过 kernel 一次提交。入口的前置检查体现了 fail-closed 策略：

```python
# pcc/runtime/py/pcc_gui_components.py
def pcc_gui_component_render_commit(
    component_id: int,
    descriptor_arena,
    descriptor_capacity: int,
    effect_arena,
    effect_capacity: int,
    effect_count_out,
    error_out,
) -> int:
    _clear_error(error_out)
    if not ptr_is_null(effect_count_out):
        store_i32(effect_count_out, 0, 0)
    component_index = _component_index(component_id)
    if component_index < 0:
        return _set_error(error_out, ERR_STALE_NODE, 0, component_id)
```

备选方案是让 render callback 直接调用 `pcc_kit_create`/`destroy`。那会让错误恢复依赖“反向撤销”一系列可观察 mutation；事件可能在撤销前命中半成品节点。descriptor + effect arena 花费额外内存，却把 rollback 简化为丢弃 work-in-progress。

## 20.4 状态队列与 lane：优先级不是一个 dirty bit

每个组件的 update 记录包含全局 enqueue sequence、lane、slot、SET 或 reducer action、operand 和 ownership 信息。四条 lane 依次为 discrete、animation、default、background。低优先级 update 被跳过时，scheduler 保存“跳过前状态”与后续需要 replay 的记录；之后重放仍按原 enqueue order 收敛。否则“低优先级 SET(5)，随后高优先级 +1”可能永久得到 1 或 5，而不是最终 6。

调度选择同时带 aging。background 等待 32 个 epoch、default 等待 8 个、animation 等待 2 个后可越过常规优先顺序，避免低 lane 饥饿：

```python
# pcc/runtime/py/pcc_gui_scheduler.py
def _select_lane(record) -> int:
    if _lane_pending(record, LANE_BACKGROUND) != 0 and load_i32(record, 76) >= 32:
        return LANE_BACKGROUND
    if _lane_pending(record, LANE_DEFAULT) != 0 and load_i32(record, 72) >= 8:
        return LANE_DEFAULT
    if _lane_pending(record, LANE_ANIMATION) != 0 and load_i32(record, 68) >= 2:
        return LANE_ANIMATION
    lane = LANE_DISCRETE
    while lane <= LANE_BACKGROUND:
        if _lane_pending(record, lane) != 0:
            return lane
        lane = lane + 1
    return -1
```

系统有 blocking sync drain 与 budgeted yield/resume 两种 work loop。预算耗尽只能丢弃 work snapshot，不能提交部分描述符；更高优先级更新可以使未提交的低优先级工作 restart。reducer 因而必须纯、确定且可重试。opaque handle 的 queue/base-queue clone 各自持有一份明确引用，取消与错误必须 exactly once release。这里借鉴 React 的更新重放问题，但不复制 Fiber 对象或完整 hook API。

一帧的逻辑顺序可以表示为：

```text
OS event
   -> kernel hit path
   -> target/bubble listener
   -> enqueue state update
   -> select lane / render descriptors
   -> keyed reconcile / atomic commit
   -> layout / render command walk
   -> native draw or present acknowledgement
```

## 20.5 事件、样式、命令与应用生命周期

### 20.5.1 一个事件 dispatch owner

`pcc_gui_events.py` 是 component callback 的唯一 dispatch owner。listener record 保存 listener id、target component、event type、callback id 和 policy context；kernel 只提供 painted hit path。dispatch 顺序是 target 后 bubble，不包含 capture。unmount 会移除 listener、取消 component work、清理 focus/hover route。

effect phase 依次为 before-mutation snapshot、mutation-time layout cleanup、structural mutation、layout creation、passive cleanup、passive creation。同步 cleanup 不能拖到 passive 阶段，否则被替换节点的原生句柄会与新节点重叠存活。错误会被记录，但清理链继续走完，避免一个 callback failure 泄漏另一个 owner 的资源。

### 20.5.2 有界 utility compiler，而不是 CSS 引擎

`pcc_gui_theme_anim.py` 是 numeric token 的唯一 owner；`pcc_gui_style.py` 增加 colour/font/size/spacing namespace、utility registry、dependency generations 和 bounded class grammar。支持的 prefix 只有 `bg`、`text`、`font`、`w`、`h`、`pad`、`gap`、`x`、`y`；negative prefix 与 `/modifier` 是两种语法。compile 后的 40 字节 immutable operation 记录精确 token/namespace generation，相关主题变化才使 cache 与组件失效。

cache hit 不重新 parse，只更新使用 epoch 和计数：

```python
# pcc/runtime/py/pcc_gui_style.py
    key_hash = _candidate_hash(class_bytes, length)
    index = _cache_find(class_bytes, length, key_hash)
    if index >= 0 and _cache_entry_current(index) != 0:
        entry = _cache_at(index)
        epoch = _next_cache_epoch()
        if epoch < 0:
            return ERR_CAPACITY
        store_i64(entry, 72, epoch)
        store_i64(
            global_addr("pcc_gui_style_cache_hits"),
            0,
            _base("pcc_gui_style_cache_hits") + 1,
        )
        return load_i32(entry, 40)
```

这不是 Tailwind compatibility。设计只吸收 candidate→generator→cached ops 的机制；不存在完整 CSS cascade、responsive variant 或任意插件执行。bounded grammar 是 freestanding/self-host 约束，也是确定性诊断的来源。

### 20.5.3 命令不是直接 `set_state`

`pcc_gui_commands.py` version-supersede 旧的 `pcc_gui_binding` 表，使 property/binding/command 只有一个 owner。invoke packet 包含 request id、command id、target id、payload、policy context 和 resolver id。sync/async 都通过同一 resolver 终结为 result、structured error 或 cancellation；duplicate/late completion fail closed。UI 内部 handler 可以直接 enqueue state，但那不叫 command boundary。

managed state v1 同样只允许 scalar 和 opaque handle。若未来把 Python 对象放进 managed state，它必须和第 10 章的五 GC slot contract 汇合，而不是让 GUI 自己维护第二套根表。

### 20.5.4 无 WebView 的 run lifecycle

`pcc_gui_app_lifecycle.py` 接受 `Ready`、`Resumed`、`MainEventsCleared`、native `WindowEvent`、Darwin `Opened`/`Reopen`、可取消 `ExitRequested` 与 exactly-once `Exit`。native adapter 先把 payload 拷入有界 owner queue；`MainEventsCleared` 是排空 UI work 后再 layout/render 的边界。接受退出后依次关闭 scheduler、command resolver/state、component/listener/effect、passive effect 与 native window handle，最后才发 `Exit`。

```python
# pcc/runtime/py/pcc_gui_app_lifecycle.py
    state = _base("pcc_gui_app_lifecycle_state_value")
    if state == APP_UNINITIALIZED:
        return ERR_OWNERSHIP
    if state == APP_EXITED or state == APP_TERMINATING:
        return ERR_LATE
    if kind == EVENT_EXIT or kind < EVENT_READY or kind > EVENT_EXIT_REQUESTED:
        return ERR_INVALID_TRANSITION
    if _payload_shape_valid(kind, payload, payload_length, flags) == 0:
        return ERR_INVALID_PAYLOAD
    head = _base("pcc_gui_app_lifecycle_head")
    tail = _base("pcc_gui_app_lifecycle_tail")
    cap = _base("pcc_gui_app_lifecycle_capacity")
    if tail - head >= cap:
        return ERR_CAPACITY
```

`WebviewEvent` 根本没有 kind；menu/tray 输入保持普通 targeted event。这里吸收的是 Tauri 后端的 event/state/command 分离，不是 Tauri runtime 或 wire protocol。

## 20.6 绘制与产品 canary

基础 GUI 模块还包括 `pcc_gui_layout.py`、`pcc_gui_elements.py`、`pcc_gui_controls.py`、`pcc_gui_window.py`、`pcc_gui_text.py`、`pcc_gui_image.py` 和 `pcc_gui_cg.py`。CoreGraphics 路线通过动态符号边界提供二维绘制；Metal 路线在 `projects/mac_diff_app/` 的 AppKit/Metal bridge 中接受 kernel command list。两者都应由 headless 语义测试证明布局、事件和状态，由单独的 Darwin hardware gate 证明 render/present reachability；没有截图权限或像素差分时，不能把 bridge acknowledgement 写成 pixel correctness。

`projects/mac_diff_app/declarative_app.py` 是当前 source canary。它保留双栏 diff 的 `LINES_L`/`LINES_R` 和 13-op、五 changed-row 语义，并实际引用 components、scheduler、events、style compiler、managed state、command resolver 与 app lifecycle。headless entry 是 `declarative_headless.py`；`app.py` 是原生入口。产品目标不是每帧手工编辑 node，而是 `state -> descriptors` 与 `events -> state`。

## 20.7 当前状态与声明卫生

当前仓库同时存在三种证据层级：

| 层级 | 当前事实 | 本章允许的表述 |
|---|---|---|
| 源码 | kernel、components、scheduler、events、style、commands、lifecycle 与 canary 文件均存在，并列入 runtime 构建表 | “source-present implementation” |
| 测试定义 | headless、current-pcc1、GC0–GC4、Darwin bridge/lifecycle 节点均在 `tests/python/` | “gate exists”，不能写本次已通过 |
| 结构化任务板 | `GUI-P2-*` 行仍为 `TODO_READY`，最新证据指向设计文档 | 正式吸收/产品 closure 尚未接受 |

本书更新没有运行 GUI 编译或硬件闸门。因此不会把源文件和测试名称转换成 `DONE_STRONG` 声明。正式完成至少要求：host-pcc 与 current-pcc1 strict self/no-libpython 分开；headless canary 参数化 GC0–GC4；Darwin window gate 给出 bridge-side render/present acknowledgement；native lifecycle trace 证明真实 `WindowEvent` 和 `Opened/Reopen` 到达 callback；所有结果绑定当前源身份。

## 20.8 历史与教训

### 20.8.1 三份 kernel 与“看起来可用”的错误宿主（2026-08）

最初的 GUI 同时有 `pcc/runtime/py/pcc_gui_kit.py`、`projects/mac_diff_app/pcc_gui_kit.py` 和 `app.py` 内联的更小 kernel。构建入口使用 `app.py`，被接受的 split-line-table 与 changed-row coalescing 却在 `kit_window.py`；已有测试只覆盖旧 control ABI 和 pre-loop statistics。任何一份都能单独演示，但没有一份同时拥有生产构建、产品语义与直接 kernel 测试。

真正问题不是“重复代码不好看”，而是证据无从归属：一个测试可能验证 shadow，应用却链接另一个 owner。后续源码把 `pcc_gui_kit.py` 定为 canonical owner，加入 generation id、reclaim、structural mutation、完整 hit path，并让 declarative app 通过 extern 使用 runtime 模块。留下的不变式是：UI 树、listener table、theme table 和 command table 都只能有一个生产 owner；project-local shadow 只能作为临时 oracle。

### 20.8.2 GUI 暴露的两个编译器边界

GUI demo 曾出现 class method 调四参数 extern 时前两个整数被错误标成对象，`0x4000000000|100` 写入 animation 记录。多个最小重现都未复现；只有完整 import graph 出现一次，因此调查保持 open，并使用直接 ABI 调用绕过，而没有宣布一般性修复。另一个问题更确定：module-level native pointer 被 GC root scan 当 Python 对象 pin，导致崩溃；workaround 是把 pointer 以 i64 存在显式 raw global array，真正修复要让 frontend 的 module-root registration 感知对象/原生类型。

两件事共同说明 GUI 为什么能成为运行时试金石：它让长寿命 raw handle、回调 arity、module global、GC root 和 native ABI 在同一个程序里相遇。正确反应不是把 workaround 升格为语言规则。caller-owned context record 是本 GUI ABI 的窄选择；通用 class-method tagging 与 module-root typing 仍应在编译器层分别解决。

## 20.9 小结

pcc GUI 的核心不是控件目录，而是一条可拥有的状态转换链。canonical kernel 拥有可回收节点、布局、clip、paint-order hit path 和绘制命令；component 层把有界描述符 reconcile 后原子提交；scheduler 用全局 enqueue order、四 lane、aging 与 replay 保持并发更新的确定性；events 独占 target/bubble dispatch 和 effect phase；style 把有界 candidate grammar 编译为 generation-sensitive immutable ops；commands 与 app lifecycle 以 exactly-once 状态机管理应用边界和退出清理。CoreGraphics/Metal/AppKit 只占具名原生边界，不引入 WebView runtime。

源码与 canary 已经存在，但结构化 `GUI-P2-*` 接受任务仍是 `TODO_READY`，本次书稿工作也没有执行相关闸门。因此准确结论是：声明式 GUI 机制在当前源码中可见，正式的 current-pcc1/GC0–GC4/Darwin 产品 closure 仍待证据收口。

## 练习

1. 阅读 [pcc_gui_kit.py](../../pcc/runtime/py/pcc_gui_kit.py) 的 `pcc_kit_destroy_subtree()`、`pcc_kit_replace_children()` 和 `_valid()`，证明 generation id 如何阻止已回收 slot 的旧事件命中。
2. 用 contract 中的 update 规则手算两组序列：低 lane `SET(5)` 后高 lane `reduce(+1)`，以及低 lane `reduce(+1)` 后高 lane `SET(5)`；说明 base queue 为何必须保存跳过后的所有已处理 update。
3. 比较 `pcc_kit_route_event_v2()` 与 legacy `pcc_kit_route_event()`，设计一个回归，确保同一 click 不会在 kernel 与 component registry 各 bubble 一次。
4. 阅读 [pcc_gui_style.py](../../pcc/runtime/py/pcc_gui_style.py)，为 `bg-accent/50 -x-3/[dense]` 写出 candidate、modifier、operation 与 generation dependency；说明为什么 warm apply 不应 parse 或 allocate。
5. 为 `mac_diff_app` 设计一份模式标注证据矩阵，分别覆盖 host-pcc headless、current-pcc1 self/no-libpython GC0–GC4、Darwin render/present reachability 与 pixel correctness，并标明哪一格目前不能由 bridge acknowledgement 推出。
