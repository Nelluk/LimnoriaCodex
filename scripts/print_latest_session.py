#!/usr/bin/env python3
"""Inspect the most recent Codex session JSONL file."""

import argparse
import glob
import json
import os
import sys


DEFAULT_WRITE_BASE = os.path.join(os.path.expanduser("~"), ".local", "share", "Codex")


def _default_sessions_root():
    write_base = os.environ.get("CODEX_WRAPPER_WRITE_BASE", DEFAULT_WRITE_BASE)
    return os.path.join(
        os.path.abspath(os.path.expanduser(write_base)),
        "state",
        "codex-home",
        "sessions",
    )


def _find_latest_session_file(sessions_root):
    pattern = os.path.join(os.path.abspath(os.path.expanduser(sessions_root)), "**", "*.jsonl")
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _iter_jsonl(path):
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_no, {"_parse_error": str(exc), "_raw": line}


def _extract_message_text(payload):
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message":
        return ""
    parts = []
    for item in payload.get("content", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("input_text", "output_text"):
            text = item.get("text")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _is_response_message(obj, role=None):
    if obj.get("type") != "response_item":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("type") != "message":
        return False
    if role is None:
        return True
    return payload.get("role") == role


def _print_irc_summary(events):
    user_messages = []
    assistant_messages = []

    for _, obj in events:
        if _is_response_message(obj, role="user"):
            text = _extract_message_text(obj.get("payload", {}))
            if text:
                user_messages.append(text)
        elif _is_response_message(obj, role="assistant"):
            text = _extract_message_text(obj.get("payload", {}))
            if text:
                assistant_messages.append(text)

    irc_prompt = ""
    for text in reversed(user_messages):
        if "SYSTEM INSTRUCTIONS:" in text and "USER QUERY:" in text:
            irc_prompt = text
            break

    if not irc_prompt:
        print("Could not find IRC prompt payload in latest session.", file=sys.stderr)
        return 2

    final_reply = assistant_messages[-1] if assistant_messages else ""

    print("\n--- Prompt Sent To Codex (Exact) ---")
    print(irc_prompt)
    print("\n--- Final Assistant Reply ---")
    if final_reply:
        print(final_reply)
    else:
        print("(empty)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Show IRC-relevant context from the newest Codex session file.",
    )
    parser.add_argument(
        "--sessions-root",
        default=_default_sessions_root(),
        help=(
            "Root directory containing session JSONL files "
            "(default: $CODEX_WRAPPER_WRITE_BASE/state/codex-home/sessions, "
            "or ~/.local/share/Codex/state/codex-home/sessions)"
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Show raw JSONL events instead of IRC summary view.",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Maximum events to print (0 means all).",
    )
    args = parser.parse_args()

    latest = _find_latest_session_file(args.sessions_root)
    if not latest:
        print(f"No session files found under: {args.sessions_root}", file=sys.stderr)
        return 1

    events = list(_iter_jsonl(latest))
    if not events:
        print(f"Session file is empty: {latest}", file=sys.stderr)
        return 2

    print(f"Latest session file: {latest}")

    if not args.raw:
        return _print_irc_summary(events)

    printed = 0
    for line_no, obj in events:
        printed += 1
        print(f"\n--- event {printed} (line {line_no}) ---")
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        if args.max_events > 0 and printed >= args.max_events:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
