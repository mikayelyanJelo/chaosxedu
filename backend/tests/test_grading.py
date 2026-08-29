from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.errors import PermissionDeniedError, ValidationError
from app.core.i18n import Messages
from app.models.platform import DEFAULT_GRADE_SCALE
from app.services.domain.grading import (
    ScaleBand,
    apply_manual_grade,
    average_grade,
    default_scale,
    grade_assignment,
    percent_to_grade,
    resolve_grade_scale,
    round_half_up,
    term_final_grade,
    validate_scale,
)

SCALE = default_scale()


def _assignment() -> SimpleNamespace:
    return SimpleNamespace(
        correct_count=0,
        total_count=0,
        raw_percent=None,
        percent=None,
        penalty_percent=0,
        grade=None,
        auto_grade=None,
        grade_overridden_by_id=None,
        grade_overridden_at=None,
        grade_override_comment=None,
    )


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(100, 5), (85, 5), (84, 4), (70, 4), (69, 3), (50, 3), (49, 2), (0, 2)],
)
def test_percent_to_grade_boundaries(percent: int, expected: int) -> None:
    assert percent_to_grade(percent, SCALE) == expected


def test_percent_to_grade_clamps_out_of_range() -> None:
    assert percent_to_grade(120, SCALE) == 5
    assert percent_to_grade(-5, SCALE) == 2


def test_validate_scale_accepts_default() -> None:
    assert validate_scale(SCALE) is None


def test_validate_scale_rejects_gap() -> None:
    bands = [
        ScaleBand(5, 86, 100),
        ScaleBand(4, 70, 84),
        ScaleBand(3, 50, 69),
        ScaleBand(2, 0, 49),
    ]
    assert validate_scale(bands) == Messages.SCALE_INVALID


def test_validate_scale_rejects_overlap() -> None:
    bands = [
        ScaleBand(5, 80, 100),
        ScaleBand(4, 70, 84),
        ScaleBand(3, 50, 69),
        ScaleBand(2, 0, 49),
    ]
    assert validate_scale(bands) == Messages.SCALE_INVALID


def test_validate_scale_rejects_incomplete_coverage() -> None:
    assert validate_scale([ScaleBand(5, 0, 99)]) == Messages.SCALE_INVALID
    assert validate_scale([]) == Messages.SCALE_INVALID


def test_validate_scale_rejects_grade_out_of_range() -> None:
    bands = [ScaleBand(6, 50, 100), ScaleBand(2, 0, 49)]
    assert validate_scale(bands) == Messages.GRADE_OUT_OF_RANGE


def test_resolve_scale_prefers_school_when_allowed() -> None:
    school_scale = [
        {"grade": 5, "min_percent": 90, "max_percent": 100},
        {"grade": 4, "min_percent": 75, "max_percent": 89},
        {"grade": 3, "min_percent": 55, "max_percent": 74},
        {"grade": 2, "min_percent": 0, "max_percent": 54},
    ]
    settings = SimpleNamespace(
        grade_scale=DEFAULT_GRADE_SCALE, allow_school_scale_override=True
    )
    school = SimpleNamespace(grade_scale=school_scale)
    assert percent_to_grade(86, resolve_grade_scale(settings, school)) == 4


def test_resolve_scale_falls_back_when_override_forbidden() -> None:
    settings = SimpleNamespace(
        grade_scale=DEFAULT_GRADE_SCALE, allow_school_scale_override=False
    )
    school = SimpleNamespace(
        grade_scale=[{"grade": 5, "min_percent": 0, "max_percent": 100}]
    )
    assert percent_to_grade(86, resolve_grade_scale(settings, school)) == 5
    assert percent_to_grade(10, resolve_grade_scale(settings, school)) == 2


def test_resolve_scale_without_school_scale() -> None:
    settings = SimpleNamespace(
        grade_scale=DEFAULT_GRADE_SCALE, allow_school_scale_override=True
    )
    assert resolve_grade_scale(settings, SimpleNamespace(grade_scale=None)) == SCALE


@pytest.mark.parametrize(
    ("value", "expected"), [(2.5, 3), (3.5, 4), (4.5, 5), (4.49, 4), (4.0, 4)]
)
def test_round_half_up(value: float, expected: int) -> None:
    assert round_half_up(value) == expected


def test_average_grade_rounds_to_tenths() -> None:
    assert average_grade([5, 4, 4]) == 4.3
    assert average_grade([5, 4]) == 4.5


def test_average_grade_ignores_missing_and_empty() -> None:
    assert average_grade([]) is None
    assert average_grade([None, None]) is None
    assert average_grade([None, 5, 3]) == 4.0


def test_term_final_grade_uses_default_weights() -> None:
    # 0.5*4 + 0.5*3 = 3.5 -> 4
    assert term_final_grade([4, 4], [3]) == 4


def test_term_final_grade_weighs_averages_not_grades() -> None:
    # Три ДЗ не перевешивают одну КР: (2+5)/2 = 3.5 -> 4, а не (2+2+2+5)/4 -> 3.
    assert term_final_grade([2, 2, 2], [5]) == 4


def test_term_final_grade_rounds_half_up() -> None:
    # (4+5)/2 = 4.5 -> 5
    assert term_final_grade([4], [5]) == 5
    # ср(ДЗ) = 3.5 -> 4
    assert term_final_grade([3, 4], []) == 4


def test_term_final_grade_skips_empty_types() -> None:
    assert term_final_grade([5, 5], []) == 5
    assert term_final_grade([], []) is None


def test_term_final_grade_custom_weights() -> None:
    weights = {"homework": 70, "control": 30}
    # 0.7*4 + 0.3*2 = 3.4 -> 3
    assert term_final_grade([4], [2], weights) == 3


def test_grade_assignment_computes_percent_and_grade() -> None:
    assignment = _assignment()
    result = grade_assignment(assignment, 23, 25, 0, SCALE)
    assert result.raw_percent == 92
    assert result.percent == 92
    assert result.grade == 5
    assert assignment.grade == 5
    assert assignment.percent == 92


def test_grade_assignment_rounds_percent_half_up() -> None:
    result = grade_assignment(None, 1, 8, 0, SCALE)  # 12.5% -> 13%
    assert result.raw_percent == 13


def test_grade_assignment_applies_penalty() -> None:
    assignment = _assignment()
    result = grade_assignment(assignment, 18, 20, 10, SCALE)  # 90% - 10% = 80%
    assert result.raw_percent == 90
    assert result.percent == 80
    assert result.grade == 4


def test_grade_assignment_penalty_never_below_zero() -> None:
    result = grade_assignment(_assignment(), 5, 20, 200, SCALE)
    assert result.raw_percent == 25
    assert result.percent == 0
    assert result.grade == 2


def test_grade_assignment_without_tasks() -> None:
    result = grade_assignment(_assignment(), 0, 0, 0, SCALE)
    assert result.raw_percent == 0
    assert result.grade == 2


def test_grade_assignment_keeps_manual_grade() -> None:
    assignment = _assignment()
    assignment.grade = 5
    assignment.grade_overridden_at = "2026-01-01"
    grade_assignment(assignment, 5, 20, 0, SCALE)
    assert assignment.grade == 5
    assert assignment.auto_grade == 2


def test_manual_grade_requires_permission() -> None:
    teacher = SimpleNamespace(id=uuid.uuid4(), can_edit_grades=False)
    with pytest.raises(PermissionDeniedError) as exc:
        apply_manual_grade(_assignment(), 5, teacher)
    assert exc.value.message_key == Messages.GRADE_EDIT_FORBIDDEN


@pytest.mark.parametrize("grade", [1, 6, 0, -1])
def test_manual_grade_range(grade: int) -> None:
    teacher = SimpleNamespace(id=uuid.uuid4(), can_edit_grades=True)
    with pytest.raises(ValidationError) as exc:
        apply_manual_grade(_assignment(), grade, teacher)
    assert exc.value.message_key == Messages.GRADE_OUT_OF_RANGE


def test_manual_grade_keeps_percent_and_auto_grade() -> None:
    assignment = _assignment()
    grade_assignment(assignment, 10, 20, 0, SCALE)
    teacher = SimpleNamespace(id=uuid.uuid4(), can_edit_grades=True)

    apply_manual_grade(assignment, 4, teacher, comment="Работа переписана")

    assert assignment.grade == 4
    assert assignment.auto_grade == 3
    assert assignment.percent == 50
    assert assignment.grade_overridden_by_id == teacher.id
    assert assignment.grade_overridden_at is not None
    assert assignment.grade_override_comment == "Работа переписана"


def test_manual_grade_twice_keeps_original_auto_grade() -> None:
    assignment = _assignment()
    grade_assignment(assignment, 10, 20, 0, SCALE)
    teacher = SimpleNamespace(id=uuid.uuid4(), can_edit_grades=True)

    apply_manual_grade(assignment, 4, teacher)
    apply_manual_grade(assignment, 5, teacher)

    assert assignment.auto_grade == 3
    assert assignment.grade == 5
