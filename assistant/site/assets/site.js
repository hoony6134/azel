// Azel PR page: language and theme toggles, and the hero moon jar (DESIGN.md §6.2–6.3) acting out a short call.
(function () {
  'use strict';
  var root = document.documentElement;
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  var lang = 'ko';

  // ---- language: Korean is in the HTML; English rides in data-en / data-en-label
  var txt = Array.prototype.slice.call(document.querySelectorAll('[data-en]'));
  var lab = Array.prototype.slice.call(document.querySelectorAll('[data-en-label]'));
  txt.forEach(function (el) { el.dataset.ko = el.textContent; });
  lab.forEach(function (el) { el.dataset.koLabel = el.getAttribute('aria-label') || ''; });
  var meta = { title: document.title, desc: document.querySelector('meta[name="description"]').content };
  var EN = { title: 'Azel — An assistant that answers when you call',
    desc: 'Azel is a Korean voice assistant you call like a person. A personal project.' };
  function setLang(l) {
    lang = l;
    root.lang = l;
    txt.forEach(function (el) { el.textContent = l === 'en' ? el.dataset.en : el.dataset.ko; });
    lab.forEach(function (el) { el.setAttribute('aria-label', l === 'en' ? el.dataset.enLabel : el.dataset.koLabel); });
    document.title = l === 'en' ? EN.title : meta.title;
    document.querySelector('meta[name="description"]').content = l === 'en' ? EN.desc : meta.desc;
    themeLabel();
    if (call) call.render();
  }

  // ---- theme: follows the OS until the visitor picks one
  var mq = matchMedia('(prefers-color-scheme: light)');
  function scheme() { return root.dataset.theme || (mq.matches ? 'light' : 'dark'); }
  function themeLabel() {
    var b = document.getElementById('theme');
    var toLight = scheme() === 'dark';
    var ko = toLight ? '밝은 화면으로' : '어두운 화면으로', en = toLight ? 'Switch to light' : 'Switch to dark';
    b.setAttribute('aria-label', lang === 'en' ? en : ko);
    root.dataset.scheme = scheme();
  }

  var tools = document.querySelector('.tools');
  tools.hidden = false;
  document.getElementById('lang').addEventListener('click', function () {
    var l = lang === 'en' ? 'ko' : 'en';
    store.set('azel-lang', l);
    setLang(l);
  });
  document.getElementById('theme').addEventListener('click', function () {
    var t = scheme() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = t;
    store.set('azel-theme', t);
    themeLabel();
    jar.recolor();
  });
  mq.addEventListener && mq.addEventListener('change', function () { themeLabel(); jar.recolor(); });

  var top = document.querySelector('.top');
  addEventListener('scroll', function () { top.classList.toggle('scrolled', scrollY > 8); }, { passive: true });

  // ---- moon jar
  var reduce = matchMedia('(prefers-reduced-motion: reduce)');
  var canvas = document.getElementById('jar');
  var jar = (function () {
    var ctx = canvas.getContext('2d'), size = 0, colors = null;
    var j = { state: 'idle', smooth: 0, shimAt: 3000 };
    function css(n) { return getComputedStyle(root).getPropertyValue(n).trim(); }
    function recolor() { colors = null; j.drawn = false; }
    function fit() {
      var dpr = Math.min(devicePixelRatio || 1, 2), w = canvas.clientWidth || 320;
      if (w * dpr === size) return;
      size = w * dpr; canvas.width = canvas.height = size;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); j.w = w; j.drawn = false;
    }
    function jarR(th) { return 1 - 0.06 * Math.sin(th) - 0.035 * Math.cos(2 * th) + 0.015 * Math.sin(3 * th + 0.6); }
    function wobble(s, th, t, L) {
      if (s === 'listening') return L * (0.05 * Math.sin(3 * th + 2.1 * t) + 0.03 * Math.sin(5 * th - 3.3 * t));
      if (s === 'thinking') return 0.012 * Math.sin(2 * th + 1.2 * t);
      if (s === 'speaking') return L * (0.07 * Math.sin(2 * th + 2.8 * t) + 0.025 * Math.sin(7 * th + 5 * t));
      return 0.018 * Math.sin(2 * th + 0.45 * t) + 0.010 * Math.sin(3 * th - 0.31 * t + 1.3);
    }
    function level(s, t) { // simulated speech envelope: syllable bumps inside phrases
      if (s !== 'listening' && s !== 'speaking') return 0;
      var syl = Math.max(0, Math.sin(t * (s === 'speaking' ? 9 : 7)));
      var phrase = Math.max(0, Math.sin(t * 0.9) + 0.35);
      return Math.min(1, 0.15 + 0.8 * syl * phrase);
    }
    function path(cx, cy, R, s, t, L, sy) {
      ctx.beginPath();
      for (var i = 0; i <= 120; i++) {
        var th = i / 120 * Math.PI * 2, r = R * jarR(th) * (1 + wobble(s, th, t, L));
        var x = cx + r * Math.cos(th), y = cy + r * Math.sin(th) * (sy || 1);
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      }
      ctx.closePath();
    }
    function draw(now) {
      fit();
      var still = reduce.matches, s = j.state;
      if (still && j.drawn) return;
      j.drawn = true;
      if (!colors) colors = { cel: css('--glazeCel'), ind: css('--glazeInd'), por: css('--porcelain'), amb: css('--glazeAmb') };
      var c = colors, w = j.w, t = still ? 0 : now / 1000;
      var breath = (s === 'idle' && !still) ? Math.sin(2 * Math.PI * t / 4.6) : 0;
      var target = still ? 0 : level(s, t);
      j.smooth += (target - j.smooth) * (target > j.smooth ? 0.55 : 0.12);
      var L = j.smooth, water = s === 'speaking';
      var cx = w / 2, cy = water ? w * 0.44 : w / 2, R = w * (water ? 0.22 : 0.245) * (1 + 0.025 * breath);  // room inside the canvas for glow and ring
      var look = {
        idle: [c.cel, c.por, c.cel, 0.35 + 0.12 * breath],
        listening: [c.cel, c.por, c.cel, 0.4 + 0.4 * L],
        thinking: [c.ind, c.por, c.ind, 0.45 + (still ? 0 : 0.2 * Math.sin(t * 4.5))],
        speaking: [c.amb, c.por, c.por, 0.45 + 0.45 * L]
      }[s];
      ctx.clearRect(0, 0, w, w);
      ctx.save(); ctx.filter = 'blur(' + Math.round(R * 0.35) + 'px)'; ctx.globalAlpha = look[3];
      path(cx, cy, R * (1.05 + (water ? 0.25 : 0.08) * L + 0.04 * breath), s, t, L); ctx.fillStyle = look[0]; ctx.fill(); ctx.restore();
      if (s === 'listening') {
        ctx.save(); ctx.globalAlpha = 0.25 + 0.5 * L; ctx.strokeStyle = c.cel; ctx.lineWidth = 1.2;
        path(cx, cy, R * (1.12 + 0.1 * L), s, t, L); ctx.stroke(); ctx.restore();
      }
      var g = ctx.createRadialGradient(cx - R * 0.2, cy - R * 0.35, 0, cx, cy, R * 1.25);
      g.addColorStop(0, look[1]); g.addColorStop(0.55, look[2]); g.addColorStop(1, look[0]);
      path(cx, cy, R, s, t, L); ctx.fillStyle = g; ctx.fill();
      if (s === 'idle' && !still) { // a soft highlight crosses the porcelain every 8–12 s
        var p = (now - j.shimAt) / 1600;
        if (p >= 1) j.shimAt = now + 8000 + Math.random() * 4000;
        else if (p >= 0) {
          var e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
          var sx = cx - 0.7 * R + 1.4 * R * e, sy = cy - 0.45 * R + 0.3 * R * e;
          ctx.save(); path(cx, cy, R, s, t, L); ctx.clip();
          var sg = ctx.createRadialGradient(sx, sy, 0, sx, sy, 0.45 * R);
          sg.addColorStop(0, 'rgba(255,255,255,' + (0.18 * Math.sin(Math.PI * p)) + ')'); sg.addColorStop(1, 'rgba(255,255,255,0)');
          ctx.fillStyle = sg; ctx.fillRect(0, 0, w, w); ctx.restore();
        }
      }
      if (s === 'thinking' && ctx.createConicGradient) {
        ctx.save(); path(cx, cy, R, s, t, L); ctx.clip();
        var cg = ctx.createConicGradient(t * Math.PI / 2, cx, cy);
        cg.addColorStop(0, 'rgba(0,0,0,0)'); cg.addColorStop(0.35, c.ind); cg.addColorStop(0.6, 'rgba(0,0,0,0)'); cg.addColorStop(0.85, c.por); cg.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.filter = 'blur(' + Math.round(R * 0.18) + 'px)'; ctx.globalAlpha = 0.4; ctx.fillStyle = cg; ctx.fillRect(0, 0, w, w); ctx.restore();
      }
      if (water) { // the water-line reflection under the jar while it speaks
        ctx.save(); ctx.filter = 'blur(8px)'; ctx.globalAlpha = 0.3;
        path(cx, cy + R * 1.6, R * (0.8 + 0.4 * L), s, t, L, -0.25); ctx.fillStyle = c.amb; ctx.fill(); ctx.restore();
      }
    }
    return { j: j, draw: draw, recolor: recolor, set: function (s) { j.state = s; j.drawn = false; } };
  })();

  // ---- the sample call: listening → thinking → speaking, three short exchanges in turn
  var stage = document.querySelector('.stage');
  var meEl = document.getElementById('me'), aiEl = document.getElementById('ai'), word = document.getElementById('state-word');
  var WORDS = { idle: ['기다리는 중', 'Waiting'], listening: ['듣고 있어요', 'Listening'], thinking: ['생각 중', 'Thinking'], speaking: ['말하는 중', 'Speaking'] };
  var TALKS = [
    [['내일 아침 일곱 시에 깨워 줘', 'Wake me up at seven tomorrow.'], ['내일 오전 7시에 전화할게요.', "Okay, I'll call you at 7:00 tomorrow morning."]],
    [['오늘 뭐 있어?', "What's on today?"], ['10시 랩 미팅, 오후 2시에 과제 마감이 있어요.', 'Lab meeting at ten, and an assignment due at two.']],
    [['맥북에서 테스트 돌려 줘', 'Run the tests on the MacBook.'], ['돌리기 전에 iPhone으로 승인 요청 보냈어요.', 'I sent an approval to your iPhone before running them.']]
  ];
  var call = {
    i: 0, phase: 'listening', t0: 0, meOn: false, aiOn: false,
    render: function () {
      var k = lang === 'en' ? 1 : 0, talk = TALKS[this.i];
      stage.dataset.state = this.phase;
      word.textContent = WORDS[this.phase][k];
      meEl.textContent = talk[0][k]; aiEl.textContent = talk[1][k];
      meEl.classList.toggle('off', !this.meOn); aiEl.classList.toggle('off', !this.aiOn);
      jar.set(this.phase);
    },
    tick: function (now) {
      var e = now - this.t0, p = this.phase, me = this.meOn, ai = this.aiOn;
      // timeline (ms): user line at 500, thinking at 2900, speaking + reply at 4300, clear at 9800, next at 10400
      if (e < 500) { p = 'listening'; me = false; ai = false; }
      else if (e < 2900) { p = 'listening'; me = true; }
      else if (e < 4300) { p = 'thinking'; me = true; }
      else if (e < 9800) { p = 'speaking'; me = true; ai = true; }
      else if (e < 10400) { p = 'listening'; me = false; ai = false; }
      else { this.i = (this.i + 1) % TALKS.length; this.t0 = now; return this.tick(now); }
      if (p !== this.phase || me !== this.meOn || ai !== this.aiOn) { this.phase = p; this.meOn = me; this.aiOn = ai; this.render(); }
    }
  };

  var visible = true, running = false;
  function frame(now) {
    if (!visible || document.hidden || reduce.matches) { running = false; return; }
    call.tick(now); jar.draw(now);
    requestAnimationFrame(frame);
  }
  function start() {
    if (reduce.matches) { // still: the finished exchange, one static jar
      call.phase = 'speaking'; call.meOn = call.aiOn = true; call.render(); jar.set('idle'); jar.draw(0); return;
    }
    if (!running) { running = true; if (!call.t0) call.t0 = performance.now(); requestAnimationFrame(frame); }
  }
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { visible = es[0].isIntersecting; if (visible) start(); }).observe(stage);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });
  reduce.addEventListener && reduce.addEventListener('change', function () { jar.recolor(); start(); });
  addEventListener('resize', function () { if (reduce.matches) { jar.recolor(); jar.draw(0); } });

  setLang(root.getAttribute('data-lang-pending') === 'en' ? 'en' : 'ko');
  themeLabel();
  start();
})();
