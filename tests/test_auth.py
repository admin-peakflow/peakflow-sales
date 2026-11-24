"""Which credential `claude -p` gets. Nothing here calls Claude."""
import json
import unittest

from grizly import llm


class AuthEnv(unittest.TestCase):
    def test_api_key_never_reaches_claude(self) -> None:
        env = llm.auth_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x",
                            "ANTHROPIC_API_KEY": "sk-ant-api"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-x")

    def test_api_key_alone_is_dropped_and_refused(self) -> None:
        env = {"ANTHROPIC_API_KEY": "sk-ant-api"}
        self.assertNotIn("ANTHROPIC_API_KEY", llm.auth_env(env))
        with self.assertRaisesRegex(RuntimeError, "CLAUDE_CODE_OAUTH_TOKEN is not set"):
            llm.oauth_token(env)

    def test_other_providers_are_dropped(self) -> None:
        env = llm.auth_env({k: "1" for k in llm.FOREIGN_CREDENTIALS})
        for k in llm.FOREIGN_CREDENTIALS:
            self.assertNotIn(k, env)

    def test_nothing_set(self) -> None:
        with self.assertRaises(RuntimeError):
            llm.oauth_token({})

    def test_api_key_in_token_slot_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not look like a token"):
            llm.oauth_token({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-api03-x"})


class Quotes(unittest.TestCase):
    def test_quoted_token_from_env_file_is_unquoted(self) -> None:
        env = {"CLAUDE_CODE_OAUTH_TOKEN": '"sk-ant-oat01-x"'}
        self.assertEqual(llm.oauth_token(env), "sk-ant-oat01-x")


class Fatal(unittest.TestCase):
    def tearDown(self) -> None:
        llm.reset_fatal()

    def test_usage_limit_is_fatal(self) -> None:
        self.assertTrue(llm.is_fatal("Claude AI usage limit reached|1764000000"))
        self.assertTrue(llm.is_fatal("Invalid bearer token"))
        self.assertFalse(llm.is_fatal("result is not JSON"))

    def test_no_call_after_fatal(self) -> None:
        llm._set_fatal("usage limit reached")
        res = llm.call_claude("x", instruction="y", schema={}, model="haiku", budget=0.01)
        self.assertTrue(res.fatal)
        self.assertIn("batch stopped", res.error or "")

    def test_error_envelope_gives_readable_message(self) -> None:
        stdout = json.dumps({"type": "result", "is_error": True, "total_cost_usd": 0,
                             "result": "Claude AI usage limit reached"})
        res = llm.parse_output(1, stdout, "")
        self.assertEqual(res.error, "Claude AI usage limit reached")


if __name__ == "__main__":
    unittest.main()
