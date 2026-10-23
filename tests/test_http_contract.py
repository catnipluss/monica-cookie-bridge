import http.client
import json
import threading
import unittest
from urllib.parse import urlencode

import httpx

import server


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, status_code=200, body=None):
        self.status_code = status_code
        self.body = body if body is not None else (
            'data: {"text":"offline mock answer"}\n\n'
            'data: {"text":"","finished":true}\n\n'
        )

    async def handle_async_request(self, _request):
        return httpx.Response(
            self.status_code,
            headers={"content-type": "text/event-stream"},
            text=self.body,
        )

    async def aclose(self):
        return None


class LocalHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            "PORT": server.PORT,
            "API_KEY": server.API_KEY,
            "COOKIE_HEADER": server.COOKIE_HEADER,
            "MODEL_IDS": server.MODEL_IDS,
            "MODEL_BOT_UIDS": server.MODEL_BOT_UIDS,
            "MONICA_TRANSPORT_FACTORY": server.MONICA_TRANSPORT_FACTORY,
            "UPSTREAM_RETRY_ATTEMPTS": server.UPSTREAM_RETRY_ATTEMPTS,
        }
        # 错误路径测试不测重试；关掉退避等待，避免每个失败请求空等 5 秒。
        server.UPSTREAM_RETRY_ATTEMPTS = 1
        self.httpd = server.ThreadingHTTPServer((server.HOST, 0), server.Handler)
        server.PORT = self.httpd.server_address[1]
        server.API_KEY = "test-local-api-key"
        server.COOKIE_HEADER = None
        server.MODEL_IDS = ["test-model"]
        server.MODEL_BOT_UIDS = {"test-model": "gpt_6_1_sol"}
        server.MONICA_TRANSPORT_FACTORY = lambda: FakeTransport()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        for name, value in self.originals.items():
            setattr(server, name, value)

    def request(self, method, path, *, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.PORT, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = (response.status, response.read())
        conn.close()
        return result

    def test_loopback_and_host_guard(self):
        conn = http.client.HTTPConnection("127.0.0.1", server.PORT, timeout=3)
        conn.putrequest("GET", "/health", skip_host=True)
        conn.putheader("Host", "evil.example")
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        conn.close()

    def test_setup_page_is_local_and_shows_only_local_api_key(self):
        status, body = self.request("GET", "/setup")
        self.assertEqual(status, 200)
        self.assertIn(b"test-local-api-key", body)
        self.assertNotIn(b"fake_session", body)

    def test_model_catalog_requires_local_api_key(self):
        status, _ = self.request("GET", "/v1/models")
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/v1/models", headers={"Authorization": f"Bearer {server.API_KEY}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "test-model")

    def test_setup_is_origin_checked_and_clears_session(self):
        form = urlencode({"cookie_header": "fake_session=not-a-real-secret", "models": "test-model", "action": "save"})
        status, _ = self.request("POST", "/setup", body=form, headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertIsNone(server.COOKIE_HEADER)

        origin = f"http://127.0.0.1:{server.PORT}"
        status, body = self.request("POST", "/setup", body=form, headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": origin})
        self.assertEqual(status, 200)
        self.assertNotIn(b"fake_session=not-a-real-secret", body)
        self.assertIn(b"test-local-api-key", body)
        self.assertEqual(server.COOKIE_HEADER, "fake_session=not-a-real-secret")

        clear_form = urlencode({"action": "clear"})
        status, _ = self.request("POST", "/setup", body=clear_form, headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": origin})
        self.assertEqual(status, 200)
        self.assertIsNone(server.COOKIE_HEADER)

    def test_setup_accepts_page_csrf_token_when_origin_is_rewritten(self):
        form = urlencode({"csrf_token": server.CSRF_TOKEN, "cookie_header": "fake_session=not-a-real-secret", "models": "test-model", "action": "save"})
        status, body = self.request("POST", "/setup", body=form, headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:11388"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"fake_session=not-a-real-secret", body)
        self.assertEqual(server.COOKIE_HEADER, "fake_session=not-a-real-secret")

    def test_malformed_message_fails_before_provider_call(self):
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"
        body = json.dumps({"model": "test-model", "messages": [{"role": ["user"], "content": "bad"}]})
        status, response = self.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": f"Bearer {server.API_KEY}", "Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn(b"invalid_request_error", response)

    def test_model_id_must_be_in_local_allowlist(self):
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"
        body = json.dumps({"model": "not-configured", "messages": [{"role": "user", "content": "hello"}]})
        status, response = self.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": f"Bearer {server.API_KEY}", "Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn(b"Choose a model ID configured", response)

    def test_mocked_chat_completion_route(self):
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"
        body = json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
        status, response = self.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": f"Bearer {server.API_KEY}", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["choices"][0]["message"]["content"], "offline mock answer")

    def test_mocked_sse_route(self):
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"
        body = json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        status, response = self.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": f"Bearer {server.API_KEY}", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn(b"offline mock answer", response)
        self.assertIn(b"data: [DONE]", response)

    def test_nonstream_provider_error_does_not_leak_exception(self):
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"
        server.MONICA_TRANSPORT_FACTORY = lambda: FakeTransport(status_code=502, body="fake_session=not-a-real-secret")
        body = json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
        status, response = self.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": f"Bearer {server.API_KEY}", "Content-Type": "application/json"})
        self.assertEqual(status, 502)
        self.assertIn(b"HTTP 502", response)
        self.assertNotIn(b"fake_session=not-a-real-secret", response)

    def test_stream_provider_error_before_headers_is_redacted(self):
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"
        server.MONICA_TRANSPORT_FACTORY = lambda: FakeTransport(status_code=502, body="fake_session=not-a-real-secret")
        body = json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        status, response = self.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": f"Bearer {server.API_KEY}", "Content-Type": "application/json"})
        self.assertEqual(status, 502)
        self.assertIn(b"HTTP 502", response)
        self.assertNotIn(b"fake_session=not-a-real-secret", response)

    def test_setup_body_limit(self):
        status, _ = self.request("POST", "/setup", body=None, headers={"Content-Length": str(server.MAX_BODY + 1), "Origin": f"http://127.0.0.1:{server.PORT}"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
