"""Configuration for Codex plugin."""

import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    from supybot.questions import expect, anything, something, yn  # noqa: F401

    conf.registerPlugin("Codex", True)


Codex = conf.registerPlugin("Codex")

conf.registerGlobalValue(
    Codex,
    "timeoutSeconds",
    registry.PositiveInteger(
        90,
        """Maximum time in seconds allowed for one Codex request.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "deepTimeoutSeconds",
    registry.PositiveInteger(
        180,
        """Maximum time in seconds allowed for one Codex deep-log request.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "maxContextLines",
    registry.PositiveInteger(
        20,
        """Maximum number of recent channel lines retained per channel.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "knownBotNicks",
    registry.SpaceSeparatedListOfStrings(
        ["HenryClay", "ne2", "ne2`"],
        """Trusted bot nicknames excluded by default from human-focused deep-history results.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "persistentMemoryEnabled",
    registry.Boolean(
        False,
        """Enable persistent Codex exchange memory per channel/PM context.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "memoryMaxExchanges",
    registry.PositiveInteger(
        8,
        """Maximum stored successful Codex exchanges per context.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "cooldownSeconds",
    registry.NonNegativeInteger(
        10,
        """Per-user cooldown between Codex requests.""",
    ),
)

conf.registerGlobalValue(
    Codex,
    "allowedChannels",
    registry.SpaceSeparatedListOfStrings(
        [],
        """Optional channel allowlist. Empty means all channels.""",
    ),
)

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
