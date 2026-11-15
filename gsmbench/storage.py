"""Additive tables in an existing SQLite database; UUIDs support two PCs."""

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sqlite3
import uuid

from .core import Point, Recipe


def new_run(sample, model, batch, recipe, demo, identity):
    return dict(id=str(uuid.uuid4()), sample=sample.strip(), model=model.strip(),
                batch=batch.strip(), recipe=asdict(recipe), demo=bool(demo),
                instrument=identity, station=platform.node(),
                measured_at=datetime.now(timezone.utc).isoformat(), state="running", points=[], error="")


def validate_run(run):
    uuid.UUID(run["id"])
    recipe = Recipe(**run["recipe"])
    recipe.validate()
    for key in ("sample", "model", "batch", "instrument", "station", "measured_at", "error"):
        if not isinstance(run[key], str):
            raise ValueError(f"Неверное поле {key}")
    if not run["sample"] or not run["model"] or type(run["demo"]) is not bool:
        raise ValueError("Нужны номер образца, маркировка и признак демо")
    if run["state"] not in ("complete", "cancelled", "failed"):
        raise ValueError("Неизвестное состояние измерения")
    points = [Point(**p) for p in run["points"]]
    if len(points) > len(recipe.levels):
        raise ValueError("Лишние точки")
    if [p.setpoint for p in points] != list(recipe.levels[:len(points)]):
        raise ValueError("Точки не соответствуют методике")
    if run["state"] == "complete" and (len(points) != len(recipe.levels) or any(not p.valid for p in points)):
        raise ValueError("Завершённое измерение содержит недостоверные точки")


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='gsm_run'").fetchone()
            tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if not exists and tables:
                backup = self.path.with_name(self.path.name + ".gsm-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
            db.execute("CREATE TABLE IF NOT EXISTS gsm_run (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS gsm_schema (version INTEGER NOT NULL)")
            version = db.execute("SELECT version FROM gsm_schema").fetchall()
            if not version:
                db.execute("INSERT INTO gsm_schema VALUES (1)")
            elif version != [(1,)]:
                raise ValueError("Неподдерживаемая версия схемы GSM")
            db.commit()

    def save_many(self, runs):
        for run in runs:
            validate_run(run)
        self.initialize()
        added = 0
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            for run in runs:
                payload = json.dumps(run, ensure_ascii=False, sort_keys=True, allow_nan=False)
                old = db.execute("SELECT payload FROM gsm_run WHERE id=?", (run["id"],)).fetchone()
                if old:
                    if json.loads(old[0]) != json.loads(payload):
                        raise ValueError("Конфликт UUID: " + run["id"])
                    continue
                db.execute("INSERT INTO gsm_run VALUES (?, ?)", (run["id"], payload))
                added += 1
        return added

    def load(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='gsm_run'").fetchone():
                return []
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM gsm_run ORDER BY rowid DESC")]

    def export_json(self, path):
        Path(path).write_text(json.dumps(dict(format="gsmbench-1", runs=self.load()),
                                       ensure_ascii=False, indent=4), encoding="utf-8")

    def import_json(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != "gsmbench-1" or not isinstance(payload.get("runs"), list):
            raise ValueError("Неизвестный формат обмена")
        return self.save_many(payload["runs"])
