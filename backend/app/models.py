from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class OfflineImageResult(Base):
    __tablename__ = "offline_image_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recognized_zh: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    recognized_en: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    draft_depth_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    ais_mmsi: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    result_uri: Mapped[str] = mapped_column(String(1024), nullable=False)


class OfflineVideoResult(Base):
    __tablename__ = "offline_video_result"
    __table_args__ = (UniqueConstraint("video_filename", "instance_id", name="uq_offline_video_result_filename_instance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_filename: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recognized_zh: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    recognized_en: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    draft_depth_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    ais_mmsi: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    to_bow: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_stern: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_port: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_starboard: Mapped[float | None] = mapped_column(Float, nullable=True)
    ais_draught: Mapped[float | None] = mapped_column("AIS_draught", Float, nullable=True)
    displacement: Mapped[float | None] = mapped_column("Displacement", Float, nullable=True)
    result_uri: Mapped[str] = mapped_column(String(1024), nullable=False)


class ShipArchive(Base):
    __tablename__ = "ship_archive"
    __table_args__ = (Index("ix_ship_archive_shipname", "shipname"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mmsi: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    imo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    callsign: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shipname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ship_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_bow: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_stern: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_port: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_starboard: Mapped[float | None] = mapped_column(Float, nullable=True)
    draught: Mapped[float | None] = mapped_column(Float, nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    static_record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_static_fields: Mapped[int] = mapped_column(Integer, nullable=False)


class RealtimeAIS(Base):
    __tablename__ = "realtime_AIS"
    __table_args__ = (Index("ix_realtime_AIS_mmsi_datetime", "MMSI", "BaseDateTime"), Index("ix_realtime_AIS_vesselname_datetime", "VesselName", "BaseDateTime"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mmsi: Mapped[str] = mapped_column("MMSI", String(16), nullable=False)
    base_datetime: Mapped[datetime] = mapped_column("BaseDateTime", DateTime(timezone=True), nullable=False, index=True)
    lat: Mapped[float | None] = mapped_column("LAT", Float, nullable=True)
    lon: Mapped[float | None] = mapped_column("LON", Float, nullable=True)
    sog: Mapped[float | None] = mapped_column("SOG", Float, nullable=True)
    cog: Mapped[float | None] = mapped_column("COG", Float, nullable=True)
    heading: Mapped[float | None] = mapped_column("Heading", Float, nullable=True)
    vessel_name: Mapped[str | None] = mapped_column("VesselName", String(256), nullable=True)
    imo: Mapped[str | None] = mapped_column("IMO", String(32), nullable=True)
    call_sign: Mapped[str | None] = mapped_column("CallSign", String(64), nullable=True)
    vessel_type: Mapped[str | None] = mapped_column("VesselType", String(128), nullable=True)
    status: Mapped[str | None] = mapped_column("Status", String(64), nullable=True)
    length: Mapped[float | None] = mapped_column("Length", Float, nullable=True)
    width: Mapped[float | None] = mapped_column("Width", Float, nullable=True)
    draft: Mapped[float | None] = mapped_column("Draft", Float, nullable=True)
    cargo: Mapped[str | None] = mapped_column("Cargo", String(128), nullable=True)
    transceiver_class: Mapped[str | None] = mapped_column("TransceiverClass", String(32), nullable=True)
    to_bow: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_stern: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_port: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_starboard: Mapped[float | None] = mapped_column(Float, nullable=True)


class RealtimeVideoResult(Base):
    """One row per live monitoring frame; vessel details are kept in payload_json."""
    __tablename__ = "realtime_video_results"
    __table_args__ = (UniqueConstraint("session_id", "frame_index", name="uq_realtime_video_session_frame"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    camera_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    image_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    vessel_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class RealtimeTrackingResult(Base):
    """One row per tracked vessel instance/session in live monitoring."""
    __tablename__ = "realtime_tracking_results"
    __table_args__ = (UniqueConstraint("session_id", "instance_id", name="uq_realtime_tracking_session_instance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(512), nullable=False)
    camera_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recognized_zh: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    recognized_en: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    draft_depth_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    ais_mmsi: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    to_bow: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_stern: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_port: Mapped[float | None] = mapped_column(Float, nullable=True)
    to_starboard: Mapped[float | None] = mapped_column(Float, nullable=True)
    ais_draught: Mapped[float | None] = mapped_column("AIS_draught", Float, nullable=True)
    displacement: Mapped[float | None] = mapped_column("Displacement", Float, nullable=True)
    result_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


# Backward-compatible Python names for callers; the physical table names above are canonical.
ImageResult = OfflineImageResult
VideoResult = OfflineVideoResult
AISLive = RealtimeAIS
