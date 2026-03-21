#!/usr/bin/env python3
"""Offline stand-in for ``claude -p`` used by the test suite.

Emulates the CLI's JSON / stream-json result shape, generates schema-conforming
structured output, and fails loudly if the adapter ever passes an API key,
uses --bare, or leaves built-in tools enabled.
"""
import json
import os
import sys
import uuid

BOOL_FLAGS = {"-p", "--print", "--verbose", "--strict-mcp-config", "--no-session-persistence",
              "--bare", "--dangerously-skip-permissions", "--include-partial-messages"}


def parse_args(argv):
    opts = {"prompt": None, "flags": set()}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in BOOL_FLAGS:
            opts["flags"].add(a)
            i += 1
            continue
        if a.startswith("--"):
            opts[a[2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        opts["prompt"] = a
        i += 1
    return opts


def instance(schema, root):
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return instance(root.get("$defs", {}).get(name, {}), root)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if sub.get("type") != "null":
                return instance(sub, root)
        return None
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        t = non_null[0] if non_null else "null"
    if t == "object":
        return {k: instance(v, root) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return []
    if t == "string":
        return "fake"
    if t == "integer":
        return int(schema.get("minimum", 0))
    if t == "number":
        return float(schema.get("minimum", 0))
    if t == "boolean":
        return False
    return None


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")


def main():
    opts = parse_args(sys.argv[1:])
    prompt = opts["prompt"] if opts["prompt"] is not None else sys.stdin.read()
    model = opts.get("model", "unknown")
    session = str(uuid.uuid4())
    stream = opts.get("output-format") == "stream-json"

    def fail(text):
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text,
              "session_id": session, "num_turns": 1, "duration_ms": 1, "total_cost_usd": 0.0,
              "usage": {}, "modelUsage": {}})
        sys.exit(1)

    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        fail("LEAKED_API_KEY: an API credential reached the CLI environment")
    if "--bare" in opts["flags"]:
        fail("BARE_MODE_USED: subscription login is disabled in bare mode")
    if "FORCE_LIMIT" in prompt:
        fail("You've hit your session limit \u00b7 resets 1:30pm (America/Los_Angeles)")
    if "FORCE_LOGOUT" in prompt:
        # The shape the real CLI answered with on 2026-10-02, subtype and all.
        emit({"type": "result", "subtype": "success", "is_error": True,
              "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
              "session_id": session, "num_turns": 1, "duration_ms": 1, "total_cost_usd": 0.0,
              "usage": {}, "modelUsage": {}})
        sys.exit(1)

    markers = [f"FAKE RESPONSE model={model}"]
    markers.append("BUILTIN_TOOLS_DISABLED" if opts.get("tools") == "" else "BUILTIN_TOOLS_ENABLED")
    if "--strict-mcp-config" in opts["flags"]:
        markers.append("STRICT_MCP")
    if opts.get("allowedTools"):
        markers.append(f"ALLOWED={opts['allowedTools']}")
    markers.append(f"SYSTEM_LEN={len(opts.get('system-prompt', ''))}")
    markers.append(f"PROMPT_HEAD={prompt[:60]!r}")
    text = " ".join(markers)

    structured = None
    if opts.get("json-schema"):
        schema = json.loads(opts["json-schema"])
        structured = instance(schema, schema)
        text = json.dumps(structured)

    usage = {"input_tokens": 12, "output_tokens": 7, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50,
             "output_tokens_details": {"thinking_tokens": 0}}
    result = {"type": "result", "subtype": "success", "is_error": False, "result": text,
              "session_id": session, "num_turns": 2 if opts.get("mcp-config") else 1, "duration_ms": 5,
              "total_cost_usd": 0.001, "usage": usage,
              "modelUsage": {model: {"inputTokens": 12, "outputTokens": 7, "costUSD": 0.001}},
              "permission_denials": [], "stop_reason": "end_turn"}
    if structured is not None:
        result["structured_output"] = structured

    if stream:
        emit({"type": "system", "subtype": "init", "session_id": session, "model": model,
              "tools": [], "mcp_servers": [{"name": "ta", "status": "connected"}] if opts.get("mcp-config") else []})
        if opts.get("mcp-config"):
            first_tool = (opts.get("allowedTools") or "mcp__ta__unknown").split(",")[0]
            emit({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_fake_1", "name": first_tool, "input": {"symbol": "FAKE"}}]},
                "session_id": session})
            emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_fake_1",
                 "content": [{"type": "text", "text": "fake tool output " * 80}]}]}, "session_id": session})
        emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
              "session_id": session})
        emit(result)
    else:
        emit(result)


if __name__ == "__main__":
    main()
