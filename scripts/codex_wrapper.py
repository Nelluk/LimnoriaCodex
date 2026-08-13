#!/usr/bin/env python3
"""Run one stateless Codex request and print only the final assistant message."""

import argparse
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
from datetime import datetime


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
        "deep_logs": True,
    },
}

DEEP_TOOLS_PATH = os.path.join(os.path.dirname(__file__), "deep_log_tools.py")
DEEP_TOOL_NAMES = ("list_log_files", "search_logs", "read_log_lines")


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
    codex_binary, layout, code_home, deep_log_dir=None, deep_log_cutoff=None
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
    if deep_log_dir:
        run_env["CODEX_DEEP_LOG_DIR"] = deep_log_dir
    if deep_log_cutoff:
        run_env["CODEX_DEEP_LOG_CUTOFF"] = deep_log_cutoff
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

    if settings.get("deep_logs"):
        enabled_tools = ",".join(_toml_string(name) for name in DEEP_TOOL_NAMES)
        cmd.extend(
            (
                "-c",
                f"mcp_servers.channel_logs.command={_toml_string(sys.executable)}",
                "-c",
                f"mcp_servers.channel_logs.args=[{_toml_string(DEEP_TOOLS_PATH)}]",
                "-c",
                'mcp_servers.channel_logs.env_vars=["CODEX_DEEP_LOG_DIR","CODEX_DEEP_LOG_CUTOFF"]',
                "-c",
                "mcp_servers.channel_logs.required=true",
                "-c",
                f"mcp_servers.channel_logs.enabled_tools=[{enabled_tools}]",
                "-c",
                'mcp_servers.channel_logs.default_tools_approval_mode="approve"',
                "-c",
                "mcp_servers.channel_logs.startup_timeout_sec=10",
                "-c",
                "mcp_servers.channel_logs.tool_timeout_sec=30",
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


def _resolve_deep_log_dir(configured):
    if not configured:
        raise RuntimeError("deep mode requires --log-dir")
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
    if not os.path.isdir(resolved) or not os.access(resolved, os.R_OK | os.X_OK):
        raise RuntimeError("deep log directory is not readable")
    if not os.path.isfile(DEEP_TOOLS_PATH):
        raise RuntimeError("deep log tools are unavailable")
    return resolved


def _resolve_deep_log_cutoff(configured):
    if not configured:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    try:
        datetime.strptime(configured, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("deep log cutoff is invalid") from exc
    return configured


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
        "--log-dir",
        default=None,
        help="Prevalidated current-channel log directory; required only in deep mode.",
    )
    parser.add_argument(
        "--log-cutoff",
        default=None,
        help="Optional local timestamp immediately before the request; defaults to now in deep mode.",
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
            deep_log_dir = _resolve_deep_log_dir(args.log_dir)
            deep_log_cutoff = _resolve_deep_log_cutoff(args.log_cutoff)
        except RuntimeError as exc:
            print(str(exc)[:300], file=sys.stderr)
            return 125
    else:
        if args.log_dir or args.log_cutoff:
            print("--log-dir and --log-cutoff are valid only in deep mode", file=sys.stderr)
            return 2
        deep_log_dir = None
        deep_log_cutoff = None
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
                deep_log_dir=deep_log_dir,
                deep_log_cutoff=deep_log_cutoff,
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
            proc = _run_with_timeout(
                cmd,
                prompt_text,
                child_env,
                timeout_seconds,
                cwd=layout["agent_cwd"],
                temp_dir=layout["temp"],
            )
        except subprocess.TimeoutExpired:
            _append_debug_line(layout, f"timeout seconds={timeout_seconds}")
            print(f"codex timed out after {timeout_seconds}s", file=sys.stderr)
            return 124
        except FileNotFoundError:
            print("codex binary not found", file=sys.stderr)
            return 127
        except OSError as exc:
            print(f"failed to launch codex: {exc}", file=sys.stderr)
            return 126

        if proc.returncode != 0:
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
