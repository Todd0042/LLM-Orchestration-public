"""Info-gathering tool: searches local workspaces cleanly."""

import os
from pathlib import Path
import subprocess

TOOL_SPEC = {
    "name": "repo_intel",
    "category": "research",
    "gpuNeed": "none",
    "description": "Search files and patterns inside the configured GitHub workspace or a given directory using ripgrep.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Regex or text pattern to search"},
            "directory": {"type": "string", "description": "Directory path (defaults to ~/GitHub or ~/Documents/GitHub)"},
        },
        "required": ["query"],
    },
}


def run(query: str, directory: str = "") -> dict:
    if directory:
        target = Path(directory).expanduser()
    elif os.environ.get("LLM_ORCH_GITHUB_DIR"):
        target = Path(os.environ["LLM_ORCH_GITHUB_DIR"]).expanduser()
    elif (Path.home() / "GitHub").is_dir():
        target = Path.home() / "GitHub"
    elif (Path.home() / "Documents" / "GitHub").is_dir():
        target = Path.home() / "Documents" / "GitHub"
    else:
        target = Path.home() / "GitHub"
    proc = subprocess.run(
        ["rg", "-n", "--max-count", "3", "--max-filesize", "500K", query, str(target)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    lines = proc.stdout.splitlines()[:60]
    return {"matches": lines, "count": len(lines), "directory": str(target)}
