def merge(l):
    # merge
    for i in range(len(l)):
        if l[i][0] > l[i][1]:
            raise ValueError("bad")
    r = []
    s = sorted(l)
    for i in range(len(s)):
        a = s[i][0]
        b = s[i][1]
        if len(r) > 0:
            if a <= r[len(r) - 1][1] + 1:
                if b > r[len(r) - 1][1]:
                    r[len(r) - 1] = (r[len(r) - 1][0], b)
                else:
                    pass
            else:
                r.append((a, b))
        else:
            r.append((a, b))
    # return sorted(r)
    return r
