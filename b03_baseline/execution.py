"""Validate, submit, and read back the baseline's ordered plans."""
from time import sleep
from uuid import uuid4

from algorithms.capacity import directional_ledger, directional_move
from algorithms.path_search import build_graph
from algorithms.qos import LIMITS, compose_qos, normalize, valid_metrics, violations
from simulator_api.services import query_service, query_service_path, set_service_path
from snapshot import data, oriented_link_ids


def validate_action(snapshot, action, allowed_ids):
    sid, flow = action["serviceId"], action["flowId"]
    services = {row["serviceId"]: row for row in snapshot["services"]}
    if sid not in allowed_ids or snapshot["serviceToFlow"].get(sid) != flow or sid not in services:
        raise ValueError("动作超出任务范围或 flow 已变化")
    service = services[sid]
    src, dst = service["srcNodeId"], service["dstNodeId"]
    if (src, dst) != (action["srcNodeId"], action["dstNodeId"]):
        raise ValueError("端点已变化")
    nodes = {n["nodeId"]: n for n in snapshot["nodes"]}
    links = {e["linkId"]: dict(e, params=normalize(e["params"])) for e in snapshot["links"]}
    if (src not in nodes or dst not in nodes or nodes[src]["status"] != "online"
            or nodes[dst]["status"] != "online"):
        raise ValueError("端点不可用")
    paths = snapshot["currentPaths"]
    current = paths[flow]
    old = list(action["oldLinkIds"])
    if current != old and current != old[::-1]:
        raise ValueError("该 flow 的现路已经变化")
    route = list(action["linkIds"])
    if not route or action["segments"] != [{"type": "link", "id": lid} for lid in route]:
        raise ValueError("路径段与链路序列不一致")
    graph = build_graph(nodes.values(), links.values())
    at, seen = src, {src}
    for i, lid in enumerate(route):
        nxt = next((v for v, edge in graph.get(at, ()) if edge == lid), None)
        if nxt is None or nxt in seen or (i < len(route)-1 and not nodes[nxt]["canTransit"]):
            raise ValueError("链路不连续、有环、已失效或中转节点不允许")
        at = nxt
        seen.add(at)
    if at != dst:
        raise ValueError("路径未到达目标")
    requirement = normalize(service["requirement"])
    params = [links[lid]["params"] for lid in route]
    if not valid_metrics(requirement) or not all(valid_metrics(p) for p in params):
        raise ValueError("当前 QoS 数据不足")
    if failures := violations(compose_qos(params), requirement):
        raise ValueError(f"最新路径未通过静态 QoS: {failures}")
    traffic = {}
    service_to_flow = snapshot["serviceToFlow"]
    for row in services.values():
        rate = row.get("estimatedTxBps", normalize(row["requirement"]).get("bandwidthBps"))
        if not isinstance(rate, (int, float)) or not 0 <= rate < float("inf"):
            raise ValueError("当前业务发送速率未知")
        traffic.setdefault(service_to_flow[row["serviceId"]], []).append(
            (row["srcNodeId"], row["dstNodeId"], rate))
    load = directional_ledger(paths, traffic, links)
    capacities = {lid: e["params"]["bandwidthBps"] for lid,e in links.items()}
    _, conflicts = directional_move(load, links, current, route, traffic[flow], capacities)
    if conflicts:
        raise ValueError(f"最新账本出现容量冲突: {conflicts}")
    return service


def actual_status(detail):
    r, a = normalize(detail.get("requirement") or {}), detail.get("actual")
    if not valid_metrics(r) or not isinstance(a, dict):
        return "unknown"
    fields = ("avgDelayMs", "jitterMs", "effBwBps", "errPct", "packetLossRate")
    if any(type(a.get(k)) not in (int, float) or not 0 <= a[k] < float("inf") for k in fields):
        return "unknown"
    if detail.get("status") != "running" or a["effBwBps"] <= 0:
        return "unknown"
    return "meets_estimated_thresholds" if (
        a["avgDelayMs"] <= r["delayMs"] + LIMITS["delayMs"]
        and a["jitterMs"] <= r["jitterMs"] + LIMITS["jitterMs"]
        and a["effBwBps"] >= r["bandwidthBps"]
        and a["errPct"] <= 100 * r["ber"]
        and a["packetLossRate"] <= r["packetLossRate"] + LIMITS["packetLossRate"]
    ) else "below_thresholds"


def execute_plan(client, plan, allowed_ids, refresh, emit, feedback_windows=2):
    for action in plan["actions"]:
        sid = action["serviceId"]
        try:
            latest = refresh()
            if latest["instanceId"] != plan["instanceId"]:
                raise ValueError("仿真实例已变化")
            validate_action(latest, action, allowed_ids)
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as error:
            emit("validation_failed", serviceId=sid, error=str(error))
            return False

        request_id = "baseline-" + uuid4().hex
        emit("submit", requestId=request_id, action=action)
        try:
            receipt = data(set_service_path(
                client, service_id=sid, request_id=request_id,
                src_node_id=action["srcNodeId"], dst_node_id=action["dstNodeId"],
                segments=action["segments"]))
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            emit("submit_outcome_unknown", serviceId=sid, requestId=request_id, error=str(error))
            return False
        emit("receipt", serviceId=sid, requestId=request_id, receipt=receipt)
        if receipt["result"] != "accepted":
            return False
        try:
            observed = data(query_service_path(client, service_id=sid))
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            emit("readback_failed", serviceId=sid, requestId=request_id, error=str(error))
            return False
        path = oriented_link_ids(observed, {e["linkId"]: e for e in latest["links"]},
                                 action["srcNodeId"], action["dstNodeId"])
        matches = path == list(action["linkIds"]) and observed.get("pathId") == receipt.get("pathId")
        emit("readback", serviceId=sid, requestId=request_id, observed=observed, matches=matches)
        if not matches:
            return False

        peers = [s["serviceId"] for s in latest["services"]
                 if latest["serviceToFlow"].get(s["serviceId"]) == action["flowId"]]
        for window in range(feedback_windows):
            sleep(1.05)
            for peer in peers:
                try:
                    response = query_service(client, service_id=peer)["response"]
                    detail = response["data"]
                    emit("qos_sample", serviceId=peer, requestId=request_id,
                         window=window + 1, tsUs=response["tsUs"],
                         actual=detail.get("actual"), requirement=detail.get("requirement"),
                         estimate=actual_status(detail))
                except (ValueError, RuntimeError, KeyError, OSError) as error:
                    emit("qos_sample_failed", serviceId=peer, requestId=request_id, error=str(error))
    return True
