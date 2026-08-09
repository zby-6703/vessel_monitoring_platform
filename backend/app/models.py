from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class ImageResult(Base):
    __tablename__ = "image_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recognized_zh: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    recognized_en: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    draft_depth_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    ais_mmsi: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    result_uri: Mapped[str] = mapped_column(String(1024), nullable=False)


class VideoResult(Base):
    __tablename__ = "video_results"
    __table_args__ = (UniqueConstraint("video_filename", "instance_id", name="uq_video_results_filename_instance"),)

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
    result_uri: Mapped[str] = mapped_column(String(1024), nullable=False)


class ShipArchive(Base):
    __tablename__ = "ship_archive"
    __table_args__ = (Index("ix_ship_archive_shipname_key", "shipname_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mmsi: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    imo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    callsign: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shipname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    shipname_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ship_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    draught: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class AISLive(Base):
    __tablename__ = "ais_live"
    __table_args__ = (Index("ix_ais_live_mmsi_received_at", "mmsi", "received_at"), Index("ix_ais_live_shipname_key", "shipname_key"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    mmsi: Mapped[str] = mapped_column(String(16), nullable=False)
    shipname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    shipname_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    course: Mapped[float | None] = mapped_column(Float, nullable=True)
    draught: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
