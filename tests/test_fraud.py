from datetime import datetime

from backend.checks.fraud import HashHistory, HistoricalMatch, build_meta, dhash, hamming, run_fraud_checks
from backend.schemas import JobContext
from tests.conftest import make_photo, to_bytes


def _meta(img):
    return build_meta(img, to_bytes(img))


def test_dhash_stable_under_resize_and_recompress():
    img = make_photo(3)
    small = img.resize((400, 300))
    assert hamming(dhash(img), dhash(small)) <= 4


def test_dhash_distinguishes_different_photos():
    assert hamming(dhash(make_photo(1)), dhash(make_photo(2))) > 12


def test_duplicate_in_job_detected(registry):
    cat = registry.get("aircon_install")
    img = make_photo(5)
    metas = {"a": _meta(img), "b": _meta(img.resize((640, 480))), "c": _meta(make_photo(6))}
    findings = run_fraud_checks(metas, JobContext(job_id="j", category=cat.id), cat.fraud)
    dup = [f for f in findings if f.code == "duplicate_in_job"]
    assert len(dup) == 1 and dup[0].photo_id == "b" and dup[0].related_photo_ids == ["a"]


class _History(HashHistory):
    def __init__(self, phash):
        self.phash = phash

    def find_similar(self, phash, max_distance, exclude_job_id):
        d = hamming(phash, self.phash)
        return [HistoricalMatch("old", "job_old", "insp_old", d)] if d <= max_distance else []


def test_cross_job_reuse_detected(registry):
    cat = registry.get("plumbing")
    img = make_photo(7)
    metas = {"x": _meta(img), "y": _meta(make_photo(8))}
    findings = run_fraud_checks(metas, JobContext(job_id="new", category=cat.id), cat.fraud, history=_History(dhash(img)))
    reused = [f for f in findings if f.code == "reused_from_other_job"]
    assert len(reused) == 1 and reused[0].photo_id == "x" and reused[0].data["matched_job_id"] == "job_old"


def test_gps_spread_and_site_distance(registry):
    cat = registry.get("plumbing")
    m1, m2 = _meta(make_photo(9)), _meta(make_photo(10))
    m1.gps_lat, m1.gps_lon = 37.5665, 126.9780  # Seoul
    m2.gps_lat, m2.gps_lon = 35.1796, 129.0756  # Busan
    job = JobContext(job_id="j", category=cat.id, site_lat=37.5665, site_lon=126.9780)
    codes = {f.code for f in run_fraud_checks({"a": m1, "b": m2}, job, cat.fraud)}
    assert "gps_spread" in codes and "gps_far_from_site" in codes


def test_capture_outside_schedule(registry):
    cat = registry.get("plumbing")
    m = _meta(make_photo(11))
    m.exif_datetime = datetime(2026, 10, 1, 9, 0)
    job = JobContext(job_id="j", category=cat.id, scheduled_start=datetime(2026, 10, 20, 9, 0), scheduled_end=datetime(2026, 10, 20, 12, 0))
    codes = {f.code for f in run_fraud_checks({"a": m}, job, cat.fraud)}
    assert "captured_outside_schedule" in codes


def test_no_exif_is_informational_only(registry):
    cat = registry.get("plumbing")
    findings = run_fraud_checks({"a": _meta(make_photo(12))}, JobContext(job_id="j", category=cat.id), cat.fraud)
    assert [f.code for f in findings] == ["no_exif"]
    assert findings[0].kind == "info"


def test_editing_software_flagged(registry):
    cat = registry.get("plumbing")
    m = _meta(make_photo(13))
    m.software = "Adobe Photoshop 25.0"
    codes = {f.code for f in run_fraud_checks({"a": m}, JobContext(job_id="j", category=cat.id), cat.fraud)}
    assert "edited_with_software" in codes
