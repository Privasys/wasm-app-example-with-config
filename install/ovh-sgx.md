# Deploy on OVH Bare Metal (SGX)

Step-by-step guide for deploying Enclave OS (Mini) with WASM support on an
OVH dedicated server with Intel SGX.

---

## 1. Order an SGX-capable server

OVH offers dedicated servers with Intel SGX support in the **RISE** range:

- **RISE-1** — Intel Xeon-E 2386G (6C/12T, 3.5 GHz), 32 GB RAM, SGX with
  up to 64 GB EPC

When ordering:
- OS: **Ubuntu 22.04** or **Ubuntu 24.04** (bare metal, not VM)
- Enable SGX in BIOS (OVH provides IPMI/KVM access)

---

## 2. Enable SGX in BIOS

Access the server's IPMI console (OVH Control Panel → Server → IPMI):

1. Reboot into BIOS setup (press **DEL** or **F2** during POST)
2. Navigate to **Security** → **SGX** (or **Processor** → **SGX**)
3. Set **SGX** to **Enabled**
4. Set **SGX Enclave Size** to maximum (e.g. **512 MB** or **Auto**)
5. Ensure **Flexible Launch Control** is **Enabled** (if available)
6. Save and exit

Verify after boot:

```bash
dmesg | grep -i sgx
# Expected: "sgx: EPC section ..."
ls /dev/sgx_enclave /dev/sgx_provision
```

---

## 3. Install SGX dependencies

```bash
# Intel SGX SDK + PSW (Platform Software)
echo 'deb [arch=amd64] https://download.01.org/intel-sgx/sgx_repo/ubuntu jammy main' | \
  sudo tee /etc/apt/sources.list.d/intel-sgx.list
wget -qO - https://download.01.org/intel-sgx/sgx_repo/ubuntu/intel-sgx-deb.key | \
  sudo apt-key add -
sudo apt-get update

sudo apt-get install -y \
  libsgx-enclave-common \
  libsgx-urts \
  libsgx-dcap-ql \
  libsgx-dcap-default-qpl \
  sgx-aesm-service

# Verify AESM is running
sudo systemctl status aesmd
```

---

## 4. Install build tools

```bash
# Rust (nightly required for SGX)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
rustup install nightly-2026-06-21
rustup default nightly-2026-06-21
rustup component add rust-src --toolchain nightly-2026-06-21

# Build essentials
sudo apt-get install -y build-essential cmake git pkg-config

# WASM tools (for pre-compiling .cwasm)
rustup target add wasm32-wasip2
cargo install cargo-component
```

---

## 5. Build Enclave OS

```bash
git clone https://github.com/Privasys/enclave-os-mini.git
cd enclave-os-mini

cmake -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_WASM=ON
cmake --build build -j$(nproc)
```

Outputs in `build/bin/`:
- `enclave-os-host` — untrusted host binary
- `enclave.signed.so` — signed SGX enclave with WASM module

---

## 6. Build the WASM test app

```bash
git clone https://github.com/Privasys/wasm-app-example.git
cd wasm-app-example

cargo component build --release

# Copy to enclave directory
cp target/wasm32-wasip1/release/wasm_example.wasm ../enclave-os-mini/build/bin/
```

---

## 7. Run the enclave

```bash
cd enclave-os-mini/build/bin
./enclave-os-host -p 8443
```

The enclave generates its RA-TLS certificate on startup (takes a few seconds),
then listens on `0.0.0.0:8443`. **No WASM apps are loaded yet** — the enclave
starts empty. Apps are loaded dynamically at runtime (see next step).

---

## 8. Load the WASM app into the enclave

The WASM app is loaded **at runtime** over the RA-TLS connection — it is not
compiled into the enclave binary.

### Pre-compile to `.cwasm`

AOT-compile the `.wasm` to a `.cwasm` artifact that matches the enclave's
Wasmtime engine settings:

```bash
cd wasm-app-example
wasmtime compile target/wasm32-wasip1/release/wasm_example.wasm -o wasm_example.cwasm
cp wasm_example.cwasm ../enclave-os-mini/build/bin/
```

### Load via the test script

The test script automatically loads the `.cwasm` before running tests:

```bash
cd enclave-os-mini/build/bin
python3 test_wasm_functions.py wasm_example.cwasm
```

### Load programmatically

You can also load the app using any RA-TLS client. The `wasm_load` command
sends the pre-compiled bytecode to the enclave, which registers the app
and embeds its SHA-256 code hash in the RA-TLS certificate:

```python
# Connect via RA-TLS, then:
with open("wasm_example.cwasm", "rb") as f:
    wasm_bytes = list(f.read())

request = {"wasm_load": {"name": "test-app", "bytes": wasm_bytes}}
# Send as length-delimited JSON frame → enclave responds with app metadata
```

See the [Dynamic WASM Loading](../README.md#dynamic-wasm-loading) section
in the main README for the full wire protocol reference, including
`wasm_call`, `wasm_list`, and `wasm_unload` commands.

---

## 9. Set up Caddy as Layer 4 proxy

The enclave terminates TLS internally. A front-end proxy must operate at
**Layer 4 (TCP passthrough)** — it must NOT terminate TLS.

### Build Caddy with L4 module

```bash
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
xcaddy build --with github.com/mholt/caddy-l4
sudo mv caddy /usr/local/bin/
```

### Configure

Create `/etc/caddy/caddy.json`:

```json
{
  "apps": {
    "layer4": {
      "servers": {
        "enclave-proxy": {
          "listen": ["0.0.0.0:443"],
          "routes": [
            {
              "match": [{"tls": {}}],
              "handle": [
                {
                  "handler": "proxy",
                  "upstreams": [{"dial": ["127.0.0.1:8443"]}]
                }
              ]
            }
          ]
        }
      }
    }
  }
}
```

### Run as systemd service

```bash
sudo tee /etc/systemd/system/caddy-l4.service > /dev/null << 'EOF'
[Unit]
Description=Caddy L4 Proxy for Enclave OS
After=network.target

[Service]
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/caddy.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now caddy-l4
```

---

## 10. DNS

Point your domain to the server's public IP:

```
enclave.example.com  A  <server-ip>
```

Clients connect to `enclave.example.com:443` — the L4 proxy
forwards TCP streams to the enclave on `:8443`.

---

## 11. Test

### From the server (direct)

```bash
cd wasm-app-example
python tests/test_wasm_functions.py wasm_example.cwasm
```

### From a remote machine (via Caddy)

```bash
cd ra-tls-clients/go
go run . --host enclave.example.com --port 443
```

### Verify RA-TLS certificate

```bash
openssl s_client -connect enclave.example.com:443 </dev/null 2>&1 | \
  openssl x509 -text -noout | grep -A2 "1.3.6.1.4.1.65230"
```

You should see the SGX quote extension OIDs in the certificate.

---

## Firewall

```bash
# Allow only TLS (443) and SSH (22)
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

> Do **not** expose port 8443 publicly — only the L4 proxy should reach it.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `/dev/sgx_enclave` missing | Enable SGX in BIOS, install `libsgx-enclave-common` |
| AESM not running | `sudo systemctl restart aesmd` |
| `SIGILL` on enclave load | CPU doesn't support SGX, or SGX disabled in BIOS |
| TLS handshake timeout | Check Caddy is proxying 443 → 8443, firewall allows 443 |
| `SGX_ERROR_ENCLAVE_FILE_ACCESS` | Check enclave `.signed.so` is in the same directory |
