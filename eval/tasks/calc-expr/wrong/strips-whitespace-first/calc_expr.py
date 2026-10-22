"""Calculator using precedence climbing over a whitespace-free copy of the source."""

import string


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


DIGITS = set(string.digits)
NAME_START = set(string.ascii_letters + "_")
BINARY = {"+": (1, "left"), "-": (1, "left"), "*": (2, "left"), "/": (2, "left"),
          "//": (2, "left"), "%": (2, "left"), "**": (4, "right")}
UNARY_PREC = 3
FUNCTIONS = {"abs": (1, 1), "round": (1, 1), "min": (1, None), "max": (1, None)}


def lex(text):
    tokens, i = [], 0
    while i < len(text):
        c = text[i]
        if c in DIGITS or (c == "." and i + 1 < len(text) and text[i + 1] in DIGITS):
            j = i
            while j < len(text) and text[j] in DIGITS:
                j += 1
            if j < len(text) and text[j] == ".":
                j += 1
                while j < len(text) and text[j] in DIGITS:
                    j += 1
            if j < len(text) and text[j] in "eE":
                k = j + 1
                if k < len(text) and text[k] in "+-":
                    k += 1
                if k < len(text) and text[k] in DIGITS:
                    j = k
                    while j < len(text) and text[j] in DIGITS:
                        j += 1
            tokens.append(("num", text[i:j], i))
            i = j
        elif c in NAME_START:
            j = i
            while j < len(text) and (text[j] in NAME_START or text[j] in DIGITS):
                j += 1
            tokens.append(("name", text[i:j], i))
            i = j
        elif text[i:i + 2] in ("**", "//"):
            tokens.append(("op", text[i:i + 2], i))
            i += 2
        elif c in "+-*/%(),":
            tokens.append(("op", c, i))
            i += 1
        else:
            tokens.append(("bad", c, i))
            i += 1
    tokens.append(("eof", "", len(text)))
    return tokens


class Parser:
    def __init__(self, text):
        self.tokens = lex(text)
        self.i = 0
        self.stack = []

    def peek(self):
        return self.tokens[self.i]

    def fail(self):
        kind, _, col = self.peek()
        if kind == "eof" and self.stack:
            raise UnclosedParenError(self.stack[-1])
        raise UnexpectedTokenError(col)

    def is_op(self, *ops):
        kind, text, _ = self.peek()
        return kind == "op" and text in ops

    def expression(self, min_prec=1):
        left = self.operand()
        while True:
            kind, text, col = self.peek()
            if kind != "op" or text not in BINARY:
                return left
            prec, assoc = BINARY[text]
            if prec < min_prec:
                return left
            self.i += 1
            if text == "**":
                right = self.operand() if not self.is_op("+", "-") else self.unary()
                right = self.continue_power(right)
            else:
                right = self.expression(prec + 1 if assoc == "left" else prec)
            left = ("bin", text, left, right, col)

    def continue_power(self, node):
        if self.is_op("**"):
            col = self.peek()[2]
            self.i += 1
            right = self.operand() if not self.is_op("+", "-") else self.unary()
            return ("bin", "**", node, self.continue_power(right), col)
        return node

    def unary(self):
        _, text, _ = self.peek()
        self.i += 1
        return ("unary", text, self.expression(UNARY_PREC))

    def operand(self):
        kind, text, col = self.peek()
        if self.is_op("+", "-"):
            return self.unary()
        if kind == "num":
            self.i += 1
            return ("num", float(text) if any(ch in text for ch in ".eE") else int(text))
        if kind == "name":
            self.i += 1
            if self.is_op("("):
                self.stack.append(self.peek()[2])
                self.i += 1
                args = []
                if not self.is_op(")"):
                    args.append(self.expression())
                    while self.is_op(","):
                        self.i += 1
                        args.append(self.expression())
                if not self.is_op(")"):
                    self.fail()
                self.i += 1
                self.stack.pop()
                return ("call", text, args, col)
            return ("var", text, col)
        if self.is_op("("):
            self.stack.append(col)
            self.i += 1
            node = self.expression()
            if not self.is_op(")"):
                self.fail()
            self.i += 1
            self.stack.pop()
            return node
        self.fail()


def compute(node, env):
    tag = node[0]
    if tag == "num":
        return node[1]
    if tag == "var":
        _, name, col = node
        if name not in env:
            raise UnknownNameError(col, name)
        return env[name]
    if tag == "unary":
        value = compute(node[2], env)
        return -value if node[1] == "-" else +value
    if tag == "call":
        _, name, args, col = node
        if name not in FUNCTIONS:
            raise UnknownNameError(col, name)
        lo, hi = FUNCTIONS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise ArityError(col, name, len(args))
        values = [compute(a, env) for a in args]
        if name == "min":
            return min(values)
        if name == "max":
            return max(values)
        return abs(values[0]) if name == "abs" else round(values[0])
    _, op, left, right, col = node
    a, b = compute(left, env), compute(right, env)
    if op in ("/", "//", "%") and b == 0:
        raise DivisionByZeroError(col)
    if op == "**":
        if a == 0 and b < 0:
            raise DivisionByZeroError(col)
        if a < 0 and isinstance(b, float) and not b.is_integer():
            raise DomainError(col)
        return a ** b
    return {"+": a + b, "-": a - b, "*": a * b}[op] if op in "+-*" else (
        a / b if op == "/" else a // b if op == "//" else a % b)


def evaluate(source, variables=None):
    text = "".join(ch for ch in source if ch not in " \t\n\r")
    parser = Parser(text)
    tree = parser.expression()
    if parser.peek()[0] != "eof":
        parser.fail()
    return compute(tree, variables or {})
