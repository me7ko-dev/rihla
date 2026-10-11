(function () {
'use strict';
var $ = function (s) { return document.querySelector(s); };
var form = $('#form'), map = null, layer = null, markers = {}, dayLayers = [];
var ICON = { sight: '🏛️', meal: '🍽️', prayer: '🕌', rest: '☕' };
var LEVEL = { verified: 'Listed halal', likely: 'Likely halal', unknown: 'Ask about halal' };
var DAYCOL = ['#0F4C45', '#C2643F', '#2F6FB3', '#7A4FB3'];
// Apple Maps only where it is the phone's own map app (iPhone, iPad, Mac); Google Maps everywhere
var IS_APPLE = /iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent);
// on foot up to `km` of streets at `pace` km a minute (4.5 km/h); young children walk slower (1.8 km takes them
// at least 30 minutes), so their walks stop at 1.2 km — the server plans the day with the same numbers
var WALK = { adults: { km: 1.8, pace: 0.075 }, kids: { km: 1.2, pace: 0.06 } };
// the server adds this Qloo audience when a child is 12 or younger, or there is a baby
function youngKids(plan) { return (plan.audiences || []).indexOf('Parents with young children') >= 0; }

function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

// directions links: the map apps do the turn-by-turn; without a start point they start from where the phone is
function ll(p) { return (+p.lat).toFixed(6) + ',' + (+p.lon).toFixed(6); }
function mapsUrl(app, to, mode, from) {
  if (app === 'apple') return 'https://maps.apple.com/?daddr=' + ll(to) + (from ? '&saddr=' + ll(from) : '') + (mode ? '&dirflg=' + (mode === 'walk' ? 'w' : 'r') : '');
  return 'https://www.google.com/maps/dir/?api=1' + (from ? '&origin=' + ll(from) : '') + '&destination=' + ll(to) + (mode ? '&travelmode=' + (mode === 'walk' ? 'walking' : 'transit') : '');
}
function navLinks(s) {
  if (s.lat == null) return '';
  var link = function (app, label) {
    return '<a class="navbtn" href="' + esc(mapsUrl(app, s, s._mode)) + '" target="_blank" rel="noopener" aria-label="Directions to ' + esc(s.name) + ' in ' + label + '">' + label + '</a>';
  };
  return '<div class="nav"><span class="navlbl" aria-hidden="true">' + (s._mode === 'walk' ? '🚶' : s._mode === 'transit' ? '🚇' : '🧭') + ' Directions</span>' +
    (IS_APPLE ? link('apple', 'Apple Maps') : '') + link('google', 'Google Maps') + '</div>';
}

// trips are kept in this browser (localStorage), so the plan is still there on the day of the trip, even on weak internet
var TRIPS = 'rihla:trips', MAX_TRIPS = 5;
function loadTrips() {
  // only whole trips: a broken entry must never stop the page
  var ok = function (x) { return x && x.id && x.plan && x.plan.destination && Array.isArray(x.plan.days) && x.plan.days.length; };
  try { var t = JSON.parse(localStorage.getItem(TRIPS) || '[]'); return Array.isArray(t) ? t.filter(ok) : []; } catch (e) { return []; }
}
function storeTrips(t) {
  try { if (!t.length) return localStorage.removeItem(TRIPS); } catch (e) { return; }
  // storage full: the oldest trips go first (private mode may refuse everything)
  for (; t.length; t = t.slice(0, -1)) { try { localStorage.setItem(TRIPS, JSON.stringify(t)); return; } catch (e) { } }
}
function tripKey(p) { return (p.destination && p.destination.name) + '|' + (p.days[0] && p.days[0].date); }
function saveTrip(plan, req) {
  var p = {};
  Object.keys(plan).forEach(function (k) { if (k !== 'llm_log') p[k] = plan[k]; });
  var key = tripKey(p);  // the same place and dates planned again replaces the older plan
  storeTrips([{ id: Date.now().toString(36), saved: Date.now(), req: req, plan: p }]
    .concat(loadTrips().filter(function (x) { return tripKey(x.plan) !== key; })).slice(0, MAX_TRIPS));
  showTrips();
}
// the date and minute of the day where the trip is, whatever time zone the phone is in
function nowIn(tz) {
  var o = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }, f;
  try { f = new Intl.DateTimeFormat('en-US', tz ? Object.assign({ timeZone: tz }, o) : o); } catch (e) { f = new Intl.DateTimeFormat('en-US', o); }
  var v = {};
  f.formatToParts(new Date()).forEach(function (x) { v[x.type] = x.value; });
  return { date: v.year + '-' + v.month + '-' + v.day, mins: (+v.hour % 24) * 60 + +v.minute };
}
// {day: index of today's day} during the trip, {inDays: n} before it, {past: true} after it
function tripToday(plan) {
  var first = plan.days[0] && plan.days[0].date, today = nowIn(plan.timezone).date;
  if (!first) return {};
  for (var i = 0; i < plan.days.length; i++) if (plan.days[i].date === today) return { day: i };
  return today < first ? { inDays: Math.round((Date.parse(first) - Date.parse(today)) / 864e5) } : { past: true };
}
// "GMT+1" at that moment, for the phone (no tz) or for the trip
function gmt(tz, at) {
  try {
    var o = { timeZoneName: 'shortOffset' };
    if (tz) o.timeZone = tz;
    var x = new Intl.DateTimeFormat('en-US', o).formatToParts(at).filter(function (x) { return x.type === 'timeZoneName'; })[0];
    return x ? x.value : '';
  } catch (e) { return ''; }
}
function dateRange(p) {
  var a = p.days[0] && p.days[0].date, b = (p.days[p.days.length - 1] || {}).date || a;
  if (!a) return '';
  var f = function (s, o) { return new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', Object.assign({ timeZone: 'UTC' }, o)); };
  if (a === b) return f(a, { day: 'numeric', month: 'short' });
  return a.slice(0, 7) === b.slice(0, 7) ? f(a, { day: 'numeric' }) + '–' + f(b, { day: 'numeric', month: 'short' })
    : f(a, { day: 'numeric', month: 'short' }) + ' – ' + f(b, { day: 'numeric', month: 'short' });
}
function showTrips() {
  var box = $('#trips'), group = function (t) { return t.day != null ? 0 : t.inDays != null ? 1 : 2; };
  if (!box) return;  // an older cached page without the strip
  var list = loadTrips().map(function (x) { return { x: x, t: tripToday(x.plan) }; });
  // today's trip first, then the next ones, then the past ones (newest first)
  list.sort(function (a, b) { return group(a.t) - group(b.t) || (a.t.inDays || 0) - (b.t.inDays || 0) || b.x.saved - a.x.saved; });
  box.hidden = !list.length;
  box.innerHTML = list.length ? '<span class="trips-h">🧳 Saved on this device</span>' + list.map(function (o) {
    var p = o.x.plan, t = o.t, city = esc((o.x.req && o.x.req.destination) || String(p.destination.name || '').split(',')[0]);
    var when = t.day != null ? 'Day ' + p.days[t.day].day + ' is today' : t.inDays === 1 ? 'tomorrow' : t.inDays != null ? 'in ' + t.inDays + ' days' : '';
    return '<span class="trip' + (t.day != null ? ' now' : '') + '"><button type="button" data-id="' + esc(o.x.id) + '">' + city +
      (dateRange(p) ? ' · ' + esc(dateRange(p)) : '') + (when ? ' · <b>' + when + '</b>' : '') + '</button>' +
      '<button type="button" class="forget" data-forget="' + esc(o.x.id) + '" title="Forget this trip" aria-label="Forget the trip to ' + city + '">×</button></span>';
  }).join('') : '';
}
function openTrip(x) {
  var r = x.req || {};
  ['destination', 'travellers', 'tastes', 'language', 'start'].forEach(function (k) { if (r[k]) form[k].value = r[k]; });
  if (r.days) form.days.value = String(r.days);
  $('#err').hidden = true;
  render(x.plan, r.destination);
  // during the trip: straight to today, on the map and in the list
  var t = tripToday(x.plan);
  if (t.day != null) {
    var b = document.querySelector('#daybar button[data-d="' + t.day + '"]'), box = $('#companion');
    if (b) b.click();
    if (box) box.scrollIntoView({ behavior: 'smooth' });
  }
}
if ($('#trips')) $('#trips').addEventListener('click', function (e) {
  var b = e.target.closest('button'); if (!b) return;
  if (b.dataset.forget) { storeTrips(loadTrips().filter(function (x) { return x.id !== b.dataset.forget; })); showTrips(); return; }
  var x = loadTrips().filter(function (x) { return x.id === b.dataset.id; })[0];
  if (x) openTrip(x);
});
showTrips();

// start date: one week from today
var d = new Date(Date.now() + 7 * 864e5);
form.start.value = d.toISOString().slice(0, 10);
document.querySelectorAll('.quick button').forEach(function (b) {
  b.addEventListener('click', function () { form.destination.value = b.dataset.dest; form.destination.focus(); });
});

// example plans, made earlier with the same agent and live Qloo data: shown instantly
document.querySelectorAll('.examples button, .peek button').forEach(function (b) {
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
      if (!r.ok || !r.body) return r.text().then(function (t) { throw new Error(errorText(r.status, t)); });
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
    .then(function (plan) {
      finish();
      try { saveTrip(plan, body); } catch (e) { }  // saving is a bonus: the plan is shown whatever happens
      render(plan, body.destination);
    })
    .catch(function (err) { finish(); $('#err').textContent = err.message; $('#err').hidden = false; });
});

// a message a person can act on, whatever the server or its proxy sent back
function errorText(status, text) {
  var j = null;
  try { j = JSON.parse(text); } catch (e) { }
  var d = j && j.detail;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d) && d.length) {  // the form did not pass the server's checks
    var f = (d[0].loc || []).slice(-1)[0], name = { destination: 'Where are you going', tastes: 'What do you love', travellers: 'Who is travelling' }[f];
    return (name ? '“' + name + '”: ' : '') + (d[0].msg || 'please check the form') + '.';
  }
  return status >= 500 ? 'Rihla’s server is busy right now. Please try again in a minute — or open an example plan below.'
    : 'Something went wrong (' + status + '). Please try again.';
}

function render(plan, city) {
  $('#result').hidden = false;
  $('#how').hidden = true;
  var n = 0, stops = 0, meals = 0, verified = 0, today = tripToday(plan), kids = youngKids(plan), rule = kids ? WALK.kids : WALK.adults;
  // the plan's times are local to the trip: say so when the phone is in another time zone
  var at = new Date(((plan.days[0] && plan.days[0].date) || new Date().toISOString().slice(0, 10)) + 'T12:00:00Z');
  var there = plan.timezone ? gmt(plan.timezone, at) : '', local = there && there !== gmt(null, at) ? there : '';
  plan.days.forEach(function (d) { d.stops.forEach(function (s) { stops++; if (s.kind === 'meal') { meals++; if (s.halal_level === 'verified') verified++; } }); });
  $('#sum').innerHTML = (plan.fallback ? '<p class="exnote">The AI writer was busy, so Rihla assembled this plan itself from the same Qloo results.</p>' : '') +
    (plan.example ? '<p class="exnote">Example plan, made with the same agent and live Qloo data. Change anything above and press “Plan my trip” for your own.</p>' : '') +
    '<h2 dir="auto">' + esc(plan.title) + '</h2><p dir="auto">' + esc(plan.summary) + '</p><div class="meta">' +
    '<span>📍 ' + esc((plan.destination.name || '').split(',').slice(0, 3).join(',')) + '</span>' +
    '<span>' + plan.days.length + (plan.days.length === 1 ? ' day' : ' days') + ' · ' + stops + ' stops</span>' +
    '<span>🍽️ ' + meals + ' halal-aware meals' + (verified ? ' (' + verified + ' listed halal)' : '') + '</span>' +
    (local ? '<span title="' + esc(plan.timezone) + '">🕒 Local times · ' + esc(local) + '</span>' : '') +
    (plan.seconds ? '<span>⏱️ planned in ' + plan.seconds + ' s</span>' : '') + '</div>' +
    ((plan.signals && plan.signals.length) || (plan.topics && plan.topics.length) || (plan.audiences && plan.audiences.length) ?
      '<div class="taste"><b>Your Qloo taste profile</b>' +
      (plan.signals || []).map(function (x) { return '<span class="sig">' + esc(x) + '</span>'; }).join('') +
      (plan.topics || []).map(function (x) { return '<span class="sig tag" title="A Qloo tag used as a taste signal">#' + esc(x) + '</span>'; }).join('') +
      (plan.audiences || []).map(function (x) { return '<span class="aud">' + esc(x) + '</span>'; }).join('') + '</div>' : '');
  var h = '';
  plan.days.forEach(function (day, di) {
    h += '<article class="card day' + (today.day === di ? ' today' : '') + '"><div class="day-h"><div><h3>' + (today.day === di ? '<span class="today-tag">Today</span> ' : '') +
      'Day ' + (di + 1) + ' · <span dir="auto">' + esc(day.theme) + '</span></h3>' +
      '<div class="date">' + esc(fmtDate(day.date)) + (day.hijri ? ' · ' + esc(day.hijri) + ' AH' : '') + '</div></div></div>';
    if (day.prayer_times) {
      h += '<div class="ptimes">' + ['Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha'].map(function (p) {
        return day.prayer_times[p] ? '<span>' + p + '<b>' + esc(day.prayer_times[p]) + '</b></span>' : '';
      }).join('') + '</div>';
    }
    var list = '', prev = null, walk = 0, rides = 0;
    day.stops.forEach(function (s, si) {
      var id = di + '-' + si;
      // how to get here from the previous stop (straight line x 1.3 for streets); the first stop of a day has no mode
      s._mode = null;
      if (prev && s.lat != null) {
        var km = distKm(prev, s) * 1.3;
        s._mode = km <= rule.km ? 'walk' : 'transit';
        if (km >= 0.15) {
          var leg = '<a href="' + esc(mapsUrl(IS_APPLE ? 'apple' : 'google', s, s._mode, prev)) + '" target="_blank" rel="noopener" title="See this leg in ' + (IS_APPLE ? 'Apple Maps' : 'Google Maps') + '">';
          if (km <= rule.km) { walk += km; list += '<li class="leg">' + leg + '🚶 ' + Math.max(2, Math.round(km / rule.pace)) + ' min walk</a></li>'; }
          else { rides++; list += '<li class="leg">' + leg + '🚇 ' + km.toFixed(1) + ' km · metro or taxi</a></li>'; }
        }
      }
      if (s.lat != null) prev = s;
      var badges = '';
      if (s.kind === 'meal' && s.halal_level) badges += '<span class="badge ' + s.halal_level + '" title="' + esc(s.halal_reason) + '">' + LEVEL[s.halal_level] + '</span>';
      if (s.because && s.because.length) badges += '<span class="badge src">Because you love ' + esc(s.because.join(' & ')) + '</span>';
      else if (s.source === 'qloo' && s.affinity && s.kind !== 'prayer') badges += '<span class="badge src">Taste match · ' + Math.round(s.affinity * 100) + '%</span>';
      if (s.requested) badges += '<span class="badge must">You asked for this</span>';
      if (s.topic && s.topic.length) badges += '<span class="badge topic">Known for ' + esc(s.topic.join(' & ')) + '</span>';
      if (s.popular && s.kind === 'sight') badges += '<span class="badge must">Must-see</span>';
      if (s.kids_ok && s.kind !== 'prayer') badges += '<span class="badge kids">Good for kids</span>';
      if (s.open_today) badges += '<span class="badge k" title="Opening hours that day (Qloo)">Open ' + esc(s.open_today) + '</span>';
      if (s.alcohol && s.kind === 'meal') badges += '<span class="badge alc" title="Qloo lists alcohol at this place">Serves alcohol</span>';
      if (s.cuisine) badges += '<span class="badge k">' + esc(s.cuisine) + '</span>';
      else if (s.categories && s.categories.length && s.kind !== 'prayer') badges += '<span class="badge k">' + esc(s.categories[0]) + '</span>';
      if (s.jumuah) badges = '<span class="badge jumuah">Jumu\'ah · Friday prayer</span>' + badges;
      var img = s.image ? '<img class="thumb" src="' + esc(s.image) + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">' : '';
      list += '<li class="stop ' + esc(s.kind) + '" data-id="' + id + '"><div class="t">' + esc(s.time) + '</div><div class="dot">' + (si + 1) + '</div>' +
        '<div class="body"><div class="txt"><h4>' + (ICON[s.kind] || '') + ' ' + esc(s.name) + '</h4><p dir="auto">' + esc(s.why) + '</p>' +
        (badges ? '<div class="badges">' + badges + '</div>' : '') + '</div>' + img + '</div>' + navLinks(s) + '</li>';
    });
    h += '<div class="move">≈ ' + walk.toFixed(1) + ' km on foot' + (kids ? ' at children’s pace' : '') + (rides ?' · ' + rides + (rides === 1 ? ' ride' : ' rides') + ' by metro or taxi' : '') + '</div>';
    h += '<ol class="stops">' + list + '</ol></article>';
  });
  if (plan.tips && plan.tips.length) h += '<section class="card tips"><h3>Good to know</h3><ul>' + plan.tips.map(function (t) { return '<li dir="auto">' + esc(t) + '</li>'; }).join('') + '</ul></section>';
  $('#days').innerHTML = h;
  var q = plan.qloo_calls || {}, qn = (q.requests || 0) + (q.cached || 0);
  $('#trace summary').textContent = '🤖 How Rihla planned this — ' + (plan.trace || []).length + ' agent steps' +
    (qn ? ' · ' + qn + ' Qloo calls' : '') + ' · model ' + String(plan.model || '').split('/').pop();
  $('#trace ol').innerHTML = (plan.trace || []).map(function (t) {
    var a = t.args || {}; var what = a.query || a.category || (typeof a.lat === 'number' ? a.lat.toFixed(3) + ', ' + a.lon.toFixed(3) : '');
    return '<li><code>' + esc(t.tool) + '</code> ' + esc(what) + (t.note ? ' → ' + esc(t.note) : ' → ' + t.found + ' found') + '</li>';
  }).join('');
  drawMap(plan);
  companion(plan, city);
  document.querySelectorAll('.stop').forEach(function (el) {
    el.addEventListener('click', function (e) {
      if (e.target.closest('a')) return; // a directions link opens the map app, not our map
      var m = markers[el.dataset.id]; if (!m) return;
      var d = +el.dataset.id.split('-')[0];
      document.querySelectorAll('#daybar button').forEach(function (x) { x.classList.toggle('on', +x.dataset.d === d); });
      showDay(d); map.setView(m.getLatLng(), 16); m.openPopup();
    });
  });
  $('#result').scrollIntoView({ behavior: 'smooth' });
}

// ---- Today: on the day of the trip Rihla is a companion — what is now and next, the next prayer and its mosque,
// halal food and mosques around you. Everything comes from the places Qloo (and OpenStreetMap) already found for
// this plan (plan.nearby), so it costs no new requests and works on weak internet. Before the trip: a preview.
var DUR = { sight: 75, meal: 60, prayer: 20, rest: 45 };  // minutes a stop usually takes (as on the server)
var PRAYERS = ['Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha'], RANK = { verified: 0, likely: 1, unknown: 2 };
var comp = { plan: null, day: 0, mins: 0, mode: 'teaser', here: null, msg: '', tab: 'halal', timer: null, pin: null };
function mm(t) { var m = /^(\d{1,2}):(\d{2})/.exec(t || ''); return m ? +m[1] * 60 + +m[2] : null; }
function hm(m) { m = ((m % 1440) + 1440) % 1440; return ('0' + Math.floor(m / 60)).slice(-2) + ':' + ('0' + m % 60).slice(-2); }
function inMin(n) { return n < 60 ? n + ' min' : Math.floor(n / 60) + ' h' + (n % 60 ? ' ' + n % 60 + ' min' : ''); }
function far(m) { return m < 1000 ? Math.max(10, Math.round(m / 10) * 10) + ' m' : (m / 1000).toFixed(1) + ' km'; }

function companion(plan, city) {
  clearInterval(comp.timer);
  comp.city = city || '';
  comp.plan = plan; comp.here = null; comp.msg = '';
  if (comp.pin) { comp.pin.remove(); comp.pin = null; }
  var t = tripToday(plan);
  if (t.day != null) {
    comp.mode = 'live'; comp.day = t.day; comp.mins = nowIn(plan.timezone).mins;
    comp.timer = setInterval(function () {
      if (comp.mode !== 'live') return;
      comp.mins = nowIn(plan.timezone).mins; drawBody();
    }, 30000);
  } else comp.mode = 'teaser';
  drawCompanion();
}
function startPreview(day) {
  var d = comp.plan.days[day] || comp.plan.days[0], dh = mm((d.prayer_times || {}).Dhuhr);
  comp.mode = 'preview'; comp.day = comp.plan.days.indexOf(d);
  comp.mins = dh ? Math.round((dh - 40) / 5) * 5 : 690;  // a little before Dhuhr: something now, a prayer next
  drawCompanion();
}
function drawCompanion() {
  var box = $('#companion'), plan = comp.plan;
  if (!box || !plan) return;
  box.hidden = false;
  if (comp.mode === 'teaser') {
    var t = tripToday(plan);
    box.className = 'card companion teaser';
    box.innerHTML = '<div class="cp-teaser"><div><h3>📱 On the day, Rihla walks with you</h3><p>Open this plan on your phone during the trip' +
      (t.inDays ? ' (it starts ' + (t.inDays === 1 ? 'tomorrow' : 'in ' + t.inDays + ' days') + ')' : '') +
      ': what is now and next, a countdown to the next prayer with the nearest mosque, and halal food around you — ' +
      'from the places Qloo already found for this plan, even on weak internet.</p></div>' +
      '<button type="button" class="cp-try" data-preview="0">▶ Preview Day 1 now</button></div>';
    return;
  }
  var city = esc(cityOf(plan));
  box.className = 'card companion';
  box.innerHTML = '<div class="cp-head"><h3>' + (comp.mode === 'live' ? '<span class="today-tag">Today</span> ' : '<span class="cp-prev">Preview</span> ') +
    'in ' + city + '</h3>' + (comp.mode === 'preview' ? '<button type="button" class="cp-x" data-close="1" aria-label="Close the preview">×</button>' : '') + '</div>' +
    (comp.mode === 'preview' ? '<div class="cp-ctl"><div class="cp-days">' + plan.days.map(function (d, i) {
      return '<button type="button" data-pday="' + i + '"' + (i === comp.day ? ' class="on"' : '') + '>Day ' + d.day + '</button>';
    }).join('') + '</div><label class="cp-slide">Pretend it is <b id="cp-time">' + hm(comp.mins) + '</b>' +
      '<input type="range" id="cp-range" min="360" max="1395" step="5" value="' + comp.mins + '" aria-label="Time of day"></label></div>' : '') +
    '<div id="cp-body"></div>';
  drawBody();
}
// where the traveller is: their phone's location (if they shared it and are in the city), else the stop they are at
function whereNow(stops) {
  if (comp.here) return comp.here;
  var at = null;
  stops.forEach(function (s) { if (s.lat != null && mm(s.time) <= comp.mins) at = s; });
  at = at || stops.filter(function (s) { return s.lat != null; })[0];
  return at ? { lat: at.lat, lon: at.lon, label: at.name } : { lat: comp.plan.destination.lat, lon: comp.plan.destination.lon, label: 'the city centre' };
}
function drawBody() {
  var body = $('#cp-body'), plan = comp.plan;
  if (!body || !plan) return;
  var day = plan.days[comp.day], stops = day.stops, now = comp.mins, kids = youngKids(plan), rule = kids ? WALK.kids : WALK.adults;
  if ($('#cp-time')) $('#cp-time').textContent = hm(now);
  var here = whereNow(stops), cur = null, next = null;
  stops.forEach(function (s) { var t = mm(s.time); if (t == null) return; if (t <= now && now < t + (DUR[s.kind] || 45)) cur = s; if (t > now && !next) next = s; });
  var walk = function (p) {  // distance and how to get there from here
    var km = distKm(here, p) * 1.3, mode = km <= rule.km ? 'walk' : 'transit';
    return { km: km, mode: mode, text: far(km * 1000) + ' · ' + (mode === 'walk' ? '🚶 ' + Math.max(2, Math.round(km / rule.pace)) + ' min' : '🚇 metro or taxi') };
  };
  var go = function (p, mode) { return navLinks({ lat: p.lat, lon: p.lon, name: p.name, _mode: mode }); };
  // now and next
  var h = '<div class="cp-grid"><section class="cp-box"><h4>Now · ' + hm(now) + '</h4>';
  if (cur) h += '<p class="cp-big">' + (ICON[cur.kind] || '') + ' ' + esc(cur.name) + '</p><p class="cp-sub">until about ' + hm(mm(cur.time) + (DUR[cur.kind] || 45)) + '</p>';
  else if (next && next === stops[0]) h += '<p class="cp-big">The day starts at ' + esc(next.time) + '</p>';
  else if (next) h += '<p class="cp-big">Free time</p><p class="cp-sub">look around below, or head to the next stop</p>';
  else h += '<p class="cp-big">That is the day done 🌙</p>' + (plan.days[comp.day + 1] && plan.days[comp.day + 1].stops[0] ?
    '<p class="cp-sub">Tomorrow starts at ' + esc(plan.days[comp.day + 1].stops[0].time) + ' · ' + esc(plan.days[comp.day + 1].stops[0].name) + '</p>' : '');
  if (next) {
    var w = walk(next);
    h += '<div class="cp-next"><span class="cp-k">Next · ' + esc(next.time) + ' · in ' + inMin(mm(next.time) - now) + '</span>' +
      '<p class="cp-name">' + (ICON[next.kind] || '') + ' ' + esc(next.name) + '</p><p class="cp-sub">' + w.text + ' from ' + esc(here.label) + '</p>' + go(next, w.mode) + '</div>';
  }
  h += '</section>';
  // the next prayer, and where to pray it
  var pt = day.prayer_times || {}, pr = null;
  PRAYERS.forEach(function (p) { var t = mm(pt[p]); if (!pr && t != null && t + 15 > now) pr = { name: p, t: t }; });
  h += '<section class="cp-box pray"><h4>Next prayer</h4>';
  if (pr) {
    var planned = stops.filter(function (s) { return s.kind === 'prayer' && s.prayer === pr.name && s.lat != null; })[0];
    var mosques = places('mosques', here).slice(0, 1), m = comp.here && mosques[0] ? mosques[0] : planned || mosques[0];
    var friday = pr.name === 'Dhuhr' && planned && planned.jumuah;
    h += '<p class="cp-big">' + (friday ? "Jumu'ah" : pr.name) + ' · ' + hm(pr.t) + '</p><p class="cp-sub cp-count">' +
      (pr.t <= now ? 'started ' + inMin(now - pr.t) + ' ago — there is still time' : 'in ' + inMin(pr.t - now)) + '</p>';
    if (m) {
      var wm = walk(m);
      h += '<p class="cp-name">🕌 ' + esc(m.name) + '</p><p class="cp-sub">' + wm.text + (m === planned ? ' · in your plan' : ' · nearest to you') + '</p>' + go(m, wm.mode);
    }
  } else {
    var tmr = plan.days[comp.day + 1] && plan.days[comp.day + 1].prayer_times;
    h += '<p class="cp-big">Isha was at ' + esc(pt.Isha || '') + '</p>' + (tmr && tmr.Fajr ? '<p class="cp-sub">Fajr tomorrow at ' + esc(tmr.Fajr) + '</p>' : '');
  }
  h += '</section></div>';
  // around you
  var tabs = { halal: '🍽️ Halal food', mosques: '🕌 Mosques', sights: '🏛️ More to see' };
  var list = places(comp.tab, here).slice(0, 4);
  h += '<section class="cp-near"><div class="cp-near-h"><h4>Around ' + (comp.here ? 'you' : esc(here.label)) + '</h4>' +
    '<button type="button" class="cp-loc" data-loc="1">📍 ' + (comp.here ? 'Update my location' : 'Use my location') + '</button></div>' +
    (comp.msg ? '<p class="cp-msg">' + esc(comp.msg) + '</p>' : '') +
    '<div class="cp-tabs" role="tablist">' + Object.keys(tabs).map(function (k) {
      return '<button type="button" role="tab" aria-selected="' + (k === comp.tab) + '" data-tab="' + k + '"' + (k === comp.tab ? ' class="on"' : '') + '>' + tabs[k] + '</button>';
    }).join('') + '</div><ul class="cp-list">' + (list.length ? list.map(function (p) {
      var w = walk(p), b = '';
      if (p.halal_level) b += '<span class="badge ' + p.halal_level + '" title="' + esc(p.halal_reason) + '">' + LEVEL[p.halal_level] + '</span>';
      if (p.alcohol && comp.tab === 'halal') b += '<span class="badge alc">Serves alcohol</span>';
      if (p.because && p.because.length) b += '<span class="badge src">Because you love ' + esc(p.because.join(' & ')) + '</span>';
      if (p.topic && p.topic.length) b += '<span class="badge topic">Known for ' + esc(p.topic.join(' & ')) + '</span>';
      if (p.popular && comp.tab === 'sights') b += '<span class="badge must">Must-see</span>';
      if (p.kids_ok && kids && comp.tab !== 'mosques') b += '<span class="badge kids">Good for kids</span>';
      if (p.cuisine) b += '<span class="badge k">' + esc(p.cuisine) + '</span>';
      else if (p.categories && p.categories.length && comp.tab === 'sights') b += '<span class="badge k">' + esc(p.categories[0]) + '</span>';
      return '<li><button type="button" class="cp-show" data-lat="' + p.lat + '" data-lon="' + p.lon + '" data-name="' + esc(p.name) + '">' + esc(p.name) + '</button>' +
        (p._inPlan ? ' <span class="cp-inplan">in your plan</span>' : '') + '<span class="cp-sub"> · ' + w.text + '</span>' +
        (b ? '<div class="badges">' + b + '</div>' : '') + go(p, w.mode) + '</li>';
    }).join('') : '<li class="cp-sub">Nothing found within walking distance — try another tab.</li>') + '</ul>' +
    '<p class="cp-note">From the ' + poolSize() + ' places Qloo and OpenStreetMap found for this plan — no new searches, works on weak internet.' +
    (plan.nearby ? '' : ' Plan this trip again for more places around you.') + '</p></section>';
  body.innerHTML = h;
  hereOnMap(here);
}
// the city as the traveller wrote it ("London", not "Greater London")
function cityOf(plan) { return String(comp.city || (plan.example && plan.example.destination) || plan.destination.name || '').split(',')[0].trim(); }
function poolSize() { var n = comp.plan.nearby || {}; return ['halal', 'mosques', 'sights'].reduce(function (a, k) { return a + (n[k] || []).length; }, 0) + comp.plan.days.reduce(function (a, d) { return a + d.stops.length; }, 0); }
// candidates of one kind near a point: the plan's own stops and the ones Qloo found but the plan did not use
function places(kind, here) {
  var want = { halal: 'meal', mosques: 'prayer', sights: 'sight' }[kind], seen = {}, out = [];
  comp.plan.days.forEach(function (d) { d.stops.forEach(function (s) { if (s.kind === want && s.lat != null) out.push(Object.assign({ _inPlan: true }, s)); }); });
  out = out.concat(((comp.plan.nearby || {})[kind]) || []).filter(function (p) {
    var k = String(p.name).toLowerCase(); if (seen[k]) return false; seen[k] = 1; return p.lat != null;
  }).map(function (p) { return Object.assign({ _m: distKm(here, p) * 1000 }, p); });
  out.sort(function (a, b) { return a._m - b._m; });
  if (kind === 'mosques') return out;
  // halal food: the most certain options within a short walk first; sights: personal matches first
  var close = out.filter(function (p) { return p._m <= 1200; });
  if (close.length < 3) return out;
  var rank = kind === 'halal' ? function (p) { return (RANK[p.halal_level] || 0) * 2 + (p.alcohol ? 3 : 0); }
    : function (p) { return -((p.topic && p.topic.length ? 2 : 0) + (p.because && p.because.length ? 1 : 0) + (p.popular ? 1 : 0)); };
  return close.sort(function (a, b) { return rank(a) - rank(b) || a._m - b._m; }).concat(out.filter(function (p) { return p._m > 1200; }));
}
function hereOnMap(here) {
  if (!map) return;
  if (comp.pin) comp.pin.remove();
  comp.pin = L.marker([here.lat, here.lon], { icon: L.divIcon({ className: '', html: '<div class="here-icon"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }), zIndexOffset: 1000 })
    .bindPopup(comp.here ? 'You are here' : 'Around ' + esc(here.label)).addTo(map);
}
var cpBox = $('#companion');
if (cpBox) {
  cpBox.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    if (b.dataset.preview != null) { startPreview(+b.dataset.preview); showDayOnMap(comp.day); }
    else if (b.dataset.close) { comp.mode = 'teaser'; comp.here = null; comp.msg = ''; if (comp.pin) { comp.pin.remove(); comp.pin = null; } drawCompanion(); }
    else if (b.dataset.pday != null) { comp.day = +b.dataset.pday; comp.here = null; drawCompanion(); showDayOnMap(comp.day); }
    else if (b.dataset.tab) { comp.tab = b.dataset.tab; drawBody(); }
    else if (b.dataset.loc) locate(b);
    else if (b.dataset.lat) {
      var p = [+b.dataset.lat, +b.dataset.lon];
      L.popup().setLatLng(p).setContent(esc(b.dataset.name)).openOn(map); map.setView(p, 16);
      if (window.innerWidth <= 900) $('#map').scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  });
  cpBox.addEventListener('input', function (e) { if (e.target.id === 'cp-range') { comp.mins = +e.target.value; drawBody(); } });
}
function showDayOnMap(d) {
  document.querySelectorAll('#daybar button').forEach(function (x) { x.classList.toggle('on', +x.dataset.d === d); });
  showDay(d);
}
// the phone's location is asked only when the traveller taps the button, and never leaves the phone
function locate(btn) {
  if (!navigator.geolocation) { comp.msg = 'This browser cannot share your location.'; return drawBody(); }
  btn.disabled = true; btn.textContent = '📍 Finding you…';
  navigator.geolocation.getCurrentPosition(function (pos) {
    var me = { lat: pos.coords.latitude, lon: pos.coords.longitude, label: 'you' }, km = distKm(me, comp.plan.destination);
    if (km > 40) { comp.here = null; comp.msg = 'You are ' + Math.round(km) + ' km from ' + cityOf(comp.plan) + ' — showing what is around your plan instead.'; }
    else { comp.here = me; comp.msg = ''; }
    drawBody();
  }, function () { comp.msg = 'Location is off — showing what is around your plan instead.'; drawBody(); }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 });
}

function showDay(d) {
  var pts = [];
  dayLayers.forEach(function (dl, i) {
    var on = d < 0 || i === d;
    if (on) { dl.addTo(layer); dl.eachLayer(function (m) { if (m.getLatLng) pts.push(m.getLatLng()); }); } else dl.remove();
  });
  if (pts.length) map.fitBounds(pts, { padding: [30, 30] });
}

function distKm(a, b) {
  var r = 6371, toR = Math.PI / 180, dLat = (b.lat - a.lat) * toR, dLon = (b.lon - a.lon) * toR;
  var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(a.lat * toR) * Math.cos(b.lat * toR) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return 2 * r * Math.asin(Math.sqrt(x));
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
        .bindPopup('<b>Day ' + (di + 1) + ' · ' + esc(s.time) + '</b><br>' + esc(s.name) + (s.halal_level && s.kind === 'meal' ? '<br><i>' + LEVEL[s.halal_level] + '</i>' : '') + navLinks(s));
      markers[di + '-' + si] = m; pts.push([s.lat, s.lon]); line.push([s.lat, s.lon]);
    });
    if (line.length > 1) L.polyline(line, { color: DAYCOL[di % DAYCOL.length], weight: 3, opacity: .55, dashArray: '6 6' }).addTo(dl);
  });
  // day buttons above the map
  var bar = $('#daybar');
  bar.innerHTML = '<button class="on" data-d="-1">All days</button>' + plan.days.map(function (d, i) { return '<button data-d="' + i + '">Day ' + (i + 1) + '</button>'; }).join('');
  bar.onclick = function (e) {
    var b = e.target.closest('button'); if (!b) return;
    bar.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
    showDay(+b.dataset.d);
  };
  if (pts.length) map.fitBounds(pts, { padding: [30, 30] }); else map.setView([plan.destination.lat, plan.destination.lon], 13);
  setTimeout(function () { map.invalidateSize(); }, 50);
}
})();
