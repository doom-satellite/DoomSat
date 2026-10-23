/*
 * What every DoomSat Open MCT page installs, live or replay. Open MCT already installs plots, tables, tabs,
 * flexible layouts, imagery, gauges, conditions and web pages by default; this adds the rest of the kit the
 * displays use, the read-only "DoomSat Displays" root, and the DoomSat views.
 */
import DoomSatPlugin from './plugin.js';

const ONE_MINUTE = 60 * 1000;

export function installCommon(openmct, { displaysUrl = 'displays/doomsat-displays.json', commanding = false,
  yamcs } = {}) {
  const theme = new URLSearchParams(window.location.search).get('theme');
  openmct.install(theme === 'snow' ? openmct.plugins.Snow()
    : theme === 'darkmatter' ? openmct.plugins.DarkmatterTheme() : openmct.plugins.Espresso());
  openmct.install(openmct.plugins.LocalStorage());
  openmct.install(openmct.plugins.MyItems());
  openmct.install(openmct.plugins.UTCTimeSystem());
  openmct.install(openmct.plugins.Conductor({
    menuOptions: [
      { name: 'Realtime', timeSystem: 'utc', clock: 'local', clockOffsets: { start: -5 * ONE_MINUTE, end: 5000 } },
      { name: 'Fixed', timeSystem: 'utc', bounds: { start: Date.now() - 30 * ONE_MINUTE, end: Date.now() } }
    ]
  }));
  openmct.install(openmct.plugins.DisplayLayout({
    showAsView: ['summary-widget', 'yamcs.image', 'doomsat.sectors', 'doomsat.candidates', 'doomsat.command',
      'conditionWidget', 'gauge', 'clock', 'timer', 'hyperlink']
  }));
  openmct.install(openmct.plugins.LADTable());
  openmct.install(openmct.plugins.BarChart());
  openmct.install(openmct.plugins.ScatterPlot());
  // Events in a time strip as markers on the shared clock, not a table (Open MCT 4.1 and later)
  const timeline = openmct.plugins.Timeline();
  openmct.install(timeline);
  if (openmct.plugins.EventTimestripPlugin) {
    openmct.install(openmct.plugins.EventTimestripPlugin(timeline.extendedLinesBus));
  }
  openmct.install(openmct.plugins.PlanLayout({ creatable: true }));
  openmct.install(openmct.plugins.Timelist());
  openmct.install(openmct.plugins.Notebook());
  openmct.install(openmct.plugins.Hyperlink());
  openmct.install(openmct.plugins.Clock({ enableClockIndicator: true }));
  openmct.install(openmct.plugins.Timer());
  openmct.install(openmct.plugins.FaultManagement());
  openmct.install(openmct.plugins.TelemetryMean());
  // Newer than Open MCT 4.1.0: present in 4.3.x and master, skipped gracefully on older builds
  ['DerivedTelemetry', 'CorrelationTelemetry'].forEach((p) => {
    if (openmct.plugins[p]) openmct.install(openmct.plugins[p]());
  });
  openmct.install(openmct.plugins.ClearData(['table', 'telemetry.plot.overlay', 'telemetry.plot.stacked']));
  openmct.install(DoomSatPlugin({ commanding, yamcs }));
  // The mission's displays, as code: read-only here, "Duplicate" into My Items to edit a copy.
  openmct.install(openmct.plugins.StaticRootPlugin({ namespace: 'doomsat', exportUrl: displaysUrl }));
}
