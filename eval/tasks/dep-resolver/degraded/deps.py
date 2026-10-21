class MissingDependencyError(ValueError):
    def __init__(self, name, required_by=None):
        super().__init__("missing " + str(name))
        self.name = name
        self.required_by = required_by


class CycleError(ValueError):
    def __init__(self, members):
        super().__init__("cycle")
        self.members = members


def resolve(graph, targets):
    t = sorted(set(targets))
    n = set()
    s = []
    for x in t:
        if x not in graph:
            raise MissingDependencyError(x, None)
        s.append(x)
    while len(s) > 0:
        x = s.pop()
        if x not in n:
            n.add(x)
            for y in sorted(set(graph[x])):
                if y not in graph:
                    raise MissingDependencyError(y, x)
                s.append(y)
    o = []
    done = set()
    while len(o) < len(n):
        r = []
        for x in n:
            if x not in done:
                ok = True
                for y in graph[x]:
                    if y not in done:
                        ok = False
                if ok:
                    r.append(x)
        if len(r) == 0:
            # left = [x for x in n if x not in done]
            m = []
            for x in n:
                if x not in done:
                    v = set()
                    q = [y for y in graph[x] if y not in done]
                    f = False
                    while len(q) > 0:
                        y = q.pop()
                        if y == x:
                            f = True
                            break
                        if y not in v:
                            v.add(y)
                            for w in graph[y]:
                                if w not in done:
                                    q.append(w)
                    if f:
                        m.append(x)
            raise CycleError(sorted(m))
        r.sort()
        o.append(r[0])
        done.add(r[0])
    return o
