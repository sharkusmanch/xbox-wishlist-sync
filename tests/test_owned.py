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
    ("Example Quest® Odyssey - GOLD EDITION", "Example Quest Odyssey"),
    ("Blockworld for Windows", "Blockworld (Windows)"),
    ("Example Ball® 21 Xbox Series X | S", "Example Ball 21"),
    ("Quiet House", "Quiet House: Console Edition"),
    ("Dragon Realm 2: Stay Brave - Reloaded Edition", "Dragon Realm 2: Stay Brave"),
    ("Sample Tale - The Series - Episode 1: Realm of Shadows", "Sample Tale: The Series"),
    ("Example Duty®: Modern Combat® II - Vault Edition", "Example Duty: Modern Combat II"),
    ("Space Hall™ 2", "Space Hall 2"),
    ("Example Duty®: Cold Front - Cross-Gen Bundle", "Example Duty: Cold Front"),
])
def test_same_game_across_store_and_ta_names(store, ta):
    assert same_game(store, ta)


@pytest.mark.parametrize("a, b", [
    ("Space Hall (2008)", "Space Hall"),
    ("Wake Light Remastered", "Wake Light"),
    ("Halo Sample 5: Guardians", "Halo Sample"),
    ("Mobster II: Definitive Edition", "Mobster II"),
])
def test_different_games_stay_apart(a, b):
    assert not same_game(a, b)


@pytest.mark.parametrize("title", ["Example™ Demo", "Battle Sample™ 1 Open Beta", "Fiction Beast Pre-Order Bundle",
                                   "Example Duty®: Black Ops 4 - Private Beta", "Atlas Sample (Pre-Order)"])
def test_demos_betas_and_preorders_are_noise(title):
    assert is_noise(title)


def test_full_games_are_not_noise():
    assert not is_noise("Sample Jump") and not is_noise("Battle Sample™ Hardline Standard Edition")


@pytest.mark.parametrize("title", ["Fiction Digital Artbook & Original Soundtrack",
                                   "Example Rally ®2 Open Multiplayer Tech Test", "Evolve Sample Big Alpha",
                                   "Elder Sample® Online: Summerset Upgrade - Prepurchase",
                                   "HOLLOW HILL 2 Bonus Content (Digital Artbook and Soundtrack）"])
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
    assert same_game("Example Doom (BATTLEMODE)", "Example Doom")
    assert not same_game("Space Hall (2008)", "Space Hall")


@pytest.mark.parametrize("store, ta, want", [
    ("Arena Sample", ["Arena Sample: Champions of the Realm"], "Arena Sample: Champions of the Realm"),
    ("Dark Spire III", ["Dark Spire"], None),
    ("Example Duty®: Black Ops 4", ["Example Duty"], None),
    ("Demon Hunt® IV - Ultimate Edition", ["Demon Hunt IV", "Demon Hunt"], "Demon Hunt IV"),
    ("Dead Sample 4 Bundle", ["Dead Sample", "Dead Sample 4"], "Dead Sample 4"),
    ("FANTASY SAGA VII", ["FANTASY SAGA VII REMAKE INTERGRADE"], None),
])
def test_possible_match_hints(store, ta, want):
    assert hint(match_keys(store), ta) == want


def test_accents_fold():
    assert same_game("Örbit Tale", "Orbit Tale")


def test_a_bare_trailing_platform_phrase_is_stripped():
    assert same_game("Example Ball 21 Xbox One", "Example Ball 21")
    assert same_game("Example Ball 22 Xbox One & Xbox Series X|S", "Example Ball 22")


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
               "B1": prod("B1", "Two Point Clinic™"), "B2": prod("B2", "Two Point Clinic: JUMBO Edition")}
    games = {g.pids: g for g in owned_games(items, orders, catalog)}
    assert set(games) == {("A1", "A2"), ("B1", "B2")}
    g = games[("A1", "A2")]
    assert g.title == "Black Forest" and g.acquisition == "bought" and g.evidence == "paid"
    assert g.key == "black forest"


def test_missing_editions_of_one_game_collapse_but_numbered_entries_do_not():
    items = [_lic(p) for p in ("F1", "F2", "M1", "M2")]
    catalog = {p: prod(p, t) for p, t in [
        ("F1", "Racer Motorsport 7"), ("F2", "Racer Motorsport 7 Ultimate Edition"),
        ("M1", "Mega Hero X Legacy Collection"), ("M2", "Mega Hero X Legacy Collection 2")]}
    out = owned_games(items, [], catalog)
    assert [(g.title, g.pids) for g in out] == [
        ("Mega Hero X Legacy Collection", ("M1",)), ("Mega Hero X Legacy Collection 2", ("M2",)),
        ("Racer Motorsport 7", ("F1", "F2"))]


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
