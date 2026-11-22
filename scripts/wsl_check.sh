#!/bin/bash
# Quick health check of the flight side: payload, Yamcs, telemetry, frames, events, links.
. "$(dirname "$0")/common.sh"
Y=http://localhost:8090
echo "=== processes ==="; ps aux | grep -E "doom_payloa[d]|fprime_yamc[s]|YamcsServe[r]|bin/DoomSa[t]" | awk '{print $11, $12}' | sort | uniq -c
echo "=== payload ==="; tail -2 "$RUN/payload.log"
echo "=== yamcs log ==="; grep -i -E "error|exception|XTCE file parsing|Shutting|Traceback|Instance .* failed" "$RUN/yamcs.log" | grep -v "sun.misc\|loadLibrary" | tail -6
echo "=== params ==="   # a channel Yamcs does not know comes back as an error ({code, type, msg}): MISSING
for c in HEALTH ENEMY_COUNT AHEAD_KIND CLEAR_FWD GOAL TIC FRAMES_SENT CHUNKS_SENT FRAME_BYTES PAYLOAD_LINK CMDS_RECEIVED EPISODE WAD_LOADS; do
  curl -s "$Y/api/processors/fprime-project/realtime/parameters/DoomSat_DoomSat/DoomSat/doom/$c" | python3 -c "import sys,json; d=json.load(sys.stdin); print('$c', 'MISSING: ' + d['msg'] if 'msg' in d else d.get('engValue'), d.get('generationTime','')[11:23])" 2>/dev/null
done
echo "=== WAD (LOAD_WAD) ==="   # names travel as zero-padded bytes: F' string telemetry does not survive fprime-xtce
for c in WAD_IWAD WAD_PWAD; do
  curl -s "$Y/api/processors/fprime-project/realtime/parameters/DoomSat_DoomSat/DoomSat/doom/$c" | python3 -c "
import sys,json,base64; d=json.load(sys.stdin); v=d.get('engValue',{}).get('binaryValue')
print('$c', repr(base64.b64decode(v).rstrip(b'\\0').decode('ascii','replace')) if v else 'MISSING: ' + d['msg'] if 'msg' in d else None, d.get('generationTime','')[11:23])" 2>/dev/null
done
echo "=== FRAME_CHUNK type ==="; curl -s "$Y/api/mdb/fprime-project/parameters/DoomSat_DoomSat/DoomSat/doom/FRAME_CHUNK" | python3 -c "
import sys,json; d=json.load(sys.stdin); t=d.get('type',{}); print(t.get('engType'), [(m['name'], m['type']['engType']) for m in t.get('member',[])])" 2>/dev/null
echo "=== FRAME_CHUNK last value ==="; curl -s "$Y/api/processors/fprime-project/realtime/parameters/DoomSat_DoomSat/DoomSat/doom/FRAME_CHUNK" | python3 -c "
import sys,json; d=json.load(sys.stdin); v=d.get('engValue',{}); 
agg=v.get('aggregateValue',{}); print({n: (val.get('uint32Value') or val.get('sint32Value') or (val.get('binaryValue') or '')[:24]) for n,val in zip(agg.get('name',[]), agg.get('value',[]))}, d.get('generationTime','')[11:23])" 2>/dev/null
echo "=== ground params ==="; curl -s "$Y/api/mdb/fprime-project/parameters?q=DoomFrame" | grep qualifiedName | head -2
echo "=== events ==="; curl -s "$Y/api/archive/fprime-project/events?limit=6&order=desc" | grep -E "\"message\"" | head -6
echo "=== links ==="; curl -s $Y/api/links/fprime-project | grep -E "\"name\"|dataInCount|dataOutCount" | paste - - - | head -4
