"""The autonomy block the Open MCT GROUND screens read, published from each decision row.

ground/pilot.py hands every decision row to OpsTelemetry.update (from intent_step, and from control_step with
no candidates). It reads only what the pilot already logs, writes only /DoomGround parameters, and nothing in
the loop reads them back: the displays sit downstream of the stack, never upstream of a decision.

The block goes up at most once a second, in one batchSet request with a short timeout, so it costs the
decision loop one round trip and never a hang. It is best effort: what Yamcs will not take is reported once
and left out, and the pilot flies on. Every value is published with an expiry, so a stalled pilot shows as
stale (grey) in Open MCT instead of freezing on its last good number.

tools/build_openmct_replay.py derives the same values from a recorded flight with decision_source and Rolling.
"""
import re
import sys
import time
from collections import deque

GROUND = "/DoomGround"
KIND = {"frontier": "FRONTIER", "door": "DOOR", "exit": "EXIT", "key": "KEY", "item": "ITEM",
        "switch": "SWITCH", "enemy": "ENEMY"}
UNKNOWN_NAME = re.compile(r'name: "' + GROUND + r'/([^"]+)"')   # how Yamcs lists ids its dictionary lacks


def decision_source(row):
    """Who settled this decision, under the labels of decision_source_t: summary.json's decision_reasons
    (research/frozen_metrics.py), checked in the same order. A rule's answer is never jev's. A jev outage,
    which targeting.decide logs as model "code (fallback)" with the rules' answers, is UNAVAILABLE, as is a
    decision with nothing to ask. Under --system-one code (scripts/play.sh --autopilot, model "code") every
    decision is RULE, where decision_reasons says asked and used."""
    sel = row.get("select") or {}
    model = row.get("model") or ""
    if row.get("unavailable") or model.startswith("code (fallback)"):
        return "UNAVAILABLE"
    if not row.get("answers") or sel.get("fallback") == "no answers":
        return "UNAVAILABLE"            # nothing to ask (no candidates), or nothing came back
    if model == "code":
        return "RULE"
    if sel.get("fallback"):
        return "UNSURE_BAND"
    if sel.get("held"):
        return "HELD"
    if sel.get("gave_up"):
        return "RULE"
    if row.get("cached"):
        return "CACHED"
    return "JEV"


def jev_caused(row):
    """Charter 7's test for an intent change the model caused (frozen_metrics.jev_share), less the rules'
    answers: jev answered, fresh or cached, and code did not overrule it with the unsure band or a hold.
    summary.json's jev_share also credits a fallback's rule answers to the model, so this can read lower."""
    sel = row.get("select") or {}
    return (bool(row.get("answers")) and not sel.get("fallback") and not sel.get("held")
            and not row.get("unavailable") and not (row.get("model") or "").startswith("code"))


class Rolling:
    """JevShare over the last `window` intent changes, a new mode or a new pick as frozen_metrics.intent_changes
    counts them (the first row is not one), and FallbackRate over the last `window` decisions."""
    def __init__(self, window=40):
        self.changes = deque(maxlen=window)     # per intent change: did jev cause it
        self.sources = deque(maxlen=window)     # per decision
        self.prev = None

    def add(self, row):
        src = decision_source(row)
        self.sources.append(src)
        now = (row.get("mode"), row.get("pick"))
        if self.prev is not None and now != self.prev:
            self.changes.append(jev_caused(row))
        self.prev = now
        return src

    def jev_share(self):
        """None until the first intent change: no share yet, rather than a zero that reads as a warning."""
        return sum(self.changes) / len(self.changes) if self.changes else None

    def fallback_rate(self):
        return sum(s != "JEV" for s in self.sources) / len(self.sources) if self.sources else None


def json_value(v):
    """A Python value as a Yamcs JSON Value, typed the way yamcs-client types it."""
    if isinstance(v, bool):
        return {"type": "BOOLEAN", "booleanValue": v}
    if isinstance(v, int):
        return ({"type": "SINT32", "sint32Value": v} if -2**31 <= v < 2**31
                else {"type": "SINT64", "sint64Value": str(v)})
    if isinstance(v, float):
        return {"type": "DOUBLE", "doubleValue": v}
    return {"type": "STRING", "stringValue": str(v)}


def batch_set(processor, timeout_s):
    """POST /api/processors/{instance}/{processor}/parameters:batchSet on the pilot's publishing client, with a
    timeout yamcs-client does not offer. Returns (HTTP status, Yamcs's message); raises when Yamcs did not
    answer. _instance and _processor are yamcs-client 2.1.0's (pinned in ground/requirements.txt)."""
    ctx = processor.ctx
    url = f"{ctx.api_root}/processors/{processor._instance}/{processor._processor}/parameters:batchSet"

    def post(body):
        if ctx.credentials:
            ctx.credentials.before_request(ctx.session, ctx.auth_root)
        r = ctx.session.post(url, json=body, timeout=timeout_s)
        if r.status_code < 300:
            return r.status_code, ""
        try:
            return r.status_code, str(r.json().get("msg") or r.text)
        except ValueError:
            return r.status_code, r.text[:300]
    return post


class OpsTelemetry:
    def __init__(self, processor, window=40, every_s=1.0, expires_s=3.0, timeout_s=0.5, retry_s=10.0,
                 post=None):
        self.post = post or batch_set(processor, timeout_s)    # tests pass a stub: no network
        self.rolling = Rolling(window)
        self.every_s, self.expires_s, self.retry_s = every_s, expires_s, retry_s
        self.last_pub = 0.0
        self.dropped = set()       # names left out until Yamcs next fails to answer: not in this Yamcs's ground
                                   # database, or a number it cannot take
        self.refused = set()       # (name, label) pairs it cannot convert, e.g. an IntentMode its enum lacks
        self.retry_at = 0.0        # after Yamcs did not answer, nothing is sent before this
        self.quiet = {}            # what was reported, and until when it stays quiet

    def update(self, row, cands):
        """Called with every decision row; publishes at most once per every_s. Never raises: a display feed
        must not take the loop down."""
        try:
            self._update(row, cands)
        except Exception as e:          # noqa: BLE001
            self.warn("feed", f"autonomy block skipped: {type(e).__name__}: {e}")

    def _update(self, row, cands):
        src = self.rolling.add(row)
        if time.time() - self.last_pub < self.every_s:
            return
        self.last_pub = time.time()
        sel, ans = row.get("select") or {}, row.get("answers") or {}
        pick = row.get("pick") if isinstance(row.get("pick"), int) else None   # the sector pilot picks a word
        values = {
            # Approximation until the payload reports the tic an intent first took effect (charter 3.4):
            # telemetry age at decision + jev round trip + command issue time.
            "DecisionAgeMs": float((row.get("tel_age_ms") or 0) + (row.get("latency_ms") or 0) + (row.get("cmd_ms") or 0)),
            "IntentMode": row.get("mode"),
            "DecisionSource": src,
            "PickSlot": pick if pick is not None else 255,
            "PickKind": KIND.get(cands[pick]["kind"], "NONE") if pick is not None and pick < len(cands) else "NONE",
            "PickGap": float(sel.get("gap") or 0),
            "PickConfidence": float(sel.get("confidence") or 0),
            "JevShare": self.rolling.jev_share(),
            "FallbackRate": self.rolling.fallback_rate(),
            "GraphVersion": int(row.get("graph_version") or 0),
            "Attempt": int(row.get("episode") or 0),
        }
        for n in range(8):
            v = ans.get(f"g_t{n}")
            values[f"Score{n}"] = float(v) if v not in (None, "") else 0.0
        if "engage" in ans:
            values["EngageAnswer"] = str(ans["engage"])
        self.publish(values)

    def publish(self, values):
        """One batchSet for the whole block. Yamcs takes a batch whole or not at all, so a refused one is
        narrowed down: names its ground database lacks are left out, and a label it cannot convert is left out
        while it lasts (a number it cannot take, like a name). Both are asked again once Yamcs has failed to
        answer, since the one that answers next may be a restarted one with the current ground database."""
        items = {k: v for k, v in values.items()
                 if v is not None and k not in self.dropped and (k, v) not in self.refused}
        status, msg = self.send(items)
        if status is None or status < 400:
            return
        missing = set(UNKNOWN_NAME.findall(msg)) & set(items)
        if missing:
            self.dropped |= missing
            names = ", ".join(sorted(missing))
            self.warn(f"missing {names}", f"not in this Yamcs's ground database, left out (restart Yamcs to load "
                      f"it): {names}", every_s=None)
            items = {k: v for k, v in items.items() if k not in missing}
            status, msg = self.send(items)
            if status is None or status < 400:
                return
        refused = {}
        for k, v in items.items():     # a value it cannot convert: find which, once
            status, msg = self.send({k: v})
            if status is not None and status >= 400:
                if isinstance(v, str):
                    self.refused.add((k, v))
                else:
                    self.dropped.add(k)
                refused[k] = f"{k}={v!r}: {msg}"
        if refused:
            self.warn("refused " + " ".join(sorted(refused)), "refused, left out: " + "; ".join(refused.values()))

    def send(self, items):
        """POST items as one batch. Returns (status, message), or (None, reason) when Yamcs did not answer or
        failed; then nothing more is sent for retry_s."""
        if not items:
            return 200, ""
        if time.time() < self.retry_at:
            return None, "backing off"
        body = {"request": [{"id": {"name": f"{GROUND}/{k}"}, "value": json_value(v),
                             "expiresIn": int(self.expires_s * 1000)} for k, v in items.items()]}
        try:
            status, msg = self.post(body)
        except Exception as e:          # noqa: BLE001  no answer in time: Yamcs down, restarting or stalled
            status, msg = None, f"{type(e).__name__}: {e}"
        if status is None or status >= 500:
            self.retry_at = time.time() + self.retry_s
            if status is None:          # down or restarting: what this Yamcs lacked, the next one may have
                self.dropped.clear()
                self.refused.clear()
            why = f"HTTP {status}: {msg}" if status else msg
            self.warn("link", f"autonomy block not published, next try in {self.retry_s:.0f} s: {why}")
            return None, msg
        return status, msg

    def warn(self, key, msg, every_s=60.0):
        """Print a problem once, and again only after every_s (None: once for the run)."""
        now = time.time()
        if now < self.quiet.get(key, 0.0):
            return
        self.quiet[key] = float("inf") if every_s is None else now + every_s
        print(f"[ops] {msg}", file=sys.stderr, flush=True)
