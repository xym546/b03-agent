"""Loopless paths on a multigraph, identified by ordered link IDs."""
from collections import defaultdict
from heapq import heappop, heappush


def build_graph(nodes, links):
    online = {n["nodeId"] for n in nodes if n["status"] == "online"}
    graph = defaultdict(list)
    for e in links:
        a, b = e["srcNodeId"], e["dstNodeId"]
        if e["status"] == "up" and a in online and b in online:
            graph[a].append((b, e["linkId"]))
            graph[b].append((a, e["linkId"]))
    return graph


def shortest(graph, src, dst, weights, banned_nodes=(), banned_edges=()):
    heap = [(0, (), (src,))]
    visited = set()
    while heap:
        cost, links, nodes = heappop(heap)
        u = nodes[-1]
        if u in visited:
            continue
        if u == dst:
            return cost, links, nodes
        visited.add(u)
        for v, lid in graph.get(u, ()):
            if v not in visited and v not in banned_nodes and (u, lid) not in banned_edges:
                heappush(heap, (cost + weights[lid], links + (lid,), nodes + (v,)))
    return None


def yen(graph, src, dst, weights, k, expired):
    first = shortest(graph, src, dst, weights)
    if first is None:
        return
    accepted, seen, heap = [first], {first[1]}, []
    yield first[1]
    while len(accepted) < k and not expired():
        _, links, nodes = accepted[-1]
        for i, spur in enumerate(nodes[:-1]):
            if expired():
                return
            root = links[:i]
            blocked = {(spur, path[i]) for _, path, _ in accepted
                       if len(path) > i and path[:i] == root}
            tail = shortest(graph, spur, dst, weights, set(nodes[:i]), blocked)
            if tail:
                cost, suffix, suffix_nodes = tail
                path = root + suffix
                if path not in seen:
                    seen.add(path)
                    heappush(heap, (sum(weights[e] for e in root) + cost,
                                    path, nodes[:i] + suffix_nodes))
        if not heap:
            return
        accepted.append(heappop(heap))
        yield accepted[-1][1]


def follows(links, path, src, dst):
    for lid in path:
        edge = links.get(lid)
        if not edge:
            return False
        a, b = edge["srcNodeId"], edge["dstNodeId"]
        if src not in (a, b):
            return False
        src = b if src == a else a
    return src == dst

def qos_paths(graph, src, dst, links, requirement, count, expired, max_expansions=600):
    """Enumerate low-delay simple paths with monotone QoS prefix pruning."""
    from .qos import LIMITS

    delay_max = requirement["delayMs"] + LIMITS["delayMs"]
    jitter_max = (requirement["jitterMs"] + LIMITS["jitterMs"]) ** 2
    ber_max = requirement["ber"] + LIMITS["ber"]
    loss_max = requirement["packetLossRate"] + LIMITS["packetLossRate"]
    min_capacity = requirement["bandwidthBps"]
    heap = [(0.0, (), (src,), 0.0, 0.0, 1.0)]
    found = 0
    for _ in range(max_expansions):
        if not heap or expired():
            return
        delay, path, nodes, jitter_sq, ber, success = heappop(heap)
        u = nodes[-1]
        if u == dst:
            if path:
                yield path
                found += 1
                if found >= count:
                    return
            continue
        for v, lid in graph.get(u, ()):
            if v in nodes:
                continue
            p = links[lid]["params"]
            new_delay = delay + p["delayMs"]
            new_jitter = jitter_sq + p["jitterMs"] ** 2
            new_ber = ber + p["ber"]
            new_success = success * (1 - p["packetLossRate"])
            if (p["bandwidthBps"] < min_capacity or new_delay > delay_max
                    or new_jitter > jitter_max or new_ber > ber_max or 1 - new_success > loss_max):
                continue
            heappush(heap, (new_delay, path + (lid,), nodes + (v,),
                            new_jitter, new_ber, new_success))
