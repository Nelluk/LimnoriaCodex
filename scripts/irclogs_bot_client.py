#!/usr/bin/env python3
"""Exec the fixed, restricted bot-to-history-host SSH transport."""

import json
import os
import re
import stat
import sys


CONFIG_ENV = "CODEX_SOJU_TRANSPORT_CONFIG"
SSH = "/usr/bin/ssh"
ALLOWED_CONFIG_FIELDS = {"destination", "identity_file", "known_hosts_file"}
DESTINATION_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$")


def _fail(message):
    print(message, file=sys.stderr)
    return 2


def _private_file(config_dir, configured, label):
    value = str(configured or "").strip()
    if not value or os.path.basename(value) != value:
        raise RuntimeError(f"invalid {label} filename")
    path = os.path.join(config_dir, value)
    if os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError(f"{label} is unavailable")
    mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    if mode & 0o077:
        raise RuntimeError(f"{label} permissions are too broad")
    return path


def _load_config():
    configured = os.environ.get(CONFIG_ENV, "").strip()
    if not configured:
        raise RuntimeError("Soju transport configuration is not set")
    path = os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
    if os.path.islink(configured) or not os.path.isfile(path):
        raise RuntimeError("Soju transport configuration is unavailable")
    mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    if mode & 0o077:
        raise RuntimeError("Soju transport configuration permissions are too broad")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Soju transport configuration is invalid") from exc
    if not isinstance(config, dict) or set(config) != ALLOWED_CONFIG_FIELDS:
        raise RuntimeError("Soju transport configuration has unexpected fields")
    destination = str(config.get("destination") or "").strip()
    if not DESTINATION_RE.fullmatch(destination):
        raise RuntimeError("Soju transport destination is invalid")
    config_dir = os.path.dirname(path)
    identity = _private_file(config_dir, config.get("identity_file"), "identity")
    known_hosts = _private_file(
        config_dir, config.get("known_hosts_file"), "known-hosts file"
    )
    return destination, identity, known_hosts


def main():
    if len(sys.argv) != 1:
        return _fail("the restricted Soju transport accepts no arguments")
    try:
        destination, identity, known_hosts = _load_config()
    except RuntimeError as exc:
        return _fail(str(exc))
    os.execv(
        SSH,
        [
            SSH,
            "-T",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-i",
            identity,
            destination,
        ],
    )


if __name__ == "__main__":
    raise SystemExit(main())
