// The viewer: renders the current frame through the chain, before/after comparison, zoom, frame transport,
// histogram, and the SVG overlay used for picking points (perspective corners, crop...) and measuring.
(() => {
  const stage = $("#viewer-stage");
  const mainBox = $("#main-box");
  const afterImg = $("#after-img");
  const beforeImg = $("#before-img");
  const beforeSide = $("#before-side");
  const overlay = $("#overlay");
  const handle = $("#split-handle");
  const pill = $("#render-pill");
  const caption = $("#viewer-caption");
  const slider = $("#frame-slider");
  const frameInput = $("#frame-input");
  const SVG = "http://www.w3.org/2000/svg";

  const view = { mode: "split", zoom: "fit", split: 50, width: 0, height: 0, gen: 0, shown: 0, originalFor: null, playing: false };
  const session = Math.random().toString(36).slice(2); // lets the backend tell a reloaded page from an old request
  let pick = null; // {step, param, points, position}
  let timer = null;
  let inflight = false;
  let queued = false;

  // ---------- rendering ----------
  function request(delay = 90) {
    clearTimeout(timer);
    timer = setTimeout(run, delay);
  }
  CL.requestPreview = request;

  async function run() {
    if (!CL.state.source) return;
    if (inflight) {
      queued = true;
      return;
    }
    inflight = true;
    queued = false;
    const gen = ++view.gen;
    const index = CL.state.index;
    const wantOriginal = view.originalFor !== `${CL.state.sourceKey}:${index}`;
    const req = { chain: CL.serializeChain(), index, gen, session, original: wantOriginal };
    if (pick) req.upto = pick.position;
    pill.textContent = "rendering…";
    pill.classList.remove("on", "bad");
    try {
      const data = await api("preview", req);
      if (data.stale || gen < view.shown) return;
      view.shown = gen;
      data.partial = !!pick;
      show(data, index);
      CL.lastPreview = data.partial ? CL.lastPreview : data;
      CL.emit("preview", data);
      showError("");
      const failed = (data.steps || []).find((s) => s.error);
      pill.textContent = failed ? "step failed" : `${data.ms} ms`;
      pill.classList.toggle("bad", !!failed);
      pill.classList.toggle("on", !failed);
    } catch (e) {
      pill.textContent = "error";
      pill.classList.add("bad");
      showError(e.message);
    } finally {
      inflight = false;
      if (queued) run();
      else if (view.playing) step(1, true);
    }
  }

  function show(data, index) {
    afterImg.src = data.image;
    view.width = data.width;
    view.height = data.height;
    if (data.original) {
      beforeImg.src = beforeSide.src = data.original;
      view.originalFor = `${CL.state.sourceKey}:${index}`;
      view.origW = data.original_width;
      view.origH = data.original_height;
    }
    overlay.setAttribute("viewBox", `0 0 ${data.width} ${data.height}`);
    applyZoom();
    drawHistogram(data.histogram);
    const t = data.time !== null && data.time !== undefined ? ` · ${formatTime(data.time)}` : "";
    const src = CL.state.source;
    const sizeNote = view.origW && (view.origW !== data.width || view.origH !== data.height) ? ` (source ${view.origW}×${view.origH})` : "";
    caption.textContent = pick
      ? `Input to step ${pick.position + 1} · ${data.width}×${data.height}`
      : `${src.count > 1 ? `Frame ${index}${t} · ` : ""}${data.width}×${data.height}${sizeNote}`;
    $("#frame-time").textContent = data.time !== null && data.time !== undefined ? formatTime(data.time) : "";
    drawOverlay();
  }

  // ---------- zoom & view modes ----------
  function applyZoom() {
    stage.dataset.zoom = view.zoom === "fit" ? "fit" : "fixed";
    if (view.zoom === "fit") {
      afterImg.style.width = afterImg.style.height = "";
      beforeSide.style.width = "";
    } else {
      const z = Number(view.zoom);
      afterImg.style.width = `${view.width * z}px`;
      afterImg.style.height = `${view.height * z}px`;
      beforeSide.style.width = `${(view.origW || view.width) * z}px`;
    }
    stage.classList.toggle("pixelated", view.zoom !== "fit" && Number(view.zoom) >= 2);
  }

  function setView(mode) {
    view.mode = mode;
    stage.dataset.view = pick ? "after" : mode;
    $$("#view-mode button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === mode)));
    updateSplit();
  }

  function updateSplit() {
    beforeImg.style.clipPath = `inset(0 ${100 - view.split}% 0 0)`;
    handle.style.left = `${view.split}%`;
  }

  $$("#view-mode button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#zoom-select").addEventListener("change", (e) => {
    view.zoom = e.target.value;
    applyZoom();
  });

  // dragging the before/after divider
  let dragSplit = false;
  handle.addEventListener("pointerdown", (e) => {
    dragSplit = true;
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragSplit) return;
    const r = mainBox.getBoundingClientRect();
    view.split = Math.min(100, Math.max(0, ((e.clientX - r.left) / r.width) * 100));
    updateSplit();
  });
  handle.addEventListener("pointerup", () => (dragSplit = false));

  // ---------- frames ----------
  function setFrame(i, fromPlay = false) {
    const src = CL.state.source;
    if (!src) return;
    const n = Math.min(Math.max(0, Math.round(i)), src.count - 1);
    if (!fromPlay) stopPlay();
    if (n === CL.state.index && !fromPlay) return;
    CL.state.index = n;
    slider.value = n;
    frameInput.value = n;
    CL.emit("frame", n);
    request(fromPlay ? 0 : 40);
  }
  CL.setFrame = setFrame;

  function step(d, fromPlay = false) {
    const src = CL.state.source;
    if (!src) return;
    if (fromPlay && CL.state.index + d >= src.count) {
      stopPlay();
      return;
    }
    setFrame(CL.state.index + d, fromPlay);
  }

  function stopPlay() {
    view.playing = false;
    $("#play-btn").textContent = "▶";
  }

  $("#first-frame").addEventListener("click", () => setFrame(0));
  $("#prev-frame").addEventListener("click", () => step(-1));
  $("#next-frame").addEventListener("click", () => step(1));
  $("#play-btn").addEventListener("click", () => {
    if (view.playing) return stopPlay();
    view.playing = true;
    $("#play-btn").textContent = "❚❚";
    step(1, true);
  });
  slider.addEventListener("input", () => setFrame(Number(slider.value)));
  frameInput.addEventListener("change", () => setFrame(Number(frameInput.value)));
  document.addEventListener("keydown", (e) => {
    if (!CL.state.source || CL.state.source.count < 2 || $("#enhance-view").classList.contains("hidden")) return;
    if (e.target.closest("input, select, textarea")) return;
    if (e.key === "ArrowRight") step(e.shiftKey ? 10 : 1);
    else if (e.key === "ArrowLeft") step(e.shiftKey ? -10 : -1);
    else if (e.key === " ") {
      e.preventDefault();
      $("#play-btn").click();
    } else return;
    e.preventDefault();
  });

  CL.on("source", (info) => {
    CL.state.sourceKey = `${info.path}:${Date.now()}`;
    view.originalFor = null;
    cancelPick();
    const multi = info.count > 1;
    $("#transport").classList.toggle("hidden", !multi);
    slider.max = Math.max(0, info.count - 1);
    frameInput.max = Math.max(0, info.count - 1);
    slider.value = frameInput.value = CL.state.index;
    request(0);
  });
  CL.on("chain", () => request());

  // ---------- histogram ----------
  function drawHistogram(h) {
    const c = $("#histogram");
    if (!h) return;
    const ctx = c.getContext("2d");
    const W = c.width;
    const H = c.height;
    ctx.clearRect(0, 0, W, H);
    const max = Math.max(1, ...h.l, ...h.r, ...h.g, ...h.b);
    const scale = (v) => H - (Math.sqrt(v) / Math.sqrt(max)) * (H - 2);
    const bw = W / h.l.length;
    ctx.fillStyle = "rgba(238,241,242,0.22)";
    h.l.forEach((v, i) => ctx.fillRect(i * bw, scale(v), bw, H - scale(v)));
    [["r", "rgba(240,90,90,0.9)"], ["g", "rgba(62,207,142,0.9)"], ["b", "rgba(96,165,250,0.9)"]].forEach(([k, col]) => {
      ctx.strokeStyle = col;
      ctx.lineWidth = 1;
      ctx.beginPath();
      h[k].forEach((v, i) => (i ? ctx.lineTo(i * bw + bw / 2, scale(v)) : ctx.moveTo(bw / 2, scale(v))));
      ctx.stroke();
    });
  }

  // ---------- overlay ----------
  function toImage(e) {
    const r = overlay.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * view.width, ((e.clientY - r.top) / r.height) * view.height];
  }

  function el(tag, attrs, parent = overlay) {
    const node = document.createElementNS(SVG, tag);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    parent.appendChild(node);
    return node;
  }
  CL.svg = el;

  // handle size in image pixels so it looks the same at any zoom
  CL.handleSize = () => {
    const r = overlay.getBoundingClientRect();
    return r.width ? (view.width / r.width) * 7 : 6;
  };

  function drawOverlay() {
    overlay.innerHTML = "";
    if (!view.width) return;
    if (pick) drawPick();
    else CL.emit("overlay");
  }
  CL.drawOverlay = drawOverlay;

  function drawPick() {
    const pts = pick.points;
    const r = CL.handleSize();
    const id = pick.step.id;
    if (pts.length >= 2) {
      if (id === "crop") {
        const [a, b] = pts;
        el("rect", { x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), width: Math.abs(b[0] - a[0]), height: Math.abs(b[1] - a[1]), class: "ov-shape" });
      } else if (id === "fisheye") {
        const [c, e] = pts;
        el("circle", { cx: c[0], cy: c[1], r: Math.hypot(e[0] - c[0], e[1] - c[1]), class: "ov-shape" });
      } else {
        el(pts.length === pick.param.count && pick.param.count > 2 ? "polygon" : "polyline", { points: pts.map((p) => p.join(",")).join(" "), class: "ov-shape" });
      }
    }
    pts.forEach((p, i) => {
      const g = el("g", { class: "ov-handle", "data-i": i });
      el("circle", { cx: p[0], cy: p[1], r, class: "ov-dot" }, g);
      const right = p[0] > view.width * 0.75; // keep labels near the right edge inside the image
      const label = el("text", { x: p[0] + (right ? -r * 1.6 : r * 1.6), y: p[1] - r * 1.2, class: "ov-label", "font-size": r * 2.2, "text-anchor": right ? "end" : "start" }, g);
      label.textContent = pick.param.labels[i] || String(i + 1);
    });
  }

  function pickText() {
    const n = pick.points.length;
    const need = pick.param.count;
    const name = CL.state.filters[pick.step.id].name;
    if (n < need) return `${name}: click the <strong>${escapeHtml(pick.param.labels[n] || `point ${n + 1}`)}</strong> (${n + 1} of ${need}). The view shows this step's input.`;
    return `${name}: all ${need} point${need > 1 ? "s" : ""} set. Drag them to adjust, then press Done.`;
  }

  CL.startPick = (stepObj, param) => {
    CL.cancelPick();
    stopPlay();
    pick = { step: stepObj, param, points: (stepObj.params[param.key] || []).map((p) => p.slice()), position: CL.state.chain.indexOf(stepObj) };
    $("#pick-banner").classList.remove("hidden");
    $("#pick-text").innerHTML = pickText();
    stage.classList.add("picking");
    setView(view.mode);
    request(0);
    stage.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };

  function commitPick() {
    if (!pick) return;
    pick.step.params[pick.param.key] = pick.points.length === pick.param.count ? pick.points.map((p) => p.map((v) => Math.round(v * 10) / 10)) : [];
  }

  function cancelPick() {
    if (!pick) return;
    pick = null;
    $("#pick-banner").classList.add("hidden");
    stage.classList.remove("picking");
    setView(view.mode);
  }
  CL.cancelPick = cancelPick;

  $("#pick-done").addEventListener("click", () => {
    if (!pick) return;
    commitPick();
    const s = pick.step;
    cancelPick();
    CL.pointsChanged(s);
  });
  $("#pick-clear").addEventListener("click", () => {
    if (!pick) return;
    pick.points = [];
    $("#pick-text").innerHTML = pickText();
    drawOverlay();
  });

  let dragIndex = -1;
  overlay.addEventListener("pointerdown", (e) => {
    if (!view.width || e.button !== 0) return;
    const p = toImage(e);
    if (pick) {
      const hit = e.target.closest(".ov-handle");
      if (hit) {
        dragIndex = Number(hit.dataset.i);
      } else if (pick.points.length < pick.param.count) {
        pick.points.push(p);
        dragIndex = pick.points.length - 1;
        $("#pick-text").innerHTML = pickText();
      } else return;
      overlay.setPointerCapture(e.pointerId);
      drawOverlay();
      e.preventDefault();
      return;
    }
    overlay.setPointerCapture(e.pointerId); // keep receiving the drag even over the split handle
    CL.emit("overlay-down", { point: p, event: e });
  });
  overlay.addEventListener("pointermove", (e) => {
    const p = toImage(e);
    if (pick && dragIndex >= 0) {
      pick.points[dragIndex] = [Math.min(Math.max(p[0], 0), view.width), Math.min(Math.max(p[1], 0), view.height)];
      drawOverlay();
      return;
    }
    if (!pick) CL.emit("overlay-move", { point: p, event: e });
  });
  overlay.addEventListener("pointerup", (e) => {
    if (pick) {
      dragIndex = -1;
      if (pick.points.length === pick.param.count) {
        // live preview of the finished points without leaving pick mode would hide the input; just update the text
        commitPick();
        CL.refreshPoints(pick.step);
      }
      return;
    }
    CL.emit("overlay-up", { point: toImage(e), event: e });
  });

  CL.view = view;
  CL.isPicking = () => !!pick;
  setView("split");
})();
