/*
 * DoomSat plugin for Open MCT: three views that no built-in view can draw, all fed through the ordinary
 * telemetry API, so they work the same against Yamcs live and against the replay provider.
 *
 *   doomsat.sectors     Sector Radar. The payload's eight-direction sensing around the player: how far the way
 *                       is open (map rays; forward, the range camera), how much of the ground is new, where doors
 *                       are, plus the bearings to the enemy, the exit, a key, pickups and Sonnet's exploration
 *                       hint. Forward is up.
 *   doomsat.candidates  Candidate Board. What the onboard world model offered (CAND0..7), what jev scored each
 *                       one (DoomGround/Score0..7), which one code picked and why, drawn as a table beside a plan
 *                       view of the player's traverse, the candidates and the pick.
 *   doomsat.command     A guarded command button, kept for later: no generated display uses it. Disabled unless
 *                       the page was installed with {commanding: true}. Its default, SET_GOAL HOLD, only changes
 *                       the GOAL the payload reports; nothing on board stops for it.
 */
const NS = 'taxonomy';
const DOOM = '/DoomSat_DoomSat/DoomSat/doom';
const GROUND = '/DoomGround';
const id = (q) => ({ namespace: NS, key: q.replace(/\//g, '~') });

const SECTORS = [
  // name, bearing (degrees, positive left, 0 = ahead), clearance channel, novelty channel, door channel.
  // CLEAR_FWD is the range camera, the other clearances map rays; NEW_* is a percentage, or 255 for unseen ground.
  ['FWD', 0, 'CLEAR_FWD', 'NEW_FWD', 'DOOR_FWD'],
  ['AL', 45, 'CLEAR_AL', 'NEW_AL', 'DOOR_AL'],
  ['LEFT', 90, 'CLEAR_LEFT', 'NEW_LEFT', 'DOOR_LEFT'],
  ['BL', 135, 'CLEAR_BL', 'NEW_BL', 'DOOR_BL'],
  ['BACK', 180, 'CLEAR_BACK', 'NEW_BACK', 'DOOR_BACK'],
  ['BR', -135, 'CLEAR_BR', 'NEW_BR', 'DOOR_BR'],
  ['RIGHT', -90, 'CLEAR_RIGHT', 'NEW_RIGHT', 'DOOR_RIGHT'],
  ['AR', -45, 'CLEAR_AR', 'NEW_AR', 'DOOR_AR']
];
const KIND_COLOR = { FRONTIER: '#6fa8dc', DOOR: '#e69138', EXIT: '#6aa84f', KEY: '#f1c232', ITEM: '#8e7cc3',
  SWITCH: '#c27ba0', ENEMY: '#cc0000' };
const MODE_COLOR = { EXPLORE: '#3d85c6', APPROACH: '#45818e', OPERATE: '#8e7cc3', FIGHT: '#cc0000',
  RETREAT: '#e69138', RECOVER: '#bf9000' };

// A datum's time in ms. openmct-yamcs gives Yamcs' generationTime as an ISO string, the replay a number.
const ms = (d) => { const t = d.timestamp ?? d.utc; return typeof t === 'number' ? t : Date.parse(t); };

/* Latest value of a set of parameters, and the series of a few, honouring the time conductor. A bounds change
 * the user makes, and a switch of mode, clears both and asks again within the new bounds. Only real time
 * subscribes: in Fixed mode nothing live reaches the view, so a review keeps showing its moment. In real time
 * each tick drops the samples older than the start bound, so the traverse keeps to the conductor's window.
 * onChange gets ({qualifiedName: value}, {qualifiedName: [[ms, value], ...]}). */
class Latest {
  constructor(openmct, names, onChange, { history = [] } = {}) {
    this.openmct = openmct;
    this.names = names;
    this.history = history; // names whose whole in-bounds series is wanted (the traverse)
    this.onChange = onChange;
    this.values = {};
    this.series = {};
    this.unsubs = [];
    this.objects = {};
    this.asked = 0; // which refresh is current: an older one that answers late is dropped
    this.boundsListener = (bounds, tick) => {
      if (tick && !this.askOnTick) { this.trim(bounds.start); return; }
      this.askOnTick = false;
      this.refresh();
    };
    // The conductor's mode menu sets no bounds: Fixed keeps the last window, real time has its own at the next tick
    this.modeListener = () => {
      this.follow();
      if (this.openmct.time.isRealTime()) this.askOnTick = true; else this.refresh();
    };
    openmct.time.on('boundsChanged', this.boundsListener);
    openmct.time.on('modeChanged', this.modeListener);
    this.load();
  }
  async load() {
    await Promise.all([...new Set([...this.names, ...this.history])].map(async (q) => {
      try {
        this.objects[q] = await this.openmct.objects.get(id(q));
      } catch (e) { /* unknown to this dictionary: shows as blank */ }
    }));
    this.follow();
    this.refresh();
  }
  follow() {
    const live = this.openmct.time.isRealTime() && !this.destroyed;
    if (!live) {
      this.unsubs.forEach((u) => u());
      this.unsubs = [];
      return;
    }
    if (this.unsubs.length) return;
    Object.entries(this.objects).forEach(([q, o]) => {
      if (!o || !this.openmct.telemetry.isTelemetryObject(o)) return;
      this.unsubs.push(this.openmct.telemetry.subscribe(o, (d) => {
        if (!this.openmct.time.isRealTime()) return; // a late delivery after the switch to Fixed
        const t = ms(d);
        this.values[q] = d.value;
        if (this.history.includes(q) && Number.isFinite(t)) {
          (this.series[q] = this.series[q] || []).push([t, d.value]);
          this.trim(this.openmct.time.getBounds().start, false);
        }
        this.emit();
      }));
    });
  }
  async refresh() {
    const asked = ++this.asked;
    const { start, end } = this.openmct.time.getBounds();
    this.values = {}; // nothing from the old bounds survives; what arrives live while asking is kept
    this.series = {};
    await Promise.all(Object.entries(this.objects).map(async ([q, o]) => {
      if (!o || !this.openmct.telemetry.isTelemetryObject(o)) return;
      try {
        if (this.names.includes(q)) {
          // openmct-yamcs answers 'latest' with [undefined] for a parameter that has never had a value
          const r = (await this.openmct.telemetry.request(o, { start, end, strategy: 'latest', size: 1 })).filter(Boolean);
          if (asked === this.asked && r.length && !(q in this.values)) this.values[q] = r[r.length - 1].value;
        }
        if (this.history.includes(q)) {
          const r = (await this.openmct.telemetry.request(o, { start, end })).filter(Boolean)
            .map((d) => [ms(d), d.value]).filter((p) => Number.isFinite(p[0]));
          if (asked !== this.asked) return;
          const last = r.length ? r[r.length - 1][0] : -Infinity;
          this.series[q] = r.concat((this.series[q] || []).filter((p) => p[0] > last));
        }
      } catch (e) { /* no answer (Yamcs down): shows as blank until the next bounds change */ }
    }));
    if (asked === this.asked) this.emit();
  }
  trim(start, emit = true) {
    let cut = false;
    Object.values(this.series).forEach((s) => {
      let k = 0;
      while (k < s.length && s[k][0] < start) k++;
      if (k) { s.splice(0, k); cut = true; }
    });
    if (cut && emit) this.emit();
  }
  emit() {
    if (!this.pending && !this.destroyed) {
      this.pending = requestAnimationFrame(() => { this.pending = null; this.onChange(this.values, this.series); });
    }
  }
  destroy() {
    this.destroyed = true;
    this.follow();
    this.openmct.time.off('boundsChanged', this.boundsListener);
    this.openmct.time.off('modeChanged', this.modeListener);
    if (this.pending) cancelAnimationFrame(this.pending);
  }
}

const svg = (w, h, body) => `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:100%;display:block"
  xmlns="http://www.w3.org/2000/svg" font-family="Helvetica Neue, Arial, sans-serif">${body}</svg>`;
const polar = (cx, cy, r, bearing) => {
  const a = (bearing * Math.PI) / 180;
  return [cx - r * Math.sin(a), cy - r * Math.cos(a)];
};
const num = (v) => (v === undefined || v === null || Number.isNaN(Number(v)) ? undefined : Number(v));

/* POS_X and POS_Y as one traverse, paired by time: each sample moves one coordinate and the other holds its last
 * value. The two series need not be the same length (the replay drops a value that repeats). */
function pairByTime(xs, ys) {
  const out = [];
  let i = 0; let j = 0; let x; let y;
  while (i < xs.length || j < ys.length) {
    const t = Math.min(xs[i]?.[0] ?? Infinity, ys[j]?.[0] ?? Infinity);
    if (!Number.isFinite(t)) break;
    while (i < xs.length && xs[i][0] === t) x = num(xs[i++][1]) ?? x;
    while (j < ys.length && ys[j][0] === t) y = num(ys[j++][1]) ?? y;
    if (x !== undefined && y !== undefined) out.push([x, y]);
  }
  return out;
}

function renderSectors(v) {
  const W = 320; const H = 320; const cx = 160; const cy = 170; const R = 120; const MAX = 400;
  const g = (k) => v[`${DOOM}/${k}`];
  let s = `<rect width="${W}" height="${H}" fill="none"/>`;
  [0.25, 0.5, 0.75, 1].forEach((f) => {
    s += `<circle cx="${cx}" cy="${cy}" r="${R * f}" fill="none" stroke="#555" stroke-dasharray="2 3"/>`;
  });
  s += `<text x="${cx + R + 4}" y="${cy + 4}" fill="#888" font-size="9">${MAX}u</text>`;
  SECTORS.forEach(([name, b, ck, nk, dk]) => {
    const clear = Math.min(num(g(ck)) ?? 0, MAX);
    const novelty = num(g(nk));
    const door = num(g(dk)) || 0;
    const r = (clear / MAX) * R;
    const [x1, y1] = polar(cx, cy, r, b + 20);
    const [x2, y2] = polar(cx, cy, r, b - 20);
    const fill = novelty === undefined ? '#444' : `rgba(106,168,79,${0.12 + 0.5 * Math.min(novelty, 100) / 100})`;
    s += `<path d="M${cx},${cy} L${x1},${y1} A${r},${r} 0 0 1 ${x2},${y2} Z" fill="${fill}" stroke="#9fc5e8" stroke-width="1"/>`;
    const [lx, ly] = polar(cx, cy, R + 14, b);
    s += `<text x="${lx}" y="${ly + 3}" fill="#ccc" font-size="10" text-anchor="middle">${name}</text>`;
    const [nx, ny] = polar(cx, cy, Math.max(r - 12, 16), b);
    if (novelty !== undefined) {
      s += `<text x="${nx}" y="${ny + 3}" fill="#fff" font-size="9" text-anchor="middle">${novelty === 255 ? 'new' : `${novelty}%`}</text>`;
    }
    if (door > 0) {
      const dr = Math.min((door * 8) / MAX, 1) * R;
      const [dx1, dy1] = polar(cx, cy, dr, b + 12);
      const [dx2, dy2] = polar(cx, cy, dr, b - 12);
      s += `<line x1="${dx1}" y1="${dy1}" x2="${dx2}" y2="${dy2}" stroke="#e69138" stroke-width="4"/>`;
    }
  });
  const marker = (bearing, dist, color, label, shape) => {
    const d = num(dist);
    if (!d) return '';
    const [x, y] = polar(cx, cy, Math.min(d / MAX, 1.08) * R, num(bearing) || 0);
    const body = shape === 'tri'
      ? `<polygon points="${x},${y - 7} ${x - 6},${y + 5} ${x + 6},${y + 5}" fill="${color}"/>`
      : `<circle cx="${x}" cy="${y}" r="5.5" fill="${color}" stroke="#000"/>`;
    return body + `<text x="${x + 8}" y="${y + 3}" fill="${color}" font-size="10" font-weight="bold" stroke="#000" stroke-width="3" paint-order="stroke">${label} ${Math.round(d)}u</text>`;
  };
  if ((num(g('ENEMY_COUNT')) || 0) > 0) s += marker(g('ENEMY_BEARING'), g('ENEMY_DIST'), '#ff4d4d', `ENEMY x${g('ENEMY_COUNT')}`);
  s += marker(g('EXIT_BEARING'), g('EXIT_DIST'), '#6aa84f', 'EXIT', 'tri');
  s += marker(g('KEY_BEARING'), g('KEY_DIST'), '#f1c232', 'KEY', 'tri');
  s += marker(g('HEALTH_BEARING'), g('HEALTH_ITEM_DIST'), '#e06666', '+HP');
  s += marker(g('AMMO_BEARING'), g('AMMO_ITEM_DIST'), '#b4a7d6', 'AMMO');
  s += marker(g('ARMOR_BEARING'), g('ARMOR_ITEM_DIST'), '#76a5af', 'ARM');
  if (g('HINT_ACTIVE') === 'True' || g('HINT_ACTIVE') === true) {
    const [hx, hy] = polar(cx, cy, R + 2, num(g('HINT_REL')) || 0);
    s += `<line x1="${cx}" y1="${cy}" x2="${hx}" y2="${hy}" stroke="#c27ba0" stroke-width="2" stroke-dasharray="5 3"/>`;
    s += `<text x="${hx}" y="${hy - 4}" fill="#c27ba0" font-size="9" text-anchor="middle">SONNET HINT</text>`;
  }
  s += `<polygon points="${cx},${cy - 9} ${cx - 6},${cy + 6} ${cx + 6},${cy + 6}" fill="#fff"/>`;
  const ang = num(g('ANGLE'));
  const stuck = g('STUCK') === 'True' || g('STUCK') === true;
  s += `<text x="8" y="16" fill="#ddd" font-size="11">HDG ${ang === undefined ? '--' : ang.toFixed(0) + '°'}</text>`;
  s += `<text x="${W - 8}" y="16" fill="${stuck ? '#ff4d4d' : '#999'}" font-size="11" text-anchor="end">${stuck ? 'STUCK' : 'moving'}</text>`;
  s += `<text x="8" y="${H - 8}" fill="#ddd" font-size="11">AHEAD ${g('AHEAD_KIND') ?? '--'} ${num(g('AHEAD_DIST')) ? Math.round(g('AHEAD_DIST')) + 'u' : ''}</text>`;
  s += `<text x="${W - 8}" y="${H - 8}" fill="#888" font-size="9" text-anchor="end">fill = new ground, orange = door</text>`;
  return svg(W, H, s);
}

function renderCandidates(v, series) {
  const g = (q) => v[q];
  const slots = [];
  const count = num(g(`${DOOM}/CAND_COUNT`));
  for (let n = 0; n < 8; n++) {
    const b = `${DOOM}/CAND${n}`;
    const kind = g(`${b}.kind`);
    if (kind === undefined || (count !== undefined && n >= count)) continue;
    slots.push({ n, kind, x: num(g(`${b}.x`)), y: num(g(`${b}.y`)), path: num(g(`${b}.pathUnits`)),
      novelty: num(g(`${b}.novelty`)), threat: num(g(`${b}.threatCount`)), score: num(g(`${GROUND}/Score${n}`)) });
  }
  const pick = num(g(`${GROUND}/PickSlot`));
  const mode = g(`${GROUND}/IntentMode`);
  const src = g(`${GROUND}/DecisionSource`);
  const gap = num(g(`${GROUND}/PickGap`));
  const conf = num(g(`${GROUND}/PickConfidence`));
  const px = num(g(`${DOOM}/POS_X`)); const py = num(g(`${DOOM}/POS_Y`)); const ang = num(g(`${DOOM}/ANGLE`));

  // ---- plan view: traverse, candidates, pick
  const W = 360; const H = 300; const pad = 16;
  const trail = pairByTime(series[`${DOOM}/POS_X`] || [], series[`${DOOM}/POS_Y`] || []);
  const pts = [...trail, ...slots.filter((c) => c.x !== undefined).map((c) => [c.x, c.y])];
  if (px !== undefined) pts.push([px, py]);
  let plan = '';
  if (pts.length) {
    const xs = pts.map((p) => p[0]); const ys = pts.map((p) => p[1]);
    const x0 = Math.min(...xs); const x1 = Math.max(...xs); const y0 = Math.min(...ys); const y1 = Math.max(...ys);
    const k = Math.min((W - 2 * pad) / Math.max(x1 - x0, 1), (H - 2 * pad) / Math.max(y1 - y0, 1));
    const X = (x) => pad + (x - x0) * k + ((W - 2 * pad) - (x1 - x0) * k) / 2;
    const Y = (y) => H - pad - (y - y0) * k - ((H - 2 * pad) - (y1 - y0) * k) / 2; // map north up
    if (trail.length > 1) {
      plan += `<polyline points="${trail.map((p) => `${X(p[0])},${Y(p[1])}`).join(' ')}" fill="none" stroke="#9fc5e8" stroke-width="1.5" opacity="0.8"/>`;
    }
    slots.forEach((c) => {
      if (c.x === undefined) return;
      const r = 3 + Math.max(0, c.score || 0);
      const picked = c.n === pick;
      if (picked && px !== undefined) {
        plan += `<line x1="${X(px)}" y1="${Y(py)}" x2="${X(c.x)}" y2="${Y(c.y)}" stroke="#fff" stroke-dasharray="4 3"/>`;
      }
      plan += `<circle cx="${X(c.x)}" cy="${Y(c.y)}" r="${r}" fill="${KIND_COLOR[c.kind] || '#aaa'}" opacity="0.85" stroke="${picked ? '#fff' : 'none'}" stroke-width="2"/>`;
      plan += `<text x="${X(c.x) + r + 2}" y="${Y(c.y) + 3}" fill="#ddd" font-size="9">t${c.n}</text>`;
    });
    if (px !== undefined) {
      const a = ((ang || 0) * Math.PI) / 180;
      const hx = X(px) + 14 * Math.cos(a); const hy = Y(py) - 14 * Math.sin(a);
      plan += `<line x1="${X(px)}" y1="${Y(py)}" x2="${hx}" y2="${hy}" stroke="#fff" stroke-width="2"/>`;
      plan += `<circle cx="${X(px)}" cy="${Y(py)}" r="4.5" fill="#fff"/>`;
    }
  } else {
    plan = `<text x="${W / 2}" y="${H / 2}" fill="#888" text-anchor="middle" font-size="12">no position in bounds</text>`;
  }
  plan += `<text x="6" y="12" fill="#888" font-size="9">traverse within the conductor's bounds, candidates sized by jev score</text>`;

  const rows = slots.map((c) => `<tr style="${c.n === pick ? 'background:rgba(255,255,255,.12);font-weight:bold' : ''}">
    <td>t${c.n}</td><td><span style="display:inline-block;width:8px;height:8px;border-radius:4px;background:${KIND_COLOR[c.kind] || '#aaa'}"></span> ${c.kind}</td>
    <td style="text-align:right">${c.score === undefined ? 'n/r' : c.score.toFixed(2)}</td>
    <td style="width:70px"><div style="height:8px;background:#3d85c6;width:${Math.max(0, (c.score || 0) / 9 * 100)}%"></div></td>
    <td style="text-align:right">${c.path ?? 'n/r'}</td><td style="text-align:right">${c.novelty ?? 'n/r'}</td>
    <td style="text-align:right">${c.threat ?? 'n/r'}</td></tr>`).join('');
  const header = `<div style="display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
    <span style="padding:2px 8px;border-radius:3px;background:${MODE_COLOR[mode] || '#555'};color:#fff;font-weight:bold">${mode ?? '--'}</span>
    <span>pick <b>${pick === undefined || pick === 255 ? '-' : 't' + pick}</b></span>
    <span>gap <b>${gap === undefined ? '--' : gap.toFixed(2)}</b></span>
    <span>conf <b>${conf === undefined ? '--' : conf.toFixed(2)}</b></span>
    <span>by <b style="color:${src === 'JEV' ? '#6aa84f' : '#e69138'}">${src ?? '--'}</b></span></div>`;
  return `<div style="display:flex;gap:10px;height:100%;font:12px Helvetica Neue,Arial,sans-serif;color:#ddd;overflow:auto">
    <div style="flex:1 1 55%;min-width:220px">${header}
      <table style="width:100%;border-collapse:collapse" cellpadding="3">
        <tr style="color:#999;text-align:left"><th>slot</th><th>kind</th><th>jev</th><th></th><th>path u</th><th>unseen</th><th>threat</th></tr>
        ${rows || '<tr><td colspan="7" style="color:#888">no candidates offered</td></tr>'}</table>
      <div style="color:#777;font-size:10px;margin-top:6px">n/r = not in this dictionary or recording. Scores on the target head's nine-level rubric.</div></div>
    <div style="flex:1 1 45%;min-width:200px">${svg(W, H, plan)}</div></div>`;
}

function liveView(openmct, domainObject, names, history, render) {
  let latest;
  return {
    show(element) {
      const div = document.createElement('div');
      div.style.cssText = 'width:100%;height:100%;overflow:hidden';
      element.appendChild(div);
      latest = new Latest(openmct, names, (v, s) => { div.innerHTML = render(v, s); }, { history });
    },
    destroy() { latest?.destroy(); }
  };
}

export default function DoomSatPlugin(options = {}) {
  const commanding = Boolean(options.commanding);
  const yamcs = options.yamcs || { url: '/yamcs-proxy/', instance: 'fprime-project', processor: 'realtime' };
  return function install(openmct) {
    openmct.types.addType('doomsat.sectors', { name: 'DoomSat Sector Radar', cssClass: 'icon-telemetry',
      description: 'The payload\'s eight-direction sensing around the player, forward up.', creatable: true,
      initialize: (o) => { o.configuration = {}; } });
    openmct.types.addType('doomsat.candidates', { name: 'DoomSat Candidate Board', cssClass: 'icon-tabular-scrolling',
      description: 'What the world model offered, what jev scored, what code picked.', creatable: true,
      initialize: (o) => { o.configuration = {}; } });
    openmct.types.addType('doomsat.command', { name: 'DoomSat Command Button', cssClass: 'icon-bell',
      description: 'A guarded, two-step command. Disabled unless the page enables commanding.', creatable: true,
      initialize: (o) => { o.configuration = { command: `${DOOM}/SET_GOAL`, args: { goal: 'HOLD' }, label: 'SET_GOAL HOLD' }; },
      form: [{ key: 'label', name: 'Label', control: 'textfield', property: ['configuration', 'label'] }] });

    const sectorNames = [...new Set(SECTORS.flatMap(([, , c, nw, d]) => [c, nw, d]).concat(
      ['ENEMY_COUNT', 'ENEMY_BEARING', 'ENEMY_DIST', 'EXIT_BEARING', 'EXIT_DIST', 'KEY_BEARING', 'KEY_DIST',
        'HEALTH_BEARING', 'HEALTH_ITEM_DIST', 'AMMO_BEARING', 'AMMO_ITEM_DIST', 'ARMOR_BEARING', 'ARMOR_ITEM_DIST',
        'HINT_ACTIVE', 'HINT_REL', 'ANGLE', 'STUCK', 'AHEAD_KIND', 'AHEAD_DIST']))].map((k) => `${DOOM}/${k}`);
    const candNames = [`${DOOM}/CAND_COUNT`, `${DOOM}/POS_X`, `${DOOM}/POS_Y`, `${DOOM}/ANGLE`,
      `${GROUND}/PickSlot`, `${GROUND}/IntentMode`, `${GROUND}/DecisionSource`, `${GROUND}/PickGap`,
      `${GROUND}/PickConfidence`];
    for (let n = 0; n < 8; n++) {
      ['kind', 'x', 'y', 'pathUnits', 'novelty', 'threatCount'].forEach((m) => candNames.push(`${DOOM}/CAND${n}.${m}`));
      candNames.push(`${GROUND}/Score${n}`);
    }

    openmct.objectViews.addProvider({
      key: 'doomsat.sectors.view', name: 'Sector Radar', cssClass: 'icon-telemetry',
      canView: (d) => d.type === 'doomsat.sectors',
      view: (d) => liveView(openmct, d, sectorNames, [], renderSectors)
    });
    openmct.objectViews.addProvider({
      key: 'doomsat.candidates.view', name: 'Candidate Board', cssClass: 'icon-tabular-scrolling',
      canView: (d) => d.type === 'doomsat.candidates',
      view: (d) => liveView(openmct, d, candNames, [`${DOOM}/POS_X`, `${DOOM}/POS_Y`], renderCandidates)
    });
    openmct.objectViews.addProvider({
      key: 'doomsat.command.view', name: 'Command Button', cssClass: 'icon-bell',
      canView: (d) => d.type === 'doomsat.command',
      view: (d) => ({
        show(element) {
          const c = d.configuration || {};
          const b = document.createElement('button');
          b.className = 'c-button';
          b.style.cssText = 'width:100%;height:100%;font-weight:bold';
          b.textContent = commanding ? c.label : `${c.label} (commanding disabled)`;
          b.disabled = !commanding;
          b.title = commanding ? `${c.command} ${JSON.stringify(c.args)}; counts as a human command`
            : 'Enable with DoomSatPlugin({commanding: true}). Code owns the loop.';
          b.onclick = async () => {
            const ok = window.confirm(`Send ${c.command} ${JSON.stringify(c.args)}?\n\nThis is a human command. ` +
              'Yamcs command history records it as human-origin.');
            if (!ok) return;
            const url = `${yamcs.url}api/processors/${yamcs.instance}/${yamcs.processor}/commands${c.command}`;
            const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ args: c.args, comment: 'human-origin: Open MCT' }) });
            openmct.notifications[r.ok ? 'info' : 'error'](`${c.command}: ${r.ok ? 'queued' : r.status}`);
          };
          element.appendChild(b);
        },
        destroy() {}
      })
    });
  };
}
