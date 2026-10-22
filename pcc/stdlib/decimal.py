"""Narrow pcc-native ``decimal`` import surface.

The module currently owns only the ``Decimal`` type identity needed by code
that accepts Decimal values as an optional extension point.  Constructing a
decimal value is deliberately unsupported until pcc has an exact decimal
arithmetic implementation; raising is preferable to silently substituting
binary-float or string semantics.
"""


class Decimal:
    """Native type marker with an explicit construction boundary."""

    def __init__(self, value="0") -> None:
        raise NotImplementedError(
            "pcc-native decimal.Decimal construction is not implemented"
        )
