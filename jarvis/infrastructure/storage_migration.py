"""Restartable, non-overwriting migration of user capability directories."""

from pathlib import Path


def migrate_capability_directories(root: Path) -> None:
    """Move complete units atomically; never merge or overwrite two units.

    A restart can resume after any completed rename. Conflicts are checked
    before the first move, so ambiguous user data stays untouched.
    """
    root = Path(root)
    moves = []
    for category in ("actions", "handlers", "modules"):
        source = root / category
        destination = root / "capabilities" / category
        if source.is_symlink():
            raise ValueError(f"Cannot migrate symlinked capability directory: {source}")
        if source.exists():
            for unit in source.iterdir():
                target = destination / unit.name
                if target.exists() or target.is_symlink():
                    raise ValueError(f"Capability migration conflict: {unit} and {target}")
                moves.append((unit, target))
    for category in ("actions", "handlers", "modules"):
        (root / "capabilities" / category).mkdir(parents=True, exist_ok=True)
    for source, destination in moves:
        source.rename(destination)
    for category in ("actions", "handlers", "modules"):
        source = root / category
        if source.exists():
            source.rmdir()
