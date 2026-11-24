"""Auto-discovers drop-in Python tools from tools/*.py for LLM tool-calling & UI triggers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def load_python_tools(tools_dir: Path, disabled: list[str] | None = None) -> dict[str, dict[str, Any]]:
    disabled_set = set(disabled or [])
    registry: dict[str, dict[str, Any]] = {}
    if not tools_dir.is_dir():
        return registry

    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        mod_name = f"llm_orch_tool_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(py_file))
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tspec = getattr(mod, "TOOL_SPEC", None)
            run_fn = getattr(mod, "run", None)
            if isinstance(tspec, dict) and callable(run_fn):
                tname = str(tspec.get("name") or py_file.stem)
                registry[tname] = {
                    "name": tname,
                    "category": str(tspec.get("category") or "general"),
                    "description": str(tspec.get("description") or ""),
                    "gpuNeed": str(tspec.get("gpuNeed") or "none"),
                    "parameters": tspec.get("parameters") or {"type": "object", "properties": {}},
                    "enabled": tname not in disabled_set,
                    "path": str(py_file),
                    "run": run_fn,
                }
        except Exception:
            continue
    return registry
