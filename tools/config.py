"""
lolabot Configuration Loader

Resolves paths and settings from:
1. LOLABOT_HOME environment variable (required, or auto-detected)
2. notolark.yaml config file at LOLABOT_HOME root
3. Sensible defaults matching standard lolabot directory layout
"""

import os
from typing import Any, Dict, Optional

_config_cache: Optional[Dict[str, Any]] = None


def _resolve_path(relative_or_absolute: str, base: str) -> str:
    """Resolve a path: if absolute, return as-is; if relative, resolve against base."""
    if os.path.isabs(relative_or_absolute):
        return relative_or_absolute
    return os.path.join(base, relative_or_absolute)


def get_home() -> str:
    """Get LOLABOT_HOME — the PA instance root directory."""
    home = os.environ.get("LOLABOT_HOME")
    if home:
        return home
    # Auto-detect: assume tools/ is one level below LOLABOT_HOME
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config() -> Dict[str, Any]:
    """Load notolark.yaml config, with caching."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    home = get_home()
    config_path = os.path.join(home, "notolark.yaml")
    config: Dict[str, Any] = {}

    if os.path.exists(config_path):
        # Privacy-critical: if notolark.yaml exists it MUST load. Silently
        # falling back to defaults would un-namespace the company indexes
        # (writing to personal default paths). Fail loud instead.
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError(
                f"notolark.yaml exists at {config_path} but PyYAML is not "
                f"installed, so config cannot be applied and paths would "
                f"silently fall back to non-company defaults. "
                f"Run: uv pip install pyyaml"
            ) from e
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            raise RuntimeError(
                f"notolark.yaml at {config_path} failed to parse ({e}); "
                f"refusing to fall back to non-company default paths."
            ) from e

    # Inject resolved home
    config["_home"] = home

    # Resolve all paths
    paths = config.get("paths", {})
    config["_resolved"] = {
        "credentials": _resolve_path(paths.get("credentials", "brain/companies-credentials.yaml"), home),
        "emails_dir": _resolve_path(paths.get("emails_dir", "emails"), home),
        "indexes_dir": _resolve_path(paths.get("indexes_dir", "indexes"), home),
        "memory_dir": _resolve_path(paths.get("memory_dir", "memory"), home),
        "brain_dir": _resolve_path(paths.get("brain_dir", "brain"), home),
        "venv": _resolve_path(paths.get("venv", ".venv"), home),
    }

    # Resolve memory paths
    mem = config.get("memory", {})
    config["_resolved"]["long_term_index"] = _resolve_path(
        mem.get("long_term_index", "indexes/memories.mv2"), home)
    config["_resolved"]["short_term_index"] = _resolve_path(
        mem.get("short_term_index", "indexes/short-term.mv2"), home)
    config["_resolved"]["metadata_db"] = _resolve_path(
        mem.get("metadata_db", "indexes/memory_meta.db"), home)

    # Resolve file index path
    files = config.get("files", {})
    config["_resolved"]["files_index"] = _resolve_path(
        files.get("index_path", "indexes/files.mv2"), home)

    # Email index (optional)
    email = config.get("email", {})
    config["_resolved"]["emails_index"] = _resolve_path(
        email.get("index_path", "indexes/emails.mv2"), home)

    _config_cache = config
    return config


def get_path(key: str) -> str:
    """Get a resolved path by key. E.g., get_path('credentials')."""
    cfg = load_config()
    return cfg["_resolved"][key]


# ---------------------------------------------------------------------------
# Bot profiles — run N specialized bots (own Lark app identity, own port,
# own single-writer state) from ONE repo + shared corpus. The default
# profile (env unset) is the primary bot with byte-identical legacy
# behavior; NOTO_BOT_PROFILE=mail runs the dedicated mail bot.
# ---------------------------------------------------------------------------

def get_profile() -> str:
    """Active bot profile for THIS process. 'default' when unset."""
    p = (os.environ.get("NOTO_BOT_PROFILE") or "").strip().lower()
    return p or "default"


def profile_config() -> Dict[str, Any]:
    """The config `profiles.<name>` block for the active profile.
    Empty dict for the default profile (which reads the legacy top-level
    keys) or when the block isn't configured."""
    prof = get_profile()
    if prof == "default":
        return {}
    return (load_config().get("profiles") or {}).get(prof) or {}


def agent_display_name() -> str:
    """User-facing bot name for this process (per-profile override)."""
    return (profile_config().get("agent_name")
            or (load_config().get("agent") or {}).get("name")
            or "Assistant")


def state_dir() -> str:
    """Directory for this bot process's single-writer state files
    (event dedupe, bot stats, last-QA). The default profile keeps the
    legacy lark/ location; every other profile gets an isolated
    lark/profiles/<name>/ so two bot processes can never clobber each
    other's state."""
    home = get_home()
    prof = get_profile()
    if prof == "default":
        return os.path.join(home, "lark")
    d = os.path.join(home, "lark", "profiles", prof)
    os.makedirs(d, exist_ok=True)
    return d
