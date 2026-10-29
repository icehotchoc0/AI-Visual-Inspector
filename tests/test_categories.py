import pytest
from pydantic import ValidationError

from backend.categories import Category, CategoryRegistry


def test_registry_loads_all_categories(registry):
    ids = registry.ids
    assert set(ids) == {"aircon_install", "washer_repair", "fridge_repair", "water_purifier", "dishwasher_install", "plumbing", "electrical", "door_install", "ventilation_fan", "faucet_replace", "eboard_install"}
    assert "window_install" not in ids  # 당장 취급하지 않음 -> categories.optional.yaml
    for c in registry.all():
        assert c.required_shots, c.id
        assert c.defect_rubric, c.id
        assert c.quality.blur_threshold > 0
        assert c.verdict.missing_required_shot in ("fail", "review")


def test_defaults_are_merged_per_category(registry):
    c = registry.get("plumbing")
    assert c.fraud.duplicate_hamming_max == 6
    assert c.verdict.min_defect_confidence == 0.6


def test_unknown_category_raises(registry):
    with pytest.raises(KeyError):
        registry.get("nope")
    assert "nope" not in registry


def test_pair_reference_validation():
    with pytest.raises(ValidationError):
        Category(id="x", name="x", shots=[{"id": "a", "name": "a"}], before_after_pairs=[("a", "zzz")])


def test_duplicate_shot_ids_rejected():
    with pytest.raises(ValidationError):
        Category(id="x", name="x", shots=[{"id": "a", "name": "a"}, {"id": "a", "name": "b"}])


def test_from_yaml_override(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "defaults:\n  quality: {blur_threshold: 10}\ncategories:\n  - id: t\n    name: T\n    quality: {min_width: 100}\n    shots: [{id: s, name: S, required: true}]\n",
        encoding="utf-8",
    )
    reg = CategoryRegistry.from_yaml(p)
    c = reg.get("t")
    assert c.quality.blur_threshold == 10 and c.quality.min_width == 100


def test_optional_categories_file_still_valid():
    from pathlib import Path
    reg = CategoryRegistry.from_yaml(Path(__file__).resolve().parent.parent / "config" / "categories.optional.yaml")
    assert set(reg.ids) == {"window_install"}
