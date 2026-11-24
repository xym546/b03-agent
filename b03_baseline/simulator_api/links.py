"""链路列表与详情。"""
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from simulator_api.client import SimulatorClient, validate_response


def query_links(
    client: SimulatorClient,
    *,
    node_id: str | None = None,
    status: Literal["up", "down"] | None = None,
):
    """返回 {dataSource, response}；失败抛出异常，不用空列表掩盖错误。"""
    if status not in (None, "up", "down"):
        raise ValueError("status 必须是 up 或 down")
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_links.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        params = {"page": 1, "size": 500}
        if node_id is not None:
            params["nodeId"] = node_id
        if status is not None:
            params["status"] = status
        response = client.get("/links", params=params)

    # mock 同样遵守通用响应约定；具体链路字段由本函数检查。
    validate_response(response)
    data = response["data"]
    if not isinstance(data.get("links"), list):
        raise ValueError("响应缺少 data.links 列表")

    if client.mode == "mock":
        links = data["links"]
        if node_id is not None:
            links = [x for x in links if node_id in (x["srcNodeId"], x["dstNodeId"])]
        if status is not None:
            links = [x for x in links if x["status"] == status]
        response["data"]["links"] = links
    return {"dataSource": client.mode, "response": response}


def query_link(client: SimulatorClient, *, link_id: str):
    """对应 GET /links/{linkId}；不将带宽参数当作实时吞吐量。"""
    if not isinstance(link_id, str) or not link_id.strip():
        raise ValueError("link_id 必须是非空字符串")
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_link_details.json")
        fixtures = json.loads(path.read_text(encoding="utf-8-sig"))
        if link_id not in fixtures:
            raise RuntimeError(f"未配置链路 {link_id} 的详情测试样例；不代表真实链路不存在")
        response = fixtures[link_id]
    else:
        response = client.get("/links/" + quote(link_id, safe=""))

    validate_response(response)
    link = response["data"]
    if link.get("linkId") != link_id:
        raise ValueError("响应中的 linkId 与请求不一致")
    if any(not isinstance(link.get(k), str) or not link[k].strip()
           for k in ("linkType", "srcNodeId", "dstNodeId")):
        raise ValueError("链路缺少有效的 linkType/srcNodeId/dstNodeId")
    if link.get("status") not in ("up", "down"):
        raise ValueError("链路 status 必须是 up 或 down")
    if not isinstance(link.get("params"), dict):
        raise ValueError("链路缺少 params 对象")
    if not isinstance(link.get("time"), str) or type(link.get("tsUs")) is not int:
        raise ValueError("链路详情缺少 time/tsUs 观测时间")
    return {"dataSource": client.mode, "response": response}
