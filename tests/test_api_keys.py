import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "EOR_Extraction_optimized",
    "Question_writing_and_verification_optomized",
    "create_remediation_assignment",
)


class ApiKeyTests(unittest.TestCase):
    def load_module(self, name, env, dotenv_effect=None):
        dependencies = {
            dependency: MagicMock()
            for dependency in (
                "openai", "dotenv", "aiomysql",
                "Question_writing_and_verification_optomized",
            )
        }
        dependencies["dotenv"].load_dotenv.side_effect = dotenv_effect
        pydantic = MagicMock()
        pydantic.BaseModel = type("BaseModel", (), {})
        dependencies["pydantic"] = pydantic
        with patch.dict(sys.modules, dependencies), patch.dict(os.environ, env, clear=True):
            spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        client = dependencies["openai"].OpenAI if name == MODULES[0] else dependencies["openai"].AsyncOpenAI
        return module, client

    def test_environment_key_fallbacks(self):
        for name in MODULES:
            for variable in ("MY_API_KEY", "OPENAI_API_KEY"):
                with self.subTest(module=name, variable=variable):
                    module, client = self.load_module(name, {variable: "test-only"})
                    manager = module.ConversationManager()
                    client.assert_called_once_with(api_key="test-only")
                    self.assertIs(manager.client, client.return_value)

    def test_key_precedence(self):
        for name in MODULES:
            with self.subTest(module=name):
                module, client = self.load_module(
                    name, {"MY_API_KEY": "legacy-test-only", "OPENAI_API_KEY": "openai-test-only"}
                )
                module.ConversationManager()
                expected = "legacy-test-only" if name == MODULES[0] else "openai-test-only"
                client.assert_called_once_with(api_key=expected)

    def test_missing_or_empty_keys_fail_before_client_creation(self):
        for name in MODULES:
            for env in ({}, {"MY_API_KEY": "", "OPENAI_API_KEY": ""}):
                with self.subTest(module=name, env=env):
                    module, client = self.load_module(name, env)
                    with self.assertRaisesRegex(RuntimeError, "MY_API_KEY|OPENAI_API_KEY"):
                        module.ConversationManager()
                    client.assert_not_called()

    def test_explicit_key_overrides_defaults(self):
        for name in MODULES[:2]:
            for env in ({}, {"OPENAI_API_KEY": "environment-test-only"}):
                with self.subTest(module=name, env=env):
                    module, client = self.load_module(name, env)
                    module.ConversationManager(api_key="explicit-test-only")
                    client.assert_called_once_with(api_key="explicit-test-only")

    def test_defaults_are_read_after_dotenv_loading(self):
        for name in MODULES:
            with self.subTest(module=name):
                def populate_environment(*args, **kwargs):
                    os.environ["MY_API_KEY"] = "dotenv-test-only"

                module, client = self.load_module(name, {}, populate_environment)
                module.ConversationManager()
                client.assert_called_once_with(api_key="dotenv-test-only")


if __name__ == "__main__":
    unittest.main()
