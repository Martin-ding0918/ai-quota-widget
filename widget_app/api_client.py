"""OpenUsage HTTP 客户端。

- 登录：POST /api/auth/login，JSON {username, password} → {token}（64 位 hex）。
- 取数：GET /api/usage（Bearer token；不带 force 走服务端 5 分钟缓存）。
- 健康：GET /api/health（免登录）。
- token 固定 30 天过期、无滑动续期：收到 401 时自动用本地凭据重新登录并重试一次。
- 若账号启用了两步验证，登录接口会返回 requires_2fa（没有 token），此时报
  AuthError 并给出明确提示（悬浮窗不支持 2FA 交互）。
"""

from __future__ import annotations

from typing import Any, List, Optional

import requests


class OpenUsageError(Exception):
    """客户端错误基类。"""


class NetworkError(OpenUsageError):
    """连不上服务器（DNS、超时、拒绝连接等）。"""


class AuthError(OpenUsageError):
    """登录失败，或令牌失效且无法自动重登录。"""


class ApiError(OpenUsageError):
    """服务器返回了非预期的响应。"""


def _detail(resp: requests.Response, default: str = "") -> str:
    """尽量从 FastAPI 风格的错误体里取 detail 字段。"""
    try:
        payload = resp.json()
    except ValueError:
        return default
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return default


class OpenUsageClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: Optional[str] = None,
        token: str = "",
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.username = (username or "").strip()
        self.password = password
        self._token = token or ""
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()

    @property
    def token(self) -> str:
        return self._token

    # ---- 内部 ----

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            return self._session.request(
                method, self.base_url + path, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise NetworkError(f"连接失败：{exc}") from exc

    # ---- 公开 API ----

    def login(self) -> str:
        """用用户名密码换新 token；成功后覆盖 self._token 并返回。"""
        if not self.username or self.password is None:
            raise AuthError("缺少用户名或密码，无法登录")
        resp = self._request(
            "POST", "/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                raise ApiError("登录响应不是有效 JSON")
            if isinstance(payload, dict) and payload.get("requires_2fa"):
                raise AuthError(
                    "该账号启用了两步验证，悬浮窗暂不支持；"
                    "请在 OpenUsage 设置中为该账号关闭 2FA 后重试"
                )
            token = payload.get("token") if isinstance(payload, dict) else None
            if not token:
                raise ApiError("登录响应缺少 token")
            self._token = str(token)
            return self._token
        if resp.status_code in (400, 401):
            raise AuthError(_detail(resp, default="用户名或密码错误"))
        raise ApiError(f"登录失败（HTTP {resp.status_code}）")

    def _get_json(self, path: str, params: Optional[dict] = None) -> Any:
        """GET 请求 + 401 自动重登录重试一次 + JSON 解析（get_usage / get_service_usage 共用）。"""
        resp = self._request("GET", path, params=params)
        if resp.status_code == 401 and self.username and self.password is not None:
            self.login()
            resp = self._request("GET", path, params=params)
        if resp.status_code == 401:
            raise AuthError("令牌无效，且没有可用的登录凭据")
        if resp.status_code != 200:
            raise ApiError(f"{path} 返回 HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise ApiError(f"{path} 响应不是有效 JSON")

    def get_usage(self, force: bool = False) -> List[Any]:
        """取全部服务用量；401 时自动重登录一次再试。"""
        params = {"force": "true"} if force else None
        data = self._get_json("/api/usage", params=params)
        if not isinstance(data, list):
            raise ApiError("/api/usage 响应格式异常（期望数组）")
        return data

    def get_service_usage(self, service_id: int, force: bool = False) -> dict:
        """GET /api/usage/{id}（force=true 让服务端实时回源）；401 自动重登录重试一次。

        自适应双速轮询的活动档用：只拉 Kimi 单服务，比全量接口开销小。
        """
        params = {"force": "true"} if force else None
        data = self._get_json(f"/api/usage/{service_id}", params=params)
        if not isinstance(data, dict):
            raise ApiError(f"/api/usage/{service_id} 响应格式异常（期望对象）")
        return data

    def health(self) -> bool:
        """免登录连通性探测；连不上或非 200 都算不健康。"""
        try:
            resp = self._request("GET", "/api/health")
        except NetworkError:
            return False
        return resp.status_code == 200
