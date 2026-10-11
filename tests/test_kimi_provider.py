import json
import unittest

import httpx
from openai import AsyncOpenAI
from pydantic import ValidationError

from nanocat.config.schema import Config, KimiProviderConfig
from nanocat.providers.kimi_provider import KimiProvider
from nanocat.providers.manager import make_provider
from nanocat.providers.registry import find_by_model


class KimiProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_token_requires_explicit_billing_mode(self):
        with self.assertRaises(ValidationError):
            KimiProviderConfig(api_key="test-key")
        self.assertIsNone(KimiProviderConfig().billing_mode)

    async def test_billing_mode_selects_endpoint(self):
        for mode, model, base in (
            ("subscription", "k3", "https://api.kimi.com/coding/v1"),
            ("pay_as_you_go", "kimi-k3", "https://api.moonshot.cn/v1"),
        ):
            with self.subTest(mode=mode):
                config = Config.model_validate(
                    {
                        "providers": {
                            "kimi": {
                                "apiKey": "test-key",
                                "billingMode": mode,
                            }
                        }
                    }
                )
                self.assertEqual(config.get_api_base(f"kimi/{model}"), base)
                provider = make_provider(f"kimi/{model}", config=config)
                self.assertIsInstance(provider, KimiProvider)
                self.assertEqual(str(provider._client.base_url), base + "/")
                await provider.aclose()

    def test_custom_endpoint_and_schema(self):
        config = KimiProviderConfig(
            api_key="test-key",
            billing_mode="subscription",
            api_base="https://api.kimi.ai/coding/v1",
        )
        self.assertEqual(config.resolved_api_base(), "https://api.kimi.ai/coding/v1")
        schema = KimiProviderConfig.model_json_schema(by_alias=True)
        self.assertEqual(
            schema["properties"]["billingMode"]["anyOf"][0]["enum"],
            ["subscription", "pay_as_you_go"],
        )

    def test_missing_kimi_key_does_not_use_other_credentials(self):
        config = Config.model_validate({"providers": {"moonshot": {"apiKey": "other-key"}}})
        with self.assertRaisesRegex(RuntimeError, "providers.kimi.apiKey"):
            make_provider("kimi/k3", config=config)
        self.assertEqual(config.get_provider_name("kimi-k2.5"), "moonshot")
        self.assertEqual(find_by_model("kimi-k2.5").name, "moonshot")

    async def test_chat_preserves_reasoning_and_tool_calls(self):
        def respond(request):
            self.assertEqual(request.url.path, "/coding/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "k3")
            self.assertEqual(body["reasoning_effort"], "max")
            self.assertEqual(body["messages"][0]["reasoning_content"], "reasoning")
            self.assertNotIn("internal", body["messages"][0])
            self.assertEqual(body["messages"][0]["tool_calls"][0]["id"], "call-1")
            return httpx.Response(
                200,
                json={
                    "id": "completion",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "k3",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "done",
                                "reasoning_content": "final reasoning",
                            },
                        }
                    ],
                },
            )

        provider = KimiProvider(api_key="test-key", api_base="https://api.kimi.com/coding/v1")
        await provider.aclose()
        provider._client = AsyncOpenAI(
            api_key="test-key",
            base_url="https://api.kimi.com/coding/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        self.addAsyncCleanup(provider.aclose)
        messages = [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "reasoning",
                "internal": True,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        ]
        result = await provider.chat(messages, model="kimi/k3", reasoning_effort="xhigh")
        self.assertEqual(result.content, "done")
        self.assertEqual(result.reasoning_content, "final reasoning")
        self.assertTrue(messages[0]["internal"])
