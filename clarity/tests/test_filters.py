"""Tests for Clarity's filters, pipeline, exports and reports. Run with:  python -m pytest tests

Most tests build a synthetic scene, damage it in a known way (blur, noise, shake, interference, perspective)
and check that the matching filter measurably undoes the damage.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import align, export, report  # noqa: E402
from backend.filters import FILTERS, Context, FilterError, catalogue  # noqa: E402
from backend.media import ImageSource, MediaError, SequenceSource, VideoSource, open_source  # noqa: E402
from backend.pipeline import ChainError, Pipeline, clean_chain  # noqa: E402

RNG = np.random.default_rng(7)


def scene(h=240, w=320) -> np.ndarray:
    """A textured test scene: shapes, text and fine detail, float BGR 0..1."""
    img = np.full((h, w, 3), 115, np.uint8)  # drawn in 8-bit: OpenCV 5's putText only draws on 8-bit images
    cv2.rectangle(img, (30, 40), (130, 120), (25, 50, 205), -1)
    cv2.circle(img, (220, 90), 45, (230, 205, 50), -1)
    cv2.putText(img, "AB12 CDE", (40, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (13, 13, 13), 3)
    for x in range(0, w, 9):
        cv2.line(img, (x, 0), (x + 20, h), (153, 153, 153), 1)
    img = img.astype(np.float32) / 255
    return img


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((np.clip(a, 0, 1) - np.clip(b, 0, 1)) ** 2))
    return 10 * np.log10(1 / max(mse, 1e-12))


def run(fid: str, img: np.ndarray, ctx: Context | None = None, **params) -> np.ndarray:
    f = FILTERS[fid]
    return np.clip(f.fn(img, f.clean(params), ctx or Context()), 0, 1)


# ---------------------------------------------------------------- registry and text
def test_every_filter_is_documented() -> None:
    assert len(FILTERS) >= 30
    for f in catalogue():
        for key in ("summary", "use", "method", "caveats"):
            assert len(f[key]) > 30, f"{f['id']} needs a fuller {key}"
        for p in f["params"]:
            assert p["label"]


@pytest.mark.parametrize("fid", sorted(FILTERS))
def test_every_filter_runs_with_defaults(fid: str) -> None:
    img = scene()
    img.flags.writeable = False  # filters must never modify their input
    frames = [img] * 5
    out = FILTERS[fid].fn(img, FILTERS[fid].clean({}), Context(index=2, count=5, fps=25, frame=lambda i: frames[i]))
    out = np.asarray(out)
    assert out.ndim == 3 and out.shape[2] == 3 and np.isfinite(out).all()


def test_params_are_clamped_and_validated() -> None:
    f = FILTERS["levels"]
    p = f.clean({"gamma": 999, "in_black": "x", "channel": "purple"})
    assert p["gamma"] == 5.0 and p["in_black"] == 0 and p["channel"] == "all"
    pts = FILTERS["perspective"].clean({"corners": [[0, 0], [1, 1]]})["corners"]
    assert pts == []  # needs exactly four


# ---------------------------------------------------------------- tone
def test_levels_maps_black_and_white_points() -> None:
    img = np.dstack([np.linspace(0, 1, 256, dtype=np.float32)[None, :].repeat(4, 0)] * 3)
    out = run("levels", img, in_black=64, in_white=192)
    assert out[0, 64, 0] == pytest.approx(0, abs=1e-6)
    assert out[0, 192, 0] == pytest.approx(1, abs=1e-6)
    assert out[0, 128, 0] == pytest.approx(0.5, abs=0.01)
    assert run("levels", img, gamma=2.0)[0, 64, 0] > img[0, 64, 0]


def test_exposure_brightens_without_clipping_highlights() -> None:
    dark = scene() * 0.15
    out = run("exposure", dark, ev=2.5, rolloff=True)
    assert out.mean() > dark.mean() * 3
    assert run("exposure", np.ones((4, 4, 3), np.float32), ev=3)[0, 0, 0] == pytest.approx(1, abs=1e-5)


def test_shadows_lift_dark_areas_more_than_bright() -> None:
    img = np.full((100, 200, 3), 0.9, np.float32)
    img[:, :100] = 0.08
    out = run("shadows_highlights", img, shadows=80)
    assert out[50, 20, 0] / img[50, 20, 0] > 2
    assert out[50, 180, 0] / img[50, 180, 0] < 1.3


def test_auto_levels_and_clahe_increase_contrast() -> None:
    flat = scene() * 0.2 + 0.4
    local = lambda im: np.abs(im - cv2.GaussianBlur(im, (0, 0), 4)).mean()  # noqa: E731  local contrast
    assert run("auto_levels", flat).std() > flat.std() * 3
    assert local(run("clahe", flat, mode="adaptive", clip=4)) > local(flat) * 1.3  # contrast-limited by design
    assert local(run("clahe", flat, mode="global")) > local(flat) * 3


def test_white_balance_neutralises_a_cast() -> None:
    grey = np.full((50, 50, 3), 0.5, np.float32)
    cast = grey * np.array([0.6, 0.9, 1.3], np.float32)  # orange (sodium-like) cast in BGR
    for mode in ("grayworld", "whitepatch"):
        out = run("white_balance", cast, mode=mode)
        assert np.ptp(out[25, 25]) < 0.01
    out = run("white_balance", cast, mode="point", point=[[10, 10]])
    assert np.ptp(out[25, 25]) < 0.01


# ---------------------------------------------------------------- deblur and noise
def test_motion_deblur_recovers_a_blurred_scene() -> None:
    sharp = scene()
    psf = __import__("backend.filters", fromlist=["motion_psf"]).motion_psf(15, 30)
    blurred = cv2.filter2D(sharp, -1, psf, borderType=cv2.BORDER_REFLECT)
    for method, gain in (("wiener", 4), ("lucy", 3)):
        out = run("motion_deblur", blurred, length=15, angle=30, noise=0.001, method=method, iterations=150)
        assert psnr(out, sharp) > psnr(blurred, sharp) + gain, method


def test_defocus_deblur_recovers_a_defocused_scene() -> None:
    from backend.filters import defocus_psf

    sharp = scene()
    blurred = cv2.filter2D(sharp, -1, defocus_psf(4, "disk"), borderType=cv2.BORDER_REFLECT)
    out = run("defocus_deblur", blurred, radius=4, noise=0.002)
    assert psnr(out, sharp) > psnr(blurred, sharp) + 3


def natural(h=240, w=320, seed=0) -> np.ndarray:
    """Smooth random texture with no repeating structure (the line pattern in scene() is itself periodic)."""
    tex = cv2.GaussianBlur(np.random.default_rng(seed).random((h, w)).astype(np.float32), (0, 0), 4)
    tex = (tex - tex.min()) / (tex.max() - tex.min())
    return np.dstack([tex] * 3) * 0.6 + 0.2


def test_periodic_noise_is_removed_and_scene_kept() -> None:
    clean = natural()
    yy, xx = np.mgrid[:240, :320]
    for period in ((7.0, 23.0), (5.3, 1e9)):  # diagonal and vertical stripes, not whole cycles per frame
        stripes = (0.12 * np.sin(2 * np.pi * (xx / period[0] + yy / period[1]))).astype(np.float32)[..., None]
        noisy = np.clip(clean + stripes, 0, 1)
        ctx = Context()
        out = run("periodic_noise", noisy, ctx)
        assert psnr(out, clean) > psnr(noisy, clean) + 10, period
        assert "removed" in ctx.notes[0]
    untouched = Context()
    assert psnr(run("periodic_noise", clean, untouched), clean) > 60
    assert untouched.notes == ["no periodic pattern found"]


def test_denoisers_reduce_gaussian_noise() -> None:
    clean = cv2.GaussianBlur(scene(), (0, 0), 1.2)
    noisy = np.clip(clean + RNG.normal(0, 0.06, clean.shape).astype(np.float32), 0, 1)
    for fid in ("gaussian_blur", "median", "bilateral", "nlmeans"):
        assert psnr(run(fid, noisy), clean) > psnr(noisy, clean) + 1, fid


def test_fft_filters_and_flatten() -> None:
    img = scene()
    low = run("fft_filter", img, mode="lowpass", high=0.1)
    assert np.abs(cv2.Laplacian(low, -1)).mean() < np.abs(cv2.Laplacian(img, -1)).mean()
    assert run("fft_filter", img, mode="highpass", low=0.05).mean() == pytest.approx(img.mean(), abs=0.03)
    shaded = img * np.linspace(0.3, 1.0, 320, dtype=np.float32)[None, :, None]
    flat = run("flatten", shaded, radius=60, stretch=False)
    left, right = flat[:, :60].mean(), flat[:, -60:].mean()
    assert abs(left - right) < abs(shaded[:, :60].mean() - shaded[:, -60:].mean()) / 2


# ---------------------------------------------------------------- geometry
def test_perspective_rectifies_a_known_quad() -> None:
    flat = scene(200, 400)
    dst = np.float32([[60, 40], [420, 80], [400, 300], [80, 260]])
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [400, 0], [400, 200], [0, 200]]), dst)
    oblique = cv2.warpPerspective(flat, m, (480, 340), flags=cv2.INTER_CUBIC)
    out = run("perspective", oblique, corners=dst.tolist(), aspect=2.0)
    restored = cv2.resize(out, (400, 200), interpolation=cv2.INTER_AREA)
    assert psnr(restored[10:-10, 10:-10], flat[10:-10, 10:-10]) > 22
    full = run("perspective", oblique, corners=dst.tolist(), output="full")
    assert full.shape[0] > out.shape[0]


def test_perspective_without_corners_is_a_no_op() -> None:
    img = scene()
    ctx = Context()
    assert np.array_equal(run("perspective", img, ctx), img)
    assert "no corners" in ctx.notes[0]


def test_crop_rotate_resize_aspect() -> None:
    img = scene()
    assert run("crop", img, corners=[[200, 150], [10, 20]]).shape == (130, 190, 3)
    with pytest.raises(FilterError):
        run("crop", img, corners=[[10, 10], [11, 11]])
    assert run("rotate", img, angle=90).shape == (320, 240, 3)
    assert np.array_equal(run("rotate", run("rotate", img, flip="h"), flip="h"), img)
    assert run("resize", img, scale=2, interpolation="nearest").shape == (480, 640, 3)
    assert np.array_equal(run("resize", img, scale=2, interpolation="nearest")[::2, ::2], img)
    out = run("aspect_ratio", np.zeros((576, 704, 3), np.float32), ratio="4:3")
    assert out.shape[:2] == (576, 768)
    with pytest.raises(FilterError):
        run("resize", np.zeros((8000, 8000, 3), np.float32), scale=8)


def test_lens_distortion_straightens_barrel_lines() -> None:
    img = np.zeros((300, 400, 3), np.float32)
    for y in range(30, 300, 60):
        cv2.line(img, (0, y), (399, y), (1, 1, 1), 2)
    # apply barrel distortion with the inverse model: correction with k1 = -0.25 should undo it
    straight = run("lens_distortion", img, k1=0.0)
    assert np.allclose(straight, img, atol=0.02)
    curved = run("lens_distortion", img, k1=0.3)
    restored = run("lens_distortion", curved, k1=-0.3)
    rows = lambda im: np.argmax(im[:, 20, 0] > 0.5)  # noqa: E731
    assert abs(rows(restored) - rows(img)) < abs(rows(curved) - rows(img)) + 1


def test_fisheye_unrolls_a_ring_into_a_band() -> None:
    img = np.zeros((400, 400, 3), np.float32)
    cv2.circle(img, (200, 200), 150, (1, 1, 1), 6)  # a ring at 75 % of the radius
    out = run("fisheye", img, mode="panorama", inner=0)
    row_profile = out[..., 0].mean(1)
    peak = int(np.argmax(row_profile))
    assert row_profile[peak] > 0.8  # the ring becomes a straight, horizontal band
    assert abs(peak / out.shape[0] - 0.25) < 0.05
    view = run("fisheye", img, mode="perspective", tilt=0, view_fov=90)
    assert view.shape[1] > view.shape[0]


def test_deinterlace_removes_combing() -> None:
    progressive = cv2.resize(scene(), (320, 240))
    shifted = np.roll(progressive, 6, axis=1)
    interlaced = progressive.copy()
    interlaced[1::2] = shifted[1::2]  # odd field recorded later, after motion
    comb = lambda im: np.abs(np.diff(im, axis=0)).mean()  # noqa: E731
    for mode in ("top", "bottom", "blend"):
        assert comb(run("deinterlace", interlaced, mode=mode)) < comb(interlaced), mode


# ---------------------------------------------------------------- multi-frame
def shaky_frames(n=12, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    noise_rng = np.random.default_rng(seed + 100)  # separate, so noisy and clean runs share the same shake
    base = cv2.copyMakeBorder(scene(), 20, 20, 20, 20, cv2.BORDER_REFLECT)
    frames, shifts = [], []
    for _ in range(n):
        dx, dy = rng.uniform(-6, 6, 2)
        m = np.float32([[1, 0, dx], [0, 1, dy]])
        f = cv2.warpAffine(base, m, (base.shape[1], base.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)[20:-20, 20:-20]
        if noise:
            f = np.clip(f + noise_rng.normal(0, noise, f.shape).astype(np.float32), 0, 1)
        frames.append(np.ascontiguousarray(f))
        shifts.append((dx, dy))
    return frames, shifts


def test_registration_finds_known_shift() -> None:
    frames, shifts = shaky_frames(2)
    m, how = align.register(align.to_gray8(frames[0]), align.to_gray8(frames[1]), "translation")
    expected = np.subtract(shifts[0], shifts[1])
    assert np.allclose(m[:2, 2], expected, atol=0.3), (m, expected, how)
    m2, how2 = align.register(align.to_gray8(frames[0]), align.to_gray8(frames[1]), "similarity")
    assert np.allclose(m2[:2, 2], expected, atol=1.0) and "ORB" in how2


def test_frame_integration_reduces_noise() -> None:
    frames, _ = shaky_frames(9, noise=0.08)
    clean, _ = shaky_frames(9)
    ctx = Context(index=4, count=9, fps=25, frame=lambda i: frames[i])
    out = run("frame_integration", frames[4], ctx, frames=9, align="translation")
    crop = (slice(12, -12), slice(12, -12))
    assert psnr(out[crop], clean[4][crop]) > psnr(frames[4][crop], clean[4][crop]) + 4
    assert "aligned" in ctx.notes[0]


def test_stabilize_removes_shake() -> None:
    frames, _ = shaky_frames(12)
    ctx_for = lambda i: Context(index=i, count=12, fps=25, frame=lambda j: frames[j])  # noqa: E731
    locked = [run("stabilize", frames[i], ctx_for(i), mode="lock", reference=0, model="translation") for i in range(12)]
    crop = (slice(15, -15), slice(15, -15))
    before = np.mean([np.abs(frames[i][crop] - frames[0][crop]).mean() for i in range(1, 12)])
    after = np.mean([np.abs(locked[i][crop] - frames[0][crop]).mean() for i in range(1, 12)])
    assert after < before / 3
    smooth = [run("stabilize", frames[i], ctx_for(i), radius=5, model="translation") for i in range(12)]
    jitter = lambda seq: np.mean([np.abs(seq[i + 1][crop] - seq[i][crop]).mean() for i in range(11)])  # noqa: E731
    assert jitter(smooth) < jitter(frames)


def test_super_resolution_beats_single_frame_interpolation() -> None:
    hi = cv2.GaussianBlur(scene(240, 320), (0, 0), 0.7)
    rng = np.random.default_rng(3)
    lows, truths, n = [], [], 12
    for _ in range(n):
        dx, dy = rng.uniform(-1.5, 1.5, 2)
        moved = cv2.warpAffine(hi, np.float32([[1, 0, dx], [0, 1, dy]]), (320, 240), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        low = cv2.resize(moved, (160, 120), interpolation=cv2.INTER_AREA)
        lows.append(np.clip(low + rng.normal(0, 0.02, low.shape).astype(np.float32), 0, 1))
        truths.append(moved)
    ctx = Context(index=0, count=n, fps=25, frame=lambda i: lows[i])
    out = run("super_resolution", lows[0], ctx, frames=n, scale=2, window="forward")
    assert out.shape == (240, 320, 3) and "12/12 registered" in ctx.notes[0]
    single = cv2.resize(lows[0], (320, 240), interpolation=cv2.INTER_CUBIC)
    crop = (slice(12, -12), slice(12, -12))
    assert psnr(out[crop], truths[0][crop]) > psnr(single[crop], truths[0][crop]) + 1.5


def test_temporal_filters_on_a_single_image_leave_a_note() -> None:
    for fid in ("frame_integration", "stabilize"):
        ctx = Context()
        out = run(fid, scene(), ctx)
        assert np.array_equal(out, scene()) and "needs a video" in ctx.notes[0]


# ---------------------------------------------------------------- sources, pipeline, export, report
@pytest.fixture()
def video(tmp_path: Path) -> Path:
    frames, _ = shaky_frames(20, noise=0.03)
    path = tmp_path / "cctv.avi"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 25, (320, 240))
    for f in frames:
        w.write((f * 255).astype(np.uint8))
    w.release()
    return path


@pytest.fixture()
def still(tmp_path: Path) -> Path:
    path = tmp_path / "plate photo.png"
    cv2.imwrite(str(path), (scene() * 65535).astype(np.uint16))
    return path


def test_sources_open_and_hash(still: Path, video: Path, tmp_path: Path) -> None:
    img = open_source(str(still))
    assert isinstance(img, ImageSource) and img.details["bit_depth"] == 16
    assert not img.frame(0).flags.writeable
    img.wait_hashes(10)
    import hashlib

    assert img.info()["files"][0]["sha256"] == hashlib.sha256(still.read_bytes()).hexdigest()

    vid = open_source(str(video))
    assert isinstance(vid, VideoSource) and vid.count == 20 and vid.fps == pytest.approx(25)
    a = vid.frame(13).copy()
    vid.frame(2)
    assert np.array_equal(vid.frame(13), a)  # random access gives the same frame back

    paths = []
    for i in range(3):
        p = tmp_path / f"shot_{10 - i}.png"
        cv2.imwrite(str(p), (scene() * 255).astype(np.uint8))
        paths.append(str(p))
    seq = open_source(paths)
    assert isinstance(seq, SequenceSource) and seq.count == 3
    assert [Path(p).name for p in seq.paths] == ["shot_8.png", "shot_9.png", "shot_10.png"]  # natural order

    with pytest.raises(MediaError):
        open_source(str(tmp_path / "missing.png"))


def test_pipeline_caches_and_reports_steps(video: Path) -> None:
    pipe = Pipeline(open_source(str(video)))
    steps = clean_chain([
        {"id": "deinterlace", "params": {"mode": "blend"}},
        {"id": "frame_integration", "params": {"frames": 5}},
        {"id": "levels", "enabled": False, "params": {"gamma": 2}},
        {"id": "unsharp"},
    ])
    img, results = pipe.render(steps, 10)
    assert img.shape == (240, 320, 3) and [r.step.id for r in results] == ["deinterlace", "frame_integration", "levels", "unsharp"]
    assert results[2].shape is None and results[1].notes
    cached = len(pipe._cache)
    steps[3].params["amount"] = 50  # only the last step should be re-run
    pipe.render(steps, 10)
    assert len(pipe._cache) == cached + 1
    before_step, _ = pipe.render(steps, 10, upto=1)
    assert before_step.shape == img.shape
    with pytest.raises(ChainError):
        clean_chain([{"id": "nope"}])


def test_failing_step_stops_the_chain_with_a_message(still: Path) -> None:
    pipe = Pipeline(open_source(str(still)))
    steps = clean_chain([{"id": "crop", "params": {"corners": [[5, 5], [6, 6]]}}, {"id": "invert"}])
    img, results = pipe.render(steps, 0)
    assert "too small" in results[0].error and results[1].shape is None
    assert img.shape == (240, 320, 3)


def test_exports_are_written_and_hashed(video: Path, tmp_path: Path) -> None:
    pipe = Pipeline(open_source(str(video)))
    steps = clean_chain([{"id": "stabilize", "params": {"radius": 3}}, {"id": "crop", "params": {"corners": [[10, 10], [210, 160]]}}])
    job = export.Job(id="t", kind="image")
    rec = export.export_image(job, pipe, steps, 5, str(tmp_path / "frame.png"), 16)
    back = cv2.imread(rec["path"], cv2.IMREAD_UNCHANGED)
    assert back.dtype == np.uint16 and back.shape == (150, 200, 3) and rec["bit_depth"] == 16
    for fmt in ("mkv_ffv1", "avi_mjpg", "mp4", "png_seq"):
        job = export.Job(id=fmt, kind="video")
        out = tmp_path / (f"seq_{fmt}" if fmt == "png_seq" else f"out_{fmt}{export.VIDEO_FORMATS[fmt]['ext']}")
        rec = export.export_video(job, pipe, steps, 2, 8, str(out), fmt)
        assert rec["frames"] == 7 and rec["width"] == 200
        if fmt == "png_seq":
            assert len(list(out.glob("*.png"))) == 7 and (out / "SHA256SUMS.txt").exists()
        else:
            cap = cv2.VideoCapture(str(out))
            assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 7
        assert len(rec["hashes"][rec["path"]]["sha256"]) == 64
    if True:  # lossless export must reproduce the pipeline exactly
        cap = cv2.VideoCapture(str(tmp_path / "out_mkv_ffv1.mkv"))
        ok, first = cap.read()
        expect, _ = pipe.render(steps, 2)
        assert ok and np.abs(first.astype(int) - (expect * 255 + 0.5).astype(int)).max() <= 1


def test_cancelled_video_export_leaves_no_file(video: Path, tmp_path: Path) -> None:
    pipe = Pipeline(open_source(str(video)))
    job = export.Job(id="c", kind="video")
    job.cancel_event.set()
    with pytest.raises(export.ExportCancelled):
        export.export_video(job, pipe, [], 0, 10, str(tmp_path / "x.mkv"), "mkv_ffv1")
    assert not (tmp_path / "x.mkv").exists()


def test_report_explains_every_step_and_lists_every_filter(video: Path) -> None:
    pipe = Pipeline(open_source(str(video)))
    steps = clean_chain([
        {"id": "deinterlace"}, {"id": "stabilize", "params": {"radius": 2}},
        {"id": "perspective", "params": {"corners": [[20, 20], [300, 30], [290, 220], [30, 210]]}},
        {"id": "levels", "enabled": False},
    ])
    meas = {"calibration": {"points": [[0, 0], [100, 0]], "length": 2, "unit": "m", "frame": 3},
            "items": [{"type": "distance", "points": [[0, 0], [50, 0]], "frame": 3},
                      {"type": "speed", "a": {"frame": 0, "point": [0, 0]}, "b": {"frame": 25, "point": [500, 0]}}]}
    ctx = report.build_context(pipe, steps, 3, {"case_number": "C-42", "examiner": "HS"}, meas, [])
    page = report.generate_report_html(ctx)
    for f in FILTERS.values():
        assert f.name.replace("&", "&amp;") in page  # appendix covers every filter
    for st in ctx["stages"]:
        assert st["summary"] and st["method"] and st["caveats"]
    assert ctx["stages"][3]["enabled"] is False and "disabled: not applied" in page
    assert ctx["stages"][0]["thumbnail"].startswith("data:image/jpeg")
    assert ctx["source"]["files"][0]["sha256"] in page and "C-42" in page
    m = ctx["measurements"]
    assert m["items"][0]["value"] == pytest.approx(1.0)
    assert m["items"][1]["kmh"] == pytest.approx(10.0 / 1.0 * 3.6)  # 10 m in 1 s
    data = report.generate_report_json(ctx)
    json.dumps(data)
    assert "images" not in data and "thumbnail" not in data["stages"][0]
    ref = report.generate_reference_html()
    assert all(f.name.replace("&", "&amp;") in ref for f in FILTERS.values())


def test_api_round_trip(video: Path, tmp_path: Path) -> None:
    from backend.api import Api

    class FakeWindow:
        def __init__(self):
            self.answers = []

        def create_file_dialog(self, *a, **k):
            return self.answers.pop(0)

    api, win = Api(), FakeWindow()
    api._attach(win)
    assert api.preview({"chain": []})["ok"] is False  # nothing open yet
    info = api.open_source(str(video))
    assert info["ok"] and info["data"]["count"] == 20
    chain = [{"id": "levels", "params": {"gamma": 1.5}}, {"id": "resize", "params": {"scale": 2}}]
    pv = api.preview({"chain": chain, "index": 4, "original": True, "gen": 1})["data"]
    assert pv["width"] == 640 and pv["image"].startswith("data:image/jpeg") and pv["original"]
    assert api.preview({"chain": chain, "index": 4, "gen": 0})["data"].get("stale")  # older than the last request
    assert api.preview({"chain": [{"id": "bogus"}], "gen": 5})["ok"] is False

    win.answers = [str(tmp_path / "proj.clarity.json")]
    assert api.save_project({"chain": chain, "index": 4, "case": {"examiner": "HS"}})["ok"]
    proj = api.load_project(str(tmp_path / "proj.clarity.json"))["data"]
    assert proj["chain"][0]["params"]["gamma"] == 1.5 and not proj["missing"]
    reopened = api.open_source(proj["source_paths"], proj["expected"])["data"]
    api._source.wait_hashes(10)
    assert api.source_info()["data"]["hash_match"] is True

    win.answers = [str(tmp_path / "rep.html")]
    assert api.save_report({"chain": chain, "index": 4}, "html")["data"]["path"]
    assert "Levels" in (tmp_path / "rep.html").read_text(encoding="utf-8")
    win.answers = [str(video)]
    assert "overwrite the source" in api.export_image({"chain": chain})["error"]
    assert reopened["count"] == 20
