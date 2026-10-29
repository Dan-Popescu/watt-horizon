"""Bounded structured table extraction inside the existing isolated PDF worker."""

from dataclasses import dataclass
from typing import Any

from radar.domain.text import squash


@dataclass(frozen=True)
class PdfTable:
    page: int
    index: int
    rows: tuple[tuple[str, ...], ...]
    bbox: tuple[float, float, float, float]

    @property
    def text(self) -> str:
        return "\n".join(squash(" ".join(row)) for row in self.rows)


def extract_tables(page: Any, *, max_rows: int, max_chars: int) -> tuple[tuple[PdfTable, ...], bool]:
    """Return extracted cells and a truncation marker; no silent row/character truncation.

    The caller bounds pages and owns the native-process deadline. Table recognition is
    opt-in because complex vector pages can be expensive even when no table is found.
    """
    tables: list[PdfTable] = []
    used_rows = used_chars = 0
    finder = page.find_tables()
    for index, table in enumerate(finder.tables, start=1):
        if table.row_count > max_rows - used_rows or table.col_count * table.row_count > max_chars - used_chars:
            return tuple(tables), True
        rows: list[tuple[str, ...]] = []
        for row in table.extract():
            cells = tuple(str(value or "") for value in row)
            size = sum(map(len, cells))
            if used_rows + 1 > max_rows or used_chars + size > max_chars:
                return tuple(tables), True
            used_rows += 1
            used_chars += size
            rows.append(cells)
        bbox = tuple(float(value) for value in table.bbox)
        tables.append(PdfTable(page.number + 1, index, tuple(rows), (bbox[0], bbox[1], bbox[2], bbox[3])))
    return tuple(tables), False
