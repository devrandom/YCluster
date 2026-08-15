"""Configuration loading for osv-feed.

TOML config files are read from a system path and a per-user path, with
the user file overriding the system file per key. Environment variables
and CLI flags override the config files. All configuration paths can be
overridden with environment variables for tests and packaging:

    OSV_FEED_SYSTEM_CONFIG  default /etc/osv-feed/config.toml
    OSV_FEED_USER_CONFIG    default ~/.config/osv-feed.toml
"""

from __future__ import annotations

import os
import tomllib

from .categories import DEFAULT_CATEGORIES
from .categories import package_belongs

DEFAULT_DB_DIR = "~/.cache/osv-feed"
DEFAULT_ACK_DB = "~/.cache/osv-feed-acks.json"
SYSTEM_CONFIG_PATH = "/etc/osv-feed/config.toml"
USER_CONFIG_PATH = "~/.config/osv-feed.toml"


def system_config_path() -> str:
    return os.environ.get("OSV_FEED_SYSTEM_CONFIG", SYSTEM_CONFIG_PATH)


def user_config_path() -> str:
    return os.path.expanduser(os.environ.get("OSV_FEED_USER_CONFIG", USER_CONFIG_PATH))


def default_db_dir() -> str:
    return os.path.expanduser(DEFAULT_DB_DIR)


def default_ack_db() -> str:
    return os.path.expanduser(DEFAULT_ACK_DB)


def _deep_merge(base: dict, update: dict) -> dict:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    """Merge system then user TOML config files into one full config."""
    merged: dict = {}
    for path in (system_config_path(), user_config_path()):
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            continue
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid TOML in {path}: {exc}") from exc
        feed = data.get("feed")
        if feed is not None and not isinstance(feed, dict):
            raise ValueError(f"{path}: [feed] must be a table")
        tracking = data.get("tracking")
        if tracking is not None and not isinstance(tracking, dict):
            raise ValueError(f"{path}: [tracking] must be a table")
        _deep_merge(merged, data)
    return merged


def _feed_value(config: dict, key: str) -> str | None:
    value = config.get("feed", {}).get(key)
    return str(value) if value else None


def resolve_db_dir(cli_value: str | None) -> str:
    """Resolve the feed DB directory honoring CLI > env > config > default."""
    if cli_value:
        return cli_value
    env = os.environ.get("OSV_FEED_DB_DIR")
    if env:
        return env
    db_dir = _feed_value(load_config(), "db_dir")
    if db_dir:
        return os.path.expanduser(db_dir)
    return default_db_dir()


def resolve_ack_db(cli_value: str | None) -> str:
    """Resolve the ack DB path honoring CLI > env > config > default."""
    if cli_value:
        return cli_value
    env = os.environ.get("OSV_FEED_ACK_DB")
    if env:
        return env
    ack_db = _feed_value(load_config(), "ack_db")
    if ack_db:
        return os.path.expanduser(ack_db)
    return default_ack_db()


def category_config(category: str, config: dict | None = None) -> dict | None:
    """Return a ``[tracking.<category>]`` table, or the built-in default.

    A user-defined category table replaces the built-in default of the same
    name; returns None only for a category with no default and no config.
    """
    cfg = load_config() if config is None else config
    tracking = cfg.get("tracking", {})
    if not isinstance(tracking, dict):
        return None
    value = tracking.get(category)
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError(f"[tracking.{category}] must be a table")
        return value
    return DEFAULT_CATEGORIES.get(category)


def tracking_categories(config: dict | None = None) -> dict[str, dict]:
    """Return category name to its table (built-in defaults + config overrides)."""
    cfg = load_config() if config is None else config
    result = dict(DEFAULT_CATEGORIES)
    tracking = cfg.get("tracking", {})
    if not isinstance(tracking, dict):
        return result
    for name, value in tracking.items():
        if not isinstance(value, dict):
            raise ValueError(f"[tracking.{name}] must be a table")
        result[str(name)] = value
    return result


def categories_for_package(package: str, config: dict | None = None) -> list[str]:
    """Return the category names whose package set includes ``package``.

    Used by ``match`` to auto-tag an uncategorized ``--pkg``/``--spec-file``
    run against every category the package belongs to.
    """
    cfg = load_config() if config is None else config
    return [
        name
        for name, table in tracking_categories(cfg).items()
        if package_belongs(package, table)
    ]
