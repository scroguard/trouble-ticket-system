"""Attachment file storage: content-addressed files under ATTACHMENT_DIR."""

import hashlib
import os
import tempfile
from pathlib import Path


def write_blob(root: Path, data: bytes) -> tuple[str, str]:
    """Store `data` once per distinct content; returns (relative path, sha256).

    Identical files are stored once, and re-storing the same content (e.g. an email
    ingested twice, or an import re-run) is a no-op. Written atomically via rename.
    """
    digest = hashlib.sha256(data).hexdigest()
    rel_path = Path(digest[:2]) / digest
    target = root / rel_path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return str(rel_path), digest
