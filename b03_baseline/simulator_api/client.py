"""通用仿真 HTTP 客户端：不处理具体链路或业务字段。"""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

def validate_response(response):
    """检查所有接口共用的响应包络，返回原字典。"""
    if not isinstance(response, dict) or type(response.get("code")) is not int:
        raise ValueError("响应缺少有效的 code")
    if response["code"] != 0:
        raise RuntimeError(f"仿真接口错误 code={response['code']}: {response.get('msg', '')}")
    if not isinstance(response.get("data"), dict):
        raise ValueError("响应缺少 data 对象")
    if not isinstance(response.get("time"), str) or type(response.get("tsUs")) is not int:
        raise ValueError("响应缺少 time/tsUs 观测时间")
    return response


class SimulatorClient:
    """复用服务器配置，向 /api/open/v1 下的接口发送请求。"""

    def __init__(self, base_url=None, token=None, agent_id=None, timeout=None,
                 *, mode=None):
        mode = mode or "mock"
        agent_id = agent_id or "traditional-baseline"
        timeout = 5.0 if timeout is None else timeout
        if mode == "real" and not token:
            raise ValueError("real 模式必须通过环境变量提供 SIM_OPEN_TOKEN")
        if mode not in ("mock", "real"):
            raise ValueError("mode 必须是 mock 或 real")
        self.mode = mode
        if mode == "real" and not base_url:
            raise ValueError("real 模式需要设置 SIM_BASE_URL")
        parts = urlsplit(base_url or "")
        if mode == "real" and (parts.scheme not in ("http", "https") or not parts.netloc):
            raise ValueError("SIM_BASE_URL 必须是 http/https 服务器地址")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("SIM_BASE_URL 只填服务器地址和端口，不带接口路径")
        if parts.username or parts.password:
            raise ValueError("服务器地址不能包含账号密码")
        if len(agent_id) > 64:
            raise ValueError("agent_id 不能超过 64 字符")
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.timeout = timeout

    def get(self, path, params=None, *, retry_of=None):
        """path 例如 /links；params 例如 {"status": "down"}。"""
        return self._request("GET", path, params=params, retry_of=retry_of)

    def post(self, path, body, *, retry_of=None):
        """发送 JSON 对象；body 的 requestId 由具体业务调用方提供。

        不自动重试，不自动生成或更换 requestId。超时可能已经执行，
        调用方应保留原请求体，重试时沿用原 requestId。
        """
        if not isinstance(body, dict):
            raise ValueError("POST body 必须是字典格式")
        return self._request("POST", path, body=body, retry_of=retry_of)

    def _request(self, method, path, *, params=None, body=None, retry_of=None):
        """GET 与 POST 共用的传输及响应检查。"""
        if self.mode != "real":
            raise ValueError("mock 模式由具体业务函数提供样例，不发送 HTTP 请求")
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("接口路径应以 / 开头，查询参数通过 params 传入")
        url = self.base_url + "/api/open/v1" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "X-Open-Token": self.token,
            "X-Agent-Id": self.agent_id,
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if retry_of is not None:
            if not isinstance(retry_of, str) or not retry_of.strip():
                raise ValueError("retry_of 必须是非空字符串")
            if "\r" in retry_of or "\n" in retry_of:
                raise ValueError("retry_of 不能含换行")
            headers["X-Retry-Of"] = retry_of
        request = Request(url, headers=headers, data=data, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as result:
                response = json.load(result)
        except HTTPError as error:
            raise RuntimeError(f"HTTP 请求失败，状态码 {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError("仿真接口连接失败或超时") from None
        return validate_response(response)
