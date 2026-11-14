"""Reads dealer notes (Finnish, Swedish, English) into facts that change how a car can move.

RULES.md: days count from the day the request arrives. A time without a day is on that day.
"tomorrow" is the next day, "the day after tomorrow" the day after. A weekday name means that
weekday 2 to 6 days after arrival. Digits are a 24-hour clock unless am/pm follows. An hour in
words is the working hour with that number, 9 to 17 ("two" is 14:00). "Half three" is 14:30.
A correction counts; a denied time ("not 3pm") does not.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

HOUR, DAY = 60, 1440


@dataclass
class NoteFacts:
    plates: bool = False
    manual: bool = False
    summer_tyres: bool = False
    pickup_from: Optional[int] = None  # absolute minute: car ready / released / not before
    pickup_hours: Optional[tuple[int, int]] = None  # daily (opens, closes), minutes of the day
    delivery_by: Optional[int] = None  # absolute minute: tighter deadline
    delivery_hours: Optional[tuple[int, int]] = None
    found: list = field(default_factory=list)


def _fold(text: str) -> str:
    """Lower case; keep ä/ö/å but also accept their plain forms by folding them."""
    t = text.lower()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return t


PLATES = re.compile(r"kilv|kilpi|rekkari|rekisterist|rekister.{0,12}(?:poist|pois)|skylt|avregistr|\bplates?\b|unregistered|deregistered|no reg")
MANUAL = re.compile(r"manuaali|kasivaiht|manuell|\bmanual\b|stick ?shift|kopplingspedal")
SUMMER = re.compile(r"kesaren|kesa renk|sommardack|summer tyre|summer tire")
# "Kesärenkaat vanteineen takakontissa" = spare summer tyres in the boot, not on the car.
SUMMER_SPARE = re.compile(r"kesarenkaat vante|kesarengasta|tavaratila|takakont|bagage|boot|trunk|kontissa")

DELIVERY = re.compile(r"toimit|perill|perille|vastaanot|kohde|destination|deliver|arriv|receiv|leverera|leverans|framme|mottag|\bdest\b")
PICKUP = re.compile(r"nouto|nouda|noudett|haett|hakea|haku|pickup|pick up|pick-up|collect|hamt|valmis|ready|klar|luovut|maksu|payment|betal")
AFTER = re.compile(r"jalkeen|alkaen|aikaisintaan|vasta|\bafter\b|\bfrom\b|earliest|not before|ei ennen|valmis|ready|efter|tidigast|inte fore|\bklar|until|luovutus|voi noutaa|saa hakea|noudettavissa|fran och med|\bfr\.?o\.?m")
BEFORE = re.compile(r"ennen|mennessa|viim|\bbefore\b|\bby\b|latest|\bfore\b|senast|innan|asti|saakka|\bclose|stang|kiinni|until|\bopen till|\btill\b")
CLOSES = re.compile(r"kiinni|we close|closes|stanger|stang|asti|saakka|open until|open till|auki vain|taalla klo")
RANGE = re.compile(r"(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?\s*(?:-|–|—|to|till)\s*(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?")
CLOCK = re.compile(r"(?:(?:klo|kl|klockan|at)\.?\s*)?\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?!\s*(?:min|km|tkm|%|€|eur|kpl|h\b))")

FI_WORDS = [  # longest first
    ("yhdeltatoista", 11), ("yhdentoista", 11), ("yksitoista", 11),
    ("kahdeltatoista", 12), ("kahdentoista", 12), ("kaksitoista", 12),
    ("yhdeksalta", 9), ("yhdeksan", 9), ("kymmenelta", 10), ("kymmenen", 10),
    ("yhdelta", 13), ("yhden", 13), ("yksi", 13), ("kahdelta", 14), ("kahden", 14), ("kaksi", 14),
    ("kolmelta", 15), ("kolmen", 15), ("kolme", 15), ("neljalta", 16), ("neljan", 16), ("nelja", 16),
    ("viidelta", 17), ("viiden", 17), ("viisi", 17),
]
SV_WORDS = [("nio", 9), ("tio", 10), ("elva", 11), ("tolv", 12), ("ett", 13), ("tva", 14), ("tre", 15), ("fyra", 16), ("fem", 17)]
EN_WORDS = [("eleven", 11), ("twelve", 12), ("noon", 12), ("nine", 9), ("ten", 10), ("one", 13), ("two", 14),
            ("three", 15), ("four", 16), ("five", 17)]
WORD_CUE = r"(?:klo|kl|klockan|at|by|before|after|from|until|efter|fore|innan|senast|ennen|jalkeen|alkaen|viim\.?|viimeistaan|half|halv|puoli)"

WEEKDAYS = [
    (r"maanantai|\bma\.?(?=\s*(?:klo|\d))|mandag|\bman\b|monday|\bmon\b", 0),
    (r"tiistai|\bti\.?(?=\s*(?:klo|\d))|tisdag|\btis\b|tuesday|\btues?\b", 1),
    (r"keskiviik|\bke\.?(?=\s*(?:klo|\d))|onsdag|\bons\b|wednesday|\bwed\b", 2),
    (r"torstai|\bto\.(?=\s*(?:klo|\d)?)|\bto(?=\s*klo)|torsdag|\btors?\b|thursday|\bthu(?:rs?)?\b", 3),
    (r"perjanta|\bpe\.?(?=\s*(?:klo|\d))|fredag|\bfre\b|friday|\bfri\b", 4),
    (r"lauanta|\bla\.?(?=\s*(?:klo|\d))|lordag|saturday|\bsat\b", 5),
]
DAY_WORDS = [
    (r"ylihuom|i overmorgon|day after tomorrow|overmorgon", 2),
    (r"huo+m+e*n+a|i ?morgon|tomorrow|\btmrw?\b|\btmr\b|\bimorgon", 1),
    (r"tanaan|idag|\btoday\b|\btonight\b", 0),
]


def _day_offset(clause: str, arrived_day: int) -> Optional[int]:
    """The day (world day number) a clause speaks of, or None for 'that day'."""
    for pat, off in DAY_WORDS:
        if re.search(pat, clause):
            return arrived_day + off
    for pat, wd in WEEKDAYS:
        if re.search(pat, clause):
            for d in range(2, 7):
                if (arrived_day + d) % 7 == wd:
                    return arrived_day + d
    return None


def _hm(h: int, m: int, ampm: Optional[str]) -> Optional[int]:
    if ampm:
        if ampm.startswith("p") and h < 12:
            h += 12
        if ampm.startswith("a") and h == 12:
            h = 0
    if h > 24 or m > 59:
        return None
    return h * HOUR + m


def _times(clause: str) -> list[tuple[int, int]]:
    """(position, minute of day) of each time in a clause, without denied ones."""
    out = []
    for m in CLOCK.finditer(clause):
        h = int(m[1])
        has_cue = m[0].strip()[:1].isalpha() or m[2] is not None or m[3] is not None
        if not has_cue:
            # a bare number: accept only next to a time word
            around = clause[max(0, m.start() - 14): m.end() + 10]
            if not re.search(r"saakka|asti|jalkeen|alkaen|mennessa|ennen|after|before|until|by|from|efter|fore|senast|innan|tidigast|latest|earliest|o'clock|uhr", around):
                continue
        t = _hm(h, int(m[2] or 0), m[3])
        if t is None:
            continue
        before = clause[max(0, m.start() - 8): m.start()]
        if re.search(r"(?:\bnot|\bei|\binte|\bnot at)\s*(?:klo|kl|at)?\s*$", before) and not re.search(r"(ennen|before|fore)", clause[m.start() - 14:m.start()] if m.start() >= 14 else ""):
            continue  # a denied time
        out.append((m.start(), t))
    # hours in words
    for words, add_half in ((FI_WORDS, "puoli"), (SV_WORDS, "halv"), (EN_WORDS, "half")):
        for w, h in words:
            for m in re.finditer(r"\b" + w + r"\b" if len(w) <= 5 else w, clause):
                pre = clause[max(0, m.start() - 16): m.start()]
                if not re.search(WORD_CUE + r"\s*$", pre) and not re.search(r"^\s*(?:jalkeen|alkaen|o'?clock|asti|saakka)", clause[m.end():m.end() + 10]):
                    continue
                t = h * HOUR
                if re.search(add_half + r"\s*$", pre):
                    t -= 30
                if re.search(r"(?:\bnot|\bei|\binte)\s*$", pre):
                    continue
                if not any(abs(p - m.start()) < 3 for p, _ in out):
                    out.append((m.start(), t))
    out.sort()
    return out


SPLIT = re.compile(r"(?<!\bviim)(?<!\bklo)(?<!\bkl)(?<!\bnro)(?<!\bn)(?<!\bto)(?<!\bpe)(?<!\bma)(?<!\bti)(?<!\bke)(?<!\bla)(?<!\bca)(?<!\bmm)\.(?=\s|$)|[!;/\n]|,(?=\s*(?:mutta|but|men)\b)")


def read_note(text: Optional[str], arrived_at: int) -> NoteFacts:
    facts = NoteFacts()
    if not text:
        return facts
    t = _fold(text)
    facts.plates = bool(PLATES.search(t)) and not re.search(r"kilvet (?:on )?paikoillaan|plates on|has plates|har skyltar", t)
    facts.manual = bool(MANUAL.search(t))
    facts.summer_tyres = bool(SUMMER.search(t)) and not SUMMER_SPARE.search(t)
    arrived_day = arrived_at // DAY
    for clause in SPLIT.split(t):
        clause = clause.strip(" ,")
        if not clause or not re.search(r"\d|" + "|".join(w for w, _ in FI_WORDS + EN_WORDS) + r"|\btva\b|\btre\b|\bfyra\b|\bfem\b|\btolv\b|\belva\b|\btio\b|\bnio\b", clause):
            continue
        is_delivery = bool(DELIVERY.search(clause)) and not re.search(r"nouto|haett|pickup|pick up|hamtning", clause)
        day = _day_offset(clause, arrived_day)
        # opening hours as a range
        rng = None
        for m in RANGE.finditer(clause):
            a = _hm(int(m[1]), int(m[2] or 0), m[3] or (m[6] if m[6] and not m[3] and int(m[1]) <= 12 and int(m[1]) > int(m[4]) else None))
            b = _hm(int(m[4]), int(m[5] or 0), m[6])
            if a is not None and b is not None and a < b and a >= 5 * HOUR:
                rng = (a, b)
        if rng:
            if is_delivery:
                facts.delivery_hours = rng
            else:
                facts.pickup_hours = rng
            facts.found.append(("range", is_delivery, rng))
            clause = RANGE.sub(" ", clause)
        times = _times(clause)
        if not times:
            continue
        # the correction counts: take the last time in the clause
        tod = times[-1][1]
        absolute = (day if day is not None else arrived_day) * DAY + tod
        if CLOSES.search(clause) and day is None:
            if is_delivery:
                o = facts.delivery_hours[0] if facts.delivery_hours else 0
                facts.delivery_hours = (o, tod)
            else:
                o = facts.pickup_hours[0] if facts.pickup_hours else 0
                facts.pickup_hours = (o, tod)
            facts.found.append(("closes", is_delivery, tod))
            continue
        if is_delivery:
            if BEFORE.search(clause) or re.search(r"must|oltava|pitaa|maste|ska vara", clause):
                facts.delivery_by = absolute if facts.delivery_by is None else min(facts.delivery_by, absolute)
                facts.found.append(("deliver_by", absolute))
            continue
        if AFTER.search(clause) and not re.search(r"\bennen\b|\bbefore\b|\bfore\b|senast|viim|latest", clause.split(str(times[-1][1] // 60))[0][-12:] if False else ""):
            if re.search(r"ei ennen|not before|inte fore", clause) or not BEFORE.search(clause) or re.search(r"jalkeen|alkaen|aikaisintaan|after|from|efter|tidigast|ready|valmis|klar|until", clause):
                facts.pickup_from = absolute if facts.pickup_from is None else max(facts.pickup_from, absolute)
                facts.found.append(("pickup_from", absolute))
                continue
        if BEFORE.search(clause):
            o = facts.pickup_hours[0] if facts.pickup_hours else 0
            c = facts.pickup_hours[1] if facts.pickup_hours else DAY
            facts.pickup_hours = (o, min(c, tod))
            facts.found.append(("pickup_before", tod))
    return facts


def apply_hours(hours, daily: Optional[tuple[int, int]]):
    """Stated opening hours narrowed by a daily range from a note."""
    if daily is None:
        return hours
    o, c = daily
    if hours is None:
        return {"opens": o, "closes": c, "weekdays": [0, 1, 2, 3, 4, 5, 6], "saturday": None}
    sat = hours.get("saturday")
    if sat:
        sat = {"opens": max(sat["opens"], o), "closes": min(sat["closes"], c)}
        if sat["opens"] >= sat["closes"]:
            sat = None
    opens, closes = max(hours["opens"], o), min(hours["closes"], c)
    if opens >= closes:
        opens, closes = o, c
    return {**hours, "opens": opens, "closes": closes, "saturday": sat}


def noted_legs(legs, facts: NoteFacts):
    """The request's legs with the note applied: narrower hours, a later ready time."""
    out = []
    for i, leg in enumerate(legs):
        pickup, delivery = dict(leg["pickup"]), dict(leg["delivery"])
        if not pickup["privateAddress"]:
            pickup["hours"] = apply_hours(pickup["hours"], facts.pickup_hours)
            if facts.pickup_from is not None and i == 0:
                pickup["window"] = {"from": max(pickup["window"]["from"], facts.pickup_from), "to": pickup["window"]["to"]}
        if not delivery["privateAddress"]:
            delivery["hours"] = apply_hours(delivery["hours"], facts.delivery_hours)
        out.append({**leg, "pickup": pickup, "delivery": delivery})
    return out
