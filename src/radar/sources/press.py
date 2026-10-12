"""Specialised press via RSS (titles, summaries, links only). Articles are leads to verify, never facts.

Commune detection is case-sensitive, whole-word, and skips communes named like common words (« Contre ») and
« Calais » inside « Pas-de-Calais ».
"""

import email.utils
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from radar.domain.enums import Category, SignalKind, SourceKind
from radar.domain.models import Signal
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.sources.base import CollectReport
from radar.sources.html import strip_tags
from radar.store.db import Database

FEEDS: dict[str, str] = {
    "pv magazine France": "https://www.pv-magazine.fr/feed/",
    "ESS News": "https://www.ess-news.com/feed/",
    "Connaissance des énergies": "https://www.connaissancedesenergies.org/rss.xml",
    "Le Monde Informatique (datacenter)": "https://www.lemondeinformatique.fr/flux-rss/thematique/datacenter/rss.xml",
    "DCMag": "https://dcmag.fr/feed/",
}
TOPIC = re.compile(r"stockage|batter|\bBESS\b|storage|data ?cent|centre de donn|campus ia", re.IGNORECASE)
DATACENTER = re.compile(r"data ?cent|centre de donn|campus ia", re.IGNORECASE)
AMBIGUOUS_COMMUNES = frozenset({"Contre", "Moulins", "Campagne", "Vaux", "Bois", "Sains", "Rue", "Bus", "Fort"})


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    published: date | None
    summary: str


def parse_feed(xml_text: str) -> list[FeedItem]:
    root = ET.fromstring(xml_text)
    items: list[FeedItem] = []
    for it in root.iter("item"):
        published = None
        if raw := (it.findtext("pubDate") or "").strip():
            try:
                published = email.utils.parsedate_to_datetime(raw).date()
            except (TypeError, ValueError):
                published = None
        items.append(
            FeedItem(
                title=strip_tags(it.findtext("title") or ""),
                link=(it.findtext("link") or "").strip(),
                published=published,
                summary=strip_tags(it.findtext("description") or "")[:600],
            )
        )
    return items


def cited_communes(text: str, communes: Iterable[str]) -> tuple[str, ...]:
    cleaned = text.replace("Pas-de-Calais", " ")
    return tuple(
        sorted(
            {
                c
                for c in communes
                if len(c) > 3
                and c not in AMBIGUOUS_COMMUNES
                and re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", cleaned)
            }
        )
    )


def to_signal(item: FeedItem, source_name: str, communes: Iterable[str]) -> Signal | None:
    text = f"{item.title} {item.summary}"
    if not TOPIC.search(text):
        return None
    cited = cited_communes(text, communes)
    if not cited:
        return None  # national news without a place in the target area: out of scope for signals
    digest = hashlib.sha1(item.link.encode("utf-8")).hexdigest()[:16]
    return Signal(
        key=f"article:{digest}",
        kind=SignalKind.ARTICLE,
        source=SourceKind.PRESS,
        title=item.title,
        url=item.link,
        observed_on=item.published,
        communes=cited,
        category_hint=Category.DATACENTER if DATACENTER.search(text) else Category.BESS,
        payload={"feed": source_name, "summary": item.summary[:300]},
    )


def collect_press(http: PoliteHttp, db: Database, communes: Iterable[str]) -> CollectReport:
    report, names = CollectReport(), list(communes)
    for source_name, url in FEEDS.items():
        try:
            items = parse_feed(http.get_text(url))
        except (SiteBlockedError, ET.ParseError) as exc:
            report.errors.append(f"{source_name}: {exc}")
            continue
        for item in items:
            report.seen += 1
            if signal := to_signal(item, source_name, names):
                with db.transaction():
                    db.upsert_signal(signal)
                report.signals += 1
    return report
