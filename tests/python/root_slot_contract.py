"""Test-only physical cell identities for parsed ownership-contract checks.

An SSA name is an address expression, not a cell identity. Resolve constant
GEPs and pointer bitcasts to (alloca, byte offset), then separately prove that
an owning registration covers the entire initialized pointer range. This is
not a replacement for the production verifier or a model of GC execution.
"""


class RootSlotContract:
    POINTER_BYTES = 8

    def __init__(self, blocks, globals_=()):
        self.blocks = tuple(blocks)
        self.rows = [(block, index, ins) for block in self.blocks
                     for index, ins in enumerate(block.instructions)]
        self.allocas = {ins.data[0]: (ins.data[1], block.name)
                        for block, _, ins in self.rows if ins.kind == "alloca"}
        self.aliases = {ins.data[1]: ins.data[3] for _, _, ins in self.rows
                        if ins.kind == "cast" and ins.data[0] == "bitcast"}
        self.geps = {ins.data[0]: ins for _, _, ins in self.rows if ins.kind == "gep"}
        self.globals = {item.name.lstrip("@"): item for item in globals_}

    def symbol(self, value):
        seen = set()
        while value in self.aliases:
            assert value not in seen, "cyclic root address alias"
            seen.add(value)
            value = self.aliases[value]
        return value

    def slot(self, value):
        """Canonicalize addresses without conflating different group cells."""
        offset, seen = 0, set()
        while value in self.aliases or value in self.geps:
            assert value not in seen, "cyclic root address alias"
            seen.add(value)
            if value in self.aliases:
                value = self.aliases[value]
                continue
            ins = self.geps[value]
            current, indices = ins.data[1], ins.data[4]
            assert indices, "root GEP requires a constant byte offset"
            for position, (_, raw_index) in enumerate(indices):
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    raise AssertionError("root GEP requires a constant byte offset") from None
                if position == 0:
                    offset += index * current.slot_size
                elif current.is_array:
                    current = current.elem
                    assert current is not None
                    stride = ((current.slot_size + current.align - 1)
                              // current.align * current.align)
                    offset += index * stride
                elif current.is_struct:
                    assert 0 <= index < len(current.fields), "root GEP field out of range"
                    offset += current.field_offset(index)
                    current = current.field_type(index)
                else:
                    raise AssertionError("root GEP indexes a scalar cell")
            value = ins.data[3]
        return value, offset

    @staticmethod
    def called(ins, name):
        return ins.kind == "call" and ins.data[2] == name

    @staticmethod
    def args(ins):
        return [value for _, value in ins.data[4]]

    def _pointer_cells(self, base):
        assert base in self.allocas, "root address must resolve to an entry alloca"
        allocated, block = self.allocas[base]
        assert block == "entry", "root allocation must dominate every exit"
        if allocated.is_ptr:
            return 1
        assert allocated.is_array and allocated.elem is not None and allocated.elem.is_ptr, (
            "root allocation must contain only contiguous pointer cells"
        )
        return allocated.count

    def require_owning(self, root, *, allow_lifo=False):
        """Require one owning map, full bounds, and pre-enter NULLs.

        Resume operands need function-wide entry registration. Ordinary spawn
        operands retain their audited lexical registrations at the use site.
        """
        if isinstance(root, str):
            root = self.slot(root)
        base, offset = root
        capacity = self._pointer_cells(base) * self.POINTER_BYTES
        assert offset >= 0 and offset % self.POINTER_BYTES == 0, "root offset is not a pointer cell"
        assert offset + self.POINTER_BYTES <= capacity, "root offset is outside its alloca"
        covering = []
        for block, index, ins in self.rows:
            if not (self.called(ins, "pcc_gc_frame_enter")
                    or self.called(ins, "pcc_gc_frame_enter_lifo")):
                continue
            map_value, start_value = self.args(ins)
            reg_base, start = self.slot(start_value)
            if reg_base != base:
                continue
            frame_map = self.globals.get(self.symbol(map_value).lstrip("@"))
            assert (frame_map is not None and frame_map.is_constant
                    and frame_map.type.describe() == "i32"), "root needs a constant i32 owning map"
            try:
                count = int(frame_map.initializer)
            except (TypeError, ValueError):
                raise AssertionError("root needs a positive owning map") from None
            assert count > 0, "root needs a positive owning map"
            end = start + count * self.POINTER_BYTES
            assert (start >= 0 and start % self.POINTER_BYTES == 0
                    and end <= capacity), "owning map range is outside its alloca"
            if not start <= offset < end:
                continue
            lexical = self.called(ins, "pcc_gc_frame_enter_lifo")
            assert allow_lifo or not lexical, "operand must not use a lexical root"
            assert lexical or block.name == "entry", "owning registration must dominate every exit"
            initialization = [previous for owner, position, previous in self.rows
                              if owner.name == "entry"
                              and (block.name != "entry" or position < index)]
            expected = {(base, position) for position in range(start, end, self.POINTER_BYTES)}
            initialized = set()
            for previous in initialization:
                if previous.kind != "store":
                    continue
                write_base, write_start = self.slot(previous.data[3])
                if write_base != base:
                    continue
                write_type, value = previous.data[:2]
                write_end = write_start + write_type.slot_size
                for cell in expected:
                    cell_start = cell[1]
                    if write_start < cell_start + self.POINTER_BYTES and cell_start < write_end:
                        if (write_start == cell_start and write_type.is_ptr
                                and write_type.slot_size == self.POINTER_BYTES and value == "null"):
                            initialized.add(cell)
                        else:
                            initialized.discard(cell)
            assert expected <= initialized, "every mapped cell needs NULL initialization before entry"
            covering.append((base, start, count))
        assert len(covering) == 1, "root needs exactly one covering owning registration"
        return covering[0]

    def require_distinct(self, values):
        roots = tuple(self.slot(value) for value in values)
        assert len(set(roots)) == len(roots), "different operands alias the same physical root cell"
        for root in roots:
            self.require_owning(root)
        return roots

    def assert_frame_exits(self, expected_group_bases=()):
        """Prove every reachable return has retired every entered frame.

        Ordinary, borrowed and special frames share the regular ledger;
        lexical frames have an independent LIFO stack. Map ownership signs
        are deliberately irrelevant to lifetime. A historical entry bit
        identifies rooted returns without depending on leave instructions.
        """
        expected_group_bases = set(expected_group_bases)
        by_name = {block.name: block for block in self.blocks}
        pending = [(self.blocks[0].name, frozenset(), (), False)]
        visited, joins, entered_groups, returns = set(), {}, set(), {}
        while pending:
            name, incoming, incoming_lifo, ever_entered = pending.pop()
            state = (name, incoming, incoming_lifo, ever_entered)
            if state in visited:
                continue
            visited.add(state)
            lifetime = (incoming, incoming_lifo)
            assert joins.get(name, lifetime) == lifetime, "inconsistent live root frames at CFG join"
            joins[name] = lifetime
            active, lifo = set(incoming), list(incoming_lifo)
            block = by_name[name]
            for ins in block.instructions:
                enter = self.called(ins, "pcc_gc_frame_enter")
                leave = self.called(ins, "pcc_gc_frame_leave")
                enter_lifo = self.called(ins, "pcc_gc_frame_enter_lifo")
                leave_lifo = self.called(ins, "pcc_gc_frame_leave_lifo")
                if not (enter or leave or enter_lifo or leave_lifo):
                    continue
                identity = self.slot(self.args(ins)[1 if enter or enter_lifo else 0])
                base, offset = identity
                if base in expected_group_bases:
                    assert offset == 0, "operand group retirement must use its registered base"
                    assert not (enter_lifo or leave_lifo), "operand groups require regular frames"
                    if enter:
                        entered_groups.add(base)
                if enter:
                    assert identity not in active, "root frame entered twice on one path"
                    active.add(identity)
                elif leave:
                    assert identity in active, "frame leave has no active registration"
                    active.remove(identity)
                elif enter_lifo:
                    lifo.append(identity)
                else:
                    assert lifo and lifo[-1] == identity, "lexical frame leave violates LIFO order"
                    lifo.pop()
                ever_entered = ever_entered or enter or enter_lifo
            term = block.terminator
            if term.kind in ("ret", "ret_void"):
                assert not active and not lifo, "return reached with active root frames: " + name
                returns.setdefault(name, set()).add(ever_entered)
                continue
            successors = []
            if term.kind == "br":
                successors.append(term.data[0])
            elif term.kind == "br_cond":
                successors.extend(term.data[1:])
            elif term.kind == "switch":
                successors.append(term.data[2])
                successors.extend(target for _, target in term.data[3])
            for target in successors:
                pending.append((target, frozenset(active), tuple(lifo), ever_entered))
        assert entered_groups == expected_group_bases, "every operand group needs a reachable registration"
        assert returns, "root contract needs a reachable return"
        return returns
