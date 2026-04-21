// Copyright (c) Privasys. All rights reserved.
// Licensed under the GNU Affero General Public License v3.0. See LICENSE file for details.

//! # WASM Integration Test App
//!
//! Seven exported functions that exercise the full Enclave OS WASM runtime:
//!
//! | Function | Tests |
//! |----------|-------|
//! | `hello` | Basic smoke test — no host imports |
//! | `get-random` | `wasi:random` → RDRAND inside SGX |
//! | `get-time` | `wasi:clocks/wall-clock` → OCALL |
//! | `kv-store` | `wasi:filesystem` → sealed KV store |
//! | `kv-read` | `wasi:filesystem` → sealed KV store |
//! | `fetch-headlines` | `privasys:enclave-os/https` → TLS egress |
//! | `analyse-data` | Records, enums, options — MCP tool demo |
//! | `auth-hello` | Authenticated-only endpoint (OIDC / FIDO2) |
//! | `role-hello` | Role-gated endpoint (requires "hello-role") |

#[allow(warnings)]
mod bindings;

use bindings::Guest;

struct TestApp;

impl Guest for TestApp {
    // ── 1. Hello World ────────────────────────────────────────────

    fn hello() -> String {
        "Hello, World!".to_string()
    }

    // ── 2. Get Random ─────────────────────────────────────────────

    fn get_random() -> u32 {
        let bytes = bindings::wasi::random::random::get_random_bytes(4);
        let raw = u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
        // Map to 1..=100
        (raw % 100) + 1
    }

    // ── 3. Get Time ───────────────────────────────────────────────

    fn get_time() -> String {
        let dt = bindings::wasi::clocks::wall_clock::now();
        format!("{}.{:09}", dt.seconds, dt.nanoseconds)
    }

    // ── 4. Store data in KV ───────────────────────────────────────

    fn kv_store(key: String, value: String) -> String {
        use bindings::wasi::filesystem::{preopens, types as fs};

        // Get the preopened root directory (backed by sealed KV store)
        let dirs = preopens::get_directories();
        if dirs.is_empty() {
            return "error: no preopened directories".to_string();
        }
        let root = &dirs[0].0;

        // Open (or create) the file — each "file" is a KV entry
        let fd = match root.open_at(
            fs::PathFlags::empty(),
            &key,
            fs::OpenFlags::CREATE | fs::OpenFlags::TRUNCATE,
            fs::DescriptorFlags::WRITE,
        ) {
            Ok(fd) => fd,
            Err(e) => return format!("error: open failed: {e:?}"),
        };

        // Write the value
        if let Err(e) = fd.write(value.as_bytes(), 0) {
            return format!("error: write failed: {e:?}");
        }

        // Sync flushes the encrypted data to the host KV store
        if let Err(e) = fd.sync_data() {
            return format!("error: sync failed: {e:?}");
        }

        format!("stored: {key}")
    }

    // ── 5. Read from KV ───────────────────────────────────────────

    fn kv_read(key: String) -> String {
        use bindings::wasi::filesystem::{preopens, types as fs};

        let dirs = preopens::get_directories();
        if dirs.is_empty() {
            return "error: no preopened directories".to_string();
        }
        let root = &dirs[0].0;

        let fd = match root.open_at(
            fs::PathFlags::empty(),
            &key,
            fs::OpenFlags::empty(),
            fs::DescriptorFlags::READ,
        ) {
            Ok(fd) => fd,
            Err(fs::ErrorCode::NoEntry) => return format!("error: key not found: {key}"),
            Err(e) => return format!("error: open failed: {e:?}"),
        };

        let stat = match fd.stat() {
            Ok(s) => s,
            Err(e) => return format!("error: stat failed: {e:?}"),
        };

        let (data, _eof) = match fd.read(stat.size, 0) {
            Ok(r) => r,
            Err(e) => return format!("error: read failed: {e:?}"),
        };

        String::from_utf8(data).unwrap_or_else(|_| "(binary data)".to_string())
    }

    // ── 6. Fetch headlines from lemonde.fr ─────────────────────────

    fn fetch_headlines() -> String {
        use bindings::privasys::enclave_os::https;

        // HTTPS GET — TLS terminates inside the enclave
        let resp = match https::fetch(
            0, // GET
            "https://www.lemonde.fr",
            &[
                ("User-Agent".into(), "wasm-test-app/1.0".into()),
                ("Accept".into(), "text/html".into()),
            ],
            None,
        ) {
            Ok(r) => r,
            Err(e) => return format!("error: {e}"),
        };

        let (status, _headers, body) = resp;
        if status != 200 {
            return format!("error: HTTP {status}");
        }

        let html = String::from_utf8_lossy(&body);
        let titles = extract_titles(&html, 10);

        if titles.is_empty() {
            "No titles found".to_string()
        } else {
            titles
                .iter()
                .enumerate()
                .map(|(i, t)| format!("{}. {}", i + 1, t))
                .collect::<Vec<_>>()
                .join("\n")
        }
    }

    // ── 7. Analyse numeric data ───────────────────────────────────

    fn analyse_data(values: Vec<f64>, config: bindings::AnalysisConfig) -> String {
        if values.is_empty() {
            return match config.format {
                bindings::OutputFormat::Json => r#"{"error":"empty input"}"#.to_string(),
                _ => "error: empty input".to_string(),
            };
        }

        let count = values.len();
        let sum: f64 = values.iter().sum();
        let mean = sum / count as f64;
        let min = values.iter().cloned().fold(f64::INFINITY, f64::min);
        let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);

        let label = config.label.as_deref().unwrap_or("result");

        match config.format {
            bindings::OutputFormat::Text => {
                if config.include_stats {
                    format!(
                        "{label}: count={count}, sum={sum:.4}, mean={mean:.4}, min={min:.4}, max={max:.4}"
                    )
                } else {
                    format!("{label}: count={count}, sum={sum:.4}")
                }
            }
            bindings::OutputFormat::Json => {
                if config.include_stats {
                    format!(
                        r#"{{"label":"{label}","count":{count},"sum":{sum:.4},"mean":{mean:.4},"min":{min:.4},"max":{max:.4}}}"#
                    )
                } else {
                    format!(
                        r#"{{"label":"{label}","count":{count},"sum":{sum:.4}}}"#
                    )
                }
            }
            bindings::OutputFormat::Csv => {
                if config.include_stats {
                    format!("label,count,sum,mean,min,max\n{label},{count},{sum:.4},{mean:.4},{min:.4},{max:.4}")
                } else {
                    format!("label,count,sum\n{label},{count},{sum:.4}")
                }
            }
        }
    }

    // ── 8. Auth Hello (authenticated-only) ─────────────────────────

    fn auth_hello() -> bindings::AuthHelloResult {
        use bindings::privasys::enclave_os::auth;

        // Auth is enforced by the runtime before this function is called.
        // Use the auth import to read back the caller's identity and roles.
        let caller = auth::get_caller_id().unwrap_or_else(|e| format!("unknown ({e})"));
        let roles = auth::get_my_roles().unwrap_or_else(|_| Vec::new());
        let ts = bindings::wasi::clocks::wall_clock::now();
        bindings::AuthHelloResult {
            caller,
            roles,
            message: "Hello from inside the enclave — you are authenticated".to_string(),
            timestamp_seconds: ts.seconds,
            timestamp_nanos: ts.nanoseconds,
            enclave: "sgx".to_string(),
        }
    }

    // ── 9. Role Hello (requires "hello-role") ───────────────────────

    fn role_hello() -> bindings::AuthHelloResult {
        use bindings::privasys::enclave_os::auth;

        // Auth + role check is enforced by the runtime before this function
        // is called.  Use the auth import to confirm our identity.
        let caller = auth::get_caller_id().unwrap_or_else(|e| format!("unknown ({e})"));
        let roles = auth::get_my_roles().unwrap_or_else(|_| Vec::new());
        let ts = bindings::wasi::clocks::wall_clock::now();
        bindings::AuthHelloResult {
            caller,
            roles,
            message: "Hello from inside the enclave — you have the hello-role".to_string(),
            timestamp_seconds: ts.seconds,
            timestamp_nanos: ts.nanoseconds,
            enclave: "sgx".to_string(),
        }
    }
}

// ── HTML title extraction (minimal, no dependencies) ──────────────

/// Extract up to `max` text contents from `<h3>…</h3>` tags.
fn extract_titles(html: &str, max: usize) -> Vec<String> {
    let mut titles = Vec::new();
    let mut pos = 0;

    while titles.len() < max {
        // Find next <h3
        let tag_start = match html[pos..].find("<h3") {
            Some(i) => pos + i,
            None => break,
        };
        // Find the closing > of the opening tag
        let content_start = match html[tag_start..].find('>') {
            Some(i) => tag_start + i + 1,
            None => break,
        };
        // Find </h3>
        let content_end = match html[content_start..].find("</h3>") {
            Some(i) => content_start + i,
            None => break,
        };

        let raw = &html[content_start..content_end];
        let text = strip_tags(raw);
        let text = text.trim();

        if !text.is_empty() {
            titles.push(text.to_string());
        }

        pos = content_end + 5; // skip past </h3>
    }

    titles
}

/// Strip HTML tags from a string fragment.
fn strip_tags(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_tag = false;
    for ch in s.chars() {
        match ch {
            '<' => in_tag = true,
            '>' => in_tag = false,
            _ if !in_tag => out.push(ch),
            _ => {}
        }
    }
    out
}

bindings::export!(TestApp with_types_in bindings);
