"""The only file that talks to a model or parses a tool call.

Two backends, one shape. `config` decides which; nothing above this line knows
there is a choice, which is what keeps `agent.py` free of backend details.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from openai import OpenAI

from . import config


def vertex_access_token() -> str:
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def make_client() -> OpenAI:
    missing = config.missing_settings()
    if missing:
        raise RuntimeError(
            "Missing settings: " + ", ".join(missing) + ". Copy .env.example to .env and fill it in."
        )
    key = vertex_access_token() if config.PROVIDER == "vertex" else (config.API_KEY or "not-needed")
    return OpenAI(
        base_url=config.API_BASE_URL,
        api_key=key,
        timeout=config.MODEL_TIMEOUT_S,
        max_retries=config.MODEL_MAX_RETRIES,
    )


def chat(
    client: OpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> Any:
    request: dict[str, Any] = {
        "model": model or config.MODEL,
        "messages": messages,
        "temperature": config.MODEL_TEMPERATURE,
    }
    if tools:
        request["tools"] = tools
        request["tool_choice"] = "auto"
    return client.chat.completions.create(**request)


def parse_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in getattr(message, "tool_calls", None) or []:
        raw = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = {"__unparsable_arguments__": raw}
        calls.append({"id": call.id, "name": call.function.name, "arguments": arguments})
    return calls


def message_to_dict(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if getattr(message, "tool_calls", None):
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                },
            }
            for call in message.tool_calls
        ]
    return payload


LOOPBACK_SETTINGS = ("PROVIDER", "API_BASE_URL", "MODEL", "GEMINI_MODEL", "MODEL_TIMEOUT_S", "MODEL_MAX_RETRIES")
FAILING_PROMPT = "answer with a server error"
PROBE_SPEC = {
    "type": "function",
    "function": {
        "name": "list_datasets",
        "description": "loopback probe",
        "parameters": {"type": "object", "properties": {"verbose": {"type": "boolean"}}},
    },
}
LOOPBACK_COMPLETION = {
    "id": "chatcmpl-loopback",
    "object": "chat.completion",
    "created": 0,
    "model": "loopback",
    "choices": [{
        "index": 0,
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-loopback",
                "type": "function",
                "function": {"name": "list_datasets", "arguments": "{\"verbose\": true}"},
            }],
        },
    }],
}


def _loopback(seen: list[dict[str, Any]]) -> Any:
    """A local endpoint that records every request body and answers like a chat server."""
    import http.server

    class Endpoint(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
            failing = (body.get("messages") or [{}])[-1].get("content") == FAILING_PROMPT
            payload = json.dumps({"error": {"message": "loopback"}} if failing else LOOPBACK_COMPLETION).encode()
            self.send_response(500 if failing else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if failing:
                self.send_header("retry-after-ms", "1")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, message: str, *args: Any) -> None:
            return None

    return http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)


def _attempts(seen: list[dict[str, Any]], client: OpenAI) -> int:
    """How many requests one failing call sent before the client gave up."""
    from openai import InternalServerError

    before = len(seen)
    try:
        chat(client, [{"role": "user", "content": FAILING_PROMPT}])
    except InternalServerError:
        pass
    return sum(item["body"]["messages"][-1]["content"] == FAILING_PROMPT for item in seen[before:])


def _self_check() -> int:
    import threading

    from . import verify

    print("LLM CLIENT -- the real SDK client, sending to a loopback endpoint\n")
    module = sys.modules[__name__]
    seen: list[dict[str, Any]] = []
    server = _loopback(seen)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = {name: getattr(config, name) for name in LOOPBACK_SETTINGS}
    minted = module.vertex_access_token
    try:
        config.PROVIDER, config.GEMINI_MODEL, config.MODEL = "vertex", "loopback", "google/loopback"
        config.API_BASE_URL = f"http://127.0.0.1:{server.server_address[1]}/v1"
        module.vertex_access_token = lambda: "loopback-token"
        client = make_client()
        bounds = (client.timeout, client.max_retries)
        response = chat(client, [{"role": "user", "content": "probe"}], tools=[PROBE_SPEC])
        with_tools = seen[-1]
        calls = parse_tool_calls(response.choices[0].message)
        chat(client, [{"role": "user", "content": "probe"}])
        without_tools = seen[-1]["body"]
        declared_attempts = _attempts(seen, client)
        config.MODEL_TIMEOUT_S, config.MODEL_MAX_RETRIES = 7.5, 0
        moved = make_client()
        moved_bounds = (moved.timeout, moved.max_retries)
        moved_attempts = _attempts(seen, moved)
    finally:
        module.vertex_access_token = minted
        for name, value in saved.items():
            setattr(config, name, value)
        server.shutdown()
        server.server_close()
    body = with_tools["body"]
    print(f"  request with tools: temperature {body.get('temperature')!r}, model {body.get('model')!r}, "
          f"tool_choice {body.get('tool_choice')!r}, path {with_tools['path']}")
    print(f"  client bounds: timeout {bounds[0]!r}, max_retries {bounds[1]!r}; a failing call sent "
          f"{declared_attempts} request(s), and {moved_attempts} with max_retries set to 0")
    checks = [
        ("every request carries the configured temperature",
         body.get("temperature") == config.MODEL_TEMPERATURE
         and without_tools.get("temperature") == config.MODEL_TEMPERATURE),
        ("the Vertex branch sends the token it minted",
         with_tools["authorization"] == "Bearer loopback-token"),
        ("the request names the configured model, its tools, and tool_choice auto",
         body.get("model") == "google/loopback" and body.get("tool_choice") == "auto"
         and [item["function"]["name"] for item in body.get("tools") or []] == ["list_datasets"]
         and with_tools["path"].endswith("/chat/completions")),
        ("a request with no tools sends no tools and no tool_choice",
         "tools" not in without_tools and "tool_choice" not in without_tools),
        ("the client takes its timeout and retry bound from config, not from the SDK defaults",
         bounds == (config.MODEL_TIMEOUT_S, config.MODEL_MAX_RETRIES) and moved_bounds == (7.5, 0)),
        ("a failing call is sent once plus the configured retries, and no more",
         declared_attempts == 1 + config.MODEL_MAX_RETRIES and moved_attempts == 1),
        ("a tool call the SDK parsed comes back with its arguments decoded",
         calls == [{"id": "call-loopback", "name": "list_datasets", "arguments": {"verbose": True}}]),
        ("the settings and the token source are put back",
         {name: getattr(config, name) for name in LOOPBACK_SETTINGS} == saved
         and module.vertex_access_token is minted),
    ]
    checks += verify.discipline_checks(module)
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else 2)
