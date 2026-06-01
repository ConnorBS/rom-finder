"""Loads .py extension files from extensions_dir and registers their sources."""

import importlib.util
from pathlib import Path
from typing import Any

from app.services.sources import registry as source_registry
from app.services.cover_sources import registry as cover_source_registry
from app.services.download_clients import registry as download_client_registry

# ext_id -> list of setting schema dicts from EXTENSION_SETTINGS
_settings_schemas: dict[str, list] = {}

# ext_id -> loaded module reference (for configure() calls)
_loaded_modules: dict[str, Any] = {}


def load_extension_file(ext_path: Path, config: dict | None = None) -> dict | None:
    """Import one extension .py, register it, call configure() if defined, return EXTENSION_INFO."""
    try:
        spec = importlib.util.spec_from_file_location(
            f"romfinder_ext_{ext_path.stem}", ext_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        info = getattr(module, "EXTENSION_INFO", None)
        if not isinstance(info, dict) or "id" not in info or "type" not in info:
            return None

        ext_id = info["id"]
        ext_type = info["type"]

        if ext_type == "rom_source":
            cls = getattr(module, "SOURCE_CLASS", None)
            if cls is None:
                return None
            instance = cls()
            if config and hasattr(instance, "configure"):
                instance.configure(config)
            source_registry.register(instance)
        elif ext_type == "cover_source":
            cls = getattr(module, "COVER_SOURCE_CLASS", None)
            if cls is None:
                return None
            instance = cls()
            if config and hasattr(instance, "configure"):
                instance.configure(config)
            cover_source_registry.register(instance)
        elif ext_type == "download_client":
            cls = getattr(module, "CLIENT_CLASS", None)
            if cls is None:
                return None
            instance = cls()
            if config and hasattr(instance, "configure"):
                instance.configure(config)
            download_client_registry.register(instance)
        else:
            return None

        _settings_schemas[ext_id] = getattr(module, "EXTENSION_SETTINGS", [])
        _loaded_modules[ext_id] = module
        return info
    except Exception as e:
        print(f"[extensions] Failed to load {ext_path.name}: {e}")
        # Also surface over HTTP (/logs, /api/status) so failures aren't
        # invisible when Docker stdout isn't accessible.
        try:
            from app.services import logger as applog
            applog.error("system", f"Extension load failed: {ext_path.name}", {"error": str(e)})
        except Exception:
            pass
        return None


def load_all_extensions(
    extensions_dir: str,
    enabled_ids: set[str] | None = None,
    configs: dict[str, dict] | None = None,
) -> list[dict]:
    """Load .py extension files at startup. If enabled_ids given, only load those."""
    ext_dir = Path(extensions_dir)
    if not ext_dir.exists():
        try:
            ext_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return []

    loaded = []
    for py_file in sorted(ext_dir.glob("*.py")):
        if enabled_ids is not None and py_file.stem not in enabled_ids:
            continue
        config = (configs or {}).get(py_file.stem) or None
        info = load_extension_file(py_file, config)
        if info:
            loaded.append(info)
    return loaded


def unload_extension(ext_id: str) -> None:
    """Deregister a loaded extension from all registries."""
    source_registry.unregister(ext_id)
    cover_source_registry.unregister(ext_id)
    download_client_registry.unregister(ext_id)
    _settings_schemas.pop(ext_id, None)
    _loaded_modules.pop(ext_id, None)


def get_settings_schemas() -> dict[str, list]:
    """Return all loaded extension settings schemas keyed by ext_id."""
    return dict(_settings_schemas)


def configure_extension(ext_id: str, config: dict) -> None:
    """Re-apply config to a loaded extension's registered source instance."""
    module = _loaded_modules.get(ext_id)
    if module is None:
        return
    ext_type = getattr(module, "EXTENSION_INFO", {}).get("type", "")
    if ext_type == "rom_source":
        instance = source_registry.get(ext_id)
        if instance and hasattr(instance, "configure"):
            instance.configure(config)
    elif ext_type == "cover_source":
        instance = cover_source_registry.get(ext_id)
        if instance and hasattr(instance, "configure"):
            instance.configure(config)
    elif ext_type == "download_client":
        instance = download_client_registry.get(ext_id)
        if instance and hasattr(instance, "configure"):
            instance.configure(config)
