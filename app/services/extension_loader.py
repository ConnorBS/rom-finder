"""Loads .py extension files from extensions_dir and registers their sources."""

import importlib.util
from pathlib import Path

from app.services.sources import registry as source_registry
from app.services.cover_sources import registry as cover_source_registry


def load_extension_file(ext_path: Path) -> dict | None:
    """Import one extension .py, register it, return its EXTENSION_INFO or None on failure."""
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

        ext_type = info["type"]
        if ext_type == "rom_source":
            cls = getattr(module, "SOURCE_CLASS", None)
            if cls is None:
                return None
            source_registry.register(cls())
        elif ext_type == "cover_source":
            cls = getattr(module, "COVER_SOURCE_CLASS", None)
            if cls is None:
                return None
            cover_source_registry.register(cls())
        else:
            return None

        return info
    except Exception as e:
        print(f"[extensions] Failed to load {ext_path.name}: {e}")
        return None


def load_all_extensions(extensions_dir: str) -> list[dict]:
    """Load all .py extension files at startup. Returns list of loaded EXTENSION_INFO dicts."""
    ext_dir = Path(extensions_dir)
    if not ext_dir.exists():
        try:
            ext_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return []

    loaded = []
    for py_file in sorted(ext_dir.glob("*.py")):
        info = load_extension_file(py_file)
        if info:
            loaded.append(info)
    return loaded


def unload_extension(ext_id: str) -> None:
    """Deregister a loaded extension from all registries."""
    source_registry.unregister(ext_id)
    cover_source_registry.unregister(ext_id)
