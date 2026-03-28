#!/usr/bin/env python3
"""
Enclave OS — WASM Integration Test Suite
=========================================

Exercises all 9 exported functions of the wasm-example app (including complex
types via connect_call) plus schema/MCP endpoints, over an RA-TLS connection
to the SGX enclave.  Outputs a clean report with pass/fail per test.

Usage:
    python tests/test_wasm_functions.py [CWASM_PATH]

The script connects to 127.0.0.1:8443 by default (edit HOST/PORT below).
Requires only Python 3.6+ stdlib.
"""

import json
import os
import socket
import ssl
import sys
import time

# ── Configuration ──────────────────────────────────────────────────────────

HOST = os.environ.get("ENCLAVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ENCLAVE_PORT", "8443"))
APP_NAME = os.environ.get("APP_NAME", "test-app")
AUTH_TOKEN = os.environ.get("ENCLAVE_AUTH_TOKEN", "")

# ── HTTP/1.1 protocol helpers (enclave expects HTTP over TLS) ─────────────


def connect():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((HOST, PORT), timeout=120)
    return ctx.wrap_socket(raw, server_hostname=HOST)


def http_post(tls, path: str, body: bytes, auth_token: str = "") -> bytes:
    """Send an HTTP/1.1 POST and return the response body."""
    addr = f"{HOST}:{PORT}"
    req = f"POST {path} HTTP/1.1\r\nHost: {addr}\r\n"
    req += f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n"
    if auth_token:
        req += f"Authorization: Bearer {auth_token}\r\n"
    req += "\r\n"
    tls.sendall(req.encode() + body)

    # Read response headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = tls.recv(16384)
        if not chunk:
            raise ConnectionError("Connection closed reading headers")
        buf += chunk

    header_end = buf.index(b"\r\n\r\n")
    header_part = buf[:header_end].decode()
    body_so_far = buf[header_end + 4:]

    # Parse status
    status_line = header_part.split("\r\n")[0]
    status_code = int(status_line.split()[1])

    # Parse content-length
    content_length = 0
    for line in header_part.split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())

    # Read remaining body
    while len(body_so_far) < content_length:
        chunk = tls.recv(65536)
        if not chunk:
            break
        body_so_far += chunk

    if status_code != 200:
        raise RuntimeError(f"HTTP {status_code}: {body_so_far.decode(errors='replace')}")
    return body_so_far[:content_length]


def send_data(tls, payload: bytes) -> bytes:
    """POST /data with JSON payload (matches ratls.Client.SendData)."""
    return http_post(tls, "/data", payload, AUTH_TOKEN)


def wasm_load(tls, name: str, path: str):
    with open(path, "rb") as f:
        wasm_bytes = list(f.read())
    payload = json.dumps({"wasm_load": {"name": name, "bytes": wasm_bytes}}).encode()
    resp = send_data(tls, payload)
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


def wasm_call(tls, app: str, function: str, params=None, app_auth=None):
    payload = {"wasm_call": {"app": app, "function": function, "params": params or []}}
    if app_auth is not None:
        payload["wasm_call"]["app_auth"] = app_auth
    resp = send_data(tls, json.dumps(payload).encode())
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


def connect_call(tls, app: str, function: str, body: dict):
    payload = json.dumps(
        {"connect_call": {"app": app, "function": function, "body": body}}
    ).encode()
    resp = send_data(tls, payload)
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


def wasm_schema(tls, app: str):
    payload = json.dumps({"wasm_schema": {"app": app}}).encode()
    resp = send_data(tls, payload)
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


def mcp_tools(tls, app: str):
    payload = json.dumps({"mcp_tools": {"app": app}}).encode()
    resp = send_data(tls, payload)
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


def app_roles(tls, app: str, action: str, data=None, app_auth=None):
    """Send an app_roles request for role management."""
    payload = {"app_roles": {"app": app, "action": action}}
    if data is not None:
        payload["app_roles"]["data"] = data
    if app_auth is not None:
        payload["app_roles"]["app_auth"] = app_auth
    payload = json.dumps(payload).encode()
    resp = send_data(tls, payload)
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


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
    """Request the typed API schema — should contain auth-hello, role-hello and analyse-data with record/enum types."""
    r = wasm_schema(tls, APP_NAME)
    if isinstance(r, dict) and "error" not in r:
        schema = r.get("schema", r)
        funcs = schema.get("functions", [])
        func_names = [f.get("name") for f in funcs]
        has_analyse = "analyse-data" in func_names
        has_hello = "hello" in func_names
        has_auth_hello = "auth-hello" in func_names
        has_role_hello = "role-hello" in func_names
        ok = has_analyse and has_hello and has_auth_hello and has_role_hello and len(func_names) >= 9
        return ok, f"{len(func_names)} functions" if ok else json.dumps(r)[:120]
    return False, json.dumps(r)[:120]


def test_mcp_tools_endpoint(tls):
    """Request MCP tool manifest — analyse-data should have record/enum JSON schema."""
    r = mcp_tools(tls, APP_NAME)
    if isinstance(r, dict) and "error" not in r:
        manifest = r.get("manifest", r)
        tools = manifest.get("tools", [])
        tool_names = [t.get("name") for t in tools]
        has_analyse = "analyse-data" in tool_names
        ok = has_analyse and len(tool_names) >= 9
        if ok:
            # Verify the analyse-data tool has the config parameter with enum
            analyse_tool = next(t for t in tools if t["name"] == "analyse-data")
            schema = analyse_tool.get("inputSchema", {})
            props = schema.get("properties", {})
            has_config = "config" in props
            return has_config, f"{len(tool_names)} tools, config schema present"
        return ok, f"{len(tool_names)} tools" if tool_names else json.dumps(r)[:120]
    return False, json.dumps(r)[:120]


# ── Authentication tests ──────────────────────────────────────────────────


def wasm_load_with_permissions(tls, name: str, path: str, permissions: dict, docs: dict | None = None):
    """Load a WASM app with a permissions policy and/or WIT-derived auth docs."""
    with open(path, "rb") as f:
        wasm_bytes = list(f.read())
    payload: dict = {"wasm_load": {"name": name, "bytes": wasm_bytes}}
    if permissions:
        payload["wasm_load"]["permissions"] = permissions
    if docs:
        payload["wasm_load"]["docs"] = docs
    resp = send_data(tls, json.dumps(payload).encode())
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode(errors="replace")}


def test_auth_hello_no_auth(tls):
    """Call auth-hello without a token — should be rejected."""
    r = wasm_call(tls, APP_NAME + "-auth", "auth-hello")
    # Should get an error about authentication required
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "authentication required" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (no token)"
    return False, json.dumps(r)[:80]


def test_auth_hello_bad_token(tls):
    """Call auth-hello with an invalid token — should be rejected."""
    r = wasm_call(tls, APP_NAME + "-auth", "auth-hello", app_auth="invalid-token-value")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "auth failed" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (bad token)"
    return False, json.dumps(r)[:80]


def test_role_hello_no_auth(tls):
    """Call role-hello without a token — should be rejected."""
    r = wasm_call(tls, APP_NAME + "-auth", "role-hello")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "authentication required" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (no token)"
    return False, json.dumps(r)[:80]


def test_role_hello_bad_token(tls):
    """Call role-hello with an invalid token — should be rejected."""
    r = wasm_call(tls, APP_NAME + "-auth", "role-hello", app_auth="invalid-token-value")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "auth failed" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (bad token)"
    return False, json.dumps(r)[:80]


def test_hello_public_with_permissions(tls):
    """Call hello (public policy) on the auth-loaded app — should succeed without token."""
    r = wasm_call(tls, APP_NAME + "-auth", "hello")
    val = extract_return_value(r)
    ok = val == "Hello, World!"
    return ok, val if ok else json.dumps(r)[:80]


# ── Role management tests ─────────────────────────────────────────────────


def test_roles_no_auth(tls):
    """Request my_roles without a token — should be rejected."""
    r = app_roles(tls, APP_NAME + "-auth", "my_roles")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "authentication required" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (no token)"
    return False, json.dumps(r)[:80]


def test_roles_bad_token(tls):
    """Request my_roles with an invalid token — should be rejected."""
    r = app_roles(tls, APP_NAME + "-auth", "my_roles", app_auth="not-a-valid-token")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "auth failed" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (bad token)"
    return False, json.dumps(r)[:80]


def test_roles_no_permissions_app(tls):
    """Request roles on an app with no permissions — should fail."""
    r = app_roles(tls, APP_NAME, "my_roles")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "no permissions" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (no permissions policy)"
    return False, json.dumps(r)[:80]


def test_roles_list_users_no_auth(tls):
    """Request list_users without a token — should be rejected."""
    r = app_roles(tls, APP_NAME + "-auth", "list_users")
    if isinstance(r, dict):
        msg = r.get("message", "")
        if "authentication required" in msg.lower() or "error" in r.get("status", ""):
            return True, "correctly rejected (no token)"
    return False, json.dumps(r)[:80]


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

# Separate test list for auth-related tests (run after loading with permissions).
AUTH_TESTS = [
    ("auth-hello",      "Auth: No Token",    "Reject unauthenticated call",        test_auth_hello_no_auth),
    ("auth-hello",      "Auth: Bad Token",   "Reject invalid token",               test_auth_hello_bad_token),
    ("role-hello",      "Role: No Token",    "Reject unauthenticated call",        test_role_hello_no_auth),
    ("role-hello",      "Role: Bad Token",   "Reject invalid token",               test_role_hello_bad_token),
    ("hello",           "Auth: Public OK",   "Public fn works without token",      test_hello_public_with_permissions),
    ("app_roles",       "Roles: No Token",   "Reject unauthenticated role req",    test_roles_no_auth),
    ("app_roles",       "Roles: Bad Token",  "Reject invalid role token",          test_roles_bad_token),
    ("app_roles",       "Roles: No Perms",   "Reject on permissionless app",       test_roles_no_permissions_app),
    ("app_roles",       "Roles: List NoAuth","Reject list_users without auth",     test_roles_list_users_no_auth),
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

    # Load WASM app (no permissions — public access)
    print(f"  Loading {wasm_path} ...")
    t0 = time.time()
    load_result = wasm_load(tls, APP_NAME, wasm_path)
    load_time = time.time() - t0
    if "error" in (load_result if isinstance(load_result, dict) else {}):
        print(f"  [FAIL] Load failed: {load_result}")
        sys.exit(1)
    print(f"  Loaded in {load_time:.2f}s")
    print()

    # Run functional tests
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

    # Load the same app again with FIDO2 + WIT-derived auth annotations
    print("  Loading app with FIDO2 + @auth annotations (test-app-auth) ...")
    # OIDC/FIDO2 provider config (operator concern)
    auth_permissions = {
        "version": 1,
        "fido2": True,
    }
    # Auth policy from WIT @auth annotations (injected via docs)
    auth_docs = {
        "auth:__default__": "authenticated",
        "auth:hello": "public",
        "auth:auth-hello": "authenticated",
        "auth:role-hello": "role(hello-role)",
    }
    t0 = time.time()
    auth_load = wasm_load_with_permissions(
        tls, APP_NAME + "-auth", wasm_path, auth_permissions, docs=auth_docs
    )
    auth_load_time = time.time() - t0
    if "error" in (auth_load if isinstance(auth_load, dict) else {}):
        print(f"  [WARN] Auth load failed: {auth_load}")
        print("  Skipping auth tests.")
    else:
        print(f"  Loaded in {auth_load_time:.2f}s")
        print()

        # Run auth tests
        print(SEP)
        print(f"  {'#':>3}  {'Test':<20} {'Result':<8}{'Details'}")
        print(SEP)

        offset = len(TESTS)
        for j, (func_name, label, desc, test_fn) in enumerate(AUTH_TESTS, 1):
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

            idx = offset + j
            detail_str = str(detail)[:40]
            print(f"  {idx:>3}  {label:<20} {icon}  {detail_str}")
            results.append((idx, func_name, label, desc, ok, detail, elapsed))

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
