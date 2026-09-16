import asyncio
import copy
import gzip
import socket
import time

import httpx
import pytest
import uvicorn
from azure.core.credentials import AccessToken
from starlette.requests import ClientDisconnect

from app import ProxyResponse, create_app, rewrite_card
from config import Settings, load_settings

SETTINGS = Settings(backend_a2a_url="https://backend.example/a2a")
CARD = {
    "name": "Test agent",
    "supportedInterfaces": [
        {
            "url": SETTINGS.backend_a2a_url,
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        },
        {
            "url": SETTINGS.backend_a2a_url,
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "0.3",
        },
    ],
    "securitySchemes": {"entra": {}},
    "securityRequirements": [{"entra": []}],
    "signatures": [{"signature": "invalid-after-rewrite"}],
    "skills": [{"id": "offer", "securityRequirements": [{"entra": []}]}],
    "capabilities": {"streaming": True, "extendedAgentCard": True},
    "customMetadata": {"preserve": True},
}


class FakeCredential:
    def __init__(self):
        self.scopes = []
        self.closed = False

    async def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return AccessToken("test-token", int(time.time()) + 3600)

    async def close(self):
        self.closed = True


async def test_startup_fetches_authenticated_card_and_serves_cached_anonymous_card():
    requests = []
    credential = FakeCredential()

    def backend(request):
        requests.append(request)
        assert request.url == SETTINGS.card_url
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json=copy.deepcopy(CARD))

    application = create_app(
        SETTINGS,
        credential_factory=lambda: credential,
        transport=httpx.MockTransport(backend),
    )
    async with application.router.lifespan_context(application):
        assert len(requests) == 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application),
            base_url="http://untrusted.example",
        ) as client:
            for path in (
                "/.well-known/agent-card.json",
                "/a2a/.well-known/agent-card.json",
                "/a2a/agentCard/v1.0",
            ):
                response = await client.get(path)
                assert response.status_code == 200
                card = response.json()
                assert card["supportedInterfaces"] == [
                    {
                        "url": "http://localhost:8000/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ]
                assert "securityRequirements" not in card
                assert "securitySchemes" not in card
                assert "signatures" not in card
                assert "securityRequirements" not in card["skills"][0]
                assert "extendedAgentCard" not in card["capabilities"]
                assert card["capabilities"]["streaming"]
                assert card["customMetadata"] == {"preserve": True}
            assert (await client.get("/healthz")).json() == {"status": "ready"}
            assert len(requests) == 1
    assert credential.closed
    assert credential.scopes == [(SETTINGS.token_scope,)]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_startup_failure_closes_credential(status):
    credential = FakeCredential()
    application = create_app(
        SETTINGS,
        credential_factory=lambda: credential,
        transport=httpx.MockTransport(lambda request: httpx.Response(status)),
    )
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        async with application.router.lifespan_context(application):
            pytest.fail("Startup must not succeed")
    assert credential.closed


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("status", [200, 400, 401, 403, 429, 500])
async def test_forwarding_preserves_wire_data_and_replaces_auth(status):
    payload = b'{"jsonrpc":"2.0", "id":"unique", "method":"SendMessage"}'
    content = gzip.compress(b'{"result": {"text": "unchanged"}}')
    streams = []
    sent = []
    credential = FakeCredential()

    async def backend(request):
        if request.method == "GET":
            return httpx.Response(
                200, json=CARD, headers={"set-cookie": "secret=value"}
            )
        sent.append(request)
        assert await request.aread() == payload
        assert request.url == SETTINGS.backend_a2a_url + "?tag=1&tag=2&text=a%2Fb"
        assert request.headers.get_list("authorization") == ["Bearer test-token"]
        assert request.headers["host"] == "backend.example"
        assert request.headers["a2a-version"] == "1.0"
        assert "cookie" not in request.headers
        assert "api-key" not in request.headers
        assert "x-hop" not in request.headers
        assert "proxy-authorization" not in request.headers
        stream = TrackedStream([content])
        streams.append(stream)
        return httpx.Response(
            status,
            stream=stream,
            headers=[
                ("content-type", "application/json"),
                ("content-encoding", "gzip"),
                ("content-length", str(len(content))),
                ("x-repeat", "first"),
                ("x-repeat", "second"),
                ("connection", "x-private"),
                ("x-private", "remove"),
                ("set-cookie", "do-not-share=secret"),
            ],
        )

    application = create_app(
        SETTINGS,
        credential_factory=lambda: credential,
        transport=httpx.MockTransport(backend),
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://proxy"
        ) as client:
            for path in ("/a2a", "/a2a/"):
                async with client.stream(
                    "POST",
                    path + "?tag=1&tag=2&text=a%2Fb",
                    content=payload,
                    headers={
                        "Authorization": "Bearer caller-token",
                        "Cookie": "client=value",
                        "api-key": "caller-key",
                        "Proxy-Authorization": "Basic ignored",
                        "A2A-Version": "1.0",
                        "Connection": "x-hop",
                        "x-hop": "remove",
                    },
                ) as response:
                    assert response.status_code == status
                    assert (
                        b"".join([chunk async for chunk in response.aiter_raw()])
                        == content
                    )
                    assert response.headers["content-encoding"] == "gzip"
                    assert response.headers["content-length"] == str(len(content))
                    assert response.headers.get_list("x-repeat") == ["first", "second"]
                    assert "x-private" not in response.headers
                    assert "set-cookie" not in response.headers
    assert len(sent) == 2
    assert all(stream.closed for stream in streams)
    assert credential.closed
    assert credential.scopes == [(SETTINGS.token_scope,)]


@pytest.mark.parametrize(
    "failure,expected",
    [
        (httpx.ConnectError("unreachable"), 502),
        (httpx.ReadTimeout("timeout"), 504),
    ],
)
async def test_gateway_failures(failure, expected):
    def backend(request):
        if request.method == "GET":
            return httpx.Response(200, json=CARD)
        raise failure

    application = create_app(
        SETTINGS,
        credential_factory=FakeCredential,
        transport=httpx.MockTransport(backend),
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://proxy"
        ) as client:
            response = await client.post("/a2a", json={})
            assert response.status_code == expected
            assert "test-token" not in response.text


async def test_redirect_is_not_followed():
    paths = []

    def backend(request):
        paths.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json=CARD)
        return httpx.Response(
            307,
            headers={"location": "https://other.example/steal"},
            stream=TrackedStream([]),
        )

    application = create_app(
        SETTINGS,
        credential_factory=FakeCredential,
        transport=httpx.MockTransport(backend),
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://proxy"
        ) as client:
            assert (await client.post("/a2a", json={})).status_code == 307
    assert paths == [SETTINGS.card_url, SETTINGS.backend_a2a_url]


async def test_stream_cleanup_on_downstream_failure():
    stream = TrackedStream([b'data: {"first":true}\n\n', b"data: done\n\n"])
    response = ProxyResponse(httpx.Response(200, stream=stream))

    async def send(message):
        raise OSError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert stream.closed


@pytest.mark.parametrize("disconnect", [False, True])
async def test_stream_is_incremental_over_socket_and_closes(disconnect):
    release_second = asyncio.Event()
    stream_closed = asyncio.Event()
    server_ready = asyncio.Event()

    class ControlledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\n\n"
            await release_second.wait()
            yield b"data: second\n\n"

        async def aclose(self):
            stream_closed.set()

    class NotifyingServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            server_ready.set()

    def backend(request):
        if request.method == "GET":
            return httpx.Response(200, json=CARD)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ControlledStream(),
        )

    application = create_app(
        SETTINGS,
        credential_factory=FakeCredential,
        transport=httpx.MockTransport(backend),
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = NotifyingServer(
            uvicorn.Config(application, access_log=False, log_level="error")
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                await server_ready.wait()
                async with httpx.AsyncClient(trust_env=False) as client:
                    async with client.stream(
                        "POST", f"http://127.0.0.1:{port}/a2a", content=b"{}"
                    ) as response:
                        assert response.status_code == 200
                        chunks = response.aiter_raw()
                        assert await anext(chunks) == b"data: first\n\n"
                        assert not stream_closed.is_set()
                        if not disconnect:
                            release_second.set()
                            assert await anext(chunks) == b"data: second\n\n"
                            assert [chunk async for chunk in chunks] == []
                    await stream_closed.wait()
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=10)


async def test_expired_credential_token_is_refreshed():
    class ExpiringCredential(FakeCredential):
        async def get_token(self, *scopes, **kwargs):
            self.scopes.append(scopes)
            first_call = len(self.scopes) == 1
            return AccessToken(
                "expired-token" if first_call else "fresh-token",
                int(time.time()) + (-1 if first_call else 3600),
            )

    credential = ExpiringCredential()

    def backend(request):
        if request.method == "GET":
            assert request.headers["authorization"] == "Bearer expired-token"
            return httpx.Response(200, json=CARD)
        assert request.headers["authorization"] == "Bearer fresh-token"
        return httpx.Response(200, stream=TrackedStream([b"{}"]))

    application = create_app(
        SETTINGS,
        credential_factory=lambda: credential,
        transport=httpx.MockTransport(backend),
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://proxy"
        ) as client:
            assert (await client.post("/a2a", json={})).status_code == 200
    assert credential.scopes == [(SETTINGS.token_scope,)] * 2
    assert credential.closed


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend_a2a_url": "http://backend.example/a2a"},
        {"backend_a2a_url": "https://user:password@backend.example/a2a"},
        {"backend_a2a_url": "https://backend.example/a2a?version=1"},
        {"agent_card_path": "/../secret"},
        {"agent_card_path": "//other.example/card"},
        {"read_timeout_seconds": 0},
        {"startup_timeout_seconds": float("nan")},
        {"public_base_url": "not-a-url"},
    ],
)
def test_invalid_settings(overrides):
    with pytest.raises(ValueError):
        Settings(**({"backend_a2a_url": SETTINGS.backend_a2a_url} | overrides))


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("IDENTITY_PROXY_PUBLIC_BASE_URL", "https://public.example")
    monkeypatch.setenv("IDENTITY_PROXY_READ_TIMEOUT_SECONDS", "45")
    settings = load_settings()
    assert settings.public_a2a_url == "https://public.example/a2a"
    assert settings.read_timeout_seconds == 45
    assert settings.card_url.endswith("/endpoint/protocols/a2a/agentCard/v1.0")


def test_card_does_not_mutate_original_or_advertise_other_destinations():
    card = copy.deepcopy(CARD)
    card["supportedInterfaces"].append(
        {
            "url": "https://other.example/rpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    )
    result = rewrite_card(card, SETTINGS)
    assert len(result["supportedInterfaces"]) == 1
    assert card["supportedInterfaces"][0]["url"] == SETTINGS.backend_a2a_url
    assert "securitySchemes" in card
