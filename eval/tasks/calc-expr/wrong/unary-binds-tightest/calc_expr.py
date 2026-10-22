"""Safe calculator: tokenize, parse to a tree, then evaluate."""

from dataclasses import dataclass


class CalcError(Exception):
    def __init__(self, column):
        super().__init__(f"column {column}")
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


@dataclass
class Tok:
    kind: str
    text: str
    col: int
    end: int


def tokenize(src):
    out = []
    i = 0
    while i < len(src):
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        start = i
        if ch.isdigit() or ch == ".":
            while i < len(src) and (src[i].isdigit() or src[i] == "."):
                i += 1
            if i < len(src) and src[i] in "eE":
                j = i + 1
                if j < len(src) and src[j] in "+-":
                    j += 1
                if j < len(src) and src[j].isdigit():
                    i = j
                    while i < len(src) and src[i].isdigit():
                        i += 1
            text = src[start:i]
            if text.count(".") > 1 or text == ".":
                raise UnexpectedTokenError(start)
            out.append(Tok("num", text, start, i))
        elif ch.isalpha() or ch == "_":
            while i < len(src) and (src[i].isalnum() or src[i] == "_"):
                i += 1
            out.append(Tok("name", src[start:i], start, i))
        elif src.startswith("**", i) or src.startswith("//", i):
            out.append(Tok("op", src[i:i + 2], start, i + 2))
            i += 2
        elif ch in "+-*/%(),":
            out.append(Tok("op", ch, start, i + 1))
            i += 1
        else:
            raise UnexpectedTokenError(i)
    end = out[-1].end if out else 0
    out.append(Tok("eof", "", end, end))
    return out


class Parser:
    def __init__(self, src):
        self.toks = tokenize(src)
        self.pos = 0
        self.parens = []

    def peek(self):
        return self.toks[self.pos]

    def take(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def at(self, *ops):
        t = self.peek()
        return t.kind == "op" and t.text in ops

    def error(self):
        t = self.peek()
        if t.kind == "eof" and self.parens:
            raise UnclosedParenError(self.parens[-1])
        raise UnexpectedTokenError(t.col)

    def parse(self):
        node = self.expr()
        if self.peek().kind != "eof":
            self.error()
        return node

    def expr(self):
        node = self.term()
        while self.at("+", "-"):
            t = self.take()
            node = ("bin", t.text, node, self.term(), t.col)
        return node

    def term(self):
        node = self.power()
        while self.at("*", "/", "//", "%"):
            t = self.take()
            node = ("bin", t.text, node, self.power(), t.col)
        return node

    def power(self):
        base = self.unary()
        if self.at("**"):
            t = self.take()
            return ("bin", "**", base, self.power(), t.col)
        return base

    def unary(self):
        if self.at("+", "-"):
            t = self.take()
            return ("neg" if t.text == "-" else "pos", self.unary())
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == "num":
            self.take()
            return ("num", float(t.text) if any(c in t.text for c in ".eE") else int(t.text))
        if t.kind == "name":
            self.take()
            if self.at("("):
                self.parens.append(self.take().col)
                args = []
                if not self.at(")"):
                    args.append(self.expr())
                    while self.at(","):
                        self.take()
                        args.append(self.expr())
                if not self.at(")"):
                    self.error()
                self.take()
                self.parens.pop()
                return ("call", t.text, args, t.col)
            return ("var", t.text, t.col)
        if self.at("("):
            self.parens.append(self.take().col)
            node = self.expr()
            if not self.at(")"):
                self.error()
            self.take()
            self.parens.pop()
            return node
        self.error()


ARITY = {"abs": (1, 1), "round": (1, 1), "min": (1, None), "max": (1, None)}


def run(node, env):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        if node[1] not in env:
            raise UnknownNameError(node[2], node[1])
        return env[node[1]]
    if kind == "neg":
        return -run(node[1], env)
    if kind == "pos":
        return +run(node[1], env)
    if kind == "call":
        _, name, args, col = node
        if name not in ARITY:
            raise UnknownNameError(col, name)
        lo, hi = ARITY[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise ArityError(col, name, len(args))
        values = [run(a, env) for a in args]
        return {"abs": abs, "round": round, "min": min, "max": max}[name](*values)
    _, op, left, right, col = node
    a = run(left, env)
    b = run(right, env)
    if op in ("/", "//", "%") and b == 0:
        raise DivisionByZeroError(col)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b
    if op == "//":
        return a // b
    if op == "%":
        return a % b
    return a ** b


def evaluate(source, variables=None):
    return run(Parser(source).parse(), variables or {})
