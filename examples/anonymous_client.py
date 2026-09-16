import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

DEFAULT_MESSAGE = (
    "I need a loan of 250,000 euros to buy a 160-square-metre house in Espoo, "
    "Finland. What is your offer?"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description="Send an anonymous A2A message")
    parser.add_argument("--base-url", default="http://localhost:62478")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--timeout", type=float, default=300)
    arguments = parser.parse_args()
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(
        arguments.base_url.rstrip("/") + "/.well-known/agent-card.json",
        timeout=arguments.timeout,
    ) as response:
        card = json.load(response)
    interface = next(
        item
        for item in card["supportedInterfaces"]
        if item["protocolBinding"] == "JSONRPC" and item["protocolVersion"] == "1.0"
    )
    endpoint = urlsplit(interface["url"])
    origin = urlsplit(arguments.base_url)
    if (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc):
        raise ValueError("Agent card points outside the anonymous proxy")
    request_id = str(uuid.uuid4())
    parameters = {
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": [{"text": arguments.message}],
        },
        "configuration": {"returnImmediately": False},
    }
    if interface.get("tenant"):
        parameters["tenant"] = interface["tenant"]
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": parameters,
    }
    request = urllib.request.Request(
        interface["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
    )
    with opener.open(request, timeout=arguments.timeout) as response:
        result = json.load(response)
    if result.get("jsonrpc") != "2.0" or result.get("id") != request_id:
        raise ValueError("Invalid JSON-RPC response envelope")
    if "error" in result:
        raise RuntimeError(json.dumps(result["error"], ensure_ascii=False))
    print(json.dumps(result["result"], indent=2, ensure_ascii=False))
    task = result["result"].get("task", {})
    state = task.get("status", {}).get("state", "")
    if state in {"TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED"}:
        raise RuntimeError(f"Agent task ended with {state}")


if __name__ == "__main__":
    try:
        main()
    except (
        urllib.error.URLError,
        ValueError,
        KeyError,
        StopIteration,
        RuntimeError,
    ) as error:
        print(f"A2A request failed: {error}", file=sys.stderr)
        sys.exit(1)
