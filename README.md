# Identity Proxy

An anonymous A2A JSON-RPC proxy written in Python with uv. The proxy uses
`DefaultAzureCredential` to authenticate outbound requests to Microsoft Foundry.
Clients need neither Azure credentials nor an Azure SDK.

The default backend is:

```text
https://myaifoundry000010.services.ai.azure.com/api/projects/project01/agents/My-Sub03/endpoint/protocols/a2a
```

## Run

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), and an Azure identity
authorized to invoke the configured Foundry agent endpoint. For local development,
install Azure CLI and sign in with `az login`. A suitable least-privilege role is
**Foundry Agent Consumer** at the appropriate scope. No inbound login is required.

```powershell
uv sync --locked
az login
uv run --locked uvicorn app:app --host 127.0.0.1 --port 8000 --no-access-log
```

The proxy fetches `<backend>/agentCard/v1.0` with an Entra bearer token before
accepting requests. Startup fails if authentication, discovery, or card validation
fails. The card is cached until restart, so discovery requests do not contact Azure.
The shared token provider caches tokens and refreshes them as needed.

If port 8000 is occupied, use a free port and set the advertised URL to match:

```powershell
$env:IDENTITY_PROXY_PUBLIC_BASE_URL = 'http://localhost:62478'
uv run --locked uvicorn app:app --host 127.0.0.1 --port 62478 --no-access-log
```

## Anonymous Clients

Both examples discover the proxy's agent card, select JSONRPC 1.0, and send
`SendMessage` with `A2A-Version: 1.0`. They reject cross-origin card URLs and
redirects, and fail on HTTP or JSON-RPC errors. Neither sends an Authorization header.

```powershell
uv run --locked python examples/anonymous_client.py
./examples/Test-AnonymousA2A.ps1
```

The Python example uses only the standard library, so it also runs with plain
`python examples/anonymous_client.py`. The PowerShell example requires 7.2+.

For the alternate port:

```powershell
uv run --locked python examples/anonymous_client.py --base-url http://localhost:62478
./examples/Test-AnonymousA2A.ps1 -BaseUrl http://localhost:62478
```

The default message in both clients is exactly:

> I need a loan of 250,000 euros to buy a 160-square-metre house in Espoo, Finland. What is your offer?

Override it with Python `--message` or PowerShell `-Message`. Responses are printed
as JSON, including task state and identifiers. A successful transport request may
return a task requiring more input, not a completed loan offer. For a follow-up,
PowerShell supports `-ContextId` and `-TaskId` from the previous response. Both
examples request a blocking response (`returnImmediately: false`) and do not poll
or automatically retry a message. Their default request timeout is 300 seconds.

## Configuration

Edit [config.toml](config.toml), or set `IDENTITY_PROXY_CONFIG` to another TOML file.
Every field also accepts an `IDENTITY_PROXY_<UPPERCASE_FIELD_NAME>` environment
override, for example `IDENTITY_PROXY_BACKEND_A2A_URL`.

| Field | Default |
| --- | --- |
| `backend_a2a_url` | Foundry endpoint above; absolute HTTPS URL |
| `agent_card_path` | `/agentCard/v1.0`, appended to the backend URL |
| `token_scope` | `https://ai.azure.com/.default` |
| `public_base_url` | `http://localhost:8000` |
| `connect_timeout_seconds` | `10` |
| `read_timeout_seconds` | `300`, upstream I/O timeout |
| `startup_timeout_seconds` | `120`, also bounds request token acquisition |
| `credential_process_timeout_seconds` | `60`, Azure CLI/PowerShell credential process timeout |

`public_base_url` is the externally reachable origin plus any reverse-proxy mount
prefix. A hosting reverse proxy must strip that prefix before forwarding requests.
Changing it does not change Uvicorn's bind address or port. Caller-supplied Host
headers never determine the advertised endpoint.

## Endpoints And Behavior

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/.well-known/agent-card.json` | Cached anonymous agent card |
| GET | `/a2a/.well-known/agent-card.json` | Same cached card |
| GET | `/a2a/agentCard/v1.0` | Same cached card |
| POST | `/a2a` or `/a2a/` | Forward to the configured backend |
| GET | `/healthz` | Local readiness after successful startup; not a live backend probe |

The card must expose JSONRPC 1.0 at the configured backend. Matching JSONRPC
interfaces are retained and rewritten to `<public_base_url>/a2a`; HTTP+JSON and
other destinations are not advertised. Authentication requirements, signatures
invalidated by rewriting, and extended-card discovery are removed. Agent metadata,
skills, and other capabilities are preserved.

The proxy forwards request bytes, query parameters, A2A headers, response status,
raw response bytes, and end-to-end headers. It replaces caller Authorization with
its own bearer token and strips cookies, API keys, proxy authentication, and
hop-by-hop headers. Backend cookies are not shared between callers. Redirects are
not followed and POST requests are not automatically retried.

Streaming responses are forwarded incrementally with upstream cleanup on client
disconnect. This does not add streaming support to an agent: the configured live
agent currently advertises `streaming: false`. Initial connection failures become
502 and initial upstream timeouts become 504. A failure after response headers
have been sent terminates the stream; the status cannot then be changed.

## Security Boundaries

**This is deliberately anonymous and defaults to loopback binding.** Anyone who
can reach it can invoke the backend using the proxy identity, spend its quota,
and potentially access tasks or contexts created by other callers. There is no
caller authentication, authorization, per-caller task isolation, rate limiting,
or message-size policy. Do not expose it publicly without adding those controls.

The backend destination is fixed by operator configuration, TLS verification is
enabled, and tokens are never returned to clients. Do not put credentials in TOML
files. Avoid enabling detailed HTTP logging that could expose messages or tokens.
The example startup commands disable access logging. Treat agent output as
untrusted content; the examples display it without executing it.

## Verify

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Automated tests use fake credentials and a local transport; they require no Azure
account and incur no model usage. Live example invocations do use the configured
agent and can incur costs. Never assert a particular loan offer as a deterministic
test result.

### Live Validation

Verified on 2026-09-16 against the default backend:

- Authenticated startup card fetch succeeded; anonymous discovery exposed only
	rewritten proxy JSON-RPC interfaces and no authentication requirements.
- Both anonymous examples sent the exact default loan message and received
	`TASK_STATE_COMPLETED` with an indicative, non-binding conditional offer:
	12-month Euribor plus a 1.20% bank margin. This is agent output, not a verified
	financial commitment or a guarantee of future responses.
- The live card advertised streaming as disabled. Incremental delivery and
	disconnect cleanup were instead verified with local socket tests.

The local validation server used `http://localhost:62478`, since ports 8000 and
8001 were already occupied. Defaults remain unchanged for future runs.