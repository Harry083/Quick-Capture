# Clarity

A desktop application for **forensic image and video enhancement**, in the same style as Quick Capture. Open a
still, a video or a set of photos, build a **chain of filters**, compare before and after, measure, export, and
produce a **report that explains every step**: what each filter does, why it's used, how it works, its caveats
and the exact parameters, with the image after each step.

Like Quick Capture it opens in its own native window through [pywebview](https://pywebview.flowrl.com/). **No
web server runs and no network port is opened**, and the evidence never leaves the machine.

## Forensic principles

- **The source is never modified.** Files are opened read-only. Decoded frames are held read-only in memory, and
  every filter works on a copy. Exports refuse to overwrite the source.
- **Hashes.** MD5 and SHA-256 of the source are computed when it opens, and of every export when it's written.
  All are listed in the report. PNG frame sequences get a `SHA256SUMS.txt` manifest.
- **Non-destructive and reproducible.** The chain is a list of filters and parameters, not a series of edits.
  Toggle, reorder or retune any step at any time. **Save project** stores the chain, frame, case details,
  measurements and the source's SHA-256. Reopening a project re-checks that hash.
- **Explained.** The text in the Filter guide, on each filter card and in the report all comes from the same
  place (`backend/filters.py`). The report appendix describes every available filter, used or not.

## Requirements and running

- Python 3.10+
- The same system web engine as Quick Capture (Edge WebView2 on Windows, WebKit on macOS, WebKitGTK or Qt on
  Linux)

```bash
cd clarity
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     # Windows
.venv\Scripts\python.exe app.py
```

On Linux/macOS use `.venv/bin/python`. Administrator rights aren't needed. Add `--debug` for the web inspector.

To build a single-file app: `python -m pip install pyinstaller` then `python -m PyInstaller --clean Clarity.spec`.
This produces `dist/Clarity.exe` (or `dist/Clarity`).

## Workflow

1. **Source**: **Browse…** for an image or video, or **Image sequence…** to treat several photos as frames (e.g.
   photos of the same plate for super-resolution). The badges show type, resolution, frame rate, frame count,
   codec and the SHA-256 once hashing finishes.
2. **Viewer**: **Split** (drag the divider), **After**, **Before** or **Side by side**, zoom from Fit up to 800 %
   (pixelated, so you see the real pixels), and a histogram of the processed frame. For video, use the frame
   slider, ◀ ▶, **← →** (Shift for ±10), and **space** to play through the processed frames.
3. **Filters**: add filters from the list. They run top to bottom. Each card has an on/off switch, reorder and
   remove buttons, sliders, **About this filter**, and a result line under its name (e.g. *rectified to 785×532 px*,
   *24 interference peaks removed*, *frames 22–29, 8/8 aligned*) with the time it took. Filters that need points
   (perspective corners, crop, neutral point, fisheye circle) have **⌖ Pick on image**. The viewer then shows that
   step's *input*, you click the points, drag to adjust, and press **Done**.
4. **Measure**: **Set scale** by dragging along something of known length, then **Distance**. **Speed**: click a
   point on a vehicle, move to a later frame, and click the same point. The result is shown in km/h and mph, using
   the scale and the frame rate.
5. **Case**: case number, exhibit, examiner, organisation, request and notes, for the report.
6. **Output**: **Export frame** (PNG/TIFF lossless 8- or 16-bit, or JPEG) and **Export video** for a frame range
   (lossless FFV1 MKV, Motion-JPEG AVI, MP4, or numbered PNGs). **View report** / **Save HTML** / **Save JSON**.

The **Filter guide** tab lists every filter with search and category chips. **+ Add to chain** adds a filter
straight from its description, and **Open as report** / **Save HTML** give the reference as a standalone document.

## Filters

| Category | Filters |
|---|---|
| Levels & exposure | Levels, Auto contrast stretch, Brightness & contrast, Exposure & gamma, Shadows & highlights (backlit subjects), Histogram equalisation (global / CLAHE) |
| Colour & channels | White balance (grey world, white patch, picked neutral point, manual), Channel select (RGB, HSV, L\*a\*b\*), Invert, Saturation |
| Sharpen & deblur | Unsharp mask, Motion deblur (linear PSF, Wiener or Richardson–Lucy), Optical deblur (defocus disc or Gaussian) |
| Denoise & frequency | Gaussian, Median, Bilateral, Non-local means, Periodic noise removal (automatic FFT notch), Frequency filter (low/high/band-pass, e.g. fingerprints), Background flatten |
| Geometry & perspective | Crop, Rotate & flip, Resize (nearest keeps real pixels), Aspect ratio correction (CCTV 704×576 → 4:3 …), Perspective correction (4 points, optional known ratio) |
| Lens & camera | Lens distortion (Brown–Conrady k1/k2), Unroll 360° camera (panorama or virtual PTZ view), Deinterlace (top/bottom field or blend) |
| Video & multi-frame | Frame integration (aligned mean/median/sum), Multi-frame super-resolution (sub-pixel registration + shift-and-add), Stabilisation (smoothed path or lock to a frame) |

These cover the typical casework examples: deblurring a moving car, integrating frames from a dark CCTV clip,
correcting the perspective of a plate, super-resolution from several frames or viewpoints, deinterlacing,
removing periodic noise, a backlit subject, unrolling a 360° camera, aspect-ratio correction, separating a
fingerprint from its background, and measuring speed from surveillance video.

### Multi-frame filters

Temporal filters see the *output of the steps above them* for neighbouring frames. So *Deinterlace →
Stabilise → Frame integration* integrates deinterlaced, stabilised frames. Every intermediate result is cached
per frame, so moving a slider only re-runs the steps after it, and stepping through a video re-uses the
neighbours' work. Stabilisation measures the motion between consecutive frames once and chains it. Frame
integration and super-resolution register each neighbour directly onto the current frame for sub-pixel
accuracy.

## What the report contains

- Summary, case details and the processing steps in order, with each step's result
- **Before and after** of the frame shown
- **Source evidence**: path, size, modified time, MD5 and SHA-256, decoder notes, and a statement that the
  original wasn't modified
- **Every step**: what it does, why it's used, how it works, caveats, all parameters (changed values
  highlighted), its result note, output size, and a thumbnail after that step. Disabled steps are listed as not
  applied.
- Measurements (recomputed from the points, not taken from the page), exported files with hashes,
  reproducibility details (software versions, the chain as JSON)
- **Appendix**: a reference of every filter available, with those used in the chain marked

## Notes and limits

- Video is decoded with OpenCV's FFmpeg backend. Proprietary DVR formats (`.dav`, `.264` with custom headers…)
  may need remuxing to a standard container first. Frame counts and frame rates come from the container. Some
  DVRs record a nominal rate that differs from the real one, so check speeds against an on-screen clock.
- Measurements are only valid in the plane of the scale reference. Rectify it first (lens distortion, then
  perspective).
- Report images are reduced-size previews. Examine the exported files.
- Enhancement makes recorded detail visible; it cannot create detail that wasn't captured. Each filter's
  caveats say what it may introduce.

## Development

```bash
python -m pytest tests                 # 61 tests: filters undo known damage, exports, reports, the API
python tests/dev_server.py             # the UI in an ordinary browser at http://127.0.0.1:8765, real backend behind it
```

The dev server exists only for UI work and browser tests. It stands in for pywebview's bridge, and the desktop
app itself never opens a port.

| Path | What it is |
|---|---|
| `backend/filters.py` | Every filter, its parameters and its explanations |
| `backend/align.py` | Frame registration (phase correlation + ECC, ORB + RANSAC) |
| `backend/pipeline.py` | Runs the chain on a frame, with caching and temporal access |
| `backend/media.py` | Read-only image / sequence / video sources and hashing |
| `backend/export.py` | Frame and video exports, hashed |
| `backend/report.py` | HTML/JSON report and the filter reference |
| `backend/api.py` | Methods the page calls through pywebview |
| `frontend/` | `index.html`, the shared `styles.css`, `clarity.css`, and one script per area |
