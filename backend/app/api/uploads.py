"""Reading uploaded files with a size cap (the server would otherwise buffer any amount)."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

from ..config import get_settings

MAX_FILE_UPLOAD = 512 * 1024 * 1024  # backups (with embedded photos), CSV and JSON imports
_CHUNK = 1024 * 1024


def max_image_upload() -> int:
    """Photos you upload are held to the same cap as photos the app downloads."""
    return get_settings().max_download_bytes


async def read_capped(f: UploadFile, limit: int) -> bytes:
    data = bytearray()
    while chunk := await f.read(_CHUNK):
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"{f.filename or 'upload'} is larger than {limit // (1024 * 1024)} MB")
    return bytes(data)
