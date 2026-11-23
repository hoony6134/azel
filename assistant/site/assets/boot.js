// Runs before first paint: theme and language the visitor picked last time (no flash), and the JS flag.
(function () {
  var d = document.documentElement, t = null, l = null;
  d.classList.add('js');
  try { t = localStorage.getItem('azel-theme'); l = localStorage.getItem('azel-lang'); } catch (e) {}
  var q = /[?&]theme=(light|dark)/.exec(location.search); if (q) t = q[1];  // for screenshots
  if (t === 'light' || t === 'dark') d.setAttribute('data-theme', t);
  if (!l && !/^ko\b/i.test(navigator.language || 'ko')) l = 'en';
  if (l === 'en') d.setAttribute('data-lang-pending', 'en');
})();
