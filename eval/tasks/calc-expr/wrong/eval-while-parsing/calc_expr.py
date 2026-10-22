"""Arithmetic evaluator that computes values during a single recursive-descent pass."""

import re

TOKEN_RE = re.compile(r"\s*(?:(\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|\d+)|([A-Za-z_]\w*)|(\*\*|//|[-+*/%(),]))")


class CalcError(Exception):
    def __init__(self, column, msg=""):
        super().__init__(msg or f"error at {column}")
        self.column = column


class UnexpectedTokenError(CalcError):
    pass


class UnclosedParenError(CalcError):
    pass


class UnknownNameError(CalcError):
    def __init__(self, column, name):
        super().__init__(column)
        self.name = name


class ArityError(CalcError):
    def __init__(self, column, name, given):
        super().__init__(column)
        self.name = name
        self.given = given


class DivisionByZeroError(CalcError, ZeroDivisionError):
    pass


class DomainError(CalcError):
    pass


def tokenize(source):
    tokens = []
    pos = 0
    while True:
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if pos >= len(source):
            break
        m = TOKEN_RE.match(source, pos)
        if not m:
            raise UnexpectedTokenError(pos)
        start = m.start(m.lastindex)
        if m.group(1):
            text = m.group(1)
            value = float(text) if any(c in text for c in ".eE") else int(text)
            tokens.append(("num", value, start))
        elif m.group(2):
            tokens.append(("name", m.group(2), start))
        else:
            tokens.append(("op", m.group(3), start))
        pos = m.end()
    tokens.append(("eof", None, len(source)))
    return tokens


FUNCS = {"abs": (abs, 1, 1), "round": (round, 1, 1), "min": (min, 1, None), "max": (max, 1, None)}


class Parser:
    def __init__(self, source, variables):
        self.tokens = tokenize(source)
        self.i = 0
        self.vars = variables or {}

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def is_op(self, *ops):
        kind, value, _ = self.peek()
        return kind == "op" and value in ops

    def parse(self):
        value = self.expr()
        if self.peek()[0] != "eof":
            raise UnexpectedTokenError(self.peek()[2])
        return value

    def expr(self):
        value = self.term()
        while self.is_op("+", "-"):
            op = self.next()[1]
            right = self.term()
            value = value + right if op == "+" else value - right
        return value

    def term(self):
        value = self.unary()
        while self.is_op("*", "/", "//", "%"):
            _, op, col = self.next()
            right = self.unary()
            if op != "*" and right == 0:
                raise DivisionByZeroError(col)
            if op == "*":
                value = value * right
            elif op == "/":
                value = value / right
            elif op == "//":
                value = value // right
            else:
                value = value % right
        return value

    def unary(self):
        if self.is_op("-"):
            self.next()
            return -self.unary()
        if self.is_op("+"):
            self.next()
            return self.unary()
        return self.power()

    def power(self):
        base = self.primary()
        if self.is_op("**"):
            _, _, col = self.next()
            exp = self.unary()
            if base == 0 and exp < 0:
                raise DivisionByZeroError(col)
            if base < 0 and isinstance(exp, float) and not exp.is_integer():
                raise DomainError(col)
            return base ** exp
        return base

    def primary(self):
        kind, value, col = self.next()
        if kind == "num":
            return value
        if kind == "name":
            if self.is_op("("):
                return self.call(value, col)
            if value not in self.vars:
                raise UnknownNameError(col, value)
            return self.vars[value]
        if kind == "op" and value == "(":
            inner = self.expr()
            if not self.is_op(")"):
                if self.peek()[0] == "eof":
                    raise UnclosedParenError(col)
                raise UnexpectedTokenError(self.peek()[2])
            self.next()
            return inner
        raise UnexpectedTokenError(col)

    def call(self, name, col):
        _, _, paren_col = self.next()
        args = []
        if not self.is_op(")"):
            args.append(self.expr())
            while self.is_op(","):
                self.next()
                args.append(self.expr())
        if not self.is_op(")"):
            if self.peek()[0] == "eof":
                raise UnclosedParenError(paren_col)
            raise UnexpectedTokenError(self.peek()[2])
        self.next()
        if name not in FUNCS:
            raise UnknownNameError(col, name)
        fn, lo, hi = FUNCS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise ArityError(col, name, len(args))
        if name in ("min", "max"):
            return fn(args)
        return fn(*args)


def evaluate(source, variables=None):
    return Parser(source, variables).parse()
