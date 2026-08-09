from __future__ import annotations

import json
from pathlib import Path

from app.database import Base, SessionLocal, archive_quality_report, engine, import_ship_archive


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[2] / "assets" / "Master_Ship_Archive_Database.csv"
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        result = import_ship_archive(db, path)
        print(json.dumps({**result, **archive_quality_report(db)}, ensure_ascii=False))
