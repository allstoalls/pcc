"""Switch/case lowering for the C frontend."""

from __future__ import annotations

from pcc.ir.compat import ir_c as ir

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.codegen.c_declaration_state import CodegenError
from pcc.frontends.c.codegen.c_types import int32_t, int64_t


class CSwitchFlowMixin:
    def codegen_Switch(self, node):

        cond_val, _ = self.codegen(node.cond)
        # Switch requires integer condition
        if isinstance(cond_val.type, ir.PointerType):
            cond_val = self.builder.ptrtoint(cond_val, int64_t)
        elif self._is_floating_ir_type(cond_val.type):
            cond_val = self.builder.fptosi(cond_val, int64_t)
        elif isinstance(cond_val.type, ir.IntType) and cond_val.type.width < int32_t.width:
            cond_val = self._integer_promotion(cond_val)

        after_bb = self.builder.function.append_basic_block("switch_end")

        # Preserve C switch semantics: grouped case labels and fallthrough
        # share code by jumping into the next label block, not directly to
        # the switch epilogue.
        if isinstance(node.stmt, c_ast.Compound):
            switch_items = list(node.stmt.block_items or [])
        elif node.stmt is not None:
            switch_items = [node.stmt]
        else:
            switch_items = []
        prelabel_items = []
        hoisted_items = []
        labels = []
        label_bodies = {}

        label_ids = set()

        def contains_switch_label(item):
            return self._stmt_contains_switch_label(item)

        def add_label(item):
            if id(item) in label_ids:
                return
            label_ids.add(id(item))
            labels.append(item)
            label_bodies.setdefault(id(item), [])

        def collect_nested_labels(item):
            if item is None or isinstance(item, c_ast.Switch):
                return
            if isinstance(item, (c_ast.Case, c_ast.Default)):
                add_label(item)
                for child in item.stmts or []:
                    collect_nested_labels(child)
                return
            for _name, child in item.children():
                if isinstance(child, list):
                    for entry in child:
                        collect_nested_labels(entry)
                else:
                    collect_nested_labels(child)

        def collect_guarded_label_sequence(items, active_label=None):
            active = active_label
            for item in list(items or []):
                if isinstance(item, (c_ast.Case, c_ast.Default)):
                    add_label(item)
                    active = collect_guarded_label_sequence(item.stmts or [], item)
                    continue
                if isinstance(item, c_ast.Compound):
                    if contains_switch_label(item):
                        active = collect_guarded_label_sequence(
                            item.block_items or [], active
                        )
                    elif active is not None:
                        label_bodies[id(active)].append(item)
                    continue
                if isinstance(item, c_ast.If) and contains_switch_label(item):
                    if active is not None:
                        label_bodies[id(active)].append(item)
                    collect_guarded_label_bodies(item)
                    continue
                if contains_switch_label(item):
                    if active is not None:
                        label_bodies[id(active)].append(item)
                    collect_nested_labels(item)
                    continue
                if active is not None:
                    label_bodies[id(active)].append(item)
            return active

        def collect_guarded_label_bodies(item):
            if item is None or isinstance(item, c_ast.Switch):
                return None
            if isinstance(item, (c_ast.Case, c_ast.Default)):
                add_label(item)
                return collect_guarded_label_sequence(item.stmts or [], item)
            if isinstance(item, c_ast.Compound):
                return collect_guarded_label_sequence(item.block_items or [], None)
            if isinstance(item, c_ast.If):
                active_true = collect_guarded_label_bodies(item.iftrue)
                active_false = collect_guarded_label_bodies(item.iffalse)
                return active_false or active_true
            for _name, child in item.children():
                if isinstance(child, list):
                    for entry in child:
                        collect_guarded_label_bodies(entry)
                else:
                    collect_guarded_label_bodies(child)
            return None

        def process_items(items, active_label):
            active = active_label
            items = list(items or [])
            for idx, item in enumerate(items):
                later_has_label = any(
                    contains_switch_label(later) for later in items[idx + 1 :]
                )
                if isinstance(item, (c_ast.Case, c_ast.Default)):
                    add_label(item)
                    active = process_items(item.stmts or [], item)
                    continue
                if isinstance(item, c_ast.Compound) and contains_switch_label(item):
                    active = process_items(item.block_items or [], active)
                    continue
                if isinstance(item, c_ast.If) and contains_switch_label(item):
                    if active is None:
                        prelabel_items.append(item)
                    else:
                        label_bodies[id(active)].append(item)
                    collect_guarded_label_bodies(item)
                    continue
                if contains_switch_label(item):
                    if active is None:
                        prelabel_items.append(item)
                    else:
                        label_bodies[id(active)].append(item)
                    collect_nested_labels(item)
                    continue
                if active is None:
                    prelabel_items.append(item)
                    continue
                if isinstance(item, c_ast.Decl) and later_has_label:
                    if item.init is not None:
                        raise CodegenError(
                            "switch-scope declaration before later case with initializer is not supported"
                        )
                    hoisted_items.append(item)
                    continue
                if later_has_label and isinstance(
                    item, (c_ast.Typedef, c_ast.EmptyStatement)
                ):
                    hoisted_items.append(item)
                    continue
                label_bodies[id(active)].append(item)
            return active

        process_items(switch_items, None)

        label_blocks = {}
        default_bb = after_bb
        for item in labels:
            bb_name = (
                "switch_default" if isinstance(item, c_ast.Default) else "switch_case"
            )
            bb = self.builder.function.append_basic_block(bb_name)
            label_blocks[id(item)] = bb
            if isinstance(item, c_ast.Default):
                default_bb = bb

        with self.new_scope():
            self.define("break", after_bb)

            for item in prelabel_items + hoisted_items:
                if isinstance(item, c_ast.Decl):
                    if item.init is not None:
                        raise CodegenError(
                            "switch-scope declaration before first case with initializer is not supported"
                        )
                    self.codegen(item)
                elif isinstance(item, (c_ast.Typedef, c_ast.EmptyStatement)):
                    self.codegen(item)

            switch_inst = self.builder.switch(cond_val, default_bb)

            for item in labels:
                if not isinstance(item, c_ast.Case):
                    continue
                # Case values must be compile-time constants
                try:
                    const_int = self._eval_const_expr(item.expr)
                    case_val = ir.Constant(cond_val.type, const_int)
                except Exception:
                    case_val, _ = self.codegen(item.expr)
                    if case_val is None:
                        continue
                    if not isinstance(case_val, ir.Constant):
                        # Non-constant case: skip (LLVM requires constants)
                        continue
                    if case_val.type != cond_val.type:
                        case_val = ir.Constant(
                            cond_val.type, self._constant_raw_value(case_val)
                        )
                switch_inst.add_case(case_val, label_blocks[id(item)])

            self._switch_contexts.append({"blocks": label_blocks})
            try:
                for idx, item in enumerate(labels):
                    self.builder.position_at_end(label_blocks[id(item)])
                    for stmt in label_bodies.get(id(item), []):
                        if self.builder.block.is_terminated:
                            if isinstance(stmt, c_ast.Label):
                                self.codegen(stmt)
                            continue
                        self.codegen(stmt)
                    if not self.builder.block.is_terminated:
                        next_bb = after_bb
                        has_nested_switch_labels = any(
                            self._stmt_contains_switch_label(stmt)
                            for stmt in label_bodies.get(id(item), [])
                        )
                        if idx + 1 < len(labels) and not has_nested_switch_labels:
                            next_bb = label_blocks[id(labels[idx + 1])]
                        self.builder.branch(next_bb)
            finally:
                self._switch_contexts.pop()

        self.builder.position_at_end(after_bb)
        return None, None

    def codegen_Case(self, node):
        if not self._switch_contexts:
            return None, None
        label_bb = self._switch_contexts[-1]["blocks"].get(id(node))
        if label_bb is None:
            return None, None
        if self.builder.block is not label_bb and not self.builder.block.is_terminated:
            self.builder.branch(label_bb)
        self.builder.position_at_end(label_bb)
        for stmt in node.stmts or []:
            self.codegen(stmt)
        return None, None

    def codegen_Default(self, node):
        if not self._switch_contexts:
            return None, None
        label_bb = self._switch_contexts[-1]["blocks"].get(id(node))
        if label_bb is None:
            return None, None
        if self.builder.block is not label_bb and not self.builder.block.is_terminated:
            self.builder.branch(label_bb)
        self.builder.position_at_end(label_bb)
        for stmt in node.stmts or []:
            self.codegen(stmt)
        return None, None
