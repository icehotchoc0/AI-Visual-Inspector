import pytest
from pydantic import ValidationError

from backend.categories import Category, CategoryRegistry


def test_registry_loads_all_categories(registry):
    ids = registry.ids
    assert set(ids) == {"aircon_install", "washer_repair", "fridge_repair", "water_purifier", "dishwasher_install", "plumbing", "electrical", "door_install", "ventilation_fan", "faucet_replace", "eboard_install", "range_hood_install", "induction_install"}
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


def test_directory_layout_groups_and_disabled(tmp_path):
    from pathlib import Path
    import yaml

    root = Path(__file__).resolve().parent.parent / "config" / "categories"
    # every category file sits in the folder of its group and is named after its id
    for f in root.glob("*/*.yaml"):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert d["group"] == f.parent.name and d["id"] == f.stem, f
    window = yaml.safe_load((root / "housing" / "window_install.yaml").read_text(encoding="utf-8"))
    assert window["enabled"] is False  # parked, not loaded
    reg = CategoryRegistry.from_path(root)
    assert [g.id for g in reg.groups] == ["appliance", "housing", "commercial"]
    assert reg.group_name("commercial") == "업무·교육 시설"
    assert [c.group for c in reg.all()] == sorted([c.group for c in reg.all()], key=["appliance", "housing", "commercial"].index)
    assert {c.id for c in reg.all("commercial")} == {"eboard_install"}


def test_directory_loader_rejects_group_folder_mismatch(tmp_path):
    (tmp_path / "_groups.yaml").write_text("groups: [{id: a, name: A, order: 1}]\n", encoding="utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.yaml").write_text("id: x\nname: X\ngroup: b\nshots: [{id: s, name: S, required: true}]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        CategoryRegistry.from_path(tmp_path)


def test_directory_loader_enabled_flag_and_order(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "late.yaml").write_text("id: late\nname: 나중\ngroup: g\norder: 50\nshots: [{id: s, name: S, required: true}]\n", encoding="utf-8")
    (tmp_path / "g" / "first.yaml").write_text("id: first\nname: 먼저\ngroup: g\norder: 1\nshots: [{id: s, name: S, required: true}]\n", encoding="utf-8")
    (tmp_path / "g" / "parked.yaml").write_text("id: parked\nname: 보류\ngroup: g\nenabled: false\nshots: [{id: s, name: S, required: true}]\n", encoding="utf-8")
    reg = CategoryRegistry.from_path(tmp_path)
    assert reg.ids == ["first", "late"]


def test_legacy_yaml_path_falls_back_to_folder(tmp_path, caplog):
    root = tmp_path / "categories"
    (root / "g").mkdir(parents=True)
    (root / "g" / "one.yaml").write_text("id: one\nname: 하나\ngroup: g\nshots: [{id: s, name: S, required: true}]\n", encoding="utf-8")
    reg = CategoryRegistry.from_path(tmp_path / "categories.yaml")  # old .env value, file gone
    assert reg.ids == ["one"]
    with pytest.raises(FileNotFoundError):
        CategoryRegistry.from_path(tmp_path / "missing.yaml")
