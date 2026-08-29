from __future__ import annotations

from typing import Any, Final

DEFAULT_LANG: Final = "ru"
SUPPORTED_LANGS: Final = ("ru",)


class Messages:
    SCHOOL_SUSPENDED = "auth.school_suspended"
    PASSWORD_CHANGE_REQUIRED = "auth.password_change_required"
    TOKEN_INVALID = "auth.token_invalid"
    TOKEN_EXPIRED = "auth.token_expired"

    PERMISSION_DENIED = "common.permission_denied"
    NOT_FOUND = "common.not_found"
    VALIDATION_FAILED = "common.validation_failed"
    INTERNAL_ERROR = "common.internal_error"

    SCHOOL_NOT_FOUND = "school.not_found"
    STATS_HIDDEN = "profile.stats_hidden"

    GRADE_EDIT_FORBIDDEN = "grade.edit_forbidden"
    GRADE_OUT_OF_RANGE = "grade.out_of_range"
    SCALE_OVERRIDE_FORBIDDEN = "grade.scale_override_forbidden"
    SCALE_INVALID = "grade.scale_invalid"


_RU: dict[str, str] = {
    Messages.SCHOOL_SUSPENDED: "Доступ вашей школы временно приостановлен.",
    Messages.PASSWORD_CHANGE_REQUIRED: "Необходимо изменить пароль.",
    Messages.TOKEN_INVALID: "Сессия недействительна. Войдите заново.",
    Messages.TOKEN_EXPIRED: "Время сессии истекло. Войдите заново.",
    Messages.PERMISSION_DENIED: "Недостаточно прав для выполнения действия.",
    Messages.NOT_FOUND: "Запрашиваемый объект не найден.",
    Messages.VALIDATION_FAILED: "Проверьте правильность заполнения полей.",
    Messages.INTERNAL_ERROR: "Внутренняя ошибка сервера. Попробуйте позже.",
    Messages.SCHOOL_NOT_FOUND: "Школа не найдена.",
    Messages.STATS_HIDDEN: (
        "Школа закрыла доступ к расширенной статистике. Результаты "
        "выполненных работ остаются доступны."
    ),
    Messages.GRADE_EDIT_FORBIDDEN: "У вас нет права изменять оценки.",
    Messages.GRADE_OUT_OF_RANGE: "Оценка должна быть от 2 до 5.",
    Messages.SCALE_OVERRIDE_FORBIDDEN: (
        "Владелец платформы не разрешил школам переопределять шкалу оценок."
    ),
    Messages.SCALE_INVALID: (
        "Шкала должна покрывать диапазон от 0 до 100 процентов без пропусков "
        "и пересечений."
    ),
}

_CATALOG: dict[str, dict[str, str]] = {"ru": _RU}


def translate(key: str, lang: str = DEFAULT_LANG, **kwargs: Any) -> str:
    table = _CATALOG.get(lang) or _CATALOG[DEFAULT_LANG]
    template = table.get(key) or _CATALOG[DEFAULT_LANG].get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def resolve_lang(header_value: str | None) -> str:
    if not header_value:
        return DEFAULT_LANG
    for chunk in header_value.split(","):
        code = chunk.split(";")[0].strip().lower().split("-")[0]
        if code in SUPPORTED_LANGS:
            return code
    return DEFAULT_LANG
