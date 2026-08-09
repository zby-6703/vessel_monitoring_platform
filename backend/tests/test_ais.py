from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, calculate_displacement, cleanup_ais_live, exact_archive_mmsi, import_ship_archive, ingest_ais_message, resolve_vessel_geometry
from app.models import AISLive, RealtimeAIS, ShipArchive


def test_visual_ais_match_requires_one_exact_archive_name():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([ShipArchive(mmsi="1", shipname="海泰 52", record_count=1, static_record_count=1, filled_static_fields=1), ShipArchive(mmsi="2", shipname="重复", record_count=1, static_record_count=1, filled_static_fields=1), ShipArchive(mmsi="3", shipname="重复", record_count=1, static_record_count=1, filled_static_fields=1)])
        db.commit()
        assert exact_archive_mmsi(db, "海泰 52", None) == ("1", "recognized_zh")
        assert exact_archive_mmsi(db, "重复", None) == (None, None)
        assert exact_archive_mmsi(db, "不存在", None) == (None, None)


def test_ais_live_is_queryable_and_cleanup_is_executable():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        record = ingest_ais_message(db, {"mmsi": "413000001", "shipname": "TEST 01", "draught": 3.2})
        assert record.vessel_name == "TEST 01"
        assert record.draft == 3.2
        record.base_datetime = datetime.now(timezone.utc) - timedelta(days=9)
        db.commit()
        assert cleanup_ais_live(db, 7) == 1
        assert db.query(AISLive).count() == 0


def test_geometry_resolution_uses_newest_exact_name_ais_rows_before_archive():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add_all([
            RealtimeAIS(mmsi="100", base_datetime=now - timedelta(minutes=1), vessel_name="TEST 01", to_bow=10, to_port=3),
            RealtimeAIS(mmsi="100", base_datetime=now, vessel_name="TEST 01", to_stern=20, to_starboard=4, draft=3.2),
            ShipArchive(mmsi="200", shipname="TEST 01", to_bow=99, to_stern=99, to_port=99, to_starboard=99, draught=99, record_count=1, static_record_count=1, filled_static_fields=9),
        ])
        db.commit()
        assert resolve_vessel_geometry(db, "TEST 01") == {"ais_mmsi": "100", "to_bow": 10, "to_stern": 20, "to_port": 3, "to_starboard": 4, "ais_draught": 3.2}


def test_geometry_resolution_falls_back_to_exact_name_archive_when_no_live_ais():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ShipArchive(mmsi="200", shipname="ARCHIVE SHIP", to_bow=10, to_stern=20, to_port=3, to_starboard=4, draught=3.2, record_count=1, static_record_count=1, filled_static_fields=9))
        db.commit()
        assert resolve_vessel_geometry(db, "ARCHIVE SHIP") == {"ais_mmsi": "200", "to_bow": 10, "to_stern": 20, "to_port": 3, "to_starboard": 4, "ais_draught": 3.2}


def test_displacement_uses_visual_draught_and_inland_default_block_coefficient():
    geometry = {"to_bow": 10, "to_stern": 20, "to_port": 3, "to_starboard": 4}
    assert calculate_displacement(4.0, geometry) == 630.0
    assert calculate_displacement(None, geometry) is None


def test_archive_import_is_idempotent(tmp_path: Path):
    source = tmp_path / "archive.csv"
    source.write_text("mmsi,imo,callsign,shipname,ship_type,to_bow,to_stern,to_port,to_starboard,draught,record_count,static_record_count,filled_static_fields\n100,IMO1,CALL,TEST 01,Cargo,10,20,3,4,3.2,2,1,9\n", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert import_ship_archive(db, source)["inserted"] == 1
        assert import_ship_archive(db, source)["updated"] == 1
        assert db.query(ShipArchive).count() == 1
