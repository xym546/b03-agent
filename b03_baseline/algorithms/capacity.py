"""Conservative shared-link accounting in bit/s."""
from collections import Counter


def directed_path(links, path, src, dst):
    """Return egress capacity keys for a service's send direction."""
    for route in (path, path[::-1]):
        at = src
        keys = []
        for lid in route:
            edge = links[lid]
            a, b = edge["srcNodeId"], edge["dstNodeId"]
            if at not in (a, b):
                break
            keys.append((lid, at))
            at = b if at == a else a
        else:
            if at == dst:
                return keys
    return []


def directional_ledger(paths, groups, links):
    load = Counter()
    for flow, path in paths.items():
        load.update(path_load(links, path, groups[flow]))
    return load


def path_load(links, path, traffic):
    load = Counter()
    for src, dst, rate in traffic:
        for key in directed_path(links, path, src, dst):
            load[key] += rate
    return load


def directional_move(load, links, old, new, traffic, capacities):
    removed = path_load(links, old, traffic)
    added = path_load(links, new, traffic)
    updated = load.copy()
    updated.subtract(removed)
    updated.update(added)
    conflicts = sorted(key for key in removed.keys() | added.keys()
                       if updated[key] > max(load[key], capacities[key[0]]) + 1e-6)
    return updated, conflicts
