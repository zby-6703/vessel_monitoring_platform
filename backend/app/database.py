from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import PLATFORM_ROOT, get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
database_url = settings.database_url
connect_args = {}

if database_url.startswith("sqlite"):
    relative_path = database_url.removeprefix("sqlite:///")
    if relative_path and not Path(relative_path).is_absolute():
        db_path = (PLATFORM_ROOT / relative_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{db_path.as_posix()}"
    connect_args = {"check_same_thread": False}

engine_options = {"pool_pre_ping": True, "pool_recycle": 1800}
if not database_url.startswith("sqlite"):
    engine_options.update({"pool_size": 10, "max_overflow": 20, "pool_timeout": 30})

engine = create_engine(database_url, connect_args=connect_args, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def exact_archive_mmsi(db: Session, recognized_zh: str | None, recognized_en: str | None) -> tuple[str | None, str | None]:
    """Return an MMSI only when a visual ship name has one exact archive match."""
    from .models import ShipArchive

    for source, value in (("recognized_zh", recognized_zh), ("recognized_en", recognized_en)):
        if not value or value == "UNKNOWN":
            continue
        rows = list(db.scalars(select(ShipArchive).where(ShipArchive.shipname == value)))
        if len(rows) == 1:
            return rows[0].mmsi, source
    return None, None


def ingest_ais_message(db: Session, payload: dict[str, Any], received_at: datetime | None = None):
    from .models import RealtimeAIS

    mmsi = str(payload.get("mmsi") or payload.get("MMSI") or "").strip()
    if not mmsi:
        raise ValueError("AIS message requires mmsi")
    base_datetime = payload.get("BaseDateTime") or payload.get("base_datetime") or received_at or datetime.now(timezone.utc)
    if isinstance(base_datetime, str):
        base_datetime = datetime.fromisoformat(base_datetime.replace("Z", "+00:00"))
    record = RealtimeAIS(
        mmsi=mmsi,
        base_datetime=base_datetime,
        lat=_float(payload.get("LAT") if "LAT" in payload else payload.get("latitude")),
        lon=_float(payload.get("LON") if "LON" in payload else payload.get("longitude")),
        sog=_float(payload.get("SOG") if "SOG" in payload else payload.get("speed")),
        cog=_float(payload.get("COG") if "COG" in payload else payload.get("course")),
        heading=_float(payload.get("Heading") if "Heading" in payload else payload.get("heading")),
        vessel_name=_text(payload, "VesselName", "vessel_name", "shipname", "ship_name", "name"),
        imo=_text(payload, "IMO", "imo"), call_sign=_text(payload, "CallSign", "callsign", "call_sign"),
        vessel_type=_text(payload, "VesselType", "vessel_type", "ship_type"), status=_text(payload, "Status", "status"),
        length=_float(payload.get("Length") if "Length" in payload else payload.get("length")),
        width=_float(payload.get("Width") if "Width" in payload else payload.get("width")),
        draft=_float(payload.get("Draft") if "Draft" in payload else payload.get("draught")),
        cargo=_text(payload, "Cargo", "cargo"), transceiver_class=_text(payload, "TransceiverClass", "transceiver_class"),
        to_bow=_float(payload.get("to_bow")), to_stern=_float(payload.get("to_stern")),
        to_port=_float(payload.get("to_port")), to_starboard=_float(payload.get("to_starboard")),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def cleanup_ais_live(db: Session, retention_days: int) -> int:
    from .models import RealtimeAIS

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = db.execute(delete(RealtimeAIS).where(RealtimeAIS.base_datetime < cutoff))
    db.commit()
    return int(result.rowcount or 0)


def import_ship_archive(db: Session, csv_path: Path) -> dict[str, int]:
    from .schemas import normalize_ship_name
    from .models import ShipArchive

    inserted = updated = skipped = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            mmsi = str(row.get("mmsi") or "").strip()
            if not mmsi:
                skipped += 1
                continue
            clean = {key: value.strip() if isinstance(value, str) and value.strip() else None for key, value in row.items()}
            existing = db.scalar(select(ShipArchive).where(ShipArchive.mmsi == mmsi))
            values = {
                "imo": clean.get("imo"), "callsign": clean.get("callsign"), "shipname": clean.get("shipname"),
                "ship_type": clean.get("ship_type"), "to_bow": _float(clean.get("to_bow")),
                "to_stern": _float(clean.get("to_stern")), "to_port": _float(clean.get("to_port")),
                "to_starboard": _float(clean.get("to_starboard")), "draught": _float(clean.get("draught")),
                "record_count": _integer(clean.get("record_count")) or 0,
                "static_record_count": _integer(clean.get("static_record_count")) or 0,
                "filled_static_fields": _integer(clean.get("filled_static_fields")) or 0,
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
                updated += 1
            else:
                db.add(ShipArchive(mmsi=mmsi, **values))
                inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def archive_quality_report(db: Session) -> dict[str, int]:
    from .models import ShipArchive

    rows = list(db.scalars(select(ShipArchive)))
    names = [row.shipname for row in rows if row.shipname]
    return {"total_rows": len(rows), "valid_shipnames": len(names), "valid_draughts": sum(row.draught is not None for row in rows), "duplicate_names": len(names) - len(set(names)), "unmatched": 0}


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def resolve_vessel_geometry(db: Session, visual_ship_name: str | None) -> dict[str, float | str | None]:
    """Resolve geometry by exact visual name: live AIS first, archive only when AIS is absent."""
    from .models import RealtimeAIS, ShipArchive

    resolved: dict[str, float | str | None] = {"ais_mmsi": None, "to_bow": None, "to_stern": None, "to_port": None, "to_starboard": None, "ais_draught": None}
    if not visual_ship_name or visual_ship_name == "UNKNOWN":
        return resolved

    ais_rows = list(db.scalars(select(RealtimeAIS).where(RealtimeAIS.vessel_name == visual_ship_name).order_by(RealtimeAIS.base_datetime.desc())))
    if ais_rows:
        for row in ais_rows:
            if resolved["ais_mmsi"] is None:
                resolved["ais_mmsi"] = row.mmsi
            for field, value in (("to_bow", row.to_bow), ("to_stern", row.to_stern), ("to_port", row.to_port), ("to_starboard", row.to_starboard), ("ais_draught", row.draft)):
                if resolved[field] is None and value is not None:
                    resolved[field] = value
            if all(resolved[field] is not None for field in ("to_bow", "to_stern", "to_port", "to_starboard", "ais_draught")):
                break
        return resolved

    # No real-time AIS name match: take the most complete exact-name archive entry.
    archive = db.scalar(select(ShipArchive).where(ShipArchive.shipname == visual_ship_name).order_by(ShipArchive.filled_static_fields.desc(), ShipArchive.record_count.desc(), ShipArchive.mmsi))
    if archive:
        resolved.update(ais_mmsi=archive.mmsi, to_bow=archive.to_bow, to_stern=archive.to_stern, to_port=archive.to_port, to_starboard=archive.to_starboard, ais_draught=archive.draught)
    return resolved


def calculate_displacement(visual_draft_m: float | None, geometry: dict[str, float | str | None], block_coefficient: float = 0.75) -> float | None:
    """Estimate inland-vessel displacement from AIS dimensions and visual draught."""
    to_bow, to_stern = geometry.get("to_bow"), geometry.get("to_stern")
    to_port, to_starboard = geometry.get("to_port"), geometry.get("to_starboard")
    values = (visual_draft_m, to_bow, to_stern, to_port, to_starboard)
    if any(value is None or not isinstance(value, (int, float)) or value <= 0 for value in values):
        return None
    return round((to_bow + to_stern) * (to_port + to_starboard) * visual_draft_m * block_coefficient, 2)


class ResultRepository:
    """The only visual-result database writer for complete pipelines."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _mmsi(db: Session, zh: str, en: str) -> str | None:
        return exact_archive_mmsi(db, zh, en)[0]

    def save_image_frame(self, frame, result_uri: str) -> None:
        from .models import OfflineImageResult

        with self.session_factory() as db:
            for vessel in frame.vessels:
                db.add(OfflineImageResult(filename=frame.sample_id, observed_at=frame.observed_at, recognized_zh=vessel.recognized_zh, recognized_en=vessel.recognized_en, draft_depth_m=vessel.draft.depth_m, status="confirmed" if vessel.draft.success and vessel.recognized_zh != "UNKNOWN" else "pending_review", ais_mmsi=self._mmsi(db, vessel.recognized_zh, vessel.recognized_en), result_uri=result_uri))
            db.commit()

    def save_video_instance(self, instance, result_uri: str) -> None:
        from .models import OfflineVideoResult

        with self.session_factory() as db:
            record = db.query(OfflineVideoResult).filter_by(video_filename=instance.source_filename, instance_id=instance.instance_id).one_or_none()
            geometry = resolve_vessel_geometry(db, instance.recognized_zh)
            if geometry["ais_mmsi"] is None and instance.recognized_en != "UNKNOWN":
                geometry = resolve_vessel_geometry(db, instance.recognized_en)
            geometry["displacement"] = calculate_displacement(instance.draft_depth_m, geometry)
            values = dict(start_time=instance.start_time, end_time=instance.end_time, recognized_zh=instance.recognized_zh, recognized_en=instance.recognized_en, draft_depth_m=instance.draft_depth_m, status=instance.status.value, result_uri=result_uri, **geometry)
            if record is None:
                db.add(OfflineVideoResult(video_filename=instance.source_filename, instance_id=instance.instance_id, **values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            db.commit()

    def save_realtime_frame(self, frame, session_id: str, source_type: str, source_name: str, camera_id: str | None, result_uri: str | None = None) -> None:
        """Persist one complete live frame, including all vessels in payload_json."""
        from .models import RealtimeVideoResult

        with self.session_factory() as db:
            db.add(RealtimeVideoResult(
                session_id=str(session_id), source_type=source_type, source_name=source_name, camera_id=camera_id,
                frame_index=int(frame.frame_index or 0), observed_at=frame.observed_at,
                image_width=frame.image_width, image_height=frame.image_height, processing_ms=frame.processing_ms,
                vessel_count=len(frame.vessels), result_uri=result_uri, payload_json=frame.model_dump(mode="json"),
            ))
            db.commit()

    def save_realtime_instance(self, instance, session_id: str, source_type: str, camera_id: str | None, result_uri: str | None = None) -> None:
        """Persist the current/final aggregate for a live vessel instance."""
        from .models import RealtimeTrackingResult

        with self.session_factory() as db:
            record = db.query(RealtimeTrackingResult).filter_by(session_id=str(session_id), instance_id=instance.instance_id).one_or_none()
            geometry = resolve_vessel_geometry(db, instance.recognized_zh)
            if geometry["ais_mmsi"] is None and instance.recognized_en != "UNKNOWN":
                geometry = resolve_vessel_geometry(db, instance.recognized_en)
            geometry["displacement"] = calculate_displacement(instance.draft_depth_m, geometry)
            values = dict(
                source_type=source_type, source_name=instance.source_filename, camera_id=camera_id,
                start_time=instance.start_time, end_time=instance.end_time,
                recognized_zh=instance.recognized_zh, recognized_en=instance.recognized_en,
                draft_depth_m=instance.draft_depth_m, status=instance.status.value,
                result_uri=result_uri, payload_json=instance.model_dump(mode="json"), **geometry,
            )
            if record is None:
                db.add(RealtimeTrackingResult(session_id=str(session_id), instance_id=instance.instance_id, **values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            db.commit()

