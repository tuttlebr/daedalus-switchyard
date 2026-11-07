"""Run from a real client: python3 verify_client.py http://HOST:30260."""
import json
import socket
import sys
import uuid
from urllib.request import Request, urlopen

BASE = sys.argv[1].rstrip("/")


def request(path, body=None):
    req = Request(BASE + path, data=json.dumps(body).encode() if body else None,
                  headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    with urlopen(req, timeout=120) as response:
        data = json.load(response)
        selected = response.headers.get("x-model-router-selected-model")
    return data, selected


def passed(test, selected=None):
    print(json.dumps(dict(source=socket.gethostname(), base_url=BASE,
                          test=test, selected=selected, passed=True)), flush=True)


assert request("/health")[0]["status"] == "ok"
passed("health")
assert len(request("/v1/models")[0]["data"]) == 7
passed("seven_models")
for path, body in [
    ("/v1/responses", {"model": "daedalus", "input": "Hello! Reply with only CLIENT_OK.", "max_output_tokens": 8192}),
    ("/v1/chat/completions", {"model": "daedalus/medium", "messages": [{"role": "user", "content": "Reply with only CLIENT_OK."}], "max_tokens": 8192}),
    ("/v1/messages", {"model": "daedalus/low", "messages": [{"role": "user", "content": "Reply with only CLIENT_OK."}], "max_tokens": 8192}),
]:
    data, selected = request(path, {**body, "stream": False})
    assert "CLIENT_OK" in json.dumps(data), data
    passed(path, selected)

tool = {"type": "function", "function": {
    "name": "read_marker", "description": "Read the requested record marker.",
    "parameters": {"type": "object", "properties": {
        "record": {"type": "object", "properties": {"key": {"type": "string"}},
                   "required": ["key"], "additionalProperties": False}},
        "required": ["record"], "additionalProperties": False}}}
messages = [{"role": "user", "content": 'Call read_marker with record object {"key":"probe"}. When its result arrives, return only that marker.'}]


def chat(model, choice):
    data, _ = request("/v1/chat/completions", dict(model=model, messages=messages,
        tools=[tool], tool_choice=choice, max_tokens=8192, stream=False))
    return data["choices"][0]["message"]


first = chat("daedalus/low", {"type": "function", "function": {"name": "read_marker"}})
call = first["tool_calls"][0]
assert json.loads(call["function"]["arguments"]) == {"record": {"key": "probe"}}
marker = uuid.uuid4().hex
messages += [first, {"role": "tool", "tool_call_id": call["id"], "content": marker}]
final = chat("daedalus/xhigh", "none")
assert final["content"].strip() == marker
passed("nested tool call and DeepSeek-to-Kimi continuation")
