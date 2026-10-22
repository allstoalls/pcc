# Chapter 20: Declarative GUI — Components, Scheduling, and a Webview-Free Application Boundary

> The GUI framework and its canary moved to
> [allstoalls/pcc-gui](https://github.com/allstoalls/pcc-gui) on 2026-09-06; the
> core repository has no GUI code. Paths below describe the design as it stood
> at core commit `977ad074`, the last one that contains it.

pcc's GUI is neither a browser embedded in a native window nor a copy of the React, Tailwind, or Tauri APIs in Python. Its objective is to let pcc-compiled Python own the path from state to drawing commands: bounded component records produce descriptors; keyed reconciliation produces effects; an atomic commit updates a reclaimable composition tree; event paths enter state queues; priority scheduling triggers local rerendering; class strings compile into cached typed operations; and commands and lifecycle use explicit request/result/error state machines to connect the application to the native window. This chapter records two kinds of fact at once: the pcc-Python GUI source and canary that are present in the repository, and the formal acceptance surface that the structured task board has not yet marked `DONE_STRONG`. Source presence does not mean every gate has passed, and conceptual absorption does not mean upstream API compatibility.

## 20.1 The Problem and Design Space: Why GUI Belongs to Execution Ownership

A GUI can look like a peripheral library, but it forces the compiler and runtime to confront long-lived state, callback ABIs, native handles, event ordering, incremental work, and shutdown cleanup. If those capabilities can be supplied only by CPython, Electron/WebView, or an external UI runtime, pcc still does not own the application's execution root. The GUI is therefore a product-level stress test of Chapter 1's thesis, not a sixth research mission.

There are three common routes:

1. Wrap platform controls directly. This is fast to implement, but state, layout, and event semantics become distributed through Objective-C/AppKit or another host, making deterministic headless comparison difficult.
2. Embed a WebView. Components and CSS arrive ready-made, but execution ownership moves to a browser runtime; no-libpython does not mean no web runtime.
3. Let pcc-Python own the composition tree, components, scheduler, styles, and command state machines, leaving only windowing, drawing, and OS event ingress behind named platform ABIs. This has the largest implementation surface but permits the entire state transition to be tested without a window.

pcc takes the third route. It absorbs three bounded mechanism families: React-style queued reducers, lanes, keyed render/commit, and effect phases; Tailwind-style namespaced tokens, utility generators, and candidate parse/compile/cache; and Tauri-backend-style targeted listeners, managed state, command resolution, and run lifecycle. It does not claim wire compatibility, the full React hook model, full CSS semantics, or a WebView API.

## 20.2 Layering: Description, Commit, and Drawing Each Have One Owner

The current source divides as follows:

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

This ownership split answers the critical question: who may mutate the committed tree? An application component may write only into a caller-owned descriptor arena. The scheduler may only select pending state updates. Styles may only produce typed operations. Component commit alone may invoke structural kernel mutations. The kernel owns nodes, layout, hit paths, and drawing commands, but never calls component callbacks. A failed render therefore cannot expose half a tree to event dispatch or drawing.

`pcc_gui_kit.py` owns a bounded, reclaimable node pool. A node id combines a generation with a slot index; a reused slot receives a new id, and stale ids fail closed. The 208-byte record carries parent/child/sibling links, geometry, layout, text, padding and gap, clipping, scrolling, and event flags. A reclaimable tree is harder than an append-only one because detach, reorder, and destroy must preserve focus, hover, owner routing, and stale-id detection. Yet keyed component commit cannot rest on a pool that leaks forever.

The v2 routing surface returns a complete leaf-to-root path and does no kernel-side bubbling:

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

Insufficient capacity yields the negative required length rather than a partial path. That choice makes event dispatch transactional: the listener owner either sees the complete ancestry or dispatches nothing.

## 20.3 The Frozen ABI: Bound Records Before Syntax Convenience

[gui_declarative_contract_v1.json](../../pcc/runtime/gui_declarative_contract_v1.json) is the machine-readable ABI authority. It freezes capacities, byte order, alignment, record fields, owners, lifetimes, error codes, lane aging, effect phases, command completion, and application transitions. `PccGuiRenderContextV1` is an 80-byte caller-owned record, `PccGuiDescriptorV1` is 72 bytes, and child identity is `(parent_component_id, key, node_kind)`. A same-key child with a different node kind is replaced, never incorrectly reused.

There are two reasons to freeze raw records before allowing arbitrary Python objects through callbacks. First, bootstrap and the self backend need one fixed ABI. Second, GC ownership remains auditable. A v1 state slot admits only `i64` and an opaque handle with explicit retain/release. `managed_ref` has a reserved kind in the contract, but cannot enter production records until it joins root registration, write barriers, tracing, and relocation updates and passes GC0 through GC4. Hiding a semantic object in `i64` would evade the collector and is explicitly forbidden.

After a component callback fills a descriptor arena, `pcc_gui_component_render_commit()` validates the ABI, capacities, keys, node owners, and resource budget before staging new nodes. The entire sibling order is then committed through the kernel. Its entry checks demonstrate the fail-closed policy:

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

The tempting alternative is for a render callback to call `pcc_kit_create` and `destroy` directly. Error recovery would then require reversing a visible mutation log, and event dispatch could observe intermediate nodes. Descriptor and effect arenas cost memory but reduce rollback to discarding work-in-progress.

## 20.4 State Queues and Lanes: Priority Is Not a Dirty Bit

Each component update records a global enqueue sequence, lane, slot, SET or reducer action, operand, and ownership data. The four lanes are discrete, animation, default, and background. When a lower-priority update is skipped, the scheduler records the state immediately before it and keeps later processed updates that must replay. Rebase still converges in original enqueue order. Without that rule, “low-lane SET(5), then high-lane +1” could settle permanently at 1 or 5 rather than 6.

Lane selection also ages work. Background waiting 32 epochs, default waiting 8, and animation waiting 2 can override ordinary priority so low lanes do not starve:

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

The implementation has a blocking synchronous drain and a budgeted yield/resume loop. Budget exhaustion discards the work snapshot; it cannot commit partial descriptors. Higher-priority work may restart uncommitted lower-priority work. Reducers must therefore be pure, deterministic, and retryable. Each queued or base-queue copy of an opaque handle owns an explicit reference, and cancellation or failure must release it exactly once. The design absorbs React's update-replay problem without copying Fiber objects or the full hook API.

One frame has this logical order:

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

## 20.5 Events, Styles, Commands, and Application Lifecycle

### 20.5.1 One Event-Dispatch Owner

`pcc_gui_events.py` is the sole owner of component callback dispatch. A listener record contains listener id, target component, event type, callback id, and policy context; the kernel supplies only the painted hit path. Dispatch goes target then bubble, with capture outside this selected contract. Unmount removes listeners, cancels component work, and clears focus and hover routes.

Effect phases are before-mutation snapshot, mutation-time layout cleanup, structural mutation, layout creation, passive cleanup, and passive creation. Synchronous cleanup cannot be deferred to the passive phase because an old node's native handle would overlap the replacement. Callback failures are recorded, but the cleanup chain continues so one owner cannot leak the next owner's resources.

### 20.5.2 A Bounded Utility Compiler, Not a CSS Engine

`pcc_gui_theme_anim.py` remains the sole owner of numeric token values. `pcc_gui_style.py` adds colour, font, size, and spacing namespaces; a utility registry; dependency generations; and a bounded class grammar. The accepted prefixes are only `bg`, `text`, `font`, `w`, `h`, `pad`, `gap`, `x`, and `y`. A negative prefix and `/modifier` are distinct constructs. Compiled immutable 40-byte operations record exact token and namespace generations, so only a relevant theme edit invalidates the cache entry and component.

A cache hit does not parse again; it only updates the usage epoch and hit counter:

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

This is not Tailwind compatibility. It absorbs candidate→generator→cached-operation mechanics. There is no complete CSS cascade, responsive variant system, or arbitrary plugin execution. The bounded grammar is both a freestanding/self-hosting constraint and the source of deterministic diagnostics.

### 20.5.3 A Command Is Not Direct `set_state`

`pcc_gui_commands.py` version-supersedes the old `pcc_gui_binding` tables so property, binding, and command storage have one owner. An invoke packet includes request id, command id, target id, payload, policy context, and resolver id. Synchronous and asynchronous calls finish through the same resolver as result, structured error, or cancellation. Duplicate and late completion fail closed. A local UI handler may enqueue state directly, but that path is not a command boundary.

Managed state v1 likewise admits only scalar values and opaque handles. A future Python-object state kind must join Chapter 10's five-GC slot contract; the GUI may not grow a second private root table.

### 20.5.4 A Webview-Free Run Lifecycle

`pcc_gui_app_lifecycle.py` accepts `Ready`, `Resumed`, `MainEventsCleared`, native `WindowEvent`, Darwin `Opened` and `Reopen`, cancellable `ExitRequested`, and exactly-once `Exit`. A native adapter first copies the payload into the bounded owner queue. `MainEventsCleared` is the point at which UI work drains before layout and rendering. Accepted exit shuts down scheduler work, command resolvers and state, components/listeners/effects, passive effects, and the native window handle, then delivers `Exit`.

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

There is no `WebviewEvent` kind. Menu and tray input remain ordinary targeted events. The absorbed idea is Tauri's separation of event, managed state, command, and lifecycle—not the Tauri runtime or wire protocol.

## 20.6 Rendering and the Product Canary

The base GUI surface also includes `pcc_gui_layout.py`, `pcc_gui_elements.py`, `pcc_gui_controls.py`, `pcc_gui_window.py`, `pcc_gui_text.py`, `pcc_gui_image.py`, and `pcc_gui_cg.py`. The CoreGraphics route provides two-dimensional drawing through dynamically resolved native symbols. The Metal route in `projects/mac_diff_app/` feeds kernel command lists to an AppKit/Metal bridge. Headless semantic tests should establish layout, events, and state; a separate Darwin hardware gate should establish render/present reachability. Without permission-gated capture or a pixel differential, bridge acknowledgement is not pixel correctness.

`projects/mac_diff_app/declarative_app.py` is the current source canary. It preserves the dual-pane diff's `LINES_L`/`LINES_R` and thirteen-operation, five-changed-row semantics while referencing components, scheduler, events, the style compiler, managed state, command resolver, and application lifecycle. `declarative_headless.py` is the non-window entry; `app.py` is the native entry. The product direction is not per-frame manual node editing, but `state -> descriptors` and `events -> state`.

## 20.7 Current Status and Claim Hygiene

The repository contains three distinct evidence levels:

| Level | Current fact | Wording permitted here |
|---|---|---|
| Source | Kernel, components, scheduler, events, style, commands, lifecycle, and canary files exist and are in the runtime build lists | “source-present implementation” |
| Test definitions | Headless, current-pcc1, GC0–GC4, and Darwin bridge/lifecycle nodes exist under `tests/python/` | “gate exists,” not “passed in this work” |
| Structured task board | The `GUI-P2-*` rows remain `TODO_READY`, with the design document as latest evidence | Formal absorption/product closure has not been accepted |

This book update did not run GUI compilation or hardware gates. It therefore does not convert source files and test names into a `DONE_STRONG` claim. Formal completion requires separate host-pcc and current-pcc1 strict self/no-libpython runs; a headless canary across GC0 through GC4; a Darwin window gate with bridge-side render/present acknowledgement; a native lifecycle trace showing real `WindowEvent` and `Opened/Reopen` delivery; and source identity bound to every result.

## 20.8 History and Lessons

### 20.8.1 Three Kernels and the Wrong Kind of “Working” Host (August 2026)

The first GUI state had three owners: `pcc/runtime/py/pcc_gui_kit.py`, `projects/mac_diff_app/pcc_gui_kit.py`, and a smaller kernel inlined into `app.py`. The build used `app.py`, while the accepted split-line-table and changed-row coalescing behavior lived in `kit_window.py`. Existing tests covered an older control ABI and pre-loop statistics rather than direct kernel render/event behavior. Each copy could support a demonstration; none simultaneously owned production build selection, accepted product semantics, and direct kernel evidence.

The defect was not cosmetic duplication. Evidence had no stable subject: a test might exercise a shadow while the application linked another owner. Later source makes `pcc_gui_kit.py` canonical, adds generation ids, reclamation, structural mutation, and complete hit paths, and has the declarative application use runtime modules through externs. The resulting invariant is that the UI tree, listener registry, theme table, and command table each have exactly one production owner. Project-local shadows may survive only as temporary oracles.

### 20.8.2 Two Compiler Boundaries Exposed by GUI

One GUI demo observed a class method calling a four-argument extern with its first two integers incorrectly tagged as objects: `0x4000000000|100` reached the animation record. Multiple minimized forms did not reproduce it; only the full import graph showed it once. The investigation therefore stayed open and used a direct ABI call as a workaround rather than declaring a general repair. A second defect was firmer: a module-level native pointer was registered as a Python GC root and then passed to `pcc_gc_pin`, causing a crash. The workaround stores the pointer as `i64` in an explicit raw global array. The real fix is type-aware module-root registration in the frontend.

Together they explain why GUI is a runtime proving ground: long-lived raw handles, callback arity, module globals, GC roots, and native ABI calls meet in one program. A workaround must not become a language rule. The caller-owned context record is a narrow GUI ABI choice; general class-method argument tagging and module-root typing remain separate compiler responsibilities.

## 20.9 Summary

pcc's GUI core is not a widget catalog but an ownable state-transition path. The canonical kernel owns reclaimable nodes, layout, clipping, paint-order hit paths, and drawing commands. Components reconcile bounded descriptors and commit atomically. The scheduler preserves global enqueue order across four lanes, aging, interruption, and replay. Events exclusively own target/bubble dispatch and effect phases. Styles compile a bounded candidate grammar into generation-sensitive immutable operations. Commands and application lifecycle use exactly-once state machines for application boundaries and shutdown cleanup. CoreGraphics, Metal, and AppKit occupy named native boundaries without introducing a WebView runtime.

The source and canary exist, but the structured `GUI-P2-*` acceptance tasks remain `TODO_READY`, and this book update did not execute their gates. The accurate conclusion is therefore: declarative GUI mechanisms are visible in current source; formal current-pcc1, GC0–GC4, and Darwin product closure still awaits evidence-bound acceptance.

## Exercises

1. Read `pcc_kit_destroy_subtree()`, `pcc_kit_replace_children()`, and `_valid()` in [pcc_gui_kit.py](../../pcc/runtime/py/pcc_gui_kit.py). Show how generation ids prevent an event for a destroyed node from reaching a reused slot.
2. Apply the contract's update rules to “low-lane SET(5), then high-lane reduce(+1)” and “low-lane reduce(+1), then high-lane SET(5).” Explain why the base queue must retain processed updates after the first skipped one.
3. Compare `pcc_kit_route_event_v2()` with legacy `pcc_kit_route_event()`. Design a regression proving that one click cannot bubble once in the kernel and again in the component registry.
4. Read [pcc_gui_style.py](../../pcc/runtime/py/pcc_gui_style.py) and derive the candidates, modifiers, operations, and generation dependencies for `bg-accent/50 -x-3/[dense]`. Explain why warm application should neither parse nor allocate.
5. Design a mode-labeled evidence matrix for `mac_diff_app`: host-pcc headless, current-pcc1 self/no-libpython across GC0–GC4, Darwin render/present reachability, and pixel correctness. Mark which cell cannot be inferred from bridge acknowledgement alone.
