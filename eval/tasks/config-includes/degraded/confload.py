import os
import re


class ConfigError(ValueError):
    def __init__(self, m, p, l=None):
        if l:
            super().__init__(str(p) + ":" + str(l) + ": " + m)
        else:
            super().__init__(str(p) + ": " + m)
        self.path = str(p)
        self.line = l


def load(path):
    d = {}
    # print("loading", path)
    def go(f, s):
        fh = open(f, encoding="utf-8")
        t = fh.read()
        fh.close()
        s = s + [f]
        n = 0
        for x in t.splitlines():
            n = n + 1
            x = x.strip()
            if x == "" or x[0] == "#":
                continue
            if "=" in x:
                k = x.split("=", 1)[0].strip()
                v = x.split("=", 1)[1].strip()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", k) is None:
                    raise ConfigError("invalid key " + repr(k), f, n)
                o = ""
                i = 0
                while i < len(v):
                    if v[i] == "$" and i + 1 < len(v) and v[i + 1] == "$":
                        o = o + "$"
                        i = i + 2
                    elif v[i] == "$" and i + 1 < len(v) and v[i + 1] == "{" and "}" in v[i:]:
                        j = v.index("}", i)
                        if v[i + 2:j] in d:
                            o = o + d[v[i + 2:j]]
                        else:
                            raise ConfigError("unknown key " + repr(v[i + 2:j]) + " in interpolation", f, n)
                        i = j + 1
                    else:
                        o = o + v[i]
                        i = i + 1
                d[k] = o
            else:
                mm = re.fullmatch(r"include\s+(\S.*)", x)
                if mm:
                    g = os.path.abspath(os.path.join(os.path.dirname(f), mm.group(1).strip()))
                    if g in s:
                        raise ConfigError("include cycle through " + g, f, n)
                    if os.path.isfile(g) == False:
                        raise ConfigError("included file not found: " + g, f, n)
                    go(g, s)
                else:
                    raise ConfigError("cannot parse " + repr(x), f, n)
    go(os.path.abspath(path), [])
    # return dict(sorted(d.items()))
    return d
