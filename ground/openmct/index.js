/*
 * Live: Open MCT against Yamcs (fprime-project). Replaces ground/openmct/index.js; scripts/start_openmct.sh
 * copies this directory into external/openmct-yamcs/example/ and webpack serves it on :9000.
 */
import installYamcsPlugins from '../src/openmct-yamcs.js';
import { installCommon } from './doomsat/common.js';

const config = {
  yamcsDictionaryEndpoint: 'http://localhost:9000/yamcs-proxy/',
  yamcsHistoricalEndpoint: 'http://localhost:9000/yamcs-proxy/',
  yamcsWebsocketEndpoint: 'ws://localhost:9000/yamcs-proxy-ws/',
  yamcsUserEndpoint: 'http://localhost:9000/yamcs-proxy/api/user/',
  yamcsInstance: 'fprime-project',
  yamcsProcessor: 'realtime',
  yamcsFolder: 'fprime-project',
  throttleRate: 1000,
  maxBufferSize: 1000000
  // planExecutionStatusParameter removed: it named /fprime-project/PlanExecutionStatus, which no MDB defines
};
// Keys match the OperatorStatus enumeration in yamcs/mdb/doom-ops.xtce.xml
const STATUS_STYLES = {
  NO_STATUS: { iconClass: 'icon-question-mark', iconClassPoll: 'icon-status-poll-question-mark' },
  GO: { iconClass: 'icon-check', iconClassPoll: 'icon-status-poll-question-mark', statusClass: 's-status-ok',
    statusBgColor: '#33cc33', statusFgColor: '#000' },
  MAYBE: { iconClass: 'icon-alert-triangle', iconClassPoll: 'icon-status-poll-question-mark',
    statusClass: 's-status-warning', statusBgColor: '#ffb66c', statusFgColor: '#000' },
  NO_GO: { iconClass: 'icon-circle-slash', iconClassPoll: 'icon-status-poll-question-mark',
    statusClass: 's-status-error', statusBgColor: '#9900cc', statusFgColor: '#fff' }
};

const openmct = window.openmct;
openmct.setAssetPath('/node_modules/openmct/dist');
installCommon(openmct, {
  displaysUrl: '/displays/doomsat-displays.json',
  commanding: new URLSearchParams(window.location.search).get('commanding') === 'on',
  yamcs: { url: '/yamcs-proxy/', instance: config.yamcsInstance, processor: config.yamcsProcessor }
});
openmct.install(installYamcsPlugins(config));
openmct.install(openmct.plugins.OperatorStatus({ statusStyles: STATUS_STYLES }));
document.addEventListener('DOMContentLoaded', () => openmct.start(document.getElementById('app')));
