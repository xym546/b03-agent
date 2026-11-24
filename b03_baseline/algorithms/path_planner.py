"""Shared, read-only B03 path planner. See algorithms/README.md."""
from collections import defaultdict
from functools import cache
from math import isfinite
from time import monotonic

from .capacity import directed_path, directional_ledger, directional_move, path_load
from .path_search import build_graph, follows, qos_paths, shortest, yen
from .priority import recommend_priority_groups
from .qos import compose_qos, normalize, valid_metrics, violations


def plan_routes(snapshot, request, *, candidate_limit=5, timeout_seconds=2.0,
                congestion_candidates=0, qos_candidates=0, adaptive_candidates=0, _verify_incremental=False):
    if type(candidate_limit) is not int or candidate_limit < 1 or timeout_seconds <= 0:
        raise ValueError("invalid search budget")
    if type(congestion_candidates) is not int or congestion_candidates < 0:
        raise ValueError("invalid congestion candidate limit")
    if type(qos_candidates) is not int or qos_candidates < 0:
        raise ValueError("invalid QoS candidate limit")
    if type(adaptive_candidates) is not int or adaptive_candidates < 0:
        raise ValueError("invalid adaptive candidate limit")
    if adaptive_candidates and (congestion_candidates or qos_candidates):
        raise ValueError("adaptive and fixed supplements are mutually exclusive")
    mode = request.get("mode", "compare")
    targets = request.get("targetServiceIds")
    if (mode not in ("compare", "fast_recovery") or not isinstance(targets, list)
            or not targets or any(not isinstance(s, str) or not s for s in targets)):
        raise ValueError("expected targetServiceIds and compare or fast_recovery mode")
    result = {"snapshotId": snapshot["snapshotId"], "instanceId": snapshot["instanceId"],
              "status": "no_feasible_plan", "actions": [], "diagnostics": [],
              "currentPathDiagnostics": [], "mandatoryCapacityDiagnostics": [], "candidateSupplementReasons": {},
              "netGain": 0, "budgetExhausted": False, "unresolvedServiceIds": []}
    def expired():
        return monotonic() >= deadline

    nodes = snapshot["nodes"]
    links = {e["linkId"]: dict(e, params=normalize(e["params"])) for e in snapshot["links"]}
    if any(not valid_metrics(e["params"]) for e in links.values()):
        raise ValueError("invalid link QoS")
    active = {s["serviceId"]: s for s in snapshot["services"] if s["status"] == "running"}
    if any(s not in active for s in targets):
        raise ValueError("target services must be running and present in snapshot")
    allowed = set(snapshot["allowedServiceIds"])
    if not set(targets) <= allowed:
        raise ValueError("target services exceed the task scope")
    mapping = snapshot["serviceToFlow"]
    paths = {f: tuple(p) for f, p in snapshot["currentPaths"].items()
             if isinstance(p, list)}
    groups = defaultdict(list)
    endpoints = {}
    traffic = defaultdict(list)
    requirements = {}
    missing = []
    for sid, svc in active.items():
        flow = mapping.get(sid)
        req = normalize(svc.get("requirement") or {})
        rate = svc.get("estimatedTxBps", req.get("bandwidthBps"))
        if (not isinstance(flow, str) or flow not in paths or not valid_metrics(req)
                or type(rate) not in (int, float) or not isfinite(rate) or rate < 0
                or any(lid not in links for lid in paths[flow])):
            missing.append(sid)
            continue
        src, dst = svc["srcNodeId"], svc["dstNodeId"]
        pair = tuple(sorted((src, dst)))
        if flow in endpoints and endpoints[flow] != pair:
            raise ValueError("flow services must have the same endpoint pair")
        endpoints[flow] = pair
        groups[flow].append(sid)
        traffic[flow].append((src, dst, rate))
        requirements[sid] = req
    if missing:
        result.update(status="insufficient_state", unresolvedServiceIds=sorted(set(targets)))
        result["diagnostics"].append({"reason": "missing_service_state", "serviceIds": missing})
        result["currentPathDiagnostics"] = [
            {"serviceId": sid, "flowId": mapping.get(sid), "reasons": ["insufficient_state"]}
            for sid in sorted(set(targets))
        ]
        return result
    paths = {f: paths[f] for f in groups}
    graph = build_graph(nodes, links.values())
    up = {lid for edges in graph.values() for _, lid in edges}
    usable = {lid: links[lid] for lid in up}
    capacities = {lid: e["params"]["bandwidthBps"] for lid, e in links.items()}
    weights = {lid: links[lid]["params"]["delayMs"] for lid in up}
    load = directional_ledger(paths, traffic, links)
    target_flows = sorted({mapping[sid] for sid in targets})
    original = paths.copy()
    metrics_cache = {}

    @cache
    def flow_path_load(flow, path):
        return path_load(links, path, traffic[flow])

    def link_occupants(current, *, include_zero_rate=False):
        index = defaultdict(set)
        for flow, path in current.items():
            for key, rate in flow_path_load(flow, path).items():
                if rate > 0 or include_zero_rate:
                    index[key].add(flow)
        return index

    initial_occupants = link_occupants(paths)
    initial_users = link_occupants(paths, include_zero_rate=True) if adaptive_candidates else initial_occupants

    @cache
    def service_path_keys(sid, path):
        svc = active[sid]
        return directed_path(links, path, svc["srcNodeId"], svc["dstNodeId"])

    def metrics(path):
        if path not in metrics_cache:
            metrics_cache[path] = compose_qos([links[lid]["params"] for lid in path])
        return metrics_cache[path]

    @cache
    def static_checks(flow, path):
        if not path or any(lid not in usable for lid in path):
            return ()
        return tuple((sid, service_path_keys(sid, path)) for sid in groups[flow]
                     if not violations(metrics(path), requirements[sid]))

    def satisfied(current, occupancy, flows=None):
        good = set()
        for flow in current if flows is None else flows:
            for sid, keys in static_checks(flow, current[flow]):
                if keys and all(occupancy[lid, origin] <= capacities[lid] + 1e-6
                                for lid, origin in keys):
                    good.add(sid)
        return good

    def changed_services(flow, path, new_load, users):
        affected = {flow}
        keys = flow_path_load(flow, paths[flow]).keys() | flow_path_load(flow, path).keys()
        for key in keys:
            limit = capacities[key[0]] + 1e-6
            if (load[key] <= limit) != (new_load[key] <= limit):
                affected.update(users[key])
        return {sid for item in affected for sid in groups[item]}, affected

    def score(good):
        return sum(active[s].get("priority", 1) for s in good)

    for sid in sorted(set(targets)):
        flow = mapping[sid]
        path = paths[flow]
        keys = service_path_keys(sid, path)
        entry = {"serviceId": sid, "flowId": flow, "reasons": []}
        if not keys or any(lid not in usable for lid, _ in keys):
            entry["reasons"].append("no_usable_path")
        else:
            failed = violations(metrics(path), requirements[sid])
            if failed:
                entry["reasons"].append("static_qos")
                entry["violatedMetrics"] = failed
            bottlenecks = []
            for lid, origin in keys:
                key = (lid, origin)
                if load[key] <= capacities[lid] + 1e-6:
                    continue
                blockers = sorted(initial_occupants[key] - {flow})
                bottlenecks.append({"linkId": lid, "fromNodeId": origin,
                                    "capacityBps": capacities[lid], "estimatedLoadBps": load[key],
                                    "blockingFlowIds": blockers})
            if bottlenecks:
                entry["reasons"].append("capacity_conflict")
                entry["bottlenecks"] = bottlenecks
        result["currentPathDiagnostics"].append(entry)
    baseline = satisfied(paths, load)
    current_good = baseline
    deadline = monotonic() + timeout_seconds
    remaining = set(target_flows)
    candidates = {}
    for flow in target_flows:
        src, dst = endpoints[flow]
        if expired():
            break
        # Endpoints may always participate. Explicitly forbidden transit nodes may not.
        blocked = {n["nodeId"] for n in nodes if n.get("canTransit") is False} - {src, dst}
        local = {u: [(v, lid) for v, lid in edges if v not in blocked]
                 for u, edges in graph.items() if u not in blocked}
        if mode == "fast_recovery" and len(target_flows) == 1:
            candidates[flow] = yen(local, src, dst, weights, candidate_limit, expired)
        else:
            candidates[flow] = list(yen(local, src, dst, weights, candidate_limit, expired))
            congestion_limit, qos_limit = congestion_candidates, qos_candidates
            if adaptive_candidates and candidates[flow] and not expired():
                # Evaluate whether a base path can add weighted service coverage
                # on this snapshot before spending budget on a supplement.
                qos_failures, capacity_failures = 0, 0
                positive = False
                for path in candidates[flow]:
                    if expired():
                        break
                    if not path or path == paths[flow] or path == paths[flow][::-1]:
                        continue
                    passing = {sid for sid, _ in static_checks(flow, path)}
                    if not passing.intersection(allowed):
                        qos_failures += 1
                        continue
                    projected, bumps = directional_move(
                        load, links, paths[flow], path, traffic[flow], capacities)
                    if bumps:
                        capacity_failures += 1
                        continue
                    affected_ids, affected_flows = changed_services(flow, path, projected, initial_users)
                    good = satisfied(paths | {flow: path}, projected, affected_flows)
                    if score(good) > score(baseline & affected_ids):
                        positive = True
                        break
                if not positive and (qos_failures or capacity_failures):
                    if capacity_failures >= qos_failures:
                        congestion_limit = adaptive_candidates
                        reason = "capacity_conflict"
                    else:
                        qos_limit = adaptive_candidates
                        reason = "static_qos"
                    result["candidateSupplementReasons"][flow] = reason
            if congestion_limit and not expired():
                old_load = flow_path_load(flow, paths[flow])
                rate_ab = sum(rate for a, _, rate in traffic[flow] if a == src)
                rate_ba = sum(rate for a, _, rate in traffic[flow] if a == dst)
                congestion_weights = {}
                for lid in up:
                    edge = links[lid]
                    a, b = edge["srcNodeId"], edge["dstNodeId"]
                    # Yen uses one weight per physical link. Use the cheaper orientation
                    # as a search heuristic, then check the actual orientation on each route.
                    background_a = load[lid, a] - old_load[lid, a]
                    background_b = load[lid, b] - old_load[lid, b]
                    projected = min(max(background_a + rate_ab, background_b + rate_ba),
                                    max(background_b + rate_ab, background_a + rate_ba))
                    utilization = projected / capacities[lid] if capacities[lid] else 10.0
                    congestion_weights[lid] = weights[lid] + 50 * max(0, utilization - 0.8)
                added = yen(local, src, dst, congestion_weights, congestion_limit, expired)
                candidates[flow] = list(dict.fromkeys([*candidates[flow], *added]))
            if qos_limit and not expired():
                # Shared flow may serve different thresholds. Search the two
                # highest-priority distinct authorized requirement profiles.
                profiles = set()
                for sid in sorted(groups[flow], key=lambda s: (-active[s].get("priority", 1), s)):
                    if sid not in allowed:
                        continue
                    requirement = requirements[sid]
                    profile = tuple(sorted((key, requirement[key]) for key in
                                           ("delayMs", "jitterMs", "ber", "packetLossRate", "bandwidthBps")))
                    if profile in profiles:
                        continue
                    profiles.add(profile)
                    added = qos_paths(local, src, dst, links, requirement, qos_limit, expired)
                    candidates[flow] = list(dict.fromkeys([*candidates[flow], *added]))
                    if len(profiles) == 2 or expired():
                        break



    while remaining and not expired():
        users = link_occupants(paths, include_zero_rate=True)
        best = None
        diagnostics = []
        for flow in sorted(remaining):
            src, dst = endpoints[flow]
            attempted = False
            for path in candidates.get(flow, ()):
                if expired():
                    break
                if path == paths[flow] or not path:
                    continue
                attempted = True
                passing = [s for s in groups[flow] if s in allowed
                           and not violations(metrics(path), requirements[s])]
                if not passing:
                    diagnostics.append({"flowId": flow, "reason": "qos_rejected", "linkIds": path})
                    continue
                new_load, clashes = directional_move(load, links, paths[flow], path, traffic[flow], capacities)
                if clashes:
                    diagnostics.append({"flowId": flow, "reason": "capacity_conflict",
                                        "linkIds": sorted({lid for lid, _ in clashes}),
                                        "requestedLinkIds": path,
                                        "blockingFlowIds": sorted(
                                            {f for key in clashes for f in users[key]
                                             if f != flow and flow_path_load(f, paths[f])[key] > 0}),
                                        "directions": [
                                            {"linkId": lid, "fromNodeId": origin,
                                             "capacityBps": capacities[lid],
                                             "projectedBps": new_load[lid, origin]}
                                            for lid, origin in clashes]})
                    continue
                proposed = paths | {flow: path}
                affected_services, affected_flows = changed_services(flow, path, new_load, users)
                updated_good = satisfied(proposed, new_load, affected_flows)
                previous_good = current_good & affected_services
                delta = score(updated_good) - score(previous_good)
                if _verify_incremental:
                    reference = satisfied(proposed, new_load)
                    assert reference == (current_good - affected_services) | updated_good
                    assert delta == score(reference) - score(current_good)
                if delta <= 0:
                    diagnostics.append({"flowId": flow, "reason": "no_positive_gain", "linkIds": path})
                    continue
                sid = min(passing, key=lambda s: (-active[s].get("priority", 1), s))
                svc = active[sid]
                ordered = path if svc["srcNodeId"] == src else path[::-1]
                action = {"flowId": flow, "serviceId": sid, "srcNodeId": svc["srcNodeId"],
                          "dstNodeId": svc["dstNodeId"],
                          "oldLinkIds": (original[flow] if follows(links, original[flow],
                                                                  svc["srcNodeId"], svc["dstNodeId"])
                                         else original[flow][::-1]),
                          "linkIds": ordered, "segments": [{"type": "link", "id": e} for e in ordered],
                          "qos": metrics(path), "marginalGain": delta}
                utilization = max((new_load[lid, origin] / capacities[lid]
                                   if capacities[lid] else float("inf"))
                                  for lid, origin in flow_path_load(flow, path))
                key = (-delta, utilization, metrics(path)["delayMs"], path, flow)
                if best is None or key < best[0]:
                    best = key, flow, proposed, new_load, (current_good - affected_services) | updated_good, action
                requested = {s for s in targets if mapping[s] == flow}
                if mode == "fast_recovery" and len(target_flows) == 1 and all(s in (updated_good if s in affected_services else current_good)
                            for s in requested):
                    best = key, flow, proposed, new_load, (current_good - affected_services) | updated_good, action
                    break
            if not attempted:
                diagnostics.append({"flowId": flow, "reason": "no_alternative_in_search"})
        if best is None:
            result["diagnostics"].extend(diagnostics)
            break
        _, flow, paths, load, current_good, action = best
        remaining.remove(flow)
        result["actions"].append(action)
    search_budget_exhausted = expired()
    # Reachability proof under the fixed snapshot's transit rules. A direction
    # contributes only when its source/destination were connected before removal.
    reported = {(b["linkId"], b["fromNodeId"])
                for row in result["currentPathDiagnostics"] for b in row.get("bottlenecks", [])}
    reported.update((b["linkId"], b["fromNodeId"])
                    for row in result["diagnostics"] for b in row.get("directions", []))
    unresolved = sorted(set(targets) - current_good)
    if unresolved and reported:
        deadline_for_diagnostics = min(deadline, monotonic() + 0.05)
        selected = sorted(reported, key=lambda key: (-(load[key] - capacities[key[0]]), key))[:4]
        transit_blocked = {n["nodeId"] for n in nodes if n.get("canTransit") is False}
        for lid, origin in selected:
            mandatory = []
            demand = 0.0
            for sid, svc in active.items():
                if monotonic() >= deadline_for_diagnostics:
                    break
                flow = mapping[sid]
                src, dst = svc["srcNodeId"], svc["dstNodeId"]
                if (lid, origin) not in directed_path(links, paths[flow], src, dst):
                    continue
                blocked = transit_blocked - {src, dst}
                if shortest(graph, src, dst, weights, blocked) is None:
                    continue
                if shortest(graph, src, dst, weights, blocked, {(origin, lid)}) is None:
                    mandatory.append(sid)
                    demand += svc.get("estimatedTxBps", requirements[sid]["bandwidthBps"])
            if demand > capacities[lid] + 1e-6:
                mandatory_flows = {mapping[sid] for sid in mandatory}
                ranked_groups = []
                for flow in sorted(mandatory_flows):
                    members = [
                        sid for sid in groups[flow]
                        if (lid, origin) in directed_path(
                            links, paths[flow], active[sid]["srcNodeId"], active[sid]["dstNodeId"])
                    ]
                    ranked_groups.append({
                        "flowId": flow,
                        "serviceIds": members,
                        "priorities": [active[sid].get("priority", 1) for sid in members],
                        "estimatedBps": flow_path_load(flow, paths[flow])[lid, origin],
                    })
                modeled_load = sum(group["estimatedBps"] for group in ranked_groups)
                background_load = max(0.0, load[lid, origin] - modeled_load)
                available = max(0.0, capacities[lid] - background_load)
                recommendation = recommend_priority_groups(
                    ranked_groups, available, lambda: monotonic() >= deadline_for_diagnostics)
                recommendation.update(
                    estimatedBackgroundBps=background_load,
                    availableBudgetBps=available,
                    estimatedTotalSelectedBps=background_load + recommendation["estimatedSelectedBps"],
                    backgroundExceedsCapacity=background_load > capacities[lid] + 1e-6,
                )
                result["mandatoryCapacityDiagnostics"].append({
                    "reason": "mandatory_direction_capacity_conflict",
                    "linkId": lid, "fromNodeId": origin, "capacityBps": capacities[lid],
                    "mandatoryEstimatedBpsLowerBound": demand,
                    "serviceIds": sorted(mandatory),
                    "priorityRecommendation": recommendation,
                })
    result.update(netGain=score(current_good) - score(baseline), budgetExhausted=search_budget_exhausted,
                  unresolvedServiceIds=unresolved)
    if result["actions"]:
        result["status"] = "partial" if unresolved else "planned"
    elif not unresolved:
        result["status"] = "keep_current"
    return result
