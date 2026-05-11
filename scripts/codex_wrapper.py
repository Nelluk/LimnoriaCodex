#!/usr/bin/env python3
"""Run one stateless Codex request and print only the final assistant message."""

import argparse
import base64
import glob
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request


DEFAULT_TIMEOUT_SECONDS = 90
TIMEOUT_ENV_VAR = "CODEX_WRAPPER_TIMEOUT"
WRITE_BASE_ENV = "CODEX_WRAPPER_WRITE_BASE"
STATE_DIR_ENV = "CODEX_WRAPPER_STATE_DIR"
OUTPUT_DIR_ENV = "CODEX_WRAPPER_OUTPUT_DIR"
TEMP_DIR_ENV = "CODEX_WRAPPER_TEMP_DIR"
CANDIDATES_ENV = "CODEX_WRAPPER_CANDIDATES"
CODEX_HOME_SOURCE_ENV = "CODEX_WRAPPER_CODEX_HOME_SOURCE"
MODE_NORMAL = "normal"
MODE_HIGH = "high"
ALLOWED_MODES = (MODE_NORMAL, MODE_HIGH)
ALLOWED_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
ALLOWED_WEB_SEARCH_CONTEXT_SIZES = ("low", "medium", "high")
NON_FATAL_ROLLOUT_ERROR = "failed to record rollout items:"
REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_SKEW_SECONDS = 30
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
BACKEND_ENV_VAR = "CODEX_WRAPPER_BACKEND"
MODE_PRESETS = {
    MODE_NORMAL: {
        "model": "gpt-5.5",
        "reasoning_effort": "low",
        "web_search": "live",
        "web_search_context_size": None,
    },
    MODE_HIGH: {
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "web_search": "live",
        "web_search_context_size": "high",
    },
}


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
    normalized_mode = str(mode or MODE_NORMAL).strip().lower()
    if normalized_mode not in ALLOWED_MODES:
        raise RuntimeError(f"unsupported mode: {mode}")

    preset = dict(MODE_PRESETS[normalized_mode])
    if reasoning_effort is not None:
        preset["reasoning_effort"] = reasoning_effort
    if web_search_context_size is not None:
        preset["web_search_context_size"] = web_search_context_size
    return preset


def _runtime_config_text(settings):
    lines = [
        f'model = "{settings["model"]}"',
        f'model_reasoning_effort = "{settings["reasoning_effort"]}"',
        'model_reasoning_summary = "none"',
        'model_verbosity = "low"',
        f'web_search = "{settings["web_search"]}"',
        "network_access = true",
        'cli_auth_credentials_store = "file"',
    ]
    context_size = settings.get("web_search_context_size")
    if context_size:
        lines.append(f'tools.web_search = {{ context_size = "{context_size}" }}')
    return "\n".join(lines) + "\n"


def _jwt_exp(token):
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        return payload.get("exp")
    except Exception:
        return None


def _read_codex_auth(code_home):
    auth_path = os.path.join(code_home, "auth.json")
    try:
        with open(auth_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise RuntimeError(f"failed to read Codex auth file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse Codex auth file: {exc}") from exc

    if data.get("auth_mode") != "chatgpt":
        raise RuntimeError("Codex auth file is not using ChatGPT auth mode")

    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise RuntimeError("Codex auth file does not contain a ChatGPT access token")
    return auth_path, data, tokens


def _write_codex_auth(auth_path, data):
    tmp_path = auth_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, auth_path)
    os.chmod(auth_path, 0o600)


def _refresh_codex_tokens(refresh_token, timeout_seconds):
    body = json.dumps(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        REFRESH_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codex token refresh failed: HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Codex token refresh failed: {exc}") from None


def _borrow_codex_key(code_home, timeout_seconds):
    auth_path, data, tokens = _read_codex_auth(code_home)
    access_token = tokens["access_token"]
    exp = _jwt_exp(access_token)
    if exp is None or time.time() < (float(exp) - REFRESH_SKEW_SECONDS):
        return access_token, tokens.get("account_id")

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Codex auth file does not contain a refresh token")

    refreshed = _refresh_codex_tokens(refresh_token, timeout_seconds)
    for key in ("access_token", "id_token", "refresh_token"):
        if refreshed.get(key):
            tokens[key] = refreshed[key]
    data["tokens"] = tokens
    data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    _write_codex_auth(auth_path, data)
    return tokens["access_token"], tokens.get("account_id")


def _extract_response_text(payload):
    if not isinstance(payload, dict):
        return ""

    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in ("output_text", "text"):
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks).strip()


def _codex_api_body(prompt_text, settings):
    body = {
        "model": settings["model"],
        "input": [{"role": "user", "content": prompt_text}],
        "instructions": "You are a helpful assistant.",
        "store": False,
        "reasoning": {"effort": settings["reasoning_effort"]},
        "text": {"verbosity": "low"},
        "stream": True,
    }
    body["tools"] = [{"type": "web_search"}]
    return body


def _codex_api_request(prompt_text, settings, layout, timeout_seconds):
    token, account_id = _borrow_codex_key(layout["code_home"], min(timeout_seconds, 30))
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id

    req = urllib.request.Request(
        f"{CODEX_BASE_URL}/responses",
        data=json.dumps(_codex_api_body(prompt_text, settings)).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            chunks = []
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        chunks.append(delta)
                elif event_type == "response.completed":
                    text = "".join(chunks).strip()
                    if text:
                        return text
                    response = event.get("response")
                    text = _extract_response_text(response)
                    if text:
                        return text
                elif event_type == "response.failed":
                    error = event.get("error") or {}
                    raise RuntimeError(f"Codex API response failed: {error}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codex API request failed: HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Codex API request failed: {exc}") from None
    if chunks:
        return "".join(chunks).strip()
    raise RuntimeError("Codex API response did not contain output text")


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


def _codex_child_env(codex_binary, layout):
    run_env = os.environ.copy()
    for key in list(run_env):
        if key.startswith("CODEX_"):
            run_env.pop(key, None)

    codex_dir = os.path.dirname(codex_binary)
    run_env["PATH"] = codex_dir + os.pathsep + run_env.get("PATH", "")
    run_env["CODEX_HOME"] = layout["code_home"]
    run_env["TMPDIR"] = layout["temp"]
    run_env["TMP"] = layout["temp"]
    run_env["TEMP"] = layout["temp"]
    run_env.setdefault("TERM", "dumb")
    run_env.setdefault("NO_COLOR", "1")
    run_env["PWD"] = layout["agent_cwd"]
    return run_env


def _read_last_message(output_path):
    try:
        with open(output_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise RuntimeError(f"failed to read codex output: {exc}") from exc


def _has_non_fatal_rollout_error(text):
    return NON_FATAL_ROLLOUT_ERROR in (text or "")


def _last_nonempty_lines(text, limit=6):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-limit:]


def _timeout_detail(exc):
    detail_lines = _last_nonempty_lines(getattr(exc, "stderr", "") or "")
    if not detail_lines:
        detail_lines = _last_nonempty_lines(getattr(exc, "stdout", "") or "")
    if not detail_lines:
        return ""
    return " | last output: " + " | ".join(detail_lines)


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
    if discovered:
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
        code_home = _ensure_writable_dir(os.path.join(state, "codex-home"), "CODEX_HOME")
        agent_cwd = _ensure_writable_dir(os.path.join(temp, "agent-cwd"), "agent cwd")
        return {
            "base": base,
            "state": state,
            "output": output,
            "temp": temp,
            "code_home": code_home,
            "agent_cwd": agent_cwd,
        }

    failures = []
    for base_candidate in _candidate_write_bases():
        try:
            base = _ensure_writable_dir(base_candidate, "write base")
            state = _ensure_writable_dir(os.path.join(base, "state"), "state dir")
            output = _ensure_writable_dir(os.path.join(base, "output"), "output dir")
            temp = _ensure_writable_dir(os.path.join(base, "tmp"), "temp dir")
            code_home = _ensure_writable_dir(os.path.join(state, "codex-home"), "CODEX_HOME")
            agent_cwd = _ensure_writable_dir(os.path.join(temp, "agent-cwd"), "agent cwd")
            return {
                "base": base,
                "state": state,
                "output": output,
                "temp": temp,
                "code_home": code_home,
                "agent_cwd": agent_cwd,
            }
        except RuntimeError as exc:
            failures.append(str(exc))

    detail = "; ".join(failures) if failures else "no candidate paths"
    raise RuntimeError(f"runtime write-path preflight failed: {detail}")


def _prepare_codex_home(runtime_codex_home, config_text):
    source_home = os.environ.get(CODEX_HOME_SOURCE_ENV, "").strip()
    if not source_home:
        source_home = os.path.join(os.path.expanduser("~"), ".codex")
    source_home = os.path.abspath(os.path.expanduser(source_home))
    runtime_codex_home = os.path.abspath(os.path.expanduser(runtime_codex_home))

    if source_home != runtime_codex_home:
        for filename in ("auth.json", "version.json", "models_cache.json"):
            source_file = os.path.join(source_home, filename)
            target_file = os.path.join(runtime_codex_home, filename)
            # Source homes are bootstrap-only. Once a runtime file exists,
            # preserve it so Codex can refresh tokens in place over time.
            if os.path.isfile(target_file) or not os.path.isfile(source_file):
                continue
            try:
                shutil.copy2(source_file, target_file)
            except OSError as exc:
                raise RuntimeError(
                    f"failed to stage {filename} into runtime CODEX_HOME: {exc}"
                ) from exc

    config_path = os.path.join(runtime_codex_home, "config.toml")
    try:
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(config_text)
    except OSError as exc:
        raise RuntimeError(
            f"failed to write hardened runtime config.toml: {exc}"
        ) from exc


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
        default=MODE_NORMAL,
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
    args = parser.parse_args()

    timeout_seconds = _resolve_timeout(args.timeout)
    settings = _runtime_settings(
        args.mode,
        reasoning_effort=args.reasoning_effort,
        web_search_context_size=args.web_search_context_size,
    )
    prompt_text = sys.stdin.read()
    if not prompt_text.strip():
        print("empty prompt", file=sys.stderr)
        return 2

    backend = os.environ.get(BACKEND_ENV_VAR, "api").strip().lower()
    if backend not in ("api", "exec"):
        print(f"unsupported Codex backend: {backend}", file=sys.stderr)
        return 2

    try:
        layout = _resolve_write_layout()
        _prepare_codex_home(layout["code_home"], _runtime_config_text(settings))
    except RuntimeError as exc:
        print(str(exc)[:300], file=sys.stderr)
        return 125

    if backend == "api":
        _append_debug_line(
            layout,
            (
                "api_request "
                f"model={settings['model']!r} "
                f"mode={args.mode!r} "
                f"wrapper_cwd={os.getcwd()!r} "
                f"stdin_tty={sys.stdin.isatty()} "
                f"stdout_tty={sys.stdout.isatty()} "
                f"stderr_tty={sys.stderr.isatty()}"
            ),
        )
        try:
            response_text = _codex_api_request(
                prompt_text,
                settings,
                layout,
                timeout_seconds,
            )
        except RuntimeError as exc:
            _append_debug_line(layout, f"api_error detail={str(exc)[:240]!r}")
            print(str(exc)[:300], file=sys.stderr)
            return 1

        sys.stdout.write(response_text)
        if not response_text.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    codex_binary = _resolve_codex_binary()
    if not codex_binary:
        print("codex binary not found in PATH or known install locations", file=sys.stderr)
        return 127

    output_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="codex-last-message-",
            suffix=".txt",
            dir=layout["output"],
            delete=False,
        ) as handle:
            output_path = handle.name

        cmd = [
            codex_binary,
            "exec",
            "-m",
            settings["model"],
            "-s",
            "read-only",
            "-C",
            layout["agent_cwd"],
            "--ignore-user-config",
            "--ignore-rules",
            # codex-cli 0.101.0 accepted values:
            # - model_reasoning_effort: none|minimal|low|medium|high|xhigh
            # - model_verbosity: low|medium|high
            # Note: with web_search="live", reasoning_effort="minimal" is rejected.
            "-c",
            f'model_reasoning_effort="{settings["reasoning_effort"]}"',
            "-c",
            'model_reasoning_summary="none"',
            "-c",
            'model_verbosity="low"',
            "-c",
            f'web_search="{settings["web_search"]}"',
            "--skip-git-repo-check",
            "--output-last-message",
            output_path,
            "-",
        ]
        context_size = settings.get("web_search_context_size")
        if context_size:
            cmd[cmd.index("--skip-git-repo-check"):cmd.index("--skip-git-repo-check")] = [
                "-c",
                f'tools.web_search.context_size="{context_size}"',
            ]

        try:
            child_env = _codex_child_env(codex_binary, layout)
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
            proc = _run_with_timeout(
                cmd,
                prompt_text,
                child_env,
                timeout_seconds,
                cwd=layout["agent_cwd"],
                temp_dir=layout["temp"],
            )
        except subprocess.TimeoutExpired as exc:
            _append_debug_line(layout, f"timeout seconds={timeout_seconds}{_timeout_detail(exc)}")
            print(
                f"codex timed out after {timeout_seconds}s{_timeout_detail(exc)}",
                file=sys.stderr,
            )
            return 124
        except FileNotFoundError:
            print("codex binary not found", file=sys.stderr)
            return 127
        except OSError as exc:
            print(f"failed to launch codex: {exc}", file=sys.stderr)
            return 126

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            _append_debug_line(
                layout,
                f"exit returncode={proc.returncode} detail_tail={_timeout_detail(proc)}",
            )
            try:
                last_message = _read_last_message(output_path)
            except RuntimeError:
                last_message = ""
            if last_message and _has_non_fatal_rollout_error(detail):
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
            print(str(exc), file=sys.stderr)
            return 125

        if not last_message:
            print("codex returned an empty final message", file=sys.stderr)
            return 3

        sys.stdout.write(last_message)
        if not last_message.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    finally:
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
