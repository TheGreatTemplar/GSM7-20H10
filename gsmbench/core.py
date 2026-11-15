"""Measurement recipes and comparison, independent of UI and hardware."""

from dataclasses import asdict, dataclass
from itertools import combinations
import math
import random


def generate_levels(start, stop, count=20, spacing="log"):
    """Generate inclusive, increasing points in the units supplied by the caller."""
    if spacing not in ("linear", "log"):
        raise ValueError("Неизвестный вид ряда")
    if type(count) is not int or not 2 <= count <= 200:
        raise ValueError("Число точек: от 2 до 200")
    if not all(math.isfinite(v) for v in (start, stop)) or not 0 < start < stop:
        raise ValueError("Нужно 0 < начало < конец")
    if spacing == "linear":
        points = [start + (stop - start) * i / (count - 1) for i in range(count)]
    else:
        step = (math.log(stop) - math.log(start)) / (count - 1)
        points = [math.exp(math.log(start) + step * i) for i in range(count)]
    points[0], points[-1] = start, stop
    return tuple(points)


@dataclass(frozen=True)
class Recipe:
    kind: str = "diode"
    levels: tuple = (0.0001, 0.0003, 0.001, 0.003, 0.01)
    limit: float = 1.0
    delay: float = 0.1
    temperature: float = 23.0
    wires: int = 2
    terminals: str = "front"

    def validate(self):
        if self.kind not in ("diode", "j310"):
            raise ValueError("Неизвестная методика")
        if self.wires not in (2, 4) or self.terminals not in ("front", "rear"):
            raise ValueError("Схема подключения: 2/4 провода, передние/задние клеммы")
        if not 1 <= len(self.levels) <= 200:
            raise ValueError("Нужно от 1 до 200 точек")
        values = (*self.levels, self.limit, self.delay, self.temperature)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Параметры должны быть конечными числами")
        if tuple(sorted(set(self.levels))) != tuple(self.levels):
            raise ValueError("Точки должны строго возрастать")
        if not 0.01 <= self.delay <= 2:
            raise ValueError("Задержка: 0,01–2 с")
        maximum, max_limit = (0.1, 2.0) if self.kind == "diode" else (10.0, 0.1)
        if not all(0 < v <= maximum for v in self.levels):
            raise ValueError(f"Точки вне диапазона (0; {maximum}] в единицах СИ")
        if not 0 < self.limit <= max_limit:
            raise ValueError(f"Ограничение должно быть в (0; {max_limit}] в единицах СИ")
        if self.kind == "j310" and max(self.levels) * self.limit > 0.3 + 1e-12:
            raise ValueError("Для этой методики Vds × Iлим должно быть ≤ 0,3 Вт")

    def signature(self):
        return self.kind, tuple(self.levels), self.limit, self.delay, self.temperature, self.wires, self.terminals


@dataclass(frozen=True)
class Point:
    setpoint: float
    voltage: float
    current: float
    status: int = 0

    @property
    def valid(self):
        return not self.status & (1 | 8 | 16 | 65536) and all(
            math.isfinite(v) and abs(v) < 1e30 for v in (self.voltage, self.current)
        )


def parse_reading(text, setpoint):
    parts = text.strip().split(",")
    if len(parts) != 3:
        raise ValueError(f"Ожидались VOLT,CURR,STAT; получено: {text[:160]}")
    voltage, current, status = map(float, parts)
    if not math.isfinite(status) or not status.is_integer() or not 0 <= status < 2 ** 24:
        raise ValueError("Некорректное слово состояния")
    point = Point(setpoint, voltage, current, int(status))
    if not point.valid:
        raise ValueError("Ограничение источника, перегрузка или недостоверное измерение")
    return point


def simulate(recipe, seed=None):
    recipe.validate()
    rng = random.Random(seed)
    shift = rng.uniform(-0.012, 0.012)
    idss = rng.uniform(0.024, 0.045)
    points = []
    for level in recipe.levels:
        if recipe.kind == "diode":
            voltage = 0.026 * 1.15 * math.log1p(level / 1e-8) + shift + level * 2
            current = level
            status = 8 if voltage >= recipe.limit else 0
        else:
            voltage = level
            current = idss * (1 - math.exp(-level / 0.7)) * (1 + level * 0.004)
            status = 8 if current >= recipe.limit else 0
        points.append(Point(level, voltage, current, status))
    return points


def match_groups(runs, size, tolerance):
    """Rank all compatible sets; a result may occur in multiple candidate sets."""
    if size not in (2, 4) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("Размер группы: 2 или 4; допуск должен быть положительным")
    if len(runs) > 60:
        raise ValueError("Выберите не более 60 измерений одной партии")
    ranked = []
    for group in combinations(runs, size):
        first = group[0]
        signature = Recipe(**first["recipe"]).signature()
        if any(Recipe(**r["recipe"]).signature() != signature or
               r["model"] != first["model"] or r["demo"] != first["demo"] or
               r["batch"] != first["batch"] or r["state"] != "complete" for r in group):
            continue
        if len({r["sample"] for r in group}) != size:
            continue
        if any(len(r["points"]) != len(signature[1]) or
               any(not Point(**p).valid for p in r["points"]) for r in group):
            continue
        field = "voltage" if signature[0] == "diode" else "current"
        worst = max(max(r["points"][i][field] for r in group) -
                    min(r["points"][i][field] for r in group) for i in range(len(signature[1])))
        if worst <= tolerance:
            ranked.append((worst, group))
    return sorted(ranked, key=lambda item: item[0])[:100]
