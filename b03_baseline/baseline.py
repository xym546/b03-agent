"""Standalone rule-based B03 baseline. No Pi, LLM, or external packages."""
import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic, sleep, time_ns
from uuid import uuid4

from algorithms.path_planner import plan_routes
from execution import execute_plan
from orchestration.impact import analyze_impact
from simulator_api.client import SimulatorClient
from simulator_api.simulation import query_events, query_time, start_simulation
from snapshot import data, fetch_snapshot

NETWORK_EVENTS = {"link_state_changed", "node_state_changed", "service_state_changed"}


def read_events(client, cursor, limit):
    response = query_events(client, since_ts_us=cursor, limit=limit)["response"]
    events = response["data"]["events"]
    next_cursor = response["data"]["nextSinceTsUs"]
    stamps = [e["tsUs"] for e in events]
    if any(t <= cursor for t in stamps) or stamps != sorted(stamps):
        raise ValueError("事件时间戳与游标不一致")
    if next_cursor != (stamps[-1] if stamps else cursor):
        raise ValueError("返回的新游标不符合接口约定")
    return events, next_cursor, response["tsUs"]


def matched_events(events, instance):
    return [event for event in events
            if event["payload"].get("instanceId") == instance
            and event["type"] in NETWORK_EVENTS
            and not (event["type"] == "service_state_changed"
                     and event["payload"].get("pathChanged") is True)]


def run(client, args, emit):
    if args.start:
        suite, scenario, script = args.start
        request_id = "baseline-start-" + uuid4().hex
        emit("start_request", requestId=request_id, suiteId=suite, scenarioId=scenario, scriptId=script)
        launch = data(start_simulation(client, request_id=request_id, suite_id=suite,
                                       scenario_id=scenario, script_id=script))
        instance = launch["instanceId"]
        cursor = max(0, launch["t0TsUs"] - 1)
        emit("started", result=launch)
    else:
        instance = args.instance_id or "mock-instance"
        cursor = args.since_ts_us
        if cursor is None:
            cursor = data(query_time(client))["tsUs"] if client.mode == "real" else 0

    emit("run_config", instanceId=instance, mode=client.mode, apply=args.apply,
         intervalSeconds=args.interval, sinceTsUs=cursor, startupCheck=args.startup_check,
         scope="event_targets", candidateLimit=args.k)
    deadline = monotonic() + args.duration
    startup_pending = args.startup_check
    polls = 0
    retry_events = []
    last_snapshot_error = None
    while monotonic() < deadline and (args.max_polls is None or polls < args.max_polls):
        started = monotonic()
        events, cursor, observed_ts = read_events(client, cursor, args.event_limit)
        relevant = matched_events(events, instance)
        other_instances = sorted({str(e["payload"]["instanceId"]) for e in events
                                  if e["payload"].get("instanceId") and
                                  e["payload"]["instanceId"] != instance})
        emit("poll", instanceId=instance, observedTsUs=observed_ts, cursor=cursor,
             received=len(events), eventIds=[e["eventId"] for e in relevant],
             ignoredInstances=other_instances)
        if other_instances and client.mode == "real" and args.apply:
            raise RuntimeError("观察到其他仿真实例事件，停止写入，请确认独占实例与游标")
        retry_events.extend(relevant)
        if retry_events or startup_pending:
            try:
                snapshot = fetch_snapshot(client, instance, args.max_snapshot_age)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
                last_snapshot_error = str(error)
                emit("snapshot_unavailable", error=last_snapshot_error,
                     pendingEvents=len(retry_events))
            else:
                last_snapshot_error = None
                if snapshot["services"]:
                    impact = analyze_impact(retry_events, services=snapshot["services"],
                                            links=snapshot["links"], nodes=snapshot["nodes"],
                                            data_source=client.mode, instance_id=instance)
                    running = {s["serviceId"] for s in snapshot["services"]}
                    targets = (running if startup_pending else
                               {row["serviceId"] for row in impact["businessesToCheck"]
                                if "needs_check" in row["assessments"]} & running)
                    startup_pending = False
                    retry_events.clear()
                    if targets:
                        snapshot["allowedServiceIds"] = sorted(targets)
                        req = {"targetServiceIds": sorted(targets), "mode": "compare"}
                        before = monotonic()
                        plan = plan_routes(snapshot, req, candidate_limit=args.k,
                                           timeout_seconds=args.plan_timeout)
                        emit("plan", snapshot=snapshot, request=req, result=plan,
                             plannerMs=round((monotonic() - before) * 1000, 3))
                        if args.apply and plan["actions"]:
                            def refresh():
                                fresh = fetch_snapshot(client, instance, args.max_snapshot_age)
                                newer, _, _ = read_events(client, cursor, args.event_limit)
                                if any(e["payload"].get("instanceId") not in (None, instance) for e in newer):
                                    raise ValueError("检测到其他实例事件")
                                if matched_events(newer, instance):
                                    raise ValueError("规划后发生新事件，等待下一轮重新规划")
                                return fresh

                            if not execute_plan(client, plan, targets, refresh, emit, args.feedback_windows):
                                emit("plan_execution_stopped", snapshotId=snapshot["snapshotId"])
                                # Stop rather than risk a second write after an ambiguous receipt.
                                return 2
                    else:
                        emit("no_targets", eventIds=[e["eventId"] for e in relevant])
                else:
                    emit("no_running_services", pendingEvents=len(retry_events))
        polls += 1
        if args.max_polls is None or polls < args.max_polls:
            sleep(max(0.0, min(args.interval - (monotonic() - started), deadline-monotonic())))
    emit("finished", polls=polls, pendingEvents=len(retry_events),
         startupCheckPending=startup_pending, lastSnapshotError=last_snapshot_error)
    return 2 if last_snapshot_error else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", nargs=3, metavar=("SUITE", "SCENARIO", "SCRIPT"))
    group.add_argument("--instance-id")
    parser.add_argument("--apply", action="store_true", help="允许提交路径；默认只读规划")
    parser.add_argument("--startup-check", action="store_true")
    parser.add_argument("--since-ts-us", type=int)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--duration", type=float, default=1800)
    parser.add_argument("--max-polls", type=int)
    parser.add_argument("--event-limit", type=int, default=1000)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--plan-timeout", type=float, default=2)
    parser.add_argument("--max-snapshot-age", type=float, default=10)
    parser.add_argument("--feedback-windows", type=int, default=2)
    parser.add_argument("--log", type=Path, default=Path("logs") / f"baseline-{time_ns()}.jsonl")
    args = parser.parse_args()
    if not (0 < args.interval <= 10 and 0 < args.duration < float("inf")
            and 0 < args.plan_timeout < float("inf") and 0 < args.max_snapshot_age < float("inf")
            and args.k >= 1 and 1 <= args.event_limit <= 1000 and 0 <= args.feedback_windows <= 5
            and (args.max_polls is None or args.max_polls > 0)
            and (args.since_ts_us is None or args.since_ts_us >= 0)):
        parser.error("时间、候选数、轮询上限或反馈窗口不合法")
    if args.mode == "real" and not (args.start or args.instance_id):
        parser.error("real 模式需要 --start 或 --instance-id")
    if args.apply and args.mode != "real":
        parser.error("mock 模式不会提交真实路径")
    if args.start and args.since_ts_us is not None:
        parser.error("--start 自动确定初始游标")
    if args.mode == "mock" and args.max_polls is None:
        args.max_polls = 1

    try:
        client = SimulatorClient(base_url=os.environ.get("SIM_BASE_URL"),
                                 token=os.environ.get("SIM_OPEN_TOKEN"),
                                 agent_id=os.environ.get("SIM_AGENT_ID"),
                                 mode=args.mode)
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("x", encoding="utf8") as handle:
            def emit(kind, **fields):
                row = dict(kind=kind, capturedAtUs=time_ns() // 1000, **fields)
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                if kind not in {"poll", "qos_sample"}:
                    print(f"[baseline] {kind}", flush=True)

            try:
                return run(client, args, emit)
            except KeyboardInterrupt:
                emit("interrupted")
                return 130
            except (ValueError, RuntimeError, KeyError, TypeError, OSError) as error:
                emit("fatal", error=str(error))
                print(str(error), file=sys.stderr)
                return 2
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        print(f"日志：{args.log}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
