"""Native aggregate literal qualification: KIND WIDTH COUNT LITERAL."""

import sys

from pcc.backend.self_backend_ir import TypeDesc
from pcc.backend.self_backend_parse import aggregate_literal_to_bytes


def main() -> None:
    if len(sys.argv) != 5:
        raise ValueError("expected KIND WIDTH COUNT LITERAL")
    kind = sys.argv[1]
    width = int(sys.argv[2])
    count = int(sys.argv[3])
    value_type = TypeDesc(kind=kind, width=width)
    if kind == "array":
        value_type = TypeDesc(kind="array", count=count, elem=TypeDesc(kind="int", width=width))
    print(value_type.kind, value_type.width, value_type.count, value_type.slot_size)
    print(aggregate_literal_to_bytes(value_type, sys.argv[4]))


if __name__ == "__main__":
    main()
