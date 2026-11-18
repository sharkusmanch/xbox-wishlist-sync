"""What the Microsoft account owns, one OwnedGame per game, and the title keys used to match Store
titles with TrueAchievements titles.

Ported from the games repo's scripts/xbox-library.py (summarize, acquisition/evidence, the key
functions). New here: accents fold ("Örbit" == "Orbit") and a bare trailing platform phrase
("... Xbox One & Xbox Series X|S") is dropped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .xbox import Product

ORDER_KINDS = {"game": "Game", "durable": "Durable", "application": "Application", "pass": "Pass",
               "consumable": "Consumable", "unmanagedconsumable": "UnmanagedConsumable"}

ROMAN = {"2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi", "7": "vii", "8": "viii", "9": "ix"}
PLATFORM_TAG = re.compile(r"\((?:xbox one|xbox 360|xbox series x\s*\|?\s*s|xbox series x|windows|pc|xbox|ios|android)\)")
PLATFORM_SUFFIX = re.compile(r"(?:\s+(?:for|on)\s+(?:windows(?:\s*10)?|xbox one|xbox series x\s*\|?\s*s)"
                             r"|\s+xbox series x\s*\|\s*s)\s*$")
# A bare phrase at the very end ("Ball 21 Xbox One", "Ball 22 Xbox One and Xbox Series X|S"). It needs a
# title before it, so a title that is only a platform name, or one that has it mid-title, is left alone.
_PLAT = r"(?:xbox series x\s*\|\s*s|xbox series x|xbox one)"
BARE_PLATFORM = re.compile(rf"(?<=\S)\s+(?:(?:for|on)\s+)?{_PLAT}(?:\s+and\s+{_PLAT})*\s*$")
SEP = re.compile(r"\s+[-–—]\s+|\s*:\s*")  # " - ", " – ", ":"; not "Cross-Gen"
EPISODE = re.compile(r"(?:\s+[-–—]\s+|\s*:\s*)episode\s+\d+.*$")
# A final " - Something Edition" / ": Something Bundle" segment is a purchase tier, unless it names a version that
# TrueAchievements lists as its own game (Definitive, Remastered, Game of the Year, Ultimate, Legendary...).
TIER_END = re.compile(r"\b(?:edition|bundle)\s*$")
PROTECTED = re.compile(r"\b(?:definitive|anniversary|remaster\w*|enhanced|director|hd|complete|legendary|year|goty|"
                       r"ultimate|special|royal|golden)\b")
TIER_PLAIN = re.compile(r"\s+(?:standard|launch|digital|digital deluxe|super deluxe|deluxe|gold|premium|day one|"
                        r"console|windows|reloaded|vault|cross-gen)\s+(?:edition|bundle)\s*$")
NOISE = re.compile(r"\b(?:demo|beta|trial|test drive|pre-?order|pre-?purchase|tech test|(?:big|closed|open) alpha|"
                   r"artbook|soundtrack|upgrade|bonus content)\b", re.I)
TRAILING_TAG = re.compile(r"\s*\((?!\d{4}\))[^)]*\)\s*$")  # "(BATTLEMODE)", but not "(2008)"
HINT_WORDS = re.compile(r"\b(?:edition|bundle|collection|expansion|part|pack|season|episode|chapter|dlc|pass)\b")
SEQUEL_WORDS = set(ROMAN.values()) | {"x", "remake", "remaster", "remastered", "rebirth", "reborn", "reunion", "re"}
RANK = {"bought": 0, "claim": 1, "free": 2}
EVIDENCE_RANK = {"paid": 0, "claim": 1, "unknown-order": 2, "legacy": 3, "free": 4}


@dataclass(frozen=True)
class OwnedGame:
    key: str  # game_key of its title
    title: str  # the shortest Store title among its products
    pids: tuple[str, ...]  # every product id folded into this game
    source: str  # "collections" when any product has a Collections licence, else "orders"
    acquisition: str  # "bought" | "claim" | "free", the strongest across its products
    evidence: str  # "paid" | "claim" | "unknown-order" | "legacy" | "free", the strongest
    gen: tuple[str, ...]  # union of the products' console generations, ConsoleGen8 before ConsoleGen9
    pc_only: bool  # no product's catalog platforms include Xbox


# ---------------------------------------------------------------- what is owned

def _line_kept(li):
    return li.get("billingState") != "Refunded" and li.get("fulfillmentState") not in ("Revoked", "Canceled")


def summarize(items, orders, catalog):
    """One row per product: owned from an active, non-trial licence, or from a purchased order line that
    Collections doesn't list (Xbox 360 / original Xbox games bought in the modern store)."""
    rows = {}
    for it in items:
        pid = it.get("productId")
        if not pid:
            continue
        trial = bool(it.get("isTrial")) or it.get("skuType") == "Trial"
        status = it.get("status") or "Active"
        r = rows.setdefault(pid, {"product_id": pid, "title": "", "kind": it.get("productKind") or "",
                                  "source": "collections", "owned": False, "trial": False, "statuses": [],
                                  "acquired": ""})
        r["owned"] = r["owned"] or (status == "Active" and not trial)
        r["trial"] = r["trial"] or trial
        if status not in r["statuses"]:
            r["statuses"].append(status)
        acquired = it.get("acquiredDate") or ""
        if acquired and (not r["acquired"] or acquired < r["acquired"]):
            r["acquired"] = acquired
    for o in orders:
        if o.get("orderState") != "Purchased":
            continue
        for li in o.get("orderLineItems") or []:
            pid = li.get("productId")
            if not pid or pid in rows or not _line_kept(li):
                continue
            kind = li.get("productType") or ""
            rows[pid] = {"product_id": pid, "title": li.get("title") or "",
                         "kind": ORDER_KINDS.get(kind.lower(), kind), "source": "orders", "owned": True,
                         "trial": False, "statuses": ["Purchased"], "acquired": o.get("orderPlacedDate") or ""}
    for pid, r in rows.items():
        c = catalog.get(pid)
        r["title"] = (c.title if c else "") or r["title"]
        r["kind"] = r["kind"] or (c.kind if c else "")
        r["title_id"] = c.title_id if c else None
    return sorted(rows.values(), key=lambda r: (r["title"] or r["product_id"]).lower())


def order_line_amounts(orders):
    """{(order ID, product ID): amount paid} for completed purchases."""
    out = {}
    for o in orders:
        if o.get("orderState") != "Purchased":
            continue
        for li in o.get("orderLineItems") or []:
            if li.get("productId") and _line_kept(li):
                amt = li.get("totalAmount")
                out[(o.get("orderId"), li["productId"])] = float(amt if amt is not None else li.get("retailPrice") or 0)
    return out


def order_totals(orders):
    """{order ID: total paid} for completed purchases."""
    out = {}
    for (oid, _), amt in order_line_amounts(orders).items():
        out[oid] = out.get(oid, 0.0) + amt
    return out


def acquisition(pid, licences, lines, order_amount=None, totals=None):
    """How a product was acquired: bought; claim (no order, permanent licence: Disc to Digital, a code); or free
    ($0: Games with Gold, free-to-play, promotions). A licence from before the order history (Sept 2016) ends
    ~100 years after it was acquired; Games with Gold ones among them carry licensingRequired."""
    if not licences:
        return "bought" if (order_amount or 0) > 0 else "free"
    totals = totals or {}
    kinds = set()
    for lic in licences:
        oid = lic.get("orderId")
        if oid:
            amt = lines.get((oid, pid))
            if amt is None:
                amt = totals.get(oid)  # a pack or bundle order: what the whole order cost
            kinds.add("bought" if amt is None or amt > 0 else "free")  # still none: older than the history
        elif (lic.get("endDate") or "").startswith("9999"):
            kinds.add("claim")
        else:
            kinds.add("free" if lic.get("licensingRequired") else "bought")
    return min(kinds, key=RANK.get)


def evidence(pid, licences, lines, totals, order_amount=None):
    """How firm the ownership is: paid (a charged order line, or the paid order a bundle came in), claim (a
    permanent licence without an order), unknown-order (its order is missing from the history), legacy (a
    licence from before the order history; EA Access handed some out), free ($0)."""
    if not licences:
        return "paid" if (order_amount or 0) > 0 else "free"
    found = set()
    for lic in licences:
        oid = lic.get("orderId")
        if oid:
            amt = lines.get((oid, pid))
            if amt is None:
                amt = totals.get(oid)
            found.add("unknown-order" if amt is None else "paid" if amt > 0 else "free")
        elif (lic.get("endDate") or "").startswith("9999"):
            found.add("claim")
        else:
            found.add("free" if lic.get("licensingRequired") else "legacy")
    return min(found, key=EVIDENCE_RANK.get)


def _strongest(e, row):
    """Folds another product of the same game into e: the strongest acquisition and evidence win."""
    if RANK.get(row.get("acquisition"), 9) < RANK.get(e.get("acquisition"), 9):
        e["acquisition"] = row["acquisition"]
    if EVIDENCE_RANK.get(row.get("evidence"), 9) < EVIDENCE_RANK.get(e.get("evidence"), 9):
        e["evidence"] = row["evidence"]


def owned_games(items, orders, catalog: dict[str, Product]) -> list[OwnedGame]:
    """One OwnedGame per game (owned, kind Game, not a demo/beta/extra), strongest evidence kept."""
    lines, totals = order_line_amounts(orders), order_totals(orders)
    licences: dict[str, list] = {}
    for it in items:
        if (it.get("status") or "Active") == "Active" and not it.get("isTrial") and it.get("skuType") != "Trial":
            licences.setdefault(it.get("productId"), []).append(it)
    paid: dict[str, float] = {}
    for (_, pid), amt in lines.items():
        paid[pid] = max(paid.get(pid, 0.0), amt)
    groups: dict[str, dict] = {}
    for r in summarize(items, orders, catalog):
        if not r["owned"] or r["kind"] != "Game" or is_noise(r["title"]):
            continue
        pid = r["product_id"]
        row = dict(r, acquisition=acquisition(pid, licences.get(pid, []), lines, paid.get(pid), totals),
                   evidence=evidence(pid, licences.get(pid, []), lines, totals, paid.get(pid)))
        key = (r["source"], game_key(r["title"]) or pid)  # a Collections and an orders-only product stay apart
        e = groups.get(key)
        if e is None:
            groups[key] = {"title": r["title"] or pid, "pids": [pid], "collections": r["source"] == "collections",
                           "acquisition": row["acquisition"], "evidence": row["evidence"], "gen": set(),
                           "known": [], "xbox": False}
            e = groups[key]
        else:
            e["pids"].append(pid)
            e["collections"] = e["collections"] or r["source"] == "collections"
            if r["title"] and len(r["title"]) < len(e["title"]):
                e["title"] = r["title"]
            _strongest(e, row)
        c = catalog.get(pid)
        if c:
            e["known"].append(c)
            e["gen"].update(c.gen)
            e["xbox"] = e["xbox"] or "Xbox" in c.platforms
    out = [OwnedGame(key=game_key(e["title"]) or e["pids"][0], title=e["title"], pids=tuple(sorted(e["pids"])),
                     source="collections" if e["collections"] else "orders", acquisition=e["acquisition"],
                     evidence=e["evidence"], gen=tuple(sorted(e["gen"])),
                     pc_only=bool(e["known"]) and not e["xbox"]) for e in groups.values()]
    return sorted(out, key=lambda g: g.title.lower())


# ---------------------------------------------------------------- title keys

def _words(t):
    return " ".join(ROMAN.get(w, w) for w in re.sub(r"[^\w\s]", " ", t).split())


def _fold(t):
    """Accents off: NFKD, then drop the combining marks."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))


def _keys(title):
    """(cleaned title, game key): the game key also drops a purchase tier, an episode and a mode tag."""
    t = (title or "").lower().replace("&", " and ").replace("’", "'")
    for ch in "®™©":  # before NFKD, which would turn ™ into "tm"
        t = t.replace(ch, "")
    t = _fold(t)
    t = PLATFORM_SUFFIX.sub("", BARE_PLATFORM.sub("", PLATFORM_TAG.sub(" ", t).strip()))
    b = TRAILING_TAG.sub("", EPISODE.sub("", t))
    seps = list(SEP.finditer(b))
    if seps:
        seg = b[seps[-1].end():]
        if TIER_END.search(seg) and not PROTECTED.search(seg):
            b = b[:seps[-1].start()]
    b = TIER_PLAIN.sub("", b)
    return _words(t), _words(b)


def match_keys(title) -> set[str]:
    """Comparable forms of a game title: the cleaned title and its game key."""
    return {k for k in _keys(title) if k}


def game_key(title) -> str:
    return _keys(title)[1]


def same_game(a, b) -> bool:
    return bool(match_keys(a) & match_keys(b))


def is_noise(title) -> bool:
    return bool(NOISE.search(title or ""))


def _sequel(rest):
    w = rest.split()[:1]
    return bool(w) and (w[0].isdigit() or w[0] in SEQUEL_WORDS)


def hint(keys, titles) -> str | None:
    """A TA title that may be the same game, for a human to check: the shortest TA title that extends this one
    ("Arena" -> "Arena: Champions of the Realm"), else the longest TA title this one extends with an edition,
    bundle or expansion word ("Demon Hunt IV - Ultimate Edition" -> "Demon Hunt IV"). Sequels never count."""
    longer = shorter = None
    for title in titles:
        for tk in match_keys(title):
            for k in keys:
                if tk.startswith(k + " ") and not _sequel(tk[len(k):]):
                    if longer is None or len(tk) < len(longer[0]):
                        longer = (tk, title)
                elif k.startswith(tk + " ") and not _sequel(k[len(tk):]) and HINT_WORDS.search(k[len(tk):]):
                    if shorter is None or len(tk) > len(shorter[0]):
                        shorter = (tk, title)
    return (longer or shorter or (None, None))[1]
