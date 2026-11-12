"""Publish complete bytes with a same-directory temporary file."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def atomic_write(path, content):
    path = Path(path)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
                                suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
