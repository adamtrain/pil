"""Where pil keeps things, and the optional config file.

Everything lives under one directory, `~/.config/pil` unless $PIL_HOME or $XDG_CONFIG_HOME
says otherwise:

    prompts/        the library: one Markdown file per prompt
    config.toml     optional settings
    state.json      what pil has learned, such as which prompts can't be shortened further

config.toml can name the claude executable and set default options for each kind of prompt,
using the same keys a prompt's own frontmatter accepts. Without it, everything runs on Opus 5.5
at high effort:

    claude = "claude"

    [transform]
    model = "haiku"
    effort = "low"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .kinds import KINDS


class ConfigError(Exception):
    pass


def home() -> Path:
    if override := os.environ.get("PIL_HOME"):
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "pil"


def prompts_dir() -> Path:
    return home() / "prompts"


def config_path() -> Path:
    return home() / "config.toml"


@dataclass(frozen=True, slots=True)
class Config:
    claude: str = "claude"
    defaults: dict[str, dict[str, Any]] = field(default_factory=dict)


def load() -> Config:
    path = config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Couldn't read {path}: {e}") from e
    defaults = {}
    for kind in KINDS:
        table = data.get(kind, {})
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [{kind}] should be a table of options.")
        defaults[kind] = table
    return Config(claude=str(data.get("claude", "claude")), defaults=defaults)
