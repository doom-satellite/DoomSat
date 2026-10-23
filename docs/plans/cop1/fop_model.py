"""Scratch model (NOT the stack): a periodic command stream over a lossy TC link, sent
  (a) as today: Type-BD frames, no retransmission, or
  (b) through Yamcs 5.12.8's FOP-1 (Cop1TcPacketHandler, transcribed below from the source read) against an
      idealised CCSDS FARM-1 on board that reports a CLCW in every TM frame (F' v4.3.0 has neither).

FOP rules transcribed (Y = yamcs-core/src/main/java/org/yamcs/tctm/ccsds/Cop1TcPacketHandler.java):
  queueTC -> waitQueue, lookForFDU if state<=2 (Y:624-631)
  lookForFDU: retransmissions first, then a new frame while sentQueueSize < K (Y:887-915)
  sendADDownstream restarts timer t1 on every AD frame sent (Y:667-669, 991-997); E41 -> lookForFDU (Y:671-676)
  CLCW handling E1..E13 (Y:721-875); timer E104/E18 with timeoutType=1 (Y:924-972)
  initiateADRetransmission marks every frame nnR..vS-1 (go-back-N) and txCount++ (Y:979-989)
  removeAcknowlegedFramesFromSentQueue resets txCount=1 (Y:1007-1016)
Yamcs defaults: K=10, t1=3 s, txLimit=3 (Y:182-184).
One command per frame: Yamcs's multiplePacketsPerFrame defaults to true (TcManagedParameters.java:157), and DoomSat
sets it false (ground/yamcs/etc/yamcs.fprime-project.yaml).
Operator: re-initialises AD with Set V(R) op_delay s (10 s here) after each suspend or alert, purging both queues;
Yamcs's resume (Y:595-611) restores the state and restarts t1 but retransmits nothing. COP-1's undelivered
commands are those purged.
"""
import heapq
import random
import statistics
import sys


def incr(x):
    return (x + 1) & 0xFF


class Sim:
    def __init__(self, rate, p_tc, p_tm, tm_hz, d_up, d_dn, t1, K, tx_limit, dur, seed, op_delay):
        self.rate, self.p_tc, self.p_tm, self.tm_hz = rate, p_tc, p_tm, tm_hz
        self.d_up, self.d_dn, self.t1, self.K, self.tx_limit = d_up, d_dn, t1, K, tx_limit
        self.dur, self.op_delay = dur, op_delay
        self.rng_tc = random.Random(f"{seed}-TC")
        self.rng_tm = random.Random(f"{seed}-TM")
        self.q, self.n = [], 0
        # FOP
        self.state, self.vS, self.nnR, self.txCount = 1, 0, 0, 1
        self.sent = {}            # seq -> [cmd_id, retx]
        self.wait = []            # FIFO of cmd ids
        self.timer_tok = 0
        # FARM
        self.VR, self.R = 0, 0
        # metrics
        self.issue_t, self.arrive_t = {}, {}
        self.arrivals = []        # arrival times of any accepted command (for gap metrics)
        self.suspends, self.alerts, self.dropped = 0, 0, 0
        self.frames_sent = 0
        self.recover_t = -1.0
        self.stall_s = 0.0        # seconds spent suspended or alerted, waiting on the operator

    def at(self, t, kind, data=None):
        self.n += 1
        heapq.heappush(self.q, (t, self.n, kind, data))

    # ---------------- FOP (Yamcs) ----------------
    def start_timer(self, now):
        self.timer_tok += 1
        self.at(now + self.t1, "timer", self.timer_tok)

    def send_ad(self, now, seq):
        self.frames_sent += 1
        self.start_timer(now)
        if self.rng_tc.random() >= self.p_tc:
            self.at(now + self.d_up, "frame", (seq, self.sent[seq][0]))

    def look_for_fdu(self, now):
        while True:
            i, did = self.nnR, False
            while i != self.vS:
                if self.sent[i][1]:
                    self.sent[i][1] = False
                    self.send_ad(now, i)
                    did = True
                    break
                i = incr(i)
            if did:
                if self.state <= 2:
                    continue          # E41 -> lookForFDU again
                return
            size = self.vS - self.nnR if self.nnR <= self.vS else self.vS + 256 - self.nnR
            if size < self.K and self.wait:
                cmd = self.wait.pop(0)
                self.sent[self.vS] = [cmd, False]
                if self.nnR == self.vS:
                    self.txCount = 1
                seq = self.vS
                self.vS = incr(self.vS)
                self.send_ad(now, seq)
                if self.state <= 2:
                    continue
            return

    def remove_acked(self, nR):
        while self.nnR != nR:
            del self.sent[self.nnR]
            self.nnR = incr(self.nnR)
        self.txCount = 1

    def initiate_retx(self):
        self.txCount += 1
        i = self.nnR
        while i != self.vS:
            self.sent[i][1] = True
            i = incr(i)

    def alert(self, now=None):
        self.alerts += 1
        self.dropped += len(self.sent)
        self.sent.clear()
        self.state = 6
        if now is not None:
            self.at(now + self.op_delay, "recover")   # AD terminated: the operator re-initialises too

    def in_window(self, nR):
        if self.nnR <= self.vS:
            return self.nnR <= nR <= self.vS
        return self.nnR <= nR or nR <= self.vS

    def on_clcw(self, now, nR, R):
        if self.state == 6:
            return
        if nR == self.vS:
            if R == 0:
                if nR == self.nnR:
                    if self.state in (2, 3):
                        self.alert(now)
                else:                                   # E2
                    self.timer_tok += 1                 # timer.cancel
                    self.remove_acked(nR)
                    self.state = 1
                    self.look_for_fdu(now)
            else:                                       # E4
                self.alert(now)
        elif self.in_window(nR):
            if R == 0:
                if nR == self.nnR:                      # E5
                    if self.state in (2, 3):
                        self.alert(now)
                else:                                   # E6
                    self.remove_acked(nR)
                    self.state = 1
                    self.look_for_fdu(now)
            else:
                if nR != self.nnR:                      # E8
                    self.remove_acked(nR)
                    self.initiate_retx()
                    self.state = 2
                    self.look_for_fdu(now)
                elif self.txCount < self.tx_limit:      # E10
                    if self.state in (1, 3):
                        self.initiate_retx()
                        self.state = 2
                        self.look_for_fdu(now)
                else:                                   # E12
                    if self.state in (1, 3):
                        self.state = 2
        else:                                           # E13
            self.alert(now)

    def on_timer(self, now, tok):
        if tok != self.timer_tok or self.state == 6:
            return
        if self.txCount < self.tx_limit:                # E104
            self.initiate_retx()
            self.look_for_fdu(now)
        else:                                           # E18: suspend
            self.suspends += 1
            self.state = 6
            # Operator recovery after op_delay: purge and re-synchronise (Set V(R)), every queued command lost
            self.at(now + self.op_delay, "recover")

    def recover(self, now):
        if self.state != 6:
            return
        self.recover_t = now
        self.dropped += len(self.sent) + len(self.wait)
        self.sent.clear()
        self.wait.clear()
        self.nnR = self.vS
        self.VR, self.R = self.vS, 0
        self.txCount, self.state = 1, 1

    # ---------------- FARM (idealised) ----------------
    def farm(self, now, seq, cmd):
        d = (seq - self.VR) & 0xFF
        if d == 0:
            self.VR = incr(self.VR)
            self.R = 0
            if cmd not in self.arrive_t:
                self.arrive_t[cmd] = now
            self.arrivals.append(now)
        elif d < 128:
            self.R = 1
        # else: negative window, duplicate, discarded

    def run_cop1(self):
        for i in range(int(self.dur * self.rate)):
            self.at(i / self.rate, "issue", i)
        for j in range(int(self.dur * self.tm_hz)):
            self.at(j / self.tm_hz, "tm")
        while self.q:
            now, _, kind, data = heapq.heappop(self.q)
            if kind == "issue":
                self.issue_t[data] = now
                self.wait.append(data)
                if self.state <= 2:
                    self.look_for_fdu(now)
            elif kind == "frame":
                self.farm(now, *data)
            elif kind == "tm":
                if self.rng_tm.random() >= self.p_tm:
                    self.at(now + self.d_dn, "clcw", (now, self.VR, self.R))
            elif kind == "clcw":
                gen, nR, R = data
                if gen > self.recover_t:     # like state 5: CLCWs from before the re-initialisation are ignored
                    self.on_clcw(now, nR, R)
            elif kind == "timer":
                self.on_timer(now, data)
            elif kind == "recover":
                self.recover(now)
        return self.metrics()

    def run_bd(self):
        for i in range(int(self.dur * self.rate)):
            t = i / self.rate
            self.issue_t[i] = t
            self.frames_sent += 1
            if self.rng_tc.random() >= self.p_tc:
                self.arrive_t[i] = t + self.d_up
                self.arrivals.append(t + self.d_up)
        return self.metrics()

    def metrics(self):
        lat = sorted(self.arrive_t[c] - self.issue_t[c] for c in self.arrive_t)
        n = len(self.issue_t)
        arr = sorted(self.arrivals)
        over = {1.5: 0.0, 3.0: 0.0}
        prev = 0.0
        for a in arr + [self.dur]:
            g = a - prev
            for th in over:
                if g > th:
                    over[th] += g - th
            prev = a
        pct = lambda f: lat[min(len(lat) - 1, int(f * len(lat)))] if lat else float("nan")
        return dict(delivered=len(lat) / n, p50=pct(0.5), p99=pct(0.99), p999=pct(0.999), max=lat[-1] if lat else 0,
                    late_1_5=sum(1 for x in lat if x > 1.5) / n,
                    lapse_1_5=over[1.5] / self.dur, lapse_3=over[3.0] / self.dur,
                    suspends_per_h=self.suspends * 3600 / self.dur, alerts_per_h=self.alerts * 3600 / self.dur,
                    dropped=self.dropped / n,
                    frames_per_cmd=self.frames_sent / n)


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0
    seeds = range(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    # tm_hz: TM frames a second carrying a CLCW (assumed; telemetry flushes at 20 Hz and frames add more)
    base = dict(tm_hz=20.0, d_up=0.02, d_dn=0.05, t1=3.0, K=10, tx_limit=3, op_delay=10.0)
    for rate, label in ((2.0, "pilot ~0.5 s"), (4.0, "pilot 0.25 s"), (1 / 0.15, "dashboard 150 ms")):
        for p in (0.01, 0.05, 0.10, 0.20):
            for mode in ("BD", "COP1"):
                rows = []
                for s in seeds:
                    sim = Sim(rate, p, p, dur=dur, seed=s, **base)
                    rows.append(sim.run_bd() if mode == "BD" else sim.run_cop1())
                agg = {k: statistics.mean(r[k] for r in rows) for k in rows[0]}
                agg["max"] = max(r["max"] for r in rows)
                print(f"{label:17s} p={p:.2f} {mode:4s} delivered {agg['delivered']:.5f}  lat p50 {agg['p50']*1000:5.0f} ms "
                      f"p99 {agg['p99']*1000:6.0f} p99.9 {agg['p999']*1000:6.0f} max {agg['max']:5.1f} s  "
                      f"cmds>1.5s late {agg['late_1_5']:.4f}  time w/o uplink >1.5s {agg['lapse_1_5']:.5f} >3s {agg['lapse_3']:.5f}  "
                      f"suspends/h {agg['suspends_per_h']:.2f} alerts/h {agg['alerts_per_h']:.2f} purged {agg['dropped']:.4f}  frames/cmd {agg['frames_per_cmd']:.3f}", flush=True)


if __name__ == "__main__":
    main()
