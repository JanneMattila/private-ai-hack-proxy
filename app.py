import asyncio
import copy
import json
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from config import Settings, load_settings

logger = logging.getLogger("identity_proxy")
HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


def filtered_headers(headers: list[tuple[bytes, bytes]], *, outbound: bool):
    blocked = HOP_HEADERS.copy()
    for name, value in headers:
        if name.lower() == b"connection":
            blocked.update(part.strip().lower() for part in value.split(b","))
    if outbound:
        blocked.update({b"host", b"authorization", b"cookie", b"api-key"})
    else:
        blocked.update({b"set-cookie", b"authorization"})
    return [(name, value) for name, value in headers if name.lower() not in blocked]


def rewrite_card(card: dict, settings: Settings) -> dict:
    if not isinstance(card, dict) or not isinstance(card.get("name"), str):
        raise ValueError("Backend returned an invalid agent card")
    result = copy.deepcopy(card)
    interfaces = result.get("supportedInterfaces", [])
    supported = [
        interface
        for interface in interfaces
        if isinstance(interface, dict)
        and interface.get("protocolBinding") == "JSONRPC"
        and interface.get("url", "").rstrip("/") == settings.backend_a2a_url.rstrip("/")
    ]
    if not supported or not any(
        item.get("protocolVersion") == "1.0" for item in supported
    ):
        raise ValueError(
            "Backend card must expose JSONRPC 1.0 at the configured endpoint"
        )
    for interface in supported:
        interface["url"] = settings.public_a2a_url
    result["supportedInterfaces"] = supported
    if "url" in result:
        result["url"] = settings.public_a2a_url
    result.pop("additionalInterfaces", None)
    result.pop("signatures", None)
    for node in [result, *result.get("skills", []), *supported]:
        for key in (
            "securitySchemes",
            "securityRequirements",
            "security",
            "authentication",
        ):
            node.pop(key, None)
    result.get("capabilities", {}).pop("extendedAgentCard", None)
    result.pop("supportsAuthenticatedExtendedCard", None)
    result.pop("supportsExtendedAgentCard", None)
    return result


class ProxyResponse(StreamingResponse):
    def __init__(self, upstream: httpx.Response):
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code)
        self.upstream = upstream
        self.raw_headers = filtered_headers(upstream.headers.raw, outbound=False)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


def create_app(
    settings: Settings | None = None,
    *,
    credential_factory: Callable[[], AsyncTokenCredential] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configuration = settings or load_settings()
        async with AsyncExitStack() as stack:
            credential = (
                credential_factory()
                if credential_factory
                else DefaultAzureCredential(
                    process_timeout=configuration.credential_process_timeout_seconds
                )
            )
            stack.push_async_callback(credential.close)
            token_provider = get_bearer_token_provider(
                credential, configuration.token_scope
            )
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=transport,
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        configuration.read_timeout_seconds,
                        connect=configuration.connect_timeout_seconds,
                    ),
                )
            )
            try:
                async with asyncio.timeout(configuration.startup_timeout_seconds):
                    token = await token_provider()
                    upstream = await client.get(
                        configuration.card_url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                        },
                    )
                    upstream.raise_for_status()
                    card = rewrite_card(upstream.json(), configuration)
            except httpx.HTTPStatusError as error:
                raise RuntimeError(
                    f"Agent card fetch failed (HTTP {error.response.status_code}). "
                    "Check the card path and Foundry endpoint permissions."
                ) from None
            except (AzureError, httpx.HTTPError, TimeoutError, ValueError) as error:
                raise RuntimeError(
                    f"Agent card initialization failed ({type(error).__name__}). "
                    "Check Azure login, configuration, and network connectivity."
                ) from None
            application.state.settings = configuration
            application.state.client = client
            application.state.token_provider = token_provider
            application.state.card = json.dumps(card, ensure_ascii=False).encode(
                "utf-8"
            )
            logger.info("Agent card cached; anonymous A2A endpoint ready")
            yield

    application = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @application.get("/.well-known/agent-card.json")
    @application.get("/a2a/.well-known/agent-card.json")
    @application.get("/a2a/agentCard/v1.0")
    async def agent_card():
        return Response(application.state.card, media_type="application/json")

    @application.get("/healthz")
    async def health():
        return {"status": "ready"}

    @application.post("/a2a")
    @application.post("/a2a/")
    async def proxy(request: Request):
        configuration = application.state.settings
        try:
            async with asyncio.timeout(configuration.startup_timeout_seconds):
                token = await application.state.token_provider()
        except (AzureError, TimeoutError):
            logger.warning("Backend credential acquisition failed")
            return JSONResponse(
                {"error": "Backend authentication unavailable"}, status_code=502
            )
        headers = filtered_headers(request.headers.raw, outbound=True)
        headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
        url = httpx.URL(configuration.backend_a2a_url).copy_with(
            query=request.scope["query_string"] or None
        )
        upstream_request = httpx.Request(
            request.method,
            url,
            headers=headers,
            content=request.stream(),
        )
        try:
            upstream = await application.state.client.send(
                upstream_request, stream=True
            )
        except httpx.TimeoutException:
            return JSONResponse({"error": "Backend timed out"}, status_code=504)
        except httpx.HTTPError:
            logger.warning("Backend connection failed")
            return JSONResponse({"error": "Backend connection failed"}, status_code=502)
        return ProxyResponse(upstream)

    return application


app = create_app()
