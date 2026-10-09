"""Every enhancement filter Clarity offers, with the explanations the report and Filter Guide show.

Each filter is a function `fn(img, params, ctx) -> img` registered with @register. Images are float32 BGR,
values 0..1, H x W x 3. Filters must not modify `img` in place (source frames are read-only). The pipeline
clips each result to 0..1.

The text attached to each filter is written for a report reader, not a programmer:
  summary  what it does, in one or two sentences
  use      when an examiner would reach for it
  method   how it works, precisely enough to reproduce
  caveats  what it cannot do, or what it might introduce

Temporal filters (stabilisation, frame integration, super-resolution) read neighbouring frames through
ctx.frame(i), which returns the output of the earlier filters in the chain for frame i.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

import cv2
import numpy as np

from . import align

CATEGORIES = (
    ("adjust", "Levels & exposure"),
    ("colour", "Colour & channels"),
    ("sharpen", "Sharpen & deblur"),
    ("denoise", "Denoise & frequency"),
    ("geometry", "Geometry & perspective"),
    ("lens", "Lens & camera"),
    ("video", "Video & multi-frame"),
)
CATEGORY_NAMES = dict(CATEGORIES)
INTERPOLATIONS = (
    ("nearest", "Nearest neighbour (keeps original pixel values)"),
    ("linear", "Bilinear"),
    ("cubic", "Bicubic"),
    ("lanczos", "Lanczos"),
)
INTER = {"nearest": cv2.INTER_NEAREST, "linear": cv2.INTER_LINEAR, "cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4}
MAX_OUTPUT_PIXELS = 80_000_000  # resize/warp/super-resolution refuse to make anything bigger than this


class FilterError(ValueError):
    """A filter can't run with these parameters; the message is shown to the user."""


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    kind: str  # float | int | choice | bool | points
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    unit: str = ""
    help: str = ""
    count: int = 0  # points: how many to pick
    labels: tuple[str, ...] = ()  # points: what each one is

    def public(self) -> dict:
        d = {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default, "unit": self.unit,
             "help": self.help}
        if self.kind in ("float", "int"):
            d.update(min=self.min, max=self.max, step=self.step)
        if self.kind == "choice":
            d["choices"] = [list(c) for c in self.choices]
        if self.kind == "points":
            d.update(count=self.count, labels=list(self.labels))
        return d

    def clean(self, value):
        """Coerce a value from the page (or a project file) into range; unknown values fall back to the default."""
        try:
            if self.kind == "bool":
                return bool(value)
            if self.kind == "choice":
                return value if value in {c[0] for c in self.choices} else self.default
            if self.kind == "points":
                pts = [[float(p[0]), float(p[1])] for p in (value or [])][: self.count]
                return pts if len(pts) == self.count and all(map(math.isfinite, sum(pts, []))) else []
            v = float(value)
            if not math.isfinite(v):
                return self.default
            if self.min is not None:
                v = max(self.min, v)
            if self.max is not None:
                v = min(self.max, v)
            return int(round(v)) if self.kind == "int" else v
        except (TypeError, ValueError, IndexError):
            return self.default


@dataclass(frozen=True)
class Filter:
    id: str
    name: str
    category: str
    summary: str
    use: str
    method: str
    caveats: str
    params: tuple[Param, ...]
    fn: Callable = field(repr=False)
    temporal: bool = False

    def clean(self, raw: dict | None) -> dict:
        raw = raw or {}
        return {p.key: p.clean(raw.get(p.key, p.default)) for p in self.params}

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category, "category_name": CATEGORY_NAMES[self.category],
            "summary": self.summary, "use": self.use, "method": self.method, "caveats": self.caveats,
            "temporal": self.temporal, "params": [p.public() for p in self.params],
        }


@dataclass
class Context:
    """What a filter can see beyond its own input image."""
    index: int = 0  # frame number being rendered
    count: int = 1  # frames in the source
    fps: float = 0.0
    frame: Callable[[int], np.ndarray] | None = None  # input to this filter for another frame number
    notes: list[str] = field(default_factory=list)  # shown under the filter and in the report
    key: str = ""  # identifies the steps before this one, so per-frame results can be cached across renders

    def note(self, text: str) -> None:
        self.notes.append(text)


FILTERS: dict[str, Filter] = {}


def register(fid: str, name: str, category: str, *, summary: str, use: str, method: str, caveats: str,
             params: tuple[Param, ...] = (), temporal: bool = False):
    def deco(fn):
        FILTERS[fid] = Filter(fid, name, category, " ".join(summary.split()), " ".join(use.split()),
                              " ".join(method.split()), " ".join(caveats.split()), params, fn, temporal)
        return fn
    return deco


def catalogue() -> list[dict]:
    """All filters, grouped and ordered by category, for the page and the report appendix."""
    order = {c: i for i, (c, _) in enumerate(CATEGORIES)}
    return [f.public() for f in sorted(FILTERS.values(), key=lambda f: order[f.category])]


# ------------------------------------------------------------------ helpers
def luminance(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def gray3(gray: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(gray.astype(np.float32), cv2.COLOR_GRAY2BGR)


def to_u8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)


def from_u8(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float32) / 255.0


def _box(x: np.ndarray, r: int) -> np.ndarray:
    return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1), borderType=cv2.BORDER_REFLECT)


def guided_filter(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """Edge-preserving smoothing (He, Sun & Tang 2010): smooths `src` without blurring across edges in `guide`."""
    mean_i, mean_p = _box(guide, radius), _box(src, radius)
    cov = _box(guide * src, radius) - mean_i * mean_p
    var = _box(guide * guide, radius) - mean_i * mean_i
    a = cov / (var + eps)
    b = mean_p - a * mean_i
    return _box(a, radius) * guide + _box(b, radius)


def _check_size(w: float, h: float) -> tuple[int, int]:
    w, h = int(round(w)), int(round(h))
    if w < 1 or h < 1:
        raise FilterError("The output would be empty — check the points or sizes")
    if w * h > MAX_OUTPUT_PIXELS:
        raise FilterError(f"The output would be {w}×{h} pixels, too large to process")
    return w, h


def _psf2otf(psf: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    padded = np.zeros(shape, np.float32)
    ph, pw = psf.shape
    padded[:ph, :pw] = psf
    padded = np.roll(padded, (-(ph // 2), -(pw // 2)), axis=(0, 1))  # centre the PSF on the origin
    return np.fft.rfft2(padded)


def deconvolve(img: np.ndarray, psf: np.ndarray, method: str, noise: float, iterations: int) -> np.ndarray:
    """Undo a known blur. Wiener: one division in the frequency domain. Richardson–Lucy: iterative, fewer ringing artefacts."""
    psf = psf / psf.sum()
    pad = max(psf.shape) * 2
    padded = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_REFLECT)
    if method == "wiener":
        otf = _psf2otf(psf, padded.shape[:2])
        kernel = np.conj(otf) / (np.abs(otf) ** 2 + noise)
        out = np.empty_like(padded)
        for c in range(padded.shape[2]):
            out[..., c] = np.fft.irfft2(np.fft.rfft2(padded[..., c]) * kernel, s=padded.shape[:2])
    else:
        flipped = psf[::-1, ::-1]
        est = padded.copy()
        for _ in range(iterations):
            blurred = cv2.filter2D(est, -1, flipped, borderType=cv2.BORDER_REFLECT)
            ratio = padded / np.maximum(blurred, 1e-4)
            est = est * cv2.filter2D(ratio, -1, psf, borderType=cv2.BORDER_REFLECT)
            # damping: a little noise-dependent smoothing keeps noise from being amplified each pass
            if noise > 1e-3:
                est = cv2.GaussianBlur(est, (0, 0), min(1.0, noise * 20))
        out = est
    return out[pad:-pad, pad:-pad]


def motion_psf(length: float, angle: float) -> np.ndarray:
    size = int(math.ceil(length)) | 1
    size = max(size, 3)
    ss = 8  # draw at 8x and shrink so short or diagonal lines are anti-aliased
    canvas = np.zeros((size * ss, size * ss), np.float32)
    c = (size * ss - 1) / 2
    dx, dy = math.cos(math.radians(angle)) * (length * ss - ss) / 2, -math.sin(math.radians(angle)) * (length * ss - ss) / 2
    cv2.line(canvas, (int(round(c - dx)), int(round(c - dy))), (int(round(c + dx)), int(round(c + dy))), 1.0, ss)
    psf = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    return psf / psf.sum()


def defocus_psf(radius: float, shape: str) -> np.ndarray:
    size = int(math.ceil(radius * 3 if shape == "gaussian" else radius)) * 2 + 1
    y, x = np.mgrid[:size, :size] - size // 2
    if shape == "gaussian":
        psf = np.exp(-(x ** 2 + y ** 2) / (2 * max(radius, 0.3) ** 2))
    else:
        ss = 8
        yy, xx = (np.mgrid[: size * ss, : size * ss] - (size * ss - 1) / 2) / ss
        disk = (xx ** 2 + yy ** 2 <= radius ** 2).astype(np.float32)
        psf = cv2.resize(disk, (size, size), interpolation=cv2.INTER_AREA)
    return (psf / psf.sum()).astype(np.float32)


DECONV_PARAMS = (
    Param("method", "Method", "choice", "wiener", choices=(("wiener", "Wiener filter"), ("lucy", "Richardson–Lucy (iterative)")),
          help="Wiener is instant and good for low-noise images. Richardson–Lucy rings less but is slower."),
    Param("noise", "Noise level", "float", 0.01, 0.0001, 0.2, 0.0001,
          help="Noise-to-signal ratio. Raise it if the result looks grainy or ripples; lower it for a crisper result. Richardson–Lucy also damps noise between passes by this amount."),
    Param("iterations", "Iterations", "int", 60, 1, 400, 1, help="Richardson–Lucy only: more passes recover more detail and more noise."),
)


# ------------------------------------------------------------------ levels & exposure
CHANNELS = (("all", "All channels"), ("r", "Red"), ("g", "Green"), ("b", "Blue"))
CHANNEL_INDEX = {"b": 0, "g": 1, "r": 2}


@register(
    "levels", "Levels", "adjust",
    summary="""Remaps the brightness range: chosen input black and white points become pure black and white,
        and a gamma value brightens or darkens the mid-tones.""",
    use="""Improve visibility in a dark, washed-out or low-contrast image, e.g. to bring up a face in shadow or
        separate characters from a dirty background. The most common first step in an enhancement.""",
    method="""out = out_black + (out_white − out_black) × clip((in − in_black)/(in_white − in_black), 0, 1)^(1/gamma),
        applied per pixel, to all channels or one. Values are on a 0–255 scale regardless of the source bit depth.""",
    caveats="""Values outside the input range are clipped to black or white, which discards that detail. It
        cannot add information that isn't in the recording; it only redistributes the existing tones.""",
    params=(
        Param("in_black", "Input black", "float", 0, 0, 254, 1, help="Pixels at or below this become black."),
        Param("in_white", "Input white", "float", 255, 1, 255, 1, help="Pixels at or above this become white."),
        Param("gamma", "Gamma (mid-tones)", "float", 1.0, 0.1, 5.0, 0.01, help="Above 1 brightens mid-tones; below 1 darkens them."),
        Param("out_black", "Output black", "float", 0, 0, 255, 1),
        Param("out_white", "Output white", "float", 255, 0, 255, 1),
        Param("channel", "Channel", "choice", "all", choices=CHANNELS),
    ),
)
def levels(img, p, ctx):
    lo, hi = p["in_black"] / 255, max(p["in_white"], p["in_black"] + 1) / 255
    ob, ow = p["out_black"] / 255, p["out_white"] / 255
    out = img.copy()
    sel = (slice(None), slice(None), CHANNEL_INDEX[p["channel"]]) if p["channel"] != "all" else (Ellipsis,)
    x = np.clip((img[sel] - lo) / (hi - lo), 0, 1) ** (1 / p["gamma"])
    out[sel] = ob + (ow - ob) * x
    return out


@register(
    "auto_levels", "Auto contrast stretch", "adjust",
    summary="""Finds the darkest and brightest values in the image (ignoring a small percentage of outliers)
        and stretches them to fill the full black-to-white range.""",
    use="""A quick, objective starting point for faded, foggy or underexposed footage. With "per channel" on it
        also removes a uniform colour cast.""",
    method="""Black and white points are the given low and high percentiles of the luminance (or of each channel
        separately); the image is then linearly rescaled so those points map to 0 and 1.""",
    caveats="""The clip percentages are deliberately discarded at each end. Per-channel stretching changes colour
        balance, so don't use it where colour itself is in question (e.g. vehicle colour).""",
    params=(
        Param("low", "Clip shadows", "float", 0.5, 0, 20, 0.1, unit="%"),
        Param("high", "Clip highlights", "float", 0.5, 0, 20, 0.1, unit="%"),
        Param("per_channel", "Per channel (removes colour cast)", "bool", False),
    ),
)
def auto_levels(img, p, ctx):
    hi_pct = 100 - p["high"]
    if p["per_channel"]:
        out = np.empty_like(img)
        for c in range(3):
            lo, hi = np.percentile(img[..., c], (p["low"], hi_pct))
            out[..., c] = (img[..., c] - lo) / max(hi - lo, 1e-6)
        ctx.note("stretched each channel separately")
        return out
    lo, hi = np.percentile(luminance(img), (p["low"], hi_pct))
    ctx.note(f"input range {lo * 255:.1f}–{hi * 255:.1f} stretched to 0–255")
    return (img - lo) / max(hi - lo, 1e-6)


@register(
    "brightness_contrast", "Brightness & contrast", "adjust",
    summary="Shifts the overall brightness and expands or compresses contrast around mid-grey.",
    use="Simple global correction for footage that is too dark, too bright, or flat.",
    method="""out = (in − 0.5) × 2^(contrast/50) + 0.5 + brightness/200. Contrast 0 and brightness 0 leave the
        image unchanged; contrast ±50 doubles or halves the tonal spread.""",
    caveats="Strong settings clip shadows or highlights to pure black or white, losing that detail.",
    params=(
        Param("brightness", "Brightness", "float", 0, -100, 100, 1),
        Param("contrast", "Contrast", "float", 0, -100, 100, 1),
    ),
)
def brightness_contrast(img, p, ctx):
    return (img - 0.5) * 2 ** (p["contrast"] / 50) + 0.5 + p["brightness"] / 200


@register(
    "exposure", "Exposure & gamma", "adjust",
    summary="""Brightens a dark image as if it had been exposed for longer (in photographic stops), with an
        optional highlight roll-off so bright areas such as lamps don't burn out.""",
    use="""Increase the exposure of dark images: night-time CCTV, an underexposed photo, or a frame where the
        subject is lost in shadow.""",
    method="""Linear gain of 2^EV, then a gamma curve out = in^(1/gamma). With highlight roll-off the gain is
        applied through a Reinhard tone curve, y = x(1 + x/w²)/(1 + x), with w the gained white level, which
        compresses values approaching white instead of clipping them.""",
    caveats="""Brightening also amplifies sensor noise and compression blocks in dark areas; combine with frame
        integration or denoising. Detail that was recorded as pure black cannot be recovered.""",
    params=(
        Param("ev", "Exposure", "float", 1.0, -4, 6, 0.05, unit="EV", help="Each +1 doubles the brightness."),
        Param("gamma", "Gamma", "float", 1.0, 0.2, 5, 0.01),
        Param("rolloff", "Highlight roll-off", "bool", True),
    ),
)
def exposure(img, p, ctx):
    gain = 2 ** p["ev"]
    x = img * gain
    if p["rolloff"] and gain > 1:
        x = x * (1 + x / (gain * gain)) / (1 + x)  # Reinhard: in = 1 (x = gain) still maps to 1
    return np.clip(x, 0, 1) ** (1 / p["gamma"])


@register(
    "shadows_highlights", "Shadows & highlights", "adjust",
    summary="""Lifts dark regions and tames bright ones locally, so a subject in shadow becomes visible
        without the bright background blowing out.""",
    use="""Enhance a backlit image: a person in front of a window or a bright sky, a face under a cap or hood,
        a car interior seen from outside, or headlights hiding a number plate.""",
    method="""The luminance is split into a smooth base layer (edge-preserving guided filter with the given
        radius) and detail. A gain is computed from the base layer, 1 + 3·shadows·(1 − base)² ×
        (1 − 0.7·highlights·base²), and every pixel's colour is multiplied by it, so local detail and colour
        ratios are kept while the local brightness is equalised.""",
    caveats="""Large amounts can create soft halos at strong edges and make an image look flat. Brightened
        shadows bring up noise. Relative brightness between areas is no longer faithful.""",
    params=(
        Param("shadows", "Shadows", "float", 50, 0, 100, 1, unit="%"),
        Param("highlights", "Highlights", "float", 0, 0, 100, 1, unit="%"),
        Param("radius", "Radius", "int", 30, 2, 300, 1, unit="px", help="Size of the regions treated as one area."),
    ),
)
def shadows_highlights(img, p, ctx):
    lum = luminance(img)
    base = np.clip(guided_filter(lum, lum, p["radius"], 0.01), 0, 1)
    gain = (1 + 3 * p["shadows"] / 100 * (1 - base) ** 2) * (1 - 0.7 * p["highlights"] / 100 * base ** 2)
    return img * gain[..., None]


@register(
    "clahe", "Histogram equalisation", "adjust",
    summary="""Spreads out the most frequent brightness values so that low-contrast detail stands out. The
        adaptive mode works region by region, bringing out detail in both dark and bright areas at once.""",
    use="""Enhance details in poor-contrast images: faded text, a fingerprint or shoe mark on a patterned
        surface, a number plate in fog, or texture in a flat grey area.""",
    method="""Applied to the L* (lightness) channel of CIE L*a*b*, so colours don't shift. Global: the
        cumulative histogram becomes the tone curve. Adaptive (CLAHE, Zuiderveld 1994): the same per tile
        on a grid (256-level histograms), with each tile's histogram clipped at the clip limit times the
        average bin count to limit noise amplification, and the tile curves bilinearly blended.""",
    caveats="""Exaggerates noise and compression artefacts in flat areas, and the tone curve is image dependent,
        so brightness can no longer be compared between frames or areas.""",
    params=(
        Param("mode", "Mode", "choice", "adaptive", choices=(("adaptive", "Adaptive (CLAHE)"), ("global", "Global"))),
        Param("clip", "Clip limit", "float", 2.0, 0.5, 20, 0.1, help="Adaptive only: higher gives more contrast and more noise."),
        Param("tiles", "Tiles", "int", 8, 2, 32, 1, help="Adaptive only: grid size across the image."),
    ),
)
def clahe(img, p, ctx):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)
    lightness = np.clip(lab[..., 0] / 100, 0, 1)
    if p["mode"] == "adaptive":
        # OpenCV scales the clip limit by the number of bins, so 16-bit input all but disables it; equalise on 256
        # levels and add back the part of each value below one level, so no precision is lost
        u8 = np.clip(lightness * 255, 0, 255).astype(np.uint8)
        residual = lightness - u8 / 255.0
        eq = cv2.createCLAHE(p["clip"], (p["tiles"], p["tiles"])).apply(u8).astype(np.float32) / 255 + residual
    else:
        hist, edges = np.histogram(lightness, 4096, (0, 1))
        cdf = np.cumsum(hist).astype(np.float64)
        cdf = (cdf - cdf[0]) / max(cdf[-1] - cdf[0], 1)
        eq = np.interp(lightness, edges[1:], cdf).astype(np.float32)
    lab = lab.copy()
    lab[..., 0] = eq * 100
    return cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)


# ------------------------------------------------------------------ colour & channels
@register(
    "white_balance", "White balance", "colour",
    summary="Removes a colour cast so that neutral surfaces (grey, white) appear neutral.",
    use="""Correct the orange cast of sodium street lights, the green of fluorescent lighting, or a camera with a
        wrong white balance, so that clothing and vehicle colours can be described more reliably.""",
    method="""Each channel is multiplied by a gain. Grey world: gains make the three channel means equal.
        White patch: gains make the 99th-percentile of each channel equal. Neutral point: gains make the 5×5
        average around a picked pixel grey. Manual: temperature and tint gains.""",
    caveats="""Under narrow-band lighting (low-pressure sodium, infrared) the original colours were never
        recorded and no correction can restore them. Always state the original colour appearance too.""",
    params=(
        Param("mode", "Method", "choice", "grayworld", choices=(
            ("grayworld", "Grey world"), ("whitepatch", "White patch"), ("point", "Neutral point (pick)"),
            ("manual", "Manual"))),
        Param("point", "Neutral point", "points", [], count=1, labels=("neutral",), help="Neutral point mode: click a surface that should be grey or white."),
        Param("temperature", "Temperature", "float", 0, -100, 100, 1, help="Manual: negative is cooler (bluer), positive warmer."),
        Param("tint", "Tint", "float", 0, -100, 100, 1, help="Manual: negative is greener, positive more magenta."),
    ),
)
def white_balance(img, p, ctx):
    mode = p["mode"]
    if mode == "grayworld":
        means = img.reshape(-1, 3).mean(0)
    elif mode == "whitepatch":
        means = np.percentile(img.reshape(-1, 3), 99, axis=0)
    elif mode == "point":
        if not p["point"]:
            ctx.note("no neutral point picked: unchanged")
            return img
        x, y = (int(round(v)) for v in p["point"][0])
        h, w = img.shape[:2]
        patch = img[max(0, y - 2): min(h, y + 3), max(0, x - 2): min(w, x + 3)]
        if not patch.size:
            ctx.note("neutral point is outside the image: unchanged")
            return img
        means = patch.reshape(-1, 3).mean(0)
    else:
        t, n = p["temperature"] / 100, p["tint"] / 100
        gains = np.array([1 - 0.3 * t, 1 - 0.3 * n, 1 + 0.3 * t], np.float32)  # B, G, R
        return img * gains
    means = np.maximum(means, 1e-4)
    gains = means.mean() / means
    ctx.note(f"gains R {gains[2]:.2f} · G {gains[1]:.2f} · B {gains[0]:.2f}")
    return img * gains.astype(np.float32)


CHANNEL_CHOICES = (
    ("luma", "Luminance (greyscale)"), ("r", "Red"), ("g", "Green"), ("b", "Blue"), ("h", "Hue"), ("s", "Saturation"),
    ("v", "Value (HSV)"), ("L", "L* lightness"), ("a", "a* green–red"), ("bb", "b* blue–yellow"),
)


@register(
    "channel", "Channel select", "colour",
    summary="Shows a single colour channel or colour-space component as a greyscale image.",
    use="""Often one channel carries the most contrast: e.g. the blue channel for a yellow plate, red for a mark
        on green paper, saturation to separate coloured ink from grey paper. Greyscale also removes misleading
        colour noise from night footage.""",
    method="""Luminance is Y = 0.299R + 0.587G + 0.114B (ITU-R BT.601). HSV and CIE L*a*b* components are
        computed with OpenCV and rescaled to 0–1 (hue 0–360°, a* and b* −128…127).""",
    caveats="All colour information is discarded in the output.",
    params=(Param("channel", "Channel", "choice", "luma", choices=CHANNEL_CHOICES),),
)
def channel(img, p, ctx):
    c = p["channel"]
    if c == "luma":
        out = luminance(img)
    elif c in CHANNEL_INDEX:
        out = img[..., CHANNEL_INDEX[c]]
    elif c in ("h", "s", "v"):
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        out = hsv[..., "hsv".index(c)] / (360.0 if c == "h" else 1.0)
    else:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)
        out = lab[..., 0] / 100 if c == "L" else (lab[..., 1 if c == "a" else 2] + 128) / 255
    return gray3(out)


@register(
    "invert", "Invert", "colour",
    summary="Turns the image into its negative: black becomes white and vice versa.",
    use="""Fingerprints and other marks developed dark-on-light or light-on-dark are compared in a standard
        polarity; the eye also sees some faint detail better in the negative.""",
    method="out = 1 − in for every channel.",
    caveats="None: it is exactly reversible.",
)
def invert(img, p, ctx):
    return 1 - img


@register(
    "saturation", "Saturation", "colour",
    summary="Strengthens or weakens colour without changing brightness.",
    use="Make a faint colour difference visible (e.g. a coloured stripe on a vehicle), or remove colour noise.",
    method="""In CIE L*a*b*, the a* and b* (colour) components are multiplied by the factor; L* (lightness) is
        unchanged.""",
    caveats="Boosted colours are not the true colours of the object; use for visibility only.",
    params=(Param("amount", "Saturation", "float", 150, 0, 400, 1, unit="%"),),
)
def saturation(img, p, ctx):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)
    lab[..., 1:] *= p["amount"] / 100
    return cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)


# ------------------------------------------------------------------ sharpen & deblur
@register(
    "unsharp", "Unsharp mask", "sharpen",
    summary="Sharpens edges by adding back the difference between the image and a blurred copy of it.",
    use="Make slightly soft detail crisper, e.g. text, edges of a vehicle, or after interpolation or deblurring.",
    method="""out = in + amount × (in − Gaussian(in, radius)), applied only where the difference exceeds the
        threshold (in 0–255 units) so that flat, noisy areas are left alone.""",
    caveats="""Sharpening does not add detail; it boosts edge contrast. Too much creates halos (light/dark
        outlines) which can be mistaken for real edges.""",
    params=(
        Param("radius", "Radius", "float", 1.5, 0.3, 30, 0.1, unit="px"),
        Param("amount", "Amount", "float", 100, 0, 500, 1, unit="%"),
        Param("threshold", "Threshold", "float", 0, 0, 64, 1),
    ),
)
def unsharp(img, p, ctx):
    blurred = cv2.GaussianBlur(img, (0, 0), p["radius"])
    diff = img - blurred
    if p["threshold"] > 0:
        diff = np.where(np.abs(diff) * 255 >= p["threshold"], diff, 0)
    return img + p["amount"] / 100 * diff


@register(
    "motion_deblur", "Motion deblur", "sharpen",
    summary="""Reverses a straight-line motion blur of a given length and direction, as caused by a moving
        vehicle or a camera moving during the exposure.""",
    use="""Deblur a moving car or its number plate, or a photo blurred by camera shake. Measure the length and
        angle of the smear on a small bright point or a sharp edge, then fine-tune until edges look crisp.""",
    method="""Deconvolution with a linear point-spread function (PSF) of the given length (pixels) and angle
        (degrees anticlockwise from horizontal). Wiener: F = G·H*/(|H|² + K) in the frequency domain with K the
        noise level. Richardson–Lucy: iterative maximum-likelihood estimation. Edges are reflected to limit
        ringing.""",
    caveats="""Only valid when the blur is uniform across the region and close to a straight line. A wrong
        length or angle creates ripples and false edges. Results can show ringing near strong edges. Crop to the
        region of interest first when the blur differs across the frame.""",
    params=(
        Param("length", "Blur length", "float", 9, 1, 150, 0.5, unit="px"),
        Param("angle", "Blur angle", "float", 0, 0, 180, 0.5, unit="°"),
        *DECONV_PARAMS,
    ),
)
def motion_deblur(img, p, ctx):
    if p["length"] < 1.5:
        return img
    return deconvolve(img, motion_psf(p["length"], p["angle"]), p["method"], p["noise"], p["iterations"])


@register(
    "defocus_deblur", "Optical deblur", "sharpen",
    summary="""Reverses blur caused by the lens being out of focus (disc) or by atmospheric or optical softness
        (Gaussian).""",
    use="""Deblur a picture to read text, a plate or a logo that is out of focus. Increase the radius until edges
        are sharpest just before ripples appear.""",
    method="""Deconvolution (Wiener or Richardson–Lucy, as for motion deblur) with a uniform disc PSF of the given
        radius, the model of an out-of-focus lens, or a Gaussian PSF with that standard deviation.""",
    caveats="""Amplifies noise; raise the noise level for noisy or heavily compressed sources. The PSF is assumed
        to be the same over the whole image.""",
    params=(
        Param("shape", "Blur type", "choice", "disk", choices=(("disk", "Out of focus (disc)"), ("gaussian", "Gaussian"))),
        Param("radius", "Radius", "float", 3, 0.5, 40, 0.1, unit="px"),
        *DECONV_PARAMS,
    ),
)
def defocus_deblur(img, p, ctx):
    return deconvolve(img, defocus_psf(p["radius"], p["shape"]), p["method"], p["noise"], p["iterations"])


# ------------------------------------------------------------------ denoise & frequency
@register(
    "gaussian_blur", "Gaussian blur", "denoise",
    summary="Smooths the image by averaging each pixel with its neighbours, weighted by distance.",
    use="Suppress fine grain or JPEG blocking before another step, or soften a pattern to see larger shapes.",
    method="Convolution with a 2-D Gaussian kernel of the given standard deviation (sigma).",
    caveats="Blurs real detail and edges along with the noise.",
    params=(Param("sigma", "Sigma", "float", 1.0, 0.1, 50, 0.1, unit="px"),),
)
def gaussian_blur(img, p, ctx):
    return cv2.GaussianBlur(img, (0, 0), p["sigma"])


@register(
    "median", "Median filter", "denoise",
    summary="Replaces each pixel with the median of its neighbourhood, removing isolated specks while keeping edges.",
    use="Remove salt-and-pepper noise, dust, dead pixels, or the speckle of a poor analogue recording.",
    method="Each output pixel is the median of the size×size square around it, per channel.",
    caveats="Removes fine lines and small text strokes narrower than about half the window.",
    params=(Param("size", "Window size", "int", 3, 3, 15, 2, unit="px"),),
)
def median(img, p, ctx):
    k = p["size"] | 1
    if k <= 5:
        return cv2.medianBlur(img, k)
    return from_u8(cv2.medianBlur(to_u8(img), k))


@register(
    "bilateral", "Bilateral filter", "denoise",
    summary="Edge-preserving smoothing: averages neighbouring pixels only when their colour is similar.",
    use="Reduce grain on skin, walls and car bodies while keeping outlines and text edges.",
    method="""Each pixel is a weighted average of the neighbourhood, weights being a spatial Gaussian (sigma space)
        times a Gaussian of the colour difference (sigma colour, in 0–255 units) (Tomasi & Manduchi 1998).""",
    caveats="Strong settings give a flat, painted look and remove low-contrast texture.",
    params=(
        Param("sigma_color", "Sigma colour", "float", 25, 1, 150, 1),
        Param("sigma_space", "Sigma space", "float", 5, 1, 30, 0.5, unit="px"),
    ),
)
def bilateral(img, p, ctx):
    d = int(p["sigma_space"] * 2) | 1
    return cv2.bilateralFilter(img, d, p["sigma_color"] / 255, p["sigma_space"])


@register(
    "nlmeans", "Non-local means denoise", "denoise",
    summary="""Strong denoising that averages patches that look alike anywhere nearby, so texture and edges
        survive better than with simple blurring.""",
    use="Clean up heavy sensor noise in low-light stills or single CCTV frames when only one frame is available.",
    method="""Non-local means (Buades, Coll & Morel 2005), OpenCV's fastNlMeansDenoisingColored: 7×7 patches
        compared within a 21×21 search window, filtering strength h for luminance and colour.""",
    caveats="""Can wipe out fine low-contrast detail and invent smooth texture; works on an 8-bit copy. With
        video, prefer frame integration, which uses genuinely independent information.""",
    params=(
        Param("strength", "Strength (h)", "float", 8, 1, 40, 0.5),
        Param("color_strength", "Colour strength", "float", 8, 0, 40, 0.5),
    ),
)
def nlmeans(img, p, ctx):
    return from_u8(cv2.fastNlMeansDenoisingColored(to_u8(img), None, p["strength"], p["color_strength"], 7, 21))


def _spectrum_coords(h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    fy = np.fft.fftfreq(h)[:, None] * 2  # in units of the Nyquist frequency, −1..1
    fx = np.fft.rfftfreq(w)[None, :] * 2
    return fy, fx


@register(
    "periodic_noise", "Periodic noise removal", "denoise",
    summary="""Finds regular, repeating interference patterns (stripes, grids, moiré, mains hum bars) as bright
        spikes in the frequency spectrum and removes them with notch filters.""",
    use="""Remove periodic noise from an image: interference lines on analogue CCTV, a halftone or screen pattern
        on a photographed document, fabric or banknote texture behind a mark, or scanning moiré.""",
    method="""The 2-D Fourier transform of the luminance is compared to a smoothed version of itself; local peaks
        that stand out by more than the sensitivity threshold, outside the protected low-frequency radius, are
        treated as interference. A Gaussian notch of the given width is placed on each peak (and its mirror) and
        applied to all three channels before transforming back.""",
    caveats="""Real repeating structure in the scene (railings, brickwork, text lines) produces peaks too and can be
        weakened. Increase the protected radius or lower the sensitivity if wanted detail disappears.""",
    params=(
        Param("sensitivity", "Sensitivity", "float", 5, 1, 10, 0.1, help="Higher finds weaker patterns."),
        Param("protect", "Protected radius", "float", 4, 0.5, 40, 0.5, unit="%", help="Percentage of the Nyquist frequency. Low frequencies (overall shapes) inside it are never touched."),
        Param("width", "Notch width", "float", 3, 0.5, 20, 0.5, unit="px"),
        Param("max_peaks", "Maximum peaks", "int", 40, 1, 200, 1),
    ),
)
def periodic_noise(img, p, ctx):
    h, w = img.shape[:2]
    lum = luminance(img)
    spec = np.fft.rfft2(lum - lum.mean())
    mag = np.log1p(np.abs(spec)).astype(np.float32)
    bg = cv2.blur(cv2.medianBlur(mag, 5), (15, 15))
    excess = mag - bg
    fy, fx = _spectrum_coords(h, w)
    radius = np.hypot(fy, fx)
    threshold = 3.2 - 0.25 * p["sensitivity"]  # log-magnitude above the local background
    local_max = excess >= cv2.dilate(excess, np.ones((5, 5), np.uint8))
    cand = (excess > threshold) & local_max & (radius > p["protect"] / 100)
    ys, xs = np.nonzero(cand)
    order = np.argsort(excess[ys, xs])[::-1][: p["max_peaks"]]
    if not len(order):
        ctx.note("no periodic pattern found")
        return img
    # positions in cycles-per-pixel space, so notches are round whatever the image aspect ratio
    ky = np.fft.fftfreq(h)[:, None] * h
    kx = np.fft.rfftfreq(w)[None, :] * w
    mask = np.ones(spec.shape, np.float32)
    reach = int(math.ceil(4 * p["width"]))  # the notch is negligible beyond 4 widths, so only touch that patch
    for i in order:
        cy, cx = ys[i], xs[i]
        rows = np.arange(cy - reach, cy + reach + 1) % h  # the spectrum wraps around vertically
        cols = np.arange(max(0, cx - reach), min(spec.shape[1], cx + reach + 1))
        py, px = ky[cy, 0], kx[0, cx]
        dy = (ky[rows, 0] - py + h / 2) % h - h / 2
        d2 = dy[:, None] ** 2 + (kx[0, cols] - px)[None, :] ** 2
        mask[np.ix_(rows, cols)] *= 1 - np.exp(-d2 / (2 * p["width"] ** 2)).astype(np.float32)
        if px < reach:
            # rfft keeps only kx ≥ 0; a peak near kx = 0 also has its mirror image (−ky, −kx) partly in this half
            mrows = np.arange(-cy - reach, -cy + reach + 1) % h
            dy = (ky[mrows, 0] + py + h / 2) % h - h / 2
            d2 = dy[:, None] ** 2 + (kx[0, cols] + px)[None, :] ** 2
            mask[np.ix_(mrows, cols)] *= 1 - np.exp(-d2 / (2 * p["width"] ** 2)).astype(np.float32)
    out = np.empty_like(img)
    for c in range(3):
        out[..., c] = np.fft.irfft2(np.fft.rfft2(img[..., c]) * mask, s=(h, w))
    ctx.note(f"{len(order)} interference peak{'s' if len(order) != 1 else ''} removed")
    return out


@register(
    "fft_filter", "Frequency filter", "denoise",
    summary="""Keeps or removes detail of a chosen size range by filtering in the frequency domain: low-pass
        smooths, high-pass keeps only fine detail, band-pass keeps one size range.""",
    use="""Separate a fingerprint from the background: a band-pass tuned to ridge spacing suppresses both
        large-scale shading and fine paper texture. High-pass flattens uneven illumination.""",
    method="""Butterworth filter (order 2) on the 2-D Fourier transform of each channel. Cut-offs are fractions of
        the Nyquist frequency (1.0 = one cycle every 2 pixels). The image mean (DC) is always kept, so the
        overall brightness is preserved.""",
    caveats="Hard cut-offs can produce faint ripples near strong edges.",
    params=(
        Param("mode", "Type", "choice", "bandpass", choices=(
            ("lowpass", "Low-pass (smooth)"), ("highpass", "High-pass (fine detail)"),
            ("bandpass", "Band-pass"), ("bandstop", "Band-stop"))),
        Param("low", "Low cut-off", "float", 0.02, 0.001, 1, 0.001, help="High-pass, band-pass and band-stop."),
        Param("high", "High cut-off", "float", 0.35, 0.001, 1, 0.001, help="Low-pass, band-pass and band-stop."),
        Param("gain", "Detail gain", "float", 1.0, 0.2, 8, 0.1, help="Multiplies the kept detail around the mean."),
    ),
)
def fft_filter(img, p, ctx):
    h, w = img.shape[:2]
    fy, fx = _spectrum_coords(h, w)
    r = np.hypot(fy, fx) + 1e-9
    lowpass = lambda c: 1 / (1 + (r / c) ** 4)  # noqa: E731  Butterworth order 2
    highpass = lambda c: 1 - lowpass(c)  # noqa: E731
    mode = p["mode"]
    if mode == "lowpass":
        resp = lowpass(p["high"])
    elif mode == "highpass":
        resp = highpass(p["low"])
    elif mode == "bandpass":
        resp = highpass(p["low"]) * lowpass(max(p["high"], p["low"]))
    else:
        resp = 1 - highpass(p["low"]) * lowpass(max(p["high"], p["low"]))
    resp = (resp * p["gain"]).astype(np.float32)
    resp[0, 0] = 1.0
    out = np.empty_like(img)
    for c in range(3):
        out[..., c] = np.fft.irfft2(np.fft.rfft2(img[..., c]) * resp, s=(h, w))
    return out


@register(
    "flatten", "Background flatten", "denoise",
    summary="""Removes uneven lighting or a slowly varying background by subtracting or dividing out a heavily
        blurred copy of the image.""",
    use="""Separate a fingerprint, shoe mark or writing from shading, a vignette, or a curved surface; even out a
        document photographed under a lamp.""",
    method="""The background is estimated with a Gaussian blur of the given radius. Subtract: out = in − bg + mean.
        Divide (flat-field): out = in / bg × mean. Optionally followed by a 0.5 % contrast stretch.""",
    caveats="""Objects larger than the radius are treated as background and faded. Relative brightness between
        areas is no longer meaningful.""",
    params=(
        Param("radius", "Radius", "float", 40, 3, 400, 1, unit="px"),
        Param("mode", "Mode", "choice", "divide", choices=(("divide", "Divide (flat-field)"), ("subtract", "Subtract"))),
        Param("stretch", "Stretch contrast afterwards", "bool", True),
    ),
)
def flatten(img, p, ctx):
    bg = cv2.GaussianBlur(img, (0, 0), p["radius"])
    mean = img.reshape(-1, 3).mean(0)
    out = img / np.maximum(bg, 1e-3) * mean if p["mode"] == "divide" else img - bg + mean
    if p["stretch"]:
        lo, hi = np.percentile(out, (0.5, 99.5))
        out = (out - lo) / max(hi - lo, 1e-6)
    return out


# ------------------------------------------------------------------ geometry & perspective
@register(
    "crop", "Crop", "geometry",
    summary="Keeps only a rectangular region of interest.",
    use="""Focus on the subject; also makes region-dependent filters (deblur, equalisation, auto levels) work on
        the area that matters instead of the whole frame.""",
    method="Pixels inside the rectangle between the two picked corners are copied unchanged.",
    caveats="None to the kept pixels. State in the report what was cropped away.",
    params=(Param("corners", "Corners", "points", [], count=2, labels=("corner", "opposite corner")),),
)
def crop(img, p, ctx):
    if not p["corners"]:
        ctx.note("no region picked: unchanged")
        return img
    (x0, y0), (x1, y1) = p["corners"]
    h, w = img.shape[:2]
    x0, x1 = sorted((int(round(max(0, min(w, x0)))), int(round(max(0, min(w, x1))))))
    y0, y1 = sorted((int(round(max(0, min(h, y0)))), int(round(max(0, min(h, y1))))))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise FilterError("The crop region is too small")
    ctx.note(f"{x1 - x0}×{y1 - y0} px at ({x0}, {y0})")
    return img[y0:y1, x0:x1].copy()


@register(
    "rotate", "Rotate & flip", "geometry",
    summary="Rotates the image by any angle and/or mirrors it.",
    use="""Level a tilted camera, turn text or a plate horizontal for reading, or undo a mirrored recording (some
        cameras and reversing cameras record mirrored).""",
    method="""Rotation about the centre with the chosen interpolation; with "expand" the canvas grows so no corner is
        cut off (new areas are black). Flips are exact pixel reorderings.""",
    caveats="Rotation by angles other than multiples of 90° resamples (interpolates) every pixel.",
    params=(
        Param("angle", "Angle", "float", 0, -180, 180, 0.1, unit="°", help="Positive is anticlockwise."),
        Param("flip", "Flip", "choice", "none", choices=(("none", "None"), ("h", "Horizontal (mirror)"), ("v", "Vertical"), ("both", "Both"))),
        Param("expand", "Expand canvas", "bool", True),
        Param("interpolation", "Interpolation", "choice", "cubic", choices=INTERPOLATIONS),
    ),
)
def rotate(img, p, ctx):
    out = img
    if p["flip"] != "none":
        out = cv2.flip(out, {"h": 1, "v": 0, "both": -1}[p["flip"]])
    a = p["angle"] % 360
    if a in (90, 180, 270):
        return cv2.rotate(out, {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_CLOCKWISE}[a])
    if a == 0:
        return out.copy() if out is img else out
    h, w = out.shape[:2]
    m = cv2.getRotationMatrix2D(((w - 1) / 2, (h - 1) / 2), p["angle"], 1.0)
    nw, nh = w, h
    if p["expand"]:
        c, s = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = _check_size(h * s + w * c, h * c + w * s)
        m[0, 2] += (nw - w) / 2
        m[1, 2] += (nh - h) / 2
    return cv2.warpAffine(out, m, (nw, nh), flags=INTER[p["interpolation"]])


@register(
    "resize", "Resize", "geometry",
    summary="Enlarges or reduces the image by a scale factor using the chosen interpolation.",
    use="""Enlarge a small region (a plate or face) for display. Nearest neighbour shows the true recorded pixels as
        blocks; bicubic/Lanczos give a smoother view that is easier to read.""",
    method="""Resampling with OpenCV's nearest, bilinear, bicubic (4×4) or Lanczos (8×8) kernels; reductions use
        area averaging to avoid aliasing.""",
    caveats="""Interpolation never adds information: smooth enlargement only estimates values between real pixels.
        For a resolution gain from video use multi-frame super-resolution.""",
    params=(
        Param("scale", "Scale", "float", 2.0, 0.1, 8, 0.05, unit="×"),
        Param("interpolation", "Interpolation", "choice", "cubic", choices=INTERPOLATIONS),
    ),
)
def resize(img, p, ctx):
    h, w = img.shape[:2]
    nw, nh = _check_size(w * p["scale"], h * p["scale"])
    flags = cv2.INTER_AREA if p["scale"] < 1 and p["interpolation"] != "nearest" else INTER[p["interpolation"]]
    return cv2.resize(img, (nw, nh), interpolation=flags)


ASPECTS = (("4:3", "4:3"), ("16:9", "16:9"), ("5:4", "5:4"), ("3:2", "3:2"), ("1:1", "1:1"), ("custom", "Custom"))


@register(
    "aspect_ratio", "Aspect ratio correction", "geometry",
    summary="""Stretches the image to the display aspect ratio it was meant to be shown at, so that circles are
        round and people are not squashed or stretched.""",
    use="""Correct the aspect ratio of CCTV footage: DVRs often store 704×576, 720×576 (PAL), 720×480 (NTSC), CIF
        352×288 or 2CIF 704×288 frames that should be displayed at 4:3 or 16:9. Essential before measuring or
        describing proportions (build, height-to-width, vehicle shape).""",
    method="""One dimension is rescaled (bicubic) so that width/height equals the target ratio; the other dimension
        is kept as recorded.""",
    caveats="""The correct ratio has to come from the system documentation or from a known object in the scene (a
        wheel, a sign); a wrong choice introduces the distortion it is meant to remove.""",
    params=(
        Param("ratio", "Display ratio", "choice", "4:3", choices=ASPECTS),
        Param("custom", "Custom ratio (width ÷ height)", "float", 1.333, 0.2, 5, 0.001),
        Param("adjust", "Change", "choice", "width", choices=(("width", "Width"), ("height", "Height"))),
    ),
)
def aspect_ratio(img, p, ctx):
    h, w = img.shape[:2]
    if p["ratio"] == "custom":
        r = p["custom"]
    else:
        a, b = p["ratio"].split(":")
        r = float(a) / float(b)
    nw, nh = _check_size(h * r, h) if p["adjust"] == "width" else _check_size(w, w / r)
    ctx.note(f"{w}×{h} → {nw}×{nh}")
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_CUBIC)


@register(
    "perspective", "Perspective correction", "geometry",
    summary="""Rectifies a flat surface photographed at an angle, by mapping four picked corners to a rectangle,
        as if viewed straight on.""",
    use="""Correct the perspective of a licence plate, a document, a sign, a screen or a floor area so that it can
        be read or measured. Pick the corners of something known to be rectangular.""",
    method="""A projective transformation (homography) is computed from the four picked corners (in order: top left,
        top right, bottom right, bottom left) to a rectangle and the image is resampled through it (bicubic).
        The rectangle size is taken from the longest opposite edges unless a known width ÷ height ratio is given
        (e.g. 4.73 for a 520×110 mm UK plate). "Whole image" keeps everything, warped, in the output.""",
    caveats="""Only the plane of the four points is correct; anything standing out of that plane is distorted.
        Pixels far from the camera are stretched and look blurred: rectification does not add resolution.""",
    params=(
        Param("corners", "Corners", "points", [], count=4, labels=("top left", "top right", "bottom right", "bottom left")),
        Param("aspect", "Known width ÷ height", "float", 0, 0, 20, 0.01, help="0 = estimate from the picked corners."),
        Param("output", "Output", "choice", "crop", choices=(("crop", "Rectified region only"), ("full", "Whole image"))),
        Param("scale", "Output scale", "float", 1.0, 0.25, 8, 0.05, unit="×"),
    ),
)
def perspective(img, p, ctx):
    if not p["corners"]:
        ctx.note("no corners picked: unchanged")
        return img
    src = np.float32(p["corners"])
    tl, tr, br, bl = src
    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    hgt = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    if w < 2 or hgt < 2:
        raise FilterError("The four corners are too close together")
    if p["aspect"] > 0:
        hgt = w / p["aspect"]
    w, hgt = w * p["scale"], hgt * p["scale"]
    dst = np.float32([[0, 0], [w, 0], [w, hgt], [0, hgt]])
    m = cv2.getPerspectiveTransform(src, dst)
    if p["output"] == "crop":
        size = _check_size(w, hgt)
        ctx.note(f"rectified to {size[0]}×{size[1]} px")
        return cv2.warpPerspective(img, m, size, flags=cv2.INTER_CUBIC)
    ih, iw = img.shape[:2]
    corners = cv2.perspectiveTransform(np.float32([[[0, 0], [iw, 0], [iw, ih], [0, ih]]]), m)[0]
    x0, y0 = corners.min(0)
    x1, y1 = corners.max(0)
    # very oblique views send the far edge towards infinity; keep the output to 4× the input area
    limit = math.sqrt(4 * iw * ih / max((x1 - x0) * (y1 - y0), 1))
    s = min(1.0, limit)
    t = np.array([[s, 0, -x0 * s], [0, s, -y0 * s], [0, 0, 1]], np.float64)
    size = _check_size((x1 - x0) * s, (y1 - y0) * s)
    if s < 1:
        ctx.note(f"output reduced to {s:.0%} to keep it a sensible size")
    return cv2.warpPerspective(img, t @ m, size, flags=cv2.INTER_CUBIC)


# ------------------------------------------------------------------ lens & camera
@register(
    "lens_distortion", "Lens distortion", "lens",
    summary="""Straightens lines that a wide-angle lens bends: barrel distortion (lines bow outwards) or pincushion
        (lines bow inwards).""",
    use="""Correct optical distortion in wide-angle CCTV, dash-cam and body-worn footage before measuring, judging
        proportions, or applying perspective correction. Adjust until straight edges in the scene (door frames,
        kerbs, walls) are straight.""",
    method="""Brown–Conrady radial model: a point at normalised radius r from the optical centre is displaced by
        (1 + k1·r² + k2·r⁴). The focal length is taken as half the image diagonal, so r = 1 at the corners.
        Negative k1 corrects barrel distortion, positive k1 pincushion. The image is resampled (bicubic) through
        OpenCV's undistortion map; zoom crops into or out of the result.""",
    caveats="""Coefficients chosen by eye are an approximation, not a calibration; for measurements, calibrate the
        camera with a chequerboard. Strong correction stretches the corners, which then look softer.""",
    params=(
        Param("k1", "k1 (main)", "float", -0.2, -1.5, 1.5, 0.005),
        Param("k2", "k2 (edges)", "float", 0.0, -1.5, 1.5, 0.005),
        Param("cx", "Centre offset x", "float", 0, -0.5, 0.5, 0.005, help="Fraction of the width; 0 is the image centre."),
        Param("cy", "Centre offset y", "float", 0, -0.5, 0.5, 0.005),
        Param("zoom", "Zoom", "float", 1.0, 0.3, 3, 0.01),
    ),
)
def lens_distortion(img, p, ctx):
    h, w = img.shape[:2]
    f = math.hypot(w, h) / 2
    cx, cy = (w - 1) / 2 + p["cx"] * w, (h - 1) / 2 + p["cy"] * h
    k = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], np.float64)
    new_k = k.copy()
    new_k[0, 0] = new_k[1, 1] = f * p["zoom"]
    dist = np.array([p["k1"], p["k2"], 0, 0], np.float64)
    mx, my = cv2.initUndistortRectifyMap(k, dist, None, new_k, (w, h), cv2.CV_32FC1)
    return cv2.remap(img, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)


@register(
    "fisheye", "Unroll 360° camera", "lens",
    summary="""Turns the circular image of a fisheye / 360° camera into a flat panorama, or into a normal
        perspective view pointed in any direction (a virtual pan-tilt-zoom camera).""",
    use="""Unroll a 360 camera from a shop or bus ceiling so people and doors look natural, or create a straight
        view of one area (a till, an entrance) for identification and for the report.""",
    method="""Equidistant fisheye model: a ray at angle θ from the optical axis lands at radius r = R·θ/(FOV/2) from
        the circle centre. Panorama: each output column is an azimuth (0–360° from the start angle) and each row a
        radius from the outer edge (horizon) to the inner limit. Perspective: rays of a virtual pinhole camera with
        the given field of view are rotated by tilt and pan and looked up in the fisheye image. Bicubic
        resampling.""",
    caveats="""The lens is assumed equidistant and the circle centre/radius must be set accurately; otherwise
        straight lines stay curved. Areas at the edge of the circle have the lowest resolution.""",
    params=(
        Param("mode", "Output", "choice", "panorama", choices=(("panorama", "360° panorama"), ("perspective", "Perspective view (virtual PTZ)"))),
        Param("circle", "Circle", "points", [], count=2, labels=("centre", "edge of circle"), help="Leave empty to use the largest circle centred in the frame."),
        Param("lens_fov", "Lens field of view", "float", 180, 90, 280, 1, unit="°"),
        Param("start", "Panorama start angle", "float", 0, -180, 180, 1, unit="°"),
        Param("inner", "Panorama inner limit", "float", 15, 0, 90, 1, unit="%", help="How close to the centre the bottom of the panorama reaches."),
        Param("ceiling", "Ceiling mounted (flip panorama)", "bool", True),
        Param("pan", "View pan", "float", 0, -180, 180, 1, unit="°"),
        Param("tilt", "View tilt", "float", 45, 0, 140, 1, unit="°", help="0 looks along the lens axis; 90 looks at the horizon."),
        Param("view_fov", "View field of view", "float", 70, 20, 150, 1, unit="°"),
        Param("height", "Output height", "int", 0, 0, 4000, 10, unit="px", help="0 = automatic."),
    ),
)
def fisheye(img, p, ctx):
    h, w = img.shape[:2]
    if p["circle"]:
        (cx, cy), (ex, ey) = p["circle"]
        radius = math.hypot(ex - cx, ey - cy)
    else:
        cx, cy, radius = (w - 1) / 2, (h - 1) / 2, min(w, h) / 2
    if radius < 4:
        raise FilterError("The fisheye circle is too small")
    half_fov = math.radians(p["lens_fov"]) / 2
    if p["mode"] == "panorama":
        inner = p["inner"] / 100
        natural_h = max(radius * (1 - inner), 8)
        natural_w = 2 * math.pi * radius * (1 + inner) / 2  # circumference half-way between the limits
        out_h = p["height"] or natural_h
        out_w, out_h = _check_size(natural_w * out_h / natural_h, out_h)
        az = math.radians(p["start"]) + np.linspace(0, 2 * np.pi, out_w, endpoint=False, dtype=np.float32)[None, :]
        frac = np.linspace(1, p["inner"] / 100, out_h, dtype=np.float32)[:, None]
        if not p["ceiling"]:
            frac, az = frac[::-1], -az
        r = radius * frac
        mx = (cx + r * np.cos(az)).astype(np.float32)
        my = (cy + r * np.sin(az)).astype(np.float32)
    else:
        out_h = p["height"] or int(min(h, 1080))
        out_w, out_h = _check_size(out_h * 4 / 3, out_h)
        f = (out_w / 2) / math.tan(math.radians(p["view_fov"]) / 2)
        u, v = np.meshgrid(np.arange(out_w, dtype=np.float32) - out_w / 2, np.arange(out_h, dtype=np.float32) - out_h / 2)
        rays = np.stack([u / f, v / f, np.ones_like(u)], -1)
        rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
        t, pa = math.radians(p["tilt"]), math.radians(p["pan"])
        rx = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]], np.float32)
        rz = np.array([[math.cos(pa), -math.sin(pa), 0], [math.sin(pa), math.cos(pa), 0], [0, 0, 1]], np.float32)
        d = rays @ (rz @ rx).T
        theta = np.arccos(np.clip(d[..., 2], -1, 1))
        phi = np.arctan2(d[..., 1], d[..., 0])
        r = radius * theta / half_fov
        mx = (cx + r * np.cos(phi)).astype(np.float32)
        my = (cy + r * np.sin(phi)).astype(np.float32)
    return cv2.remap(img, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)


@register(
    "deinterlace", "Deinterlace", "lens",
    summary="""Removes the comb-like jagged edges of interlaced video, where each frame is two half-pictures (fields)
        recorded 1/50 s or 1/60 s apart.""",
    use="""Video deinterlacing for analogue CCTV, DVR and broadcast footage: moving people and vehicles show combing
        that hides their outline. Keeping a single field gives a clean picture from one instant in time.""",
    method="""Top / bottom field: only the even (or odd) lines are kept and the missing lines are interpolated from
        the lines above and below. Blend: each line is averaged with its neighbours (vertical [1 2 1]/4
        filter), merging both fields.""",
    caveats="""Single-field modes halve the true vertical resolution; blend mixes two moments in time, so moving
        objects look doubled. Use on interlaced sources only: on progressive video it only softens.""",
    params=(Param("mode", "Mode", "choice", "top", choices=(("top", "Keep top field (even lines)"), ("bottom", "Keep bottom field (odd lines)"), ("blend", "Blend fields"))),),
)
def deinterlace(img, p, ctx):
    if p["mode"] == "blend":
        return cv2.filter2D(img, -1, np.array([[0.25], [0.5], [0.25]], np.float32), borderType=cv2.BORDER_REFLECT)
    h = img.shape[0]
    start = 0 if p["mode"] == "top" else 1
    out = img.copy()
    for y in range(1 - start, h, 2):
        above, below = y - 1, y + 1
        if above < 0:
            out[y] = img[below]
        elif below >= h:
            out[y] = img[above]
        else:
            out[y] = (img[above] + img[below]) / 2
    return out


# ------------------------------------------------------------------ video & multi-frame
def _window(ctx: Context, frames: int, centred: bool) -> list[int]:
    if centred:
        half = (frames - 1) / 2
        idx = range(ctx.index - math.floor(half), ctx.index + math.ceil(half) + 1)
    else:
        idx = range(ctx.index, ctx.index + frames)
    return sorted({min(max(i, 0), ctx.count - 1) for i in idx})


def _aligned_stack(img, ctx, indices, model, scale=1.0, interp=cv2.INTER_CUBIC):
    """Each frame in `indices` registered onto the current frame (and optionally upscaled). Returns (stack, matched)."""
    h, w = img.shape[:2]
    size = (int(round(w * scale)), int(round(h * scale)))
    o = (scale - 1) / 2  # pixel-centre convention, as cv2.resize
    s = np.array([[scale, 0, o], [0, scale, o], [0, 0, 1]], np.float64)
    ref_gray = align.to_gray8(img)
    frames, matched = [], 0
    for i in indices:
        other = img if i == ctx.index else ctx.frame(i)
        if other.shape != img.shape:
            raise FilterError("Frames have different sizes and can't be combined")
        if i == ctx.index or model == "none":
            m, ok = np.eye(3), True
        else:
            m, how = align.register(ref_gray, align.to_gray8(other), model)
            ok = not how.startswith("none")
        matched += ok
        m = s @ m  # moving-frame pixels → enlarged reference grid
        frames.append(other if np.allclose(m, np.eye(3)) else align.warp(other, m, size, "replicate", interp))
    return frames, matched


@register(
    "frame_integration", "Frame integration", "video",
    summary="""Averages several consecutive frames, optionally after aligning them, so random noise cancels out while
        the static scene stays. Noise falls by about √N for N frames.""",
    use="""Integrate multiple frames to improve visibility: frame integration on a dark CCTV video, a static number
        plate on a parked car, a face of a person standing still, or text on a sign, where every single frame is
        too noisy.""",
    method="""Frames from the window around the current frame (or starting at it) are registered onto the current
        frame (translation: phase correlation refined with ECC; similarity/perspective: ORB features with RANSAC),
        warped (bicubic) and combined per pixel by the mean, median (robust to passing objects) or sum (mean × N,
        to brighten very dark footage).""",
    caveats="""Anything that moves differently from the alignment model (people walking, a moving car when aligning
        on the background) is blurred or ghosted. The result is a composite of several moments, which must be
        stated: give the frame range in the report.""",
    params=(
        Param("frames", "Frames", "int", 8, 2, 128, 1),
        Param("window", "Window", "choice", "centred", choices=(("centred", "Centred on current frame"), ("forward", "From current frame onwards"))),
        Param("align", "Align frames", "choice", "translation", choices=(
            ("none", "No (fixed camera, static subject)"), ("translation", "Translation"),
            ("similarity", "Rotation + scale"), ("perspective", "Perspective"))),
        Param("combine", "Combine", "choice", "mean", choices=(("mean", "Mean"), ("median", "Median"), ("sum", "Sum (brighten)"))),
    ),
    temporal=True,
)
def frame_integration(img, p, ctx):
    if ctx.count < 2 or ctx.frame is None:
        ctx.note("needs a video or image sequence: unchanged")
        return img
    idx = _window(ctx, p["frames"], p["window"] == "centred")
    frames, matched = _aligned_stack(img, ctx, idx, p["align"])
    stack = np.stack(frames)
    out = np.median(stack, 0) if p["combine"] == "median" else stack.mean(0)
    if p["combine"] == "sum":
        out = out * len(frames)
    ctx.note(f"frames {idx[0]}–{idx[-1]} ({len(idx)})" + (f", {matched}/{len(idx)} aligned" if p["align"] != "none" else ""))
    return out.astype(np.float32)


@register(
    "super_resolution", "Multi-frame super-resolution", "video",
    summary="""Combines several frames in which the subject sits at slightly different sub-pixel positions into one
        enlarged image with more real detail and less noise than any single frame.""",
    use="""Multi-frame super-resolution of a number plate or a face that is a few pixels high; also super-resolution
        from different perspectives with the perspective model, e.g. several photos or frames of the same flat
        object (a plate, a sign) taken from slightly different positions.""",
    method="""Every frame in the window is registered onto the current frame with sub-pixel accuracy (translation:
        phase correlation + ECC; similarity/perspective: ORB + RANSAC), then warped directly onto a grid enlarged by
        the scale factor (bicubic) and the stack is fused by the per-pixel median or mean (shift-and-add).""",
    caveats="""Works only for a subject that moves rigidly (or a static subject with a moving camera), and needs real
        sub-pixel movement between frames. Crop to the subject first: the whole frame is registered as one, so a
        moving subject on a static background must be isolated. Compression can limit the gain.""",
    params=(
        Param("frames", "Frames", "int", 8, 2, 64, 1),
        Param("scale", "Scale", "float", 2, 1, 4, 0.5, unit="×"),
        Param("align", "Motion model", "choice", "translation", choices=(
            ("translation", "Translation"), ("similarity", "Rotation + scale"), ("perspective", "Perspective (different viewpoints)"))),
        Param("combine", "Fuse", "choice", "median", choices=(("median", "Median (robust)"), ("mean", "Mean (smoothest)"))),
        Param("window", "Window", "choice", "centred", choices=(("centred", "Centred on current frame"), ("forward", "From current frame onwards"))),
    ),
    temporal=True,
)
def super_resolution(img, p, ctx):
    h, w = img.shape[:2]
    _check_size(w * p["scale"], h * p["scale"])
    if ctx.count < 2 or ctx.frame is None:
        ctx.note("needs a video or image sequence: enlarged only")
        return cv2.resize(img, None, fx=p["scale"], fy=p["scale"], interpolation=cv2.INTER_CUBIC)
    idx = _window(ctx, p["frames"], p["window"] == "centred")
    frames, matched = _aligned_stack(img, ctx, idx, p["align"], p["scale"])
    stack = np.stack(frames)
    out = np.median(stack, 0) if p["combine"] == "median" else stack.mean(0)
    ctx.note(f"frames {idx[0]}–{idx[-1]}, {matched}/{len(idx)} registered, ×{p['scale']:g}")
    return out.astype(np.float32)


_PAIR_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()


def _consecutive(ctx: Context, i: int, model: str) -> np.ndarray:
    """Matrix mapping frame i+1 onto frame i (cached per chain prefix, frame and model)."""
    key = (ctx.key, ctx.count, i, model)
    hit = _PAIR_CACHE.get(key)
    if hit is None:
        hit, _ = align.register(align.to_gray8(ctx.frame(i)), align.to_gray8(ctx.frame(i + 1)), model)
        _PAIR_CACHE[key] = hit
        if len(_PAIR_CACHE) > 20000:
            _PAIR_CACHE.popitem(last=False)
    return hit


def _chain_down(ctx: Context, a: int, b: int, model: str) -> np.ndarray:
    """Matrix mapping frame b onto frame a (a < b), composed from consecutive motions."""
    m = np.eye(3)
    for k in range(a, b):
        m = m @ _consecutive(ctx, k, model)
    return m


@register(
    "stabilize", "Stabilisation", "video",
    summary="""Removes camera shake: each frame is shifted (and optionally rotated and scaled) to cancel the
        unwanted motion, so the scene stays still and moving subjects are easier to follow.""",
    use="""Stabilise shaky handheld, body-worn or wind-blown CCTV footage; lock the view onto a reference frame to
        compare frames, or prepare for frame integration and super-resolution.""",
    method="""Smooth: the motion between each pair of consecutive frames is measured (registration as for frame
        integration) and chained to give the motion of the current frame relative to every frame in a window of
        ±radius frames; the average of those motions is the smoothed camera
        path, and the frame is warped by it, removing shake faster than the window while keeping deliberate
        pans. Lock: every frame is warped onto the chosen reference frame. Borders uncovered by the shift are
        black or repeat the edge pixels; zoom hides them.""",
    caveats="""Motion is estimated from the whole frame, so large moving objects can disturb it. Rolling-shutter
        wobble is not corrected. Stabilisation resamples every frame.""",
    params=(
        Param("mode", "Mode", "choice", "smooth", choices=(("smooth", "Smooth camera path"), ("lock", "Lock to reference frame"))),
        Param("radius", "Smoothing radius", "int", 10, 1, 60, 1, unit="frames"),
        Param("reference", "Reference frame", "int", 0, 0, 1_000_000, 1, help="Lock mode: frame number everything is aligned to."),
        Param("model", "Motion model", "choice", "similarity", choices=(("translation", "Translation"), ("similarity", "Rotation + scale"))),
        Param("border", "Borders", "choice", "black", choices=(("black", "Black"), ("replicate", "Repeat edge pixels"))),
        Param("zoom", "Zoom", "float", 1.0, 1.0, 1.5, 0.01, unit="×"),
    ),
    temporal=True,
)
def stabilize(img, p, ctx):
    if ctx.count < 2 or ctx.frame is None:
        ctx.note("needs a video or image sequence: unchanged")
        return img
    h, w = img.shape[:2]
    ref_gray = align.to_gray8(img)
    if p["mode"] == "lock":
        ref = min(p["reference"], ctx.count - 1)
        m = np.eye(3) if ref == ctx.index else align.register(align.to_gray8(ctx.frame(ref)), ref_gray, p["model"])[0]
        ctx.note(f"locked to frame {ref}")
    else:
        # motion between consecutive frames is measured once and cached; the motion from this frame to any
        # neighbour is the chain of consecutive motions, so each new frame costs about one registration
        window = _window(ctx, 2 * p["radius"] + 1, True)
        params = []
        for j in window:
            # m_tj maps this frame's pixels onto neighbour j
            if j < ctx.index:
                m_tj = _chain_down(ctx, j, ctx.index, p["model"])
            elif j > ctx.index:
                m_tj = np.linalg.inv(_chain_down(ctx, ctx.index, j, p["model"]))
            else:
                m_tj = np.eye(3)
            params.append(align.decompose(m_tj))
        # moving this frame by the mean of those motions puts it on the average (smoothed) camera path
        m = align.compose(*np.mean(params, 0))
        dx, dy, a, _ = align.decompose(m)
        ctx.note(f"correction {dx:+.1f}, {dy:+.1f} px, {math.degrees(a):+.2f}°")
    if p["zoom"] != 1:
        c = np.array([[p["zoom"], 0, (1 - p["zoom"]) * (w - 1) / 2], [0, p["zoom"], (1 - p["zoom"]) * (h - 1) / 2], [0, 0, 1]])
        m = c @ m
    return align.warp(img, m, (w, h), p["border"])
