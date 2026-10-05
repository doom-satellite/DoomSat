# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""A lossy link: forward UDP datagrams between the F' comm bridge and Yamcs, dropping a share of them.

    python tools/lossy_relay.py --loss 5 [--seed 1] [--tm 50000:51000] [--tc 51001:50001]

Each --tm/--tc is LISTEN:FORWARD on 127.0.0.1. Every datagram (one TM or TC transfer frame) is dropped
independently with probability --loss percent, separately per direction, from a seeded generator. Counts go to
stdout every --report seconds and on exit (Ctrl-C or SIGTERM).

Start Yamcs behind it with DOOMSAT_RELAY=1 (ground/yamcs/launch.py moves Yamcs's TM link to 51000 and its TC link
to 51001; the bridge keeps 50000 and 50001):

    DOOMSAT_RELAY=1 scripts/flight.sh start
    python tools/lossy_relay.py --loss 5

With --loss 0 it is a plain pass-through, which is how to check the splice itself costs nothing.
"""
import argparse
import random
import selectors
import signal
import socket
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loss", type=float, default=5.0, help="percent of datagrams dropped in each direction")
    p.add_argument("--tm-loss", type=float, help="percent for TM only (default --loss)")
    p.add_argument("--tc-loss", type=float, help="percent for TC only (default --loss)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--tm", default="50000:51000", help="downlink LISTEN:FORWARD (bridge -> Yamcs)")
    p.add_argument("--tc", default="51001:50001", help="uplink LISTEN:FORWARD (Yamcs -> bridge)")
    p.add_argument("--report", type=float, default=10.0, help="seconds between count lines (0: only at exit)")
    a = p.parse_args()

    sel = selectors.DefaultSelector()
    stats = {}
    for name, spec, loss in (("TM", a.tm, a.tm_loss), ("TC", a.tc, a.tc_loss)):
        listen, forward = (int(x) for x in spec.split(":"))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        s.bind(("127.0.0.1", listen))
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stats[name] = dict(sent=0, dropped=0, loss=(a.loss if loss is None else loss) / 100.0,
                           rng=random.Random(f"{a.seed}-{name}"), out=out, to=("127.0.0.1", forward))
        sel.register(s, selectors.EVENT_READ, name)
        print(f"[relay] {name} 127.0.0.1:{listen} -> :{forward}, dropping {stats[name]['loss'] * 100:g}%", flush=True)

    def report():
        print("[relay] " + "  ".join(f"{n} sent {st['sent']} dropped {st['dropped']}" for n, st in stats.items()),
              flush=True)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)   # `kill` or pkill: still print the final counts

    last = time.time()
    try:
        while True:
            for key, _ in sel.select(timeout=1.0):
                data = key.fileobj.recv(65536)
                st = stats[key.data]
                if st["rng"].random() < st["loss"]:
                    st["dropped"] += 1
                else:
                    st["out"].sendto(data, st["to"])
                    st["sent"] += 1
            if a.report and time.time() - last >= a.report:
                last = time.time()
                report()
    except KeyboardInterrupt:
        pass
    finally:
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
