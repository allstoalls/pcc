"""pcc.frontends.c.parse.c_parse_driver — P6C.5 α1 step 3: native LR driver.

Drives the frozen LR tables in ``pcc.frontends.c.parse.c_parsetab`` against a
token stream produced by the existing ``CLexer`` (α2 replaces that
tokenizer). Calls grammar actions via ``pcc.frontends.c.parse.c_parser_actions``.

**No PLY runtime import.** The driver, the action layer, and the
table module are all PLY-free at the source level. (The overall pcc
package still loads PLY transitively via ``pcc/__init__.py`` — that's
a separate surface clean-up, see α3.)

Architecture:

    source text ──► CLexer (tokens) ──► CParseDriver ──► c_ast.FileAST
                                             │
                                             ├── ACTION/GOTO (c_parsetab)
                                             └── actions (c_parser_actions)

The driver itself is ~250 LoC of standard shift/reduce state machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pcc.frontends.c.parse import c_parsetab as _tab
from pcc.frontends.c.parse.c_parser_actions import CParserActions
from pcc.frontends.c.parse.plyparser import ParseError


@dataclass
class _Symbol:
    """A symbol on the parser value stack. Matches the minimal surface
    area expected by action code reading ``p.slice[i].type``."""

    type: str
    value: Any
    lineno: int = 0


class _PSlot:
    """The ``p`` object passed to each grammar action.

    API mirrors PLY's minimal surface:
      - ``p[0]``         : set the LHS synthesized value
      - ``p[i]`` (i > 0) : read the i-th RHS value
      - ``p.lineno(i)``  : line number of the i-th RHS symbol
      - ``p.slice``      : list of ``_Symbol``; actions that need it
                           look at ``.type`` (grammar symbol name)
    """

    __slots__ = ("_symbols", "_values")

    def __init__(self, symbols: list[_Symbol]) -> None:
        # symbols[0] is a placeholder for the LHS; the action writes
        # its synthesized value into it via ``p[0] = ...``. The driver
        # reads it back after the call.
        self._symbols = symbols
        # Parallel values list so ``p[i] = ...`` / ``p[i]`` read are
        # O(1). Kept in sync with _symbols[i].value.
        self._values = [s.value for s in symbols]

    def __len__(self) -> int:
        # PLY semantics: ``len(p)`` returns RHS length + 1 (for the
        # LHS slot at index 0). Some grammar actions use ``len(p)``
        # to pick the right alternative in a combined rule.
        return len(self._values)

    def __getitem__(self, i: int) -> Any:
        return self._values[i]

    def __setitem__(self, i: int, value: Any) -> None:
        self._values[i] = value
        if i < len(self._symbols):
            self._symbols[i].value = value

    def lineno(self, i: int) -> int:
        return self._symbols[i].lineno

    @property
    def slice(self) -> list[_Symbol]:
        return self._symbols


class CParseDriver:
    """LR shift/reduce driver. Stateless between parses (state lives
    in the per-parse stacks) — a single instance is reusable."""

    def __init__(self) -> None:
        # Lazily built action-name → callable table. Built from
        # CParserActions' MRO on first parse.
        self._action_cache: dict[str, Any] | None = None

    def _build_action_table(self, actions: CParserActions) -> dict:
        """Collect all ``p_*`` methods from the actions instance.
        Returns a dict keyed by action name (matching the third
        element of ``c_parsetab.PRODUCTIONS``)."""
        table: dict = {}
        cls = type(actions)
        for base in cls.__mro__:
            for name, fn in base.__dict__.items():
                if name.startswith("p_") and callable(fn):
                    table.setdefault(name, fn)
        return table

    def parse(self, src: str, filename: str = "<input>") -> Any:
        """Parse ``src`` to a c_ast tree. Raises ``ParseError`` on
        syntax errors. Uses the native ``c_lex.CLexer`` — no PLY
        lex runtime on the default path."""
        from pcc.frontends.c.parse.c_lex import CLexer  # native, PLY-free

        actions = CParserActions(filename=filename)
        if self._action_cache is None:
            self._action_cache = self._build_action_table(actions)
        action_table = self._action_cache

        # Build the lexer with callbacks that update the actions state.
        def _on_lbrace() -> None:
            actions._scope_stack.append({})

        def _on_rbrace() -> None:
            actions._scope_stack.pop()

        def _type_lookup(name: str) -> bool:
            for scope in reversed(actions._scope_stack):
                if name in scope:
                    return scope[name]
            return False

        def _lex_error(msg, line, column) -> None:
            raise ParseError(f"{filename}:{line}:{column}: {msg}")

        lexer = CLexer(
            error_func=_lex_error,
            on_lbrace_func=_on_lbrace,
            on_rbrace_func=_on_rbrace,
            type_lookup_func=_type_lookup,
        )
        lexer.build(optimize=False)
        lexer.input(src)

        # Run the shift/reduce state machine.
        return self._run(lexer, actions, action_table)

    def _run(self, lexer: Any, actions: CParserActions, action_table: dict) -> Any:
        """Core shift/reduce loop."""
        state_stack: list[int] = [0]
        sym_stack: list[_Symbol] = []

        lookahead: _Symbol | None = None

        while True:
            if lookahead is None:
                tok = lexer.token()
                if tok is None:
                    lookahead = _Symbol(type="$end", value=None, lineno=0)
                else:
                    lookahead = _Symbol(
                        type=tok.type, value=tok.value,
                        lineno=getattr(tok, "lineno", 0),
                    )
                actions._last_yielded_token = lookahead

            state = state_stack[-1]
            row = _tab.ACTION.get(state, {})
            act = row.get(lookahead.type)
            if act is None:
                # Try to recover via PLY's "error" token — minimal: raise.
                self._syntax_error(lookahead, state, actions)
                return None

            if act == 0:
                # Augmented start accept — single-value stack.
                return sym_stack[-1].value if sym_stack else None

            if act > 0:
                # Shift: push lookahead, go to new state.
                sym_stack.append(lookahead)
                state_stack.append(act)
                lookahead = None
                continue

            # Reduce by production -act
            prod_idx = -act
            lhs, rlen, action_name, _str = _tab.PRODUCTIONS[prod_idx]

            # Collect RHS symbols (rlen items from top of stack).
            if rlen > 0:
                rhs_syms = sym_stack[-rlen:]
                del sym_stack[-rlen:]
                del state_stack[-rlen:]
            else:
                rhs_syms = []

            # Build the p slot: position 0 = LHS placeholder, 1..rlen = RHS.
            lhs_sym = _Symbol(type=lhs, value=None,
                              lineno=rhs_syms[0].lineno if rhs_syms else 0)
            p_slot = _PSlot([lhs_sym] + rhs_syms)

            # Dispatch action.
            if action_name == "_accept":
                # Augmented: just pop the single RHS into LHS
                lhs_sym.value = rhs_syms[0].value if rhs_syms else None
            elif action_name == "_opt_rule":
                # Synthetic ``x_opt -> x | empty`` — identity: p[0] = p[1]
                lhs_sym.value = rhs_syms[0].value if rhs_syms else None
            else:
                fn = action_table.get(action_name)
                if fn is None:
                    raise ParseError(
                        f"no action registered for {action_name!r} "
                        f"(production {prod_idx}: {_str})"
                    )
                fn(actions, p_slot)
                # p_slot[0] may have been set by the action.
                lhs_sym.value = p_slot[0]

            # Push the reduced nonterminal and look up GOTO.
            sym_stack.append(lhs_sym)
            goto_row = _tab.GOTO.get(state_stack[-1], {})
            next_state = goto_row.get(lhs)
            if next_state is None:
                raise ParseError(
                    f"no GOTO[{state_stack[-1]}][{lhs!r}] after reducing "
                    f"production {prod_idx}"
                )
            state_stack.append(next_state)

    def _syntax_error(
        self, tok: _Symbol, state: int, actions: CParserActions,
    ) -> None:
        """Mirror PLY's p_error call-site — invoke actions' error
        reporter and raise ``ParseError``."""
        coord = actions._coord(tok.lineno)
        msg = f"before {tok.type!r} ({tok.value!r})"
        raise ParseError(f"{coord}: {msg}")


def parse(src: str, filename: str = "<input>") -> Any:
    """Convenience entry: parse one source string via a fresh driver."""
    return CParseDriver().parse(src, filename=filename)
