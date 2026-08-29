from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.models import (
    DEFAULT_TERM_WEIGHTS,
    AssignmentStatus,
    ProgramQuarter,
    School,
    Topic,
    User,
    UserRole,
    Work,
    WorkAssignment,
    WorkKind,
)
from app.services.domain import grading
from app.services.domain.works import JOURNAL_KINDS


@dataclass(frozen=True, slots=True)
class JournalCell:
    assignment_id: uuid.UUID
    status: AssignmentStatus
    grade: int | None
    percent: float | None
    grade_overridden: bool


@dataclass(frozen=True, slots=True)
class JournalColumn:
    work: Work
    topics: list[str] = field(default_factory=list)


@dataclass(slots=True)
class JournalRow:
    student: User
    cells: dict[uuid.UUID, JournalCell] = field(default_factory=dict)
    average_total: float | None = None
    average_homework: float | None = None
    average_control: float | None = None
    term_grade: int | None = None


@dataclass(slots=True)
class Journal:
    columns: list[JournalColumn]
    rows: list[JournalRow]
    term_weights: dict[str, int]
    # Четверти отдаём вместе с сеткой, чтобы экран не ходил за ними в каталог
    # отдельным запросом перед тем, как спросить сам журнал.
    quarters: list[ProgramQuarter] = field(default_factory=list)
    program_quarter_id: uuid.UUID | None = None


def _matches_scope(work: Work, key: str, wanted: uuid.UUID | None) -> bool:
    # Достаточно одной задачи из выбранного класса или четверти. Требовать
    # совпадения целиком нельзя: работа по темам двух четвертей пропала бы из
    # журнала совсем, вместо того чтобы стоять в каждой из них.
    if wanted is None:
        return True
    values = (work.scope or {}).get(key) or []
    return str(wanted) in values


async def _roster(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    works: list[Work],
    include_current_members: bool,
) -> list[User]:
    # Одного текущего состава мало: ученик, переведённый в другую группу или
    # выпущенный, обязан остаться в журнале того года, в котором он учился, —
    # иначе выставленные ему отметки становятся недостижимы. Поэтому к составу
    # добавляются все, кому назначались работы этой сетки.
    #
    # Сам текущий состав добавляется только к журналу действующего года: в
    # архиве он дал бы пустые строки тех, кто тогда здесь не учился.
    ids: set[uuid.UUID] = set()
    if include_current_members:
        ids.update(
            await session.scalars(
                select(User.id).where(
                    User.group_id == group_id,
                    User.role == UserRole.STUDENT,
                    User.deleted_at.is_(None),
                )
            )
        )
    if works:
        ids.update(
            await session.scalars(
                select(WorkAssignment.student_id).where(
                    WorkAssignment.work_id.in_([work.id for work in works]),
                    WorkAssignment.is_retry.is_(False),
                )
            )
        )
    if not ids:
        return []

    return list(
        await session.scalars(
            select(User)
            .where(User.id.in_(ids), User.deleted_at.is_(None))
            .order_by(User.last_name, User.first_name)
        )
    )


async def _topic_names(
    session: AsyncSession, works: Sequence[Work]
) -> dict[uuid.UUID, list[str]]:
    # Темы берём из охвата работы, записанного при назначении, а не из её
    # состава: иначе на каждую колонку пришлось бы четыре JOIN.
    per_work: dict[uuid.UUID, list[uuid.UUID]] = {}
    wanted: set[uuid.UUID] = set()
    for work in works:
        ids = [uuid.UUID(value) for value in (work.scope or {}).get("topic_ids") or []]
        per_work[work.id] = ids
        wanted.update(ids)
    if not wanted:
        return {work.id: [] for work in works}

    rows = await session.execute(
        select(Topic.id, Topic.name, ProgramQuarter.ordinal, Topic.position)
        .join(ProgramQuarter, ProgramQuarter.id == Topic.program_quarter_id)
        .where(Topic.id.in_(wanted))
    )
    order: dict[uuid.UUID, tuple[int, int]] = {}
    names: dict[uuid.UUID, str] = {}
    for topic_id, name, quarter_ordinal, position in rows:
        names[topic_id] = name
        order[topic_id] = (quarter_ordinal, position)

    return {
        work_id: [
            names[topic_id]
            for topic_id in sorted(
                (value for value in ids if value in names),
                key=lambda value: order[value],
            )
        ]
        for work_id, ids in per_work.items()
    }


async def quarters_of(
    session: AsyncSession, program_grade_id: uuid.UUID | None
) -> list[ProgramQuarter]:
    if program_grade_id is None:
        return []
    return list(
        await session.scalars(
            select(ProgramQuarter)
            .where(ProgramQuarter.program_grade_id == program_grade_id)
            .order_by(ProgramQuarter.ordinal)
        )
    )


async def build_journal(
    session: AsyncSession,
    *,
    school: School,
    group_id: uuid.UUID,
    program_grade_id: uuid.UUID | None = None,
    program_quarter_id: uuid.UUID | None = None,
    auto_quarter: bool = False,
    kinds: Sequence[WorkKind] | None = None,
    academic_year_id: uuid.UUID | None = None,
    include_current_members: bool = True,
) -> Journal:
    quarters = await quarters_of(session, program_grade_id)
    # Экран журнала открывается без выбранной четверти и просит сетку сразу,
    # не выясняя состав программы отдельным запросом. Без этого флага пустая
    # четверть по-прежнему означает «все» — на этом стоит выгрузка в Excel.
    if program_quarter_id is None and auto_quarter and quarters:
        program_quarter_id = quarters[0].id
    statement = (
        select(Work)
        .where(
            Work.group_id == group_id,
            Work.school_id == school.id,
            Work.kind.in_(JOURNAL_KINDS),
            Work.deleted_at.is_(None),
        )
        # Номер вторым ключом: работы, созданные в одну секунду, иначе встают
        # произвольно и колонки переставляются между открытиями журнала.
        .order_by(Work.created_at, Work.number, Work.id)
    )
    if academic_year_id is not None:
        statement = statement.where(Work.academic_year_id == academic_year_id)
    works = list(await session.scalars(statement))
    works = [
        work
        for work in works
        if _matches_scope(work, "program_grade_ids", program_grade_id)
        and _matches_scope(work, "program_quarter_ids", program_quarter_id)
    ]

    # Фильтр по типу прячет колонки, но не влияет на средние и итог: они
    # считаются по всем работам, иначе при выборе «Домашние задания» итог за
    # четверть перестал бы быть итогом.
    wanted_kinds = frozenset(kinds) if kinds else None
    columns = [
        work for work in works if wanted_kinds is None or work.kind in wanted_kinds
    ]
    shown = {work.id for work in columns}

    students = await _roster(
        session,
        group_id=group_id,
        works=works,
        include_current_members=include_current_members,
    )
    rows = {student.id: JournalRow(student=student) for student in students}
    weights = {
        **DEFAULT_TERM_WEIGHTS,
        **((school.settings or {}).get("term_weights") or {}),
    }

    if works and students:
        assignments = await session.scalars(
            select(WorkAssignment)
            .where(
                WorkAssignment.work_id.in_([work.id for work in works]),
                WorkAssignment.student_id.in_([student.id for student in students]),
                WorkAssignment.is_retry.is_(False),
            )
            # Работы и ученики уже загружены выше, а joined-подгрузка
            # размножала бы их полные строки на каждую ячейку сетки.
            .options(noload(WorkAssignment.work), noload(WorkAssignment.student))
        )
        kind_by_work = {work.id: work.kind for work in works}
        grades: dict[uuid.UUID, dict[WorkKind, list[int]]] = {
            student.id: {kind: [] for kind in JOURNAL_KINDS} for student in students
        }

        for assignment in assignments:
            row = rows.get(assignment.student_id)
            if row is None:
                continue
            if assignment.work_id in shown:
                row.cells[assignment.work_id] = JournalCell(
                    assignment_id=assignment.id,
                    status=assignment.status,
                    grade=assignment.grade,
                    percent=(
                        float(assignment.percent)
                        if assignment.percent is not None
                        else None
                    ),
                    grade_overridden=assignment.grade_overridden_at is not None,
                )
            if assignment.grade is not None:
                kind = kind_by_work.get(assignment.work_id)
                if kind in grades[assignment.student_id]:
                    grades[assignment.student_id][kind].append(assignment.grade)

        for student_id, row in rows.items():
            by_kind = grades[student_id]
            row.average_total = grading.average_grade(
                [grade for values in by_kind.values() for grade in values]
            )
            row.average_homework = grading.average_grade(by_kind[WorkKind.HOMEWORK])
            row.average_control = grading.average_grade(by_kind[WorkKind.CONTROL])
            row.term_grade = grading.term_final_grade(
                by_kind[WorkKind.HOMEWORK],
                by_kind[WorkKind.CONTROL],
                weights,
            )

    topics_by_work = await _topic_names(session, columns)
    return Journal(
        columns=[
            JournalColumn(work=work, topics=topics_by_work.get(work.id, []))
            for work in columns
        ],
        rows=[rows[student.id] for student in students],
        term_weights=weights,
        quarters=quarters,
        program_quarter_id=program_quarter_id,
    )


async def work_summary(
    session: AsyncSession, *, work_id: uuid.UUID
) -> tuple[int, int, float | None]:
    assignments = list(
        await session.scalars(
            select(WorkAssignment)
            .where(
                WorkAssignment.work_id == work_id,
                WorkAssignment.is_retry.is_(False),
            )
            .options(noload(WorkAssignment.work), noload(WorkAssignment.student))
        )
    )
    submitted = [item for item in assignments if item.submitted_at is not None]
    average = grading.average_grade(
        [item.grade for item in submitted if item.grade is not None]
    )
    return len(assignments), len(submitted), average


def deadline_state(assignment: WorkAssignment, now: datetime) -> AssignmentStatus:
    if assignment.status in (AssignmentStatus.SUBMITTED, AssignmentStatus.OVERDUE):
        return assignment.status
    deadline = assignment.effective_deadline
    if deadline is not None and deadline < now:
        return AssignmentStatus.OVERDUE
    return assignment.status
