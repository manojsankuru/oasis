import json

from openai import OpenAI

from . import config


def vertex_access_token():
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def make_client():
    missing = config.missing_settings()
    if missing:
        raise RuntimeError(
            "Missing settings: " + ", ".join(missing) + ". Copy .env.example to .env and fill it in."
        )
    if config.PROVIDER == "vertex":
        return OpenAI(base_url=config.API_BASE_URL, api_key=vertex_access_token())
    return OpenAI(base_url=config.API_BASE_URL, api_key=config.API_KEY or "not-needed")


def chat(client, messages, tools=None, model=None):
    request = {"model": model or config.MODEL, "messages": messages}
    if tools:
        request["tools"] = tools
        request["tool_choice"] = "auto"
    return client.chat.completions.create(**request)


def parse_tool_calls(message):
    calls = []
    for call in getattr(message, "tool_calls", None) or []:
        raw = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = {"__unparsable_arguments__": raw}
        calls.append({"id": call.id, "name": call.function.name, "arguments": arguments})
    return calls


def message_to_dict(message):
    payload = {"role": "assistant", "content": message.content or ""}
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
