import asyncio
import unittest
from unittest.mock import patch

import httpx

from nanocat.application.command_dispatcher import CommandDispatcher
from nanocat.bus.events import InboundMessage
from nanocat.config.schema import Config
from nanocat.providers.discovery import ModelDiscoveryError, discover_models


class ModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, respond):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.aclose)
        return patch("nanocat.providers.discovery.httpx.AsyncClient", return_value=client)

    async def test_kimi_billing_endpoints_and_auth(self):
        for mode, host, path in (
            ("subscription", "api.kimi.com", "/coding/v1/models"),
            ("pay_as_you_go", "api.moonshot.cn", "/v1/models"),
        ):
            config = Config.model_validate(
                {
                    "providers": {
                        "kimi": {
                            "apiKey": "test-key",
                            "billingMode": mode,
                            "extraHeaders": {"X-Test": "value"},
                        }
                    }
                }
            )

            def respond(request):
                self.assertEqual(request.url.host, host)
                self.assertEqual(request.url.path, path)
                self.assertEqual(request.headers["authorization"], "Bearer test-key")
                self.assertEqual(request.headers["x-test"], "value")
                return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]})

            with self.client(respond):
                self.assertEqual(await discover_models(config, "kimi"), ("kimi", ["a", "b"]))

    async def test_anthropic_pagination(self):
        config = Config.model_validate({"providers": {"anthropic": {"apiKey": "test-key"}}})

        def respond(request):
            self.assertEqual(request.headers["x-api-key"], "test-key")
            if request.url.params.get("after_id"):
                return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False})
            return httpx.Response(
                200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"}
            )

        with self.client(respond):
            self.assertEqual(await discover_models(config, "anthropic"), ("anthropic", ["a", "b"]))

    async def test_invalid_http_and_repeated_cursor(self):
        config = Config.model_validate({"providers": {"openai": {"apiKey": "test-key"}}})
        for response in (
            httpx.Response(401, text="secret upstream data"),
            httpx.Response(200, json={"unexpected": []}),
            httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"}),
        ):
            with self.client(lambda request: response):
                with self.assertRaises(ModelDiscoveryError) as error:
                    await discover_models(config, "openai")
                self.assertNotIn("secret", str(error.exception))

    async def test_unknown_and_oauth_provider_fail_without_network(self):
        for name in ("unknown", "openai_codex", "kimi"):
            with self.assertRaises(ModelDiscoveryError):
                await discover_models(Config(), name)

    async def test_cancellation_propagates(self):
        async def respond(request):
            raise asyncio.CancelledError

        with self.client(respond):
            with self.assertRaises(asyncio.CancelledError):
                await discover_models(Config(), "custom")

    def test_discovery_does_not_block_fast_command_lane(self):
        dispatcher = CommandDispatcher(None, None)
        message = InboundMessage(
            channel="web", chat_id="local", sender_id="user", content="/model discover kimi"
        )
        self.assertFalse(dispatcher._is_fast_command(message))
        message.content = "/stop"
        self.assertTrue(dispatcher._is_fast_command(message))
