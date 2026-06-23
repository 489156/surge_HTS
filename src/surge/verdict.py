"""The truth layer — the single most important module.

A critical-engineer's reframe: this project generates far more PREDICTION
surface (surge, duel, rotation, watch) than it has VALIDATION depth. Every other
module proposes; this one judges. For each strategy with a forward record it asks
one ruthless question:

    Does it beat its NAIVE BASELINE, on out-of-sample forward data, by a margin
    that is not explainable by luck?

and returns a blunt verdict: 🟢 EDGE / 🟡 NO-EDGE / 🔴 INSUFFICIENT / ⛔ NEGATIVE.
The headline answers the only question that matters for a user about to risk
money: "Has anything here earned the right to be believed yet?" — and today the
honest answer is no.

Baselines (the part most retail tools omit, which is why backtests lie):
- duel      → "always buy the bull leg" (semis drift up); a coin is not the bar.
- rotation  → the +10%/T+5 hit rate of names that did NOT pass the gate (does the
              filter add anything over the candidate pool?).
- surge     → a 0.5 coin on the candidates' next-day WIN RATE. ("+100% rate vs
              the market base rate" is a trap: it only proves a micro-cap screen
              picks micro-caps — hugely significant, yet the typical pick still
              loses. The fat-tail mean is inflated by a few lottery wins; the
              honest question is whether *most* picks even rise.)
"""

from __future__ import annotations

import math

from .config import settings
from .db import connect

MIN_N = settings.variant_min_n   # forward picks needed before any verdict (30)
Z = settings.variant_promote_z   # one-sided significance bar (1.64 ≈ 95%)


def _binom_z(wins: int, n: int, p0: float | None) -> float:
    if not n or p0 is None:
        return 0.0
    se = math.sqrt(p0 * (1 - p0) / n)
    return ((wins / n) - p0) / se if se else 0.0


def evalue_bernoulli(outcomes: list[int], p0: float | None) -> float:
    """Anytime-valid e-value for H0: p ≤ p0  vs  H1: p > p0 on an ORDERED win/loss
    sequence — a plug-in (running-MLE, Krichevsky–Trofimov) test martingale.

    Why anytime-valid is the honest path: a fixed-n z-test is valid only at one
    pre-chosen n, so monitoring it every day and graduating the first day it 'passes'
    silently inflates the false-positive rate (optional-stopping bias — the classic
    way backtests lie). An e-value has no such defect: by Ville's inequality
    P(sup_n e_n ≥ 1/α) ≤ α, so rejecting when e ≥ 1/α controls Type-I error at α under
    ANY stopping rule, including 'peek daily, stop when decisive'. A genuine edge can
    therefore be confirmed the moment evidence is strong; a noise edge never crosses.

    Why plug-in (not a uniform mixture): each step bets with the running rate estimated
    from PAST outcomes only — `(wins_so_far+½)/(k+1)` — so it adapts to the true rate
    and achieves the optimal (GROW) growth rate KL(p‖p0) per step, the most POWERFUL
    honest choice. (A uniform Beta mixture is valid too but badly under-powered for
    moderate edges — e≈1.2 at 20/30.) Closed-form, no SciPy.

    No-falsehood notes: (i) anytime-validity costs ~2× the sample of a *peeked* test —
    that is the price of not fooling yourself, not a defect; (ii) p0 is the empirical
    baseline (itself estimated) — a fully paired/McNemar version would remove that
    slack; treated as known here, a small documented approximation."""
    if p0 is None or not (0.0 < p0 < 1.0) or not outcomes:
        return 0.0
    eps = 1e-6
    w = 0.0
    log_e = 0.0
    for k, x in enumerate(outcomes):
        # KT running estimate from PAST outcomes only, CLAMPED to [p0, 1-eps]: this
        # makes the test strictly ONE-SIDED (H1: p > p0). Never bet below p0, so a
        # strategy whose rate sits at/under the baseline accrues NO evidence (e→1) —
        # without the clamp a below-baseline series would falsely accumulate from the
        # down-side and fake a signal. (Valid e-process: E[factor|H0]≤1 for all p≤p0.)
        phat = min(max((w + 0.5) / (k + 1), p0), 1.0 - eps)
        if x:
            log_e += math.log(phat) - math.log(p0)
        else:
            log_e += math.log(1.0 - phat) - math.log(1.0 - p0)
        w += x
    return math.exp(log_e)


def evalue_returns(returns: list[float], lo: float = -1.0, hi: float = 1.0,
                   lam_max: float = 0.5) -> float:
    """Anytime-valid e-value for H0: mean ≤ 0  vs  H1: mean > 0 on an ORDERED sequence
    of per-trade NET returns — a hedged-capital (testing-by-betting) martingale
    (Waudby-Smith & Ramdas). COMPLEMENTS evalue_bernoulli: the binary test throws away
    return MAGNITUDE; this one uses it, so on a variable/fat-tailed payoff it can reach
    decisive evidence on fewer trades.

    Construction: capital K_t = Π (1 + λ_i·x_i), x_i the return winsorized to [lo, hi]
    and λ_i a PREDICTABLE bet (running mean/variance from PAST only, the GROW/Kelly
    fraction) clamped to [0, lam_max]. λ ≥ 0 makes it strictly ONE-SIDED — a strategy
    whose mean sits at/below 0 bets ~0 and accrues NO evidence (K stays ≈1). With
    lam_max·|lo| < 1 capital stays positive. Under H0, E[1+λ_i x_i | past]=1+λ_i·E[x_i]
    ≤ 1, so K is a non-negative supermartingale ⇒ by Ville P(sup_t K_t ≥ 1/α) ≤ α:
    valid to monitor every day and graduate when K ≥ 1/α (peek-safe).

    Honesty: clipping must only ever LOWER the mean to stay conservatively valid, so we
    winsorize the UPPER tail ONLY (x = min(r, hi)) — exactly right for fat-tailed surge,
    where a couple of +500% lottery wins must not fake a mean-edge. We do NOT lower-clip:
    raising a sub-floor value up to lo would INCREASE the mean and break one-sided
    validity, so a return below `lo` (a data error — a long cannot lose >100%, so real
    returns are ≥ -1) is SKIPPED, not clamped up. With lam_max·|lo| < 1 capital stays
    positive. This is a CORROBORATING DIAGNOSTIC, not part of the ⭐ gate (see _finish);
    power vs the binary test depends on the payoff spread and neither dominates."""
    if not returns:
        return 0.0
    k = 1.0
    s = ss = 0.0
    m = 0
    for r in returns:
        if r is None or r != r or r < lo:       # skip None / NaN / sub-floor data errors
            continue                            # (never clamp UP — that would inflate the mean)
        x = min(r, hi)                          # winsorize the UPPER tail only (conservative)
        if m >= 2:
            mu = s / m
            var = max(ss / m - mu * mu, 1e-6)
            lam = min(max(mu / var, 0.0), lam_max)   # predictable, one-sided, positivity
        else:
            lam = 0.0                           # no bet until a little history exists
        k *= 1.0 + lam * x
        if k <= 1e-12:                          # numerical floor (shouldn't trigger)
            k = 1e-12
        s += x
        ss += x * x
        m += 1
    return k


def paired_evalue(hits: list[int], base_hits: list[int]) -> float:
    """Anytime-valid PAIRED e-value (McNemar-style) — does the strategy beat its
    baseline on the SAME sessions? Removes the estimated-baseline slack noted in
    evalue_bernoulli (there p0 is an estimate; here we compare head-to-head). Among
    DISCORDANT sessions (strategy and baseline disagree) the win goes to whoever was
    right; under H0 of no difference that is a fair coin, so we test P>0.5 with the
    one-sided binary e-value on the discordant outcomes — itself peek-safe. A
    CORROBORATING DIAGNOSTIC, not part of the ⭐ gate (see _finish)."""
    if len(hits) != len(base_hits):              # aligned per-session lists required;
        raise ValueError(                        # silent zip-truncation would drop pairs
            f"paired_evalue length mismatch: {len(hits)} vs {len(base_hits)}")
    disc = [h for h, b in zip(hits, base_hits) if h != b]
    return evalue_bernoulli(disc, 0.5) if disc else 0.0


def _verdict(n: int, excess: float | None, z: float) -> tuple[str, str]:
    if n < MIN_N:
        return "🔴", f"데이터 부족 (n={n} < {MIN_N})"
    if excess is None:
        return "🔴", "기준선 산출 불가"
    if excess <= 0:
        return ("⛔", "기준선보다 유의하게 나쁨") if z <= -Z else \
               ("🟡", "엣지 없음 (기준선 이하)")
    return ("🟢", f"엣지 (z={z:.1f})") if z >= Z else \
           ("🟡", f"미검증 (z={z:.1f}, 유의성 부족)")


def _iso_week(d) -> object:
    """(ISO year, week) for a date string — the spread unit. Falls back to the
    YYYY-MM prefix for unparseable seeds so it never raises."""
    try:
        import datetime
        return datetime.date.fromisoformat(str(d)[:10]).isocalendar()[:2]
    except Exception:  # noqa: BLE001
        return str(d)[:7]


def _signal_grade(outcomes: list[int], dates: list, base_p: float | None,
                  net: float | None, gate_e: float | None = None,
                  comp_label: str = "") -> tuple[bool, list[dict]]:
    """Conservative graduation 🟢 → ⭐ SIGNAL. A statistically-distinguishable edge
    is still only a *hypothesis*; to be acted on as a signal it must clear ALL five.

    The bar was re-cut to reach a verdict faster WITHOUT lowering quality. Two moves:
    (a) raw sample size (was n≥100, 3 calendar months) is traded for STRUCTURAL
    robustness (split-half + week-spread) — a stronger overfit guard than a calendar
    floor. (b) the statistical-evidence test is now an ANYTIME-VALID e-value instead
    of a daily-peeked CI: the old peeked CI over-fired under continuous monitoring
    (optional-stopping bias), so swapping it both removes a real flaw AND lets a true
    edge graduate sooner. The n-floor is therefore safely the sanity floor (30), with
    the e-value — not raw n — governing sufficiency.
      1. SAMPLE      n ≥ signal_min_n (sanity floor).
      2. SPREAD      ≥ signal_min_weeks distinct ISO weeks (not one lucky burst).
      3. EVIDENCE    anytime-valid e-value ≥ 1/α (peek-safe; see evalue_bernoulli).
      4. STABILITY   the edge holds in BOTH the first and second half of the
                     chronological sample (a decayed/fluke edge fails one half).
      5. ECONOMIC    net-of-cost outcome > 0 (hit-rate ≠ money).
    Returns (is_signal, checks) with a per-condition checklist for display."""
    n = len(outcomes)
    order = sorted(range(n), key=lambda i: str(dates[i])) if dates else list(range(n))
    c1 = n >= settings.signal_min_n
    weeks = len({_iso_week(d) for d in dates if d})
    c2 = weeks >= settings.signal_min_weeks
    thr = settings.signal_evalue_threshold
    # EVIDENCE = the single pre-registered BINARY e-value (peek-safe). NOT an average of
    # tracks: under family-wise error control, adding tracks to the gate can only TRADE
    # AWAY this track's power (a strong binary edge must not be diluted by a weak
    # continuous one). evalue_returns / paired_evalue ride along as DIAGNOSTICS (comp_label).
    if gate_e is None:                           # standalone call → compute the binary gate
        gate_e = (evalue_bernoulli([outcomes[i] for i in order], base_p)
                  if base_p is not None else 0.0)
    c3 = gate_e >= thr
    c4 = False
    h1 = h2 = None
    if base_p is not None and n >= 4:
        h = n // 2
        first = [outcomes[i] for i in order[:h]]
        second = [outcomes[i] for i in order[h:]]
        h1, h2 = sum(first) / len(first), sum(second) / len(second)
        c4 = h1 > base_p and h2 > base_p
    c5 = net is not None and net > 0
    checks = [
        {"k": f"표본 n≥{settings.signal_min_n}", "ok": c1, "v": f"n={n}"},
        {"k": f"기간분산 {settings.signal_min_weeks}주+", "ok": c2, "v": f"{weeks}주"},
        {"k": f"증거 e≥{thr:g} (peek-safe)", "ok": c3,
         "v": f"e={gate_e:.1f}{comp_label}"},
        {"k": "전·후반 모두 유지", "ok": c4,
         "v": (f"{h1*100:.0f}/{h2*100:.0f}%" if h1 is not None else "")},
        {"k": "비용차감 순익>0", "ok": c5,
         "v": (f"{net*100:+.1f}%" if net is not None else "측정불가")},
    ]
    return all([c1, c2, c3, c4, c5]), checks


def _epct(e: float, thr: float) -> float:
    """Progress toward a confirmed signal on a log scale: e=1 → 0%, e=thr → 100%."""
    return max(0.0, min(1.0, math.log(e) / math.log(thr))) if (e > 0 and thr > 1) else 0.0


def _finish(d: dict, outcomes: list[int], dates: list, base_p: float | None,
            net: float | None, returns: list[float] | None = None,
            base_hits: list[int] | None = None) -> dict:
    """Attach the ⭐ graduation grade. ⭐ requires the strategy to already be a 🟢 edge
    AND clear every signal condition; otherwise it stays a hypothesis.

    The EVIDENCE condition is the single pre-registered anytime-valid BINARY e-value
    (evalue_bernoulli). The continuous-magnitude (evalue_returns) and paired/McNemar
    (paired_evalue) e-values are computed and surfaced as CORROBORATING DIAGNOSTICS but
    do NOT enter the gate: under family-wise error control there is no free lunch —
    folding extra tracks into the gate can only TRADE AWAY the binary track's power (an
    earlier 'average the tracks' attempt diluted a strong binary edge below threshold and
    made ⭐ harder, the opposite of the goal). They inform; the binary test graduates.

    Chronological-order invariant: `returns` MUST already be in time order — evalue_
    returns is path-dependent. Every caller obtains it from a query with ORDER BY
    decision_date / snapshot_date, so the sequence is chronological; do not pass an
    unordered list here."""
    is_sig, checks = False, []
    n = d["n"]
    thr = settings.signal_evalue_threshold
    order = sorted(range(n), key=lambda i: str(dates[i])) if dates else list(range(n))
    ev_bin = (evalue_bernoulli([outcomes[i] for i in order], base_p)
              if (base_p is not None and n) else 0.0)
    ev_ret = evalue_returns(returns) if returns else 0.0      # diagnostic (NOT gating)
    ev_pair = (paired_evalue([outcomes[i] for i in order],
                             [base_hits[i] for i in order]) if base_hits else 0.0)  # diagnostic
    d["evalue"] = round(ev_bin, 2)               # the GATE e-value = binary, undiluted
    d["evalue_ret"] = round(ev_ret, 2)           # corroboration only
    d["paired_e"] = round(ev_pair, 2)            # corroboration only
    d["evidence_pct"] = _epct(ev_bin, thr)
    d["evidence_ret_pct"] = _epct(ev_ret, thr)
    comp = (f" · 보조진단 연속{ev_ret:.0f}"
            + (f"·페어{ev_pair:.0f}" if base_hits else ""))
    if n >= MIN_N:
        passed, checks = _signal_grade(outcomes, dates, base_p, net, ev_bin, comp)
        is_sig = passed and d["mark"] == "🟢"
    d["signal"] = is_sig
    d["signal_checks"] = checks
    if is_sig:
        d["mark"] = "⭐"
        d["verdict"] = "신호 — 5조건 모두 충족"
    return d


def _duel() -> dict:
    with connect() as c:
        rows = c.execute(
            "SELECT correct, soxx_oc_ret, decision_date, pnl_pct FROM duel_decisions "
            "WHERE evaluated_at IS NOT NULL AND side != 'STAND_ASIDE' "
            "AND correct IS NOT NULL ORDER BY decision_date").fetchall()
    n = len(rows)
    wins = sum(r["correct"] for r in rows)
    up = sum(1 for r in rows if (r["soxx_oc_ret"] or 0) > 0)
    base = max(up, n - up) / n if n else None   # always-bull OR always-bear, whichever
    acc = wins / n if n else None
    excess = (acc - base) if (acc is not None and base is not None) else None
    z = _binom_z(wins, n, base)
    mark, msg = _verdict(n, excess, z)
    pnls = [r["pnl_pct"] for r in rows if r["pnl_pct"] is not None]
    d = {"strategy": "duel (US 레버리지 페어)", "metric": "방향 적중률",
         "value": acc, "baseline": base, "excess": excess, "z": z,
         "n": n, "mark": mark, "verdict": msg}
    return _finish(d, [r["correct"] for r in rows],
                   [r["decision_date"] for r in rows], base,
                   (sum(pnls) / len(pnls)) if pnls else None,  # net of slippage
                   returns=pnls,                               # continuous-magnitude track
                   base_hits=[1 if (r["soxx_oc_ret"] or 0) > 0 else 0
                              for r in rows])                  # paired vs always-bull


def _rotation() -> dict:
    with connect() as c:
        rows = c.execute(
            "SELECT passed_filter, hit_t5, ret_t5, decision_date "
            "FROM rotation_decisions WHERE evaluated_at IS NOT NULL "
            "AND hit_t5 IS NOT NULL ORDER BY decision_date").fetchall()
    passed = [r for r in rows if r["passed_filter"] == 1]
    other = [r["hit_t5"] for r in rows if r["passed_filter"] == 0]
    n = len(passed)
    wins = sum(r["hit_t5"] for r in passed)
    base = (sum(other) / len(other)) if other else None   # does the gate add value
    acc = wins / n if n else None
    excess = (acc - base) if (acc is not None and base is not None) else None
    z = _binom_z(wins, n, base)
    mark, msg = _verdict(n, excess, z)
    rets = [r["ret_t5"] for r in passed if r["ret_t5"] is not None]
    d = {"strategy": "rotation (KR 회전)", "metric": "+10%/T+5 도달률",
         "value": acc, "baseline": base, "excess": excess, "z": z,
         "n": n, "mark": mark, "verdict": msg}
    return _finish(d, [r["hit_t5"] for r in passed],
                   [r["decision_date"] for r in passed], base,
                   (sum(rets) / len(rets)) if rets else None,
                   returns=rets)             # continuous-magnitude track (no clean pairing)


def _surge() -> dict:
    # HONEST metric — robust to the fat tail. "+100% rate vs market base rate"
    # is a false-edge trap (it only proves the screen picks micro-caps), and the
    # arithmetic mean return is inflated by a few +500% lottery wins. The honest
    # "typical outcome" test is the WIN RATE: do more than half the picks even
    # rise next day, vs a 0.5 coin?
    with connect() as c:
        rows = c.execute(
            "SELECT next_pct, snapshot_date FROM candidate_outcomes "
            "WHERE next_pct IS NOT NULL ORDER BY snapshot_date").fetchall()
    vals = [r["next_pct"] for r in rows]
    n = len(vals)
    wins = sum(1 for v in vals if v > 0)
    acc = (wins / n) if n else None
    excess = (acc - 0.5) if acc is not None else None
    z = _binom_z(wins, n, 0.5)
    mark, msg = _verdict(n, excess, z)
    # economic check uses the MEDIAN (not the fat-tail mean) — the typical pick
    median = sorted(vals)[n // 2] / 100 if n else None   # next_pct stored as %
    d = {"strategy": "surge (US +100% 점화)", "metric": "익일 상승 비율",
         "value": acc, "baseline": 0.5, "excess": excess, "z": z,
         "n": n, "mark": mark, "verdict": msg}
    return _finish(d, [1 if v > 0 else 0 for v in vals],
                   [r["snapshot_date"] for r in rows], 0.5, median,
                   returns=[v / 100.0 for v in vals])   # next_pct(%) → fraction; winsorized


def assess() -> list[dict]:
    return [_duel(), _rotation(), _surge()]


def headline() -> str:
    rows = assess()
    sig = [r for r in rows if r.get("signal")]
    if sig:
        return f"⭐ 신호 {len(sig)}개 (5조건 충족) — " + ", ".join(
            r["strategy"] for r in sig)
    proven = [r for r in rows if r["mark"] == "🟢"]
    if proven:
        return (f"🟢 검증된 엣지 {len(proven)}개 — 단, ⭐ 신호 조건은 미충족(아직 가설): "
                + ", ".join(r["strategy"] for r in proven))
    enough = [r for r in rows if r["n"] >= MIN_N]
    if not enough:
        return "검증된 엣지: 없음 — 전진 표본 부족 (어떤 전략도 베팅 근거 미충족)"
    return "검증된 엣지: 없음 — 표본은 쌓였으나 어떤 전략도 기준선을 유의하게 못 넘김"
