import asyncio
import json
import unittest

import httpx

import server


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, status_code=200, body=None):
        self.status_code = status_code
        self.body = body if body is not None else (
            'data: {"text":"OK"}\n\n'
            'data: {"text":"。"}\n\n'
            'data: {"text":"","finished":true}\n\n'
        )
        self.method = None
        self.url = None
        self.cookie = None
        self.payload = None

    async def handle_async_request(self, request):
        self.method = request.method
        self.url = str(request.url)
        self.cookie = request.headers.get("cookie")
        self.payload = json.loads(request.content)
        return httpx.Response(
            self.status_code,
            headers={"content-type": "text/event-stream"},
            text=self.body,
        )

    async def aclose(self):
        return None


class MonicaProviderMockTests(unittest.TestCase):
    def setUp(self):
        self.original_cookie = server.COOKIE_HEADER
        self.original_transport_factory = server.MONICA_TRANSPORT_FACTORY
        server.COOKIE_HEADER = "fake_session=not-a-real-secret"

    def tearDown(self):
        server.COOKIE_HEADER = self.original_cookie
        server.MONICA_TRANSPORT_FACTORY = self.original_transport_factory

    def test_current_web_request_and_sse_are_parsed(self):
        transport = FakeTransport()
        server.MONICA_TRANSPORT_FACTORY = lambda: transport

        answer = asyncio.run(server.Handler._collect_reply(None, "gpt-6.1-sol", "offline prompt"))

        self.assertEqual(answer, "OK。")
        self.assertEqual(transport.method, "POST")
        self.assertEqual(transport.url, "https://api.monica.im/api/custom_bot/chat")
        self.assertEqual(transport.cookie, "fake_session=not-a-real-secret")
        self.assertEqual(transport.payload["bot_uid"], "gpt_6_1_sol")
        self.assertEqual(transport.payload["data"]["use_model"], "gpt-6.1-sol")
        question = transport.payload["data"]["items"][1]
        self.assertEqual(question["item_type"], "question")
        self.assertEqual(question["data"]["content"], "offline prompt")
        self.assertTrue(transport.payload["task_uid"].startswith("task:"))

    def test_observed_gemini_bot_uid_mapping(self):
        payload = server._monica_request_payload("gemini-3.1-pro-preview-thinking", "offline prompt")
        self.assertEqual(payload["bot_uid"], "gemini_3_1_pro_preview_think")

    def test_upstream_status_is_reported_without_body(self):
        server.MONICA_TRANSPORT_FACTORY = lambda: FakeTransport(
            status_code=401,
            body="fake_session=not-a-real-secret",
        )
        with self.assertRaises(server.MonicaUpstreamError) as caught:
            asyncio.run(server.Handler._collect_reply(None, "gpt-6.1-sol", "offline prompt"))
        self.assertEqual(caught.exception.status_code, 401)
        message = server._upstream_message(caught.exception)
        self.assertIn("401", message)
        self.assertNotIn("not-a-real-secret", message)


if __name__ == "__main__":
    unittest.main()
