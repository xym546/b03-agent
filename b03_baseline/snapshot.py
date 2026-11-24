"""Read a planning snapshot through the simulator's public API."""
from time import monotonic
from uuid import uuid4

from algorithms.path_search import follows
from simulator_api.links import query_links
from simulator_api.services import query_service, query_service_path, query_services
from simulator_api.simulation import query_topology


def data(result):
    return result["response"]["data"]


def fetch_rows(client, kind, id_field, **filters):
    if client.mode == "mock":
        query = query_links if kind == "links" else query_services
        return data(query(client, **filters))[kind]
    rows = {}
    for page in range(1, 101):
        batch = client.get("/" + kind, {"page": page, "size": 500, **filters})["data"][kind]
        for row in batch:
            key = row[id_field]
            if key in rows:
                raise ValueError(f"{kind} 分页出现重复 ID，快照可能已变化")
            rows[key] = row
        if len(batch) < 500:
            return list(rows.values())
    raise ValueError(f"{kind} 分页超过限制，未形成完整快照")


def oriented_link_ids(path, links, src, dst):
    segments = path.get("segments")
    if not isinstance(segments, list) or any(
            s.get("type") != "link" or not isinstance(s.get("id"), str) for s in segments):
        return None
    ids = [s["id"] for s in segments]
    if not ids:
        return [] if path.get("pathId") is None else None
    if follows(links, ids, src, dst):
        return ids
    if follows(links, ids[::-1], src, dst):
        return ids[::-1]
    return None


def fetch_snapshot(client, instance_id, max_age=10.0):
    started = monotonic()
    topo_response = query_topology(client)["response"]
    nodes = [
        dict(row, canTransit=row.get("canTransit", row.get("nodeType") == "ground"))
        for row in topo_response["data"]["nodes"]
    ]
    node_map = {row["nodeId"]: row for row in nodes}
    links = fetch_rows(client, "links", "linkId")
    link_map = {row["linkId"]: row for row in links}
    services, mapping, paths, issues = [], {}, {}, []
    owners = {}
    for node in nodes:
        if ip := node.get("ip"):
            if ip in owners and owners[ip] != node["nodeId"]:
                raise ValueError("快照存在重复节点 IP，无法安全归并底层 flow")
            owners[ip] = node["nodeId"]

    for item in fetch_rows(client, "services", "serviceId", status="running"):
        sid = item["serviceId"]
        detail = data(query_service(client, service_id=sid))
        if detail["status"] != "running":
            raise ValueError(f"{sid} 在采集期间改变运行状态，请重取快照")
        services.append(detail)
        src, dst = sorted((detail["srcNodeId"], detail["dstNodeId"]))
        if src not in node_map or dst not in node_map:
            raise ValueError(f"{sid} 的业务端点不在拓扑中")
        flow = f"{src}|{dst}"
        mapping[sid] = flow
        path = detail.get("path") or {}
        ids = oriented_link_ids(path, link_map, src, dst)
        if ids is None or not path.get("segments"):
            path = data(query_service_path(client, service_id=sid))
            ids = oriented_link_ids(path, link_map, src, dst)
        if ids is None:
            issues.append({"serviceId": sid, "reason": "unknown_current_path"})
        elif flow in paths and paths[flow] != ids:
            issues.append({"serviceId": sid, "reason": "inconsistent_shared_flow_path"})
        else:
            paths[flow] = ids
        detail["path"] = path

    if issues:
        raise ValueError(f"存在无法核对的现路：{issues}")
    if monotonic() - started > max_age:
        raise ValueError("快照采集跨度超限")
    return dict(snapshotId=uuid4().hex, instanceId=instance_id, tsUs=topo_response["tsUs"],
                nodes=nodes, links=links, services=services, serviceToFlow=mapping,
                currentPaths=paths, allowedServiceIds=[])
