"""Passage recording settings, archive and browser-playable media endpoints."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .recording import RecordingSettings, recording_manager

router = APIRouter(tags=["passage recordings"])


@router.get("/api/recording/settings")
def settings():
    return recording_manager.get_settings()


@router.put("/api/recording/settings")
def save_settings(payload: RecordingSettings):
    try:
        return recording_manager.update_settings(payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/recording/status")
def status():
    return recording_manager.status()


@router.get("/api/recordings")
def recordings(query: str = Query("", max_length=200), page: int = Query(1, ge=1), page_size: int = Query(12, ge=1, le=100), camera_id: str = Query("", max_length=100), date_from: datetime | None = None, date_to: datetime | None = None):
    # Naive ISO inputs are interpreted as UTC; frontend sends explicit offsets.
    date_from = date_from.replace(tzinfo=timezone.utc) if date_from and date_from.tzinfo is None else date_from
    date_to = date_to.replace(tzinfo=timezone.utc) if date_to and date_to.tzinfo is None else date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "开始时间不能晚于结束时间")
    return recording_manager.list_recordings(query, page, page_size, camera_id, date_from, date_to)


@router.delete("/api/recordings/{recording_id}")
def delete_recording(recording_id: str):
    try:
        recording_manager.delete_recording(recording_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "录像不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": True}


@router.get("/api/recordings/{recording_id}/{kind}")
def media(recording_id: str, kind: Literal["original", "focused", "thumbnail"], download: bool = False):
    try:
        path = recording_manager.media_path(recording_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(404, "录像文件不存在或仍在编码") from exc
    return FileResponse(path, media_type="image/jpeg" if kind == "thumbnail" else "video/mp4", filename=f"{recording_id}-{path.name}" if download else None, content_disposition_type="attachment" if download else "inline")
