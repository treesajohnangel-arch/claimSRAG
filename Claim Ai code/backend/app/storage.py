"""Local document storage (Phase 3): the smallest server-side store for the original policy PDFs.

The file of a document lives at ``<root>/<storage_reference>``. The root is chosen by the server code that builds
the storage; no client value ever selects it.

- **References are not paths.** Only references in the server's own format are accepted: 32 to 128 URL-safe
  characters (``new_storage_reference`` generates 43). So a reference can never contain ``/``, ``\\``, ``..``, a drive
  or an extension, and always names a file directly inside the root.
- **No redirection:** a symbolic link at a reference is refused (``O_NOFOLLOW``), and so is anything that is not a
  regular file.
- **Write once:** an existing file is never replaced, because a recorded version's file is fixed (D-025).
- **Generic errors:** every failure raises ``StorageError`` with the same message, which never includes a path.

This is a development adapter on the local filesystem. There is no cloud or object storage. The upload route
(D-035, ``app.indexing``) and the operator tooling write to it.
"""

import os
import re
import secrets
import stat
from pathlib import Path

STORAGE_FAILURE_MESSAGE = "Document source unavailable"

_REFERENCE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def new_storage_reference() -> str:
    """An opaque, unguessable reference chosen by the server: 32 random bytes, URL-safe (43 characters). It carries
    no tenant, policy or file-name information."""
    return secrets.token_urlsafe(32)


class StorageError(Exception):
    """A document file could not be stored or read. Always the same generic message."""

    def __init__(self) -> None:
        super().__init__(STORAGE_FAILURE_MESSAGE)


class LocalDocumentStorage:
    """Original document files under one root directory, addressed only by storage reference."""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def _path(self, storage_reference: str) -> Path:
        if not isinstance(storage_reference, str) or not _REFERENCE.fullmatch(storage_reference):
            raise StorageError
        return self._root / storage_reference

    def read(self, storage_reference: str) -> bytes:
        """The bytes of the file stored under this reference."""
        path = self._path(storage_reference)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise StorageError from None
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):  # checked on the open descriptor, not the name
                with os.fdopen(fd, "rb", closefd=False) as file:
                    return file.read()
        except OSError:
            raise StorageError from None
        finally:
            os.close(fd)
        raise StorageError  # a directory, FIFO, device or socket

    def write(self, storage_reference: str, data: bytes) -> None:
        """Store a new file under a new reference. An existing file (or link) is never replaced."""
        path = self._path(storage_reference)
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as file:
                file.write(data)
        except OSError:
            raise StorageError from None
