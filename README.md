# WASM App Example

A WebAssembly Component that exercises all major capabilities of the
[Enclave OS (Mini)](https://github.com/Privasys/enclave-os-mini) WASM runtime.
Designed as a reference implementation and integration test for validating
new releases.

**Seven exported functions** cover every host import available to WASM apps
running inside an SGX enclave:

| # | Function | WASI Interface | What it tests |
|---|----------|---------------|----------------|
| 1 | `hello` | *(none)* | Smoke test — pure guest code, no host imports |
| 2 | `get-random` | `wasi:random` | Hardware RNG (RDRAND inside SGX) |
| 3 | `get-time` | `wasi:clocks/wall-clock` | Wall clock via OCALL |
| 4 | `kv-store` | `wasi:filesystem` | Write to sealed KV store |
| 5 | `kv-read` | `wasi:filesystem` | Read from sealed KV store |
| 6 | `fetch-headlines` | `privasys:enclave-os/https` | HTTPS egress (TLS inside SGX) |
| 7 | `analyse-data` | *(none)* | Records, enums, options — MCP tool demo |

---

## Quick Start

### Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Rust | stable 1.82+ | `rustup update stable` |
| WASI target | — | `rustup target add wasm32-wasip2` |
| cargo-component | latest | `cargo install cargo-component` |

### Build

```bash
cargo component build --release
```

Output: `target/wasm32-wasip1/release/wasm_example.wasm`

### Pre-compile (optional)

For faster load times you can AOT-compile to `.cwasm` using Wasmtime:

```bash
wasmtime compile target/wasm32-wasip1/release/wasm_example.wasm -o wasm_example.cwasm
```

### Run the integration tests

With the enclave running (see [Deployment](#deployment)):

```bash
python tests/test_wasm_functions.py wasm_example.cwasm
```

```
================================================================
  Enclave OS (Mini) - WASM Integration Test Suite
================================================================

  Connecting to 127.0.0.1:8443 ...
  Connected: TLSv1.3, TLS_AES_256_GCM_SHA384

  Loading wasm_example.cwasm ...
  Loaded in 0.12s

  ------------------------------------------------------------
    #  Test                 Result  Details
  ------------------------------------------------------------
    1  Hello World          ✔  Hello, World!
    2  Random Number        ✔  45
    3  Wall Clock           ✔  1772364814.000000000
    4  KV Store (write)     ✔  greeting = 'Hello from WASM in SGX!'
    5  KV Store (read)      ✔  Hello from WASM in SGX!
    6  HTTPS Egress         ✔  9 headlines — 1. Guides d'achat
  ------------------------------------------------------------

  ✔ Result: ALL 6 TESTS PASSED
```

---

## How it works

### WASM inside SGX

Enclave OS (Mini) embeds a [Wasmtime](https://wasmtime.dev/) runtime inside
the SGX enclave. WASM apps are:

1. **Compiled externally** (this project) into a Component Model `.wasm`
2. **Pre-compiled** to `.cwasm` (AOT) with Wasmtime — no Cranelift inside SGX
3. **Loaded dynamically** at runtime via [`wasm_load`](#1-load--wasm_load) over the RA-TLS wire protocol — the enclave starts empty, no apps are compiled in
4. **Attested automatically** — the SHA-256 of the WASM bytecode is embedded
   in the config Merkle tree and in every RA-TLS certificate
   (OID `1.3.6.1.4.1.65230.2.3`)
5. **Called over RA-TLS** via JSON [`wasm_call`](#2-call--wasm_call) envelopes
6. **Executed statelessly** — each call gets a fresh instance with a
   10 million instruction fuel budget
7. **Managed at runtime** — apps can be [`listed`](#3-list--wasm_list) and [`unloaded`](#7-unload--wasm_unload) without restarting the enclave
8. **MCP-ready** — the enclave can emit [MCP tool manifests](#6-mcp-tools--mcp_tools) derived from WIT types and `///` doc comments

### Wire protocol

All communication happens over a single **RA-TLS** connection using
**length-delimited JSON frames** (4-byte big-endian length prefix).

**Call parameters** are typed JSON values:

```json
{"type": "string", "value": "hello"}
{"type": "u32", "value": 42}
```

---

## Dynamic WASM Loading

The enclave starts **empty** — no WASM apps are compiled in. Apps are loaded,
called, and managed entirely at runtime over the RA-TLS wire protocol.

### Lifecycle

```
 ┌───────────────┐     RA-TLS     ┌───────────────────────────┐
 │  Client       │◄──────────────►│  SGX Enclave              │
 │  (ra-tls-cli) │                │                           │
 │               │  wasm_load ──► │  1. SHA-256 code hash     │
 │               │                │  2. Deserialize (AOT)     │
 │               │                │  3. Introspect exports    │
 │               │  ◄── loaded    │  4. Register app          │
 │               │                │  5. Embed hash in cert    │
 │               │  wasm_call ──► │                           │
 │               │  ◄── result    │  Fresh instance per call  │
 │               │                │  (stateless, fuel-limited)│
 └───────────────┘                └───────────────────────────┘
```

### 1. Load — `wasm_load`

Send the pre-compiled `.cwasm` bytecode to the enclave. The enclave:

1. **Hashes** the bytecode (SHA-256) — this becomes the app's identity
2. **Deserializes** the AOT artifact (no Cranelift compilation inside SGX)
3. **Introspects** the component to discover all exported functions
4. **Generates** an AES-256 encryption key via RDRAND for the app's KV store
   *(or accepts a caller-supplied key — see [BYOK](#bring-your-own-key))*
5. **Registers** the app under the given name
6. **Re-derives** the RA-TLS certificate with the new code hash in the
   config Merkle tree (OID `1.3.6.1.4.1.65230.2.3`)

**Request:**

```json
{
  "wasm_load": {
    "name": "test-app",
    "bytes": [0, 97, 115, 109, ...],
    "hostname": "app.enclave.example.com"
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | App identifier — used in subsequent `wasm_call` requests |
| `bytes` | yes | Raw `.cwasm` bytecode as a JSON integer array |
| `hostname` | no | SNI hostname for a dedicated per-app TLS certificate (defaults to `name`) |
| `encryption_key` | no | Hex-encoded 32-byte AES-256 key for KV store ([BYOK](#bring-your-own-key)) |
| `mcp_enabled` | no | Whether to expose this app as an MCP tool server (defaults to `true`) |

**Response:**

```json
{
  "status": "loaded",
  "app": {
    "name": "test-app",
    "hostname": "app.enclave.example.com",
    "code_hash": "a1b2c3d4...64hex",
    "key_source": "generated",
    "exports": [
      {"name": "hello", "param_count": 0, "result_count": 1},
      {"name": "kv-store", "param_count": 2, "result_count": 1}
    ]
  }
}
```

The `code_hash` is the SHA-256 of the loaded bytecode. Remote clients can
verify this value in the RA-TLS certificate to confirm exactly what code
is running inside the enclave.

### 2. Call — `wasm_call`

Invoke an exported function on a loaded app. Each call creates a **fresh
WASM instance** (stateless execution) with a fuel budget of 10 million
instructions.

**Request:**

```json
{
  "wasm_call": {
    "app": "test-app",
    "function": "hello",
    "params": []
  }
}
```

**Response:**

```json
{
  "status": "ok",
  "returns": [{"type": "string", "value": "Hello, World!"}]
}
```

**With parameters:**

```json
{
  "wasm_call": {
    "app": "test-app",
    "function": "kv-store",
    "params": [
      {"type": "string", "value": "greeting"},
      {"type": "string", "value": "Hello from WASM in SGX!"}
    ]
  }
}
```

### 3. List — `wasm_list`

Query all currently loaded apps with metadata.

**Request:**

```json
{"wasm_list": {}}
```

**Response:**

```json
{
  "status": "apps",
  "apps": [
    {
      "name": "test-app",
      "hostname": "app.enclave.example.com",
      "code_hash": "a1b2c3d4...64hex",
      "key_source": "generated",
      "exports": [
        {"name": "hello", "param_count": 0, "result_count": 1},
        {"name": "get-random", "param_count": 0, "result_count": 1},
        {"name": "get-time", "param_count": 0, "result_count": 1},
        {"name": "kv-store", "param_count": 2, "result_count": 1},
        {"name": "kv-read", "param_count": 1, "result_count": 1},
        {"name": "fetch-headlines", "param_count": 0, "result_count": 1}
      ]
    }
  ]
}
```

### 5. Schema — `wasm_schema`

Retrieve the typed API schema for a loaded app. Includes WIT type
information, function signatures, and `///` doc comment descriptions.

**Request:**

```json
{"wasm_schema": {"app": "test-app"}}
```

**Response (abbreviated):**

```json
{
  "status": "schema",
  "schema": {
    "app_name": "test-app",
    "mcp_enabled": true,
    "interfaces": [
      {
        "name": "default",
        "functions": [
          {
            "name": "hello",
            "description": "Smoke-test export — returns a greeting with no host imports.",
            "params": [],
            "results": [{"name": "", "ty": "string"}]
          }
        ]
      }
    ]
  }
}
```

### 6. MCP Tools — `mcp_tools`

Retrieve the MCP (Model Context Protocol) tool manifest for a loaded app.
Each exported function is described as an MCP tool with a name, description
(from `///` doc comments in the WIT definition), and a JSON Schema input
derived from WIT parameter types.

Requires `mcp_enabled` to be `true` (the default).

**Request:**

```json
{"mcp_tools": {"app": "test-app"}}
```

**Response (abbreviated):**

```json
{
  "status": "mcp_tools",
  "manifest": {
    "name": "test-app",
    "tools": [
      {
        "name": "hello",
        "description": "Smoke-test export — returns a greeting with no host imports.",
        "inputSchema": {
          "type": "object",
          "properties": {},
          "required": []
        }
      },
      {
        "name": "analyse-data",
        "description": "Analyse a list of floating-point values with configurable output.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "values": {
              "type": "array",
              "items": {"type": "number"}
            },
            "config": {
              "type": "object",
              "properties": {
                "include-stats": {"type": "boolean"},
                "label": {"type": ["string", "null"]},
                "format": {
                  "type": "string",
                  "enum": ["text", "json", "csv"]
                }
              },
              "required": ["include-stats", "label", "format"]
            }
          },
          "required": ["values", "config"]
        }
      }
    ]
  }
}
```

The `///` doc comments flow from the `.wit` file → `package-docs` WASM
custom section → enclave runtime introspection → MCP manifest. No glue
code or separate tool definitions are needed.

### 7. Unload — `wasm_unload`

Remove an app from the enclave. The in-memory encryption key is destroyed,
making any KV data written with a generated key **permanently unrecoverable**.

**Request:**

```json
{"wasm_unload": {"name": "test-app"}}
```

**Response:**

```json
{"status": "unloaded", "name": "test-app"}
```

### Bring Your Own Key

By default, the enclave generates a random AES-256 encryption key per app
via RDRAND. The key lives only in enclave memory — if the app is unloaded,
the key and any data encrypted with it are gone forever.

For persistent data across app reloads, supply an `encryption_key` during
`wasm_load`:

```json
{
  "wasm_load": {
    "name": "my-app",
    "bytes": [0, 97, 115, 109, ...],
    "encryption_key": "a1b2c3d4e5f6...64hex"
  }
}
```

The key must be exactly 32 bytes (64 hex characters). The enclave will use
this key for all KV store operations for this app. The `key_source` field
in the response will report `"byok"` instead of `"generated"`.

### Python example (complete)

```python
import json, socket, ssl, struct

def frame(data):
    return struct.pack(">I", len(data)) + data

# Connect via RA-TLS
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE  # or verify against RA-TLS CA
sock = ctx.wrap_socket(
    socket.create_connection(("127.0.0.1", 8443)),
    server_hostname="127.0.0.1",
)

# Load the WASM app
with open("wasm_example.cwasm", "rb") as f:
    wasm_bytes = list(f.read())

load_req = json.dumps({
    "wasm_load": {"name": "my-app", "bytes": wasm_bytes}
}).encode()
sock.sendall(frame(json.dumps({"Data": list(load_req)}).encode()))

# Read response (simplified — production code should handle partial reads)
buf = sock.recv(65536)
length = struct.unpack(">I", buf[:4])[0]
resp = json.loads(buf[4:4+length])
inner = json.loads(bytes(resp["Data"]))
print("Loaded:", json.dumps(inner, indent=2))

# Call a function
call_req = json.dumps({
    "wasm_call": {"app": "my-app", "function": "hello", "params": []}
}).encode()
sock.sendall(frame(json.dumps({"Data": list(call_req)}).encode()))

buf = sock.recv(65536)
length = struct.unpack(">I", buf[:4])[0]
resp = json.loads(buf[4:4+length])
result = json.loads(bytes(resp["Data"]))
print("Result:", json.dumps(result, indent=2))
# {"status": "ok", "returns": [{"type": "string", "value": "Hello, World!"}]}

sock.close()
```

---

## Test API reference

### 1. `hello` — Smoke test

```json
{"wasm_call": {"app": "test-app", "function": "hello", "params": []}}
```

Returns: `"Hello, World!"`

No host imports. Validates the end-to-end path: RA-TLS → wire protocol →
WASM instantiation → guest code → response.

### 2. `get-random` — Hardware RNG

```json
{"wasm_call": {"app": "test-app", "function": "get-random", "params": []}}
```

Returns: integer in `[1, 100]`

Uses `wasi:random/random.get-random-bytes(4)` → maps to RDRAND inside SGX.
Different value on each call.

### 3. `get-time` — Wall clock

```json
{"wasm_call": {"app": "test-app", "function": "get-time", "params": []}}
```

Returns: `"<seconds>.<nanoseconds>"` (e.g. `"1772364814.000000000"`)

Uses `wasi:clocks/wall-clock.now()` → OCALL to host for current UNIX time.

### 4. `kv-store` — Sealed KV write

```json
{
  "wasm_call": {
    "app": "test-app",
    "function": "kv-store",
    "params": [
      {"type": "string", "value": "greeting"},
      {"type": "string", "value": "Hello from WASM in SGX!"}
    ]
  }
}
```

Returns: `"stored: greeting"`

Opens a "file" via `wasi:filesystem`, writes the value, and calls
`sync-data()` to flush encrypted data to the host-side sealed KV store.

### 5. `kv-read` — Sealed KV read

```json
{
  "wasm_call": {
    "app": "test-app",
    "function": "kv-read",
    "params": [{"type": "string", "value": "greeting"}]
  }
}
```

Returns: `"Hello from WASM in SGX!"` (the value stored by `kv-store`)

Data persists across calls and enclave restarts (same MRENCLAVE required).
Each app is namespace-isolated: `app:<name>/fs:<path>`.

### 6. `fetch-headlines` — HTTPS egress

```json
{"wasm_call": {"app": "test-app", "function": "fetch-headlines", "params": []}}
```

Returns: numbered list of Le Monde headlines (up to 10)

Uses `privasys:enclave-os/https.fetch()` to make an HTTPS GET request.
TLS 1.3 terminates **inside the enclave** using rustls + Mozilla root CAs.
The host only sees encrypted TCP bytes.

### 7. `analyse-data` — Records, enums, options (MCP demo)

```json
{
  "wasm_call": {
    "app": "test-app",
    "function": "analyse-data",
    "params": [
      {"type": "list<float64>", "value": [1.0, 2.5, 3.0, 4.5, 5.0]},
      {"type": "record", "value": {
        "include-stats": true,
        "label": "sample",
        "format": "json"
      }}
    ]
  }
}
```

Returns: JSON string with count, sum, mean, min, max.

Exercises WIT records (`analysis-config`), enums (`output-format`), and
options (`option<string>`). Designed to demonstrate MCP tool generation
with complex input types — the `mcp_tools` endpoint derives a full JSON
Schema from these WIT types automatically.

---

## Connecting with RA-TLS clients

Use [ra-tls-clients](https://github.com/Privasys/ra-tls-clients) to connect,
verify attestation, and interact with the enclave.

### Go CLI (recommended)

```bash
cd ra-tls-clients/go
go run . --host <server> --port 443
```

The CLI verifies the RA-TLS certificate (DCAP quote + ReportData binding)
then drops into an interactive session where you can send JSON commands.

### Python

```python
from ratls_client import RaTlsClient

client = RaTlsClient("server.example.com", 443, ca_cert="path/to/ca.crt")
client.connect()

# Load the WASM app
with open("wasm_example.cwasm", "rb") as f:
    wasm_bytes = list(f.read())
client.send({"wasm_load": {"name": "test-app", "bytes": wasm_bytes}})
result = client.recv()

# Call a function
client.send({"wasm_call": {"app": "test-app", "function": "hello", "params": []}})
result = client.recv()
print(result)  # {"status": "ok", "returns": [{"type": "string", "value": "Hello, World!"}]}
```

Available clients: **Go**, **Python**, **Rust**, **TypeScript**, **C# (.NET)**
— see [ra-tls-clients](https://github.com/Privasys/ra-tls-clients) for details.

---

## Deployment

### Building Enclave OS with WASM support

See the [Enclave OS (Mini) README](https://github.com/Privasys/enclave-os-mini)
for full build instructions. The WASM runtime is enabled by building with the
`wasm-enclave` composition crate:

```bash
cd enclave-os-mini
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_WASM=ON -DWASM_ENCLAVE_DIR=/path/to/wasm-app-example/enclave
make -j$(nproc)
```

This produces `enclave-os-host` and `enclave-os-enclave.signed.so` in `build/bin/`.

### Running the enclave

```bash
cd build/bin
./enclave-os-host -p 8443
```

The enclave listens on `0.0.0.0:8443` for RA-TLS connections. WASM apps are
loaded dynamically — no apps need to be present at build time.

### Production: Layer 4 proxy

The enclave terminates TLS internally — a front-end load balancer must operate
at **Layer 4 (TCP passthrough)**. See the
[Layer 4 Proxy Guide](https://github.com/Privasys/enclave-os-mini/blob/main/docs/layer4-proxy.md)
for Caddy (with caddy-l4) and HAProxy configurations.

For cloud-specific setup instructions, see:
- [OVH Bare Metal (SGX)](install/ovh-sgx.md)

---

## Project structure

```
wasm-example/
├── Cargo.toml                 # cdylib crate, depends on wit-bindgen-rt
├── README.md                  # This file
├── src/
│   ├── lib.rs                 # 7 exported functions (~250 lines)
│   └── bindings.rs            # Auto-generated by wit-bindgen (do not edit)
├── tests/
│   └── test_wasm_functions.py # Integration test suite (all 6 functions)
├── install/
│   └── ovh-sgx.md             # OVH bare metal SGX deployment guide
└── wit/
    ├── world.wit              # Component world (imports + exports)
    └── deps/
        ├── clocks/            # wasi:clocks@0.2.0
        ├── enclave-os/        # privasys:enclave-os@0.1.0 (HTTPS)
        ├── filesystem/        # wasi:filesystem@0.2.0
        ├── io/                # wasi:io@0.2.0
        └── random/            # wasi:random@0.2.0
```

WIT interfaces are copied from the
[Enclave OS WASM SDK](https://github.com/Privasys/enclave-os-mini/tree/main/crates/enclave-os-wasm/sdk).
The full SDK includes additional interfaces (`cli`, `sockets`, `crypto`,
`keystore`) not used by this example.

---

## Expected results

| Function | Expected output | Validation |
|----------|----------------|------------|
| `hello` | `"Hello, World!"` | Exact string match |
| `get-random` | Integer `[1, 100]` | Range check, varies per call |
| `get-time` | `"<unix_ts>.000000000"` | Valid recent UNIX timestamp |
| `kv-store("k","v")` | `"stored: k"` | Exact match |
| `kv-read("k")` | `"v"` | Returns previously stored value |
| `kv-read("missing")` | `"error: key not found: missing"` | Error message |
| `fetch-headlines` | Numbered list (1-10 items) | At least 2 headlines |
| `analyse-data([1,2,3],{...})` | Stats string/JSON/CSV | Contains count, sum, mean |

---

## Security notes

- **Stateless execution**: Each call creates a fresh WASM instance. KV data
  persists via `sync-data()` → sealed KV store on the host.
- **Namespace isolation**: Each app's files and keys are prefixed with
  `app:<name>/` — apps cannot access each other's data.
- **HTTPS only**: The egress interface only supports `https://` URLs —
  `http://` requests are rejected. The host never sees plaintext.
- **Attestation**: The SHA-256 hash of the loaded WASM bytecode is embedded
  in the RA-TLS certificate, allowing remote clients to verify exactly what
  code is running.

## License

This project is licensed under the
[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html).
