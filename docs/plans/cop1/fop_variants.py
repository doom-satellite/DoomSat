import statistics, fop_model as m
def run(label, rate, p, **kw):
    base = dict(tm_hz=20.0, d_up=0.02, d_dn=0.05, t1=3.0, K=10, tx_limit=3, op_delay=10.0)
    base.update(kw)
    rows = [m.Sim(rate, p, p, dur=3600, seed=s, **base).run_cop1() for s in range(5)]
    a = {k: statistics.mean(r[k] for r in rows) for k in rows[0]}
    print(f"{label:38s} p={p:.2f} delivered {a['delivered']:.4f} p99 {a['p99']*1000:5.0f} ms  w/o uplink>1.5s {a['lapse_1_5']:.4f} >3s {a['lapse_3']:.4f} suspends/h {a['suspends_per_h']:.1f}")
for p in (0.05, 0.10):
    run("4 Hz, t1 3 s, K 10 (Yamcs defaults)", 4.0, p)
    run("4 Hz, t1 1 s, K 10", 4.0, p, t1=1.0)
    run("4 Hz, t1 1 s, K 10, txLimit 10", 4.0, p, t1=1.0, tx_limit=10)
    run("4 Hz, t1 1 s, K 3", 4.0, p, t1=1.0, K=3)
    run("4 Hz, t1 3 s, K 10, CLCW 5 Hz", 4.0, p, tm_hz=5.0)
