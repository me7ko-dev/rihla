(function () {
'use strict';
var $ = function (s) { return document.querySelector(s); };
var form = $('#form'), map = null, layer = null, markers = {}, dayLayers = [];
var ICON = { sight: '🏛️', meal: '🍽️', prayer: '🕌', rest: '☕' };
var LEVEL = { verified: 'Listed halal', likely: 'Likely halal', unknown: 'Ask about halal' };
var DAYCOL = ['#0F4C45', '#C2643F', '#2F6FB3', '#7A4FB3'];

function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

// start date: one week from today
var d = new Date(Date.now() + 7 * 864e5);
form.start.value = d.toISOString().slice(0, 10);
document.querySelectorAll('.quick button').forEach(function (b) {
  b.addEventListener('click', function () { form.destination.value = b.dataset.dest; form.destination.focus(); });
});

// example plans, made earlier with the same agent and live Qloo data: shown instantly
document.querySelectorAll('.examples button').forEach(function (b) {
  b.addEventListener('click', function () {
    $('#err').hidden = true;
    fetch('/static/examples/' + b.dataset.ex + '.json').then(function (r) { if (!r.ok) throw new Error('Example not found'); return r.json(); })
      .then(function (plan) {
        var ex = plan.example || {};
        form.destination.value = ex.destination || ''; form.travellers.value = ex.travellers || '';
        form.tastes.value = ex.tastes || ''; form.days.value = String(ex.days || plan.days.length);
        render(plan);
      })
      .catch(function (err) { $('#err').textContent = err.message; $('#err').hidden = false; });
  });
});

form.addEventListener('submit', function (e) {
  e.preventDefault();
  var body = {
    destination: form.destination.value.trim(), days: +form.days.value, start: form.start.value || null,
    travellers: form.travellers.value.trim(), tastes: form.tastes.value.trim(), language: form.language.value
  };
  $('#err').hidden = true;
  $('#loading').hidden = false;
  form.querySelector('.go').disabled = true;
  var list = $('#steps'); list.innerHTML = '';
  var started = Date.now(), timer = setInterval(function () { $('#elapsed').textContent = Math.round((Date.now() - started) / 1000) + ' s'; }, 1000);
  // steps with an id are tool calls that run in parallel: each one is ticked off when its result arrives
  function step(text, id) {
    list.querySelectorAll('li.now:not([data-id])').forEach(function (p) { p.classList.remove('now'); p.classList.add('ok'); });
    var li = document.createElement('li'); li.className = 'now'; li.textContent = text;
    if (id) li.dataset.id = id;
    list.appendChild(li);
    while (list.children.length > 8) list.removeChild(list.firstChild);
  }
  function found(id, n) {
    var li = id ? list.querySelector('li[data-id="' + CSS.escape(id) + '"]') : list.querySelector('li.now');
    if (!li) return;
    li.classList.remove('now'); li.classList.add('ok');
    if (n != null) li.dataset.n = n;
  }
  function finish() { clearInterval(timer); $('#loading').hidden = true; form.querySelector('.go').disabled = false; }
  // the plan arrives as a stream of server-sent events: steps first, then the plan
  fetch('/api/plan/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) {
      if (!r.ok || !r.body) return r.json().then(function (j) { throw new Error(j.detail || 'Something went wrong'); });
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
      function pump() {
        return reader.read().then(function (x) {
          if (x.done) throw new Error('The connection closed before the plan was ready. Please try again.');
          buf += dec.decode(x.value, { stream: true });
          var parts = buf.split('\n\n'); buf = parts.pop();
          for (var i = 0; i < parts.length; i++) {
            var line = parts[i].split('\n').filter(function (l) { return l.indexOf('data: ') === 0; })[0];
            if (!line) continue;
            var ev = JSON.parse(line.slice(6));
            if (ev.type === 'step') step(ev.text, ev.id);
            else if (ev.type === 'found') found(ev.id, ev.n);
            else if (ev.type === 'error') throw new Error(ev.detail);
            else if (ev.type === 'done') { reader.cancel(); return ev.plan; }
          }
          return pump();
        });
      }
      return pump();
    })
    .then(function (plan) { finish(); render(plan); })
    .catch(function (err) { finish(); $('#err').textContent = err.message; $('#err').hidden = false; });
});

function render(plan) {
  $('#result').hidden = false;
  var n = 0, stops = 0, meals = 0, verified = 0;
  plan.days.forEach(function (d) { d.stops.forEach(function (s) { stops++; if (s.kind === 'meal') { meals++; if (s.halal_level === 'verified') verified++; } }); });
  $('#sum').innerHTML = (plan.example ? '<p class="exnote">Example plan, made with the same agent and live Qloo data. Change anything above and press “Plan my trip” for your own.</p>' : '') +
    '<h2>' + esc(plan.title) + '</h2><p>' + esc(plan.summary) + '</p><div class="meta">' +
    '<span>📍 ' + esc((plan.destination.name || '').split(',').slice(0, 3).join(',')) + '</span>' +
    '<span>' + plan.days.length + (plan.days.length === 1 ? ' day' : ' days') + ' · ' + stops + ' stops</span>' +
    '<span>🍽️ ' + meals + ' halal-aware meals' + (verified ? ' (' + verified + ' listed halal)' : '') + '</span>' +
    (plan.seconds ? '<span>⏱️ planned in ' + plan.seconds + ' s</span>' : '') + '</div>' +
    ((plan.signals && plan.signals.length) || (plan.audiences && plan.audiences.length) ?
      '<div class="taste"><b>Your Qloo taste profile</b>' +
      (plan.signals || []).map(function (x) { return '<span class="sig">' + esc(x) + '</span>'; }).join('') +
      (plan.audiences || []).map(function (x) { return '<span class="aud">' + esc(x) + '</span>'; }).join('') + '</div>' : '');
  var h = '';
  plan.days.forEach(function (day, di) {
    h += '<article class="card day"><div class="day-h"><div><h3>Day ' + day.day + ' · ' + esc(day.theme) + '</h3>' +
      '<div class="date">' + esc(fmtDate(day.date)) + (day.hijri ? ' · ' + esc(day.hijri) + ' AH' : '') + '</div></div></div>';
    if (day.prayer_times) {
      h += '<div class="ptimes">' + ['Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha'].map(function (p) {
        return day.prayer_times[p] ? '<span>' + p + '<b>' + esc(day.prayer_times[p]) + '</b></span>' : '';
      }).join('') + '</div>';
    }
    h += '<ol class="stops">';
    day.stops.forEach(function (s, si) {
      var id = di + '-' + si;
      var badges = '';
      if (s.kind === 'meal' && s.halal_level) badges += '<span class="badge ' + s.halal_level + '" title="' + esc(s.halal_reason) + '">' + LEVEL[s.halal_level] + '</span>';
      if (s.because && s.because.length) badges += '<span class="badge src">Because you love ' + esc(s.because.join(' & ')) + '</span>';
      else if (s.source === 'qloo' && s.affinity && s.kind !== 'prayer') badges += '<span class="badge src">Taste match · ' + Math.round(s.affinity * 100) + '%</span>';
      if (s.topic && s.topic.length) badges += '<span class="badge topic">Known for ' + esc(s.topic.join(' & ')) + '</span>';
      if (s.popular && s.kind === 'sight') badges += '<span class="badge must">Must-see</span>';
      if (s.kids_ok && s.kind !== 'prayer') badges += '<span class="badge kids">Good for kids</span>';
      if (s.open_today) badges += '<span class="badge k" title="Opening hours that day (Qloo)">Open ' + esc(s.open_today) + '</span>';
      if (s.alcohol && s.kind === 'meal') badges += '<span class="badge alc" title="Qloo lists alcohol at this place">Serves alcohol</span>';
      if (s.cuisine) badges += '<span class="badge k">' + esc(s.cuisine) + '</span>';
      else if (s.categories && s.categories.length && s.kind !== 'prayer') badges += '<span class="badge k">' + esc(s.categories[0]) + '</span>';
      var img = s.image ? '<img class="thumb" src="' + esc(s.image) + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">' : '';
      h += '<li class="stop ' + esc(s.kind) + '" data-id="' + id + '"><div class="t">' + esc(s.time) + '</div><div class="dot">' + (si + 1) + '</div>' +
        '<div class="body"><div class="txt"><h4>' + (ICON[s.kind] || '') + ' ' + esc(s.name) + '</h4><p>' + esc(s.why) + '</p>' +
        (badges ? '<div class="badges">' + badges + '</div>' : '') + '</div>' + img + '</div></li>';
    });
    h += '</ol></article>';
  });
  if (plan.tips && plan.tips.length) h += '<section class="card tips"><h3>Good to know</h3><ul>' + plan.tips.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul></section>';
  $('#days').innerHTML = h;
  $('#trace ol').innerHTML = (plan.trace || []).map(function (t) {
    var a = t.args || {}; var what = a.query || a.category || (typeof a.lat === 'number' ? a.lat.toFixed(3) + ', ' + a.lon.toFixed(3) : '');
    return '<li><code>' + esc(t.tool) + '</code> ' + esc(what) + (t.note ? ' → ' + esc(t.note) : ' → ' + t.found + ' found') + '</li>';
  }).join('');
  drawMap(plan);
  document.querySelectorAll('.stop').forEach(function (el) {
    el.addEventListener('click', function () {
      var m = markers[el.dataset.id]; if (!m) return;
      var d = +el.dataset.id.split('-')[0];
      document.querySelectorAll('#daybar button').forEach(function (x) { x.classList.toggle('on', +x.dataset.d === d); });
      showDay(d); map.setView(m.getLatLng(), 16); m.openPopup();
    });
  });
  $('#result').scrollIntoView({ behavior: 'smooth' });
}

function showDay(d) {
  var pts = [];
  dayLayers.forEach(function (dl, i) {
    var on = d < 0 || i === d;
    if (on) { dl.addTo(layer); dl.eachLayer(function (m) { if (m.getLatLng) pts.push(m.getLatLng()); }); } else dl.remove();
  });
  if (pts.length) map.fitBounds(pts, { padding: [30, 30] });
}

function fmtDate(iso) {
  if (!iso) return '';
  var d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
}

function drawMap(plan) {
  if (!map) {
    map = L.map('map', { scrollWheelZoom: false });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 19
    }).addTo(map);
  }
  if (layer) layer.remove();
  layer = L.layerGroup().addTo(map); markers = {}; dayLayers = [];
  var pts = [];
  plan.days.forEach(function (day, di) {
    var line = [], dl = L.layerGroup().addTo(layer); dayLayers.push(dl);
    day.stops.forEach(function (s, si) {
      if (s.lat == null) return;
      var col = s.kind === 'prayer' ? '#0F7A5C' : s.kind === 'meal' ? '#C2643F' : '#2F6FB3';
      var icon = L.divIcon({ className: '', html: '<div class="num-icon" style="background:' + col + '">' + (si + 1) + '</div>', iconSize: [26, 26], iconAnchor: [13, 13] });
      var m = L.marker([s.lat, s.lon], { icon: icon }).addTo(dl)
        .bindPopup('<b>Day ' + day.day + ' · ' + esc(s.time) + '</b><br>' + esc(s.name) + (s.halal_level && s.kind === 'meal' ? '<br><i>' + LEVEL[s.halal_level] + '</i>' : ''));
      markers[di + '-' + si] = m; pts.push([s.lat, s.lon]); line.push([s.lat, s.lon]);
    });
    if (line.length > 1) L.polyline(line, { color: DAYCOL[di % DAYCOL.length], weight: 3, opacity: .55, dashArray: '6 6' }).addTo(dl);
  });
  // day buttons above the map
  var bar = $('#daybar');
  bar.innerHTML = '<button class="on" data-d="-1">All days</button>' + plan.days.map(function (d, i) { return '<button data-d="' + i + '">Day ' + d.day + '</button>'; }).join('');
  bar.onclick = function (e) {
    var b = e.target.closest('button'); if (!b) return;
    bar.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
    showDay(+b.dataset.d);
  };
  if (pts.length) map.fitBounds(pts, { padding: [30, 30] }); else map.setView([plan.destination.lat, plan.destination.lon], 13);
  setTimeout(function () { map.invalidateSize(); }, 50);
}
})();
