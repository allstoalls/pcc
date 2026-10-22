"""Function-local pointer identity checks for emitted ownership IR."""
import re


def function_bodies(text):
    """Yield definitions, excluding declarations and keeping SSA scopes apart."""
    for match in re.finditer(r'^define [^\n]*@([\w.$]+)\([^\n]*\).*?^}', text, re.M | re.S):
        yield match[1], match[0]


def pointer_bitcast_aliases(body):
    """Recognize only ptr-to-ptr bitcasts in one function body.

    GEPs (including zero offsets) and address-space casts keep their own
    identities. Following either requires a separate proof.
    """
    assert len(re.findall(r'^define\b', body, re.M)) <= 1, 'multiple SSA scopes'
    return dict(re.findall(
        r'^\s*(%[\w.$]+) = bitcast ptr (%[\w.$]+) to ptr\s*$', body, re.M,
    ))


def canonical_pointer(value, aliases):
    visited = set()
    while value in aliases:
        assert value not in visited, ('cyclic pointer alias', value)
        visited.add(value)
        value = aliases[value]
    return value
