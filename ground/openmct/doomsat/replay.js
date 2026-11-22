/*
 * DoomSat replay provider for Open MCT.
 *
 * Serves one recorded flight (a pack built by tools/build_openmct_replay.py) under exactly the identifiers
 * the openmct-yamcs plugin uses live: namespace "taxonomy", key = Yamcs qualified name with "/" -> "~".
 * So the same display JSON runs against Yamcs in flight and against this in replay, with no edits.
 *
 * Time. With ?anchor=<epoch ms> the recording is served at its own timestamps shifted so that it starts at
 * the anchor (anchor=0 means "as recorded"); use that with a fixed time conductor for reviews and screenshots.
 * Without an anchor the recording plays as if live, starting now, and loops.
 *
 * Honesty. Values arrive with the provenance the pack gives them. Parameters the recording never carried
 * return no data rather than invented data, so an empty panel in replay means "not recorded", never "zero".
 */
const NS = 'taxonomy';
const ROOT_KEY = 'spacecraft';
const LEVELS = ['watch', 'warning', 'distress', 'critical', 'severe'];
const LIMIT_COLORS = { watch: 'cyan', warning: 'yellow', distress: 'orange', critical: 'red', severe: 'purple' };
const LIMIT_CSS = { watch: 'is-limit--yellow', warning: 'is-limit--yellow', distress: 'is-limit--red',
  critical: 'is-limit--red', severe: 'is-limit--red' };
const SEVERITY_RANK = { INFO: 0, WATCH: 1, WARNING: 2, DISTRESS: 3, CRITICAL: 4, SEVERE: 5 };

const toId = (q) => q.replace(/\//g, '~');
const toQ = (id) => id.replace(/~/g, '/');

export default function DoomSatReplayPlugin(options = {}) {
  const packUrl = options.packUrl;
  const params = new URLSearchParams(window.location.search);
  const anchorParam = params.get('anchor');

  return function install(openmct) {
    const pack = fetch(packUrl).then((r) => r.json());
    const base = packUrl.replace(/[^/]*$/, '');
    let offset = 0;
    let period = 0;
    const ready = pack.then((p) => {
      period = p.meta.t1 - p.meta.t0 + 5000;
      offset = anchorParam === null ? Date.now() - p.meta.t0
        : (Number(anchorParam) === 0 ? 0 : Number(anchorParam) - p.meta.t0);
      return p;
    });
    const live = anchorParam === null;

    // The shift from recorded to displayed time; when live it advances one period per loop.
    function loopShift(p) {
      if (!live) return offset;
      const elapsed = Date.now() - (p.meta.t0 + offset);
      return offset + Math.max(0, Math.floor(elapsed / period)) * period;
    }

    ['yamcs.telemetry', 'yamcs.image', 'yamcs.string', 'yamcs.aggregate', 'yamcs.events',
      'yamcs.events.severity', 'yamcs.commands'].forEach((key) => {
      if (!openmct.types.listKeys().includes(key)) {
        openmct.types.addType(key, { name: key.replace('yamcs.', 'Replay ') , cssClass: 'icon-telemetry' });
      }
    });

    // ---------------------------------------------------------------- objects
    const objects = {};
    function folder(key, name, location) {
      objects[key] = objects[key] || { identifier: { namespace: NS, key }, name, type: 'folder',
        location, composition: [] };
      return objects[key];
    }
    function valueMetadata(entry) {
      const v = { key: 'value', name: 'Value', hints: { range: 1 } };
      if (entry.unit) v.unit = entry.unit;
      if (entry.eng === 'enumeration') {
        v.format = 'enum';
        v.enumerations = (entry.enum || []).map(([value, string]) => ({ value, string }));
      } else if (entry.eng === 'string') {
        v.format = 'string';
        v.hints = {};
      }
      return v;
    }
    function limitsFor(entry) {
      if (!entry.alarms) return undefined;
      const out = {};
      LEVELS.forEach((lvl) => {
        const r = entry.alarms[lvl];
        if (!r) return;
        out[lvl.toUpperCase()] = { low: { color: LIMIT_COLORS[lvl], value: r[0] ?? undefined },
          high: { color: LIMIT_COLORS[lvl], value: r[1] ?? undefined } };
      });
      return out;
    }

    const built = ready.then((p) => {
      const root = folder(ROOT_KEY, `fprime-project (REPLAY ${p.meta.name})`, 'ROOT');
      const images = new Set(Object.keys(p.images));
      Object.entries(p.dictionary).forEach(([q, entry]) => {
        const [path, member] = q.split('.');
        const parts = path.split('/').filter(Boolean);
        let parent = root;
        for (let i = 0; i < parts.length - 1; i++) {
          const fk = toId('/' + parts.slice(0, i + 1).join('/'));
          const f = folder(fk, parts[i], openmct.objects.makeKeyString(parent.identifier));
          if (!parent.composition.find((c) => c.key === fk)) parent.composition.push(f.identifier);
          parent = f;
        }
        const id = toId(q);
        let type = 'yamcs.telemetry';
        if (images.has(q)) type = 'yamcs.image';
        else if (entry.eng === 'string') type = 'yamcs.string';
        else if (entry.eng === 'aggregate') type = 'yamcs.aggregate';
        const obj = {
          identifier: { namespace: NS, key: id },
          name: member ? `${parts[parts.length - 1]}.${member}` : parts[parts.length - 1],
          type,
          location: openmct.objects.makeKeyString((member ? objects[toId(path)] : parent)?.identifier
            || parent.identifier),
          configuration: {},
          telemetry: { values: [{ key: 'utc', source: 'timestamp', name: 'Timestamp', format: 'utc',
            hints: { domain: 1 } }] }
        };
        if (type === 'yamcs.aggregate') {
          obj.composition = [];
        } else if (type === 'yamcs.image') {
          obj.telemetry.values.push({ key: 'value', name: 'Image', format: 'image', hints: { image: 1 } });
        } else {
          obj.telemetry.values.push(valueMetadata(entry));
        }
        const lim = limitsFor(entry);
        if (lim) obj.configuration.limits = lim;
        obj.replay = { recorded: p.meta.provenance.recorded.includes(q),
          derived: p.meta.provenance.derived.includes(q), snapshotLag: (entry.source || '').includes('snapshot lag') };
        objects[id] = obj;
        if (member) {
          const agg = objects[toId(path)];
          if (agg) agg.composition.push(obj.identifier);
        } else {
          parent.composition.push(obj.identifier);
        }
      });
      const evTel = { values: [
        { key: 'utc', source: 'timestamp', name: 'Generation Time', format: 'utc', hints: { domain: 1 } },
        { key: 'severity', name: 'Severity', format: 'string' },
        { key: 'source', name: 'Source', format: 'string' },
        { key: 'message', name: 'Message', format: 'string', hints: { label: 0 } }
      ] };
      objects['yamcs.events'] = { identifier: { namespace: NS, key: 'yamcs.events' }, name: 'Events',
        type: 'yamcs.events', location: `${NS}:${ROOT_KEY}`, telemetry: evTel, composition: [] };
      root.composition.push(objects['yamcs.events'].identifier);
      ['info', 'watch', 'warning', 'distress', 'critical'].forEach((s) => {
        const k = `yamcs.events.severity.${s}`;
        objects[k] = { identifier: { namespace: NS, key: k }, name: `Events: ${s}`, type: 'yamcs.events.severity',
          location: `${NS}:yamcs.events`, telemetry: evTel, severity: s.toUpperCase() };
        objects['yamcs.events'].composition.push(objects[k].identifier);
      });
      objects['yamcs.commands'] = { identifier: { namespace: NS, key: 'yamcs.commands' }, name: 'Commands',
        type: 'yamcs.commands', location: `${NS}:${ROOT_KEY}`, telemetry: { values: [
          { key: 'utc', source: 'timestamp', name: 'Generation Time', format: 'utc', hints: { domain: 1 } },
          { key: 'commandName', name: 'Command', format: 'string' },
          { key: 'args', name: 'Arguments', format: 'string' },
          { key: 'status', name: 'Status', format: 'string' }
        ] } };
      root.composition.push(objects['yamcs.commands'].identifier);
      return p;
    });

    openmct.objects.addRoot({ namespace: NS, key: ROOT_KEY }, openmct.priority.HIGH);
    openmct.objects.addProvider(NS, {
      get(identifier) {
        return built.then(() => {
          const o = objects[identifier.key];
          if (!o) {
            // A name the display asks for that no dictionary knows: surface it, don't hide it.
            return { identifier, name: `MISSING ${toQ(identifier.key)}`, type: 'unknown' };
          }
          return structuredClone(o);
        });
      }
    });

    // ---------------------------------------------------------------- data
    function seriesFor(p, domainObject) {
      const key = domainObject.identifier.key;
      if (key.startsWith('yamcs.events')) {
        const min = domainObject.severity ? SEVERITY_RANK[domainObject.severity] : 0;
        return p.events.filter((e) => SEVERITY_RANK[e[1]] >= min)
          .map(([t, severity, source, message]) => ({ t, datum: { severity, source, message } }));
      }
      if (key === 'yamcs.commands') {
        return p.commands.map(([t, commandName, args]) => ({ t, datum: { commandName,
          args: Object.entries(args).map(([k, v]) => `${k}=${v}`).join(' '),
          status: 'issued (ack not recorded)' } }));
      }
      const q = toQ(key);
      if (p.images[q]) {
        return p.images[q].map(([t, url]) => ({ t, datum: { value: base + url } }));
      }
      const entry = p.dictionary[q] || {};
      return (p.series[q] || []).map(([t, v]) => {
        let value = v;
        if (entry.eng === 'enumeration' && typeof v === 'boolean') value = v ? 'True' : 'False';
        return { t, datum: { value } };
      });
    }
    function toDatum(item, s) {
      return { timestamp: item.t + s, ...item.datum };
    }

    const provider = {
      supportsRequest: (d) => d.identifier.namespace === NS && d.type !== 'folder',
      supportsSubscribe: (d) => d.identifier.namespace === NS && d.type !== 'folder',
      request(domainObject, options = {}) {
        return ready.then((p) => {
          const s = loopShift(p);
          const now = live ? Date.now() : Infinity;
          const end = Math.min(options.end ?? Infinity, now);
          const start = options.start ?? -Infinity;
          const all = seriesFor(p, domainObject);
          if (options.strategy === 'latest' || options.size === 1) {
            let last;
            for (const it of all) {
              if (it.t + s <= end) last = it; else break;
            }
            return last ? [toDatum(last, s)] : [];
          }
          return all.filter((it) => it.t + s >= start && it.t + s <= end).map((it) => toDatum(it, s));
        });
      },
      subscribe(domainObject, callback) {
        if (!live) return () => {};
        let cursor = Date.now();
        let stopped = false;
        let timer;
        ready.then((p) => {
          const all = seriesFor(p, domainObject);
          timer = setInterval(() => {
            if (stopped) return;
            const now = Date.now();
            const s = loopShift(p);
            all.forEach((it) => {
              const t = it.t + s;
              if (t > cursor && t <= now) callback(toDatum(it, s));
            });
            cursor = now;
          }, 250);
        });
        return () => { stopped = true; clearInterval(timer); };
      },
      supportsLimits: (d) => d.identifier.namespace === NS && Boolean(d.configuration?.limits || d.enumAlarms),
      getLimits: (d) => ({ limits: () => Promise.resolve(d.configuration?.limits || {}) }),
      getLimitEvaluator(domainObject) {
        const q = toQ(domainObject.identifier.key);
        return {
          evaluate(datum, valueMetadata) {
            if (!valueMetadata || valueMetadata.key !== 'value') return undefined;
            const entry = cachedPack?.dictionary[q];
            const al = entry?.alarms;
            if (!al) return undefined;
            let hit;
            const v = datum.value;
            if (al.enum) {
              LEVELS.forEach((lvl) => { if ((al.enum[lvl] || []).includes(String(v))) hit = lvl; });
            } else {
              LEVELS.forEach((lvl) => {
                const r = al[lvl];
                if (r && ((r[0] !== null && v < r[0]) || (r[1] !== null && v > r[1]))) hit = lvl;
              });
            }
            return hit ? { cssClass: LIMIT_CSS[hit], name: hit.toUpperCase(), low: -Infinity, high: Infinity }
              : undefined;
          }
        };
      }
    };
    let cachedPack;
    ready.then((p) => { cachedPack = p; });
    openmct.telemetry.addProvider(provider);

    // Tell the operator what they are looking at, in the status bar.
    const indicator = openmct.indicators.simpleIndicator();
    indicator.iconClass('icon-history');
    indicator.statusClass('s-status-caution');
    ready.then((p) => {
      indicator.text(`REPLAY ${p.meta.name}: ${p.meta.rows} decisions, ${p.meta.frames} frames` +
        (live ? ' (looping as live)' : ' (recorded time)'));
      indicator.description(`Recorded ${p.meta.provenance.recorded.length} parameters, derived ` +
        `${p.meta.provenance.derived.length}; everything else was not recorded and shows no data. ` +
        `Not in the committed XTCE snapshot (types from Doom.fpp): ${(p.meta.xtce_snapshot_lag || []).join(', ') || 'none'}.`);
    });
    openmct.indicators.add(indicator);
  };
}
