from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.core.errors import PermissionDeniedError, ValidationError
from app.core.i18n import Messages
from app.models.base import WorkKind
from app.models.platform import (
    DEFAULT_GRADE_SCALE,
    DEFAULT_TERM_WEIGHTS,
)

MIN_GRADE = 2
MAX_GRADE = 5


@dataclass(frozen=True, slots=True)
class GradeDistribution:
    excellent: int = 0
    good: int = 0
    satisfactory: int = 0
    poor: int = 0

    @property
    def total(self) -> int:
        return self.excellent + self.good + self.satisfactory + self.poor


def distribution_of(grades: Iterable[int | None]) -> GradeDistribution:
    counts = {5: 0, 4: 0, 3: 0, 2: 0}
    for grade in grades:
        if grade is not None and grade in counts:
            counts[grade] += 1
    return GradeDistribution(
        excellent=counts[5],
        good=counts[4],
        satisfactory=counts[3],
        poor=counts[2],
    )


@dataclass(frozen=True, slots=True)
class ScaleBand:
    grade: int
    min_percent: int
    max_percent: int

    def contains(self, percent: float) -> bool:
        return self.min_percent <= percent <= self.max_percent

    def as_dict(self) -> dict[str, int]:
        return {
            "grade": self.grade,
            "min_percent": self.min_percent,
            "max_percent": self.max_percent,
        }


@dataclass(frozen=True, slots=True)
class GradeResult:
    correct: int
    total: int
    raw_percent: int
    percent: int
    grade: int
    penalty_percent: float


class _SettingsLike(Protocol):
    grade_scale: Any
    allow_school_scale_override: bool


class _SchoolLike(Protocol):
    grade_scale: Any


class _TeacherLike(Protocol):
    id: Any
    can_edit_grades: bool


class _AssignmentLike(Protocol):
    grade: int | None
    auto_grade: int | None
    grade_overridden_by_id: Any
    grade_overridden_at: Any


def round_half_up(value: float, digits: int = 0) -> float:
    # Встроенный round округляет по-банковски: 2.5 превращается в 2.
    quant = Decimal(1).scaleb(-digits)
    result = (Decimal(str(value)) / quant + Decimal("0.5")).to_integral_value(
        rounding="ROUND_FLOOR"
    ) * quant
    return float(result)


def parse_scale(raw: Iterable[Mapping[str, Any]] | None) -> list[ScaleBand]:
    if not raw:
        return []
    bands = [
        ScaleBand(
            grade=int(item["grade"]),
            min_percent=int(item["min_percent"]),
            max_percent=int(item["max_percent"]),
        )
        for item in raw
    ]
    return sorted(bands, key=lambda band: band.min_percent, reverse=True)


def default_scale() -> list[ScaleBand]:
    return parse_scale(DEFAULT_GRADE_SCALE)


def resolve_grade_scale(
    platform_settings: _SettingsLike | None,
    school: _SchoolLike | None = None,
) -> list[ScaleBand]:
    if platform_settings is None:
        return default_scale()

    # Используем собственную шкалу
    if (
        school is not None
        and getattr(school, "grade_scale", None)
        and platform_settings.allow_school_scale_override
    ):
        school_scale = parse_scale(school.grade_scale)
        if school_scale:
            return school_scale

    owner_scale = parse_scale(platform_settings.grade_scale)
    return owner_scale or default_scale()


def validate_scale(bands: Sequence[ScaleBand]) -> str | None:
    if not bands:
        return Messages.SCALE_INVALID

    grades = [band.grade for band in bands]
    if len(set(grades)) != len(grades):
        return Messages.SCALE_INVALID
    if any(band.grade < MIN_GRADE or band.grade > MAX_GRADE for band in bands):
        return Messages.GRADE_OUT_OF_RANGE

    ordered = sorted(bands, key=lambda band: band.min_percent)
    if ordered[0].min_percent != 0 or ordered[-1].max_percent != 100:
        return Messages.SCALE_INVALID

    expected_start = 0
    for band in ordered:
        if band.min_percent > band.max_percent:
            return Messages.SCALE_INVALID
        if band.min_percent != expected_start:
            return Messages.SCALE_INVALID
        expected_start = band.max_percent + 1

    # Отметка должна расти вместе с процентом.
    if [band.grade for band in ordered] != sorted(grades):
        return Messages.SCALE_INVALID
    return None


def percent_to_grade(percent: float, scale: Sequence[ScaleBand]) -> int:
    bands = scale or default_scale()
    ordered = sorted(bands, key=lambda band: band.min_percent, reverse=True)
    for band in ordered:
        if band.contains(percent):
            return band.grade
    # Процент после штрафов может выйти за 0..100 — прижимаем к краю шкалы.
    return ordered[0].grade if percent > ordered[0].max_percent else ordered[-1].grade


def average_grade(grades: Iterable[int | None]) -> float | None:
    values = [int(grade) for grade in grades if grade is not None]
    if not values:
        return None
    return round_half_up(sum(values) / len(values), 1)


def average_percent(percents: Iterable[float | None]) -> float | None:
    values = [float(percent) for percent in percents if percent is not None]
    if not values:
        return None
    return round_half_up(sum(values) / len(values), 1)


def term_final_grade(
    hw_grades: Iterable[int | None],
    control_grades: Iterable[int | None],
    weights: Mapping[str, float] | None = None,
) -> int | None:
    # Вес принадлежит средней по типу работ, а не отдельной оценке: сколько бы
    # ни было домашних заданий, их вклад в итог ограничен весом типа. Тип без
    # оценок выпадает из расчёта, итог считается по оставшемуся.
    scale_weights = {**DEFAULT_TERM_WEIGHTS, **(weights or {})}
    buckets = (
        (WorkKind.HOMEWORK.value, hw_grades),
        (WorkKind.CONTROL.value, control_grades),
    )

    numerator = 0.0
    denominator = 0.0
    for key, raw_grades in buckets:
        values = [int(grade) for grade in raw_grades if grade is not None]
        if not values:
            continue
        weight = float(scale_weights.get(key, 0))
        if weight <= 0:
            continue
        numerator += weight * (sum(values) / len(values))
        denominator += weight

    if denominator == 0:
        return None
    return int(round_half_up(numerator / denominator))


def grade_assignment(
    assignment: Any,
    correct: int,
    total: int,
    penalty_percent: float,
    scale: Sequence[ScaleBand],
) -> GradeResult:
    raw_percent = 0 if total <= 0 else int(round_half_up(correct * 100.0 / total))
    penalty = max(0.0, float(penalty_percent))
    percent = max(0, int(round_half_up(raw_percent - penalty)))
    grade = percent_to_grade(percent, scale)

    if assignment is not None:
        assignment.correct_count = correct
        assignment.total_count = total
        assignment.raw_percent = raw_percent
        assignment.percent = percent
        assignment.penalty_percent = penalty
        assignment.auto_grade = grade
        # Отметку, выставленную учителем вручную, пересчёт не трогает.
        if assignment.grade_overridden_at is None:
            assignment.grade = grade

    return GradeResult(
        correct=correct,
        total=total,
        raw_percent=raw_percent,
        percent=percent,
        grade=grade,
        penalty_percent=penalty,
    )


def apply_manual_grade(
    assignment: _AssignmentLike,
    new_grade: int,
    teacher: _TeacherLike,
    comment: str | None = None,
) -> None:
    if not getattr(teacher, "can_edit_grades", False):
        raise PermissionDeniedError(Messages.GRADE_EDIT_FORBIDDEN)
    if not isinstance(new_grade, int) or not MIN_GRADE <= new_grade <= MAX_GRADE:
        raise ValidationError(Messages.GRADE_OUT_OF_RANGE)

    # Автоматическую отметку запоминаем только при первой правке, иначе вторая
    # правка съест её и показывать рядом с оценкой станет нечего.
    if assignment.auto_grade is None:
        assignment.auto_grade = assignment.grade

    assignment.grade = new_grade
    assignment.grade_overridden_by_id = teacher.id
    assignment.grade_overridden_at = datetime.now(UTC)
    if comment is not None:
        assignment.grade_override_comment = comment
