"""
Deterministic fraud / integrity checks.

Catches the submission problems a VLM is bad at and a hash is good at:
  * the same photo uploaded twice in one job (padding the checklist)
  * a photo re-used from an earlier job (cross-job reuse, via the store)
  * EXIF evidence that photos came from different places or times
  * EXIF software tags that indicate editing

Perceptual hashing is implemented here (64-bit dHash) rather than pulling
in another dependency; it is robust to re-encoding/resizing and small
crops, which is what messenger apps do to photos.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from PIL import Image

from backend.categories import FraudThresholds
from backend.schemas import Finding, JobContext, PhotoMeta

_EDITING_SOFTWARE_MARKERS = ("photoshop", "lightroom", "snapseed", "picsart", "gimp", "canva", "pixlr", "affinity")


# ------------------------------------------------------------ hashing ----
def dhash(image: Image.Image, hash_size: int = 8) -> str:
    """64-bit difference hash as a 16-char hex string."""
    import numpy as np

    gray = np.asarray(image.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS), dtype=np.int16)
    diff = gray[:, 1:] > gray[:, :-1]  # (hash_size, hash_size) booleans, row-major
    bits = 0
    for bit in diff.flatten():
        bits = (bits << 1) | int(bit)
    return f"{bits:0{hash_size * hash_size // 4}x}"


def hamming(hex_a: str, hex_b: str) -> int:
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- EXIF ----
def _rational(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            return v[0] / v[1]
        except Exception:  # noqa: BLE001
            return 0.0


def _dms_to_deg(dms, ref: str | None) -> Optional[float]:
    try:
        deg = _rational(dms[0]) + _rational(dms[1]) / 60.0 + _rational(dms[2]) / 3600.0
    except Exception:  # noqa: BLE001
        return None
    if ref in ("S", "W"):
        deg = -deg
    return round(deg, 6)


def read_exif(image: Image.Image) -> dict:
    """Best-effort EXIF extraction. Missing EXIF is normal (messenger apps strip it)."""
    out: dict = {"datetime": None, "lat": None, "lon": None, "camera_model": None, "software": None}
    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001
        return out
    if not exif:
        return out

    from PIL.ExifTags import IFD, Base

    model = exif.get(Base.Model)
    if model:
        out["camera_model"] = str(model).strip("\x00 ")
    software = exif.get(Base.Software)
    if software:
        out["software"] = str(software).strip("\x00 ")

    dt_raw = None
    try:
        exif_ifd = exif.get_ifd(IFD.Exif)
        dt_raw = exif_ifd.get(Base.DateTimeOriginal) or exif_ifd.get(Base.DateTimeDigitized)
    except Exception:  # noqa: BLE001
        pass
    dt_raw = dt_raw or exif.get(Base.DateTime)
    if dt_raw:
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                out["datetime"] = datetime.strptime(str(dt_raw).strip("\x00 "), fmt)
                break
            except ValueError:
                continue

    try:
        gps = exif.get_ifd(IFD.GPSInfo)
    except Exception:  # noqa: BLE001
        gps = {}
    if gps:
        # GPS tag numbers: 1 LatRef, 2 Lat, 3 LonRef, 4 Lon
        lat = _dms_to_deg(gps.get(2), gps.get(1)) if gps.get(2) else None
        lon = _dms_to_deg(gps.get(4), gps.get(3)) if gps.get(4) else None
        if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0):
            out["lat"], out["lon"] = lat, lon
    return out


def build_meta(image: Image.Image, raw_bytes: bytes) -> PhotoMeta:
    exif = read_exif(image)
    return PhotoMeta(
        exif_datetime=exif["datetime"],
        gps_lat=exif["lat"],
        gps_lon=exif["lon"],
        camera_model=exif["camera_model"],
        software=exif["software"],
        perceptual_hash=dhash(image),
        sha256=sha256_hex(raw_bytes),
    )


# ------------------------------------------------------------ geometry ----
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# -------------------------------------------------------------- checks ----
@dataclass
class HistoricalMatch:
    photo_id: str
    job_id: str
    inspection_id: str
    distance: int


class HashHistory:
    """Interface the store implements so this module stays DB-agnostic."""

    def find_similar(self, phash: str, max_distance: int, exclude_job_id: str) -> list[HistoricalMatch]:  # pragma: no cover
        raise NotImplementedError


def check_within_job_duplicates(metas: dict[str, PhotoMeta], thresholds: FraudThresholds) -> list[Finding]:
    findings: list[Finding] = []
    ids = list(metas)
    seen_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if metas[a].sha256 == metas[b].sha256:
                dist = 0
            else:
                dist = hamming(metas[a].perceptual_hash, metas[b].perceptual_hash)
            if dist <= thresholds.duplicate_hamming_max:
                seen_pairs.add((a, b))
                findings.append(
                    Finding(
                        code="duplicate_in_job",
                        kind="fraud",
                        source="fraud_check",
                        severity="high",
                        photo_id=b,
                        related_photo_ids=[a],
                        message=f"사진 '{b}'은(는) 같은 작업의 사진 '{a}'와 동일하거나 거의 동일합니다 (중복 제출).",
                        data={"hamming": dist},
                    )
                )
            elif dist <= thresholds.near_duplicate_hamming_max:
                findings.append(
                    Finding(
                        code="near_duplicate_in_job",
                        kind="fraud",
                        source="fraud_check",
                        severity="low",
                        confidence=0.7,
                        photo_id=b,
                        related_photo_ids=[a],
                        message=f"사진 '{b}'은(는) 사진 '{a}'와 매우 유사합니다. 같은 컷을 반복 제출한 것인지 확인이 필요합니다.",
                        data={"hamming": dist},
                    )
                )
    return findings


def check_cross_job_reuse(
    metas: dict[str, PhotoMeta],
    job_id: str,
    history: Optional[HashHistory],
    thresholds: FraudThresholds,
) -> list[Finding]:
    if history is None:
        return []
    findings: list[Finding] = []
    for photo_id, meta in metas.items():
        matches = history.find_similar(meta.perceptual_hash, thresholds.near_duplicate_hamming_max, exclude_job_id=job_id)
        if not matches:
            continue
        best = min(matches, key=lambda m: m.distance)
        exact = best.distance <= thresholds.duplicate_hamming_max
        findings.append(
            Finding(
                code="reused_from_other_job" if exact else "similar_to_other_job",
                kind="fraud",
                source="fraud_check",
                severity="high" if exact else "medium",
                confidence=1.0 if exact else 0.7,
                photo_id=photo_id,
                message=(
                    f"사진 '{photo_id}'은(는) 이전 작업 '{best.job_id}'에 제출된 사진과 {'동일' if exact else '매우 유사'}합니다 "
                    f"({'다른 현장 사진 재사용 의심' if exact else '확인 필요'})."
                ),
                data={"hamming": best.distance, "matched_job_id": best.job_id, "matched_inspection_id": best.inspection_id, "num_matches": len(matches)},
            )
        )
    return findings


def check_exif_consistency(metas: dict[str, PhotoMeta], job: JobContext, thresholds: FraudThresholds) -> list[Finding]:
    findings: list[Finding] = []

    # Editing software
    for photo_id, meta in metas.items():
        if meta.software and any(m in meta.software.lower() for m in _EDITING_SOFTWARE_MARKERS):
            findings.append(
                Finding(
                    code="edited_with_software",
                    kind="fraud",
                    source="fraud_check",
                    severity="medium",
                    confidence=0.8,
                    photo_id=photo_id,
                    message=f"사진 '{photo_id}'의 메타데이터에 편집 프로그램({meta.software})이 기록되어 있습니다.",
                    data={"software": meta.software},
                )
            )

    # GPS spread between photos of the same job
    gps = {pid: (m.gps_lat, m.gps_lon) for pid, m in metas.items() if m.gps_lat is not None and m.gps_lon is not None}
    if len(gps) >= 2:
        ids = list(gps)
        anchor = gps[ids[0]]
        far = []
        for pid in ids[1:]:
            d = haversine_km(anchor[0], anchor[1], gps[pid][0], gps[pid][1])
            if d > thresholds.gps_max_spread_km:
                far.append((pid, round(d, 2)))
        if far:
            findings.append(
                Finding(
                    code="gps_spread",
                    kind="fraud",
                    source="fraud_check",
                    severity="high",
                    confidence=0.9,
                    related_photo_ids=[ids[0]] + [p for p, _ in far],
                    message=f"같은 작업의 사진들이 서로 {max(d for _, d in far)} km 이상 떨어진 위치에서 촬영되었습니다 (다른 현장 사진 의심).",
                    data={"anchor_photo_id": ids[0], "far": far},
                )
            )

    # GPS vs declared site
    if job.site_lat is not None and job.site_lon is not None:
        for pid, (lat, lon) in gps.items():
            d = haversine_km(job.site_lat, job.site_lon, lat, lon)
            if d > max(thresholds.gps_max_spread_km, 1.0):
                findings.append(
                    Finding(
                        code="gps_far_from_site",
                        kind="fraud",
                        source="fraud_check",
                        severity="high",
                        confidence=0.85,
                        photo_id=pid,
                        message=f"사진 '{pid}'의 촬영 위치가 작업 현장에서 {d:.1f} km 떨어져 있습니다.",
                        data={"distance_km": round(d, 2)},
                    )
                )

    # Capture time vs scheduled window (with slack - technicians arrive early/leave late)
    if job.scheduled_start or job.scheduled_end:
        slack = timedelta(hours=3)
        start = (job.scheduled_start - slack) if job.scheduled_start else None
        end = (job.scheduled_end + slack) if job.scheduled_end else None
        for pid, meta in metas.items():
            dt = meta.exif_datetime
            if dt is None:
                continue
            dt_naive = dt.replace(tzinfo=None)
            s = start.replace(tzinfo=None) if start else None
            e = end.replace(tzinfo=None) if end else None
            if (s and dt_naive < s) or (e and dt_naive > e):
                findings.append(
                    Finding(
                        code="captured_outside_schedule",
                        kind="fraud",
                        source="fraud_check",
                        severity="medium",
                        confidence=0.75,
                        photo_id=pid,
                        message=f"사진 '{pid}'의 촬영 시각({dt_naive:%Y-%m-%d %H:%M})이 예정된 작업 시간대 밖입니다.",
                        data={"exif_datetime": dt_naive.isoformat()},
                    )
                )

    # Capture-time spread inside the job (photos days apart)
    times = sorted(m.exif_datetime.replace(tzinfo=None) for m in metas.values() if m.exif_datetime)
    if len(times) >= 2 and (times[-1] - times[0]) > timedelta(hours=24):
        findings.append(
            Finding(
                code="capture_time_spread",
                kind="fraud",
                source="fraud_check",
                severity="medium",
                confidence=0.7,
                message=f"같은 작업의 사진 촬영 시각이 {((times[-1]-times[0]).total_seconds()/3600):.0f}시간 이상 차이 납니다. 다른 날 촬영된 사진이 섞였는지 확인이 필요합니다.",
                data={"earliest": times[0].isoformat(), "latest": times[-1].isoformat()},
            )
        )

    # Missing EXIF everywhere is informational only - many apps strip it.
    if metas and all(m.exif_datetime is None and m.gps_lat is None for m in metas.values()):
        findings.append(
            Finding(
                code="no_exif",
                kind="info",
                source="fraud_check",
                severity="low",
                confidence=1.0,
                message="모든 사진에 촬영 시각/위치 메타데이터가 없어 시간·위치 검증을 수행하지 못했습니다 (메신저 전송 시 흔히 제거됨).",
            )
        )
    return findings


def run_fraud_checks(
    metas: dict[str, PhotoMeta],
    job: JobContext,
    thresholds: FraudThresholds,
    history: Optional[HashHistory] = None,
) -> list[Finding]:
    findings = []
    findings += check_within_job_duplicates(metas, thresholds)
    findings += check_cross_job_reuse(metas, job.job_id, history, thresholds)
    findings += check_exif_consistency(metas, job, thresholds)
    return findings
