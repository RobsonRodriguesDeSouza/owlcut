/* OwlCut clip kit — builds intro, word-by-word captions and outro for a vertical clip
 * onto a HyperFrames GSAP timeline. Deterministic: no clocks, no randomness,
 * no font measurement (sizes come from character counts, so a late font load
 * cannot change the layout between preview and render).
 *
 *   ClipKit.mount(tl, document.getElementById("ck"), {
 *     width: 1080, height: 1920, duration: 31.2,
 *     words: [{ text: "Nobody", start: 0.12, end: 0.48 }, ...],   // seconds in the clip
 *     caption: "highlight" | "karaoke" | "clean" | "impact" | "none",
 *     intro: { style: "hook" | "title-card" | "letters" | "none", text, sub, duration },
 *     outro: { style: "cta" | "none", text, duration },
 *     accent: "#ff2d55", accentInk: "#fff", keywords: ["secret"]
 *   });
 */
(function () {
  "use strict";

  var STYLE = {
    highlight: { maxWords: 3, base: 96, charW: 0.74, lines: 2 },
    karaoke: { maxWords: 4, base: 82, charW: 0.6, lines: 2 },
    clean: { maxWords: 5, base: 84, charW: 0.58, lines: 2 },
    impact: { maxWords: 1, base: 210, charW: 0.5, lines: 1 },
  };

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function norm(s) { return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^\w]/g, ""); }

  // Split words into on-screen groups: style word cap, break after sentence
  // punctuation and on pauses longer than 0.55 s.
  function groupWords(words, maxWords) {
    var groups = [], cur = [];
    for (var i = 0; i < words.length; i++) {
      var w = words[i];
      var prev = cur[cur.length - 1];
      if (cur.length && (cur.length >= maxWords || (w.start - prev.end) > 0.55)) { groups.push(cur); cur = []; }
      cur.push(w);
      if (/[.!?…:]$/.test(w.text) && maxWords > 1) { groups.push(cur); cur = []; }
    }
    if (cur.length) groups.push(cur);
    return groups;
  }

  // u = frame width / 1080: every size is authored for a 1080-wide frame.
  function fitSize(text, longest, cfg, avail, u) {
    var perLine = Math.max(longest, Math.ceil(text.length / cfg.lines));
    var size = Math.floor(avail / (perLine * cfg.charW));
    return clamp(size, Math.round(cfg.base * 0.5 * u), Math.round(cfg.base * u));
  }

  function layoutVars(layer, o) {
    var H = o.height, W = o.width;
    var s = layer.style;
    // Safe zones for TikTok/Reels/Shorts: keep clear of the bottom ~22 %
    // (caption/handle) and the right ~12 % (action buttons).
    s.setProperty("--ck-pad-l", Math.round(W * 0.085) + "px");
    s.setProperty("--ck-pad-r", Math.round(W * 0.13) + "px");
    s.setProperty("--ck-cap-y", Math.round(H * 0.66) + "px");
    s.setProperty("--ck-slam-y", Math.round(H * 0.5) + "px");
    s.setProperty("--ck-hook-y", Math.round(H * 0.24) + "px");
    s.setProperty("--ck-card-y", Math.round(H * 0.12) + "px");
    s.setProperty("--ck-cta-y", Math.round(H * 0.4) + "px");
    s.setProperty("--ck-accent", o.accent || "#ff2d55");
    s.setProperty("--ck-accent-ink", o.accentInk || "#ffffff");
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---------------------------------------------------------------- captions
  function captions(tl, layer, o, avail) {
    var style = o.caption || "highlight";
    if (style === "none" || !STYLE[style] || !(o.words && o.words.length)) return;
    var cfg = STYLE[style];
    var keys = (o.keywords || []).map(norm);
    var wrap = el("div", "ck-caps ck-cap-" + style);
    layer.appendChild(wrap);
    var groups = groupWords(o.words, cfg.maxWords);

    groups.forEach(function (g, gi) {
      var next = groups[gi + 1];
      var gStart = g[0].start;
      var gEnd = Math.min(g[g.length - 1].end + 0.35, next ? next[0].start - 0.04 : o.duration);
      var text = g.map(function (w) { return w.text; }).join(" ");
      var longest = g.reduce(function (m, w) { return Math.max(m, w.text.length); }, 0);
      var size = fitSize(text, longest, cfg, avail, o.u);

      var grp = el("div", "ck-group");
      grp.style.fontSize = size + "px";
      var wordEls = g.map(function (w) {
        var wd = el("span", "ck-word");
        if (style === "highlight") wd.appendChild(el("span", "ck-bg"));
        wd.appendChild(document.createTextNode(w.text));
        if (keys.indexOf(norm(w.text)) >= 0 || (style === "impact" && w.text.length >= 8)) wd.classList.add("ck-key");
        grp.appendChild(wd);
        return wd;
      });
      wrap.appendChild(grp);

      tl.set(grp, { visibility: "visible" }, gStart);
      if (style === "impact") {
        var dir = [[0, 120], [-160, 0], [0, -120], [160, 0]][gi % 4].map(function (v) { return v * o.u; });
        tl.fromTo(grp, { opacity: 0, x: dir[0], y: dir[1], scale: 1.35 },
          { opacity: 1, x: 0, y: 0, scale: 1, duration: 0.16, ease: "power4.out" }, gStart);
      } else {
        tl.fromTo(grp, { opacity: 0, y: 18 * o.u }, { opacity: 1, y: 0, duration: 0.14, ease: "power2.out" }, gStart);
      }

      g.forEach(function (w, i) {
        var wd = wordEls[i];
        if (style === "highlight") {
          var bg = wd.firstChild;
          tl.to(bg, { opacity: 1, scaleX: 1, duration: 0.14, ease: "power2.out" }, w.start);
          // text takes the accent's ink while its box is up (white on yellow is unreadable)
          tl.to(wd, { color: o.accentInk || "#ffffff", textShadow: "0 0 0 rgba(0,0,0,0)", duration: 0.08 }, w.start);
          tl.to(wd, { color: "#ffffff", textShadow: "0 6px 18px rgba(0,0,0,.55), 0 0 2px rgba(0,0,0,.9)", duration: 0.08 }, Math.max(w.start + 0.15, w.end));
          tl.fromTo(wd, { scale: 1 }, { scale: 1.08, duration: 0.08, ease: "power2.out", yoyo: true, repeat: 1 }, w.start);
          tl.to(bg, { opacity: 0, duration: 0.1, ease: "power2.in" }, Math.max(w.start + 0.15, w.end));
          tl.set(bg, { scaleX: 0 }, Math.max(w.start + 0.26, w.end + 0.1));
        } else if (style === "karaoke") {
          var on = wd.classList.contains("ck-key") ? (o.accent || "#ff2d55") : "#111111";
          tl.to(wd, { color: on, duration: 0.08, ease: "none" }, w.start);
        } else if (style === "clean") {
          tl.to(wd, { clipPath: "inset(0 0% 0 0)", duration: clamp(w.end - w.start, 0.12, 0.3), ease: "power2.out" }, w.start);
        }
      });

      tl.to(grp, { opacity: 0, duration: 0.1, ease: "power2.in" }, Math.max(gStart + 0.2, gEnd - 0.1));
      tl.set(grp, { visibility: "hidden" }, Math.max(gStart + 0.3, gEnd));
    });
  }

  // ---------------------------------------------------------------- intros
  function intro(tl, layer, o, avail) {
    var it = o.intro || {};
    var style = it.style || "none";
    var text = (it.text || "").trim();
    if (style === "none" || !text) return;
    var d = clamp(Number(it.duration) || 2.4, 1.2, 4);

    if (style === "hook") {
      var shade = el("div", "ck-shade");
      var hook = el("div", "ck-hook");
      var words = text.split(/\s+/);
      var size = clamp(Math.floor(avail / (Math.max(10, Math.ceil(text.length / 3)) * 0.7)), 54 * o.u, 104 * o.u);
      hook.style.fontSize = size + "px";
      var spans = words.map(function (w, i) {
        var s = el("span", null, w);
        hook.appendChild(s);
        if (i < words.length - 1) hook.appendChild(document.createTextNode(" "));
        return s;
      });
      var rule = el("span", "ck-hook-rule");
      hook.appendChild(rule);
      layer.appendChild(shade);
      layer.appendChild(hook);
      tl.to(shade, { opacity: 1, duration: 0.25, ease: "power2.out" }, 0);
      tl.fromTo(spans, { opacity: 0, y: 14, filter: "blur(12px)" },
        { opacity: 1, y: 0, filter: "blur(0px)", duration: 0.36, stagger: 0.05, ease: "power3.out" }, 0.05);
      tl.to(rule, { scaleX: 1, duration: 0.4, ease: "power3.out" }, 0.25 + words.length * 0.05);
      tl.to([hook, shade], { opacity: 0, duration: 0.3, ease: "power2.in" }, d - 0.3);
    } else if (style === "letters") {
      var box = el("div", "ck-letters");
      var lw = text.split(/\s+/);
      var longest = lw.reduce(function (m, w) { return Math.max(m, w.length); }, 0);
      box.style.fontSize = clamp(Math.floor(avail / (Math.max(longest, Math.ceil(text.length / 3)) * 0.52)), 70 * o.u, 180 * o.u) + "px";
      var keys = (o.keywords || []).map(norm);
      var chars = [];
      lw.forEach(function (w) {
        var wd = el("span", "ck-lw");
        if (keys.indexOf(norm(w)) >= 0) wd.classList.add("ck-key");
        w.split("").forEach(function (c) { var ch = el("span", "ck-ch", c); wd.appendChild(ch); chars.push(ch); });
        box.appendChild(wd);
      });
      var dim = el("div", "ck-shade");
      layer.appendChild(dim);
      layer.appendChild(box);
      tl.to(dim, { opacity: 1, duration: 0.2 }, 0);
      tl.to(chars, { y: "0%", duration: 0.42, stagger: 0.022, ease: "power4.out" }, 0.05);
      tl.to([box, dim], { opacity: 0, duration: 0.3, ease: "power2.in" }, d - 0.3);
    } else if (style === "title-card") {
      var card = el("div", "ck-card");
      var tab = el("div", "ck-card-tab");
      var body = el("div", "ck-card-body");
      var title = el("span", "ck-card-title", text);
      title.style.fontSize = clamp(Math.floor(avail * 0.9 / (Math.max(12, Math.ceil(text.length / 2)) * 0.62)), 48 * o.u, 68 * o.u) + "px";
      body.appendChild(title);
      var sub = null;
      if (it.sub) { sub = el("span", "ck-card-sub", it.sub); sub.style.fontSize = Math.round(30 * o.u) + "px"; body.appendChild(sub); }
      card.appendChild(tab);
      card.appendChild(body);
      layer.appendChild(card);
      d = Math.max(d, 2.8);
      tl.to(card, { clipPath: "inset(0 0% 0 0)", duration: 0.55, ease: "power3.out" }, 0.1);
      tl.to(tab, { scaleY: 1, duration: 0.45, ease: "power2.out" }, 0.28);
      tl.to(title, { y: 0, opacity: 1, duration: 0.5, ease: "power3.out" }, 0.34);
      if (sub) tl.to(sub, { y: 0, opacity: 1, duration: 0.5, ease: "power3.out" }, 0.44);
      tl.to(card, { y: 18, opacity: 0, duration: 0.35, ease: "power2.in" }, d - 0.35);
    }
  }

  // ---------------------------------------------------------------- outro
  function outro(tl, layer, o) {
    var ot = o.outro || {};
    if (ot.style !== "cta" || !ot.text) return;
    var d = clamp(Number(ot.duration) || 2.2, 1.2, 4);
    var t0 = Math.max(0, o.duration - d);
    var pill = el("div", "ck-cta");
    pill.style.fontSize = clamp(Math.floor(o.width * 0.8 / (ot.text.length * 0.66)), 36 * o.u, 64 * o.u) + "px";
    pill.appendChild(document.createTextNode(ot.text));
    pill.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>');
    layer.appendChild(pill);
    tl.to(pill, { opacity: 1, scale: 1, duration: 0.3, ease: "back.out(2)" }, t0);
    tl.to(pill, { x: 6, duration: 0.35, ease: "sine.inOut", yoyo: true, repeat: 3 }, t0 + 0.4);
  }

  function mount(tl, layer, o) {
    o = o || {};
    o.width = o.width || 1080;
    o.height = o.height || 1920;
    o.duration = o.duration || 10;
    o.u = o.width / 1080;
    layer.classList.add("ck-layer");
    layer.style.setProperty("--ck-u", o.u);
    layoutVars(layer, o);
    var avail = o.width * (1 - 0.085 - 0.13);
    intro(tl, layer, o, avail);
    captions(tl, layer, o, avail);
    outro(tl, layer, o);
    return tl;
  }

  window.ClipKit = { mount: mount, groupWords: groupWords, STYLES: Object.keys(STYLE) };
})();
