import sys

from . import config, llm_client

PROBE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "get_population",
            "description": "Return the population of a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def check_models(client):
    if not config.SUPPORTS_MODEL_LISTING:
        token = llm_client.vertex_access_token()
        print(f"  Vertex AI has no /models endpoint; checked credentials instead")
        print(f"  access token minted: {len(token)} chars")
        print(f"  project: {config.GOOGLE_CLOUD_PROJECT}  location: {config.GOOGLE_CLOUD_LOCATION}")
        return True
    names = [model.id for model in client.models.list()]
    print(f"  models available: {names[:20]}")
    if config.MODEL and config.MODEL not in names:
        print(f"  warning: CLEMSON_MODEL='{config.MODEL}' is not in the list above")
    return True


def check_chat(client):
    response = llm_client.chat(
        client,
        [{"role": "user", "content": "Reply with the single word: ready"}],
    )
    print(f"  reply: {response.choices[0].message.content!r}")
    return True


def check_tool_calling(client):
    response = llm_client.chat(
        client,
        [{"role": "user", "content": "What is the population of Clemson, South Carolina? Use the tool."}],
        tools=PROBE_TOOL,
    )
    calls = llm_client.parse_tool_calls(response.choices[0].message)
    if not calls:
        print("  no tool_calls returned; this server may not support function calling")
        print(f"  content was: {response.choices[0].message.content!r}")
        return False
    print(f"  tool call: {calls[0]['name']}({calls[0]['arguments']})")
    return True


CHECKS = [
    ("1. list models", check_models),
    ("2. plain chat completion", check_chat),
    ("3. tool calling", check_tool_calling),
]


def main():
    missing = config.missing_settings()
    if missing:
        print("FAIL: missing settings: " + ", ".join(missing))
        print("Copy .env.example to .env and fill it in.")
        return 1

    print(f"provider: {config.PROVIDER}")
    print(f"base_url: {config.API_BASE_URL}  (from {config.ENDPOINT_SOURCE})")
    print(f"model:    {config.MODEL}")
    if config.PROVIDER == "vertex":
        print("auth:     google application default credentials\n")
    else:
        print(f"api_key:  {'set, ' + str(len(config.API_KEY)) + ' chars' if config.API_KEY else 'NOT SET'}\n")

    client = llm_client.make_client()
    failures = 0
    for label, check in CHECKS:
        print(label)
        try:
            passed = check(client)
        except Exception as exc:
            passed = False
            print(f"  {type(exc).__name__}: {exc}")
        print("  PASS\n" if passed else "  FAIL\n")
        failures += 0 if passed else 1

    print("all checks passed" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
