"""Frame registration: how far one frame has moved relative to another.

Used by stabilisation, frame integration and multi-frame super-resolution. Every function returns a 3x3
matrix that maps pixel coordinates in the *moving* frame to the *reference* frame, so
cv2.warpPerspective(moving, M, ref_size) lines the moving frame up on the reference.
"""
from __future__ import annotations

import cv2
import numpy as np

MODELS = ("translation", "similarity", "perspective")
MAX_FEATURE_SIDE = 1280  # feature matching runs on a downscaled copy of larger frames
MIN_INLIERS = 8


def to_gray8(img: np.ndarray) -> np.ndarray:
    """Float BGR [0,1] → contrast-normalised 8-bit grey, so dark CCTV frames still have features to match."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    lo, hi = np.percentile(gray, (0.5, 99.5))
    gray = np.clip((gray - lo) / max(hi - lo, 1e-6), 0, 1)
    return (gray * 255).astype(np.uint8)


def _phase(ref: np.ndarray, mov: np.ndarray) -> tuple[np.ndarray, float]:
    """Sub-pixel translation by phase correlation. Returns (matrix, response 0..1)."""
    a, b = ref.astype(np.float32), mov.astype(np.float32)
    window = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a, b, window)
    # phaseCorrelate reports how far `mov` is shifted from `ref`; undo that shift
    return np.array([[1, 0, -dx], [0, 1, -dy], [0, 0, 1]], np.float64), float(response)


def _ecc_translation(ref: np.ndarray, mov: np.ndarray, init: np.ndarray) -> np.ndarray:
    """Refine a translation with ECC (enhanced correlation coefficient) for sub-pixel accuracy."""
    warp = init[:2].astype(np.float32).copy()
    # ECC's warp maps reference coordinates into the moving image, the inverse of ours
    warp[:, 2] *= -1
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 1e-4)  # phase correlation is already close
    try:
        _, warp = cv2.findTransformECC(ref, mov, warp, cv2.MOTION_TRANSLATION, criteria, None, 3)
    except cv2.error:
        return init
    return np.array([[1, 0, -warp[0, 2]], [0, 1, -warp[1, 2]], [0, 0, 1]], np.float64)


def _features(ref: np.ndarray, mov: np.ndarray, model: str) -> np.ndarray | None:
    scale = min(1.0, MAX_FEATURE_SIDE / max(ref.shape))
    if scale < 1:
        ref = cv2.resize(ref, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        mov = cv2.resize(mov, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    orb = cv2.ORB_create(3000)
    k1, d1 = orb.detectAndCompute(ref, None)
    k2, d2 = orb.detectAndCompute(mov, None)
    if d1 is None or d2 is None or len(k1) < MIN_INLIERS or len(k2) < MIN_INLIERS:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d2, d1)
    if len(matches) < MIN_INLIERS:
        return None
    src = np.float32([k2[m.queryIdx].pt for m in matches])
    dst = np.float32([k1[m.trainIdx].pt for m in matches])
    if model == "perspective":
        m, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    else:
        m, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
        if m is not None:
            m = np.vstack([m, [0, 0, 1]])
    if m is None or inliers is None or int(inliers.sum()) < MIN_INLIERS:
        return None
    s = np.diag([scale, scale, 1.0])
    return np.linalg.inv(s) @ m @ s  # back to full-resolution coordinates


def register(ref: np.ndarray, mov: np.ndarray, model: str = "translation") -> tuple[np.ndarray, str]:
    """Estimate the motion of `mov` relative to `ref` (both 8-bit grey, same size).

    Returns (3x3 matrix mapping mov → ref, method used). Falls back to phase correlation, then to no motion,
    so callers always get a matrix; the method string says which one was used.
    """
    if model not in MODELS:
        raise ValueError(f"Unknown motion model: {model}")
    if model != "translation":
        m = _features(ref, mov, model)
        if m is not None:
            return m, f"{model} (ORB features + RANSAC)"
    m, response = _phase(ref, mov)
    if response < 0.02:
        return np.eye(3), "none (no reliable match)"
    return _ecc_translation(ref, mov, m), "translation (phase correlation + ECC)"


def warp(img: np.ndarray, m: np.ndarray, size: tuple[int, int], border: str = "black",
         interpolation: int = cv2.INTER_CUBIC) -> np.ndarray:
    mode = cv2.BORDER_REPLICATE if border == "replicate" else cv2.BORDER_CONSTANT
    if np.allclose(m[2], [0, 0, 1]):
        return cv2.warpAffine(img, m[:2], size, flags=interpolation, borderMode=mode)
    return cv2.warpPerspective(img, m, size, flags=interpolation, borderMode=mode)


def decompose(m: np.ndarray) -> tuple[float, float, float, float]:
    """Similarity part of a matrix as (dx, dy, angle radians, log scale), for averaging trajectories."""
    a, b = m[0, 0], m[1, 0]
    return float(m[0, 2]), float(m[1, 2]), float(np.arctan2(b, a)), float(np.log(max(np.hypot(a, b), 1e-6)))


def compose(dx: float, dy: float, angle: float, log_scale: float) -> np.ndarray:
    s = np.exp(log_scale)
    c, n = s * np.cos(angle), s * np.sin(angle)
    return np.array([[c, -n, dx], [n, c, dy], [0, 0, 1]], np.float64)
