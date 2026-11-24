"""节点列表与详情。"""
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from simulator_api.client import SimulatorClient, validate_response


def query_nodes(
    client: SimulatorClient,
    *,
    node_type: Literal["uav", "ground", "air"] | None = None,
    status: Literal["online", "offline"] | None = None,
):
    """查询节点列表，可按节点类型及 online/offline 状态筛选。"""
    if node_type not in (None, "uav", "ground", "air"):
        raise ValueError("node_type 必须是 uav、ground 或 air")
    if status not in (None, "online", "offline"):
        raise ValueError("status 必须是 online 或 offline")

    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_nodes.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        params = {"page": 1, "size": 500}
        if node_type is not None:
            params["type"] = node_type
        if status is not None:
            params["status"] = status
        response = client.get("/nodes", params=params)

    validate_response(response)
    nodes = response["data"].get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("响应缺少 data.nodes 列表")
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("节点列表元素必须是对象")
        if any(not isinstance(node.get(k), str) or not node[k]
               for k in ("nodeId", "name")):
            raise ValueError("节点缺少有效的 nodeId/name")
        if node.get("nodeType") not in ("uav", "ground", "air"):
            raise ValueError("节点 nodeType 无效")
        if node.get("status") not in ("online", "offline"):
            raise ValueError("节点 status 无效")

    if client.mode == "mock":
        if node_type is not None:
            nodes = [node for node in nodes if node["nodeType"] == node_type]
        if status is not None:
            nodes = [node for node in nodes if node["status"] == status]
        response["data"]["nodes"] = nodes
    return {"dataSource": client.mode, "response": response}


def query_node(client: SimulatorClient, *, node_id: str):
    """返回节点详情，保留官方字段，不推断节点恢复能力。"""
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("node_id 必须是非空字符串")
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_node_details.json")
        fixtures = json.loads(path.read_text(encoding="utf-8-sig"))
        if node_id not in fixtures:
            raise RuntimeError(f"未配置节点 {node_id} 的详情测试样例；不代表真实节点不存在")
        response = fixtures[node_id]
    else:
        response = client.get("/nodes/" + quote(node_id, safe=""))

    validate_response(response)
    node = response["data"]
    if node.get("nodeId") != node_id:
        raise ValueError("响应中的 nodeId 与请求不一致")
    if any(not isinstance(node.get(k), str) or not node[k].strip()
           for k in ("name", "ip", "mask")):
        raise ValueError("节点缺少有效的 name/ip/mask")
    if node.get("nodeType") not in ("uav", "ground", "air"):
        raise ValueError("节点 nodeType 无效")
    if node.get("status") not in ("online", "offline"):
        raise ValueError("节点 status 必须是 online 或 offline")
    if not isinstance(node.get("time"), str) or type(node.get("tsUs")) is not int:
        raise ValueError("节点详情缺少 time/tsUs 观测时间")
    return {"dataSource": client.mode, "response": response}
