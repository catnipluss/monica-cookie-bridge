import unittest

import server


class MonicaBridgeTests(unittest.TestCase):
    def test_transcript_serializes_roles_as_json_data(self):
        prompt = server.format_messages([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up"},
        ])
        self.assertIn('"role":"system","content":"Be concise."', prompt)
        self.assertIn('"role":"assistant","content":"First answer"', prompt)
        self.assertTrue(prompt.endswith('[{"role":"system","content":"Be concise."},{"role":"user","content":"First question"},{"role":"assistant","content":"First answer"},{"role":"user","content":"Follow-up"}]'))

    def test_rejects_non_string_role(self):
        with self.assertRaises(ValueError):
            server.format_messages([{"role": ["user"], "content": "bad"}])

    def test_tolerates_multimodal_and_tool_history(self):
        # 富对话历史（多模态块、tool 角色、tool_calls）必须转成纯文本，而不是 400。
        prompt = server.format_messages([
            {"role": "system", "content": "Be concise."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "file body"},
            {"role": "user", "content": [
                {"type": "text", "text": "Now summarize."},
                {"type": "image_url", "image_url": {"url": "x"}},
            ]},
        ])
        self.assertIn('"role":"system","content":"Be concise."', prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("[tool result c1]", prompt)
        self.assertIn("Now summarize.", prompt)
        self.assertIn("[non-text block 'image_url' omitted]", prompt)

    def test_model_catalog(self):
        original = server.MODEL_IDS
        try:
            server.MODEL_IDS = ["gpt-4o-mini", "claude-3.5-sonnet"]
            result = server.list_models()
            self.assertEqual([x["id"] for x in result["data"]], server.MODEL_IDS)
        finally:
            server.MODEL_IDS = original

    def test_verified_bot_mappings_match_observed_web_catalog(self):
        self.assertEqual(server.MODEL_BOT_UIDS["gpt-6.1-sol"], "gpt_6_1_sol")
        self.assertEqual(server.MODEL_BOT_UIDS["gemini-3.1-pro-preview-thinking"], "gemini_3_1_pro_preview_think")


if __name__ == "__main__":
    unittest.main()
