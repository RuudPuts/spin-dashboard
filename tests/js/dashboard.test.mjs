// Frontend-tests: laden het dashboard in jsdom met een nagebootste fetch.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';

const HTML = readFileSync(new URL('../../app/static/index.html', import.meta.url), 'utf8');
const now = Date.now();
const ep = (minAgo) => Math.floor((now - minAgo * 60e3) / 1000);
// lokale datum als dagnaam, zoals de server die gebruikt
const dayName = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const today = dayName(now);
// days.json + dagbestanden zoals de server ze levert: served({'2026-10-09': [regels]})
const served = (days) => Object.fromEntries([
  ['days.json', JSON.stringify({ days: Object.keys(days) })],
  ...Object.entries(days).map(([d, lines]) => [`days/${d}.csv`, lines.join('\n') + '\n']),
]);

function load(files, url = 'http://dash/?lang=nl', languages = ['en-US'], stored = null, setup = null) {
  const dom = new JSDOM(HTML, {
    runScripts: 'dangerously',
    url,
    beforeParse(w) {
      Object.defineProperty(w.navigator, 'languages', { value: languages });
      if (stored) w.localStorage.setItem('spindash.lang', stored);
      if (setup) setup(w);
      w.__fetched = [];
      w.fetch = (u) => {
        w.__fetched.push(u);
        const key = Object.keys(files).find((k) => u.includes(k));
        if (!key || files[key] === null) return Promise.resolve({ ok: false });
        return Promise.resolve({ ok: true, text: () => Promise.resolve(files[key]),
                                 json: () => Promise.resolve(JSON.parse(files[key])) });
      };
    },
  });
  return new Promise((resolve) => setTimeout(() => resolve(dom), 300));
}
const text = (el) => el.textContent.replace(/\s+/g, ' ').trim();
const rows = (doc) => [...doc.querySelectorAll('#events tr')].map(text);

const POOL = served({ [today]: [
  `${ep(300)},disk,sdd,trunk`, `${ep(300)},disk,sde,trunk2`,
  `${ep(300)},state,sdd,standby`, `${ep(300)},state,sde,standby`,
  `${ep(121)},who,unraid,shfs (via /mnt/user),1,geopend,/mnt/trunk/`,
  `${ep(121)},who,container,bazarr,2,geopend,/mnt/trunk/Media/series/a.mkv`,
  `${ep(120)},spin,sdd,active`, `${ep(119.5)},spin,sde,active`,
  `${ep(110)},who,gebruiker,SMB-share,3,geopend,/mnt/trunk/Media/films/b.mkv`,
  `${ep(100)},who,container,plex,4,geopend,/mnt/trunk/Media/series/c.mkv`,
  `${ep(60)},spin,sdd,standby`, `${ep(60)},spin,sde,standby`,
] });

test('missing data shows a status message instead of the dashboard', async () => {
  const dom = await load({ 'days.json': null });
  const d = dom.window.document;
  assert.equal(d.getElementById('status').hidden, false);
  assert.equal(d.getElementById('dash').hidden, true);
  dom.window.close();
});

test('demo mode renders lanes for every disk', async () => {
  const dom = await load({}, 'http://dash/?demo&lang=nl');
  const d = dom.window.document;
  assert.equal(d.querySelectorAll('.lane').length, 6);
  assert.match(d.getElementById('sourceLine').textContent, /Voorbeelddata/);
  dom.window.close();
});

test('pool members are grouped into one row with names from disks.csv', async () => {
  const dom = await load(POOL);
  const r = rows(dom.window.document);
  assert.ok(r.some((x) => /trunk, trunk2 Opgespind/.test(x)), r.join('\n'));
  assert.ok(r.some((x) => /trunk, trunk2 Naar standby/.test(x)));
  dom.window.close();
});

test('the cause is the earliest file and shows the container name', async () => {
  const dom = await load(POOL);
  const up = rows(dom.window.document).find((x) => /Opgespind/.test(x));
  assert.match(up, /Media\/series gelezen: a\.mkv bazarr/);
  dom.window.close();
});

test('session list collapses SMB access to one entry per share', async () => {
  const dom = await load(POOL);
  const d = dom.window.document;
  d.querySelector('tr.has-sess .sess').click();
  const files = [...d.querySelectorAll('.files li')].map(text);
  assert.deepEqual(files.map((f) => f.replace(/^\S+ /, '')), [
    'R gelezen bazarr /Media/series/a.mkv',
    'via share SMB-share /Media/',
    'R gelezen plex /Media/series/c.mkv',
  ]);
  dom.window.close();
});

test('"Door wie" ranks sources of spin-ups', async () => {
  const dom = await load(POOL);
  const causes = text(dom.window.document.getElementById('causes'));
  assert.match(causes, /Media\/series 1 Door wie bazarr 1/);
  dom.window.close();
});

test('filtering on one disk shows its events separately', async () => {
  const dom = await load(POOL);
  const d = dom.window.document;
  const sel = d.getElementById('devFilter');
  sel.value = 'sde';
  sel.dispatchEvent(new dom.window.Event('change'));
  const r = rows(d);
  assert.ok(r.every((x) => !/trunk, trunk2/.test(x)));
  assert.ok(r.some((x) => /trunk2 Opgespind/.test(x)));
  dom.window.close();
});

// ---------- talen ----------
test('language follows the browser and falls back to English', async () => {
  let dom = await load(POOL, 'http://dash/', ['de-DE', 'en']);
  let d = dom.window.document;
  assert.equal(text(d.querySelector('h1')), 'Wann laufen meine Festplatten');
  assert.equal(d.documentElement.lang, 'de');
  assert.ok(rows(d).some((x) => /trunk, trunk2 Angelaufen/.test(x)));
  dom.window.close();
  dom = await load(POOL, 'http://dash/', ['ja-JP']);
  d = dom.window.document;
  assert.equal(text(d.querySelector('h1')), 'When do my drives spin');
  assert.ok(rows(d).some((x) => /Spun up/.test(x)));
  dom.window.close();
});

test('switching language translates the page and is remembered', async () => {
  const dom = await load(POOL, 'http://dash/', ['en-US']);
  const d = dom.window.document;
  const sel = d.getElementById('lang');
  for (const [code, heading, up] of [
    ['fr', 'Quand mes disques tournent-ils', 'Démarré'],
    ['es', 'Cuándo giran mis discos', 'Arrancado'],
    ['nl', 'Wanneer draaien mijn schijven', 'Opgespind'],
  ]) {
    sel.value = code;
    sel.dispatchEvent(new dom.window.Event('change'));
    assert.equal(text(d.querySelector('h1')), heading);
    assert.ok(rows(d).some((x) => x.includes(up)), code);
    assert.equal(d.querySelector('[data-range="all"]').textContent, { fr: 'Tout', es: 'Todo', nl: 'Alles' }[code]);
  }
  assert.equal(dom.window.localStorage.getItem('spindash.lang'), 'nl');
  dom.window.close();
});

test('a remembered language wins over the browser language', async () => {
  let dom = await load(POOL, 'http://dash/', ['de-DE'], 'es');
  assert.equal(text(dom.window.document.querySelector('h1')), 'Cuándo giran mis discos');
  assert.equal(dom.window.document.getElementById('lang').value, 'es');
  dom.window.close();
  dom = await load(POOL, 'http://dash/', ['de-DE'], 'xx');       // onbekende waarde: browsertaal
  assert.equal(text(dom.window.document.querySelector('h1')), 'Wann laufen meine Festplatten');
  dom.window.close();
});

test('every language has the same keys as English', async () => {
  const dom = await load(POOL, 'http://dash/', ['en-US']);
  const d = dom.window.document;
  const codes = [...d.querySelectorAll('#lang option')].map((o) => o.value);
  assert.deepEqual(codes, ['en', 'nl', 'fr', 'de', 'es']);
  const m = HTML.match(/const I18N = (\{[\s\S]*?\n\});/);
  const dict = new Function(`return ${m[1]}`)();
  const keys = Object.keys(dict.en).sort();
  for (const c of codes) assert.deepEqual(Object.keys(dict[c]).sort(), keys, c);
  dom.window.close();
});

// ---------- bladeren ----------
const day = 24 * 60;
const SPREAD = served({ [today]: [`${ep(40 * day)},disk,sdd,disk1`, ...[40, 35, 9, 8.9, 1.5, 1.4, 0.2, 0.1].map((d, i) =>
  `${ep(d * day)},spin,sdd,${i % 2 ? 'standby' : 'active'}`)] });
// een wissel toont eerst het skelet; wacht tot het dashboard terug is
const settle = async (d) => {
  for (let i = 0; i < 150 && (d.getElementById('dash').hidden || !d.getElementById('loader').hidden); i++)
    await new Promise((r) => setTimeout(r, 20));
};
const click = async (d, sel) => { d.querySelector(sel).click(); await settle(d); };
const upTimes = (d) => rows(d).filter((x) => /Opgespind/.test(x)).length;

test('day view steps one day back and forward', async () => {
  const dom = await load(SPREAD);
  const d = dom.window.document;
  await click(d, '[data-range="1"]');
  assert.equal(d.getElementById('nav').hidden, false);
  assert.equal(d.getElementById('next').disabled, true);
  assert.equal(d.getElementById('now').hidden, false);
  assert.equal(d.getElementById('now').getAttribute('aria-pressed'), 'true');
  assert.equal(upTimes(d), 1);                          // 0,2 dag geleden
  await click(d, '#prev');
  assert.equal(upTimes(d), 1);                          // 1,5 dag geleden
  assert.equal(d.getElementById('next').disabled, false);
  assert.equal(d.getElementById('now').getAttribute('aria-pressed'), 'false');
  assert.equal(d.getElementById('prev').getAttribute('aria-label'), 'Vorige dag');
  await click(d, '#prev');
  assert.equal(upTimes(d), 0);                          // 2 tot 3 dagen geleden: niets
  await click(d, '#next'); await click(d, '#next');
  assert.equal(d.getElementById('next').disabled, true);
  assert.equal(upTimes(d), 1);
  dom.window.close();
});

test('week and month views step a whole period, and "now" jumps back', async () => {
  const dom = await load(SPREAD);
  const d = dom.window.document;
  await click(d, '[data-range="7"]');
  assert.equal(upTimes(d), 2);                          // 1,5 en 0,2 dag
  await click(d, '#prev');
  assert.equal(upTimes(d), 1);                          // 9 dagen
  assert.equal(d.getElementById('prev').getAttribute('aria-label'), 'Vorige week');
  await click(d, '#now');
  assert.equal(upTimes(d), 2);
  assert.equal(d.getElementById('now').getAttribute('aria-pressed'), 'true');
  await click(d, '[data-range="30"]');
  assert.equal(upTimes(d), 3);
  await click(d, '#prev');
  assert.equal(upTimes(d), 1);                          // 35 dagen; 40 dagen is het begin van de data
  assert.equal(d.getElementById('prev').disabled, true);
  assert.equal(d.getElementById('prev').getAttribute('aria-label'), 'Vorige maand');
  await click(d, '[data-range="all"]');
  assert.equal(d.getElementById('nav').hidden, true);
  assert.equal(upTimes(d), 4);
  dom.window.close();
});

// ---------- info-icoontjes ----------
const ROOT = served({ [today]: [
  `${ep(300)},disk,sdd,trunk`, `${ep(300)},state,sdd,standby`,
  `${ep(121)},who,unraid,shfs (via /mnt/user),1,geopend,/mnt/trunk/`,
  `${ep(120)},spin,sdd,active`, `${ep(60)},spin,sdd,standby`,
  `${ep(51)},who,onbekend,find (al gestopt),9,geopend,/mnt/trunk/Media/x.nfo`,
  `${ep(50)},spin,sdd,active`, `${ep(40)},spin,sdd,standby`,
] });

test('ranking rows that need explaining get an info icon with a tooltip', async () => {
  const dom = await load(ROOT);
  const d = dom.window.document, w = dom.window;
  const infos = [...d.querySelectorAll('#causes .info')].map((b) => [text(b.closest('.cause-name').querySelector('.cn')), b.dataset.info]);
  assert.deepEqual(infos, [
    ['Root van trunk', 'rootNote'],
    ['shfs (via /mnt/user)', 'shfsNote'],
    ['find (al gestopt)', 'stoppedNote'],
  ]);
  assert.equal(d.querySelectorAll('#causes p').length, 0);         // geen losse uitlegalinea's meer
  const btn = d.querySelector('#causes .info');
  const tip = d.getElementById('tip');
  btn.dispatchEvent(new w.Event('focusin', { bubbles: true }));
  assert.equal(tip.hidden, false);
  assert.match(tip.textContent, /^Root van een pool betekent/);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(tip.hidden, true);
  btn.click();                                                       // tikken op telefoon
  assert.equal(tip.hidden, false);
  btn.click();
  assert.equal(tip.hidden, true);
  dom.window.close();
});

test('tooltip text follows the chosen language', async () => {
  const dom = await load(ROOT, 'http://dash/?lang=de');
  const d = dom.window.document;
  const btn = d.querySelector('#causes .info[data-info="stoppedNote"]');
  assert.equal(text(btn.closest('.cause-name').querySelector('.cn')), 'find (bereits beendet)');
  btn.click();
  assert.match(d.getElementById('tip').textContent, /^Der Prozess war bereits beendet/);
  dom.window.close();
});

// ---------- dagbestanden ----------
test('day files are combined, and a state line is not a spin-up', async () => {
  const dom = await load(served({
    '2026-10-08': [`${ep(30 * 60)},disk,sdd,trunk`, `${ep(30 * 60)},state,sdd,standby`,
                   `${ep(26 * 60)},spin,sdd,active`, `${ep(25 * 60)},spin,sdd,standby`],
    '2026-10-09': [`${ep(20 * 60)},disk,sdd,trunk`, `${ep(20 * 60)},state,sdd,standby`,
                   `${ep(3 * 60)},state,sdd,active`,                 // na een herstart: draait al
                   `${ep(60)},spin,sdd,standby`],
  }));
  const d = dom.window.document;
  await click(d, '[data-range="all"]');                  // de spin-up van gisteren valt buiten de 24 uur
  const r = rows(d);
  assert.equal(r.filter((x) => /Opgespind/.test(x)).length, 1, r.join('\n'));   // alleen die van gisteren
  assert.equal(r.filter((x) => /Naar standby/.test(x)).length, 2);
  assert.match(text(d.querySelector('.lane-stats')), /^1 spin-ups/);
  dom.window.close();
});

// ---------- lezen/schrijven en actieve schijven ----------
const RW = served({ [today]: [
  `${ep(300)},disk,sdd,trunk`,
  `${ep(300)},state,sdd,active`,                                       // draait al, geen spin-up
  `${ep(200)},who,container,plex,10,geschreven,/mnt/trunk/appdata/plex/Logs/server.log`,
  `${ep(190)},who,container,plex,10,geschreven,/mnt/trunk/appdata/plex/Logs/server.log`,
  `${ep(180)},who,container,sonarr,11,geschreven,/mnt/trunk/appdata/sonarr/sonarr.db`,
  `${ep(120)},who,container,bazarr,12,geopend,/mnt/trunk/Media/series/a.mkv`,
] });

test('a write event is grouped under the drive in the active-drives view', async () => {
  const dom = await load(RW);
  const d = dom.window.document;
  const ad = d.getElementById('actDrives');
  assert.ok(ad.querySelector('.actdisk'), text(ad));
  assert.match(text(ad), /plex/);
  assert.match(text(ad), /sonarr/);
  dom.window.close();
});

test('a write event in a session list carries a W badge, a read an R badge', async () => {
  const SES = served({ [today]: [
    `${ep(300)},disk,sdd,trunk`, `${ep(300)},state,sdd,standby`,
    `${ep(121)},who,container,plex,10,geschreven,/mnt/trunk/appdata/plex/Logs/server.log`,
    `${ep(120.5)},who,container,bazarr,2,geopend,/mnt/trunk/Media/series/a.mkv`,
    `${ep(120)},spin,sdd,active`, `${ep(60)},spin,sdd,standby`,
  ] });
  const dom = await load(SES);
  const d = dom.window.document;
  d.querySelector('tr.has-sess .sess').click();
  const badges = [...d.querySelectorAll('.files .ev .rw')].map((b) => b.textContent);
  assert.ok(badges.includes('W'), badges.join(','));   // geschreven -> W
  assert.ok(badges.includes('R'), badges.join(','));   // geopend op bestand -> R
  dom.window.close();
});

test('active drives ranks the top writer and shows write counts', async () => {
  const dom = await load(RW);
  const d = dom.window.document;
  const ad = text(d.getElementById('actDrives'));
  assert.match(ad, /trunk/);
  // plex schreef 2x, sonarr 1x, bazarr las 1x -> schrijven staat boven lezen
  assert.match(ad, /Schrijven/);
  assert.match(ad, /Lezen/);
  // volgorde binnen Schrijven: plex (2) voor sonarr (1)
  const wcol = d.querySelector('.actdisk .actcols > div');
  const names = [...wcol.querySelectorAll('.cn')].map(text);
  assert.ok(names.indexOf('plex') < names.indexOf('sonarr'), names.join(','));
  dom.window.close();
});

test('legacy who line without direction still renders without a badge', async () => {
  // oud 'geopend'-record van een bestand: geen R/W-badge (richting onbekend), geen crash
  const LEG = served({ [today]: [
    `${ep(300)},disk,sdd,trunk`, `${ep(300)},state,sdd,standby`,
    `${ep(121)},who,container,bazarr,2,geopend,/mnt/trunk/Media/series/a.mkv`,
    `${ep(120)},spin,sdd,active`, `${ep(60)},spin,sdd,standby`,
  ] });
  const dom = await load(LEG);
  const d = dom.window.document;
  const up = rows(d).find((x) => /Opgespind/.test(x));
  assert.match(up, /gelezen: a\.mkv/);            // oude regel toont als 'gelezen', geen richting-badge
  dom.window.close();
});

// ---------- live feed, laatste uur en pad-overlay ----------
test('live activity feed shows recent files newest first with R/W dot', async () => {
  const dom = await load(RW);
  const d = dom.window.document;
  const items = [...d.querySelectorAll('#feed li')];
  assert.ok(items.length >= 3, items.map(text).join(' | '));
  // nieuwste (bazarr, 120 min geleden) bovenaan; oudste (plex, 200 min) onderaan
  assert.match(text(items[0]), /a\.mkv/);
  assert.match(text(items[items.length - 1]), /server\.log/);
  // R/W-punten aanwezig
  assert.ok(d.querySelector('#feed .fb.w'), 'write badge');
  assert.ok(d.querySelector('#feed .fb.r'), 'read badge');
  // relatieve tijd aanwezig (min of uur geleden)
  assert.match(text(items[0]), /\d+ (min|u) geleden/);
  dom.window.close();
});

test('feed shows the folder structure and the full path in the overlay', async () => {
  const dom = await load(RW);
  const d = dom.window.document, w = dom.window;
  const fp = [...d.querySelectorAll('#feed .fp')].find((el) => /a\.mkv/.test(el.textContent));
  // zichtbaar: share-relatief pad met mappen, niet alleen de bestandsnaam
  assert.equal(fp.textContent, '/Media/series/a.mkv');
  // volledige pad in data-path voor de hover-overlay
  assert.equal(fp.dataset.path, '/mnt/trunk/Media/series/a.mkv');
  fp.dispatchEvent(new w.Event('focusin', { bubbles: true }));
  const tip = d.getElementById('tip');
  assert.equal(tip.hidden, false);
  assert.equal(tip.textContent, '/mnt/trunk/Media/series/a.mkv');
  dom.window.close();
});

test('last hour toggle narrows the window and shows minute ticks', async () => {
  const dom = await load(RW);
  const d = dom.window.document, w = dom.window;
  d.querySelector('[data-range="h"]').click();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(d.querySelector('[data-range="h"]').getAttribute('aria-pressed'), 'true');
  // gebeurtenissen ouder dan een uur vallen buiten de tabel
  const r = rows(d);
  assert.equal(r.filter((x) => /Opgespind|Naar standby/.test(x)).length, 0, r.join('\n'));
  dom.window.close();
});

test('hovering a file path in Active drives shows the full path overlay', async () => {
  const dom = await load(RW);
  const d = dom.window.document, w = dom.window;
  const el = d.querySelector('#actDrives .haspath');
  assert.ok(el, 'file row with path');
  el.dispatchEvent(new w.Event('focusin', { bubbles: true }));
  const tip = d.getElementById('tip');
  assert.equal(tip.hidden, false);
  assert.match(tip.textContent, /\/appdata\//);
  el.dispatchEvent(new w.Event('focusout', { bubbles: true }));
  assert.equal(tip.hidden, true);
  dom.window.close();
});

// ---------- live stream (Server-Sent Events) ----------
function loadWithStream(files) {
  let handler = null;
  const dom = new JSDOM(HTML, {
    runScripts: 'dangerously',
    url: 'http://dash/?lang=nl',
    beforeParse(w) {
      Object.defineProperty(w.navigator, 'languages', { value: ['en-US'] });
      w.fetch = (u) => {
        const key = Object.keys(files).find((k) => u.includes(k));
        if (!key || files[key] === null) return Promise.resolve({ ok: false });
        return Promise.resolve({ ok: true, text: () => Promise.resolve(files[key]),
                                 json: () => Promise.resolve(JSON.parse(files[key])) });
      };
      w.EventSource = class {
        constructor() { }
        set onmessage(fn) { handler = fn; }
        set onerror(fn) { }
        close() { }
      };
      w.__push = (line) => handler && handler({ data: line });
    },
  });
  return new Promise((resolve) => setTimeout(() => resolve(dom), 300));
}

test('a streamed who line appears in the feed immediately', async () => {
  const dom = await loadWithStream(RW);
  const d = dom.window.document, w = dom.window;
  const before = d.querySelectorAll('#feed li').length;
  // simuleer een event dat de server net heeft gelogd
  w.__push(`${Math.floor(Date.now() / 1000)},who,container,radarr,99,geschreven,/mnt/trunk/appdata/radarr/radarr.db`);
  await new Promise((r) => setTimeout(r, 50));
  const items = [...d.querySelectorAll('#feed li')];
  assert.ok(items.length > before, 'feed groeit zonder te wachten op de minuten-fetch');
  assert.match(text(items[0]), /radarr\.db/);          // nieuwste bovenaan
  assert.ok(items[0].querySelector('.fb.w'), 'write badge op het gestreamde event');
  assert.ok(items[0].classList.contains('fresh'), 'nieuw item krijgt een highlight');
  dom.window.close();
});

// ---------- laadpaneel ----------
test('first load shows a loading panel with progress, then the dashboard', async () => {
  let release;
  const slow = new Promise((r) => { release = r; });
  const files = served({
    '2026-10-08': [`${ep(30 * 60)},disk,sdd,trunk`, `${ep(30 * 60)},state,sdd,standby`],
    '2026-10-09': [],
  });
  files['days.json'] = JSON.stringify({ days: ['2026-10-08', '2026-10-09'], bytes: 2 * 1048576 });
  const day2 = [`${ep(120)},spin,sdd,active`, `${ep(60)},spin,sdd,standby`].join('\n') + '\n';
  files['days/2026-10-09.csv'] = slow.then(() => day2);              // tweede dag komt later binnen
  const dom = await load(files);
  const d = dom.window.document;
  const bar = d.getElementById('loaderBar');
  assert.equal(d.getElementById('loader').hidden, false);
  assert.equal(d.getElementById('dash').hidden, true);
  assert.match(d.getElementById('loaderText').textContent, /^1 van 2 dagen geladen · 0,0 van 2,0 MB$/);
  assert.equal(bar.classList.contains('indeterminate'), false);
  release();
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(d.getElementById('loader').hidden, true);
  assert.equal(d.getElementById('dash').hidden, false);
  assert.equal(bar.getAttribute('aria-valuenow'), '96');                 // laatste stap: opbouwen
  assert.ok(rows(d).some((x) => /Opgespind/.test(x)));
  dom.window.close();
});

test('no data hides the loading panel and shows the status message', async () => {
  const dom = await load({ 'days.json': null });
  const d = dom.window.document;
  assert.equal(d.getElementById('loader').hidden, true);
  assert.equal(d.getElementById('status').hidden, false);
  dom.window.close();
});

// ---------- alleen de dagen van de getoonde periode ----------
test('only the days of the shown period are fetched; browsing back loads the rest', async () => {
  const days = {};
  for (let k = 11; k >= 0; k--){
    const t0 = now - k * day * 60e3;
    days[dayName(t0)] = [`${Math.floor(t0 / 1000) - 7200},disk,sdd,disk1`,
      `${Math.floor(t0 / 1000) - 3600},spin,sdd,active`, `${Math.floor(t0 / 1000) - 1800},spin,sdd,standby`];
  }
  const files = served(days);
  files['days.json'] = JSON.stringify({ days: Object.keys(days), sizes: Object.keys(days).map(() => 100) });
  const dom = await load(files);
  const d = dom.window.document;
  const fetchedDays = () => dom.window.__fetched.filter((u) => u.includes('/days/')).map((u) => u.match(/(\d{4}-\d{2}-\d{2})/)[1]);
  const names = Object.keys(days);
  // standaard 24 uur: vandaag, gisteren en een dag marge
  assert.equal(d.querySelector('[data-range="1"]').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(fetchedDays().sort(), names.slice(9));
  assert.equal(upTimes(d), 1);
  d.querySelector('[data-range="7"]').click();          // wissel: skelet en laadbalk, de knop is meteen actief
  assert.equal(d.getElementById('dash').hidden, true);
  assert.equal(d.getElementById('loader').hidden, false);
  assert.equal(d.getElementById('loaderBar').hidden, false);
  assert.equal(d.querySelector('[data-range="7"]').getAttribute('aria-pressed'), 'true');
  await settle(d);
  assert.deepEqual(fetchedDays().sort(), names.slice(3));
  assert.equal(upTimes(d), 7);
  await click(d, '#prev');                               // week terug: de oudere dagen worden bijgeladen
  assert.deepEqual(fetchedDays().sort(), names);
  assert.equal(upTimes(d), 5);                           // 7 t/m 11 dagen geleden
  const before = fetchedDays().length;
  d.querySelector('[data-range="all"]').click();         // alles staat al in het geheugen: toch even het skelet
  assert.equal(d.getElementById('loader').hidden, false);
  await settle(d);
  assert.equal(fetchedDays().length, before);
  assert.equal(upTimes(d), 12);
  dom.window.close();
});

test('loading finishes in a hidden tab, where requestAnimationFrame never fires', async () => {
  const dom = await load(POOL, 'http://dash/?lang=nl', ['en-US'], null, (w) => { w.requestAnimationFrame = () => 0; });
  const d = dom.window.document;
  assert.equal(d.getElementById('dash').hidden, false);
  assert.equal(d.getElementById('loader').hidden, true);
  dom.window.close();
});
