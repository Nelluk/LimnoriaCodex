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
@config plugins.codex.deepTimeoutSeconds 180
@config plugins.codex.maxContextLines 20
@config plugins.codex.persistentMemoryEnabled false
@config plugins.codex.memoryMaxExchanges 8
@config plugins.codex.cooldownSeconds 10
```

Optional channel allowlist:

```irc
@config plugins.codex.allowedChannels "#example #bots"
```

`@codexdeep` uses a deployment-owned restricted transport configured at
`data/Codex/soju-history/transport.json`. The production policy exposes only
the canonical Freenode `#debate2016` and Libera `##debate2016` lineage, and the
command is therefore available only from Libera `##debate2016`.

## Authentication

The default `exec` backend uses the normal shared Codex home at `~/.codex`.
Override it with `CODEX_WRAPPER_EXEC_CODEX_HOME` when needed. It uses the
official CLI to read and refresh that shared authentication; it does not copy
`auth.json` or write bot configuration into the shared home.

Exec requests ignore shared user configuration and rules, run without persisted
session files, and use an empty plugin-owned working directory. Local shell,
apps, plugins, hooks, browser/computer use, memories, and multi-agent features
are disabled. Hosted web search remains available for ordinary modes. Deep
mode disables web search and exposes only seven read-only canonical-history
tools through a plugin-owned local MCP server and restricted SSH transport.

The current deployment is validated with `codex-cli 0.147.0`.

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
rules and exposes no general local shell or filesystem tool to IRC prompts.
Deep mode adds only its fixed-scope, read-only canonical-history tools.

## Commands

```irc
@terra <prompt>
@terrahigh <prompt>
@terrano <prompt>
@luna <prompt>
@lunahigh <prompt>
@lunano <prompt>
@codexlong <prompt>
@codexdeep <prompt>
```

Owner-only memory commands:

```irc
@codexmem [<channel-or-nick>]
@codexreset [<channel-or-nick>]
```

Behavior summary:

- Each request is stateless.
- Recent channel or private-message context is included as untrusted context. Deep mode includes only the 25 most recent channel lines and uses them solely to resolve query ambiguity; canonical-history tools must verify historical claims.
- `@terra`/`@terrahigh` and `@luna`/`@lunahigh` expose medium- and high-reasoning model primitives.
- `@terrano` and `@lunano` use their model's higher-effort preset without including channel context or prior Codex memory in the prompt.
- `@codexlong` uses a larger in-memory transcript buffer for channel analysis, with local hour markers and per-line times.
- `@codexdeep` searches canonical Soju history for the debate2016 channel lineage and is unavailable in private messages or other channels.
- Returned history is untrusted evidence. The model receives only `search`, `search_summary`, `history_summary`, `context`, `conversations`, `speaker_history`, and `aggregate`; it cannot choose another network, target, scope, remote command, or local path.
- `history_summary` provides exact entire-scope totals and boundary messages; `search_summary` provides exact topical totals and chronological edges; `context` expands a message ID; `search` finds individual evidence; `conversations` reconstructs participant-aware exchanges; `speaker_history` samples a speaker timeline; and `aggregate` groups exact counts by sender/day/week/month/year.
- Deep requests have a 20-call history-tool budget. The prompt directs Codex to plan first, use the operation matching the question, inspect completeness/truncation metadata, combine only explicit aliases, and avoid searching for nonhistorical requests.
- The native `codex`, `codexhigh`, and `codexno` names are intentionally free for Aka aliases.
- Optional persistent memory stores timestamped successful Codex command query/reply pairs per context.
- Output is sanitized for IRC by stripping markdown, links, control characters, and excess formatting.
- Only one Codex request runs at a time; concurrent requests are rejected as busy.

## Runtime Settings

Main Limnoria registry settings:

- `timeoutSeconds`: max end-to-end runtime for one Codex request.
- `deepTimeoutSeconds`: max runtime for one `@codexdeep` request; default 180 seconds.
- `maxContextLines`: recent IRC context lines retained per channel or PM context.
- `persistentMemoryEnabled`: enables persisted successful Codex exchange memory.
- `memoryMaxExchanges`: maximum stored successful Codex exchanges per context.
- `cooldownSeconds`: per-user rate limit.
- `allowedChannels`: optional channel allowlist; empty means all channels.

Operational defaults in `plugin.py`:

- Canonical wrapper script: `scripts/codex_wrapper.py`
- Wrapper runtime base: Limnoria data directory for this plugin.
- `@terrahigh` and `@lunahigh` reasoning effort: `high`
- `@terrahigh` and `@lunahigh` web search context size: `high`
- `@luna` and `@lunahigh` use the experimental `gpt-5.6-luna` model.
- `@terrano` and `@lunano` use the matching model's high reasoning and web search settings.
- `@codexlong` context size: 1000 captured lines.
- `@codexlong` context time format: hourly local markers plus `[HH:MM]` line prefixes.
- `@codexdeep` supplies the invoking nick and a 25-line query-resolution context block so pronouns, nearby quotations, and references such as “that” can be resolved in historical questions.
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
- Hosted web search enabled through `web_search = "live"` except in deep mode.
- Exec sessions are ephemeral and use shared CLI authentication while ignoring
  shared user configuration and rules.
- Exec local tool families are disabled; the read-only sandbox is retained as
  defense in depth.

Mode-specific defaults:

- `@terra`: `gpt-5.6-terra` with `model_reasoning_effort = "medium"`
- `@terrahigh`: `gpt-5.6-terra` with `model_reasoning_effort = "high"`
- `@terrano`: `gpt-5.6-terra` with `model_reasoning_effort = "high"` without prompt context sections.
- `@luna`: `gpt-5.6-luna` with `model_reasoning_effort = "medium"`
- `@lunahigh`: `gpt-5.6-luna` with `model_reasoning_effort = "high"`
- `@lunano`: `gpt-5.6-luna` with `model_reasoning_effort = "high"` without prompt context sections.
- `@codexlong`: `gpt-5.6-luna` with `model_reasoning_effort = "high"` and transcript-analysis prompt instructions.
- `@codexdeep`: `gpt-5.6-luna` with `model_reasoning_effort = "high"`, web search disabled, and only the seven canonical Soju history tools enabled.

## Manual Wrapper Test

Default hardened Codex CLI backend using shared `~/.codex` authentication:

```bash
printf '%s\n' 'Say hello in one short sentence.' | plugins/Codex/scripts/codex_wrapper.py --timeout 90
printf '%s\n' 'Verify the latest Fedora release.' | plugins/Codex/scripts/codex_wrapper.py --timeout 90 --mode terrahigh
printf '%s\n' 'What did Alice and Bob discuss on election day?' | \
  plugins/Codex/scripts/codex_wrapper.py --timeout 180 --mode deep \
  --soju-transport-config /path/to/private/transport.json \
  --soju-cutoff '<exclusive-UTC-cutoff>'
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

## Private Usage Telemetry

Every wrapper invocation appends one private JSONL record to
`OUTPUT_DIR/usage-telemetry.jsonl` (normally
`data/Codex/output/usage-telemetry.jsonl`). The file is forced to mode `0600`
and does not contain prompts, replies, channel logs, credentials, or local
paths.
The active log rotates at 5 MB, retaining one `usage-telemetry.jsonl.1`
backup, so telemetry is bounded to roughly 10 MB.

Each record includes the mode, model, reasoning effort, result status, elapsed
time, and the exact input, cached-input, output, and reasoning-output counters
reported by `codex exec --json`. After the model process exits, the wrapper also
uses the documented app-server `account/rateLimits/read` method to record all
currently returned quota buckets, their integer used percentages, window
durations, and reset times. Bucket IDs and window types are dynamic; consumers
must not assume that every account has fixed five-hour and weekly buckets.

Quota collection has a five-second deadline and is fail-open. If app-server or
the account lookup is unavailable, the record says the quota snapshot is
unavailable while the original Codex result and IRC response remain unchanged.
Because quota percentages are integer snapshots and other Codex clients may use
the same account, differences between records are observational rather than an
exact per-request quota charge.

Deep-mode MCP calls append separate metadata-only records to
`OUTPUT_DIR/soju-tool-telemetry.jsonl`. Each record includes its UTC start time,
parent Codex request ID, sequential call index, tool/status, duration, response
size, and available count/completeness fields. Queries and returned message
bodies are never logged.

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

On the deployed production service, `@reload Codex` has proved unreliable. After
an approved production deployment, perform one full `ircbot.service` restart
and verify the new process and plugin behavior instead of relying on an in-bot
plugin reload.

- `Codex runtime path error`: verify the Limnoria process can write to its data directory.
- `codex binary not found`: install Codex CLI and ensure `codex` is on the bot process PATH.
- Authentication failures: verify the bot account can read and
  update the shared `~/.codex/auth.json`, or set
  `CODEX_WRAPPER_EXEC_CODEX_HOME` to the intended shared Codex home.
- Timeouts: increase `plugins.codex.timeoutSeconds`, shorten prompts, or reduce unnecessary context.
- Canonical-history failures: confirm the private transport configuration, identity, and pinned host data are readable by the bot account and that the restricted endpoint is reachable.
- Missing quota snapshots: inspect `usage-telemetry.jsonl`; quota collection is optional and never fails the IRC request.
- Busy replies: the plugin allows one active request at a time.

## License

MIT
