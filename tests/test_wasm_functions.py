#!/usr/bin/env python3
"""
Enclave OS — WASM Integration Test Suite
=========================================

Exercises all 7 exported functions of the wasm-example app (including complex
types via connect_call) plus schema/MCP endpoints, over an RA-TLS connection
to the SGX enclave.  Outputs a clean report with pass/fail per test.

Usage:
    python tests/test_wasm_functions.py [CWASM_PATH]

The script connects to 127.0.0.1:8443 by default (edit HOST/PORT below).
Requires only Python 3.6+ stdlib.
"""

import json
import socket
import ssl
import struct
import sys
import time

# ── Configuration ──────────────────────────────────────────────────────────

HOST = "127.0.0.1"
PORT = 8443
APP_NAME = "test-app"

# ── Wire protocol helpers ─────────────────────────────────────────────────


def encode_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def decode_frame(data: bytes):
    if len(data) < 4:
        return None, data
    length = struct.unpack(">I", data[:4])[0]
    if len(data) < 4 + length:
        return None, data
    return data[4 : 4 + length], data[4 + length :]


def make_request(variant: str, value=None) -> bytes:
    return json.dumps(variant if value is None else {variant: value}).encode()


def connect():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((HOST, PORT), timeout=60)
    return ctx.wrap_socket(raw, server_hostname=HOST)


def send_recv(tls, payload: bytes) -> bytes:
    tls.sendall(encode_frame(payload))
    buf = b""
    while True:
        chunk = tls.recv(16384)
        if not chunk:
            raise ConnectionError("Connection closed by server")
        buf += chunk
        result, _ = decode_frame(buf)
        if result is not None:
            return result


def wasm_load(tls, name: str, path: str):
    with open(path, "rb") as f:
        wasm_bytes = list(f.read())
    inner = json.dumps({"wasm_load": {"name": name, "bytes": wasm_bytes}}).encode()
    resp = json.loads(send_recv(tls, make_request("Data", list(inner))))
    if "Data" in resp:
        return json.loads(bytes(resp["Data"]))
    if "Error" in resp:
        return {"error": bytes(resp["Error"]).decode(errors="replace")}
    return resp


def wasm_call(tls, app: str, function: str, params=None):
    inner = json.dumps(
        {"wasm_call": {"app": app, "function": function, "params": params or []}}
    ).encode()
    resp = json.loads(send_recv(tls, make_request("Data", list(inner))))
    if "Data" in resp:
        raw = bytes(resp["Data"])
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw.decode(errors="replace")}
    if "Error" in resp:
        return {"error": bytes(resp["Error"]).decode(errors="replace")}
    return resp


def connect_call(tls, app: str, function: str, body: dict):
    inner = json.dumps(
        {"connect_call": {"app": app, "function": function, "body": body}}
    ).encode()
    resp = json.loads(send_recv(tls, make_request("Data", list(inner))))
    if "Data" in resp:
        raw = bytes(resp["Data"])
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw.decode(errors="replace")}
    if "Error" in resp:
        return {"error": bytes(resp["Error"]).decode(errors="replace")}
    return resp


def wasm_schema(tls, app: str):
    inner = json.dumps({"wasm_schema": {"app": app}}).encode()
    resp = json.loads(send_recv(tls, make_request("Data", list(inner))))
    if "Data" in resp:
        raw = bytes(resp["Data"])
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw.decode(errors="replace")}
    if "Error" in resp:
        return {"error": bytes(resp["Error"]).decode(errors="replace")}
    return resp


def mcp_tools(tls, app: str):
    inner = json.dumps({"mcp_tools": {"app": app}}).encode()
    resp = json.loads(send_recv(tls, make_request("Data", list(inner))))
    if "Data" in resp:
        raw = bytes(resp["Data"])
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw.decode(errors="replace")}
    if "Error" in resp:
        return {"error": bytes(resp["Error"]).decode(errors="replace")}
    return resp


# ── Test definitions ──────────────────────────────────────────────────────


def extract_return_value(result):
    """Extract the value from a wasm_call result.

    Expected shape: {"status": "ok", "returns": [{"type": "...", "value": ...}]}
    """
    if isinstance(result, dict) and result.get("status") == "ok":
        returns = result.get("returns", [])
        if returns and isinstance(returns, list):
            return returns[0].get("value")
    return None


def test_hello(tls):
    r = wasm_call(tls, APP_NAME, "hello")
    val = extract_return_value(r)
    ok = val == "Hello, World!"
    return ok, val if ok else json.dumps(r)


def test_get_random(tls):
    r = wasm_call(tls, APP_NAME, "get-random")
    val = extract_return_value(r)
    ok = isinstance(val, int) and 1 <= val <= 100
    return ok, str(val) if ok else json.dumps(r)


def test_get_time(tls):
    r = wasm_call(tls, APP_NAME, "get-time")
    val = extract_return_value(r)
    if val and isinstance(val, str) and "." in val:
        try:
            ts = float(val.split(".")[0])
            ok = 1_704_067_200 < ts < 1_893_456_000
            return ok, val if ok else f"timestamp out of range: {val}"
        except ValueError:
            pass
    return False, json.dumps(r)


def test_kv_store(tls):
    r = wasm_call(
        tls, APP_NAME, "kv-store",
        [
            {"type": "string", "value": "greeting"},
            {"type": "string", "value": "Hello from WASM in SGX!"},
        ],
    )
    val = extract_return_value(r)
    ok = val == "stored: greeting"
    return ok, "greeting = 'Hello from WASM in SGX!'" if ok else json.dumps(r)


def test_kv_read(tls):
    r = wasm_call(
        tls, APP_NAME, "kv-read",
        [{"type": "string", "value": "greeting"}],
    )
    val = extract_return_value(r)
    ok = val == "Hello from WASM in SGX!"
    return ok, val if ok else json.dumps(r)


def test_fetch_headlines(tls):
    r = wasm_call(tls, APP_NAME, "fetch-headlines")
    val = extract_return_value(r)
    if val and isinstance(val, str):
        lines = [l for l in val.strip().split("\n") if l.strip()]
        ok = len(lines) >= 2 and lines[0].startswith("1.")
        preview = lines[0][:72] if lines else ""
        return ok, f"{len(lines)} headlines — {preview}" if ok else val
    return False, json.dumps(r)


# ── Complex-type tests (analyse-data: record + enum + option) ─────────────


def test_analyse_data_json(tls):
    """Call analyse-data with JSON output, include-stats, and a label."""
    r = connect_call(tls, APP_NAME, "analyse-data", {
        "values": [1.0, 2.5, 3.0, 4.5, 5.0],
        "config": {
            "include-stats": True,
            "label": "sample",
            "format": "json",
        },
    })
    val = extract_return_value(r)
    if val and isinstance(val, str):
        try:
            obj = json.loads(val)
            checks = (
                obj.get("label") == "sample"
                and obj.get("count") == 5
                and "mean" in obj
                and "min" in obj
                and "max" in obj
            )
            return checks, f"label={obj.get('label')} count={obj.get('count')}" if checks else val
        except json.JSONDecodeError:
            pass
    return False, json.dumps(r)


def test_analyse_data_csv(tls):
    """Call analyse-data with CSV output and no label (option<string> = null)."""
    r = connect_call(tls, APP_NAME, "analyse-data", {
        "values": [10.0, 20.0],
        "config": {
            "include-stats": False,
            "label": None,
            "format": "csv",
        },
    })
    val = extract_return_value(r)
    if val and isinstance(val, str):
        lines = val.strip().split("\n")
        ok = len(lines) == 2 and lines[0].startswith("label,count,sum")
        return ok, lines[1] if ok else val
    return False, json.dumps(r)


def test_analyse_data_text(tls):
    """Call analyse-data with text output and stats enabled."""
    r = connect_call(tls, APP_NAME, "analyse-data", {
        "values": [100.0],
        "config": {
            "include-stats": True,
            "label": "single",
            "format": "text",
        },
    })
    val = extract_return_value(r)
    if val and isinstance(val, str):
        ok = "single:" in val and "count=1" in val and "mean=" in val
        return ok, val[:72] if ok else val
    return False, json.dumps(r)


def test_analyse_data_empty(tls):
    """Call analyse-data with empty values — should return error response."""
    r = connect_call(tls, APP_NAME, "analyse-data", {
        "values": [],
        "config": {
            "include-stats": False,
            "label": None,
            "format": "json",
        },
    })
    val = extract_return_value(r)
    if val and isinstance(val, str):
        ok = "error" in val.lower() or "empty" in val.lower()
        return ok, val if ok else val
    return False, json.dumps(r)


# ── Schema / MCP tests ────────────────────────────────────────────────────


def test_wasm_schema_endpoint(tls):
    """Request the typed API schema — should contain analyse-data with record/enum types."""
    r = wasm_schema(tls, APP_NAME)
    if isinstance(r, dict) and "error" not in r:
        funcs = r.get("functions", [])
        func_names = [f.get("name") for f in funcs]
        has_analyse = "analyse-data" in func_names
        has_hello = "hello" in func_names
        ok = has_analyse and has_hello and len(func_names) >= 7
        return ok, f"{len(func_names)} functions" if ok else json.dumps(r)[:120]
    return False, json.dumps(r)[:120]


def test_mcp_tools_endpoint(tls):
    """Request MCP tool manifest — analyse-data should have record/enum JSON schema."""
    r = mcp_tools(tls, APP_NAME)
    if isinstance(r, dict) and "error" not in r:
        tools = r.get("tools", [])
        tool_names = [t.get("name") for t in tools]
        has_analyse = "analyse-data" in tool_names
        ok = has_analyse and len(tool_names) >= 7
        if ok:
            # Verify the analyse-data tool has the config parameter with enum
            analyse_tool = next(t for t in tools if t["name"] == "analyse-data")
            schema = analyse_tool.get("inputSchema", {})
            props = schema.get("properties", {})
            has_config = "config" in props
            return has_config, f"{len(tool_names)} tools, config schema present"
        return ok, f"{len(tool_names)} tools" if tool_names else json.dumps(r)[:120]
    return False, json.dumps(r)[:120]


# ── Runner ────────────────────────────────────────────────────────────────

TESTS = [
    ("hello",           "Hello World",       "Smoke test (no imports)",            test_hello),
    ("get-random",      "Random Number",     "wasi:random -> RDRAND",              test_get_random),
    ("get-time",        "Wall Clock",        "wasi:clocks -> OCALL",               test_get_time),
    ("kv-store",        "KV Store (write)",  "wasi:filesystem -> sealed KV",       test_kv_store),
    ("kv-read",         "KV Store (read)",   "wasi:filesystem -> sealed KV",       test_kv_read),
    ("fetch-headlines", "HTTPS Egress",      "privasys:enclave-os/https -> TLS",   test_fetch_headlines),
    ("analyse-data",    "Analyse (JSON)",    "connect_call: record+enum → json",   test_analyse_data_json),
    ("analyse-data",    "Analyse (CSV)",     "connect_call: option<null> → csv",   test_analyse_data_csv),
    ("analyse-data",    "Analyse (Text)",    "connect_call: record+enum → text",   test_analyse_data_text),
    ("analyse-data",    "Analyse (Empty)",   "connect_call: edge case (empty)",    test_analyse_data_empty),
    ("wasm_schema",     "API Schema",        "wasm_schema: typed export list",     test_wasm_schema_endpoint),
    ("mcp_tools",       "MCP Tools",         "mcp_tools: JSON Schema manifest",    test_mcp_tools_endpoint),
]


def main():
    wasm_path = sys.argv[1] if len(sys.argv) > 1 else "wasm_example.cwasm"

    print()
    print("=" * 64)
    print("  Enclave OS (Mini) - WASM Integration Test Suite")
    print("=" * 64)
    print()

    # Connect
    print(f"  Connecting to {HOST}:{PORT} ...")
    try:
        tls = connect()
    except Exception as e:
        print(f"  [FAIL] Connection failed: {e}")
        sys.exit(1)
    print(f"  Connected: {tls.version()}, {tls.cipher()[0]}")
    print()

    # Load WASM app
    print(f"  Loading {wasm_path} ...")
    t0 = time.time()
    load_result = wasm_load(tls, APP_NAME, wasm_path)
    load_time = time.time() - t0
    if "error" in (load_result if isinstance(load_result, dict) else {}):
        print(f"  [FAIL] Load failed: {load_result}")
        sys.exit(1)
    print(f"  Loaded in {load_time:.2f}s")
    print()

    # Run tests
    SEP = "  " + "-" * 60
    print(SEP)
    print(f"  {'#':>3}  {'Test':<20} {'Result':<8}{'Details'}")
    print(SEP)

    passed = 0
    failed = 0
    results = []

    for i, (func_name, label, desc, test_fn) in enumerate(TESTS, 1):
        t0 = time.time()
        try:
            ok, detail = test_fn(tls)
        except Exception as e:
            ok, detail = False, f"Exception: {e}"
        elapsed = time.time() - t0

        icon = "\u2714" if ok else "\u274c"
        if ok:
            passed += 1
        else:
            failed += 1

        detail_str = str(detail)[:40]
        print(f"  {i:>3}  {label:<20} {icon}  {detail_str}")
        results.append((i, func_name, label, desc, ok, detail, elapsed))

    print(SEP)
    print()

    # Summary
    total = passed + failed
    if failed == 0:
        print(f"  \u2714 Result: ALL {total} TESTS PASSED")
    else:
        print(f"  \u274c Result: {failed}/{total} TESTS FAILED")
    print()

    # Detailed results
    print("  Detailed Results")
    print("  " + "-" * 40)
    for i, func_name, label, desc, ok, detail, elapsed in results:
        icon = "\u2714" if ok else "\u274c"
        print(f"  {icon} {i}. {label} ({func_name}) - {elapsed:.2f}s")
        print(f"       WASI: {desc}")
        print(f"       Output: {detail}")
        print()

    tls.close()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
