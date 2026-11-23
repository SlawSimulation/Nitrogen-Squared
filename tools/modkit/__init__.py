"""GPL-3.0-or-later. Nitrogen's launcher-side manager for framework .psxmod packages.

The pinned PSXRecomp framework owns the package format, schema, resolution
and plugin activation. This package only installs player packages into a
durable store under .local/mods, records selections, and prepares the runtime
directory's mods/installed tree and state.toml before launch. Authoritative
schema and plugin checks are delegated to the built runtime itself through
its --audit-mod-plugins mode. See docs/MODS.md.
"""

class ModError(ValueError):
    """A package or store operation was refused; the message is player-facing."""


# Nitrogen's game identity as the framework sees it ([game] id in game.toml).
GAME_ID = "SLUS-00637"
