# /// script
# requires-python = ">=3.12"
# dependencies = ["a2a-sdk>=1.1.1,<2", "httpx>=0.28.1,<1"]
# ///

import argparse
import asyncio
import sys
import uuid
from urllib.parse import urlsplit

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.client.errors import A2AClientError
from a2a.types import AgentInterface, Message, Part, Role, SendMessageRequest, TaskState
from a2a.utils.errors import A2AError
from google.protobuf.json_format import MessageToJson

DEFAULT_MESSAGE = (
    "I need a loan of 250,000 euros to buy a 160-square-metre house in Espoo, "
    "Finland. What is your offer?"
)


async def send_message(http_client: httpx.AsyncClient, base_url: str, text: str):
    card = await A2ACardResolver(http_client, base_url).get_agent_card()
    interface = next(
        (
            item
            for item in card.supported_interfaces
            if item.protocol_binding == "JSONRPC" and item.protocol_version == "1.0"
        ),
        None,
    )
    if interface is None:
        raise ValueError("Agent card has no JSONRPC 1.0 interface")
    endpoint = urlsplit(interface.url)
    origin = urlsplit(base_url)
    if (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc):
        raise ValueError("Agent card points outside the anonymous proxy")
    selected_interface = AgentInterface()
    selected_interface.CopyFrom(interface)
    del card.supported_interfaces[:]
    card.supported_interfaces.append(selected_interface)
    config = ClientConfig(
        httpx_client=http_client,
        streaming=False,
        polling=False,
        supported_protocol_bindings=["JSONRPC"],
    )
    request = SendMessageRequest(
        message=Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
        )
    )
    async with await create_client(card, client_config=config) as client:
        async for response in client.send_message(request):
            print(MessageToJson(response, ensure_ascii=False))
            if response.HasField("task") and response.task.status.state in {
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_REJECTED,
                TaskState.TASK_STATE_CANCELED,
            }:
                state = TaskState.Name(response.task.status.state)
                raise RuntimeError(f"Agent task ended with {state}")


async def main():
    parser = argparse.ArgumentParser(
        description="Send an anonymous message with A2A SDK"
    )
    parser.add_argument("--base-url", default="http://localhost:62478")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--timeout", type=float, default=300)
    arguments = parser.parse_args()
    async with httpx.AsyncClient(
        timeout=arguments.timeout, follow_redirects=False
    ) as http_client:
        await send_message(http_client, arguments.base_url, arguments.message)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (
        A2AClientError,
        A2AError,
        httpx.HTTPError,
        ValueError,
        RuntimeError,
    ) as error:
        print(f"A2A request failed: {error}", file=sys.stderr)
        sys.exit(1)
