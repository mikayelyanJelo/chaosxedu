from __future__ import annotations

from typing import Any

from openpyxl.cell.cell import TYPE_STRING


# Excel считает формулой любую ячейку, чей текст начинается со знака равенства,
# и openpyxl это повторяет. Фамилия вида =HYPERLINK(...) сработала бы у того,
# кто открыл выгрузку, а не у того, кто её ввёл. Апостроф не спасает: в готовом
# файле он остаётся видимым символом, поэтому назначаем строковый тип явно.
def text_cell(sheet: Any, *, row: int, column: int, value: str | None) -> Any:
    cell = sheet.cell(row=row, column=column, value=value)
    if isinstance(value, str):
        # После присваивания: сеттер значения сам ставит строке тип формулы.
        cell.data_type = TYPE_STRING
    return cell
