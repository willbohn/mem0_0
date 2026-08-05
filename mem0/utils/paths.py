from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def resolve_mem0_dir(
    environ: Mapping[str, str] | None = None,
    *,
    home: str | Path | None = None,
) -> Path:
    """Resolve local state without abandoning an existing pre-fork directory.

    ``MEM0_DIR`` remains authoritative. Existing ``~/.mem0_0`` state wins next;
    existing ``~/.mem0`` state remains in place until an explicit migration;
    otherwise fresh installs initialize under the fork's ``~/.mem0_0`` name.
    """

    environment = os.environ if environ is None else environ
    explicit = environment.get("MEM0_DIR")
    if explicit:
        return Path(explicit).expanduser()

    home_path = Path.home() if home is None else Path(home).expanduser()
    fork_path = home_path / ".mem0_0"
    legacy_path = home_path / ".mem0"
    if fork_path.exists():
        return fork_path
    if legacy_path.exists():
        return legacy_path
    return fork_path
