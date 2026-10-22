"""Safe arithmetic expression evaluator: tokenizer, recursive-descent parser, tree evaluator."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from string import ascii_letters

Number = int | float


class CalcError(Exception):
    def __init__(self, column: int, message: str = ""):
        super().__init__(message or f"column {column}")
        self.column = column


class UnexpectedTokenError(CalcError):
    pass


class UnclosedParenError(CalcError):
    pass


class UnknownNameError(CalcError):
    def __init__(self, column: int, name: str):
        super().__init__(column, f"unknown name {name!r} at column {column}")
        self.name = name


class ArityError(CalcError):
    def __init__(self, column: int, name: str, given: int):
        super().__init__(column, f"{name}() given {given} arguments at column {column}")
        self.name = name
        self.given = given


class DivisionByZeroError(CalcError, ZeroDivisionError):
    pass


class DomainError(CalcError):
    pass


_WHITESPACE = frozenset(" \t\n\r")
_DIGITS = frozenset("0123456789")
_NAME_START = frozenset(ascii_letters + "_")
_NAME_CHARS = _NAME_START | _DIGITS
_TWO_CHAR_OPS = ("**", "//")
_ONE_CHAR_TOKENS = frozenset("+-*/%(),")


@dataclass(frozen=True)
class _Token:
    kind: str  # "num", "name", "op" (operators and punctuation), "invalid", "eof"
    text: str
    column: int


def _scan_number(source: str, start: int) -> int:
    """End index of the longest number literal starting at `start`."""
    end, n = start, len(source)
    while end < n and source[end] in _DIGITS:
        end += 1
    if end < n and source[end] == ".":
        end += 1
        while end < n and source[end] in _DIGITS:
            end += 1
    if end < n and source[end] in "eE":
        probe = end + 1
        if probe < n and source[probe] in "+-":
            probe += 1
        if probe < n and source[probe] in _DIGITS:
            end = probe
            while end < n and source[end] in _DIGITS:
                end += 1
    return end


def _tokenize(source: str) -> list[_Token]:
    # Invalid characters become tokens rather than raising here: the parser must
    # report the leftmost unacceptable token, which may come before them.
    tokens, i, n = [], 0, len(source)
    while i < n:
        char = source[i]
        if char in _WHITESPACE:
            i += 1
        elif char in _DIGITS or (char == "." and i + 1 < n and source[i + 1] in _DIGITS):
            end = _scan_number(source, i)
            tokens.append(_Token("num", source[i:end], i))
            i = end
        elif char in _NAME_START:
            end = i + 1
            while end < n and source[end] in _NAME_CHARS:
                end += 1
            tokens.append(_Token("name", source[i:end], i))
            i = end
        elif source.startswith(_TWO_CHAR_OPS, i):
            tokens.append(_Token("op", source[i:i + 2], i))
            i += 2
        elif char in _ONE_CHAR_TOKENS:
            tokens.append(_Token("op", char, i))
            i += 1
        else:
            tokens.append(_Token("invalid", char, i))
            i += 1
    tokens.append(_Token("eof", "", n))
    return tokens


@dataclass(frozen=True)
class _Num:
    value: Number


@dataclass(frozen=True)
class _Var:
    name: str
    column: int


@dataclass(frozen=True)
class _Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class _Binary:
    op: str
    left: object
    right: object
    column: int


@dataclass(frozen=True)
class _Call:
    name: str
    args: tuple
    column: int


class _Parser:
    def __init__(self, source: str):
        self._tokens = _tokenize(source)
        self._pos = 0
        self._open_parens: list[int] = []

    def parse(self):
        node = self._expression()
        if self._peek().kind != "eof":
            self._fail()
        return node

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _at(self, *texts: str) -> bool:
        token = self._peek()
        return token.kind == "op" and token.text in texts

    def _advance(self) -> _Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _fail(self):
        token = self._peek()
        if token.kind == "eof" and self._open_parens:
            raise UnclosedParenError(self._open_parens[-1])
        raise UnexpectedTokenError(token.column)

    def _open(self) -> None:
        self._open_parens.append(self._advance().column)

    def _close(self) -> None:
        if not self._at(")"):
            self._fail()
        self._advance()
        self._open_parens.pop()

    def _expression(self):
        node = self._term()
        while self._at("+", "-"):
            op = self._advance()
            node = _Binary(op.text, node, self._term(), op.column)
        return node

    def _term(self):
        node = self._unary()
        while self._at("*", "/", "//", "%"):
            op = self._advance()
            node = _Binary(op.text, node, self._unary(), op.column)
        return node

    def _unary(self):
        if self._at("+", "-"):
            op = self._advance()
            return _Unary(op.text, self._unary())
        return self._power()

    def _power(self):
        base = self._primary()
        if self._at("**"):
            op = self._advance()
            return _Binary("**", base, self._unary(), op.column)
        return base

    def _primary(self):
        token = self._peek()
        if token.kind == "num":
            self._advance()
            is_float = any(c in token.text for c in ".eE")
            return _Num(float(token.text) if is_float else int(token.text))
        if token.kind == "name":
            self._advance()
            if self._at("("):
                return self._call(token)
            return _Var(token.text, token.column)
        if self._at("("):
            self._open()
            node = self._expression()
            self._close()
            return node
        self._fail()

    def _call(self, name: _Token):
        self._open()
        args = []
        if not self._at(")"):
            args.append(self._expression())
            while self._at(","):
                self._advance()
                args.append(self._expression())
        self._close()
        return _Call(name.text, tuple(args), name.column)


@dataclass(frozen=True)
class _Function:
    apply: Callable[..., Number]
    min_args: int
    max_args: int | None


_FUNCTIONS = {
    "abs": _Function(abs, 1, 1),
    "round": _Function(round, 1, 1),
    "min": _Function(lambda *args: min(args), 1, None),
    "max": _Function(lambda *args: max(args), 1, None),
}


def _apply_binary(op: str, left: Number, right: Number, column: int) -> Number:
    if op in ("/", "//", "%") and right == 0:
        raise DivisionByZeroError(column)
    if op == "**":
        if left == 0 and right < 0:
            raise DivisionByZeroError(column)
        if left < 0 and isinstance(right, float) and not right.is_integer():
            raise DomainError(column)
    match op:
        case "+":
            return left + right
        case "-":
            return left - right
        case "*":
            return left * right
        case "/":
            return left / right
        case "//":
            return left // right
        case "%":
            return left % right
        case _:
            try:
                return left ** right
            except OverflowError:
                raise DomainError(column) from None


def _evaluate(node, variables: Mapping[str, Number]) -> Number:
    match node:
        case _Num(value):
            return value
        case _Var(name, column):
            if name not in variables:
                raise UnknownNameError(column, name)
            return variables[name]
        case _Unary(op, operand):
            value = _evaluate(operand, variables)
            return -value if op == "-" else +value
        case _Binary(op, left, right, column):
            left_value = _evaluate(left, variables)
            right_value = _evaluate(right, variables)
            return _apply_binary(op, left_value, right_value, column)
        case _Call(name, args, column):
            function = _FUNCTIONS.get(name)
            if function is None:
                raise UnknownNameError(column, name)
            if len(args) < function.min_args or (function.max_args is not None and len(args) > function.max_args):
                raise ArityError(column, name, len(args))
            return function.apply(*(_evaluate(arg, variables) for arg in args))
    raise AssertionError(f"unknown node {node!r}")


def evaluate(source: str, variables: Mapping[str, Number] | None = None) -> Number:
    tree = _Parser(source).parse()
    return _evaluate(tree, {} if variables is None else variables)
