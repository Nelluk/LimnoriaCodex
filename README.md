# Codex Limnoria Plugin

Stateless IRC command integration for Codex-backed model requests.

This plugin is intentionally designed for people who have access to Codex through a
ChatGPT/Codex subscription. It does not use an OpenAI API key. Instead, the wrapper
uses Codex CLI-style ChatGPT authentication state from a local `auth.json` runtime
home, refreshes that state as needed, and sends requests to the Codex backend.

## Status

This is a small, opinionated plugin extracted from a live Limnoria bot. It is useful
as a working reference for subscription-backed Codex usage from IRC, but the
authentication path depends on Codex/ChatGPT internals rather than the public
OpenAI API-key workflow.

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

The default backend expects a Codex/ChatGPT `auth.json` in the wrapper runtime home.
By default, the plugin creates that runtime under Limnoria's data directory for the
plugin, typically equivalent to:

```text
data/Codex/state/codex-home
```

On a headless server, log in with device auth against that runtime home:

```bash
CODEX_HOME=/path/to/limnoria/data/Codex/state/codex-home codex login --device-auth
```

The wrapper keeps its own runtime `config.toml`, uses file-backed auth, and does
not import trusted Codex project settings from the source `CODEX_HOME`.

If you already have a Codex CLI home and want to bootstrap the runtime from it, set
`CODEX_WRAPPER_CODEX_HOME_SOURCE` for the wrapper process. This is only a bootstrap
copy for files such as `auth.json`; it is not a continuing sync mechanism.

## Commands

```irc
@codex <prompt>
@codexhigh <prompt>
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
- `@codexhigh` reasoning effort: `medium`
- `@codexhigh` web search context size: `high`
- `@codexlong` context size: 1000 captured lines.
- `@codexlong` context time format: hourly local markers plus `[HH:MM]` line prefixes.
- Captured IRC line cap: 200 chars per line.
- Reply cap: 1200 chars total.
- Memory age cap: 72 hours.
- Memory field cap: 280 chars per query/reply field.
- Concurrency: one active Codex request.

Codex runtime defaults in `scripts/codex_wrapper.py`:

- `model = "gpt-5.5"`
- `model_reasoning_summary = "none"`
- `model_verbosity = "low"`
- Hosted web search tool enabled for the default direct backend.
- Legacy exec backend only: `web_search = "live"`

Mode-specific defaults:

- `@codex`: `model_reasoning_effort = "low"`
- `@codexhigh`: `model_reasoning_effort = "medium"`
- `@codexlong`: `model_reasoning_effort = "medium"` with transcript-analysis prompt instructions.

## Manual Wrapper Test

Default direct Codex backend:

```bash
printf '%s\n' 'Say hello in one short sentence.' | plugins/Codex/scripts/codex_wrapper.py --timeout 90
printf '%s\n' 'Verify the latest Fedora release.' | plugins/Codex/scripts/codex_wrapper.py --timeout 90 --mode high
```

Legacy Codex CLI backend:

```bash
printf '%s\n' 'Say hello in one short sentence.' | CODEX_WRAPPER_BACKEND=exec plugins/Codex/scripts/codex_wrapper.py --timeout 90
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
- `codex binary not found`: this applies only to `CODEX_WRAPPER_BACKEND=exec`; install Codex CLI and ensure `codex` is on the bot process PATH.
- Authentication failures: re-run `codex login --device-auth` with `CODEX_HOME` pointed at the plugin runtime home.
- Timeouts: increase `plugins.codex.timeoutSeconds`, shorten prompts, or reduce unnecessary context.
- Busy replies: the plugin allows one active request at a time.

## License

MIT
