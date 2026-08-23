"""Lifecycle owner for NanoCat core API and Web/BFF listeners."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import uvicorn
from loguru import logger

from nanocat.api.app import create_api_app, create_web_app
from nanocat.api.artifacts import ArtifactRegistry
from nanocat.api.auth import ApiAuthenticator, WebSessionAuth, token_fingerprint
from nanocat.api.events import SseBroker
from nanocat.channels.web import WebChannel


class _EmbeddedServer(uvicorn.Server):
    """Run Uvicorn under the runtime supervisor without owning process signals."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _bind_socket(host: str, port: int) -> socket.socket:
    errors: list[OSError] = []
    for family, socktype, protocol, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    ):
        listener = socket.socket(family, socktype, protocol)
        try:
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(address)
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            return listener
        except OSError as exc:
            errors.append(exc)
            listener.close()
    if errors:
        raise errors[-1]
    raise OSError(f"Unable to resolve listener host {host!r}")


def _socket_url(listener: socket.socket, configured_host: str) -> str:
    address = listener.getsockname()
    bound_host = str(address[0])
    port = int(address[1])
    if listener.family == socket.AF_INET6:
        host = "::1" if bound_host in {"::", "0:0:0:0:0:0:0:0"} else bound_host
        authority = f"[{host}]" if ":" in host else host
        return f"http://{authority}:{port}"
    host = "127.0.0.1" if bound_host == "0.0.0.0" else bound_host
    return f"http://{host}:{port}"


class ApiRuntime:
    """Own both listeners, authentication state, proxy client and SSE broker."""

    def __init__(
        self,
        *,
        control: Any,
        config: Any,
        configuration: Any,
        web_channel: WebChannel | None,
        activity_journal: Any | None = None,
        static_dir: str | Path | None = None,
        terminal_output: Callable[[str], None] | None = None,
        endpoint_file: str | Path | None = None,
    ) -> None:
        self.control = control
        self.config = config
        self.configuration = configuration
        self.web_channel = web_channel
        self.activity_journal = activity_journal
        self.static_dir = Path(static_dir) if static_dir is not None else None
        self.terminal_output = terminal_output or (
            lambda message: print(message, file=sys.stderr, flush=True)
        )
        self.endpoint_file = Path(endpoint_file) if endpoint_file is not None else None
        self.broker = SseBroker()
        self.artifacts = ArtifactRegistry(config.workspace_path / "media" / "web")
        self.api_auth = ApiAuthenticator(
            config.api.auth_token if config.api.enabled else None
        )
        web_config = config.channels.web
        self.web_auth = WebSessionAuth(
            web_config.password,
            trusted_proxies=list(web_config.trusted_proxies),
        )
        self._servers: list[uvicorn.Server] = []
        self._tasks: list[asyncio.Task[Any]] = []
        self._listeners: list[socket.socket] = []
        self._started = False
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.core_url: str | None = None
        self.web_url: str | None = None

        if web_channel is not None:
            web_channel.bind_broker(self.broker)
            web_channel.bind_control(control)
            web_channel.bind_artifacts(self.artifacts)
        if activity_journal is not None:
            bind_activity = getattr(control, "set_activity_journal", None)
            if callable(bind_activity):
                bind_activity(activity_journal)
            if web_channel is not None:
                web_channel.bind_activity(activity_journal)

    @property
    def enabled(self) -> bool:
        return bool(self.config.api.enabled or self.config.channels.web.enabled)

    @property
    def endpoints(self) -> dict[str, Any]:
        return {
            "coreApi": self.core_url if self.config.api.enabled else None,
            "privateCoreApi": (
                self.core_url if not self.config.api.enabled and self.core_url else None
            ),
            "web": self.web_url,
            "tokenFingerprint": token_fingerprint(self.api_auth.token),
            "webProtected": self.web_auth.protected,
        }

    async def apply_configuration(
        self,
        config: Any,
        changed_paths: tuple[str, ...],
    ) -> None:
        """Apply listener-independent HTTP and Web authentication settings."""
        changed = set(changed_paths)
        if "api.authToken" in changed:
            self.api_auth.reconfigure(config.api.auth_token)
            if self.api_auth.generated:
                self.terminal_output(f"NanoCat API generated token: {self.api_auth.token}")
        if changed & {"channels.web.password", "channels.web.trustedProxies"}:
            await self.web_auth.reconfigure(
                config.channels.web.password,
                trusted_proxies=list(config.channels.web.trusted_proxies),
            )
        if changed & {
            "api.authToken",
            "channels.web.password",
            "channels.web.trustedProxies",
        }:
            try:
                self._write_endpoints()
            except OSError as exc:
                logger.warning(
                    "Failed to refresh HTTP endpoint metadata ({})",
                    type(exc).__name__,
                )

    async def _start_server(
        self, app: Any, *, host: str, port: int, name: str
    ) -> tuple[uvicorn.Server, socket.socket, str]:
        listener = _bind_socket(host, port)
        server = _EmbeddedServer(
            uvicorn.Config(
                app,
                host=host,
                port=int(listener.getsockname()[1]),
                log_config=None,
                access_log=False,
                lifespan="on",
                proxy_headers=False,
                limit_concurrency=256,
                timeout_keep_alive=10,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]), name=f"nanocat.{name}")
        self._servers.append(server)
        self._listeners.append(listener)
        self._tasks.append(task)
        deadline = asyncio.get_running_loop().time() + 5.0
        while not server.started:
            if task.done():
                error = task.exception()
                raise RuntimeError(f"{name} listener failed to start") from error
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{name} listener startup timed out")
            await asyncio.sleep(0.01)
        url = _socket_url(listener, host)
        logger.info("NanoCat {} listening at {}", name, url)
        return server, listener, url

    async def start(self) -> None:
        """Bind required listeners and return after both startup handshakes."""
        if self._started:
            return
        if self._closed:
            raise RuntimeError("API runtime cannot restart after close")
        self._started = True
        api_enabled = bool(self.config.api.enabled)
        web_enabled = bool(self.config.channels.web.enabled)
        if not api_enabled and not web_enabled:
            return
        if self.web_channel is None:
            raise RuntimeError("HTTP ingress requires the WebChannel adapter")

        if api_enabled and self.api_auth.generated:
            self.terminal_output(f"NanoCat API generated token: {self.api_auth.token}")
        logger.info(
            "NanoCat API authentication token fingerprint: {}",
            token_fingerprint(self.api_auth.token),
        )

        core_host = self.config.api.host if api_enabled else "127.0.0.1"
        core_port = self.config.api.port if api_enabled else 0
        if api_enabled and core_host not in {"127.0.0.1", "::1", "localhost"}:
            logger.critical(
                "NanoCat public API is exposed over plain HTTP; terminate TLS at a trusted proxy"
            )
        core_app = create_api_app(
            control=self.control,
            channel=self.web_channel,  # type: ignore[arg-type]
            broker=self.broker,
            authenticator=self.api_auth,
            config=self.config,
            configuration=self.configuration,
            activity_journal=self.activity_journal,
            artifact_registry=self.artifacts,
        )
        _, _, self.core_url = await self._start_server(
            core_app, host=core_host, port=core_port, name="api"
        )

        if web_enabled:
            web_config = self.config.channels.web
            if not self.web_auth.protected:
                if web_config.host not in {"127.0.0.1", "::1", "localhost"}:
                    logger.critical(
                        "NanoCat Web is exposed beyond loopback without password protection"
                    )
            elif web_config.host not in {"127.0.0.1", "::1", "localhost"}:
                logger.critical(
                    "NanoCat Web login is exposed over plain HTTP; terminate TLS at a trusted proxy"
                )
            web_app = create_web_app(
                core_api_url=self.core_url,
                api_token=lambda: self.api_auth.token,
                auth=self.web_auth,
                static_dir=self.static_dir,
            )
            _, _, self.web_url = await self._start_server(
                web_app,
                host=web_config.host,
                port=web_config.port,
                name="web",
            )
            if not self.web_auth.protected:
                logger.warning(
                    "NanoCat Web has no password; anyone able to reach {} has full access",
                    self.web_url,
                )
        self._write_endpoints()

    def _write_endpoints(self) -> None:
        """Publish bound listener URLs for local supervision and health probes."""
        if self.endpoint_file is None:
            return
        self.endpoint_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.endpoints, ensure_ascii=False, separators=(",", ":"))
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.endpoint_file.name}.",
            suffix=".tmp",
            dir=self.endpoint_file.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.endpoint_file)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    async def close(self) -> None:
        """Stop listeners in reverse order and release all stream subscribers."""
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            try:
                for server in reversed(self._servers):
                    server.should_exit = True
                try:
                    await self.broker.close()
                except Exception as exc:
                    logger.warning("Failed to close SSE broker ({})", type(exc).__name__)
                if self._tasks:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*reversed(self._tasks), return_exceptions=True),
                            timeout=7.0,
                        )
                    except asyncio.TimeoutError:
                        for task in self._tasks:
                            task.cancel()
                        stopped, still_pending = await asyncio.wait(
                            self._tasks,
                            timeout=2.0,
                        )
                        if stopped:
                            await asyncio.gather(*stopped, return_exceptions=True)
                        if still_pending:
                            logger.error(
                                "{} HTTP server task(s) did not stop within the hard deadline",
                                len(still_pending),
                            )
                    except Exception as exc:
                        logger.warning(
                            "Failed while joining HTTP server tasks ({})",
                            type(exc).__name__,
                        )
            finally:
                if self.endpoint_file is not None:
                    try:
                        self.endpoint_file.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "Failed to remove HTTP endpoint file ({})", type(exc).__name__
                        )
                for listener in self._listeners:
                    try:
                        listener.close()
                    except OSError:
                        pass
                try:
                    await self.artifacts.close()
                except Exception as exc:
                    logger.warning(
                        "Failed to close HTTP artifact registry ({})", type(exc).__name__
                    )
                self._servers.clear()
                self._tasks.clear()
                self._listeners.clear()
                self._closed = True

    async def wait(self) -> None:
        """Surface an unexpected listener exit to the runtime supervisor."""
        if not self._started:
            await self.start()
        if not self._tasks:
            return
        done, _ = await asyncio.wait(tuple(self._tasks), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
        if not self._closing and not self._closed:
            raise RuntimeError("NanoCat HTTP listener stopped unexpectedly")
