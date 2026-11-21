"""test_api_client.py — mock HTTP：登录、401 自动重登录、错误分支。"""

import sys
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from widget_app.api_client import ApiError, AuthError, NetworkError, OpenUsageClient

TOKEN = "a" * 64
NEW_TOKEN = "b" * 64


class FakeResponse:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload
        self._invalid_json = invalid_json
        self.text = ""

    def json(self):
        if self._invalid_json:
            raise ValueError("bad json")
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按脚本顺序返回响应/抛异常；记录全部调用便于断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers or {},
            "json": kw.get("json"),
            "params": kw.get("params"),
        })
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(script, **kw):
    sess = FakeSession(script)
    client = OpenUsageClient("http://test/", "u", "p", session=sess, **kw)
    return client, sess


class TestLogin(unittest.TestCase):
    def test_login_success(self):
        client, sess = make_client([FakeResponse(200, {"token": TOKEN, "user": {}})])
        token = client.login()
        self.assertEqual(token, TOKEN)
        self.assertEqual(client.token, TOKEN)
        call = sess.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "http://test/api/auth/login")
        self.assertEqual(call["json"], {"username": "u", "password": "p"})

    def test_login_invalid_credentials_detail(self):
        client, _ = make_client([FakeResponse(401, {"detail": "用户名或密码错误"})])
        with self.assertRaises(AuthError) as ctx:
            client.login()
        self.assertIn("用户名或密码错误", str(ctx.exception))

    def test_login_missing_credentials(self):
        client = OpenUsageClient("http://test", "", None)
        with self.assertRaises(AuthError):
            client.login()

    def test_login_requires_2fa(self):
        client, _ = make_client([FakeResponse(200, {"requires_2fa": True, "challenge_token": "x"})])
        with self.assertRaises(AuthError) as ctx:
            client.login()
        self.assertIn("两步验证", str(ctx.exception))

    def test_login_response_without_token(self):
        client, _ = make_client([FakeResponse(200, {"user": {}})])
        with self.assertRaises(ApiError):
            client.login()

    def test_login_server_error(self):
        client, _ = make_client([FakeResponse(500, None)])
        with self.assertRaises(ApiError):
            client.login()


class TestGetUsage(unittest.TestCase):
    def test_get_usage_ok(self):
        services = [{"service_type": "custom", "name": "Kimi Code"}]
        client, sess = make_client([FakeResponse(200, services)])
        data = client.get_usage()
        self.assertEqual(data, services)
        call = sess.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "http://test/api/usage")
        self.assertEqual(call["params"], None)

    def test_get_usage_force_param(self):
        client, sess = make_client([FakeResponse(200, [])])
        client.get_usage(force=True)
        self.assertEqual(sess.calls[0]["params"], {"force": "true"})

    def test_get_usage_401_then_relogin(self):
        services = [{"service_type": "custom"}]
        script = [
            FakeResponse(401, {"detail": "令牌已过期"}),
            FakeResponse(200, {"token": NEW_TOKEN}),
            FakeResponse(200, services),
        ]
        client, sess = make_client(script)
        data = client.get_usage()
        self.assertEqual(data, services)
        gets = [c for c in sess.calls if c["method"] == "GET"]
        posts = [c for c in sess.calls if c["method"] == "POST"]
        self.assertEqual(len(gets), 2)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["json"], {"username": "u", "password": "p"})
        # 重试请求带上了新 token
        self.assertEqual(gets[1]["headers"]["Authorization"], f"Bearer {NEW_TOKEN}")
        self.assertEqual(client.token, NEW_TOKEN)

    def test_get_usage_401_after_relogin_raises(self):
        script = [
            FakeResponse(401, {"detail": "令牌已过期"}),
            FakeResponse(200, {"token": NEW_TOKEN}),
            FakeResponse(401, {"detail": "令牌已过期"}),
        ]
        client, sess = make_client(script)
        with self.assertRaises(AuthError):
            client.get_usage()
        self.assertEqual(len(sess.calls), 3)

    def test_get_usage_401_without_credentials_no_login(self):
        client = OpenUsageClient("http://test", "", None, token="t" * 8,
                                 session=FakeSession([FakeResponse(401, {"detail": "x"})]))
        with self.assertRaises(AuthError):
            client.get_usage()

    def test_get_usage_500(self):
        client, _ = make_client([FakeResponse(500, None)])
        with self.assertRaises(ApiError):
            client.get_usage()

    def test_get_usage_invalid_json(self):
        client, _ = make_client([FakeResponse(200, None, invalid_json=True)])
        with self.assertRaises(ApiError):
            client.get_usage()

    def test_get_usage_not_a_list(self):
        client, _ = make_client([FakeResponse(200, {"unexpected": True})])
        with self.assertRaises(ApiError):
            client.get_usage()

    def test_get_usage_network_error(self):
        client, _ = make_client([requests.exceptions.ConnectionError("boom")])
        with self.assertRaises(NetworkError):
            client.get_usage()

    def test_network_error_message_kept(self):
        client, _ = make_client([requests.exceptions.Timeout("timeout!")])
        with self.assertRaises(NetworkError) as ctx:
            client.get_usage()
        self.assertIn("连接失败", str(ctx.exception))


class TestHealth(unittest.TestCase):
    def test_health_ok(self):
        client, _ = make_client([FakeResponse(200, {"status": "ok"})])
        self.assertTrue(client.health())

    def test_health_non_200(self):
        client, _ = make_client([FakeResponse(404, None)])
        self.assertFalse(client.health())

    def test_health_network_error(self):
        client, _ = make_client([requests.exceptions.ConnectionError("down")])
        self.assertFalse(client.health())


class TestBaseUrl(unittest.TestCase):
    def test_trailing_slash_stripped(self):
        client, sess = make_client([FakeResponse(200, {"status": "ok"})])
        client.health()
        self.assertEqual(sess.calls[0]["url"], "http://test/api/health")


class TestGetServiceUsage(unittest.TestCase):
    """单服务轮询端点 /api/usage/{id}（活动档 force 回源用）。"""

    def test_path_and_params(self):
        client, sess = make_client([FakeResponse(200, {"id": 3, "service_type": "custom"})])
        data = client.get_service_usage(3)
        self.assertEqual(data, {"id": 3, "service_type": "custom"})
        call = sess.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "http://test/api/usage/3")
        self.assertEqual(call["params"], None)

    def test_force_param(self):
        client, sess = make_client([FakeResponse(200, {"id": 3})])
        client.get_service_usage(3, force=True)
        self.assertEqual(sess.calls[0]["params"], {"force": "true"})

    def test_401_relogin(self):
        script = [
            FakeResponse(401, {"detail": "令牌已过期"}),
            FakeResponse(200, {"token": NEW_TOKEN}),
            FakeResponse(200, {"id": 3}),
        ]
        client, sess = make_client(script)
        client.get_service_usage(3)
        self.assertEqual(client.token, NEW_TOKEN)
        self.assertEqual(sess.calls[2]["headers"]["Authorization"], f"Bearer {NEW_TOKEN}")

    def test_not_a_dict(self):
        client, _ = make_client([FakeResponse(200, [])])
        with self.assertRaises(ApiError):
            client.get_service_usage(3)

    def test_server_error(self):
        client, _ = make_client([FakeResponse(500, None)])
        with self.assertRaises(ApiError):
            client.get_service_usage(3)


if __name__ == "__main__":
    unittest.main()
