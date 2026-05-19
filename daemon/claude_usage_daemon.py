#!/usr/bin/env python3
"""Clawdmeter Usage Tracker Daemon (BLE).

Polls Claude API rate-limit headers, reads local Codex token-use logs, and
writes a JSON payload to the ESP32 "Claude Controller" peripheral over a
custom GATT service.
"""

import asyncio
import getpass
import json
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

VERSION = "0.3.1"
DEVICE_NAME = "Claude Controller"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0
CODEX_5H_WINDOW_SECS = 5 * 60 * 60
CODEX_7D_WINDOW_SECS = 7 * 24 * 60 * 60


# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"
CODEX_LOG_DB = Path(os.environ.get("CODEX_USAGE_SQLITE", Path.home() / ".codex" / "logs_2.sqlite"))
CODEX_CMD = os.environ.get("CODEX_CMD") or str(Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd")

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

TOKEN_USAGE_RE = re.compile(r"(input|output)_token_count=(\d+)")
TOTAL_TOKENS_RE = re.compile(r"codex\.turn\.token_usage\.total_tokens=(\d+)")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


CODEX_5H_TOKEN_BUDGET = _env_int("CODEX_5H_TOKEN_BUDGET", 10000000)
CODEX_7D_TOKEN_BUDGET = _env_int("CODEX_7D_TOKEN_BUDGET", 50000000)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


async def scan_for_device() -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    payload = {
        "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "ok": True,
    }
    return payload


def _usage_error(status: str) -> dict:
    return {
        "s": 0,
        "sr": -1,
        "w": 0,
        "wr": -1,
        "st": status[:15],
        "ok": False,
    }


def _extract_codex_tokens(body: str) -> int:
    counts = {name: int(value) for name, value in TOKEN_USAGE_RE.findall(body)}
    if counts:
        return counts.get("input", 0) + counts.get("output", 0)

    m = TOTAL_TOKENS_RE.search(body)
    if m:
        return int(m.group(1))
    return 0


def _pct_used(tokens: int, budget: int) -> int:
    if budget <= 0:
        return 0
    return max(0, min(100, int(round((tokens / budget) * 100))))


def _reset_minutes_from_epoch(reset_ts: int | float | None) -> int:
    if reset_ts is None:
        return -1
    mins = (float(reset_ts) - time.time()) / 60.0
    return int(round(mins)) if mins > 0 else 0


def _remaining_percent(used: int | float | None) -> int:
    if used is None:
        return 0
    return max(0, min(100, int(round(100 - float(used)))))


def _reader_thread(stream, out_queue: queue.Queue[str]) -> None:
    try:
        for line in stream:
            out_queue.put(line.rstrip("\n"))
    finally:
        out_queue.put("")


def _codex_app_server_message(method: str, msg_id: str, params: dict | None = None) -> dict:
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _query_codex_rate_limits() -> dict | None:
    """Ask Codex's own app-server for the same limit snapshot the TUI uses."""
    codex_cmd = Path(CODEX_CMD)
    if not codex_cmd.exists():
        log(f"Codex command not found: {codex_cmd}")
        return None

    try:
        proc = subprocess.Popen(
            [str(codex_cmd), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        log(f"Codex app-server start failed: {e}")
        return None

    out_queue: queue.Queue[str] = queue.Queue()
    err_queue: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_reader_thread, args=(proc.stdout, out_queue), daemon=True).start()
    threading.Thread(target=_reader_thread, args=(proc.stderr, err_queue), daemon=True).start()

    def send(msg: dict) -> bool:
        if proc.stdin is None:
            return False
        try:
            proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
            proc.stdin.flush()
            return True
        except OSError:
            return False

    init = _codex_app_server_message(
        "initialize",
        "clawdmeter-init",
        {
            "clientInfo": {
                "name": "clawdmeter",
                "title": "Clawdmeter",
                "version": VERSION,
            },
            "capabilities": None,
        },
    )
    rate_limits = _codex_app_server_message(
        "account/rateLimits/read",
        "clawdmeter-rate-limits",
    )

    result: dict | None = None
    deadline = time.time() + 20.0
    try:
        if not send(init):
            return None

        initialized = False
        while time.time() < deadline:
            timeout = max(0.1, min(0.5, deadline - time.time()))
            try:
                line = out_queue.get(timeout=timeout)
            except queue.Empty:
                continue
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if msg.get("id") == "clawdmeter-init":
                initialized = True
                if not send(rate_limits):
                    return None
                continue

            if initialized and msg.get("id") == "clawdmeter-rate-limits":
                result = msg.get("result")
                break

        while not err_queue.empty():
            err_line = err_queue.get_nowait()
            if err_line:
                log(f"Codex app-server stderr: {err_line[:160]}")
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    if not result:
        log("Codex rate-limit read returned no result")
        return None

    snapshots = result.get("rateLimitsByLimitId") or {}
    snapshot = snapshots.get("codex") or result.get("rateLimits") or {}
    primary = snapshot.get("primary") or {}
    secondary = snapshot.get("secondary") or {}

    return {
        "s": _remaining_percent(primary.get("usedPercent")),
        "sr": _reset_minutes_from_epoch(primary.get("resetsAt")),
        "w": _remaining_percent(secondary.get("usedPercent")),
        "wr": _reset_minutes_from_epoch(secondary.get("resetsAt")),
        "st": "left",
        "ok": True,
    }


def _poll_codex_usage_estimate() -> dict:
    """Fallback estimate from the local Codex sqlite log."""
    if not CODEX_LOG_DB.exists():
        return {**_usage_error("nolog"), "t5": 0, "t7": 0}

    now = int(time.time())
    cutoff_5h = now - CODEX_5H_WINDOW_SECS
    cutoff_7d = now - CODEX_7D_WINDOW_SECS
    total_5h = 0
    total_7d = 0

    try:
        db_uri = f"{CODEX_LOG_DB.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(db_uri, uri=True, timeout=2) as conn:
            rows = conn.execute(
                """
                SELECT ts, feedback_log_body
                FROM logs
                WHERE target = 'codex_otel.trace_safe'
                  AND ts >= ?
                  AND feedback_log_body LIKE '%event.kind=response.completed%'
                """,
                (cutoff_7d,),
            ).fetchall()

            # Older builds can log only the aggregate token metric.
            if not rows:
                rows = conn.execute(
                    """
                    SELECT ts, feedback_log_body
                    FROM logs
                    WHERE ts >= ?
                      AND feedback_log_body LIKE '%codex.turn.token_usage.total_tokens=%'
                    """,
                    (cutoff_7d,),
                ).fetchall()
    except sqlite3.Error as e:
        log(f"Codex usage log read failed: {e}")
        return {**_usage_error("dberr"), "t5": 0, "t7": 0}

    for ts, body in rows:
        tokens = _extract_codex_tokens(body or "")
        total_7d += tokens
        if int(ts) >= cutoff_5h:
            total_5h += tokens

    return {
        "s": _pct_used(total_5h, CODEX_5H_TOKEN_BUDGET),
        "sr": 0,
        "w": _pct_used(total_7d, CODEX_7D_TOKEN_BUDGET),
        "wr": 0,
        "st": "est",
        "ok": True,
        "t5": total_5h,
        "t7": total_7d,
    }


def poll_codex_usage() -> dict:
    official = _query_codex_rate_limits()
    if official is not None:
        return official
    return _poll_codex_usage_estimate()


async def poll_usage(token: str | None) -> dict:
    if token:
        claude = await poll_api(token)
        if claude is None:
            claude = _usage_error("apierr")
    else:
        claude = _usage_error("noauth")

    return {
        "claude": claude,
        "codex": poll_codex_usage(),
    }


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


async def connect_and_run(address: str, stop_event: asyncio.Event) -> bool:
    """Connect to a known address and poll until disconnected or stopped.

    Returns True if the connection was used successfully (so the caller
    keeps the cached address), False if the connection failed and the
    cache should be invalidated.
    """
    log(f"Connecting to {address}...")
    client = BleakClient(address)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                token = read_token()
                if not token:
                    log("No Claude token; sending Codex/local usage only")
                payload = await poll_usage(token)
                if await session.write_payload(payload):
                    last_poll = time.time()
                    used_successfully = True

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log(f"=== Clawdmeter Usage Tracker Daemon v{VERSION} (BLE) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")
    log(
        "Codex estimate budgets: "
        f"5h={CODEX_5H_TOKEN_BUDGET:,} tokens, "
        f"7d={CODEX_7D_TOKEN_BUDGET:,} tokens"
    )

    backoff = 1
    while not stop_event.is_set():
        address = load_cached_address()
        if not address:
            address = await scan_for_device()
            if address:
                save_address(address)
            else:
                log(f"Device not found, retrying in {backoff}s...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)
                continue

        ok = await connect_and_run(address, stop_event)
        if not ok:
            log("Invalidating cached address")
            SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
