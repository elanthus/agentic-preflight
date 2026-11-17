"""Repository compatibility preconditions shared by setup and run creation."""

from pathlib import Path

from . import gitx
from .errors import UnsupportedObjectFormat


def require_supported_object_format(repo: Path) -> None:
    """Refuse repositories whose object IDs cannot fit run and attestation records."""
    object_format = gitx.out(repo, "rev-parse", "--show-object-format")
    if object_format != "sha1":
        raise UnsupportedObjectFormat(object_format)
