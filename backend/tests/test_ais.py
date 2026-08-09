from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, cleanup_ais_live, exact_archive_mmsi, import_ship_archive, ingest_ais_message
from app.models import AISLive, ShipArchive


def test_visual_ais_match_requires_one_exact_normalized_archive_name():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([ShipArchive(mmsi="1", shipname="海泰 52", shipname_key="海泰52", payload_json={}), ShipArchive(mmsi="2", shipname="重复", shipname_key="重复", payload_json={}), ShipArchive(mmsi="3", shipname="重复", shipname_key="重复", payload_json={})])
        db.commit()
        assert exact_archive_mmsi(db, " 海泰 52 ", None) == ("1", "recognized_zh")
        assert exact_archive_mmsi(db, "重复", None) == (None, None)
        assert exact_archive_mmsi(db, "不存在", None) == (None, None)


def test_ais_live_is_queryable_and_cleanup_is_executable():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        record = ingest_ais_message(db, {"mmsi": "413000001", "shipname": "TEST 01", "draught": 3.2})
        assert record.shipname_key == "TEST01"
        record.received_at = datetime.now(timezone.utc) - timedelta(days=9)
        db.commit()
        assert cleanup_ais_live(db, 7) == 1
        assert db.query(AISLive).count() == 0


def test_archive_import_is_idempotent(tmp_path: Path):
    source = tmp_path / "archive.csv"
    source.write_text("mmsi,imo,callsign,shipname,ship_type,draught\n100,IMO1,CALL,TEST 01,Cargo,3.2\n", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert import_ship_archive(db, source)["inserted"] == 1
        assert import_ship_archive(db, source)["updated"] == 1
        assert db.query(ShipArchive).count() == 1
