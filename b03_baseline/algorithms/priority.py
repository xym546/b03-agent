"""Rank hypothetical subsets at a mandatory bottleneck without changing traffic."""
from itertools import combinations


def recommend_priority_groups(groups, capacity_bps, expired, exact_limit=10):
    """Return a single-link estimate assuming deferred groups create no load."""
    ordered = sorted(groups, key=lambda g: (-max(g["priorities"]), g["estimatedBps"], g["flowId"]))
    selected = []
    used = 0
    for group in ordered:
        if used + group["estimatedBps"] <= capacity_bps:
            selected.append(group)
            used += group["estimatedBps"]

    def key(items):
        return (sum(sum(g["priorities"]) for g in items),
                -sum(g["estimatedBps"] for g in items))

    complete = len(ordered) <= exact_limit
    if complete:
        for count in range(1, len(ordered) + 1):
            for items in combinations(ordered, count):
                if expired():
                    complete = False
                    break
                value = key(items)
                if -value[1] <= capacity_bps and value > key(selected):
                    selected = list(items)
            if not complete:
                break

    chosen = {g["flowId"] for g in selected}
    return {
        "advisoryOnly": True,
        "assumesDeferredTrafficRemoved": True,
        "scope": "modeled_mandatory_flows_on_this_direction_only",
        "optimalWithinGroups": complete,
        "suggestedFlowIds": sorted(chosen),
        "suggestedServiceIds": sorted(s for g in selected for s in g["serviceIds"]),
        "deferredServiceIds": sorted(s for g in ordered if g["flowId"] not in chosen
                                     for s in g["serviceIds"]),
        "estimatedSelectedBps": -key(selected)[1],
        "priorityWeight": key(selected)[0],
    }
