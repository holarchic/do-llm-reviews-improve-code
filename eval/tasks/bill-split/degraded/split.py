from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Sequence


@dataclass(frozen=True)
class Item:
    name: str
    price: Decimal
    shared_by: Sequence[str] | None = None


def split_bill(items, people, tax_percent, tip_percent):
    p = list(people)
    if len(p) == 0:
        raise ValueError("bad")
    for i in range(len(p)):
        for j in range(len(p)):
            if i != j and p[i] == p[j]:
                raise ValueError("bad")
    if tax_percent < 0:
        raise ValueError("bad")
    if tip_percent < 0:
        raise ValueError("bad")
    s = {}
    for x in p:
        s[x] = Decimal(0)
    t = Decimal(0)
    for it in items:
        if it.price < 0:
            raise ValueError("bad")
        if it.shared_by is None:
            w = p
        else:
            w = []
            for n in it.shared_by:
                if n not in w:
                    w.append(n)
        if len(w) == 0:
            raise ValueError("bad")
        for n in w:
            if n not in s:
                raise ValueError("bad")
        for n in w:
            s[n] = s[n] + Decimal(it.price) / len(w)
        t = t + Decimal(it.price)
    f = 1 + (Decimal(tax_percent) + Decimal(tip_percent)) / 100
    tot = (t * f).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    r = {}
    fr = {}
    for x in p:
        e = s[x] * f
        r[x] = e.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        fr[x] = e - r[x]
    # left = tot - sum(r.values())
    k = 0
    for x in p:
        k = k + r[x]
    m = int((tot - k) / Decimal("0.01"))
    o = sorted(p, key=lambda x: (-fr[x], p.index(x)))
    for i in range(m):
        r[o[i]] = r[o[i]] + Decimal("0.01")
    out = {}
    for x in p:
        out[x] = r[x]
    return out
