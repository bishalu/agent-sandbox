"""Mirror the host's Claude Code plugins into a sandbox (spec R-24).

Skills ride along as read-only mounts (R-19), but a plugin is not a skill
directory: Claude Code finds plugins through two registries under
`~/.claude/plugins/` (`installed_plugins.json`, `known_marketplaces.json`)
whose entries are absolute host paths into the plugin cache and into each
marketplace's checkout. Copying the registries into the agent home and
mounting every directory they point at, read-only, at its *host* path makes
those absolute paths resolve inside the container unchanged. The agent home's
`settings.json` then gets the host's `enabledPlugins` map merged in, because
Claude Code only loads a plugin that is both installed and enabled.

Nothing here is a credential: the cache holds skill files and manifests.
The registries are copied, never mounted, so the sandbox can write its own
plugin state without touching the host's. Mirroring runs on every `run` and
`enter`, so a plugin installed on the host after the sandbox was created shows
up on the next start. `mirror_plugins: false` in config.json turns it off.
"""

import json
import os
import pathlib
import shutil

from . import config
from .mounts import Mount

HOST_PLUGINS = config.HOME / ".claude" / "plugins"
HOST_SETTINGS = config.HOME / ".claude" / "settings.json"
REGISTRIES = ("installed_plugins.json", "known_marketplaces.json")


def enabled(cfg):
    return bool(config.resolve("mirror_plugins", None, cfg))


def _load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _install_dirs():
    """Every directory the two registries point at, deduplicated, existing only."""
    dirs = []
    installed = _load(HOST_PLUGINS / "installed_plugins.json").get("plugins", {})
    for entries in installed.values():
        for e in entries if isinstance(entries, list) else [entries]:
            p = (e or {}).get("installPath")
            if p:
                dirs.append(pathlib.Path(p))
    for m in _load(HOST_PLUGINS / "known_marketplaces.json").values():
        p = (m or {}).get("installLocation")
        if p:
            dirs.append(pathlib.Path(p))
    return dirs


def mounts(cfg):
    """Read-only mounts, each at its host path, covering every registry target.

    The cache and marketplaces directories are mounted whole when they exist,
    which covers most targets in two mounts; anything outside them (a
    marketplace that is a plain directory elsewhere on disk) is mounted on
    its own. A target that no longer exists is skipped rather than failing
    the run: the registry entry is stale on the host too.
    """
    if not enabled(cfg) or not (HOST_PLUGINS / "installed_plugins.json").is_file():
        return []
    roots = [HOST_PLUGINS / "cache", HOST_PLUGINS / "marketplaces"]
    out = [Mount(r, str(r), read_only=True, purpose="plugins") for r in roots if r.is_dir()]
    covered = [r.resolve() for r in roots if r.is_dir()]
    seen = set()
    for d in _install_dirs():
        real = d.resolve()
        if not real.is_dir() or real in seen:
            continue
        if any(real == c or c in real.parents for c in covered):
            continue
        seen.add(real)
        out.append(Mount(real, str(d), read_only=True, purpose="plugins"))
    return out


def sync(home, cfg):
    """Copy the registries and merge enabledPlugins into the sandbox settings.

    Returns the list of plugin names enabled, for disclosure. Idempotent.
    """
    if not enabled(cfg) or not (HOST_PLUGINS / "installed_plugins.json").is_file():
        return []
    dest = home.claude / "plugins"
    dest.mkdir(mode=0o700, exist_ok=True)
    for name in REGISTRIES:
        src = HOST_PLUGINS / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    enabled_map = _load(HOST_SETTINGS).get("enabledPlugins", {}) or {}
    settings_file = home.claude / "settings.json"
    settings = _load(settings_file)
    merged = dict(settings.get("enabledPlugins", {}) or {})
    merged.update(enabled_map)
    if merged != settings.get("enabledPlugins"):
        settings["enabledPlugins"] = merged
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    return sorted(k for k, v in merged.items() if v)
