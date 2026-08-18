#!/usr/bin/env python3
"""Run one stateless Codex request and print only the final assistant message."""

import argparse
import glob
import json
import os
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import types
import uuid
from datetime import datetime, timezone


DEFAULT_TIMEOUT_SECONDS = 90
TIMEOUT_ENV_VAR = "CODEX_WRAPPER_TIMEOUT"
WRITE_BASE_ENV = "CODEX_WRAPPER_WRITE_BASE"
STATE_DIR_ENV = "CODEX_WRAPPER_STATE_DIR"
OUTPUT_DIR_ENV = "CODEX_WRAPPER_OUTPUT_DIR"
TEMP_DIR_ENV = "CODEX_WRAPPER_TEMP_DIR"
CANDIDATES_ENV = "CODEX_WRAPPER_CANDIDATES"
EXEC_CODEX_HOME_ENV = "CODEX_WRAPPER_EXEC_CODEX_HOME"
MODE_TERRA = "terra"
MODE_TERRA_HIGH = "terrahigh"
MODE_LUNA = "luna"
MODE_LUNA_HIGH = "lunahigh"
MODE_DEEP = "deep"
ALLOWED_MODES = (MODE_TERRA, MODE_TERRA_HIGH, MODE_LUNA, MODE_LUNA_HIGH, MODE_DEEP)
ALLOWED_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
ALLOWED_WEB_SEARCH_CONTEXT_SIZES = ("low", "medium", "high")
NON_FATAL_ROLLOUT_ERROR = "failed to record rollout items:"
USAGE_LOG_FILENAME = "usage-telemetry.jsonl"
USAGE_LOG_MAX_BYTES = 5_000_000
QUOTA_TIMEOUT_SECONDS = 5
MAX_QUOTA_BUCKETS = 20
MAX_RPC_BUFFER_BYTES = 1_000_000
EXEC_DISABLED_FEATURES = (
    "shell_tool",
    "apps",
    "browser_use",
    "computer_use",
    "hooks",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
)
MODE_PRESETS = {
    MODE_TERRA: {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "web_search": "live",
        "web_search_context_size": None,
    },
    MODE_TERRA_HIGH: {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "web_search": "live",
        "web_search_context_size": "high",
    },
    MODE_LUNA: {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "web_search": "live",
        "web_search_context_size": None,
    },
    MODE_LUNA_HIGH: {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "web_search": "live",
        "web_search_context_size": "high",
    },
    MODE_DEEP: {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "web_search": "disabled",
        "web_search_context_size": None,
        "soju_history": True,
    },
}

SOJU_TOOLS_PATH = os.path.join(
    os.path.dirname(__file__), "soju_history_tools.py"
)
SOJU_TOOL_NAMES = (
    "search",
    "search_summary",
    "history_summary",
    "context",
    "conversations",
    "speaker_history",
    "aggregate",
)
SOJU_TRANSPORT_CONFIG_ENV = "CODEX_SOJU_TRANSPORT_CONFIG"
SOJU_CUTOFF_ENV = "CODEX_SOJU_CUTOFF"
SOJU_TELEMETRY_PATH_ENV = "CODEX_SOJU_TELEMETRY_PATH"
SOJU_REQUEST_ID_ENV = "CODEX_SOJU_REQUEST_ID"


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("timeout must be > 0")
    return parsed


def _resolve_timeout(cli_timeout):
    if cli_timeout is not None:
        return cli_timeout

    env_timeout = os.environ.get(TIMEOUT_ENV_VAR)
    if not env_timeout:
        return DEFAULT_TIMEOUT_SECONDS

    try:
        return _positive_int(env_timeout)
    except argparse.ArgumentTypeError:
        return DEFAULT_TIMEOUT_SECONDS


def _runtime_settings(mode, reasoning_effort=None, web_search_context_size=None):
    normalized_mode = str(mode or MODE_TERRA).strip().lower()
    if normalized_mode not in ALLOWED_MODES:
        raise RuntimeError(f"unsupported mode: {mode}")

    preset = dict(MODE_PRESETS[normalized_mode])
    if reasoning_effort is not None:
        preset["reasoning_effort"] = reasoning_effort
    if web_search_context_size is not None:
        preset["web_search_context_size"] = web_search_context_size
    return preset


def _terminate_process_group(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return


def _kill_process_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _run_with_timeout(cmd, input_text, env, timeout_seconds, cwd=None, temp_dir=None):
    temp_root = temp_dir or tempfile.gettempdir()
    os.makedirs(temp_root, mode=0o700, exist_ok=True)
    paths = []
    proc = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=temp_root, prefix="codex-stdin-", delete=False
        ) as stdin_file:
            stdin_file.write(input_text)
            if input_text and not input_text.endswith("\n"):
                stdin_file.write("\n")
            stdin_path = stdin_file.name
        paths.append(stdin_path)

        stdout_fd, stdout_path = tempfile.mkstemp(prefix="codex-stdout-", dir=temp_root)
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="codex-stderr-", dir=temp_root)
        os.close(stdout_fd)
        os.close(stderr_fd)
        paths.extend((stdout_path, stderr_path))

        with open(stdin_path, "r", encoding="utf-8") as stdin_handle, open(
            stdout_path, "w", encoding="utf-8"
        ) as stdout_handle, open(stderr_path, "w", encoding="utf-8") as stderr_handle:
            proc = subprocess.Popen(
                cmd,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(proc.pid)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_process_group(proc.pid)
                    proc.wait()
                stdout_handle.flush()
                stderr_handle.flush()
                exc.stdout = _read_text_file(stdout_path)
                exc.stderr = _read_text_file(stderr_path)
                raise
            stdout_handle.flush()
            stderr_handle.flush()

        return types.SimpleNamespace(
            returncode=returncode,
            stdout=_read_text_file(stdout_path),
            stderr=_read_text_file(stderr_path),
        )
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _resolve_exec_codex_home():
    configured = os.environ.get(EXEC_CODEX_HOME_ENV, "").strip()
    candidate = configured or os.path.join(os.path.expanduser("~"), ".codex")
    code_home = os.path.abspath(os.path.expanduser(candidate))
    auth_path = os.path.join(code_home, "auth.json")

    if not os.path.isdir(code_home):
        raise RuntimeError(f"shared Codex home does not exist: {code_home}")
    if not os.path.isfile(auth_path):
        raise RuntimeError(f"shared Codex auth file does not exist: {auth_path}")
    if not os.access(auth_path, os.R_OK | os.W_OK):
        raise RuntimeError(f"shared Codex auth file is not readable and writable: {auth_path}")
    if not os.access(code_home, os.W_OK):
        raise RuntimeError(f"shared Codex home is not writable: {code_home}")
    return code_home


def _codex_child_env(
    codex_binary,
    layout,
    code_home,
    soju_transport_config=None,
    soju_cutoff=None,
    soju_request_id=None,
):
    inherited_keys = (
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    )
    run_env = {key: os.environ[key] for key in inherited_keys if os.environ.get(key)}

    codex_dir = os.path.dirname(codex_binary)
    run_env["PATH"] = codex_dir + os.pathsep + run_env.get("PATH", "")
    run_env["CODEX_HOME"] = code_home
    run_env["TMPDIR"] = layout["temp"]
    run_env["TMP"] = layout["temp"]
    run_env["TEMP"] = layout["temp"]
    run_env.setdefault("TERM", "dumb")
    run_env.setdefault("NO_COLOR", "1")
    run_env["PWD"] = layout["agent_cwd"]
    if soju_transport_config:
        run_env[SOJU_TRANSPORT_CONFIG_ENV] = soju_transport_config
    if soju_cutoff:
        run_env[SOJU_CUTOFF_ENV] = soju_cutoff
    if soju_transport_config and soju_cutoff:
        run_env[SOJU_TELEMETRY_PATH_ENV] = os.path.join(
            layout["output"], "soju-tool-telemetry.jsonl"
        )
        if soju_request_id:
            run_env[SOJU_REQUEST_ID_ENV] = str(soju_request_id)
    return run_env


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=True)


def _exec_command(codex_binary, settings, layout, output_path):
    cmd = [
        codex_binary,
        "exec",
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--color",
        "never",
        "--json",
        "-m",
        settings["model"],
        "-s",
        "read-only",
        "-C",
        layout["agent_cwd"],
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'history.persistence="none"',
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        f'model_reasoning_effort="{settings["reasoning_effort"]}"',
        "-c",
        'model_reasoning_summary="none"',
        "-c",
        'model_verbosity="low"',
        "-c",
        f'web_search="{settings["web_search"]}"',
    ]
    for feature in EXEC_DISABLED_FEATURES:
        cmd.extend(("-c", f"features.{feature}=false"))

    if settings.get("soju_history"):
        enabled_tools = ",".join(_toml_string(name) for name in SOJU_TOOL_NAMES)
        cmd.extend(
            (
                "-c",
                f"mcp_servers.soju_history.command={_toml_string(sys.executable)}",
                "-c",
                f"mcp_servers.soju_history.args=[{_toml_string(SOJU_TOOLS_PATH)}]",
                "-c",
                (
                    'mcp_servers.soju_history.env_vars=['
                    f'"{SOJU_TRANSPORT_CONFIG_ENV}","{SOJU_CUTOFF_ENV}",'
                    f'"{SOJU_TELEMETRY_PATH_ENV}","{SOJU_REQUEST_ID_ENV}"]'
                ),
                "-c",
                "mcp_servers.soju_history.required=true",
                "-c",
                f"mcp_servers.soju_history.enabled_tools=[{enabled_tools}]",
                "-c",
                'mcp_servers.soju_history.default_tools_approval_mode="approve"',
                "-c",
                "mcp_servers.soju_history.startup_timeout_sec=10",
                "-c",
                "mcp_servers.soju_history.tool_timeout_sec=35",
            )
        )

    context_size = settings.get("web_search_context_size")
    if context_size:
        cmd.extend(("-c", f'tools.web_search.context_size="{context_size}"'))

    cmd.extend(("--output-last-message", output_path, "-"))
    return cmd


def _read_last_message(output_path):
    try:
        with open(output_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise RuntimeError(f"failed to read codex output: {exc}") from exc


def _has_non_fatal_rollout_error(text):
    return NON_FATAL_ROLLOUT_ERROR in (text or "")


def _structured_error_detail(text):
    """Extract useful failure text from codex exec's JSONL event stream."""
    messages = []
    for line in (text or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") not in (
            "error",
            "turn.failed",
        ):
            continue

        candidates = [event.get("message")]
        error = event.get("error")
        if isinstance(error, str):
            candidates.append(error)
        elif isinstance(error, dict):
            candidates.extend(
                error.get(key) for key in ("message", "detail", "description")
            )

        for candidate in candidates:
            if isinstance(candidate, str):
                candidate = " ".join(candidate.split())
                if candidate and candidate not in messages:
                    messages.append(candidate)

    return " | ".join(messages)[-2000:]


def _turn_usage(text):
    """Return the final exact token counters from a codex exec JSONL stream."""
    usage = None
    for line in (text or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        candidate = event.get("usage")
        if isinstance(candidate, dict):
            usage = candidate

    if usage is None:
        return None

    normalized = {}
    required = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    for key in required:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        normalized[key] = value

    cache_write = usage.get("cache_write_input_tokens")
    if (
        isinstance(cache_write, int)
        and not isinstance(cache_write, bool)
        and cache_write >= 0
    ):
        normalized["cache_write_input_tokens"] = cache_write
    normalized["total_tokens"] = (
        normalized["input_tokens"] + normalized["output_tokens"]
    )
    return normalized


def _bounded_string(value, limit=100):
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value[:limit]


def _quota_window(value):
    if not isinstance(value, dict):
        return None
    used_percent = value.get("usedPercent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, int):
        return None
    result = {"used_percent": used_percent}
    for source, target in (
        ("windowDurationMins", "window_duration_minutes"),
        ("resetsAt", "resets_at"),
    ):
        item = value.get(source)
        if isinstance(item, int) and not isinstance(item, bool):
            result[target] = item
    return result


def _quota_bucket(value):
    if not isinstance(value, dict):
        return None
    result = {
        "limit_id": _bounded_string(value.get("limitId")),
        "limit_name": _bounded_string(value.get("limitName")),
        "plan_type": _bounded_string(value.get("planType")),
        "primary": _quota_window(value.get("primary")),
        "secondary": _quota_window(value.get("secondary")),
        "rate_limit_reached_type": _bounded_string(value.get("rateLimitReachedType")),
    }
    spend_control_reached = value.get("spendControlReached")
    if isinstance(spend_control_reached, bool):
        result["spend_control_reached"] = spend_control_reached

    credits = value.get("credits")
    if isinstance(credits, dict):
        safe_credits = {}
        for source, target in (
            ("hasCredits", "has_credits"),
            ("unlimited", "unlimited"),
        ):
            item = credits.get(source)
            if isinstance(item, bool):
                safe_credits[target] = item
        balance = _bounded_string(credits.get("balance"), limit=40)
        if balance is not None:
            safe_credits["balance"] = balance
        if safe_credits:
            result["credits"] = safe_credits
    return result


def _sanitize_rate_limits(result):
    if not isinstance(result, dict):
        raise RuntimeError("invalid quota response")

    buckets = {}
    source_buckets = result.get("rateLimitsByLimitId")
    if isinstance(source_buckets, dict):
        for key, value in list(source_buckets.items())[:MAX_QUOTA_BUCKETS]:
            safe_key = _bounded_string(key)
            safe_bucket = _quota_bucket(value)
            if safe_key and safe_bucket is not None:
                buckets[safe_key] = safe_bucket

    if not buckets:
        fallback = _quota_bucket(result.get("rateLimits"))
        if fallback is None:
            raise RuntimeError("quota response had no rate-limit buckets")
        fallback_key = fallback.get("limit_id") or "default"
        buckets[fallback_key] = fallback

    sanitized = {"buckets": buckets}
    reset_credits = result.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        available = reset_credits.get("availableCount")
        if (
            isinstance(available, int)
            and not isinstance(available, bool)
            and available >= 0
        ):
            sanitized["rate_limit_reset_credits_available"] = available
    return sanitized


def _write_rpc_message(proc, message):
    payload = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
    proc.stdin.write(payload.encode("utf-8") + b"\n")
    proc.stdin.flush()


def _read_rpc_response(proc, request_id, deadline, buffered=b""):
    while True:
        while b"\n" in buffered:
            raw_line, buffered = buffered.split(b"\n", 1)
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message, buffered

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("quota request timed out")
        readable, _, _ = select.select([proc.stdout], [], [], remaining)
        if not readable:
            raise TimeoutError("quota request timed out")
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError("app-server closed before responding")
        buffered += chunk
        if len(buffered) > MAX_RPC_BUFFER_BYTES:
            raise RuntimeError("app-server response exceeded safety limit")


def _stop_app_server(proc):
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    if proc.poll() is not None:
        return
    _terminate_process_group(proc.pid)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _read_quota_snapshot(codex_binary, env, cwd, timeout_seconds=QUOTA_TIMEOUT_SECONDS):
    """Read ChatGPT quota metadata through the documented app-server API."""
    proc = None
    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        proc = subprocess.Popen(
            [codex_binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        _write_rpc_message(
            proc,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "limnoria_codex",
                        "title": "Limnoria Codex Plugin",
                        "version": "1.0.0",
                    }
                },
            },
        )
        response, buffered = _read_rpc_response(proc, 1, deadline)
        if response.get("error") or not isinstance(response.get("result"), dict):
            raise RuntimeError("app-server initialization failed")

        _write_rpc_message(proc, {"method": "initialized", "params": {}})
        _write_rpc_message(proc, {"method": "account/rateLimits/read", "id": 2})
        response, _ = _read_rpc_response(proc, 2, deadline, buffered)
        if response.get("error") or not isinstance(response.get("result"), dict):
            raise RuntimeError("quota request failed")
        return {
            "status": "available",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            **_sanitize_rate_limits(response["result"]),
        }
    except TimeoutError:
        return {
            "status": "unavailable",
            "error": "timeout",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception:
        return {
            "status": "unavailable",
            "error": "request_failed",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        _stop_app_server(proc)


def _append_usage_record(layout, record):
    output_dir = layout.get("output") if isinstance(layout, dict) else ""
    if not output_dir:
        return False
    path = os.path.join(output_dir, USAGE_LOG_FILENAME)
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    try:
        try:
            if os.path.getsize(path) >= USAGE_LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _debug_env_summary(env):
    keys = (
        "HOME",
        "PATH",
        "PWD",
        "TERM",
        "NO_COLOR",
        "COLORTERM",
        "SSH_TTY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    )
    return ",".join(f"{key}={'set' if env.get(key) else 'unset'}" for key in keys)


def _append_debug_line(layout, message):
    output_dir = layout.get("output") if isinstance(layout, dict) else ""
    if not output_dir:
        return
    try:
        path = os.path.join(output_dir, "wrapper-debug.log")
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} pid={os.getpid()} {message}\n")
    except OSError:
        pass


def _resolve_codex_binary():
    explicit = os.environ.get("CODEX_BIN", "").strip()
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit

    discovered = shutil.which("codex")
    session_launcher = os.path.join(".codex", "tmp", "arg0")
    if discovered and session_launcher not in os.path.abspath(discovered):
        return discovered

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "codex"),
        "/usr/local/bin/codex",
        "/usr/bin/codex",
    ]

    nvm_matches = sorted(
        glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin", "codex"))
    )
    candidates.extend(reversed(nvm_matches))

    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


def _ensure_writable_dir(path, label):
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        os.makedirs(resolved, mode=0o700, exist_ok=True)
        probe_path = os.path.join(
            resolved,
            f".codex-wrapper-write-test-{os.getpid()}-{int(os.times().elapsed * 1000)}",
        )
        with open(probe_path, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe_path)
    except Exception as exc:
        raise RuntimeError(f"{label} not writable: {resolved} ({exc})") from exc
    return resolved


def _candidate_write_bases():
    explicit = os.environ.get(WRITE_BASE_ENV, "").strip()
    if explicit:
        return [explicit]

    candidates = []
    extra = os.environ.get(CANDIDATES_ENV, "").strip()
    if extra:
        for part in extra.split(os.pathsep):
            entry = part.strip()
            if entry:
                candidates.append(entry)

    home = os.path.expanduser("~")
    candidates.extend(
        [
            os.path.join(home, ".local", "share", "Codex"),
            "/tmp/Codex",
        ]
    )

    deduped = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _resolve_write_layout():
    configured_base = os.environ.get(WRITE_BASE_ENV, "").strip()
    configured_state = os.environ.get(STATE_DIR_ENV, "").strip()
    configured_output = os.environ.get(OUTPUT_DIR_ENV, "").strip()
    configured_temp = os.environ.get(TEMP_DIR_ENV, "").strip()

    if configured_state or configured_output or configured_temp:
        if not (configured_base and configured_state and configured_output and configured_temp):
            raise RuntimeError(
                "incomplete runtime path configuration; expected base/state/output/temp"
            )

        base = _ensure_writable_dir(configured_base, "write base")
        state = _ensure_writable_dir(configured_state, "state dir")
        output = _ensure_writable_dir(configured_output, "output dir")
        temp = _ensure_writable_dir(configured_temp, "temp dir")
        agent_cwd = _ensure_writable_dir(os.path.join(temp, "agent-cwd"), "agent cwd")
        return {
            "base": base,
            "state": state,
            "output": output,
            "temp": temp,
            "agent_cwd": agent_cwd,
        }

    failures = []
    for base_candidate in _candidate_write_bases():
        try:
            base = _ensure_writable_dir(base_candidate, "write base")
            state = _ensure_writable_dir(os.path.join(base, "state"), "state dir")
            output = _ensure_writable_dir(os.path.join(base, "output"), "output dir")
            temp = _ensure_writable_dir(os.path.join(base, "tmp"), "temp dir")
            agent_cwd = _ensure_writable_dir(os.path.join(temp, "agent-cwd"), "agent cwd")
            return {
                "base": base,
                "state": state,
                "output": output,
                "temp": temp,
                "agent_cwd": agent_cwd,
            }
        except RuntimeError as exc:
            failures.append(str(exc))

    detail = "; ".join(failures) if failures else "no candidate paths"
    raise RuntimeError(f"runtime write-path preflight failed: {detail}")


def _resolve_soju_transport_config(configured):
    if not configured:
        raise RuntimeError("deep mode requires --soju-transport-config")
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
    if (
        os.path.islink(configured)
        or not os.path.isfile(resolved)
        or not os.access(resolved, os.R_OK)
    ):
        raise RuntimeError("canonical history transport configuration is not readable")
    if not os.path.isfile(SOJU_TOOLS_PATH):
        raise RuntimeError("canonical history tools are unavailable")
    return resolved


def _resolve_soju_cutoff(configured):
    if not configured:
        raise RuntimeError("deep mode requires --soju-cutoff")
    value = str(configured).strip()
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("canonical history cutoff is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RuntimeError("canonical history cutoff must be UTC")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Run one Codex request with fixed production settings.",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=None,
        help=(
            "Max total seconds for the codex process. "
            f"Defaults to {DEFAULT_TIMEOUT_SECONDS} or ${TIMEOUT_ENV_VAR}."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=ALLOWED_MODES,
        default=MODE_TERRA,
        help="Execution preset to use for the Codex request.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=ALLOWED_REASONING_EFFORTS,
        default=None,
        help="Optional reasoning effort override for the selected mode.",
    )
    parser.add_argument(
        "--web-search-context-size",
        choices=ALLOWED_WEB_SEARCH_CONTEXT_SIZES,
        default=None,
        help="Optional web search context size override for the selected mode.",
    )
    parser.add_argument(
        "--soju-transport-config",
        default=None,
        help="Prevalidated private transport configuration; required only in deep mode.",
    )
    parser.add_argument(
        "--soju-cutoff",
        default=None,
        help="Trusted exclusive UTC history cutoff; required only in deep mode.",
    )
    args = parser.parse_args()

    timeout_seconds = _resolve_timeout(args.timeout)
    settings = _runtime_settings(
        args.mode,
        reasoning_effort=args.reasoning_effort,
        web_search_context_size=args.web_search_context_size,
    )
    if args.mode == MODE_DEEP:
        try:
            soju_transport_config = _resolve_soju_transport_config(
                args.soju_transport_config
            )
            soju_cutoff = _resolve_soju_cutoff(args.soju_cutoff)
        except RuntimeError as exc:
            print(str(exc)[:300], file=sys.stderr)
            return 125
    else:
        if args.soju_transport_config or args.soju_cutoff:
            print(
                "--soju-transport-config and --soju-cutoff are valid only in deep mode",
                file=sys.stderr,
            )
            return 2
        soju_transport_config = None
        soju_cutoff = None
    prompt_text = sys.stdin.read()
    if not prompt_text.strip():
        print("empty prompt", file=sys.stderr)
        return 2

    try:
        layout = _resolve_write_layout()
        exec_code_home = _resolve_exec_codex_home()
    except RuntimeError as exc:
        print(str(exc)[:300], file=sys.stderr)
        return 125

    codex_binary = _resolve_codex_binary()
    if not codex_binary:
        print("codex binary not found in PATH or known install locations", file=sys.stderr)
        return 127

    output_path = ""
    telemetry_started = None
    telemetry_started_at = None
    telemetry_stdout = ""
    telemetry_status = "not_started"
    telemetry_exit_code = None
    telemetry_request_id = uuid.uuid4().hex
    child_env = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="codex-last-message-",
            suffix=".txt",
            dir=layout["output"],
            delete=False,
        ) as handle:
            output_path = handle.name

        cmd = _exec_command(codex_binary, settings, layout, output_path)

        try:
            child_env = _codex_child_env(
                codex_binary,
                layout,
                code_home=exec_code_home,
                soju_transport_config=soju_transport_config,
                soju_cutoff=soju_cutoff,
                soju_request_id=telemetry_request_id,
            )
            _append_debug_line(
                layout,
                (
                    "launch "
                    f"binary={codex_binary!r} "
                    f"agent_cwd={layout['agent_cwd']!r} "
                    f"wrapper_cwd={os.getcwd()!r} "
                    f"stdin_tty={sys.stdin.isatty()} "
                    f"stdout_tty={sys.stdout.isatty()} "
                    f"stderr_tty={sys.stderr.isatty()} "
                    f"env={_debug_env_summary(child_env)}"
                ),
            )
            telemetry_started = time.monotonic()
            telemetry_started_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            telemetry_status = "running"
            proc = _run_with_timeout(
                cmd,
                prompt_text,
                child_env,
                timeout_seconds,
                cwd=layout["agent_cwd"],
                temp_dir=layout["temp"],
            )
        except subprocess.TimeoutExpired as exc:
            telemetry_stdout = exc.stdout or ""
            telemetry_status = "timeout"
            _append_debug_line(layout, f"timeout seconds={timeout_seconds}")
            print(f"codex timed out after {timeout_seconds}s", file=sys.stderr)
            return 124
        except FileNotFoundError:
            telemetry_status = "launch_error"
            print("codex binary not found", file=sys.stderr)
            return 127
        except OSError as exc:
            telemetry_status = "launch_error"
            print(f"failed to launch codex: {exc}", file=sys.stderr)
            return 126

        telemetry_stdout = proc.stdout or ""
        telemetry_exit_code = proc.returncode
        if proc.returncode != 0:
            telemetry_status = "codex_error"
            raw_detail = (proc.stderr or proc.stdout or "").strip()
            structured_detail = _structured_error_detail(proc.stdout)
            detail = structured_detail or raw_detail
            debug_detail = structured_detail or (
                f"unstructured stderr_chars={len(proc.stderr or '')} "
                f"stdout_chars={len(proc.stdout or '')}"
            )
            _append_debug_line(
                layout,
                f"exit returncode={proc.returncode} detail={debug_detail[-300:]}",
            )
            try:
                last_message = _read_last_message(output_path)
            except RuntimeError:
                last_message = ""
            if last_message and _has_non_fatal_rollout_error(detail):
                telemetry_status = "recovered_rollout_error"
                sys.stdout.write(last_message)
                if not last_message.endswith("\n"):
                    sys.stdout.write("\n")
                return 0
            if detail:
                detail = detail.splitlines()[-1]
            else:
                detail = f"codex exited with status {proc.returncode}"
            print(detail[:300], file=sys.stderr)
            return proc.returncode

        try:
            last_message = _read_last_message(output_path)
        except RuntimeError as exc:
            telemetry_status = "output_error"
            print(str(exc), file=sys.stderr)
            return 125

        if not last_message:
            telemetry_status = "empty_response"
            print("codex returned an empty final message", file=sys.stderr)
            return 3

        telemetry_status = "success"
        sys.stdout.write(last_message)
        if not last_message.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    finally:
        if telemetry_started is not None and child_env is not None:
            duration_ms = int((time.monotonic() - telemetry_started) * 1000)
            quota_env = dict(child_env)
            quota_env.pop(SOJU_TRANSPORT_CONFIG_ENV, None)
            quota_env.pop(SOJU_CUTOFF_ENV, None)
            quota_env.pop(SOJU_TELEMETRY_PATH_ENV, None)
            quota_env.pop(SOJU_REQUEST_ID_ENV, None)
            try:
                quota = _read_quota_snapshot(
                    codex_binary,
                    quota_env,
                    layout["agent_cwd"],
                )
            except Exception:
                quota = {"status": "unavailable", "error": "internal_error"}
            record = {
                "schema_version": 1,
                "request_id": telemetry_request_id,
                "started_at": telemetry_started_at,
                "mode": args.mode,
                "model": settings["model"],
                "reasoning_effort": settings["reasoning_effort"],
                "status": telemetry_status,
                "duration_ms": duration_ms,
                "exit_code": telemetry_exit_code,
                "tokens": _turn_usage(telemetry_stdout),
                "quota": quota,
            }
            if not _append_usage_record(layout, record):
                _append_debug_line(layout, "usage telemetry write failed")
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
