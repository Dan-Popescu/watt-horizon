"""Retain quantity context before deciding whether one scalar project fact is justified."""

import re
from collections.abc import Iterable, Iterator
from typing import TypedDict

from radar.domain.quantities import NUMBER, measurements
from radar.domain.quantity_context import Asset, ProjectState, QuantityObservation
from radar.domain.text import fold, squash
from radar.reading.pdf import PdfContent
from radar.reading.tables import PdfTable

MAX_OBSERVATIONS = 200
_PHASE = re.compile(r"\b(?:phase|tranche)\s+([a-z0-9-]{1,24})\b")
_SCENARIO = re.compile(r"\b(?:scenario|variante|option)\s+([a-z0-9-]{1,24})\b")
_NUMBER = re.compile(rf"^\s*({NUMBER})(?:\s*(?:à|a|[-–])\s*({NUMBER}))?\s*$")
_UNIT = re.compile(r"\b([kMG]?Wh?(?:ac|dc|c)?)\b", re.IGNORECASE)
_PROJECT_NAME = re.compile(r"\bprojet\s+(?:nomme|denomme)\s+[«\"']?([a-z0-9_-]{2,40})\b")


class Context(TypedDict):
    asset: Asset
    state: ProjectState
    phase: str | None
    scenario: str | None
    project: str | None


def context(text: str, position: int | None = None) -> Context:
    normalized = fold(text)
    state: ProjectState
    asset: Asset
    existing = bool(re.search(r"\b(?:existant\w*|actuel\w*|ancien\w*)\b", normalized))
    proposed = bool(re.search(r"\b(?:projets?|prevu\w*|propose\w*|prevoit|futur\w*)\b", normalized))
    if existing and proposed:
        state = "unknown"
    elif existing:
        state = "existing"
    elif re.search(r"\b(?:exemple|hypotheti\w*|voisin\w*)\b", normalized):
        state = "example"
    elif proposed:
        state = "proposed"
    else:
        state = "unknown"
    storage = bool(re.search(r"stockage|batter|\bbess\b", normalized))
    solar = bool(re.search(r"photovolt|agrivolta|solaire|\beolien", normalized))
    if position is not None:
        position = len(fold(text[:position]))
        anchors: list[tuple[int, Asset]] = []
        asset_patterns: tuple[tuple[str, Asset], ...] = (
            (r"stockage|batter|\bbess\b", "storage"),
            (r"photovolt|agrivolta|solaire|\beolien", "solar"),
            (r"datacenter|data center|centres? de donnees", "datacenter"),
            (r"puissance de raccord|capacite de raccord|poste electrique", "connection"),
            (r"transformateur|onduleur|groupe electrogene", "equipment"),
        )
        for pattern, kind in asset_patterns:
            for hit in re.finditer(pattern, normalized):
                # Local association retains context; it never sums asset powers.
                anchors.append((abs(position - hit.end()), kind))
        asset = min(anchors, key=lambda item: item[0])[1] if anchors else "unknown"
    elif re.search(r"transformateur|onduleur|groupe electrogene", normalized):
        asset = "equipment"
    elif re.search(r"raccordement|puissance de raccord|poste electrique", normalized):
        asset = "connection"
    elif storage and solar:
        asset = "mixed"
    elif storage:
        asset = "storage"
    elif solar:
        asset = "solar"
    elif re.search(r"datacenter|data center|centres? de donnees", normalized):
        asset = "datacenter"
    else:
        asset = "unknown"
    phases, scenarios = set(_PHASE.findall(normalized)), set(_SCENARIO.findall(normalized))
    projects = set(_PROJECT_NAME.findall(normalized))
    return {
        "state": state,
        "asset": asset,
        "phase": next(iter(phases)) if len(phases) == 1 else "multiple" if phases else None,
        "scenario": next(iter(scenarios)) if len(scenarios) == 1 else "multiple" if scenarios else None,
        "project": next(iter(projects)) if len(projects) == 1 else "multiple" if projects else None,
    }


def table_observations(document_id: str, table: PdfTable) -> Iterator[QuantityObservation]:
    """Keep row context, including meaning carried only by the column headings."""
    if not table.rows:
        return
    headers = table.rows[0]
    header_quote = squash(" ".join(headers))
    if len(header_quote) > 550:
        return
    for row_index, row in enumerate(table.rows[1:], start=2):
        quote = squash(" ".join(row))
        if not 0 < len(quote) <= 550:
            continue
        contextual = context(quote)
        for header, cell in zip(headers, row, strict=False):
            if re.match(r"^(?:phase|tranche)\b", fold(header)) and cell.strip():
                contextual["phase"] = fold(cell)[:40]
            if re.match(r"^(?:scenario|variante|option)\b", fold(header)) and cell.strip():
                contextual["scenario"] = fold(cell)[:40]
        for column, cell in enumerate(row):
            inline = measurements(cell)
            unit_in_cell = bool(inline)
            if not inline and column < len(headers) and (number := _NUMBER.fullmatch(cell)):
                units = _UNIT.findall(headers[column])
                if len(units) == 1:
                    # Header normalization is an observation, never invented inline proof.
                    inline = measurements(f"{number[1]}" + (f" à {number[2]}" if number[2] else "") + " " + units[0])
            for value in inline:
                local: Context = {**contextual}
                column_header = headers[column] if column < len(headers) else ""
                column_context = context(column_header)
                for state in (context(header_quote)["state"], column_context["state"]):
                    if state in {"existing", "example"}:
                        local["state"] = "unknown" if local["state"] == "proposed" else state
                if column_context["asset"] != "unknown":
                    local["asset"] = (
                        column_context["asset"]
                        if local["asset"] == "unknown"
                        else local["asset"]
                        if local["asset"] == column_context["asset"]
                        else "mixed"
                    )
                units = _UNIT.findall(column_header)
                basis = value.basis
                if len(units) > 1:
                    local["state"] = "unknown"
                elif units:
                    header_unit = measurements("1 " + units[0])[0]
                    cell_units = [measurements("1 " + unit)[0] for unit in _UNIT.findall(cell)]
                    if header_unit.dimension != value.dimension or any(
                        unit.dimension != header_unit.dimension
                        or unit.high != header_unit.high
                        or (unit.basis and header_unit.basis and unit.basis != header_unit.basis)
                        for unit in cell_units
                    ):
                        local["state"] = "unknown"
                    elif not basis:
                        basis = header_unit.basis
                yield QuantityObservation(
                    document_id=document_id,
                    page=table.page,
                    locator=f"table:{table.index}:row:{row_index}:column:{column + 1}",
                    quote=quote,
                    header_quote=header_quote,
                    low=value.low,
                    high=value.high,
                    dimension=value.dimension,
                    basis=basis,
                    source_kind="table",
                    unit_in_cell=unit_in_cell,
                    **local,
                )


def observe_quantities(documents: Iterable[tuple[str, PdfContent]]) -> tuple[QuantityObservation, ...]:
    observations: list[QuantityObservation] = []
    for document_id, content in documents:
        table_pages = {table.page for table in content.tables}
        for page in range(1, content.pages + 1):
            text = content.ocr_texts.get(page) or content.page_texts[page - 1]
            for raw in re.split(r"(?<=[.;])\s+|\n\s*\n", text):
                quote = squash(raw)
                if not 0 < len(quote) <= 550:
                    continue
                for value in measurements(quote):
                    observation = QuantityObservation(
                        document_id=document_id,
                        page=page,
                        locator=f"page:{page}",
                        quote=quote,
                        low=value.low,
                        high=value.high,
                        dimension=value.dimension,
                        basis=value.basis,
                        source_kind="text",
                        **context(quote, value.start),
                    )
                    if page in table_pages and observation.eligible:
                        # Retain literal narrative readings but never claim they
                        # lost no context when this page also contains a table.
                        observation = observation.model_copy(update={"state": "unknown"})
                    observations.append(observation)
                    if len(observations) >= MAX_OBSERVATIONS:
                        return tuple(observations)
        for table in content.tables:
            for observation in table_observations(document_id, table):
                observations.append(observation)
                if len(observations) >= MAX_OBSERVATIONS:
                    return tuple(observations)
        for segment in content.segments:
            for raw in re.split(r"(?<=[.;])\s+|\n\s*\n", segment.text):
                quote = squash(raw)
                if not 0 < len(quote) <= 550:
                    continue
                values = measurements(quote)
                unit_in_cell = bool(values)
                units = _UNIT.findall(segment.header)
                if not values and (number := _NUMBER.fullmatch(quote)) and len(units) == 1:
                    values = measurements(f"{number[1]}" + (f" à {number[2]}" if number[2] else "") + " " + units[0])
                contextual = context(" ".join((segment.header, segment.context or quote)))
                for value in values:
                    reason = segment.reason or "uncertain segment" if segment.uncertain else ""
                    if len(units) > 1:
                        reason = reason or "ambiguous column units"
                    elif units:
                        header = measurements("1 " + units[0])[0]
                        cell_units = [measurements("1 " + unit)[0] for unit in _UNIT.findall(quote)]
                        if header.dimension != value.dimension or any(
                            unit.high != header.high
                            or unit.dimension != header.dimension
                            or (header.basis and unit.basis and header.basis != unit.basis)
                            for unit in cell_units
                        ):
                            reason = reason or "column and cell units disagree"
                    observations.append(
                        QuantityObservation(
                            document_id=document_id,
                            locator=segment.locator,
                            quote=quote,
                            header_quote=squash(segment.header)[:550],
                            low=value.low,
                            high=value.high,
                            dimension=value.dimension,
                            basis=value.basis or (measurements("1 " + units[0])[0].basis if len(units) == 1 else ""),
                            source_kind="table" if segment.header else "text",
                            unit_in_cell=unit_in_cell,
                            uncertain_reason=reason,
                            **contextual,
                        )
                    )
                    if len(observations) >= MAX_OBSERVATIONS:
                        return tuple(observations)
    return tuple(observations)
