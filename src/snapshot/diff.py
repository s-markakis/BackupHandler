"""
diff.py - Snapshot comparison.
"""

import json
from pathlib import Path

# ─── Snapshot Diff ──────────────────────────────────────────────────────────


def diff_snapshots(logger, snapshot_a_path, snapshot_b_path):
    """
    Compare two snapshots and return what was added/removed.

    Parameters:
        logger: Logger instance.
        snapshot_a_path (str): Path to the older snapshot.
        snapshot_b_path (str): Path to the newer snapshot.

    Returns:
        dict: Differences organized by category with 'added' and 'removed' lists.
    """
    a = json.loads(Path(snapshot_a_path).read_text())
    b = json.loads(Path(snapshot_b_path).read_text())

    diff = {}

    # Compare package lists
    for category in ("apt", "snap", "npm", "go", "winget"):
        pkgs_a = set(_extract_names(a.get("packages", {}).get(category, [])))
        pkgs_b = set(_extract_names(b.get("packages", {}).get(category, [])))
        added = sorted(pkgs_b - pkgs_a)
        removed = sorted(pkgs_a - pkgs_b)
        if added or removed:
            diff[category] = {"added": added, "removed": removed}

    # Cargo
    cargo_a = {p["name"] for p in a.get("packages", {}).get("cargo", []) if isinstance(p, dict)}
    cargo_b = {p["name"] for p in b.get("packages", {}).get("cargo", []) if isinstance(p, dict)}
    if cargo_a != cargo_b:
        diff["cargo"] = {"added": sorted(cargo_b - cargo_a), "removed": sorted(cargo_a - cargo_b)}

    # VS Code extensions
    ext_a = set(a.get("apps", {}).get("vscode_extensions", []))
    ext_b = set(b.get("apps", {}).get("vscode_extensions", []))
    if ext_a != ext_b:
        diff["vscode_extensions"] = {"added": sorted(ext_b - ext_a), "removed": sorted(ext_a - ext_b)}

    # Dotfiles
    dot_a = set(a.get("configs", {}).get("dotfiles", {}).keys())
    dot_b = set(b.get("configs", {}).get("dotfiles", {}).keys())
    if dot_a != dot_b:
        diff["dotfiles"] = {"added": sorted(dot_b - dot_a), "removed": sorted(dot_a - dot_b)}

    logger.info(
        f"Snapshot diff: {sum(len(v.get('added', [])) + len(v.get('removed', [])) for v in diff.values())} changes across {len(diff)} categories"
    )
    return diff


def _extract_names(items):
    """Extract package names from a list that may contain strings or dicts."""
    names = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.append(item.get("name", item.get("app_id", "")))
    return names
