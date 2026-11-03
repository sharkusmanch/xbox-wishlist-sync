import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from test_ta import BASE, FakeFS, assert_anonymous, client, logged_in, resp
from wsync import ta_coll
from wsync.ta import IncompleteRead, TAError
from wsync.ta_coll import OwnedRow, list_filters, list_url, parse_list

FIX = Path(__file__).parent / "fixtures"
PAGE = (FIX / "ta_collection_page.html").read_text()
LIST = (FIX / "ta_collection_list.html").read_text()
COLL = BASE + "/gamer/Test+Gamer/gamecollection"


# ---------------------------------------------------------------- filters


def test_list_filters_read_the_ajaxlistfilter_inputs_like_the_page_does():
    f = dict(list_filters(PAGE))
    assert f["GameView"] == "optListView"  # the checked radio only
    assert f["ddlSortBy"] == "Lastunlock"  # the selected option wins
    assert f["ddlDLCInclusionSetting"] == "AllDLC"
    assert f["ddlTitleType"] == "Game"
    assert f["sddGameMediaID"] == " "  # no selection: the first option
    assert f["chkExcludeNotOwned"] == "True" and f["chkColTitlename"] == "True"
    assert f["oGameCollection_ItemsPerPage"] == "30" and f["oGameCollection_ShowAll"] == "False"
    assert f["txtGamerID"] == "99999"
    assert "chkJustThesePlatforms" not in f  # unchecked box
    assert "txtSearchString" not in f  # empty text box
    assert "ddlSomethingElse" not in f  # outside every .ajaxlistfilter container


def test_list_filters_never_narrow_by_the_multi_selects():
    # ownership / play status / platform are `multiple` selects with nothing chosen (= any);
    # taking their first option would silently filter the list to "Owned" + "Playing now" (live 2026-11-02)
    names = [k for k, _ in list_filters(PAGE)]
    for n in ("sddOwnerShipStatusIDs", "sddPlayStatusIDs", "ddlPlatformIDs"):
        assert n not in names
    chosen = PAGE.replace('<option value="2">', '<option value="2" selected>', 1)
    assert "sddOwnerShipStatusIDs" not in [k for k, _ in list_filters(chosen)]


# ---------------------------------------------------------------- list_url


def params(url):
    q = parse_qs(urlsplit(url).query)
    assert q["function"] == ["AjaxList"] and "executeformfunction" in urlsplit(url).query
    head, _, rest = q["params"][0].partition("|")
    assert head == "oGameCollection"
    return rest.replace("/\\pipe\\/", "|")


def test_list_url_drops_not_owned_filter_sets_page_size_and_columns():
    url = list_url(COLL, list_filters(PAGE), 3)
    assert url.startswith(COLL + "?executeformfunction&function=AjaxList&params=")
    p = params(url)
    assert "chkExcludeNotOwned" not in p
    assert "&oGameCollection_ItemsPerPage=100" in p and "&oGameCollection_ItemsPerPage=30" not in p
    assert "&oGameCollection_Page=3" in p and "&oGameCollection_ShowAll=False" in p
    for col in ("Platform", "Ownershipstatus", "Media"):
        assert p.count(f"&chkCol{col}=True") == 1
    # the page's own column choices are kept, not duplicated
    assert p.count("&chkColTitlename=True") == 1
    assert "&txtGamerID=99999" in p


def test_list_url_does_not_double_a_column_the_page_already_has():
    filters = list_filters(PAGE) + [("chkColMedia", "True")]
    assert params(list_url(COLL, filters, 1)).count("&chkColMedia=True") == 1


def test_list_url_escapes_ampersands_and_pipes_like_the_page_script():
    url = list_url(COLL, [("chkColFoo", "A&B&C|D")], 1)
    # `&` -> *amp* (first one of each value only, as AJAXList.ReplaceCharacters does); `|` -> /\pipe\/
    assert "/\\pipe\\/" in parse_qs(urlsplit(url).query)["params"][0]
    assert "&chkColFoo=A*amp*B&C|D&" in params(url) + "&"
    assert "|" not in urlsplit(url).query and "&" not in urlsplit(url).query.split("params=")[1]


# ---------------------------------------------------------------- parse_list


def test_parse_list_reads_rows_and_the_footer_total():
    rows, total = parse_list(LIST)
    assert total == 9 and len(rows) == 9
    assert rows[0] == OwnedRow(ta_id=101, title="Test Game Alpha", platform="xbox-one", ownership="Owned", media="Digital")
    assert [r.platform for r in rows] == ["xbox-one", "xbox-series-x-s", "xbox-series-x-s", "xbox-360",
                                          "xbox-series-x-s", "windows", "xbox-360", "xbox-one", "xbox-one"]
    assert [r.ownership for r in rows] == ["Owned"] * 8 + ["EA Play"]
    assert [r.media for r in rows] == ["Digital", "", "Disc", "Disc & Digital", "Disc-to-Digital", "Digital",
                                       "Disc", "Digital", "Digital"]
    assert rows[1].title == "Test Game's Beta"  # entity decoded
    assert rows[7].title == "Test Game Theta & Iota"  # TA leaves a bare & in titles
    assert rows[4].title == "Test Game: Epsilon" and rows[4].ta_id == 105


def test_parse_list_ids_are_unique_ints_and_the_row_is_frozen():
    rows, _ = parse_list(LIST)
    assert len({r.ta_id for r in rows}) == 9 and all(isinstance(r.ta_id, int) for r in rows)
    with pytest.raises(Exception):
        rows[0].title = "x"


def test_parse_list_without_rows_or_total():
    assert parse_list("<div>There are no matching titles</div>") == ([], None)
    rows, total = parse_list(re.split(r'<tr class="(?:odd|even)">', LIST)[0] + "</table>")
    assert rows == [] and total is None


def test_parse_list_total_with_thousands_separator():
    rows, total = parse_list(LIST.replace(" 9 Titles", " 1,234 Titles"))
    assert total == 1234 and len(rows) == 9


@pytest.mark.parametrize("bad", [
    lambda s: s.replace(">Ownership<", ">Owner<"),  # a column we need is gone
    lambda s: s.replace("EditDetails(101", "EditSomething(101"),  # a row without its id
])
def test_parse_list_refuses_a_layout_it_does_not_understand(bad):
    with pytest.raises(ValueError):
        parse_list(bad(LIST))


# ---------------------------------------------------------------- collection()


MULTI_SELECTS = ("sddOwnerShipStatusIDs", "sddPlayStatusIDs", "ddlPlatformIDs")


def fetched_params(url):
    return parse_qs(urlsplit(url).query)["params"][0].replace("/\\pipe\\/", "|")


def pairs(p):
    """name -> value from a `oGameCollection|&k=v&k=v` params string (values here carry no &)."""
    body = p.partition("|")[2] if p.startswith("oGameCollection|") else p
    return dict(kv.split("=", 1) for kv in body.split("&") if kv)


def make_row(i, own="Owned", plat="xbox-one", media="Digital", title=None):
    title = title or f"Game {i}"
    return (f'<tr class="odd"><td class="gamethumb"><a href="/game/G-{i}/achievements" title="{title}"></a></td>'
            f'<td class="smallgame"><a href="/game/G-{i}/achievements">{title}</a></td>'
            f'<td><img src="/p.png" alt="{plat}" title="{plat}"/></td>'
            f'<td class="small">{own}</td><td class="small">{media}</td>'
            f'<td><a class="button" onclick="GameCollection.EditDetails({1000 + i}, false, true);">Edit</a></td></tr>')


HEAD = ('<tr><th></th><th class="left">Title</th><th>Platform</th><th>Ownership</th><th>Media</th>'
        '<th>Edit</th></tr>')


def fragment(ids, total, **kw):
    footer = f'<tr class="total"><td colspan="2" class="left"> {total:,} Titles</td><td></td></tr>' if total is not None else ""
    return ('<div class="listview"><table class="maintable">' + HEAD
            + "".join(make_row(i, **kw) for i in ids) + footer + "</table></div>")


def collection_routes(total, pages=None, page_total=None):
    """The collection page plus an AjaxList endpoint serving `total` rows, 100 per page."""
    seen = []

    def serve(url, body, headers):
        n = int(re.search(r"oGameCollection_Page=(\d+)", parse_qs(urlsplit(url).query)["params"][0]).group(1))
        seen.append(n)
        if pages is not None and n in pages:
            return resp(pages[n])
        ids = range((n - 1) * 100, min(n * 100, total))
        return resp(fragment(ids, total if page_total is None else page_total))

    routes = {
        ("GET", "=/gamer/Test+Gamer/gamecollection"): resp(PAGE),
        ("GET", "/gamer/Test+Gamer/gamecollection?"): serve,
    }
    return routes, seen


def test_collection_pages_until_the_footer_total():
    routes, seen = collection_routes(250)
    c, http = logged_in(routes)
    rows = c.collection()
    assert len(rows) == 250 and seen == [1, 2, 3]
    assert rows[0].ta_id == 1000 and rows[-1].ta_id == 1249
    assert http.urls("POST") == [] and all(call["method"] == "GET" for call in http.calls)
    fetched = [fetched_params(call["url"]) for call in http.calls if "AjaxList" in call["url"]]
    assert len(fetched) == 3
    for p in fetched:
        assert pairs(p)["oGameCollection_ItemsPerPage"] == "100"
        for name in MULTI_SELECTS:  # the narrowing ones are never sent
            assert f"&{name}=" not in p


def test_collection_with_one_exact_page_reads_one_page():
    routes, seen = collection_routes(100)
    c, _ = logged_in(routes)
    assert len(c.collection()) == 100 and seen == [1]


def test_collection_short_read_is_incomplete():
    pages = {2: fragment(range(100, 199), 250)}  # 99 rows where the total promises 100
    routes, _ = collection_routes(250, pages)
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead) as e:
        c.collection()
    assert "249" in str(e.value) and "250" in str(e.value)


def test_collection_missing_total_is_incomplete():
    routes, _ = collection_routes(10, {1: fragment(range(10), None)})
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead):
        c.collection()


def test_collection_empty_answer_is_incomplete():
    routes, _ = collection_routes(10, {1: "<div>There are no matching titles</div>"})
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead):
        c.collection()


def test_collection_rows_repeated_across_pages_are_incomplete():
    # a shifting sort can serve a title on two pages and skip another: the count still matches
    pages = {2: fragment(range(99, 199), 200)}
    routes, _ = collection_routes(200, pages)
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead) as e:
        c.collection()
    assert "twice" in str(e.value)


def test_collection_total_changing_between_pages_is_incomplete():
    pages = {2: fragment(range(100, 200), 201)}
    routes, _ = collection_routes(200, pages)
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead):
        c.collection()


def test_collection_refuses_an_absurd_total():
    routes, seen = collection_routes(10, {1: fragment(range(100), 10_000_000)})
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead):
        c.collection()
    assert seen == [1]


def test_collection_unreadable_row_is_incomplete():
    routes, _ = collection_routes(3, {1: fragment(range(3), 3).replace("EditDetails(1001", "Nope(1001")})
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead):
        c.collection()


def test_collection_http_error_names_no_gamertag():
    routes, _ = collection_routes(150)
    routes[("GET", "/gamer/Test+Gamer/gamecollection?")] = [resp(fragment(range(100), 150)), resp(status=500)]
    c, _ = logged_in(routes)
    with pytest.raises(TAError) as e:
        c.collection()
    assert "Test" not in str(e.value) and "<gamer>" in str(e.value) and not isinstance(e.value, IncompleteRead)


def test_collection_logs_in_when_the_page_is_gated_and_keeps_credentials_off_flaresolverr():
    from test_ta import GATED, SECRET, login_routes
    routes = login_routes()
    ok, _ = collection_routes(2)
    routes[("GET", "=/gamer/Test+Gamer/gamecollection")] = [resp(GATED), resp(PAGE)]
    routes[("GET", "/gamer/Test+Gamer/gamecollection?")] = ok[("GET", "/gamer/Test+Gamer/gamecollection?")]
    fs = FakeFS()
    from test_ta import FakeHttp
    http = FakeHttp(routes)
    c = client(http, fs)
    assert len(c.collection()) == 2
    assert http.urls("POST") == ["/login"]
    assert any(SECRET in (call["body"] or "") for call in http.calls)
    assert_anonymous(fs)


def test_module_exports_the_dataclass_fields_in_the_documented_order():
    assert [f for f in OwnedRow.__dataclass_fields__] == ["ta_id", "title", "platform", "ownership", "media"]
    assert ta_coll.PER_PAGE == 100


# ---------------------------------------------------------------- pinned filters (a saved view must not narrow the read)

PINNED = {"ddlStartedStatus": "0", "ddlDifficulty": "0", "ddlCompletionStatus": " ", "sddGameMediaID": " ",
          "ddlGameNotes": " ", "ddlMythicStatus": " ", "asdGamePropertyID": " ", "ddlTitleType": "Game",
          "ddlSortBy": "TitleName", "oGameCollection_Order": "TitleName"}


def choose(page, select, value):
    """The page with `value` the selected option of `select` (and nothing else selected in it)."""
    m = re.search(r'<select[^>]*name="%s".*?</select>' % select, page, re.S)
    block = re.sub(r'\s+selected(="selected")?', "", m.group(0))
    block = block.replace(f'<option value="{value}"', f'<option value="{value}" selected', 1)
    assert f'value="{value}" selected' in block
    return page.replace(m.group(0), block)


def narrowed_view():
    page = PAGE
    for select, value in [("ddlStartedStatus", "1"), ("ddlDifficulty", "3"), ("ddlCompletionStatus", "Incomplete only"),
                          ("sddGameMediaID", "1"), ("ddlGameNotes", "Has notes"), ("ddlMythicStatus", "Mythic earned"),
                          ("asdGamePropertyID", "133"), ("ddlTitleType", "Title"), ("ddlSortBy", "Timeplayed")]:
        page = choose(page, select, value)
    return page.replace('name="txtSearchString" id="txtSearchString" value=""',
                        'name="txtSearchString" id="txtSearchString" value="zelda"')


def test_the_narrowed_fixture_view_really_is_narrowed():
    f = dict(list_filters(narrowed_view()))
    assert f["ddlStartedStatus"] == "1" and f["txtSearchString"] == "zelda" and f["ddlTitleType"] == "Title"
    assert f["sddGameMediaID"] == "1" and f["asdGamePropertyID"] == "133" and f["ddlSortBy"] == "Timeplayed"


def test_list_url_pins_every_narrowing_filter_whatever_view_the_page_saved():
    p = pairs(params(list_url(COLL, list_filters(narrowed_view()), 2)))
    assert "txtSearchString" not in p and "chkExcludeNotOwned" not in p
    for name, value in PINNED.items():
        assert p[name].replace("%20", " ") == value, name
    # a pinned filter appears once even though the page also had it
    assert params(list_url(COLL, list_filters(narrowed_view()), 2)).count("&ddlTitleType=") == 1


def test_list_url_pins_filters_the_page_omitted_too():
    p = pairs(params(list_url(COLL, [("txtGamerID", "99999")], 1)))
    for name, value in PINNED.items():
        assert p[name] == value, name


def test_pinned_values_match_the_pages_own_defaults():
    # data-default in the live markup (2026-11-02): "all" for every filter but the sort and title type,
    # which the sync fixes on purpose (stable order; games only, the page's blank default includes apps)
    for name in ("ddlStartedStatus", "ddlDifficulty", "ddlCompletionStatus", "sddGameMediaID", "ddlGameNotes",
                 "ddlMythicStatus", "asdGamePropertyID"):
        m = re.search(r'<select[^>]*name="%s"[^>]*>' % name, PAGE)
        assert f'data-default="{PINNED[name]}"' in m.group(0), name


def test_collection_fetches_only_pinned_urls_for_a_narrowed_saved_view():
    routes, _ = collection_routes(120)
    routes[("GET", "=/gamer/Test+Gamer/gamecollection")] = resp(narrowed_view())
    c, http = logged_in(routes)
    assert len(c.collection()) == 120
    lists = [pairs(fetched_params(call["url"])) for call in http.calls if "AjaxList" in call["url"]]
    assert len(lists) == 2
    for p in lists:
        assert "txtSearchString" not in p
        for name, value in PINNED.items():
            assert p[name] == value, name


def test_every_filter_of_the_real_page_is_known():
    assert ta_coll.unknown_filters(list_filters(PAGE)) == []
    assert ta_coll.unknown_filters(list_filters(narrowed_view())) == []


def test_an_unknown_filter_makes_collection_fail_loudly_by_name_only():
    page = PAGE.replace('<span class="ajaxlistfilter">',
                        '<span class="ajaxlistfilter"><input type="checkbox" name="chkHideFavourites" value="True" checked>'
                        '<input type="hidden" name="txtSecret" value="private-value">', 1)
    routes, seen = collection_routes(5)
    routes[("GET", "=/gamer/Test+Gamer/gamecollection")] = resp(page)
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead) as e:
        c.collection()
    assert "chkHideFavourites" in str(e.value) and "txtSecret" in str(e.value)
    assert "private-value" not in str(e.value) and seen == []  # no list page was read


@pytest.mark.parametrize("required", ["txtGamerID", "oGameCollection_Order"])
def test_collection_needs_the_gamer_id_and_order_filters(required):
    page = PAGE.replace(f'name="{required}"', 'name="renamed"')
    routes, seen = collection_routes(5)
    routes[("GET", "=/gamer/Test+Gamer/gamecollection")] = resp(page)
    c, _ = logged_in(routes)
    with pytest.raises(IncompleteRead):
        c.collection()
    assert seen == []
