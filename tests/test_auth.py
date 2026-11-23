"""Which credential `claude -p` gets. Nothing here calls Claude."""
import unittest

from grizly import llm


class AuthEnv(unittest.TestCase):
    def test_oauth_token_wins_and_drops_api_key(self) -> None:
        env = llm.auth_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x",
                            "ANTHROPIC_API_KEY": "sk-ant-api"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(llm.auth_mode(env), "oauth")

    def test_api_key_alone_is_kept(self) -> None:
        env = llm.auth_env({"ANTHROPIC_API_KEY": "sk-ant-api", "CLAUDE_CODE_OAUTH_TOKEN": ""})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-api")
        self.assertEqual(llm.auth_mode(env), "api_key")

    def test_nothing_set(self) -> None:
        self.assertEqual(llm.auth_mode({}), "")




class Quotes(unittest.TestCase):
    def test_quoted_key_from_env_file_is_unquoted(self) -> None:
        env = llm.auth_env({"ANTHROPIC_API_KEY": '"sk-ant-api"'})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-api")


if __name__ == "__main__":
    unittest.main()
