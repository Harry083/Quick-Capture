// Measurements on the processed image: a scale reference, distances, and speed between two frames.
// Points are in the processed image's pixel coordinates; the backend recomputes every value for the report.
(() => {
  const M = () => CL.state.measurements;
  const listEl = $("#measure-list");
  const hint = $("#measure-hint");
  const lengthInput = $("#scale-length");
  const unitSelect = $("#scale-unit");
  const FACT = { m: 1, cm: 0.01, mm: 0.001, ft: 0.3048, in: 0.0254 };
  let tool = null;
  let drawing = null; // [p0, p1] while dragging a line
  let speedA = null; // {frame, point}

  const dist = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
  const size = () => [CL.view.width, CL.view.height];
  const metresPerPixel = () => {
    const c = M().calibration;
    return c && c.length > 0 && dist(...c.points) > 0 ? (c.length * FACT[c.unit]) / dist(...c.points) : null;
  };

  function setTool(t) {
    tool = tool === t ? null : t;
    drawing = null;
    speedA = null;
    $$("#measure-tools button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tool === tool)));
    $("#viewer-stage").classList.toggle("measuring", !!tool);
    hint.textContent = {
      scale: "Drag along an object of known length (in the plane you will measure in).",
      distance: "Drag from one point to another.",
      speed: "Click a point on the vehicle; then go to a later frame and click the same point.",
    }[tool] || "";
    if (tool && CL.view.mode === "before") $$("#view-mode button").find((b) => b.dataset.view === "after").click();
    CL.drawOverlay();
  }
  $$("#measure-tools button").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));

  function saveScaleFields() {
    const c = M().calibration;
    if (!c) return;
    c.length = Number(lengthInput.value) || 0;
    c.unit = unitSelect.value;
    render();
  }
  lengthInput.addEventListener("input", saveScaleFields);
  unitSelect.addEventListener("change", saveScaleFields);

  CL.on("overlay-down", ({ point }) => {
    if (!tool || CL.isPicking()) return;
    if (tool === "speed") {
      if (!speedA) {
        speedA = { frame: CL.state.index, point };
        hint.textContent = `Point A set on frame ${CL.state.index}. Move to a later frame (→) and click the same point.`;
      } else if (speedA.frame === CL.state.index) {
        hint.textContent = "Point B must be on a different frame. Step forward first.";
        return;
      } else {
        M().items.push({ type: "speed", a: speedA, b: { frame: CL.state.index, point }, size: size() });
        speedA = null;
        hint.textContent = "Speed added. Click to start another, or choose a different tool.";
        render();
      }
      CL.drawOverlay();
      return;
    }
    drawing = [point, point];
  });
  CL.on("overlay-move", ({ point }) => {
    if (!drawing) return;
    drawing[1] = point;
    CL.drawOverlay();
  });
  CL.on("overlay-up", ({ point }) => {
    if (!drawing) return;
    drawing[1] = point;
    const pts = drawing.map((p) => p.map((v) => Math.round(v * 10) / 10));
    drawing = null;
    if (dist(...pts) < 2) return CL.drawOverlay();
    if (tool === "scale") {
      M().calibration = { points: pts, length: Number(lengthInput.value) || 1, unit: unitSelect.value, frame: CL.state.index, size: size() };
      hint.textContent = "Scale set. Enter the real length of that reference above if you haven't already.";
    } else {
      M().items.push({ type: "distance", points: pts, frame: CL.state.index, size: size() });
    }
    render();
    CL.drawOverlay();
  });

  // ---------- drawing ----------
  function line(a, b, cls, text) {
    CL.svg("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: cls });
    const r = CL.handleSize();
    [a, b].forEach((p) => CL.svg("circle", { cx: p[0], cy: p[1], r: r * 0.6, class: `${cls} end` }));
    if (text) {
      const t = CL.svg("text", { x: (a[0] + b[0]) / 2 + r, y: (a[1] + b[1]) / 2 - r, class: "ov-label", "font-size": r * 2.2 });
      t.textContent = text;
    }
  }

  CL.on("overlay", () => {
    const m = M();
    const mpp = metresPerPixel();
    const unit = m.calibration ? m.calibration.unit : "m";
    if (m.calibration) line(...m.calibration.points, "ov-scale", `ref ${m.calibration.length} ${m.calibration.unit}`);
    m.items.forEach((it, i) => {
      if (it.type === "distance") {
        const other = it.frame !== CL.state.index && CL.state.source.count > 1;
        const v = mpp ? `${((dist(...it.points) * mpp) / FACT[unit]).toFixed(2)} ${unit}` : `${dist(...it.points).toFixed(0)} px`;
        line(...it.points, other ? "ov-measure faint" : "ov-measure", `#${i + 1} ${v}`);
      } else {
        [["A", it.a], ["B", it.b]].forEach(([n, s]) => {
          if (s.frame !== CL.state.index) return;
          const r = CL.handleSize();
          CL.svg("circle", { cx: s.point[0], cy: s.point[1], r, class: "ov-speed" });
          const t = CL.svg("text", { x: s.point[0] + r * 1.5, y: s.point[1] - r, class: "ov-label", "font-size": r * 2.2 });
          t.textContent = `#${i + 1} ${n}`;
        });
      }
    });
    if (speedA && speedA.frame === CL.state.index) {
      const r = CL.handleSize();
      CL.svg("circle", { cx: speedA.point[0], cy: speedA.point[1], r, class: "ov-speed" });
    }
    if (drawing) line(...drawing, tool === "scale" ? "ov-scale" : "ov-measure");
  });

  // ---------- list ----------
  function sizeWarning(it) {
    const [w, h] = size();
    return it.size && w && (it.size[0] !== w || it.size[1] !== h)
      ? ` <span class="err" title="The chain has changed the image size since this was measured">⚠ made on ${it.size[0]}×${it.size[1]}</span>`
      : "";
  }

  function render() {
    const m = M();
    const mpp = metresPerPixel();
    const unit = m.calibration ? m.calibration.unit : "m";
    const rows = [];
    if (m.calibration) {
      const c = m.calibration;
      rows.push(`<tr><td>Scale</td><td>${c.length} ${escapeHtml(c.unit)} = ${dist(...c.points).toFixed(1)} px${mpp ? ` · ${(mpp * 1000).toFixed(2)} mm/px` : ""}${sizeWarning(c)}</td>
        <td><button class="link" data-del="cal">remove</button></td></tr>`);
    }
    m.items.forEach((it, i) => {
      let text;
      if (it.type === "distance") {
        const px = dist(...it.points);
        text = `Distance · ${px.toFixed(1)} px${mpp ? ` = <strong>${((px * mpp) / FACT[unit]).toFixed(3)} ${unit}</strong>` : ""} · frame ${it.frame}`;
      } else {
        const px = dist(it.a.point, it.b.point);
        const frames = Math.abs(it.b.frame - it.a.frame);
        const fps = CL.state.source.fps;
        let v = "";
        if (mpp && fps && frames) {
          const mps = (px * mpp) / (frames / fps);
          v = ` = <strong>${(mps * 3.6).toFixed(1)} km/h · ${(mps * 2.236936).toFixed(1)} mph</strong>`;
        } else v = ` <span class="muted">(${!mpp ? "set a scale" : "no frame rate"})</span>`;
        text = `Speed · frames ${it.a.frame}→${it.b.frame}${fps ? ` (${(frames / fps).toFixed(3)} s)` : ""} · ${px.toFixed(1)} px${v}`;
      }
      rows.push(`<tr><td>#${i + 1}</td><td>${text}${sizeWarning(it)}
        <input type="text" class="path-input label-input" data-label="${i}" placeholder="label" value="${escapeHtml(it.label || "")}" /></td>
        <td>${it.frame !== undefined || it.a ? `<button class="link" data-goto="${it.frame !== undefined ? it.frame : it.a.frame}">go to</button> ` : ""}<button class="link" data-del="${i}">remove</button></td></tr>`);
    });
    listEl.innerHTML = rows.length ? `<table class="meta-table measure-table">${rows.join("")}</table>` : `<p class="placeholder">No measurements yet.</p>`;
    if (m.calibration) {
      lengthInput.value = m.calibration.length;
      unitSelect.value = m.calibration.unit;
    }
  }

  listEl.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.del === "cal") M().calibration = null;
    else if (b.dataset.del !== undefined) M().items.splice(Number(b.dataset.del), 1);
    else if (b.dataset.goto !== undefined) return CL.setFrame(Number(b.dataset.goto));
    render();
    CL.drawOverlay();
  });
  listEl.addEventListener("input", (e) => {
    if (e.target.dataset.label !== undefined) M().items[Number(e.target.dataset.label)].label = e.target.value;
  });

  CL.on("measurements", () => {
    render();
    CL.drawOverlay();
  });
  CL.on("source", () => {
    setTool(null);
    render();
  });
  CL.on("preview", (d) => {
    if (!d.partial) render();
  });
})();
