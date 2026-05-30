#![windows_subsystem = "windows"] // no console window (runs from Startup shortcut)

// Clawdmeter usage daemon (Rust port of claude_usage_daemon.py).
// Polls Claude API rate-limit headers + Codex sqlite log, writes nested JSON
// {"claude":{...},"codex":{...}} to the ESP32 "Claude Controller" over BLE GATT.
// Single static binary — no python/bleak runtime drift (native-first hard rule).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use btleplug::api::{Central, Manager as _, Peripheral as _, ScanFilter, WriteType};
use btleplug::platform::Manager;
use serde_json::{json, Value};
use uuid::Uuid;

const DEVICE_NAME: &str = "Claude Controller";
const RX_UUID_STR: &str = "4c41555a-4465-7669-6365-000000000002"; // host writes JSON here
const API_URL: &str = "https://api.anthropic.com/v1/messages";
const POLL_SECS: u64 = 300;
const SCAN_SECS: u64 = 8;
const CODEX_5H_SECS: i64 = 5 * 60 * 60;
const CODEX_7D_SECS: i64 = 7 * 24 * 60 * 60;
const CODEX_5H_BUDGET: i64 = 10_000_000;
const CODEX_7D_BUDGET: i64 = 50_000_000;

fn log(m: &str) {
    use std::io::Write;
    let line = format!("[{}] {}\n", chrono::Local::now().format("%H:%M:%S"), m);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(r"C:\DEVEL\Clawdmeter\clawdmeter-rs.out.log")
    {
        let _ = f.write_all(line.as_bytes());
    }
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn err_usage(st: &str) -> Value {
    json!({"s": 0, "sr": -1, "w": 0, "wr": -1, "st": st, "ok": false})
}

/// Read OAuth accessToken from ~/.claude/.credentials.json (direct or nested).
fn read_token() -> Option<String> {
    let p = dirs::home_dir()?.join(".claude").join(".credentials.json");
    let blob = std::fs::read_to_string(p).ok()?;
    let v: Value = serde_json::from_str(&blob).ok()?;
    if let Some(t) = v.get("accessToken").and_then(|x| x.as_str()) {
        return Some(t.to_string());
    }
    if let Some(obj) = v.as_object() {
        for (_, val) in obj {
            if let Some(t) = val.get("accessToken").and_then(|x| x.as_str()) {
                return Some(t.to_string());
            }
        }
    }
    None
}

/// Claude usage from Anthropic API response rate-limit headers.
async fn poll_claude(http: &reqwest::Client, token: &str) -> Value {
    let body = json!({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}]
    });
    let resp = match http
        .post(API_URL)
        .header("anthropic-version", "2023-06-01")
        .header("anthropic-beta", "oauth-2025-04-20")
        .header("content-type", "application/json")
        .header("user-agent", "claude-code/2.1.5")
        .header("authorization", format!("Bearer {token}"))
        .json(&body)
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => {
            log(&format!("API call failed: {e}"));
            return err_usage("apierr");
        }
    };
    let status = resp.status().as_u16();
    let h = resp.headers().clone();
    let get = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).map(|s| s.to_string());
    // Anthropic emits rate-limit headers even on 429; bail only if absent.
    if status >= 400 && get("anthropic-ratelimit-unified-5h-utilization").is_none() {
        log(&format!("API HTTP {status}"));
        return err_usage("apierr");
    }
    let now = now_secs();
    let pct = |s: Option<String>| {
        s.and_then(|x| x.parse::<f64>().ok()).map(|f| (f * 100.0).round() as i64).unwrap_or(0)
    };
    let resetm = |s: Option<String>| {
        s.and_then(|x| x.parse::<f64>().ok())
            .map(|r| {
                let m = (r - now) / 60.0;
                if m > 0.0 { m.round() as i64 } else { 0 }
            })
            .unwrap_or(0)
    };
    json!({
        "s": pct(get("anthropic-ratelimit-unified-5h-utilization")),
        "sr": resetm(get("anthropic-ratelimit-unified-5h-reset")),
        "w": pct(get("anthropic-ratelimit-unified-7d-utilization")),
        "wr": resetm(get("anthropic-ratelimit-unified-7d-reset")),
        "st": get("anthropic-ratelimit-unified-5h-status").unwrap_or_else(|| "unknown".into()),
        "ok": true
    })
}

/// Codex usage estimate from ~/.codex/logs_2.sqlite token totals vs budgets.
fn codex_estimate() -> Value {
    let db = match dirs::home_dir().map(|h| h.join(".codex").join("logs_2.sqlite")) {
        Some(p) if p.exists() => p,
        _ => return err_usage("nolog"),
    };
    let now = now_secs() as i64;
    let c5 = now - CODEX_5H_SECS;
    let c7 = now - CODEX_7D_SECS;
    let conn = match rusqlite::Connection::open_with_flags(
        &db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(c) => c,
        Err(e) => {
            log(&format!("Codex db open failed: {e}"));
            return err_usage("dberr");
        }
    };
    let re_io = regex::Regex::new(r"(input|output)_token_count=(\d+)").unwrap();
    let re_tot = regex::Regex::new(r"codex\.turn\.token_usage\.total_tokens=(\d+)").unwrap();
    let mut total5: i64 = 0;
    let mut total7: i64 = 0;
    let sql = "SELECT ts, feedback_log_body FROM logs WHERE ts >= ?1 \
               AND (feedback_log_body LIKE '%event.kind=response.completed%' \
                    OR feedback_log_body LIKE '%codex.turn.token_usage.total_tokens=%')";
    if let Ok(mut stmt) = conn.prepare(sql) {
        if let Ok(rows) = stmt.query_map([c7], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1).unwrap_or_default()))
        }) {
            for (ts, body) in rows.flatten() {
                let mut io = 0i64;
                let mut found = false;
                for cap in re_io.captures_iter(&body) {
                    io += cap[2].parse::<i64>().unwrap_or(0);
                    found = true;
                }
                let tok = if found {
                    io
                } else if let Some(c) = re_tot.captures(&body) {
                    c[1].parse::<i64>().unwrap_or(0)
                } else {
                    0
                };
                total7 += tok;
                if ts >= c5 {
                    total5 += tok;
                }
            }
        }
    }
    let pct = |t: i64, b: i64| if b > 0 { ((t as f64 / b as f64 * 100.0).round() as i64).clamp(0, 100) } else { 0 };
    json!({
        "s": pct(total5, CODEX_5H_BUDGET), "sr": 0,
        "w": pct(total7, CODEX_7D_BUDGET), "wr": 0,
        "st": "est", "ok": true
    })
}

async fn build_payload(http: &reqwest::Client) -> Value {
    let claude = match read_token() {
        Some(t) => poll_claude(http, &t).await,
        None => err_usage("noauth"),
    };
    json!({"claude": claude, "codex": codex_estimate()})
}

async fn run_session(http: &reqwest::Client, rx_uuid: Uuid) -> Result<(), Box<dyn std::error::Error>> {
    let manager = Manager::new().await?;
    let adapter = manager.adapters().await?.into_iter().next().ok_or("no BLE adapter")?;
    log(&format!("Scanning for '{DEVICE_NAME}' ({SCAN_SECS}s)..."));
    adapter.start_scan(ScanFilter::default()).await?;
    tokio::time::sleep(Duration::from_secs(SCAN_SECS)).await;
    let _ = adapter.stop_scan().await;

    let mut dev = None;
    for p in adapter.peripherals().await? {
        if let Ok(Some(props)) = p.properties().await {
            if props.local_name.as_deref() == Some(DEVICE_NAME) {
                dev = Some(p);
                break;
            }
        }
    }
    let dev = dev.ok_or("device not found in scan")?;
    log("Connecting...");
    dev.connect().await?;
    dev.discover_services().await?;
    let rx = dev
        .characteristics()
        .into_iter()
        .find(|c| c.uuid == rx_uuid)
        .ok_or("RX characteristic not found")?;
    log("Connected");

    loop {
        let payload = build_payload(http).await;
        let data = serde_json::to_vec(&payload)?;
        log(&format!("Sending: {}", String::from_utf8_lossy(&data)));
        if let Err(e) = dev.write(&rx, &data, WriteType::WithoutResponse).await {
            log(&format!("Write failed: {e}"));
            return Err(e.into());
        }
        if !dev.is_connected().await.unwrap_or(false) {
            log("Device disconnected");
            return Ok(());
        }
        tokio::time::sleep(Duration::from_secs(POLL_SECS)).await;
    }
}

#[tokio::main]
async fn main() {
    log(&format!("=== Clawdmeter Usage Daemon (Rust) v0.4.0 — poll {POLL_SECS}s ==="));
    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
        .expect("http client");
    let rx_uuid = Uuid::parse_str(RX_UUID_STR).expect("uuid");
    loop {
        match run_session(&http, rx_uuid).await {
            Ok(()) => log("Session ended, reconnecting in 10s"),
            Err(e) => log(&format!("Session error: {e} — retry in 10s")),
        }
        tokio::time::sleep(Duration::from_secs(10)).await;
    }
}
