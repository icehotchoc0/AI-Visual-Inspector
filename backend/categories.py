"""
Category registry: loads the category config into typed objects.

Categories (what work is being inspected, which photos are required, which
visual defects to look for) live in YAML, not in code, so the service can
cover new work types without a deploy. The same objects feed the VLM
prompts, the checklist check, and the /v1/categories endpoint.

Two layouts are supported:
  * a directory  config/categories/<group>/<id>.yaml  (one file per work
    type) plus _defaults.yaml (shared thresholds) and _groups.yaml (group
    names + order) - the layout used by this repo;
  * a single YAML file with `defaults:` and `categories:` (legacy / tests).
A category file with `enabled: false` is validated but not registered.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Phase = Literal["before", "after", "any"]
Severity = Literal["low", "medium", "high"]


class ShotSpec(BaseModel):
    id: str
    name: str
    description: str = ""
    required: bool = False
    phase: Phase = "any"


class DefectSpec(BaseModel):
    code: str
    name: str
    description: str = ""
    default_severity: Severity = "medium"


class QualityThresholds(BaseModel):
    min_width: int = 640
    min_height: int = 480
    blur_threshold: float = 60.0
    dark_threshold: float = 45.0
    bright_threshold: float = 225.0
    low_contrast_threshold: float = 18.0


class FraudThresholds(BaseModel):
    duplicate_hamming_max: int = 6
    near_duplicate_hamming_max: int = 12
    gps_max_spread_km: float = 0.5


class VerdictPolicy(BaseModel):
    min_defect_confidence: float = 0.6
    missing_required_shot: Literal["fail", "review"] = "fail"


class AnomalyPlugin(BaseModel):
    enabled: bool = False
    model_key: str = ""
    shot_types: list[str] = Field(default_factory=list)


class Group(BaseModel):
    id: str
    name: str
    order: int = 100


class Category(BaseModel):
    id: str
    name: str
    group: str = "other"
    enabled: bool = True
    order: int = 100  # 그룹 안에서의 표시 순서 (작을수록 앞). 같으면 이름순
    description: str = ""
    shots: list[ShotSpec] = Field(default_factory=list)
    before_after_pairs: list[tuple[str, str]] = Field(default_factory=list)
    defect_rubric: list[DefectSpec] = Field(default_factory=list)
    quality: QualityThresholds = Field(default_factory=QualityThresholds)
    fraud: FraudThresholds = Field(default_factory=FraudThresholds)
    verdict: VerdictPolicy = Field(default_factory=VerdictPolicy)
    anomaly: AnomalyPlugin = Field(default_factory=AnomalyPlugin)

    @model_validator(mode="after")
    def _check_references(self) -> "Category":
        shot_ids = {s.id for s in self.shots}
        if len(shot_ids) != len(self.shots):
            raise ValueError(f"category '{self.id}': duplicate shot ids")
        for before, after in self.before_after_pairs:
            if before not in shot_ids or after not in shot_ids:
                raise ValueError(
                    f"category '{self.id}': before_after_pair ({before}, {after}) references unknown shot"
                )
        for shot_type in self.anomaly.shot_types:
            if shot_type not in shot_ids:
                raise ValueError(f"category '{self.id}': anomaly.shot_types references unknown shot '{shot_type}'")
        codes = [d.code for d in self.defect_rubric]
        if len(set(codes)) != len(codes):
            raise ValueError(f"category '{self.id}': duplicate defect codes")
        return self

    # -- convenience -------------------------------------------------------
    @property
    def shot_ids(self) -> list[str]:
        return [s.id for s in self.shots]

    @property
    def required_shots(self) -> list[ShotSpec]:
        return [s for s in self.shots if s.required]

    def shot(self, shot_id: str) -> ShotSpec | None:
        return next((s for s in self.shots if s.id == shot_id), None)

    def defect(self, code: str) -> DefectSpec | None:
        return next((d for d in self.defect_rubric if d.code == code), None)


class CategoryRegistry:
    def __init__(self, categories: list[Category], groups: list[Group] | None = None):
        groups = groups or []
        known = {g.id: g for g in groups}
        for c in categories:
            known.setdefault(c.group, Group(id=c.group, name=c.group))
        self._groups = sorted(known.values(), key=lambda g: (g.order, g.id))
        order = {g.id: i for i, g in enumerate(self._groups)}
        categories = sorted(categories, key=lambda c: (order.get(c.group, 999), c.order, c.name))
        self._by_id = {c.id: c for c in categories}
        if len(self._by_id) != len(categories):
            raise ValueError("duplicate category ids")

    # -- loading -----------------------------------------------------------
    @classmethod
    def from_path(cls, path: str | Path) -> "CategoryRegistry":
        path = Path(path)
        return cls.from_dir(path) if path.is_dir() else cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CategoryRegistry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        defaults = raw.get("defaults", {}) or {}
        groups = [Group(**g) for g in (raw.get("groups") or [])]
        categories = [cls._build(entry, defaults, source=str(path)) for entry in raw.get("categories", []) or []]
        return cls([c for c in categories if c.enabled], groups)

    @classmethod
    def from_dir(cls, path: str | Path) -> "CategoryRegistry":
        root = Path(path)
        defaults_file = root / "_defaults.yaml"
        defaults = ((yaml.safe_load(defaults_file.read_text(encoding="utf-8")) or {}).get("defaults") or {}) if defaults_file.exists() else {}
        groups_file = root / "_groups.yaml"
        groups = [Group(**g) for g in ((yaml.safe_load(groups_file.read_text(encoding="utf-8")) or {}).get("groups") or [])] if groups_file.exists() else []
        categories: list[Category] = []
        for file in sorted(root.glob("*/*.yaml")):
            if file.name.startswith("_"):
                continue
            raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            entries = raw.get("categories") if isinstance(raw, dict) and "categories" in raw else [raw]
            for entry in entries:
                cat = cls._build(entry, defaults, source=str(file))
                if cat.group != file.parent.name:
                    raise ValueError(f"{file}: group '{cat.group}' does not match folder '{file.parent.name}'")
                if cat.id != file.stem and len(entries) == 1:
                    raise ValueError(f"{file}: id '{cat.id}' does not match file name")
                if cat.enabled:
                    categories.append(cat)
        return cls(categories, groups)

    @staticmethod
    def _build(entry: dict, defaults: dict, source: str = "") -> Category:
        merged = dict(entry)
        for key in ("quality", "fraud", "verdict"):
            merged[key] = {**(defaults.get(key) or {}), **(entry.get(key) or {})}
        merged["before_after_pairs"] = [tuple(p) for p in entry.get("before_after_pairs", []) or []]
        try:
            return Category(**merged)
        except Exception as exc:  # noqa: BLE001 - re-raise with the file name for operators
            raise ValueError(f"invalid category in {source}: {exc}") from exc

    # -- lookup ------------------------------------------------------------
    def get(self, category_id: str) -> Category:
        try:
            return self._by_id[category_id]
        except KeyError:
            raise KeyError(f"Unknown category '{category_id}'. Known: {sorted(self._by_id)}") from None

    def __contains__(self, category_id: str) -> bool:
        return category_id in self._by_id

    def all(self, group: str | None = None) -> list[Category]:
        return [c for c in self._by_id.values() if group is None or c.group == group]

    @property
    def ids(self) -> list[str]:
        return list(self._by_id)

    @property
    def groups(self) -> list[Group]:
        return list(self._groups)

    def group_name(self, group_id: str) -> str:
        return next((g.name for g in self._groups if g.id == group_id), group_id)


@lru_cache(maxsize=4)
def load_registry(path: str) -> CategoryRegistry:
    return CategoryRegistry.from_path(path)
