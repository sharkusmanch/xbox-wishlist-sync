"""Owned-game classification and title keys (pure logic, fictional data)."""
import pytest

from wsync.owned import (OwnedGame, acquisition, evidence, game_key, hint, is_noise, match_keys,
                         order_line_amounts, order_totals, owned_games, same_game, summarize)
from wsync.xbox import Product


def prod(pid, title, platforms=("PC", "Xbox"), gen=("ConsoleGen9",), kind="Game"):
    return Product(pid=pid, title=title, kind=kind, title_id=None, platforms=tuple(platforms), gen=tuple(gen))


def test_summarize_owned_games_from_collections_and_order_only_purchases():
    items = [
        {"productId": "GAME", "productKind": "Game", "status": "Active", "acquiredDate": "2024-01-02T00:00:00Z"},
        {"productId": "TRIAL", "productKind": "Game", "status": "Active", "isTrial": True},
        {"productId": "GONE", "productKind": "Game", "status": "Revoked"},
    ]
    orders = [
        {"orderId": "1", "orderState": "Purchased", "orderPlacedDate": "2022-05-01T00:00:00Z",
         "orderLineItems": [{"productId": "BC360", "productType": "Game", "title": "Old Example"},
                            {"productId": "GAME", "productType": "Game", "title": "Example"}]},
        {"orderId": "2", "orderState": "Refunded", "orderLineItems": [{"productId": "REFUND", "productType": "Game"}]},
        {"orderId": "3", "orderState": "Purchased", "orderLineItems": [
            {"productId": "LINEREFUND", "productType": "Game", "billingState": "Refunded",
             "fulfillmentState": "Revoked"}]},
    ]
    catalog = {"GAME": prod("GAME", "Example")}
    rows = {r["product_id"]: r for r in summarize(items, orders, catalog)}
    assert rows["GAME"]["owned"] and rows["GAME"]["source"] == "collections" and rows["GAME"]["title"] == "Example"
    assert not rows["TRIAL"]["owned"] and not rows["GONE"]["owned"]
    assert rows["BC360"]["owned"] and rows["BC360"]["source"] == "orders" and rows["BC360"]["title"] == "Old Example"
    assert "REFUND" not in rows and "LINEREFUND" not in rows


@pytest.mark.parametrize("store, ta", [
    ("Example Voyage® Odyssey - GOLD EDITION", "Example Voyage Odyssey"),
    ("Blockworld for Windows", "Blockworld (Windows)"),
    ("Example Ball® 21 Xbox Series X | S", "Example Ball 21"),
    ("Silent Cottage", "Silent Cottage: Console Edition"),
    ("Moon Tower 2: Stay Brave - Reloaded Edition", "Moon Tower 2: Stay Brave"),
    ("Sample Tale - The Series - Episode 1: Hollow Road", "Sample Tale: The Series"),
    ("Example Strike®: Modern Clash® II - Vault Edition", "Example Strike: Modern Clash II"),
    ("Space Hall™ 2", "Space Hall 2"),
    ("Example Strike®: Cold Front - Cross-Gen Bundle", "Example Strike: Cold Front"),
])
def test_same_game_across_store_and_ta_names(store, ta):
    assert same_game(store, ta)


@pytest.mark.parametrize("a, b", [
    ("Space Hall (2008)", "Space Hall"),
    ("Dim Lantern Remastered", "Dim Lantern"),
    ("Quartz Vale 5: Wardens", "Quartz Vale"),
    ("Harbor Boss II: Definitive Edition", "Harbor Boss II"),
])
def test_different_games_stay_apart(a, b):
    assert not same_game(a, b)


@pytest.mark.parametrize("title", ["Example™ Demo", "Tin Harbor™ 1 Open Beta", "Fiction Beast Pre-Order Bundle",
                                   "Example Strike®: Iron Dawn 4 - Private Beta", "Map Sample (Pre-Order)"])
def test_demos_betas_and_preorders_are_noise(title):
    assert is_noise(title)


def test_full_games_are_not_noise():
    assert not is_noise("Sample Jump") and not is_noise("Tin Harbor™ Standard Edition")


@pytest.mark.parametrize("title", ["Fiction Digital Artbook & Original Soundtrack",
                                   "Example Rally ®2 Open Multiplayer Tech Test", "Moss Giant Big Alpha",
                                   "Old Lore® Online: Coastland Upgrade - Prepurchase",
                                   "PALE RIDGE 2 Bonus Content (Digital Artbook and Soundtrack）"])
def test_extras_are_noise(title):
    assert is_noise(title)


def test_acquisition_types():
    lines = {("O1", "BOUGHT"): 59.99, ("O2", "FREE"): 0.0}
    lic = lambda pid, **kw: [dict({"productId": pid, "status": "Active", "endDate": "9999-12-31T00:00:00Z"}, **kw)]
    assert acquisition("BOUGHT", lic("BOUGHT", orderId="O1"), lines) == "bought"
    assert acquisition("FREE", lic("FREE", orderId="O2"), lines) == "free"
    assert acquisition("OLD", lic("OLD", orderId="O-2015"), lines) == "bought"
    assert acquisition("D2D", lic("D2D"), lines) == "claim"
    assert acquisition("LEGACY", lic("LEGACY", endDate="2115-03-01T00:00:00Z"), lines) == "bought"
    assert acquisition("GWG", lic("GWG", endDate="2114-10-04T00:00:00Z", licensingRequired=True), lines) == "free"
    assert acquisition("BC", [], lines, order_amount=4.99) == "bought"
    assert acquisition("BC0", [], lines, order_amount=0.0) == "free"


def test_order_line_amounts_come_from_purchased_orders_only():
    orders = [
        {"orderId": "O1", "orderState": "Purchased",
         "orderLineItems": [{"productId": "A", "totalAmount": 19.99}, {"productId": "B", "totalAmount": 0}]},
        {"orderId": "O2", "orderState": "Refunded", "orderLineItems": [{"productId": "C", "totalAmount": 9.99}]},
        {"orderId": "O3", "orderState": "CheckingOut", "orderLineItems": [{"productId": "D", "totalAmount": 5}]},
        {"orderId": "O4", "orderState": "Purchased",
         "orderLineItems": [{"productId": "E", "totalAmount": 9, "billingState": "Refunded",
                             "fulfillmentState": "Revoked"}]},
    ]
    assert order_line_amounts(orders) == {("O1", "A"): 19.99, ("O1", "B"): 0.0}


def test_acquisition_uses_the_order_total_when_the_product_is_not_a_line_of_it():
    lic = [{"productId": "GAME", "status": "Active", "orderId": "PACK", "endDate": "9999-12-31T00:00:00Z"}]
    assert acquisition("GAME", lic, {("PACK", "SKIN"): 0.0}, totals={"PACK": 0.0}) == "free"
    assert acquisition("GAME", lic, {("PACK", "BUNDLE"): 59.99}, totals={"PACK": 59.99}) == "bought"


def test_order_totals_sum_purchased_lines():
    orders = [{"orderId": "O1", "orderState": "Purchased",
               "orderLineItems": [{"productId": "A", "totalAmount": 5}, {"productId": "B", "totalAmount": 0}]},
              {"orderId": "O2", "orderState": "Canceled", "orderLineItems": [{"productId": "C", "totalAmount": 9}]}]
    assert order_totals(orders) == {"O1": 5.0}


def test_a_trailing_mode_tag_is_the_same_game_but_a_year_is_not():
    assert same_game("Example Blast (SPRINTMODE)", "Example Blast")
    assert not same_game("Space Hall (2008)", "Space Hall")


@pytest.mark.parametrize("store, ta, want", [
    ("Arena Sample", ["Arena Sample: Heroes of Dusk"], "Arena Sample: Heroes of Dusk"),
    ("Dark Spire III", ["Dark Spire"], None),
    ("Example Strike®: Iron Dawn 4", ["Example Strike"], None),
    ("Demon Hunt® IV - Ultimate Edition", ["Demon Hunt IV", "Demon Hunt"], "Demon Hunt IV"),
    ("Grave Run 4 Bundle", ["Grave Run", "Grave Run 4"], "Grave Run 4"),
    ("ORBIT SAGA VII", ["ORBIT SAGA VII REMAKE PLUS"], None),
])
def test_possible_match_hints(store, ta, want):
    assert hint(match_keys(store), ta) == want


def test_accents_fold():
    assert same_game("Örbit Tale", "Orbit Tale")


def test_a_bare_trailing_platform_phrase_is_stripped():
    assert same_game("Example Ball 21 Xbox One", "Example Ball 21")
    assert same_game("Example Ball 22 Xbox One & Xbox Series X|S", "Example Ball 22")
    assert same_game("Example for Xbox One", "Example")
    assert same_game("Example on Xbox Series X|S", "Example")
    assert same_game("Example for Xbox Series X|S", "Example")
    assert same_game("Example for Windows 10", "Example")


def test_a_separator_left_by_a_platform_phrase_does_not_block_the_tier():
    assert game_key("Example Quest Premium Edition - Xbox One") == "example quest"
    assert same_game("Example Quest Premium Edition - Xbox One", "Example Quest")
    assert same_game("Example Quest Deluxe Edition: Xbox Series X|S", "Example Quest")
    assert same_game("Example Quest - Xbox One", "Example Quest")


def test_a_platform_in_the_middle_or_alone_is_not_mangled():
    assert game_key("Xbox One Racer 2") == "xbox one racer ii"
    assert game_key("Xbox One") == "xbox one"
    assert not same_game("Example Ball Xbox One Edition Plus", "Example Ball")


def _lic(pid, **kw):
    return dict({"productId": pid, "productKind": "Game", "status": "Active",
                 "endDate": "9999-12-31T00:00:00Z"}, **kw)


def test_one_row_per_game_when_microsoft_has_several_products():
    items = [_lic("A1", orderId="O1"), _lic("A2", orderId="O2"), _lic("B1", orderId="O1"), _lic("B2", orderId="O1")]
    orders = [{"orderId": "O1", "orderState": "Purchased", "orderLineItems": [
        {"productId": "A1", "totalAmount": 10}, {"productId": "B1", "totalAmount": 10}, {"productId": "B2", "totalAmount": 10}]},
        {"orderId": "O2", "orderState": "Purchased", "orderLineItems": [{"productId": "A2", "totalAmount": 0}]}]
    catalog = {"A1": prod("A1", "Black Forest"), "A2": prod("A2", "Black Forest - Standard Edition"),
               "B1": prod("B1", "Twin Peak Clinic™"), "B2": prod("B2", "Twin Peak Clinic: MEGA Edition")}
    games = {g.pids: g for g in owned_games(items, orders, catalog)}
    assert set(games) == {("A1", "A2"), ("B1", "B2")}
    g = games[("A1", "A2")]
    assert g.title == "Black Forest" and g.acquisition == "bought" and g.evidence == "paid"
    assert g.key == "black forest"


def test_tier_editions_collapse_but_protected_editions_and_numbered_entries_stay_apart():
    items = [_lic(p) for p in ("F1", "F2", "F3", "U1", "M1", "M2")]
    catalog = {p: prod(p, t) for p, t in [
        ("F1", "Track Master 7"), ("F2", "Track Master 7 Deluxe Edition"), ("F3", "Track Master 7 - Gold Edition"),
        ("U1", "Track Master 7 Ultimate Edition"),
        ("M1", "Tiny Bot X Legacy Collection"), ("M2", "Tiny Bot X Legacy Collection 2")]}
    out = owned_games(items, [], catalog)
    assert [(g.title, g.pids) for g in out] == [
        ("Tiny Bot X Legacy Collection", ("M1",)), ("Tiny Bot X Legacy Collection 2", ("M2",)),
        ("Track Master 7", ("F1", "F2", "F3")), ("Track Master 7 Ultimate Edition", ("U1",))]


def test_a_remaster_edition_is_a_separate_game_from_the_order_only_original():
    items = [_lic("DE")]
    orders = [{"orderId": "O1", "orderState": "Purchased", "orderLineItems": [
        {"productId": "OG", "productType": "Game", "title": "Example Saga", "totalAmount": 10}]}]
    catalog = {"DE": prod("DE", "Example Saga: Definitive Edition", gen=("ConsoleGen9",)),
               "OG": prod("OG", "Example Saga", gen=("ConsoleGen8",))}
    games = {g.pids: g for g in owned_games(items, orders, catalog)}
    assert set(games) == {("DE",), ("OG",)}
    assert (games[("OG",)].source, games[("OG",)].gen, games[("OG",)].evidence) == ("orders", ("ConsoleGen8",), "paid")
    assert games[("DE",)].source == "collections" and games[("DE",)].gen == ("ConsoleGen9",)


def test_a_collections_product_and_an_orders_only_product_with_one_title_stay_two_games():
    items = [_lic("C1")]
    orders = [{"orderId": "O1", "orderState": "Purchased", "orderLineItems": [
        {"productId": "O1P", "productType": "Game", "title": "Same Title", "totalAmount": 5}]}]
    catalog = {"C1": prod("C1", "Same Title")}
    out = owned_games(items, orders, catalog)
    assert sorted((g.source, g.pids) for g in out) == [("collections", ("C1",)), ("orders", ("O1P",))]


def test_evidence_levels():
    lines = {("O1", "PAID"): 19.99, ("O2", "ZERO"): 0.0, ("O3", "OTHER"): 35.99}
    totals = {"O1": 19.99, "O2": 0.0, "O3": 35.99}
    lic = lambda pid, **kw: [dict({"productId": pid, "status": "Active", "endDate": "9999-12-31T00:00:00Z"}, **kw)]
    assert evidence("PAID", lic("PAID", orderId="O1"), lines, totals) == "paid"
    assert evidence("BUNDLED", lic("BUNDLED", orderId="O3"), lines, totals) == "paid"
    assert evidence("ZERO", lic("ZERO", orderId="O2"), lines, totals) == "free"
    assert evidence("GONE", lic("GONE", orderId="O-OLD"), lines, totals) == "unknown-order"
    assert evidence("D2D", lic("D2D"), lines, totals) == "claim"
    assert evidence("OLD", lic("OLD", endDate="2115-01-01T00:00:00Z"), lines, totals) == "legacy"
    assert evidence("GWG", lic("GWG", endDate="2115-01-01T00:00:00Z", licensingRequired=True), lines, totals) == "free"
    assert evidence("BC", [], lines, totals, order_amount=4.99) == "paid"


def test_grouped_rows_keep_the_strongest_evidence():
    items = [_lic("A1", orderId="GONE"), _lic("A2", orderId="O1")]
    orders = [{"orderId": "O1", "orderState": "Purchased",
               "orderLineItems": [{"productId": "A2", "totalAmount": 20}]}]
    catalog = {"A1": prod("A1", "Game X"), "A2": prod("A2", "Game X - Deluxe Edition")}
    (g,) = owned_games(items, orders, catalog)
    assert (g.acquisition, g.evidence) == ("bought", "paid")


def test_an_owned_game_carries_source_generation_and_pc_only():
    orders = [{"orderId": "O1", "orderState": "Purchased", "orderPlacedDate": "2022-01-01T00:00:00Z",
               "orderLineItems": [{"productId": "B1", "productType": "Game", "title": "Order Only", "totalAmount": 5}]}]
    (g,) = owned_games([], orders, {"B1": prod("B1", "Order Only", gen=("ConsoleGen8",))})
    assert (g.source, g.gen, g.pc_only) == ("orders", ("ConsoleGen8",), False)
    assert isinstance(g, OwnedGame) and g.acquisition == "bought" and g.evidence == "paid"


def test_an_owned_game_carries_its_latest_acquisition():
    """The latest acquisition of any of its products: an active, non-trial licence's acquiredDate or a purchased
    order line's orderPlacedDate, as UTC ISO text whatever form Microsoft wrote it in; None when none is known."""
    items = [_lic("A1", orderId="O1", acquiredDate="2024-01-02T03:04:05.1234567+00:00"),
             _lic("A2", acquiredDate="2025-06-01T10:00:00Z"),
             _lic("A2", status="Revoked", acquiredDate="2026-01-01T00:00:00Z"),  # not an owning licence
             _lic("A2", isTrial=True, acquiredDate="2026-02-01T00:00:00Z"),  # a trial: not one either
             _lic("N1"),  # no date at all
             _lic("B1", acquiredDate="not a date")]
    orders = [{"orderId": "O1", "orderState": "Purchased", "orderPlacedDate": "2024-01-02T03:00:00+00:00",
               "orderLineItems": [{"productId": "A1", "totalAmount": 10}]},
              {"orderId": "O2", "orderState": "Purchased", "orderPlacedDate": "2025-07-04T09:30:00.5+00:00",
               "orderLineItems": [{"productId": "C1", "productType": "Game", "title": "Order Only",
                                   "totalAmount": 5}]},
              {"orderId": "O3", "orderState": "Purchased", "orderPlacedDate": "2026-03-01T00:00:00+00:00",
               "orderLineItems": [{"productId": "C1", "totalAmount": 5, "billingState": "Refunded",
                                   "fulfillmentState": "Revoked"}]},  # refunded: not a purchase
              {"orderId": "O4", "orderState": "Canceled", "orderPlacedDate": "2026-04-01T00:00:00+00:00",
               "orderLineItems": [{"productId": "N1", "totalAmount": 5}]},
              {"orderId": "O5", "orderState": "Purchased", "orderPlacedDate": "2025-09-09T09:09:09Z",
               "orderLineItems": [{"productId": "A1", "totalAmount": 3}]}]  # A1's latest: after its licence
    catalog = {"A1": prod("A1", "Example Deep"), "A2": prod("A2", "Example Deep - Deluxe Edition"),
               "N1": prod("N1", "Example Plain"), "B1": prod("B1", "Example Broken")}
    games = {g.title: g for g in owned_games(items, orders, catalog)}
    assert games["Example Deep"].pids == ("A1", "A2")
    assert games["Example Deep"].acquired == "2025-09-09T09:09:09+00:00"
    assert games["Order Only"].acquired == "2025-07-04T09:30:00.500000+00:00"
    assert games["Example Plain"].acquired is None and games["Example Broken"].acquired is None


def test_owned_game_fields_keep_their_order_and_acquired_defaults_to_none():
    """Positional construction and stored games from before `acquired` existed keep working."""
    g = OwnedGame("k", "T", ("P1",), "collections", "bought", "paid", ("ConsoleGen9",), False)
    assert g.acquired is None


def test_a_product_without_platform_data_never_makes_a_game_pc_only():
    """A bundle's catalog entry lists no platforms (and no generation): it neither proves nor disproves PC-only. A
    game is PC-only only when a product lists its platforms and none of those includes Xbox."""
    items = [_lic(p) for p in ("B1", "C1", "B2", "P1", "B3")]
    catalog = {"B1": prod("B1", "Example Heist V: Premium Edition", platforms=(), gen=()),  # a bundle...
               "C1": prod("C1", "Example Heist V"),  # ...and a console product of the same game
               "B2": prod("B2", "Example Lone Bundle", platforms=(), gen=()),  # a bundle alone
               "P1": prod("P1", "Example Desk Game", platforms=("PC",), gen=()),  # PC-only...
               "B3": prod("B3", "Example Desk Game - Deluxe Edition", platforms=(), gen=())}  # ...with a bundle
    games = {g.pids: g for g in owned_games(items, [], catalog)}
    assert set(games) == {("B1", "C1"), ("B2",), ("B3", "P1")}
    assert not games[("B1", "C1")].pc_only and games[("B1", "C1")].gen == ("ConsoleGen9",)
    assert not games[("B2",)].pc_only and games[("B2",)].gen == ()
    assert games[("B3", "P1")].pc_only


def test_pc_only_and_catalog_gaps_and_filtering():
    items = [_lic("PC1"), _lic("NC1"), _lic("G8", orderId="X"), _lic("G9"), _lic("DEMO"), _lic("DLC", productKind="Durable")]
    catalog = {"PC1": prod("PC1", "Desk Only", platforms=("PC",), gen=()),
               "G8": prod("G8", "Both Gens", gen=("ConsoleGen9", "ConsoleGen8")),
               "G9": prod("G9", "Both Gens - Deluxe Edition", gen=("ConsoleGen9",)),
               "DEMO": prod("DEMO", "Some Demo"), "DLC": prod("DLC", "Some DLC", kind="Durable")}
    games = {g.pids: g for g in owned_games(items, [], catalog)}
    assert games[("PC1",)].pc_only and games[("PC1",)].source == "collections"
    assert not games[("NC1",)].pc_only          # not in the catalog
    assert games[("G8", "G9")].gen == ("ConsoleGen8", "ConsoleGen9")
    assert len(games) == 3                       # demo and DLC left out
