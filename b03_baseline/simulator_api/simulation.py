"""拓扑、事件、时间及仿真启动。"""
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from simulator_api.client import SimulatorClient, validate_response


def query_topology(
    client: SimulatorClient,
    *,
    node_type: Literal["uav", "ground", "air"] | None = None,
):
    """对应 GET /topology；类型筛选只筛节点，保留服务端返回的链路。"""
    if node_type not in (None, "uav", "ground", "air"):
        raise ValueError("node_type 必须是 uav、ground 或 air")
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_topology.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        params = {"type": node_type} if node_type is not None else None
        response = client.get("/topology", params=params)

    validate_response(response)
    data = response["data"]
    if not isinstance(data.get("nodes"), list) or not isinstance(data.get("links"), list):
        raise ValueError("拓扑响应必须包含 nodes 和 links 列表")
    for node in data["nodes"]:
        if not isinstance(node, dict):
            raise ValueError("拓扑中的每个节点必须是字典格式")
        if not isinstance(node.get("nodeId"), str) or not node["nodeId"].strip():
            raise ValueError("拓扑节点缺少有效的 nodeId")
        if node.get("nodeType") not in ("uav", "ground", "air"):
            raise ValueError("拓扑节点 nodeType 无效")
        if node.get("status") not in ("online", "offline"):
            raise ValueError("拓扑节点 status 无效")
    for link in data["links"]:
        if not isinstance(link, dict):
            raise ValueError("拓扑中的每条链路必须是字典格式")
        if any(not isinstance(link.get(k), str) or not link[k].strip()
               for k in ("linkId", "linkType", "srcNodeId", "dstNodeId")):
            raise ValueError("拓扑链路缺少有效的标识字段")
        if link.get("status") not in ("up", "down"):
            raise ValueError("拓扑链路 status 无效")

    if client.mode == "mock" and node_type is not None:
        # 参赛包实现只筛 nodes，不筛 links；不能当作同类型节点的诱导子图。
        data["nodes"] = [node for node in data["nodes"] if node["nodeType"] == node_type]
    return {"dataSource": client.mode, "response": response}


def query_events(client: SimulatorClient, *, since_ts_us: int = 0, limit: int = 100):
    """查询 tsUs 严格大于游标的一批事件，返回服务端 nextSinceTsUs。"""
    if type(since_ts_us) is not int or since_ts_us < 0:
        raise ValueError("since_ts_us 必须是非负整数")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit 必须是 1~1000 的整数")
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_events.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        response = client.get("/events", params={"sinceTsUs": since_ts_us, "limit": limit})

    validate_response(response)
    data = response["data"]
    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError("事件响应缺少 events 列表")
    if type(data.get("nextSinceTsUs")) is not int or data["nextSinceTsUs"] < 0:
        raise ValueError("事件响应缺少有效的 nextSinceTsUs")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("每个事件必须是字典格式")
        if any(not isinstance(event.get(k), str) or not event[k].strip()
               for k in ("eventId", "type", "time")):
            raise ValueError("事件缺少有效的 eventId/type/time")
        if type(event.get("tsUs")) is not int or event["tsUs"] < 0:
            raise ValueError("事件 tsUs 必须是非负整数")
        if not isinstance(event.get("payload"), dict):
            raise ValueError("事件 payload 必须是字典格式")
    if client.mode == "mock":
        # 与参赛包实现一致：严格大于游标，保留事件产生顺序。
        events = [event for event in events if event["tsUs"] > since_ts_us][:limit]
        data["events"] = events
        data["nextSinceTsUs"] = events[-1]["tsUs"] if events else since_ts_us
    return {"dataSource": client.mode, "response": response}


def query_time(client: SimulatorClient):
    """返回服务器时间；mock 仅提供固定占位值，不是实时时钟。"""
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_time.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        response = client.get("/time")

    validate_response(response)
    data = response["data"]
    if not isinstance(data.get("time"), str) or type(data.get("tsUs")) is not int:
        raise ValueError("时间响应缺少 data.time/data.tsUs")
    return {"dataSource": client.mode, "response": response}


def start_simulation(
    client: SimulatorClient,
    *,
    request_id: str,
    suite_id: str,
    scenario_id: str,
    script_id: str,
    retry_of: str | None = None,
):
    """对应 POST /simulation/start；ID 由调用方提供，不自动重试。"""
    for name, value in (("request_id", request_id), ("suite_id", suite_id),
                        ("scenario_id", scenario_id), ("script_id", script_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串")
    if len(request_id) > 64:
        raise ValueError("request_id 不能超过 64 字符")
    if retry_of is not None:
        if not isinstance(retry_of, str) or not retry_of.strip():
            raise ValueError("retry_of 必须是非空字符串")
        if "\r" in retry_of or "\n" in retry_of:
            raise ValueError("retry_of 不能含换行")

    body = {
        "requestId": request_id,
        "suiteId": suite_id,
        "scenarioId": scenario_id,
        "scriptId": script_id,
    }
    if client.mode == "mock":
        # 固定回执模板，只演示本次请求对应的结构，不证明场景实际存在。
        path = (Path(__file__).parent / "mock").joinpath("mock_start_simulation.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
        response["data"].update(suiteId=suite_id, scenarioId=scenario_id, scriptId=script_id)
    else:
        response = client.post("/simulation/start", body, retry_of=retry_of)

    validate_response(response)
    data = response["data"]
    if data.get("result") != "started":
        raise ValueError("仿真启动回执 result 必须是 started")
    for field, expected in (("suiteId", suite_id), ("scenarioId", scenario_id),
                            ("scriptId", script_id)):
        if data.get(field) != expected:
            raise ValueError(f"启动回执中的 {field} 与请求不一致")
    if any(not isinstance(data.get(k), str) or not data[k].strip()
           for k in ("instanceId", "t0Time")):
        raise ValueError("启动回执缺少有效的 instanceId/t0Time")
    if type(data.get("t0TsUs")) is not int:
        raise ValueError("启动回执缺少有效的 t0TsUs")
    return {"dataSource": client.mode, "response": response}
