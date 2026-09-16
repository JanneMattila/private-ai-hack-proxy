import json

import httpx
import pytest

pytest.importorskip("a2a", reason="Install a2a-sdk to run the SDK example tests")

from a2a.utils.errors import A2AError

from examples import anonymous_client2

BASE_URL = "http://localhost:62478"


def agent_card(url=BASE_URL + "/a2a"):
    return {
        "name": "SDK test agent",
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False},
    }


@pytest.mark.parametrize("failure", [False, True])
async def test_sdk_sends_anonymous_v1_message(failure, capsys):
    requests = []

    def backend(request):
        requests.append(request)
        assert "authorization" not in request.headers
        if request.method == "GET":
            assert str(request.url) == BASE_URL + "/.well-known/agent-card.json"
            return httpx.Response(200, json=agent_card())
        assert str(request.url) == BASE_URL + "/a2a"
        assert request.headers["a2a-version"] == "1.0"
        payload = json.loads(request.content)
        assert payload["method"] == "SendMessage"
        message = payload["params"]["message"]
        assert message["role"] == "ROLE_USER"
        assert message["messageId"]
        assert message["parts"] == [{"text": anonymous_client2.DEFAULT_MESSAGE}]
        assert not payload["params"].get("configuration", {}).get("returnImmediately")
        result = {"jsonrpc": "2.0", "id": payload["id"]}
        if failure:
            result["error"] = {"code": -32603, "message": "Backend error"}
        else:
            result["result"] = {
                "task": {
                    "id": "task-id",
                    "contextId": "context-id",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                }
            }
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        if failure:
            with pytest.raises(A2AError):
                await anonymous_client2.send_message(
                    client, BASE_URL, anonymous_client2.DEFAULT_MESSAGE
                )
        else:
            await anonymous_client2.send_message(
                client, BASE_URL, anonymous_client2.DEFAULT_MESSAGE
            )
            assert json.loads(capsys.readouterr().out)["task"]["id"] == "task-id"
    assert len(requests) == 2


async def test_sdk_rejects_cross_origin_card():
    requests = []

    def backend(request):
        requests.append(request)
        return httpx.Response(200, json=agent_card("https://other.example/a2a"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        with pytest.raises(ValueError, match="outside"):
            await anonymous_client2.send_message(client, BASE_URL, "test")
    assert len(requests) == 1
