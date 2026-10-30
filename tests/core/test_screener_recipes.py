"""推荐组合：静态词条也必须过词表，拼错的字段不能等到用户点下去才炸（D221）。"""

from __future__ import annotations

from sams.core.screener_fields import fields_for, validate
from sams.core.screener_recipes import RECIPES, recipes_for


def test_every_recipe_passes_the_vocabulary():
    for item in RECIPES:
        assert validate(item.market, list(item.conditions)), item.key
        available = {field.name for field in fields_for(item.market)}
        assert {cond.field for cond in item.conditions} <= available, item.key


def test_recipe_keys_and_names_are_unique():
    assert len({item.key for item in RECIPES}) == len(RECIPES)
    assert len({item.name for item in RECIPES}) == len(RECIPES)


def test_each_recipe_explains_what_it_screens_says_where_it_comes_from_and_its_limit():
    for item in RECIPES:
        assert item.description.strip() and item.basis.strip() and item.caveat.strip()
        assert len(item.name) <= 40, item.key
        assert len(item.description) <= 200, item.key


def test_a_recipe_does_not_repeat_the_same_field():
    """同一个字段两条条件能跑，但「ROE ≥ 15% 且 ROE ≥ 10%」只是在卡片上凑行数。"""
    for item in RECIPES:
        names = [cond.field for cond in item.conditions]
        assert len(names) == len(set(names)), item.key


def test_the_recipe_card_is_market_specific():
    assert {item.market for item in recipes_for("A")} == {"A"}
    assert recipes_for("HK") == ()
