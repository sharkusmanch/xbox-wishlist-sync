from wsync.match import Match, base_title, norm, pick


def test_norm_delta_symbol():
    assert norm("METAL GEAR SOLID Δ: SNAKE EATER") == norm("Metal Gear Solid Delta: Snake Eater")


def test_norm_strips_combining_marks():
    assert norm("ŌKAMI HD") == norm("Okami HD")


def test_norm_ampersand_is_and():
    assert norm("Disney Illusion Island Starring Mickey & Friends") == norm(
        "Disney Illusion Island Starring Mickey and Friends"
    )


def test_norm_drops_trademark_symbols():
    assert norm("STAR WARS Zero Company™") == norm("STAR WARS Zero Company")
    assert norm("Game® ©") == norm("Game")


def test_norm_curly_quotes():
    assert norm("Baldur’s Gate 3") == norm("Baldur's Gate 3")
    assert norm("“Quoted” Game") == norm('"Quoted" Game')


def test_norm_apostrophe_lookalikes():
    for mark in ("\u02bc", "\u00b4", "`", "\u2032"):
        assert norm(f"Baldur{mark}s Gate") == norm("Baldur's Gate")


def test_norm_collapses_punctuation_and_space():
    assert norm("  Hades  II:  Night-Fall!! ") == norm("hades ii night fall")


def test_norm_does_not_merge_distinct_titles():
    assert norm("Persona 3 Reload") != norm("Persona 3 Portable")
    assert norm("Fable") != norm("Fable II")


def test_base_title_removes_edition_noise():
    assert base_title("CONTROL Resonant Digital Deluxe Edition") == norm("CONTROL Resonant")
    assert base_title("FINAL FANTASY VII REVELATION Premium Plus Edition") == norm(
        "Final Fantasy VII Revelation"
    )


def test_base_title_longest_phrase_first():
    # Shortest-first would strip only "enhanced" and leave "foo xbox one x".
    assert base_title("Foo Xbox One X Enhanced") == "foo"
    assert base_title("Foo Xbox Series X|S Optimized") == "foo"


def test_base_title_strips_repeated_suffixes():
    assert base_title("Foo Deluxe Edition for Xbox One") == "foo"
    assert base_title("Foo Gold Edition (Xbox Series X|S)") == "foo"


def test_base_title_leaves_mid_title_words():
    assert base_title("Enhanced Wars") == norm("Enhanced Wars")
    assert base_title("Edition Wars Gold Edition") == norm("Edition Wars")


def test_base_title_platform_suffixes():
    assert base_title("Foo for Xbox Series X|S") == "foo"
    assert base_title("Foo (Xbox Series X|S)") == "foo"
    assert base_title("Foo for Xbox One") == "foo"
    assert base_title("Foo (Xbox One)") == "foo"
    assert base_title("Foo Enhanced") == "foo"


def test_base_title_whole_words_only():
    # "enhanced" inside a longer word must survive
    assert base_title("Unenhanced Tales") == "unenhanced tales"
    assert base_title("Gold Editions Of Rust") == "gold editions of rust"


def test_base_title_never_empty():
    assert base_title("Standard Edition") == "standard edition"


def test_pick_exact_match():
    m = pick("Persona 3 Reload", [("Persona 3 Reload", "a"), ("Persona 3 Portable", "b")])
    assert m.status == "matched"
    assert m.target == "a"


def test_pick_edition_noise_match():
    m = pick("Fable Premium Edition", [("Fable", "x"), ("Fable II", "y")])
    assert m.status == "matched"
    assert m.target == "x"


def test_pick_candidate_edition_noise_match():
    m = pick("Fable", [("Fable Standard Edition", "x"), ("Fable II", "y")])
    assert (m.status, m.target) == ("matched", "x")


def test_pick_no_fuzzy_match_is_ambiguous():
    m = pick("Judas", [("Judas Priest Rocks", "z")])
    assert m.status == "ambiguous"
    assert m.target is None
    assert m.candidates == ["Judas Priest Rocks"]


def test_pick_none_without_candidates():
    m = pick("Persona 6", [])
    assert m == Match(status="none", target=None, candidates=[])


def test_pick_two_equal_norm_is_ambiguous():
    m = pick("Foo", [("Foo", "1"), ("FOO", "2")])
    assert m.status == "ambiguous"
    assert m.target is None


def test_pick_two_equal_norm_does_not_fall_through_to_base():
    m = pick("Foo", [("Foo", "1"), ("Foo", "2"), ("Foo Deluxe Edition", "3")])
    assert m.status == "ambiguous"


def test_pick_exact_tier_wins_over_base_tier():
    m = pick("Foo", [("Foo", "1"), ("Foo Deluxe Edition", "2")])
    assert (m.status, m.target) == ("matched", "1")


def test_pick_two_equal_base_is_ambiguous():
    m = pick("Foo", [("Foo Deluxe Edition", "1"), ("Foo Gold Edition", "2")])
    assert m.status == "ambiguous"
    assert m.target is None


def test_pick_unicode_item_matches_ascii_candidate():
    m = pick("METAL GEAR SOLID Δ: SNAKE EATER", [("Metal Gear Solid Delta: Snake Eater", "m")])
    assert (m.status, m.target) == ("matched", "m")
