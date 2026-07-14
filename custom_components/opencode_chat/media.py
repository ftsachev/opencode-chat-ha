from __future__ import annotations

import base64
import logging
import os
import shutil
import uuid
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MEDIA_DIR_NAME = "opencode_chat_media"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def media_root(hass: HomeAssistant) -> str:
    return hass.config.path(MEDIA_DIR_NAME)


def session_dir(hass: HomeAssistant, session_id: str) -> str:
    return os.path.join(media_root(hass), session_id)


def ensure_media_root(hass: HomeAssistant) -> None:
    os.makedirs(media_root(hass), exist_ok=True)


def save_image(
    hass: HomeAssistant,
    session_id: str,
    media_type: str,
    data_b64: str,
) -> dict[str, Any]:
    ext = ALLOWED_TYPES.get(media_type)
    if ext is None:
        raise ValueError(f"Unsupported image type: {media_type}")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception as err:
        raise ValueError(f"Invalid base64: {err}") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large ({len(raw)} bytes)")
    if len(raw) < 16:
        raise ValueError("Image data too small")
    dir_ = session_dir(hass, session_id)
    os.makedirs(dir_, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(dir_, filename), "wb") as f:
        f.write(raw)
    return {
        "type": "image_ref",
        "filename": filename,
        "media_type": media_type,
        "bytes": len(raw),
    }


def delete_session_media(hass: HomeAssistant, session_id: str) -> None:
    path = session_dir(hass, session_id)
    if os.path.isdir(path):
        try:
            shutil.rmtree(path)
        except OSError:
            _LOGGER.exception("Could not remove media dir for session %s", session_id)
