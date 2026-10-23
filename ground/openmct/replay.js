/*
 * Replay: the DoomSat displays against a recorded flight, no Yamcs, no build step.
 *   python3 tools/openmct_serve.py     then open http://localhost:8071/replay.html
 * ?pack=replay/flight-32/pack.json   which recording
 * ?anchor=0                          serve recorded timestamps (use with a fixed time conductor)
 * (no anchor)                        play as if live from now, looping
 */
import { installCommon } from './doomsat/common.js';
import DoomSatReplayPlugin from './doomsat/replay.js';

const openmct = window.openmct;
const q = new URLSearchParams(window.location.search);
openmct.setAssetPath('node_modules/openmct/dist');
installCommon(openmct, { commanding: false, displaysUrl: 'displays/doomsat-displays.replay.json' });
openmct.install(DoomSatReplayPlugin({ packUrl: q.get('pack') || 'replay/flight-32/pack.json' }));
openmct.start(document.getElementById('app'));
