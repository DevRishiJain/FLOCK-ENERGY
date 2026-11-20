import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS hierarchy_nodes (
  id TEXT PRIMARY KEY,
  level TEXT NOT NULL CHECK (level IN ('zone','circle','division','subdivision','substation','feeder','dt')),
  code TEXT NOT NULL,
  name TEXT,
  parent_id TEXT REFERENCES hierarchy_nodes(id),
  UNIQUE(level, code)
);
CREATE INDEX IF NOT EXISTS ix_nodes_parent ON hierarchy_nodes(parent_id);
CREATE TABLE IF NOT EXISTS transformers (
  code TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES hierarchy_nodes(id),
  name TEXT, feeder_code TEXT, capacity_kva INTEGER
);
CREATE TABLE IF NOT EXISTS meters (
  meter_id TEXT PRIMARY KEY,
  serial_no TEXT, make TEXT, phase_type TEXT, install_status TEXT, install_type TEXT, build TEXT,
  dt_node_id TEXT REFERENCES hierarchy_nodes(id),
  lat REAL, lng REAL, synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meters_dt ON meters(dt_node_id);
CREATE INDEX IF NOT EXISTS ix_meters_status ON meters(install_status);
CREATE INDEX IF NOT EXISTS ix_meters_geo ON meters(lat, lng);
CREATE TABLE IF NOT EXISTS readings (
  meter_id TEXT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
  ts TEXT NOT NULL, kwh REAL, kvah REAL, volt_r REAL,
  PRIMARY KEY (meter_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS data_quality_issues (
  id INTEGER PRIMARY KEY, sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
  kind TEXT NOT NULL, entity TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','ok','failed')),
  meters INTEGER, readings INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('reader','admin'))
);
"""


def connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init(path: str) -> None:
    c = connect(path)
    c.executescript(SCHEMA)
    c.close()


@contextmanager
def tx(c: sqlite3.Connection):
    try:
        c.execute("BEGIN")
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
