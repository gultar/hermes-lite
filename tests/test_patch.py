"""Patch repairs for small-model slips, and the approximate-match safety rules."""
import pytest

from hermes_lite.tools import fuzzy

JS = r'''function a() {
  x.replace(/'/g, "\\'");
}
function b(artist, album) {
  const c = document.getElementById('view');
  c.innerHTML = '<div class="loading">Loading</div>';
}
'''


def test_doubled_backslashes_are_halved_in_both_strings():
    line = JS.splitlines()[1].strip()          # as it really is in the file
    doubled = line.replace("\\", "\\\\")    # what a model emitting JSON sometimes sends
    out, how, n = fuzzy.apply(JS, doubled, doubled.replace("x.", "y."), False)
    assert line.replace("x.", "y.") in out and "halved" in how


def test_line_number_prefixes_are_stripped():
    old = "4|function b(artist, album) {\n5|  const c = document.getElementById('view');"
    out, how, _ = fuzzy.apply(JS, old, old.replace("album)", "album, opts)"), False)
    assert "function b(artist, album, opts) {" in out and "|function" not in out


def test_stray_quote_escapes_removed():
    old = "c.innerHTML = \\'<div class=\"loading\">Loading</div>\\';"
    out, _, _ = fuzzy.apply(JS, old, old.replace("Loading<", "Fetching<"), False)
    assert "c.innerHTML = '<div class=\"loading\">Fetching</div>';" in out


def test_single_line_near_miss_is_refused_with_hint():
    with pytest.raises(ValueError) as exc:
        fuzzy.apply(JS, "c.innerHTML = '<div class=\"spinner\">Loading</div>';", "x", False)
    assert "closest lines" in str(exc.value) and "start_line" in str(exc.value)
