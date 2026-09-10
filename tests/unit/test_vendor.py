import pytest

from second_brain_receipts.domain.vendor import normalize_vendor_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (" Jollibee ", "jollibee"),
        ("JOLLIBEE", "jollibee"),
        ("ABC   Hardware", "abc hardware"),
        ("ABC\t\nHardware", "abc hardware"),
        ("ＡＢＣ\u00a0Hardware", "abc hardware"),
        ("Straße", "strasse"),
        ("Cafe\u0301", "café"),
        ("A&B, Inc.", "a&b, inc."),
    ],
)
def test_normalization(name, expected):
    assert normalize_vendor_name(name) == expected
    assert normalize_vendor_name(expected) == expected


@pytest.mark.parametrize("name", ["", " \t\n", "\u00a0"])
def test_blank_name(name):
    with pytest.raises(ValueError):
        normalize_vendor_name(name)
