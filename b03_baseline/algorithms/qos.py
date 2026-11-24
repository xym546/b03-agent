"""NetSim static path metrics and submission tolerances."""
from math import isfinite, prod, sqrt

LIMITS = {"delayMs": 1.0, "jitterMs": 1e-9, "ber": 1e-12, "packetLossRate": 1e-9}
FIELDS = (*LIMITS, "bandwidthBps")


def normalize(params):
    result = dict(params)
    legacy = result.pop("bandwitdhBps", None)
    if legacy is not None:
        if "bandwidthBps" in result and result["bandwidthBps"] != legacy:
            raise ValueError("conflicting bandwidth fields")
        result["bandwidthBps"] = legacy
    return result


def valid_metrics(params):
    return all(type(params.get(key)) in (int, float) and isfinite(params[key])
               and params[key] >= 0 for key in FIELDS) and params["packetLossRate"] <= 1


def compose_qos(params):
    return {
        "delayMs": sum(p["delayMs"] for p in params),
        "jitterMs": sqrt(sum(p["jitterMs"] ** 2 for p in params)),
        "bandwidthBps": min(p["bandwidthBps"] for p in params),
        "ber": sum(p["ber"] for p in params),
        "packetLossRate": 1 - prod(1 - p["packetLossRate"] for p in params),
    }


def violations(metrics, requirement):
    failed = [key for key, tolerance in LIMITS.items()
              if metrics[key] > requirement[key] + tolerance]
    if metrics["bandwidthBps"] < requirement["bandwidthBps"]:
        failed.append("bandwidthBps")
    return failed
