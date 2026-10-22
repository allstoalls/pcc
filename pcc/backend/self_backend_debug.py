"""Original source coordinates retained by the owned IR parser.

Metadata is data, not an instruction suffix or an inferred source line.  Keep
function scope and each retained block's instruction/terminator coordinates
separate from the semantic operand planes. Native debug writers consume this
sidecar only once they can bind it to their actual emitted instruction offsets.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from . import BackendUnavailable


@dataclass(frozen=True)
class DebugNode:
    kind: str
    fields: dict[str, str]


@dataclass(frozen=True)
class DebugSource:
    filename: str
    directory: str


@dataclass(frozen=True)
class DebugLocation:
    source: DebugSource
    line: int
    column: int
    discriminator: int
    scope: int
    identity: int


@dataclass(frozen=True)
class DebugBlock:
    name: str
    instructions: list[DebugLocation | None]
    terminator: DebugLocation | None


@dataclass
class FunctionDebugInfo:
    symbol: str
    name: str
    source: DebugSource
    line: int
    scope: int
    unit: int
    blocks: list[DebugBlock]


def debug_attachment_fragment(line: str, depth: int) -> tuple[str, int, int]:
    """Remove one actual top-level !dbg attachment, never quoted text.

    Leave unrelated metadata/attributes intact for their existing admission.
    The caller must retain and resolve the returned identity, not discard it.
    """
    quoted = False
    i = 0
    found = -1
    clean = line
    while i < len(line):
        char = line[i]
        if quoted:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                quoted = False
        elif char == ';':
            break
        elif char == '"':
            quoted = True
        elif char in '([{<':
            depth += 1
        elif char in ')]}>':
            depth -= 1
        elif depth == 0 and line.startswith('!dbg', i) and (i == 0 or line[i - 1] in ' ,\t'):
            match = re.match(r'!dbg\s+!([0-9]+)(?=\s|,|$)', line[i:])
            if match is None:
                raise BackendUnavailable('malformed source debug attachment')
            if found >= 0:
                raise BackendUnavailable('duplicate source debug attachment')
            found = int(match.group(1))
            end = i + match.end()
            start = i
            while start > 0 and line[start - 1] in ' \t':
                start -= 1
            if start > 0 and line[start - 1] == ',':
                start -= 1
            clean = line[:start] + line[end:]
            # Validate the rest too, without moving indexes in the original.
            i = end
            continue
        i += 1
    return clean, found, depth


def debug_attachment(line: str) -> tuple[str, int]:
    clean, identity, _depth = debug_attachment_fragment(line, 0)
    return clean, identity


def _reference(value: str, label: str) -> int:
    if not re.fullmatch(r'![0-9]+', value):
        raise BackendUnavailable('source debug ' + label + ' needs a metadata reference')
    return int(value[1:])


def _integer(value: str, label: str) -> int:
    if re.fullmatch(r'[0-9]+', value) is None:
        raise BackendUnavailable('source debug ' + label + ' needs a nonnegative integer')
    return int(value)


def _string(value: str, label: str) -> str:
    from .self_backend_parse import decode_llvm_c_string
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise BackendUnavailable('source debug ' + label + ' needs an original string')
    try:
        decoded = decode_llvm_c_string('c' + value).decode('utf-8')
        if '\0' in decoded:
            raise BackendUnavailable('source debug ' + label + ' contains NUL')
        return decoded
    except UnicodeError as error:
        raise BackendUnavailable('source debug ' + label + ' is not UTF-8') from error


class DebugMetadata:
    def __init__(self, nodes: dict[int, DebugNode]) -> None:
        self.nodes = nodes
        self.functions: dict[str, FunctionDebugInfo] = {}
        self.locations: dict[int, DebugLocation] = {}
        self.location_owners: dict[int, int] = {}
        self.sources: dict[int, DebugSource] = {}

    def node(self, identity: int, kind: str) -> DebugNode:
        node = self.nodes.get(identity)
        if node is None or node.kind != kind:
            raise BackendUnavailable('source debug !' + str(identity) + ' requires ' + kind)
        return node

    def source(self, identity: int) -> DebugSource:
        found = self.sources.get(identity)
        if found is not None:
            return found
        node = self.node(identity, 'DIFile')
        source = DebugSource(
            _string(node.fields.get('filename', ''), 'filename'),
            _string(node.fields.get('directory', '""'), 'directory'),
        )
        self.sources[identity] = source
        return source

    def function(self, identity: int, symbol: str) -> FunctionDebugInfo:
        node = self.node(identity, 'DISubprogram')
        file_id = _reference(node.fields.get('file', ''), 'function file')
        unit_id = _reference(node.fields.get('unit', ''), 'function unit')
        self.node(unit_id, 'DICompileUnit')
        function = FunctionDebugInfo(
            symbol, _string(node.fields.get('name', ''), 'function name'),
            self.source(file_id), _integer(node.fields.get('line', '0'), 'function line'),
            identity, unit_id, [],
        )
        self.functions[symbol] = function
        return function

    def location(self, identity: int, function: FunctionDebugInfo) -> DebugLocation | None:
        if identity < 0:
            return None
        found = self.locations.get(identity)
        if found is not None:
            if self.location_owners[identity] != function.scope:
                raise BackendUnavailable('source debug location belongs to another function')
            return found
        node = self.node(identity, 'DILocation')
        if 'inlinedAt' in node.fields and node.fields['inlinedAt'] != 'null':
            raise BackendUnavailable('owned source debug inline call chains are not admitted yet')
        scope = _reference(node.fields.get('scope', ''), 'location scope')
        current = scope
        source = None
        discriminator = 0
        discriminator_seen = False
        visited: set[int] = set()
        while True:
            if current in visited:
                raise BackendUnavailable('cyclic source debug scope')
            visited.add(current)
            parent = self.nodes.get(current)
            if parent is None:
                raise BackendUnavailable('missing source debug scope !' + str(current))
            if source is None and 'file' in parent.fields:
                source = self.source(_reference(parent.fields['file'], 'scope file'))
            if parent.kind == 'DISubprogram':
                if current != function.scope:
                    raise BackendUnavailable('source debug location belongs to another function')
                break
            if parent.kind not in ('DILexicalBlock', 'DILexicalBlockFile'):
                raise BackendUnavailable('unsupported source debug scope ' + parent.kind)
            if parent.kind == 'DILexicalBlockFile' and not discriminator_seen:
                discriminator = _integer(parent.fields.get('discriminator', '0'), 'discriminator')
                discriminator_seen = True
            current = _reference(parent.fields.get('scope', ''), 'parent scope')
        if source is None:
            raise BackendUnavailable('source debug location has no original file')
        location = DebugLocation(
            source, _integer(node.fields.get('line', '0'), 'line'),
            _integer(node.fields.get('column', '0'), 'column'), discriminator, scope, identity,
        )
        self.locations[identity] = location
        self.location_owners[identity] = function.scope
        return location

    def block(
        self, function: FunctionDebugInfo, name: str,
        instruction_ids: list[int], terminator_ids: list[int],
    ) -> None:
        terminator_id = -1
        for identity in terminator_ids:
            if identity >= 0:
                if terminator_id >= 0 and identity != terminator_id:
                    raise BackendUnavailable('conflicting multi-line terminator source locations')
                terminator_id = identity
        function.blocks.append(DebugBlock(
            name, [self.location(identity, function) for identity in instruction_ids],
            self.location(terminator_id, function),
        ))


def parse_debug_metadata(ir_text: str) -> DebugMetadata | None:
    if '!dbg' not in ir_text:
        return None
    # Compiler source may itself contain a literal '!dbg'. No debug nodes
    # means no per-instruction sidecar or attachment scan on the ordinary path.
    if re.search(r'(?m)^\s*![0-9]+\s*=\s*(?:distinct\s+)?!DI\w+\(', ir_text) is None:
        return None
    from .self_backend_parse import split_top_level
    nodes: dict[int, DebugNode] = {}
    for line in ir_text.splitlines():
        match = re.fullmatch(r'\s*!(\d+)\s*=\s*(?:distinct\s+)?!(DI\w+)\((.*)\)\s*', line)
        if match is None:
            continue
        identity = int(match.group(1))
        if identity in nodes:
            raise BackendUnavailable('duplicate source debug metadata !' + str(identity))
        fields: dict[str, str] = {}
        if match.group(2) not in ('DIFile', 'DICompileUnit', 'DISubprogram', 'DILocation', 'DILexicalBlock', 'DILexicalBlockFile'):
            nodes[identity] = DebugNode(match.group(2), {'$raw': match.group(3)})
            continue
        for field in split_top_level(match.group(3)):
            name, separator, value = field.partition(':')
            if not separator or name.strip() in fields:
                raise BackendUnavailable('malformed source debug metadata field')
            fields[name.strip()] = value.strip()
        nodes[identity] = DebugNode(match.group(2), fields)
    return DebugMetadata(nodes)
