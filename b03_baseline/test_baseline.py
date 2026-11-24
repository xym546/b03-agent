"""Local checks of the closed loop through the expected HTTP response shape."""
from types import SimpleNamespace
import unittest

from baseline import run


class FakeSimulator:
    mode = "real"

    def __init__(self):
        self.path = ["old"]
        self.path_id = "P0"
        self.posts = []
        self.fail = False
        self.nodes = [dict(nodeId=n, nodeType="ground", status="online") for n in ("A", "B", "C")]
        metrics = dict(delayMs=1, jitterMs=0, ber=0, packetLossRate=0, bandwidthBps=10)
        self.links = [dict(linkId=lid, srcNodeId=a, dstNodeId=b, status=state,
                           params=metrics.copy(), linkType="fiber")
                      for lid, a, b, state in (("old", "A", "C", "down"),
                                               ("x", "A", "B", "up"), ("y", "B", "C", "up"))]

    @staticmethod
    def response(data):
        return dict(code=0, msg="ok", time="00:00:05.000000", tsUs=5000000, data=data)

    def get(self, path, params=None):
        service = dict(serviceId="S1", srcNodeId="A", dstNodeId="C", status="running",
                       serviceType="data", priority=2,
                       requirement=dict(delayMs=10, jitterMs=5, ber=.01,
                                        packetLossRate=.1, bandwidthBps=5))
        current = dict(serviceId="S1", pathId=self.path_id, source="system",
                       effectiveTime="00:00:05.000000",
                       segments=[dict(type="link", id=i) for i in self.path])
        if path == "/topology":
            data = dict(nodes=self.nodes, links=self.links)
        elif path == "/links":
            data = dict(links=self.links)
        elif path == "/services":
            data = dict(services=[service])
        elif path == "/services/S1":
            data = dict(service, path=current)
        elif path == "/services/S1/path":
            data = current
        elif path == "/events":
            now = params["sinceTsUs"]
            events = [dict(eventId="E1", type="link_state_changed", time="00:00:04.000000",
                           tsUs=4000000, payload=dict(instanceId="I", linkId="old"))] if now < 4000000 else []
            data = dict(events=events, nextSinceTsUs=4000000 if events else now)
        else:
            raise AssertionError(path)
        return self.response(data)

    def post(self, path, body, **kwargs):
        assert path == "/services/S1/path"
        self.posts.append(body)
        if self.fail:
            raise RuntimeError("timeout")
        self.path = [s["id"] for s in body["segments"]]
        self.path_id = "P1"
        return self.response(dict(result="accepted", pathId="P1",
                                  effectiveTime="00:00:05.000000", effectiveTsUs=5000000))


class BaselineTests(unittest.TestCase):
    def exercise(self, *, apply=True, fail=False):
        args = SimpleNamespace(start=None, instance_id="I", since_ts_us=0, apply=apply,
                               interval=5, duration=5, startup_check=False, max_polls=1,
                               event_limit=1000, max_snapshot_age=10, k=5, plan_timeout=2,
                               feedback_windows=0)
        client, logs = FakeSimulator(), []
        client.fail = fail
        code = run(client, args, lambda kind, **fields: logs.append((kind, fields)))
        return code, client, logs

    def test_event_to_path_readback(self):
        code, client, logs = self.exercise()
        self.assertEqual(0, code, logs)
        self.assertEqual(1, len(client.posts))
        self.assertEqual(["x", "y"], client.path)
        self.assertTrue(any(k == "readback" and data["matches"] for k, data in logs))

    def test_readonly_returns_plan_without_post(self):
        code, client, logs = self.exercise(apply=False)
        self.assertEqual(0, code, logs)
        self.assertFalse(client.posts)
        self.assertTrue(any(k == "plan" and data["result"]["actions"] for k, data in logs))

    def test_unknown_submit_result_is_not_retried(self):
        code, client, logs = self.exercise(fail=True)
        self.assertEqual(2, code, logs)
        self.assertEqual(1, len(client.posts))
        self.assertTrue(any(k == "submit_outcome_unknown" for k, _ in logs))


if __name__ == "__main__":
    unittest.main()
