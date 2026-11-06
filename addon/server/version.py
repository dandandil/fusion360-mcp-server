"""Add-in version: a hash of its source files.

The add-in reports it in ``ping``; the server computes it over the repo's
``addon/`` and warns when the copy loaded in Fusion differs.  No manual
version bumps to forget.  Pure Python, no ``adsk``.
"""

import hashlib
from pathlib import Path

SOURCE_SUFFIXES = (".py", ".manifest")


def addon_hash(addon_root) -> str:
    """Short sha256 over the add-in sources (paths and contents, LF endings)."""
    root = Path(addon_root)
    digest = hashlib.sha256()
    files = sorted(
        p
        for p in root.rglob("*")
        if p.suffix in SOURCE_SUFFIXES and "__pycache__" not in p.parts
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]
