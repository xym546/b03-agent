"""按事件关联业务，输出待检查清单；不计算 QoS、不选择策略、不提交路径。"""
import argparse
import json

from simulator_api.client import SimulatorClient
from simulator_api.links import query_link, query_links
from simulator_api.nodes import query_node
from simulator_api.services import query_service, query_service_path, query_services
from simulator_api.simulation import query_events


def collect_and_analyze_impact(client, events, *, instance_id):
    """按本批事件补查观测，然后生成待检查业务清单。"""
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValueError("必须指定要分析的 instance_id")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError("事件必须是字典列表")

    issues = []

    def fetch(query, **params):
        try:
            return query(client, **params)["response"]["data"]
        except (ValueError, RuntimeError, OSError) as error:
            resource_id = params.get("service_id") or params.get("link_id") or params.get("node_id")
            issues.append({
                "query": query.__name__, "resourceId": resource_id,
                "reason": str(error),
            })
            return None

    def collect_ids(event_type, field):
        ids = {}
        for event in events:
            payload = event.get("payload")
            if event.get("type") == event_type and isinstance(payload, dict):
                resource_id = payload.get(field)
                if (payload.get("instanceId") == instance_id
                        and isinstance(resource_id, str) and resource_id.strip()):
                    ids[resource_id] = None
        return ids

    service_ids = collect_ids("service_state_changed", "serviceId")
    link_ids = collect_ids("link_state_changed", "linkId")
    node_ids = collect_ids("node_state_changed", "nodeId")
    network_event = bool(link_ids or node_ids)
    summaries = {}
    if network_event:
        response = fetch(query_services, status="running")
        '''这里只查询了运行中业务。'''
        if response is not None:
            rows = response["services"]
            if client.mode == "real" and len(rows) >= 500:
                issues.append({"query": "query_services", "resourceId": None,
                               "reason": "业务列表达到单页上限，影响范围可能未查全"})
            for row in rows:
                if row.get("status") == "running":
                    summaries[row["serviceId"]] = row
                    service_ids[row["serviceId"]] = None

    services = []
    for service_id in service_ids:
        detail = fetch(query_service, service_id=service_id)
        # 摘要仍能证明业务端点，但不能替代缺失的路径。
        observation = detail if detail is not None else summaries.get(service_id)
        if observation is None:
            continue
        if (network_event and observation.get("status") == "running"
                and not (observation.get("path") or {}).get("segments")):
            path = fetch(query_service_path, service_id=service_id)
            if path is not None:
                observation = {**observation, "path": path}
        services.append(observation)

    # 节点事件需要链路两端信息，才能识别经过该节点的业务。
    links = {}
    if node_ids:
        response = fetch(query_links)
        if response is not None:
            links = {link["linkId"]: link for link in response["links"]}
            if client.mode == "real" and len(response["links"]) >= 500:
                issues.append({"query": "query_links", "resourceId": None,
                               "reason": "链路列表达到单页上限，部分路径端点可能未知"})
    for link_id in link_ids:
        if link_id not in links:
            link = fetch(query_link, link_id=link_id)
            if link is not None:
                links[link_id] = link
    nodes = []
    for node_id in node_ids:
        node = fetch(query_node, node_id=node_id)
        if node is not None:
            nodes.append(node)

    result = analyze_impact(
        events, services=services, links=list(links.values()), nodes=nodes,
        data_source=client.mode, instance_id=instance_id,
    )
    result["queryIssues"] = issues
    return result


def analyze_impact(events, *, services, links, nodes, data_source, instance_id):
    """输入同一测试实例的观测。

    services 为业务详情列表（允许路径缺失），links/nodes 为当前列表。
    缺少数据会输出 unknown，不视为业务不受影响。调用方负责取数和时间新鲜度。
    """
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("必须指定要分析的 instance_id")
    for value in (events, services, links, nodes):
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError("事件、业务、链路、节点必须是字典列表")
    link_index = {link["linkId"]: link for link in links}
    node_index = {node["nodeId"]: node for node in nodes}
    service_index = {service["serviceId"]: service for service in services}
    results = {}
    unhandled = []
    ignored = 0

    def record(service_id, assessment, reason, event, resource=None):
        item = results.setdefault(service_id, {
            "serviceId": service_id,
            "assessments": [], "evidence": [],
            "serviceObservation": service_index.get(service_id),
        })
        if assessment not in item["assessments"]:
            item["assessments"].append(assessment)
        evidence = {
            "eventId": event.get("eventId"),
            "srcEventId": event.get("payload", {}).get("srcEventId"),
            "eventTsUs": event.get("tsUs"),
            "reason": reason,
            "event": event,
        }
        if resource is not None:
            evidence["currentResource"] = resource
        item["evidence"].append(evidence)

    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            unhandled.append({"event": event, "reason": "缺少有效 payload"})
            continue
        if not payload.get("instanceId"):
            unhandled.append({"event": event, "reason": "事件缺少 instanceId"})
            continue
        if payload["instanceId"] != instance_id:
            ignored += 1
            continue
        kind = event.get("type")
        if kind == "service_state_changed":
            sid = payload.get("serviceId")
            if not isinstance(sid, str) or not sid:
                unhandled.append({"event": event, "reason": "业务事件缺少 serviceId"})
            else:
                record(sid, "needs_check",
                       "事件已明确业务对象；需检查当前状态，不能仅凭历史事件认定仍失败", event)
            continue
        if kind not in ("link_state_changed", "node_state_changed"):
            unhandled.append({"event": event, "reason": "尚未支持的事件类型"})
            continue
        key = "linkId" if kind == "link_state_changed" else "nodeId"
        resource_id = payload.get(key)
        if not isinstance(resource_id, str) or not resource_id:
            unhandled.append({"event": event, "reason": f"事件缺少 {key}"})
            continue
        resource = (link_index if key == "linkId" else node_index).get(resource_id)
        for sid, service in service_index.items():
            # 网络事件只关联在运业务，直接业务事件走上面的分支。
            if service.get("status") != "running":
                continue
            if key == "nodeId" and resource_id in (service.get("srcNodeId"), service.get("dstNodeId")):
                record(sid, "needs_check", "变化节点是业务端点；端点离线不能仅靠改路解决", event, resource)
                continue
            path = service.get("path")
            segments = path.get("segments") if isinstance(path, dict) else None
            if not isinstance(segments, list) or not segments:
                record(sid, "unknown", "没有可用的当前路径，暂不能排除关联", event, resource)
                continue
            matched, uncertain = False, resource is None
            for segment in segments:
                if not isinstance(segment, dict):
                    uncertain = True
                    continue
                segment_type, segment_id = segment.get("type"), segment.get("id")
                if segment_type == "link":
                    if key == "linkId":
                        matched |= segment_id == resource_id
                    else:
                        link = link_index.get(segment_id)
                        if link is None:
                            uncertain = True
                        else:
                            matched |= resource_id in (link.get("srcNodeId"), link.get("dstNodeId"))
                elif segment_type == "node":
                    matched |= key == "nodeId" and segment_id == resource_id
                    uncertain = True  # 必经节点段不是完整逐跳路径，不能排除自动补齐链路。
                else:
                    uncertain = True
            if matched:
                if key == "linkId":
                    status = resource.get("status") if resource else None
                    reason = ("当前路径经过该链路，最新查询显示链路 down" if status == "down"
                              else "当前路径经过变化链路；结合最新参数和业务需求进一步校验，不直接认定故障")
                else:
                    reason = "当前路径经过变化节点；移动/上线不自动等于业务失败，需结合最新状态检查"
                record(sid, "needs_check", reason, event, resource)
            elif uncertain:
                record(sid, "unknown", "路径或资源数据不完整，不能排除影响", event, resource)

    return {
        "dataSource": data_source, "instanceId": instance_id,
        "businessesToCheck": list(results.values()),
        "unhandledEvents": unhandled, "ignoredOtherInstanceEvents": ignored,
        "limitations": [
            "这是待检查清单，不是故障确诊、恢复方案或 LLM 分流结果",
            "未做路径 QoS 计算；调用方需保证业务列表完整及观测时效",
            "未列出的业务仅表示本批数据中未发现关联，不保证全网无异常",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="影响分析样例：只读取 mock，不调用模型或控制接口")
    parser.add_argument("--demo", action="store_true", required=True)
    parser.parse_args()
    client = SimulatorClient(mode="mock")
    events = query_events(client)["response"]["data"]["events"]
    result = collect_and_analyze_impact(client, events, instance_id="mock-instance")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
