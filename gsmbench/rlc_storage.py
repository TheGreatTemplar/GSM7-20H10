"""Independent SQLite archive for LCR measurements."""

from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid


class RlcStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS e729_series '
                '(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, settings TEXT NOT NULL, '
                'status TEXT NOT NULL, error TEXT NOT NULL DEFAULT "")')
            connection.execute('CREATE TABLE IF NOT EXISTS e729_points '
                '(series_id TEXT NOT NULL REFERENCES e729_series(id), position INTEGER NOT NULL, '
                'payload TEXT NOT NULL, PRIMARY KEY(series_id, position))')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self, settings):
        identifier = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute('INSERT INTO e729_series(id,created_at,settings,status) VALUES(?,?,?,?)',
                (identifier, datetime.now(timezone.utc).isoformat(),
                 json.dumps(settings, ensure_ascii=False, allow_nan=False), 'running'))
        return identifier

    def append(self, identifier, position, point):
        with self.connect() as connection:
            connection.execute('INSERT INTO e729_points VALUES(?,?,?)',
                (identifier, position, json.dumps(point, ensure_ascii=False, allow_nan=False)))

    def finish(self, identifier, status, error=''):
        with self.connect() as connection:
            connection.execute('UPDATE e729_series SET status=?,error=? WHERE id=?',
                (status, error, identifier))

    def series(self):
        with self.connect() as connection:
            rows = connection.execute('SELECT s.id,s.created_at,s.settings,s.status,s.error,'
                '(SELECT COUNT(*) FROM e729_points p WHERE p.series_id=s.id) '
                'FROM e729_series s ORDER BY s.created_at DESC').fetchall()
        return [dict(id=row[0], created_at=row[1], settings=json.loads(row[2]),
                     status=row[3], error=row[4], count=row[5]) for row in rows]

    def points(self, identifier):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT payload FROM e729_points WHERE series_id=? ORDER BY position', (identifier,))]

    def recalculate_uncertainty(self, identifier, fixture_id, ac_level_v):
        from .rlc_uncertainty import measurement_uncertainty
        with self.connect() as connection:
            row = connection.execute('SELECT settings FROM e729_series WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('Серия не найдена')
            settings = json.loads(row[0])
            settings.update(fixture_id=fixture_id, operator_ac_level_v=ac_level_v,
                            uncertainty_recalculated_at=datetime.now(timezone.utc).isoformat())
            connection.execute('UPDATE e729_series SET settings=? WHERE id=?',
                (json.dumps(settings, ensure_ascii=False, allow_nan=False), identifier))
            rows = connection.execute('SELECT position,payload FROM e729_points WHERE series_id=?', (identifier,)).fetchall()
            for position, payload in rows:
                point = json.loads(payload)
                point['uncertainty'] = measurement_uncertainty(point, fixture_id, ac_level_v)
                connection.execute('UPDATE e729_points SET payload=? WHERE series_id=? AND position=?',
                    (json.dumps(point, ensure_ascii=False, allow_nan=False), identifier, position))
