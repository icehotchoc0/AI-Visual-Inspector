from backend.checks.checklist import evaluate_checklist


def test_all_required_present_no_findings(registry):
    cat = registry.get("aircon_install")
    resolved = {f"p{i}": s.id for i, s in enumerate(cat.required_shots)}
    items, findings = evaluate_checklist(cat, resolved)
    assert findings == []
    assert all(i.satisfied for i in items if i.required)


def test_missing_required_shot(registry):
    cat = registry.get("aircon_install")
    items, findings = evaluate_checklist(cat, {"p1": "indoor_unit_before", "p2": "indoor_unit_after"})
    missing = {f.data["shot_id"] for f in findings if f.code == "missing_required_shot"}
    assert missing == {"outdoor_unit_after", "pipe_finish"}


def test_rejected_photo_does_not_satisfy_shot(registry):
    cat = registry.get("plumbing")
    resolved = {"p1": "site_before", "p2": "pipe_connection", "p3": "leak_test", "p4": "site_after"}
    items, findings = evaluate_checklist(cat, resolved, rejected_photo_ids={"p3"})
    f = next(f for f in findings if f.code == "missing_required_shot")
    assert f.data["shot_id"] == "leak_test" and "반려" in f.message and f.related_photo_ids == ["p3"]


def test_before_after_incomplete(registry):
    cat = registry.get("door_install")
    _, findings = evaluate_checklist(cat, {"p1": "door_closed", "p2": "door_open"})
    assert "before_after_incomplete" in {f.code for f in findings}


def test_unclassified_photo_flagged(registry):
    cat = registry.get("plumbing")
    _, findings = evaluate_checklist(cat, {"p1": None})
    assert any(f.code == "unclassified_photo" and f.photo_id == "p1" for f in findings)
