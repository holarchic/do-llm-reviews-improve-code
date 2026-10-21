def merge(spans):
    for start, end in spans:
        if start > end:
            raise ValueError(f"span starts after it ends: {(start, end)}")
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
