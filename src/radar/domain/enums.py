"""Closed vocabularies shared by every layer. Values are stable identifiers stored in the database."""

from enum import StrEnum


class Category(StrEnum):
    """What a filing or signal is about. Only TARGET categories become projects."""

    BESS = "bess"  # standalone battery energy storage
    BESS_HYBRID = "bess_hybrid"  # storage co-located with solar or wind
    STORAGE_SUBSTATION = "storage_substation"  # private substation built to connect storage
    DATACENTER = "datacenter"
    RENEWABLE_SUBSTATION = "renewable_substation"  # private substation for solar/wind only (network context)
    PUBLIC_GRID = "public_grid"  # grid operator substation, renovation (network context)
    BATTERY_WASTE = "battery_waste"  # used batteries, recycling: frequent false positive
    OTHER = "other"
    NEEDS_REVIEW = "needs_review"  # a human must decide (e.g. key pages unreadable)
    UNCLASSIFIED = "unclassified"  # no classifier available yet


TARGET_CATEGORIES = frozenset({Category.BESS, Category.BESS_HYBRID, Category.STORAGE_SUBSTATION, Category.DATACENTER})
CONTEXT_CATEGORIES = frozenset({Category.RENEWABLE_SUBSTATION, Category.PUBLIC_GRID})

CATEGORY_LABELS_FR: dict[Category, str] = {
    Category.BESS: "Stockage par batteries (autonome)",
    Category.BESS_HYBRID: "Stockage par batteries couplé à du solaire ou de l'éolien",
    Category.STORAGE_SUBSTATION: "Poste électrique privé servant à raccorder du stockage",
    Category.DATACENTER: "Centre de données (data center)",
    Category.RENEWABLE_SUBSTATION: "Poste électrique privé pour du solaire/éolien uniquement",
    Category.PUBLIC_GRID: "Poste public (gestionnaire de réseau) ou rénovation",
    Category.BATTERY_WASTE: "Batteries usagées, déchets, recyclage",
    Category.OTHER: "Autre",
    Category.NEEDS_REVIEW: "À vérifier par un humain",
    Category.UNCLASSIFIED: "Non classé",
}


class SourceKind(StrEnum):
    DREAL_CASE_BY_CASE = "dreal_case_by_case"
    MRAE_OPINION = "mrae_opinion"
    PREFECTURE = "prefecture"
    GEORISQUES = "georisques"
    COMPANY_REGISTRY = "company_registry"
    PRESS = "press"
    MANUAL = "manual"


class DocumentRole(StrEnum):
    FORM = "form"  # cerfa 14734 case-by-case request
    DECISION = "decision"
    OPINION = "opinion"
    ARTICLE = "article"
    OTHER = "other"


class ReviewDecision(StrEnum):
    """Outcome of the case-by-case environmental review."""

    NOT_SUBJECT = "not_subject"  # no impact study required
    SUBJECT = "subject"  # impact study required: public inquiry will follow
    PENDING = "pending"  # request filed, decision not published yet


class CheckStatus(StrEnum):
    OK = "ok"
    ALERT = "alert"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    REVIEW = "review"  # must be reviewed before publication


class PowerStatus(StrEnum):
    KNOWN = "known"  # read in a document
    ESTIMATED = "estimated"  # computed from physical clues, with a range
    UNKNOWN = "unknown"


class SignalKind(StrEnum):
    COMPANY = "company"  # newly created project company
    ARTICLE = "article"
    INSTALLATION = "installation"  # classified installation (Géorisques)
