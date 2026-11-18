"""The library plan (pure): Microsoft ownership against the TA collection -> TA adds, fixes and hand checks.

Ported from the games repo's tests/test_ta_library_fix.py (make_plan, slug_candidates, page_title /
accept_page) and the reconcile cases of tests/test_xbox_library.py, with fictional titles and ids, plus
the generation rules (the TA page's own Platform field decides) and the job's lookups and overrides.
"""

import re

from wsync.library import (Action, LibraryPlan, accept_page, candidate_slugs, lookup_key, override_for, plan_library,
                           reconcile, slug_candidates, ta_slug)
from wsync.owned import OwnedGame, game_key
from wsync.ta import ONE, SERIES, TAGame, page_title
from wsync.ta_coll import OwnedRow

X360, WIN = "Xbox 360", "Windows"
GEN8, GEN9 = "ConsoleGen8", "ConsoleGen9"
OWNED_DIGITAL = (("ownership", "Owned"), ("media", "Digital"))
P1, P2, P3, P4, P5 = (f"9ZZTEST0000{n}" for n in range(1, 6))


def owned(title, pid=P1, source="collections", acquisition="bought", evidence="paid", gen=(GEN9,), pc_only=False,
          pids=None):
    return OwnedGame(game_key(title), title, tuple(sorted(pids or (pid,))), source, acquisition, evidence, gen,
                     pc_only)


def row(ta_id, title, ownership="Owned", platform="xbox-series-x-s", media=""):
    return OwnedRow(ta_id, title, platform, ownership, media)


def page(ta_id, title, *platforms, slug=None):
    platforms = platforms or (SERIES,)
    return TAGame(ta_id, title, platforms[0], slug or re.sub(r"\W+", "-", title).strip("-"), tuple(platforms))


def plan(games, rows=(), pages=None, overrides=None, gwg=(), ea=()):
    return plan_library(list(games), list(rows), pages or {}, overrides or {}, set(gwg), set(ea))


def found(game, *pages):
    return {lookup_key(game): list(pages)}


def kinds(p):
    return [r.kind for r in p.review]


def writes(p):
    return [(a.kind, a.ta_id) for a in p.actions]


COLLECTION = [
    row(100, "Example Knight", "Xbox Game Pass (deprecated)"),
    row(101, "Example Max", "No longer have", media="Disc"),
    row(102, "Example Brawler (Windows)", "Xbox Game Pass (deprecated)", platform="windows"),
]


# ------------------------------------------------------------------ ported from test_ta_library_fix.py

def test_a_mislabelled_entry_becomes_owned_and_digital_when_media_is_blank():
    g = owned("Example Knight")
    p = plan([g], COLLECTION)
    assert p.actions == [Action("fix", g, 100, "", "Example Knight", OWNED_DIGITAL)]
    assert not p.review and not p.lookups and p.ok == 0


def test_a_mislabelled_entry_keeps_its_media():
    assert plan([owned("Example Max")], COLLECTION).actions[0].sets == (("ownership", "Owned"),)


def test_a_windows_entry_is_fixed_only_for_a_pc_only_purchase():
    console = owned("Example Brawler")
    p = plan([console], COLLECTION)
    assert p.actions == [] and p.lookups == [console]  # it looks for the console page instead
    pc = owned("Example Brawler", gen=(), pc_only=True)
    assert writes(plan([pc], COLLECTION)) == [("fix", 102)]


def test_a_missing_game_with_one_ta_page_is_added_as_owned_and_digital():
    g = owned("Example Peak")
    p = plan([g], pages=found(g, page(200, "Example Peak", slug="Example-Peak")))
    assert p.actions == [Action("add", g, 200, "Example-Peak", "Example Peak", OWNED_DIGITAL)]
    assert not p.review and not p.lookups


def test_a_free_game_on_tas_games_with_gold_list_is_added_as_games_with_gold():
    g = owned("Example Armory", acquisition="free", evidence="free")
    pages = found(g, page(201, "Example Armory"))
    assert plan([g], pages=pages, gwg={201}).actions[0].sets == (("ownership", "Games with Gold"),
                                                                 ("media", "Digital"))
    assert plan([g], pages=pages, gwg={999}).actions[0].sets == OWNED_DIGITAL  # free, but not a Games with Gold game
    bought = owned("Example Armory")
    assert plan([bought], pages=found(bought, page(201, "Example Armory")), gwg={201}).actions[0].sets == OWNED_DIGITAL


def test_possible_matches_and_ambiguous_games_go_to_review():
    arena = owned("Example Arena", pid=P1, acquisition="free", evidence="free")
    parts = owned("(Dis)Example", pid=P2, gen=(GEN8,))
    pages = found(parts, page(202, "(Dis)Example", ONE), page(203, "(Dis)Example (Xbox One)", ONE))
    p = plan([arena, parts], [row(300, "Example Arena: Heroes of Elsewhere")], pages)
    assert p.actions == [] and p.lookups == []  # a possible match needs no page lookup
    assert [(r.pid, r.kind, r.reason.split(":")[0]) for r in p.review] == [
        (P1, "possible-match", "possible match in TA"), (P2, "versions", "several TA game pages for Xbox One")]
    assert p.review[0].reason == "possible match in TA: Example Arena: Heroes of Elsewhere"


def test_a_console_purchase_with_only_a_windows_ta_entry_adds_the_console_entry():
    g = owned("Example Brawler")
    pages = found(g, page(304, "Example Brawler"), page(102, "Example Brawler (Windows)", WIN))
    p = plan([g], COLLECTION, pages)
    assert p.review == [] and writes(p) == [("add", 304)]  # the Windows entry is left as it is
    assert p.actions[0].sets == OWNED_DIGITAL


def test_a_subscription_status_is_overridden_only_with_a_paid_order_or_a_claim():
    for ev in ("unknown-order", "legacy"):  # TA: Xbox Game Pass (deprecated)
        p = plan([owned("Example Knight", evidence=ev)], COLLECTION)
        assert p.actions == [] and kinds(p) == ["subscription"]
        assert p.review[0].reason.startswith("no paid order on record: TA says Xbox Game Pass (deprecated)")
    for ev in ("paid", "claim"):
        assert writes(plan([owned("Example Knight", evidence=ev)], COLLECTION)) == [("fix", 100)]


def test_every_subscription_status_needs_a_paid_order():
    for status in ("PC Game Pass", "EA Play", "Ubisoft+", "Xbox Game Pass Premium", "Xbox Game Pass Standard"):
        p = plan([owned("Example Knight", evidence="legacy")], [row(100, "Example Knight", status)])
        assert p.actions == [] and kinds(p) == ["subscription"], status


def test_no_longer_have_is_fixed_on_any_active_licence():
    assert writes(plan([owned("Example Max", evidence="legacy")], COLLECTION)) == [("fix", 101)]
    renting = [row(105, "Example Max", "Renting")]
    assert writes(plan([owned("Example Max", evidence="unknown-order")], renting)) == [("fix", 105)]


def test_any_other_ta_status_is_left_alone_whatever_the_licence():
    """Only a subscription status (with a paid order or a claim) and No longer have / Renting are ever fixed."""
    others = ("Looking to trade/sell", "Loaned out", "Ordered", "Unable to play", "Borrowing", "Trial",
              "Some Status TA Adds Later", "")
    for status in others:
        for ev in ("paid", "claim"):
            p = plan([owned("Example Knight", evidence=ev)], [row(100, "Example Knight", status)])
            assert (p.actions, p.review, p.lookups) == ([], [], []), (status, ev)  # no fix, no review item
    # several entries of the game, none of them fixable: left alone too
    rows = [row(100, "Example Knight", "Loaned out"), row(101, "Example Knight (Xbox One)", "Trial", platform="xbox-one")]
    p = plan([owned("Example Knight", gen=(GEN8, GEN9))], rows)
    assert (p.actions, p.review) == ([], [])
    # a looked-up page whose entry is listed under another title: the same rule
    g = owned("Example Odd Name")
    p = plan([g], [row(111, "Example Proper Name", "Loaned out")], found(g, page(111, "Example Odd Name")))
    assert (p.actions, p.review) == ([], [])
    # a fixable entry beside a left-alone one is still several entries: a hand check, never a write
    rows = [row(100, "Example Knight", "Loaned out"), row(101, "Example Knight (Xbox One)", "No longer have",
                                                         platform="xbox-one")]
    p = plan([owned("Example Knight", gen=(GEN8, GEN9))], rows)
    assert p.actions == [] and kinds(p) == ["versions"]


def test_a_pre_2016_licence_for_an_ea_play_game_goes_to_review():
    g = owned("Example Field 4", evidence="legacy")
    pages = found(g, page(400, "Example Field 4"))
    p = plan([g], pages=pages, ea={400})
    assert p.actions == [] and kinds(p) == ["subscription"]
    assert p.review[0].reason.startswith("pre-2016 licence for an EA Play game")
    assert writes(plan([owned("Example Field 4")], pages=pages, ea={400})) == [("add", 400)]  # a paid one is added


def test_slug_candidates_follow_tas_url_form():
    assert slug_candidates("EXAMPLE OF TIDES: A Quiet Test") == ["EXAMPLE-OF-TIDES-A-Quiet-Test"]
    assert slug_candidates("Example Valley: HD Collection") == ["Example-Valley-HD-Collection"]
    assert slug_candidates("Example 40,000: Sidearm") == ["Example-40000-Sidearm"]
    assert slug_candidates("Some Designer's Example VI - Digital Deluxe Edition") == [
        "Some-Designers-Example-VI-Digital-Deluxe-Edition", "Some-Designers-Example-VI"]
    assert slug_candidates("EXAMPLECOM® 2") == ["EXAMPLECOM-2"]
    assert ta_slug("Fast & Example-Word") == "Fast-and-Example-Word"


def test_a_game_with_no_ta_entry_is_looked_up_on_its_guessed_pages():
    g = owned("Example Tales Remastered")
    p = plan([g])
    assert p.lookups == [g] and p.actions == [] and p.review == []
    assert candidate_slugs(g) == ["Example-Tales-Remastered", "Example-Tales-Remastered-Xbox-One",
                                  "Example-Tales-Remastered-Xbox-Series-XS"]


def test_page_title_and_acceptance():
    html = "<html><head><title>Example Valley HD Collection Achievements | TrueAchievements</title></head></html>"
    assert page_title(html) == "Example Valley HD Collection"
    assert accept_page("Example Valley: HD Collection", page(1, "Example Valley HD Collection"))
    assert not accept_page("Example Valley: HD Collection", page(1, "Example Valley 2"))
    assert accept_page("Example Saga", page(1, "Example Saga (Xbox 360)", X360))  # a platform tag is not the title
    assert accept_page("Example Saga", page(1, "Example Saga (Windows)", WIN))
    assert not accept_page("Example Saga", page(1, "", SERIES))


def test_a_console_purchase_with_only_a_windows_entry_looks_up_the_console_pages():
    g = owned("Example Brawler")
    p = plan([g], COLLECTION)
    assert p.lookups == [g] and p.actions == [] and p.review == []
    assert not any(s.endswith("-Windows") for s in candidate_slugs(g))


# ------------------------------------------------------------------ ported from test_xbox_library.py (reconcile)

def test_owned_games_are_ok_wrong_or_missing():
    games = [owned("Example Peak", pid=P1), owned("Example Gilded", pid=P2), owned("Example Depths", pid=P3),
             owned("Example Arena", pid=P4, acquisition="free", evidence="free"),
             owned("Example Home", pid=P5, acquisition="free", evidence="free")]
    rows = [row(1, "Example Gilded", "PC Game Pass"), row(2, "Example Depths"),
            row(3, "Example Arena: Heroes of Elsewhere"), row(4, "Example Home: Console Edition", "Games with Gold")]
    got = reconcile(games, rows)
    assert [(m.game.title, m.status) for m in got] == [
        ("Example Peak", "missing"), ("Example Gilded", "wrong"), ("Example Depths", "ok"),
        ("Example Arena", "missing"), ("Example Home", "ok")]  # a free claim vs Games with Gold is fine
    assert [m.hint for m in got if m.status == "missing"] == [None, "Example Arena: Heroes of Elsewhere"]
    assert [r.ta_id for r in got[1].rows] == [1]
    p = plan(games, rows)
    assert p.lookups == [games[0]] and writes(p) == [("fix", 1)] and kinds(p) == ["possible-match"]
    assert p.ok == 2


def test_a_free_game_in_ta_with_any_status_is_left_alone():
    g = owned("Example Knight", acquisition="free", evidence="free")
    assert [m.status for m in reconcile([g], COLLECTION)] == ["ok"]
    p = plan([g], COLLECTION)
    assert p.actions == [] and p.review == [] and p.ok == 1
    p = plan([g], COLLECTION, found(g, page(100, "Example Knight", slug="Example-Knight-X")),
             {P1: {"ta": "Example-Knight-X"}})
    assert p.actions == [] and p.review == [] and p.ok == 1


# ------------------------------------------------------------------ generations

def test_a_collections_licence_never_matches_a_360_page():
    saga = owned("Example Saga", gen=(GEN8,))
    old, new = page(500, "Example Saga", X360), page(501, "Example Saga (Xbox One)", ONE, SERIES)
    gone_360 = [row(500, "Example Saga", "No longer have", platform="xbox-360")]
    p = plan([saga], gone_360)
    assert p.lookups == [saga] and p.actions == []  # the 360 entry is another game: never fixed
    p = plan([saga], gone_360, found(saga, old))
    assert p.actions == [] and kinds(p) == ["generation"]
    assert writes(plan([saga], gone_360, found(saga, old, new))) == [("add", 501)]
    p = plan([saga], [row(500, "Example Saga", platform="xbox-360")], found(saga, old, new))
    assert writes(p) == [("add", 501)] and p.ok == 0  # an Owned 360 entry doesn't count either
    tagged = page(502, "Example Saga (Xbox 360)", ONE)  # a 360 title tag, whatever the Platform field says
    assert kinds(plan([saga], pages=found(saga, tagged))) == ["generation"]
    slugged = page(506, "Example Saga", ONE, slug="Example-Saga-Xbox-360")  # or a 360 slug
    assert kinds(plan([saga], pages=found(saga, slugged))) == ["generation"]


def test_an_orders_only_game_with_360_and_modern_pages_goes_to_review():
    saga = owned("Example Saga", source="orders", gen=(GEN8,))
    old, new = page(500, "Example Saga", X360), page(501, "Example Saga (Xbox One)", ONE, SERIES)
    plan_ = plan([saga], pages=found(saga, old, new))
    assert [r.kind for r in plan_.review] == ["versions"] and not plan_.actions
    both = page(503, "Example Saga", X360, ONE)  # one page listing both kinds
    assert kinds(plan([saga], pages=found(saga, both))) == ["versions"]


def test_an_orders_only_game_takes_the_360_page_or_the_modern_one_when_only_that_fits():
    saga = owned("Example Saga", source="orders", gen=(GEN8,))
    old, new = page(500, "Example Saga", X360), page(501, "Example Saga (Xbox One)", ONE, SERIES)
    assert writes(plan([saga], pages=found(saga, old))) == [("add", 500)]
    assert writes(plan([saga], pages=found(saga, new))) == [("add", 501)]
    original = page(504, "Example Saga (Xbox)", "Xbox")
    assert kinds(plan([saga], pages=found(saga, old, original))) == ["versions"]  # two last-generation pages
    pc = owned("Example Saga", source="orders", gen=(), pc_only=True)  # a PC-only licence is never a 360 game
    assert kinds(plan([pc], pages=found(pc, old))) == ["generation"]
    assert plan([pc], [row(500, "Example Saga", "No longer have", platform="xbox-360")]).lookups == [pc]


def test_an_orders_only_game_optimised_for_series_is_never_a_360_game():
    old = page(500, "Example Saga", X360)
    gone_360 = [row(500, "Example Saga", "No longer have", platform="xbox-360")]
    for gen in ((GEN9,), (GEN8, GEN9)):  # Series X|S, Smart Delivery
        g = owned("Example Saga", source="orders", gen=gen)
        p = plan([g], gone_360)
        assert p.actions == [] and p.lookups == [g], gen  # the 360 entry is another game: no fix
        p = plan([g], gone_360, found(g, old))
        assert p.actions == [] and kinds(p) == ["generation"], gen  # and its page no add
    bc = owned("Example Saga", source="orders", gen=(GEN8,))  # a backward compatible 360 purchase
    assert writes(plan([bc], gone_360)) == [("fix", 500)]
    assert writes(plan([bc], pages=found(bc, old))) == [("add", 500)]


def test_an_orders_only_copy_of_a_collections_game_never_writes_a_360_entry():
    old = page(500, "Example Saga", X360)
    series = page(501, "Example Saga", SERIES, slug="Example-Saga-Xbox-Series-XS")
    gone_360 = [row(500, "Example Saga", "No longer have", platform="xbox-360")]
    coll = owned("Example Saga", pid=P1)
    bundle = owned("Example Saga - Deluxe Bundle", pid=P2, source="orders")  # both ConsoleGen9
    p = plan([coll, bundle], gone_360, {P1: [old, series], P2: [old, series]})
    assert writes(p) == [("add", 501)] and p.review == []  # the Series add, and nothing written to the 360 entry
    coll8 = owned("Example Saga", pid=P1, gen=(GEN8,))
    bundle8 = owned("Example Saga - Deluxe Bundle", pid=P2, source="orders", gen=(GEN8,))
    p = plan([coll8, bundle8], gone_360)
    assert p.actions == [] and p.lookups == [coll8] and [(r.pid, r.kind) for r in p.review] == [(P2, "versions")]
    p = plan([coll8, bundle8], pages={P1: [old], P2: [old]})
    assert p.actions == [] and [(r.pid, r.kind) for r in p.review] == [(P1, "generation"), (P2, "versions")]
    assert writes(plan([bundle8], gone_360)) == [("fix", 500)]  # without a Collections copy: a 360 purchase


def test_fixes_never_cross_generations():
    coll = owned("Example Saga", gen=(GEN8,))
    orders = owned("Example Saga", source="orders", gen=(GEN8,))
    rows_360 = [row(500, "Example Saga", "No longer have", platform="xbox-360")]
    assert plan([coll], rows_360).actions == []
    assert writes(plan([orders], rows_360)) == [("fix", 500)]
    both = rows_360 + [row(501, "Example Saga (Xbox One)", "Xbox Game Pass (deprecated)", platform="xbox-one")]
    p = plan([orders], both)
    assert p.actions == [] and kinds(p) == ["versions"]
    assert p.review[0].reason.startswith("TA has Xbox 360 / Xbox and newer entries: Example Saga [xbox-360]")
    tagged = [row(502, "Example Saga (Xbox 360)", "No longer have", platform="xbox-one")]  # a 360 title tag
    assert plan([coll], tagged).actions == [] and writes(plan([orders], tagged)) == [("fix", 502)]
    assert writes(plan([coll], both)) == [("fix", 501)]  # the Collections licence sees only the modern entry


def test_several_entries_of_one_game_without_an_owned_one_go_to_review():
    rows = [row(600, "Example Quest", "Xbox Game Pass (deprecated)"),
            row(601, "Example Quest (Xbox One)", "No longer have", platform="xbox-one")]
    p = plan([owned("Example Quest", gen=(GEN8, GEN9))], rows)
    assert p.actions == [] and kinds(p) == ["versions"]
    p = plan([owned("Example Quest", gen=(GEN8, GEN9))], rows[:1] + [row(601, "Example Quest (Xbox One)")])
    assert p.actions == [] and p.ok == 1  # one Owned entry: nothing to do


def test_console_generation_picks_the_series_or_one_page():
    series = page(600, "Example Quest", SERIES, WIN)
    one = page(601, "Example Quest (Xbox One)", ONE, SERIES)

    def adds(gen, *pages):
        g = owned("Example Quest", gen=gen)
        return sorted(a.ta_id for a in plan([g], pages=found(g, *pages)).actions)

    assert adds((GEN9,), series, one) == [600]
    assert adds((GEN8,), series, one) == [601]
    assert adds((GEN8, GEN9), series, one) == [600, 601]  # Smart Delivery owns both
    shared = page(602, "Example Quest", ONE, SERIES)
    assert adds((GEN8, GEN9), shared) == [602]  # one page listing both: one add
    assert adds((GEN9,), shared) == [602]
    assert adds((GEN8, GEN9), shared, page(602, "Example Quest", ONE, SERIES, slug="Example-Quest-Gold")) == [602]
    assert adds((GEN9,), TAGame(604, "Example Quest", SERIES, "Example-Quest")) == [604]
    g = owned("Example Quest", gen=(GEN8,))
    p = plan([g], pages=found(g, page(603, "Example Quest", SERIES)))
    assert p.actions == [] and kinds(p) == ["generation"]
    g = owned("Example Quest", gen=())  # no console generation in the Store catalog
    p = plan([g], pages=found(g, series))
    assert p.actions == [] and kinds(p) == ["generation"]


def test_a_pc_only_purchase_adds_the_windows_page():
    console, windows = page(700, "Example Quest", SERIES), page(701, "Example Quest (Windows)", WIN)
    pc = owned("Example Quest", gen=(), pc_only=True)
    assert writes(plan([pc], pages=found(pc, console, windows))) == [("add", 701)]
    con = owned("Example Quest", gen=(GEN9,))
    assert writes(plan([con], pages=found(con, console, windows))) == [("add", 700)]
    assert kinds(plan([pc], pages=found(pc, console))) == ["generation"]
    anywhere = page(702, "Example Quest", SERIES, WIN)  # a console page that also lists Windows
    assert writes(plan([pc], pages=found(pc, anywhere))) == [("add", 702)]


# ------------------------------------------------------------------ lookups, overrides, merging, safety

def test_games_not_looked_up_yet_are_lookups_not_review():
    g = owned("Example Peak")
    p = plan([g])
    assert p.lookups == [g] and p.review == [] and p.actions == []
    assert plan([g], pages={lookup_key(g): None}).lookups == [g]
    p = plan([g], pages=found(g))  # looked up: no page at all
    assert p.lookups == [] and kinds(p) == ["no-page"]
    p = plan([g], pages=found(g, page(204, "Example Peak 2")))  # only another game's page
    assert p.actions == [] and kinds(p) == ["no-page"] and "Example Peak 2" in p.review[0].reason
    assert kinds(plan([g], pages=found(g, page(-7, "Example Peak")))) == ["no-page"]  # unreleased placeholder id


def test_a_review_and_a_lookup_are_keyed_by_the_games_first_product_id():
    g = owned("Example Peak", pids=(P3, P2))
    assert lookup_key(g) == P2
    assert plan([g], pages={P2: []}).review[0].pid == P2
    assert plan([g], pages={P3: []}).lookups == [g]


def test_overrides_skip_a_product_or_point_it_at_a_page():
    g = owned("Example Odd Name", pids=(P1, P2))
    rows = [row(900, "Example Odd Name", "Xbox Game Pass (deprecated)")]
    assert plan([g], rows, overrides={P2: {"ta": "skip"}}) == LibraryPlan([], [], [], 0)  # any product's skip
    assert candidate_slugs(g, "Example-Proper-Name") == ["Example-Proper-Name"]
    pinned = {P2: {"ta": "Example-Proper-Name"}}
    p = plan([g], rows, overrides=pinned)
    assert p.lookups == [g] and p.actions == []  # the title match (900) no longer counts
    proper = page(800, "Example Proper Name", X360, slug="Example-Proper-Name")  # title and generation unchecked
    p = plan([g], rows, found(g, proper), pinned)
    assert p.actions == [Action("add", g, 800, "Example-Proper-Name", "Example Proper Name", OWNED_DIGITAL)]
    listed = [row(800, "Example Proper Name", "No longer have", platform="xbox-360")]
    assert writes(plan([g], listed, found(g, proper), pinned)) == [("fix", 800)]
    p = plan([g], [row(800, "Example Proper Name", media="Disc")], found(g, proper), pinned)
    assert p.actions == [] and p.ok == 1


def test_an_override_whose_page_is_missing_is_an_override_review():
    g = owned("Example Odd Name", pids=(P1, P2))
    typo = {P2: {"ta": "Example-Proper-Nmae"}}
    assert plan([g], overrides=typo).lookups == [g]  # not looked up yet
    p = plan([g], pages=found(g), overrides=typo)  # looked up: nothing at that address
    assert p.actions == [] and p.lookups == [] and [(r.pid, r.kind) for r in p.review] == [(P2, "override")]
    assert p.review[0].reason == "the library override's TA page Example-Proper-Nmae was not found"
    p = plan([g], pages=found(g, page(801, "Example Odd Name")), overrides=typo)  # not the override's page
    assert p.actions == [] and p.lookups == [] and kinds(p) == ["override"]
    assert p.review[0].reason == ("the library override's TA page Example-Proper-Nmae was not found "
                                  "(found: Example Odd Name)")
    unreleased = page(-7, "Example Proper Name", slug="Example-Proper-Nmae")  # a placeholder id is no page to write
    assert kinds(plan([g], pages=found(g, unreleased), overrides=typo)) == ["override"]


def test_an_override_on_any_product_applies_to_its_game_and_skip_wins():
    g = owned("Example Odd Name", pids=(P1, P2, P3))
    assert override_for(g, {}) is None
    assert override_for(g, {P3: {"ta": "Example-B"}, P2: {"ta": "Example-A"}}) == "Example-A"  # first product's
    both = {P1: {"ta": "Example-A"}, P3: {"ta": "skip"}}
    assert override_for(g, both) == "skip" and plan([g], overrides=both) == LibraryPlan()


def test_candidate_slugs_and_variants_by_source():
    assert candidate_slugs(owned("Example Quest")) == [
        "Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS"]
    assert candidate_slugs(owned("Example Quest", gen=(), pc_only=True)) == [
        "Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS", "Example-Quest-Windows"]
    assert candidate_slugs(owned("Example Quest", source="orders", gen=(GEN8,))) == [
        "Example-Quest", "Example-Quest-Xbox-360", "Example-Quest-Xbox-One"]
    # an orders-only game optimised for Series X|S, or a PC-only one, is a modern game: no -Xbox-360
    assert candidate_slugs(owned("Example Quest", source="orders", gen=(GEN9,))) == [
        "Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS"]
    assert candidate_slugs(owned("Example Quest", source="orders", gen=(), pc_only=True)) == [
        "Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS", "Example-Quest-Windows"]
    # a purchase tier: the variants go on the base title; a version TA lists as its own game: on the full title
    assert candidate_slugs(owned("Example Quest - Deluxe Edition", pc_only=True)) == [
        "Example-Quest-Deluxe-Edition", "Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS",
        "Example-Quest-Windows"]
    assert candidate_slugs(owned("Example Quest: Definitive Edition", source="orders", gen=(GEN8,))) == [
        "Example-Quest-Definitive-Edition", "Example-Quest", "Example-Quest-Definitive-Edition-Xbox-360",
        "Example-Quest-Definitive-Edition-Xbox-One"]
    assert candidate_slugs(owned("Example Quest"), "skip") == []


def test_candidate_slugs_drop_platform_phrases_and_plain_tiers():
    bare = ["Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS"]
    for title in ("Example Quest for Xbox Series X|S", "Example Quest - Xbox Series X|S", "Example Quest (Xbox One)",
                  "Example Quest Xbox One & Xbox Series X|S", "Example Quest on Xbox One"):
        assert candidate_slugs(owned(title)) == bare, title  # never a variant stacked on a platform phrase
    assert candidate_slugs(owned("Example Quest Deluxe Edition")) == [
        "Example-Quest-Deluxe-Edition", "Example-Quest", "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS"]
    assert candidate_slugs(owned("Example Quest Definitive Edition")) == [  # a version TA lists as its own game
        "Example-Quest-Definitive-Edition", "Example-Quest-Definitive-Edition-Xbox-One",
        "Example-Quest-Definitive-Edition-Xbox-Series-XS"]
    longest = owned("Example Quest Gold Edition - Deluxe Edition for Windows", gen=(), pc_only=True)
    assert candidate_slugs(longest) == [  # most specific first, never more than 6
        "Example-Quest-Gold-Edition-Deluxe-Edition", "Example-Quest-Gold-Edition", "Example-Quest",
        "Example-Quest-Xbox-One", "Example-Quest-Xbox-Series-XS", "Example-Quest-Windows"]


def test_actions_on_one_ta_game_are_merged_keeping_the_stronger_write():
    coll = owned("Example Quest", pid=P1, acquisition="free", evidence="free")
    orders = owned("Example Quest", pid=P2, source="orders")
    shared = page(900, "Example Quest", SERIES)
    for games in ([coll, orders], [orders, coll]):
        p = plan(games, pages={P1: [shared], P2: [shared]}, gwg={900})
        assert [(a.ta_id, a.sets[0][1], a.game.pids) for a in p.actions] == [(900, "Owned", (P2,))]
    rows = [row(901, "Example Quest", "No longer have")]
    p = plan([owned("Example Quest", pid=P1), owned("Example Quest", pid=P2, source="orders")], rows)
    assert writes(p) == [("fix", 901)]


def test_nothing_is_planned_against_an_owned_entry_or_existing_media():
    games = [owned("Example Knight"), owned("Example Max", pid=P2), owned("Example Depths", pid=P3),
             owned("Example Peak", pid=P4, acquisition="free", evidence="free"), owned("Example Odd Name", pid=P5)]
    rows = COLLECTION + [row(110, "Example Depths", media="Disc"), row(111, "Example Proper Name", "Games with Gold")]
    pages = {P4: [page(112, "Example Peak")], P5: [page(111, "Example Proper Name", slug="Example-Proper-Name")]}
    p = plan(games, rows, pages, {P5: {"ta": "Example-Proper-Name"}}, gwg={112})
    by_id = {r.ta_id: r for r in rows}
    assert sorted(writes(p)) == [("add", 112), ("fix", 100), ("fix", 101)]
    for a in p.actions:
        sets = dict(a.sets)
        assert sets["ownership"] in ("Owned", "Games with Gold") and set(sets) <= {"ownership", "media"}
        if a.kind == "fix":
            assert by_id[a.ta_id].ownership not in ("Owned", "Games with Gold") and sets["ownership"] == "Owned"
            assert "media" not in sets or by_id[a.ta_id].media == ""
        else:
            assert a.ta_id not in by_id and sets["media"] == "Digital"


def test_rows_of_other_platforms_are_not_xbox_entries():
    g = owned("Example Peak")
    p = plan([g], [row(120, "Example Peak", "No longer have", platform="android")])
    assert p.actions == [] and p.lookups == [g]
