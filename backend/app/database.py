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
    """Return an MMSI only for one exact normalized visual-name match."""
    from .schemas import normalize_ship_name
    from .models import ShipArchive

    for source, value in (("recognized_zh", recognized_zh), ("recognized_en", recognized_en)):
        key = normalize_ship_name(value)
        if not key or key == "UNKNOWN":
            continue
        rows = list(db.scalars(select(ShipArchive).where(ShipArchive.shipname_key == key)))
        if len(rows) == 1:
            return rows[0].mmsi, source
    return None, None


def ingest_ais_message(db: Session, payload: dict[str, Any], received_at: datetime | None = None):
    from .schemas import normalize_ship_name
    from .models import AISLive

    mmsi = str(payload.get("mmsi") or payload.get("MMSI") or "").strip()
    if not mmsi:
        raise ValueError("AIS message requires mmsi")
    name = payload.get("shipname") or payload.get("ship_name") or payload.get("name")
    record = AISLive(
        received_at=received_at or datetime.now(timezone.utc),
        mmsi=mmsi,
        shipname=str(name).strip() if name else None,
        shipname_key=normalize_ship_name(str(name)) if name else None,
        latitude=_float(payload.get("latitude")), longitude=_float(payload.get("longitude")),
        speed=_float(payload.get("speed")), course=_float(payload.get("course")),
        draught=_float(payload.get("draught")), payload_json=payload,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def cleanup_ais_live(db: Session, retention_days: int) -> int:
    from .models import AISLive

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = db.execute(delete(AISLive).where(AISLive.received_at < cutoff))
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
                "shipname_key": normalize_ship_name(clean.get("shipname")), "ship_type": clean.get("ship_type"),
                "draught": _float(clean.get("draught")), "payload_json": clean,
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
    names = [row.shipname_key for row in rows if row.shipname_key]
    return {"total_rows": len(rows), "valid_shipnames": len(names), "valid_draughts": sum(row.draught is not None for row in rows), "duplicate_names": len(names) - len(set(names)), "unmatched": 0}


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class ResultRepository:
    """The only visual-result database writer for complete pipelines."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _mmsi(db: Session, zh: str, en: str) -> str | None:
        from .models import ShipArchive
        from .schemas import normalize_ship_name

        for name in (zh, en):
            key = normalize_ship_name(name)
            if key and key != "UNKNOWN":
                matches = db.query(ShipArchive).filter_by(shipname_key=key).all()
                if len(matches) == 1:
                    return matches[0].mmsi
        return None

    def save_image_frame(self, frame, result_uri: str) -> None:
        from .models import ImageResult

        with self.session_factory() as db:
            for vessel in frame.vessels:
                db.add(ImageResult(filename=frame.sample_id, observed_at=frame.observed_at, recognized_zh=vessel.recognized_zh, recognized_en=vessel.recognized_en, draft_depth_m=vessel.draft.depth_m, status="confirmed" if vessel.draft.success and vessel.recognized_zh != "UNKNOWN" else "pending_review", ais_mmsi=self._mmsi(db, vessel.recognized_zh, vessel.recognized_en), result_uri=result_uri))
            db.commit()

    def save_video_instance(self, instance, result_uri: str) -> None:
        from .models import VideoResult

        with self.session_factory() as db:
            record = db.query(VideoResult).filter_by(video_filename=instance.source_filename, instance_id=instance.instance_id).one_or_none()
            values = dict(start_time=instance.start_time, end_time=instance.end_time, recognized_zh=instance.recognized_zh, recognized_en=instance.recognized_en, draft_depth_m=instance.draft_depth_m, status=instance.status.value, ais_mmsi=self._mmsi(db, instance.recognized_zh, instance.recognized_en), result_uri=result_uri)
            if record is None:
                db.add(VideoResult(video_filename=instance.source_filename, instance_id=instance.instance_id, **values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            db.commit()

