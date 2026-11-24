"""业务查询及路径设置。"""
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from simulator_api.client import SimulatorClient, validate_response


ServiceStatus = Literal["pending", "running", "stopped", "finished", "failed"]


SERVICE_STATUSES = ("pending", "running", "stopped", "finished", "failed")


def query_services(
    client: SimulatorClient,
    *,
    node_id: str | None = None,
    status: ServiceStatus | None = None,
):
    """返回业务列表；mock 中 node_id 筛选源/目的节点，不表示路径经过节点。"""
    if node_id is not None and (not isinstance(node_id, str) or not node_id.strip()):
        raise ValueError("node_id 必须是非空字符串")
    if status not in (None, *SERVICE_STATUSES):
        raise ValueError("status 必须是 pending/running/stopped/finished/failed")

    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_services.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        params = {"page": 1, "size": 500}
        if node_id is not None:
            params["nodeId"] = node_id
        if status is not None:
            params["status"] = status
        response = client.get("/services", params=params)

    validate_response(response)
    services = response["data"].get("services")
    if not isinstance(services, list):
        raise ValueError("响应缺少 data.services 列表")
    for service in services:
        if not isinstance(service, dict):
            raise ValueError("每个业务必须是字典格式")
        if any(not isinstance(service.get(k), str) or not service[k].strip()
               for k in ("serviceId", "srcNodeId", "dstNodeId")):
            raise ValueError("业务缺少有效的 serviceId/srcNodeId/dstNodeId")
        if service.get("serviceType") not in ("data", "image", "voice", "video"):
            raise ValueError("业务 serviceType 无效")
        if service.get("status") not in SERVICE_STATUSES:
            raise ValueError("业务 status 无效")
        if type(service.get("priority")) is not int:
            raise ValueError("业务 priority 必须是整数")

    if client.mode == "mock":
        if node_id is not None:
            services = [s for s in services if node_id in (s["srcNodeId"], s["dstNodeId"])]
        if status is not None:
            services = [s for s in services if s["status"] == status]
        response["data"]["services"] = services
    return {"dataSource": client.mode, "response": response}


def query_service(client: SimulatorClient, *, service_id: str):
    """对应 GET /services/{serviceId}，返回 dataSource 与官方响应。"""
    if not isinstance(service_id, str) or not service_id.strip():
        raise ValueError("service_id 必须是非空字符串")
    if client.mode == "mock":
        # 文件是按业务 ID 索引的样例集合，不是官方接口响应本身。
        path = (Path(__file__).parent / "mock").joinpath("mock_service_details.json")
        fixtures = json.loads(path.read_text(encoding="utf-8-sig"))
        if service_id not in fixtures:
            raise RuntimeError(f"仿真接口错误 code=1003: 不存在的测试业务 {service_id}")
        response = fixtures[service_id]
    else:
        response = client.get("/services/" + quote(service_id, safe=""))

    validate_response(response)
    service = response["data"]
    if service.get("serviceId") != service_id:
        raise ValueError("响应中的 serviceId 与请求不一致")
    if any(not isinstance(service.get(k), str) or not service[k].strip()
           for k in ("srcNodeId", "dstNodeId")):
        raise ValueError("业务缺少有效的 srcNodeId/dstNodeId")
    if service.get("status") not in SERVICE_STATUSES:
        raise ValueError("业务 status 无效")
    if service.get("serviceType") not in ("data", "image", "voice", "video"):
        raise ValueError("业务 serviceType 无效")
    if type(service.get("priority")) is not int:
        raise ValueError("业务 priority 必须是整数")

    # 文档未明确不同业务阶段的缺失值约定，先允许缺失或 null。
    # 不能把没有实测数据解释成实际指标为 0，也不能补一条虚构路径。
    for field in ("requirement", "actual", "path"):
        if service.get(field) is not None and not isinstance(service[field], dict):
            raise ValueError(f"业务 {field} 必须是字典或 null")
    current_path = service.get("path")
    if current_path is not None:
        segments = current_path.get("segments")
        if not isinstance(segments, list):
            raise ValueError("业务 path 缺少 segments 列表")
        for segment in segments:
            if (not isinstance(segment, dict)
                    or segment.get("type") not in ("link", "node")
                    or not isinstance(segment.get("id"), str)
                    or not segment["id"].strip()):
                raise ValueError("业务路径段必须具有合法的 type 和 id")
    return {"dataSource": client.mode, "response": response}


def query_service_path(client: SimulatorClient, *, service_id: str):
    """对应 GET /services/{serviceId}/path，保留官方字段及额外字段。"""
    if not isinstance(service_id, str) or not service_id.strip():
        raise ValueError("service_id 必须是非空字符串")
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_service_paths.json")
        fixtures = json.loads(path.read_text(encoding="utf-8-sig"))
        if service_id not in fixtures:
            raise RuntimeError(f"未配置业务 {service_id} 的路径测试样例；不代表真实业务不存在")
        response = fixtures[service_id]
    else:
        response = client.get("/services/" + quote(service_id, safe="") + "/path")

    validate_response(response)
    data = response["data"]
    if data.get("serviceId") != service_id:
        raise ValueError("响应中的 serviceId 与请求不一致")
    if not isinstance(data.get("segments"), list):
        raise ValueError("路径响应缺少 segments 列表")
    for segment in data["segments"]:
        if (not isinstance(segment, dict)
                or segment.get("type") not in ("link", "node")
                or not isinstance(segment.get("id"), str)
                or not segment["id"].strip()):
            raise ValueError("路径段必须具有合法的 type 和 id")
        if "auto" in segment and type(segment["auto"]) is not bool:
            raise ValueError("路径段 auto 必须是布尔值")

    # 空路径的 pathId/source/生效时间约定未在文档中明确，先允许 null。
    # 不自行补值，不推断服务失败后会返回哪种路径表现。
    for field in ("pathId", "effectiveTime", "source"):
        if data.get(field) is not None and not isinstance(data[field], str):
            raise ValueError(f"路径字段 {field} 必须是字符串或 null")
        if data["segments"] and (not isinstance(data.get(field), str) or not data[field].strip()):
            raise ValueError(f"非空路径缺少有效的 {field}")
    return {"dataSource": client.mode, "response": response}


def set_service_path(
    client: SimulatorClient,
    *,
    service_id: str,
    request_id: str,
    src_node_id: str,
    dst_node_id: str,
    segments: list[dict],
    retry_of: str | None = None,
):
    """提交路径；不自动生成 ID/重试，不替代连通性与 QoS 检查。"""
    for name, value in (("service_id", service_id), ("request_id", request_id),
                        ("src_node_id", src_node_id), ("dst_node_id", dst_node_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串")
    if len(request_id) > 64:
        raise ValueError("request_id 不能超过 64 字符")
    if retry_of is not None:
        if not isinstance(retry_of, str) or not retry_of.strip():
            raise ValueError("retry_of 必须是非空字符串")
        if "\r" in retry_of or "\n" in retry_of:
            raise ValueError("retry_of 不能含换行")
    if not isinstance(segments, list) or not segments:
        raise ValueError("segments 必须是非空列表")
    copied_segments = []
    for segment in segments:
        if (not isinstance(segment, dict)
                or set(segment) != {"type", "id"}
                or segment.get("type") not in ("link", "node")
                or not isinstance(segment.get("id"), str)
                or not segment["id"].strip()):
            raise ValueError("每个路径段必须只包含 type（link/node）和非空 id")
        copied_segments.append(dict(segment))
    body = {
        "requestId": request_id,
        "srcNodeId": src_node_id,
        "dstNodeId": dst_node_id,
        "segments": copied_segments,
    }
    if client.mode == "mock":
        path = (Path(__file__).parent / "mock").joinpath("mock_set_service_path.json")
        response = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        response = client.post(
            "/services/" + quote(service_id, safe="") + "/path",
            body, retry_of=retry_of,
        )
    validate_response(response)
    data = response["data"]
    if data.get("result") not in ("accepted", "rejected"):
        raise ValueError("路径回执 result 必须是 accepted 或 rejected")
    if data["result"] == "accepted":
        if any(not isinstance(data.get(k), str) or not data[k].strip()
               for k in ("pathId", "effectiveTime")):
            raise ValueError("受理回执缺少有效的 pathId/effectiveTime")
        if type(data.get("effectiveTsUs")) is not int:
            raise ValueError("受理回执缺少有效的 effectiveTsUs")
    # rejected 原样交给调用方处理；不能只凭函数返回就当作受理成功。
    return {"dataSource": client.mode, "response": response}
