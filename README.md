# Codex Limnoria Plugin

Stateless IRC command integration for Codex-backed model requests.

This plugin is intentionally designed for people who have access to Codex through a
ChatGPT/Codex subscription. It does not use an OpenAI API key. Instead, the wrapper
uses the installed Codex CLI with shared ChatGPT authentication and a hardened,
plugin-owned execution workspace.

## Status

This is a small, opinionated plugin extracted from a live Limnoria bot. It uses
the supported non-interactive `codex exec` interface for subscription-backed
requests from IRC.

## Install

1. Copy this directory into your Limnoria plugin path as `plugins/Codex`.
2. Make the wrapper executable:

```bash
chmod +x plugins/Codex/scripts/codex_wrapper.py
```

3. Load the plugin in Limnoria:

```irc
@load Codex
```

4. Configure the main registry settings as needed:

```irc
@config plugins.codex.timeoutSeconds 90
@config plugins.codex.maxContextLines 20
@config plugins.codex.persistentMemoryEnabled false
@config plugins.codex.memoryMaxExchanges 8
@config plugins.codex.cooldownSeconds 10
```

Optional channel allowlist:

```irc
@config plugins.codex.allowedChannels "#example #bots"
```

## Authentication

The default `exec` backend uses the normal shared Codex home at `~/.codex`.
Override it with `CODEX_WRAPPER_EXEC_CODEX_HOME` when needed. It uses the
official CLI to read and refresh that shared authentication; it does not copy
`auth.json` or write bot configuration into the shared home.

Exec requests ignore shared user configuration and rules, run without persisted
session files, and use an empty plugin-owned working directory. Local shell,
apps, plugins, hooks, browser/computer use, memories, and multi-agent features
are disabled. Hosted web search remains available.

The current deployment is validated with `codex-cli 0.144.1`.

### systemd deployment

If the bot service uses `ProtectHome=read-only`, grant the service write access
to the shared Codex home so the official CLI can refresh authentication. For
example, `/etc/systemd/system/ircbot.service.d/codex-auth.conf`:

```ini
[Service]
ReadWritePaths=/home/nelluk/.codex
```

Then run `systemctl daemon-reload` and restart the bot service. Other home paths
remain read-only. The Codex child still ignores shared user configuration and
rules and exposes no local shell or filesystem tool to IRC prompts.

## Commands

```irc
@codex <prompt>
@codexhigh <prompt>
@codexno <prompt>
@codexlong <prompt>
```

Owner-only memory commands:

```irc
@codexmem [<channel-or-nick>]
@codexreset [<channel-or-nick>]
```

Behavior summary:

- Each request is stateless.
- Recent channel or private-message context is included as untrusted context.
- `@codexhigh` uses a higher-effort Codex preset.
- `@codexno` uses the higher-effort preset without including channel context or prior Codex memory in the prompt.
- `@codexlong` uses a larger in-memory transcript buffer for channel analysis, with local hour markers and per-line times.
- Optional persistent memory stores timestamped successful Codex command query/reply pairs per context.
- Output is sanitized for IRC by stripping markdown, links, control characters, and excess formatting.
- Only one Codex request runs at a time; concurrent requests are rejected as busy.

## Runtime Settings

Main Limnoria registry settings:

- `timeoutSeconds`: max end-to-end runtime for one Codex request.
- `maxContextLines`: recent IRC context lines retained per channel or PM context.
- `persistentMemoryEnabled`: enables persisted successful Codex exchange memory.
- `memoryMaxExchanges`: maximum stored successful Codex exchanges per context.
- `cooldownSeconds`: per-user rate limit.
- `allowedChannels`: optional channel allowlist; empty means all channels.

Operational defaults in `plugin.py`:

- Wrapper script: `scripts/codex_wrapper.py`
- Wrapper runtime base: Limnoria data directory for this plugin.
- `@codexhigh` reasoning effort: `high`
- `@codexhigh` web search context size: `high`
- `@luna` and `@lunahigh` use the experimental `gpt-5.6-luna` model.
- `@codexno` reasoning and web search context settings: same as `@codexhigh`.
- `@codexlong` context size: 1000 captured lines.
- `@codexlong` context time format: hourly local markers plus `[HH:MM]` line prefixes.
- Captured IRC line cap: 200 chars per line.
- Reply cap: 1200 chars total.
- Memory age cap: 72 hours.
- Memory field cap: 280 chars per query/reply field.
- Concurrency: one active Codex request.

Codex runtime defaults in `scripts/codex_wrapper.py`:

- Backend: hardened `codex exec`.
- Default model: `gpt-5.6-terra`; Luna model: `gpt-5.6-luna`.
- `model_reasoning_summary = "none"`
- `model_verbosity = "low"`
- Hosted web search enabled through `web_search = "live"`.
- Exec sessions are ephemeral and use shared CLI authentication while ignoring
  shared user configuration and rules.
- Exec local tool families are disabled; the read-only sandbox is retained as
  defense in depth.

Mode-specific defaults:

- `@codex`: `model_reasoning_effort = "medium"`
- `@codexhigh`: `model_reasoning_effort = "high"`
- `@luna`: `gpt-5.6-luna` with `model_reasoning_effort = "medium"`
- `@lunahigh`: `gpt-5.6-luna` with `model_reasoning_effort = "high"`
- `@codexno`: `model_reasoning_effort = "high"` without prompt context sections.
- `@codexlong`: `gpt-5.6-luna` with `model_reasoning_effort = "high"` and transcript-analysis prompt instructions.

## Manual Wrapper Test

Default hardened Codex CLI backend using shared `~/.codex` authentication:

```bash
printf '%s\n' 'Say hello in one short sentence.' | plugins/Codex/scripts/codex_wrapper.py --timeout 90
printf '%s\n' 'Verify the latest Fedora release.' | plugins/Codex/scripts/codex_wrapper.py --timeout 90 --mode high
```

Custom shared Codex home:

```bash
printf '%s\n' 'Say hello.' | \
  CODEX_WRAPPER_EXEC_CODEX_HOME=/path/to/shared/.codex \
  plugins/Codex/scripts/codex_wrapper.py --timeout 90
```

Custom runtime base:

```bash
CODEX_WRAPPER_WRITE_BASE=/path/to/runtime \
CODEX_WRAPPER_STATE_DIR=/path/to/runtime/state \
CODEX_WRAPPER_OUTPUT_DIR=/path/to/runtime/output \
CODEX_WRAPPER_TEMP_DIR=/path/to/runtime/tmp \
plugins/Codex/scripts/codex_wrapper.py --timeout 90
```

## Tests

Run tests from a writable directory so Limnoria can create its relative logs path:

```bash
PYTHONPATH=/path/to/Limnoria-parent python -m unittest plugins.Codex.test
```

For a bot installed at `/opt/limnoria` with plugins under `/opt/limnoria/plugins`,
that usually means:

```bash
cd /tmp
PYTHONPATH=/opt/limnoria /opt/limnoria/bin/python -m unittest plugins.Codex.test
```

## Troubleshooting

- `Codex runtime path error`: verify the Limnoria process can write to its data directory.
- `codex binary not found`: install Codex CLI and ensure `codex` is on the bot process PATH.
- Authentication failures: verify the bot account can read and
  update the shared `~/.codex/auth.json`, or set
  `CODEX_WRAPPER_EXEC_CODEX_HOME` to the intended shared Codex home.
- Timeouts: increase `plugins.codex.timeoutSeconds`, shorten prompts, or reduce unnecessary context.
- Busy replies: the plugin allows one active request at a time.

## License

MIT
