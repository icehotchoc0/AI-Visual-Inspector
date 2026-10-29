import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training"))

from evaluate_eval_set import evaluate, load_jobs  # noqa: E402
from tests.conftest import FakeVLM, analysis, defect, make_photo, make_pipeline, to_bytes


def test_eval_report_metrics(registry, test_settings, tmp_path):
    eval_dir = tmp_path / "eval" / "plumbing"
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    jobs = []
    for j in range(2):
        jid = f"J{j}"
        (eval_dir / "photos" / jid).mkdir(parents=True)
        photos = []
        for i, s in enumerate(shots):
            (eval_dir / "photos" / jid / f"p{i}.jpg").write_bytes(to_bytes(make_photo(1000 + 10 * j + i)))
            photos.append({"photo_id": f"p{i}", "shot_type": s, "phase": "after", "defects": [{"code": "leak_visible", "severity": "high"}] if (j == 1 and s == "leak_test") else []})
        jobs.append({"job_id": jid, "category": "plumbing", "expected_verdict": "FAIL" if j == 1 else "PASS", "photos": photos, "fraud": []})
    (eval_dir / "jobs.jsonl").write_text("\n".join(json.dumps(j) for j in jobs), encoding="utf-8")

    # Fake VLM finds the leak on every leak_test photo -> job 0 becomes a false FAIL, job 1 a true FAIL.
    fake = FakeVLM(script={"leak_test": analysis("leak_test", [defect("leak_visible", "high", 0.9)])})
    pipe = make_pipeline(registry, test_settings, fake)
    report = evaluate(pipe, eval_dir, load_jobs(eval_dir / "jobs.jsonl", None), 0.6, run_vlm=True)
    assert report["jobs"] == 2
    assert report["verdict_accuracy"] == 0.5
    assert report["verdict_confusion"] == {"FAIL->FAIL": 1, "PASS->FAIL": 1}
    assert report["shot_accuracy"] == 1.0
    assert report["defects"]["leak_visible"] == {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0}
