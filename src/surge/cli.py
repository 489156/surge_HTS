"""Command-line entrypoint:  surge <command>

    surge init        create the database
    surge universe    refresh the US securities master (NASDAQ Trader)
    surge snapshot    run the daily feature-snapshot ("박제") job
    surge watchlist   show today's ranked surge candidates (the product output)
    surge reversals   fade watchlist — popped names likely to reverse down next day
    surge fade        label archived surges as held/faded the next day
    surge backfill-outcomes  record realized next-day move for past candidates
    surge eval        predictability metrics (Precision@K, lift vs base rate)
    surge surges      list recently archived surge events
    surge stats       show row counts per table

    surge backtest    leak-free strategy backtest (+ Monte Carlo / walk-forward / crash)
    surge duel        tonight's SOXL-vs-SOXS direction call (persisted + scored later)
    surge duel-backtest / duel-eval   replay the rule over history / score past calls

  Trading platform (paper default; live is human-gated):
    surge trade       run one decision cycle (agents→debate→risk→execution)
    surge portfolio   positions, equity, risk state
    surge approvals   live-order approval queue (--approve / --reject)
    surge killswitch  trigger/reset the safety override
    surge dashboard   launch the HTS web dashboard (FastAPI)
"""

from __future__ import annotations

import argparse
import json
import sys

from loguru import logger

from . import eval as evaluation
from . import pipeline, reversion
from .db import connect, init_db


def _cmd_init(_args) -> int:
    init_db()
    return 0


def _cmd_universe(_args) -> int:
    pipeline.update_universe()
    return 0


def _cmd_snapshot(args) -> int:
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    summary = pipeline.run_snapshot(
        symbols=syms, period=args.period, limit=args.limit, fast=args.fast,
        asof=args.asof,
    )
    print(summary)
    return 0 if "error" not in summary else 1


def _cmd_fade(_args) -> int:
    n = pipeline.update_sustained()
    with connect() as conn:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN sustained=1 THEN 1 ELSE 0 END) held, "
            "SUM(CASE WHEN sustained=0 THEN 1 ELSE 0 END) faded, "
            "SUM(CASE WHEN sustained IS NULL THEN 1 ELSE 0 END) pending "
            "FROM surge_events"
        ).fetchone()
    print(f"fade-labeled this run: {n}")
    print(f"held={row['held'] or 0}  faded={row['faded'] or 0}  pending={row['pending'] or 0}")
    return 0


def _cmd_surges(args) -> int:
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, event_date, surge_pct, intraday_high_pct "
            "FROM surge_events ORDER BY event_date DESC, surge_pct DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    for r in rows:
        print(f"{r['event_date']}  {r['symbol']:<8} "
              f"+{r['surge_pct']:.0f}%  (intraday +{r['intraday_high_pct'] or 0:.0f}%)")
    if not rows:
        print("(no surge events archived yet)")
    return 0


def _cmd_watchlist(args) -> int:
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(snapshot_date) d FROM candidates"
        ).fetchone()["d"]
        if not latest:
            print("(no candidates yet — run `surge snapshot` first)")
            return 0
        rows = conn.execute(
            "SELECT * FROM candidates WHERE snapshot_date = ? "
            "ORDER BY score DESC LIMIT ?",
            (latest, args.limit),
        ).fetchall()
    print(f"\n  Surge watchlist — {latest}  (Top {len(rows)} by setup score)\n")
    for r in rows:
        fl = f"{r['shares_float']/1e6:.1f}M" if r["shares_float"] else "—"
        print(
            f"  {r['score']:>5.1f}  {r['symbol']:<7} "
            f"${r['close']:<7.2f} float={fl:<8} "
            f"chg={r['pct_change'] or 0:+.0f}%"
        )
        reasons = json.loads(r["reasons"]) if r["reasons"] else []
        if reasons and args.why:
            print(f"         └ {' · '.join(reasons)}")
    print()
    return 0


def _cmd_reversals(args) -> int:
    """Fade watchlist: already-popped names likely to reverse down next day."""
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(snapshot_date) d FROM daily_snapshot"
        ).fetchone()["d"]
        if not latest:
            print("(no snapshots yet — run `surge snapshot` first)")
            return 0
        rows = conn.execute(
            "SELECT ds.*, tf.exhausted, tf.pending_offering, tf.illiquid "
            "FROM daily_snapshot ds "
            "LEFT JOIN trap_flags tf "
            "  ON ds.symbol = tf.symbol AND ds.snapshot_date = tf.snapshot_date "
            "WHERE ds.snapshot_date = ? AND ds.pct_change >= ?",
            (latest, reversion.settings.near_surge_pct),
        ).fetchall()
    snaps = [dict(r) for r in rows]
    traps = {s["symbol"]: s for s in snaps}
    ranked = reversion.rank_reversions(snaps, traps, min_score=args.min_score)[: args.limit]
    print(f"\n  Reversion (fade) watchlist — {latest}  "
          f"(Top {len(ranked)} likely to fade next day)\n")
    if not ranked:
        print("  (no post-pop fade candidates today)\n")
        return 0
    for r in ranked:
        s = r["snap"]
        print(f"  {r['score']:>5.1f}  {s['symbol']:<7} ${s['close'] or 0:<7.2f} "
              f"chg={s.get('pct_change') or 0:+.0f}%")
        if args.why:
            print(f"         └ {' · '.join(r['reasons'])}")
    print()
    return 0


def _cmd_backfill_outcomes(_args) -> int:
    n = evaluation.backfill_outcomes()
    print(f"backfilled {n} candidate outcomes")
    return 0


def _cmd_eval(args) -> int:
    s = evaluation.summary()
    print("\n  Predictability summary")
    print(f"  candidates evaluated : {s['candidates_evaluated']}")
    if not s["candidates_evaluated"]:
        print("  (no realized outcomes yet — run `surge backfill-outcomes` after a")
        print("   later session is available, i.e. once a candidate's next day exists)\n")
        return 0
    print(f"  base rate (surge)    : {s['base_rate']*100:.3f}%")
    print(f"  candidate hit  ≥{settings_near():.0f}% : {s['candidate_hit_rate']*100:.1f}%")
    print(f"  candidate surge +100%: {s['candidate_surge_rate']*100:.1f}%")
    print(f"  mean next-day move   : {s['mean_next_pct']:+.1f}%")
    if s["lift_vs_base"] is not None:
        print(f"  lift vs base rate    : ×{s['lift_vs_base']:.1f}")
    pk = evaluation.precision_at_k(args.k)
    if pk.get("days"):
        print(f"\n  Precision@{pk['k']} over {pk['days']} day(s): "
              f"hit {pk['mean_hit_rate']*100:.1f}% · surge {pk['mean_surge_rate']*100:.1f}%")
    print()
    return 0


def settings_near() -> float:
    from .config import settings
    return settings.near_surge_pct


def _cmd_rotation(args) -> int:
    from .rotation import engine

    cands = engine.run_and_store()
    if not cands:
        print("  후보 없음 (데이터 미수신 — `surge krx-check`)")
        return 0
    passed = [c for c in cands if c["passed"]]
    print(f"\n  ── ROTATION 스크린 ({cands[0]['date']}) — "
          f"{len(cands)}종목 중 {len(passed)} 통과 ──")
    print("  (AMVF: 가장 뜨거운 노드 제외, 관심이 *이동해 갈* 후방 1~2노드 우선)")
    print(f"  {'통과':<4}{'종목':<16}{'score':>6} {'수급σ':>6}{'RVOL':>6}{'체인/노드'}")
    for c in cands[: args.limit]:
        mark = "✓" if c["passed"] else "·"
        chain = f"{c['chain'] or '-'}:{c['node'] or '-'}(b{c['back_steps']})"
        print(f"  {mark:<4}{c['name']+'('+c['ticker']+')':<16}{c['score']:>6.2f}"
              f"{c['smart_money']:>6.1f}{c['rvol']:>6.1f}  {chain}")
        if args.why:
            print(f"        └ {' · '.join(c['reasons'])}")
    print("\n  ※ 정보·도구 — 투자자문 아님. 콜은 저장되며 T+1/T+3/T+5 사후 채점.\n")
    return 0


def _cmd_rotation_eval(_args) -> int:
    from .rotation import engine

    n = engine.evaluate()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, AVG(ret_t5) a5, "
            "AVG(CASE WHEN hit_t5=1 THEN 1.0 ELSE 0 END) hr "
            "FROM rotation_decisions WHERE evaluated_at IS NOT NULL "
            "AND passed_filter=1").fetchone()
    print(f"\n  rotation 채점: 이번 {n}건 확정")
    if row["n"]:
        print(f"  통과종목 누적 {row['n']}건 · T+5 평균 {(row['a5'] or 0)*100:+.1f}% · "
              f"+10% 도달률 {(row['hr'] or 0)*100:.0f}%")
    else:
        print("  (아직 T+5 확정된 통과 종목 없음)")
    lb = engine.variant_leaderboard()
    top = [(nm, s) for nm, s in lb["ranked"] if s["mean_t5"] is not None][:5]
    if top:
        print("  변형 A/B (top-3 픽 T+5 평균):")
        for nm, s in top:
            print(f"   · {nm:<12} {s['mean_t5']*100:+.1f}% (n{s['n']})")
        if lb["recommend"]:
            r = lb["recommend"]
            print(f"   🏆 승격 후보: {r['variant']} ({r['mean_t5']*100:+.1f}%, n{r['n']})")
    print()
    return 0


def _cmd_verdict(_args) -> int:
    from . import verdict as V

    rows = V.assess()
    print("\n  ══ 검증 게이트 (이 시스템을 믿어도 되는가?) ══")
    print(f"  {'전략':<22}{'지표':>10}{'기준선':>9}{'초과':>8}{'n':>5}  판정")
    for r in rows:
        def pc(x):
            return f"{x*100:.0f}%" if x is not None else "—"
        ex = (f"{r['excess']*100:+.0f}%p" if r["excess"] is not None else "—")
        print(f"  {r['mark']} {r['strategy']:<20}{pc(r['value']):>10}"
              f"{pc(r['baseline']):>9}{ex:>8}{r['n']:>5}  {r['verdict']}")
        # ⭐ graduation checklist — only shown once a strategy is a 🟢 edge
        # (a candidate for becoming a signal); each ✓/✗ is one of the 5 gates.
        if r.get("signal_checks") and r["mark"] in ("🟢", "⭐"):
            chk = " · ".join(
                f"{'✓' if c['ok'] else '✗'}{c['k']}"
                + (f"({c['v']})" if c["v"] else "")
                for c in r["signal_checks"])
            print(f"      ⭐신호 조건: {chk}")
    print(f"\n  결론: {V.headline()}")
    print("  ※ 🟢=통계적 엣지(가설) · ⭐=신호(5조건 충족, 베팅 참조 가능). "
          "⭐ 전까지 산출물은 추천이 아니라 가설.\n")
    return 0


def _cmd_watch(args) -> int:
    from .watch import engine

    horizons = (["short", "swing", "long"] if args.horizon == "all"
                else [args.horizon])
    for h in horizons:
        if h == "long":
            rows = engine.multibagger()
            print("\n  ── 장기 멀티배거 추적 도시어 (구조적 옵셔널리티, 확률 아님) ──")
            print(f"  {'종목':<22}{'점수':>5} {'시총':>7}{'52주낙폭':>9}  테마/10x여지")
            for r in rows[: args.limit]:
                print(f"  {r['name']+'('+r['ticker']+')':<22}{r['score']:>5.0f}"
                      f"{r['cap']:>7}{r['drawdown']*100:>8.0f}%  {r['theme']} · {r['tenx']}")
            print("  ※ 확률 부여 없음 — 작을수록 10x 여지 큼(로또 성격). 분산·소액·장기.")
            continue
        rows = engine.levels(h, persist=args.persist)
        label = "단기(≤1주)" if h == "short" else "스윙(1주~3개월)"
        print(f"\n  ── {label} 매수가치 + 가격대 ({rows[0]['asof'] if rows else '—'}) ──")
        print(f"  {'종목':<20}{'점수':>4}  {'현재':>10}  매수존 / 손절 / 목표 (R:R)")

        def fp(v, mkt):   # KR won (no decimals) vs US $ (2 decimals)
            return f"{v:,.0f}" if mkt == "kr" else f"{v:,.2f}"
        for r in rows[: args.limit]:
            m = r["mkt"]
            print(f"  {r['name']+'('+r['ticker']+')':<20}{r['score']:>4.0f}  "
                  f"{fp(r['ref'], m):>10}  {fp(r['buy_low'], m)}~{fp(r['buy_high'], m)}"
                  f" / 손 {fp(r['stop'], m)} / 익 {fp(r['target'], m)} (R:R {r['rr']})")
            if args.why:
                print(f"        └ {' · '.join(r['reasons'])}")
    print("\n  ※ 정보·도구 — 투자자문 아님. 가격은 ATR/추세 기반 기계적 산출(추천 아님).\n")
    return 0


def _cmd_krx_check(_args) -> int:
    from .sources import krx

    h = krx.health()
    print("\n  ── KRX 연결 점검 ──")
    print(f"  자격증명: SURGE_KRX_ID/PW "
          f"{'설정됨 ✓' if h['creds_present'] else '미설정 ✗'}"
          f"{' (→ KRX_ID/PW 브리지)' if h['creds_bridged'] else ''}")
    for c in h["capabilities"]:
        mark = "✓" if c["ok"] else "✗"
        tag = "MDC,로그인" if c["gated"] else "keyless"
        extra = (f" rows={c['rows']}" if c["ok"]
                 else (f" — {c['error']}" if c["error"] else " rows=0(미수신)"))
        print(f"   {mark} {c['capability']:<30}[{tag}]{extra}")
    if h["keyless_ok"] and h["gated_ok"]:
        print("\n  ✅ 완전 가동 — rotation champion 전 계층 사용 가능\n")
    elif h["keyless_ok"]:
        print("\n  🟢 핵심 계층 가동: 가격·구조 + **스마트머니(외인/기관, Naver keyless)**.")
        print("     공매도(L7, 비중 5%)만 KRX 계정 필요 — 없어도 champion 대부분 작동\n")
    else:
        print("\n  🔴 keyless 계층 미수신 — 네트워크/패키지(`uv sync --extra kr`) 확인\n")
    return 0


def _cmd_quotes(args) -> int:
    from .sources import quotes

    syms = [s.strip().upper() for s in args.symbols.split(",")]
    print()
    for sym in syms:
        q = quotes.fetch_quote(sym)
        if q:
            print(f"  {sym:<6} ${q['price']:,.2f}   (source: {q['source']})")
        else:
            print(f"  {sym:<6} — 모든 시세 소스 실패")
    if args.health:
        print("\n  Provider health (probe: SOXL):")
        for h in quotes.provider_health("SOXL"):
            mark = "✓" if h["ok"] else ("미설정" if not h["configured"] else "✗")
            px = f"${h['price']:,.2f}" if h["price"] else "—"
            print(f"   {mark:<4} {h['provider']:<12} {px:>10}  {h['ms']}ms")
        print("\n  finnhub 활성화: .env에 SURGE_FINNHUB_API_KEY 설정 (무료 60콜/분)")
    print()
    return 0


def _print_duel_card(d, pair: dict) -> None:
    from .config import settings

    print(f"\n  ── {pair['name']} ({pair['bull']} vs {pair['bear']}) — "
          f"{d.date} 미국 세션 ──")
    if d.side == "STAND_ASIDE":
        print(f"  판정: ⏸  관망 (STAND_ASIDE)   score={d.score:+.2f}")
    else:
        emoji = "🟢" if d.side == pair["bull"] else "🔴"
        print(f"  판정: {emoji} {d.side} 매수   score={d.score:+.2f} "
              f"확신도={d.conviction:.2f} ({'풀' if d.size_factor == 1 else '절반'} 사이즈)")
        if d.entry_ref and d.stop_price and d.target_price:
            print(f"  계획: 진입≈${d.entry_ref:.2f}  손절 ${d.stop_price:.2f}  "
                  f"익절 ${d.target_price:.2f}  (미체결 시 종가 청산, 오버나이트 금지)")
        elif d.entry_ref:
            print(f"  계획: 진입≈${d.entry_ref:.2f}  ⚠ ATR 결측 — 브래킷 산출 불가, "
                  "수동 손절 설정 필요")
        print(f"  비중: 자본의 {d.size_pct*100:.0f}% "
              f"(예: ${settings.starting_capital:,.0f} 기준 "
              f"${settings.starting_capital*d.size_pct:,.0f})")
    if d.shadow_prob is not None:
        from .duel import calibration
        p = d.shadow_prob
        line = f"  적응 엔진: P(상승) {p*100:.1f}%"
        look = calibration.lookup(d.pair_id, p)
        if look:
            line += f" — 이 확신 구간({look['bucket']})의 과거 적중률:"
            if look["replay_acc"] is not None:
                line += (f" {look['replay_acc']*100:.1f}%"
                         f" (리플레이 n={look['replay_n']:,})")
            if look["forward_n"]:
                line += (f" · 전진 {look['forward_acc']*100:.0f}%"
                         f" (n={look['forward_n']})")
        print(line)
    print("  근거:")
    for r in d.reasons:
        print(f"   · {r}")


def _cmd_duel(args) -> int:
    from .duel import data as duel_data
    from .duel import live as duel_live
    from .duel.pairs import PAIRS, get_pair

    pair_ids = list(PAIRS) if args.pair == "all" else [args.pair]
    unknown = [p for p in pair_ids if p not in PAIRS]
    if unknown:
        print(f"  알 수 없는 페어: {unknown} — 선택지: {', '.join(PAIRS)} 또는 all")
        return 1
    # macro/Asia frames are identical across pairs — fetch once
    shared = duel_data.fetch_shared("6mo") if len(pair_ids) > 1 else None
    for pid in pair_ids:
        pair = get_pair(pid)
        try:
            d = duel_live.tonight(with_futures=not args.no_futures, pair_id=pid,
                                  shared=shared)
        except Exception as exc:  # noqa: BLE001 — one pair must not kill the rest
            print(f"\n  {pair['name']}: 데이터 부족/오류 — {exc}")
            continue
        _print_duel_card(d, pair)
    print("\n  ※ 투자자문 아님 · 수익 보장 없음 · 3배 레버리지 상품 · 콜은 저장되며"
          " `surge duel-eval`로 사후 채점됩니다.\n")
    return 0


def _conviction(score: float) -> tuple[str, str, str]:
    """Intuitive read of a duel score: (방향, 확신등급, 막대). The score is a
    weighted directional VOTE in ~[-1,1] — sign = leg, |size| = how strongly the
    signals agree (NOT a probability or expected return). Tiers mirror the sizing
    gate: <0.15 관망 · 0.15–0.35 하프 · ≥0.35 풀."""
    c = abs(score)
    arrow = "▲강세" if score >= 0 else "▼약세"
    if c < 0.15:
        return arrow, "관망", "○○○"
    if c < 0.35:
        return arrow, "하프", "●●○"
    return arrow, "풀", "●●●"


def _cmd_report(_args) -> int:
    """One-screen daily report: tonight's calls, yesterday's score, cumulative
    record, signal quality, system health. The single thing to read each day."""
    import json as _json

    from .config import settings
    from .duel import gapanalysis
    from .duel import live as duel_live
    from .duel.pairs import PAIRS

    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(decision_date) d FROM duel_decisions").fetchone()["d"]
        calls = conn.execute(
            "SELECT * FROM duel_decisions WHERE decision_date=? ORDER BY pair",
            (latest,),
        ).fetchall() if latest else []
        scored = conn.execute(
            "SELECT * FROM duel_decisions WHERE evaluated_at IS NOT NULL "
            "ORDER BY decision_date DESC, pair LIMIT 8").fetchall()

    from . import verdict as _V
    print(f"\n  검증 게이트: {_V.headline()}  (상세: surge verdict)")
    print(f"\n┌─ DUEL 데일리 리포트 ({latest or '—'} 세션 콜) " + "─" * 22)
    for r in calls:
        pair = PAIRS.get(r["pair"], {})
        mark = ("🟢" if r["side"] == pair.get("bull")
                else "🔴" if r["side"] == pair.get("bear") else "⏸")
        plan = ""
        if r["side"] != "STAND_ASIDE" and r["entry_ref"]:
            plan = (f"  진입${r['entry_ref']:.2f}"
                    + (f" 손절${r['stop_price']:.2f} 익절${r['target_price']:.2f}"
                       if r["stop_price"] else " (브래킷 결측)"))
        if r["side"] == "STAND_ASIDE":
            body = "⏸ 관망 (신호 합의 부족)"
        else:
            arrow, tier, bar = _conviction(r["score"])
            body = f"{r['side']:<6} {arrow} · 확신 {tier} {bar} (score{r['score']:+.2f})"
        print(f"│ {mark} {r['pair']:<11} {body}{plan}")
    print("│ ※ 확신=신호 합의 강도(확률·수익률 아님) · 관망<하프<풀 · score 부호=방향")

    t = duel_live._tally()
    n_bets = t["wins"] + t["losses"]
    acc = f"{t['accuracy']*100:.0f}%" if t["accuracy"] is not None else "—"
    # A flashy 100% on a handful of bets reads as "it works" — make the number
    # self-qualify until it clears the significance floor (see surge verdict).
    if t["accuracy"] is not None and n_bets < settings.variant_min_n:
        acc += f"·표본부족 n<{settings.variant_min_n}"
    print(f"├─ 누적: 베팅 {n_bets} (적중 {t['wins']}·오답 {t['losses']} → {acc}) · "
          f"관망 {t['abstains']} · 평균손익 {(t['avg_pnl_pct'] or 0)*100:+.2f}%")

    recent_scored = [r for r in scored if r["correct"] is not None][:4]
    if recent_scored:
        line = " · ".join(
            f"{'✓' if r['correct'] else '✗'}{r['side']}{(r['pnl_pct'] or 0)*100:+.1f}%"
            for r in recent_scored)
        print(f"├─ 최근 채점: {line}")

    ga = gapanalysis.analyze()
    if ga["component_accuracy"]:
        top = sorted(ga["component_accuracy"].items(),
                     key=lambda x: -(x[1]["rate"] or 0))
        line = " · ".join(f"{k} {v['rate']*100:.0f}%" for k, v in top[:6]
                          if v["rate"] is not None)
        print(f"├─ 신호 일치율(전진): {line}")
    notes = []
    if ga["n_wrong"]:
        notes.append(f"갭선반영 {ga['gap_absorbed']}/{ga['n_wrong']}")
    if ga["whipsaw"]:
        notes.append(f"휩쏘 {ga['whipsaw']}")
    if ga["abstain_avg_move"] is not None:
        notes.append(f"관망기회비용 평균 {ga['abstain_avg_move']*100:.1f}%")
    if notes:
        print(f"├─ 갭 분석: {' · '.join(notes)}")

    from .duel import variants
    lb = variants.leaderboard()
    top = [(n, s) for n, s in lb["ranked"] if s["acc"] is not None][:4]
    if top:
        line = " · ".join(
            f"{n}{' 👑' if n == lb['active'] else ''} {s['acc']*100:.0f}%(n{s['n']})"
            for n, s in top)
        print(f"├─ 변형 A/B: {line}")
    if lb["recommend"]:
        rec = lb["recommend"]
        print(f"├─ 🏆 승격 후보: {rec['variant']} {rec['acc']*100:.0f}% "
              f"(z={rec['z']:.1f}) → surge variants --promote {rec['variant']}")
    print("└─ ※ 정보·도구 제공 — 투자자문 아님 · 3x 상품 · STAND_ASIDE도 판정\n")
    _ = _json  # (reserved for --json output later)
    return 0


def _cmd_factors(args) -> int:
    from .duel import factors
    from .duel.pairs import PAIRS

    if getattr(args, "backfill", False):
        pairs = list(PAIRS) if args.pair == "all" else [args.pair]
        total = 0
        for pid in pairs:
            n = factors.backfill(period=args.period, pair_id=pid)
            total += n
            print(f"  섀도 팩터 백필: {pid} {n}세션 전진 채점")
        print(f"  합계 {total}세션 — 리더보드 즉시 갱신\n")

    lb = factors.leaderboard()
    base = lb.get("baseline")
    bp = f"{base*100:.0f}%" if base is not None else "—"
    print("  ── 섀도 팩터 리더보드 (미사용 후보 신호의 전진 부호적중) ──")
    print("  '어떤 팩터를 추가했어야 하나' — 라이브 결정엔 미관여, 누수 0")
    print(f"  기준선(always-bull/bear)={bp} · 추가는 기준선 유의 초과 必")
    print(f"  {'후보 팩터':<18}{'적중률':>8}{'n':>6}")
    for name, s in lb["ranked"]:
        acc = f"{s['acc']*100:.0f}%" if s["acc"] is not None else "—"
        print(f"  {name:<18}{acc:>8}{s['n']:>6}")
    rec = lb["recommend"]
    if rec:
        print(f"\n  🧩 추가 후보: '{rec['factor']}' {rec['acc']*100:.0f}% vs "
              f"기준선 {rec['baseline']*100:.0f}% (z={rec['z']:.2f} ≥ {rec['z_req']:.2f}, n{rec['n']})")
        print("     → signals.py WEIGHTS에 사람이 추가(과적합 게이트 통과분만)")
    else:
        from .config import settings
        print(f"\n  추가 후보 없음 (기준: n≥{settings.variant_min_n}, "
              "기준선 유의 초과 · Šidák 보정 — 노이즈 팩터 차단)")
    print()
    return 0


def _cmd_kr_factors(args) -> int:
    from .config import settings
    from .rotation import factors as kf

    if getattr(args, "backfill", False):
        n = kf.backfill(period_days=args.days)
        print(f"  KR 섀도 팩터 백필: {n} (티커×세션) 전진 채점 (DataLab 검색급등)")
    lb = kf.leaderboard()
    base = lb.get("baseline")
    bp = f"{base*100:.0f}%" if base is not None else "—"
    print("\n  ── KR 어텐션 섀도 팩터 (검색급등이 +10%/T+5를 선별하는가) ──")
    print(f"  풀 기저 도달률={bp} (n{lb['pool_n']}) · 추가는 기저 유의 초과 必")
    print(f"  {'후보 팩터':<18}{'점화시도달':>10}{'n':>6}")
    for name, s in lb["ranked"]:
        acc = f"{s['acc']*100:.0f}%" if s["acc"] is not None else "—"
        flag = " ⚠" if s.get("caveat") else ""
        print(f"  {name:<18}{acc:>10}{s['n']:>6}{flag}")
        if s.get("caveat"):
            print(f"      ⚠ {s['caveat']}")
    rec = lb["recommend"]
    if rec:
        print(f"\n  🧩 추가 후보: '{rec['factor']}' {rec['acc']*100:.0f}% vs "
              f"기저 {rec['baseline']*100:.0f}% (z={rec['z']:.2f} ≥ {rec['z_req']:.2f}, n{rec['n']})")
        print("     → rotation 스크린 컴포넌트에 사람이 추가(게이트 통과분만)")
    else:
        print(f"\n  추가 후보 없음 (기준: n≥{settings.variant_min_n}, "
              "기저 유의 초과 · Šidák 보정)")
    print()
    return 0


def _cmd_duel_gap(args) -> int:
    from .duel import gapanalysis

    res = gapanalysis.analyze(None if args.pair == "all" else args.pair)
    print(f"\n  ── 예측 vs 실측 갭 원인 분석 ({args.pair}) ──")
    print(f"  분석 대상: 콜 {res['n_calls']}건 (베팅 {res['n_bets']} · "
          f"오답 {res['n_wrong']} · 관망 {res['abstain_n']})")
    if res["n_wrong"]:
        print(f"  오답 중 갭 선반영: {res['gap_absorbed']}건 "
              f"({res['gap_absorbed']/res['n_wrong']*100:.0f}%)")
    if res["whipsaw"]:
        print(f"  휩쏘 스탑아웃(방향 적중·실행 손실): {res['whipsaw']}건")
    if res["abstain_avg_move"] is not None:
        print(f"  관망일 평균 |움직임|: {res['abstain_avg_move']*100:.1f}%")
    if res["component_accuracy"]:
        print("\n  신호별 방향 일치율 (라이브 전진 기록, |v|≥0.1):")
        for name, s in sorted(res["component_accuracy"].items(),
                              key=lambda x: -(x[1]["rate"] or 0)):
            rate = f"{s['rate']*100:.0f}%" if s["rate"] is not None else "—"
            print(f"   · {name:<16} {rate:>5}  (n={s['total']})")
    recent = res["calls"][-args.limit:]
    if recent:
        print("\n  최근 콜별 원인:")
        for c in recent:
            lab = f"{c['label']*100:+.1f}%" if c["label"] is not None else "—"
            gap = f"갭{c['gap_ret']*100:+.1f}%" if c["gap_ret"] is not None else ""
            mark = {1: "✓", 0: "✗"}.get(c["correct"], "⏸")
            print(f"   {mark} {c['date']} [{c['pair']}] {c['side']:<12} "
                  f"score={c['score']:+.2f} 실측={lab} {gap}")
            for cause in c["causes"]:
                print(f"      └ {cause}")
    print()
    return 0


def _cmd_variants(args) -> int:
    from .duel import variants

    if args.backfill:
        n = variants.backfill()
        print(f"  변형 백필: {n} champion 콜에서 섀도 변형 생성·채점")
    if args.reset:
        variants.set_active("champion")
        print("  active 모델 → champion(기본 가중치)로 리셋")
        return 0
    if args.promote:
        variants.set_active(args.promote)
        print(f"  ✅ active 모델 승격: '{args.promote}' — 다음 콜부터 적용")
        return 0

    lb = variants.leaderboard()
    base = lb.get("baseline")
    bp = f"{base*100:.0f}%" if base is not None else "—"
    print(f"\n  ── 모델 변형 리더보드 (전진·방향성 채점, active='{lb['active']}') ──")
    print("  (변형은 매일 방향을 강제 커밋 → 신호 스킬 비교용; champion 실거래는 관망 포함)")
    print(f"  기준선(always-bull/bear)={bp} · 승격은 champion+기준선 동시 초과 必")
    print(f"  {'변형':<16}{'정확도':>8}{'n':>6}")
    for name, s in lb["ranked"]:
        acc = f"{s['acc']*100:.0f}%" if s["acc"] is not None else "—"
        star = " 👑" if name == lb["active"] else (
            " 🔬" if name in lb.get("discovered", []) else "")
        print(f"  {name:<16}{acc:>8}{s['n']:>6}{star}")
    win = lb.get("window")
    if win:
        print(f"  최근{win['n']}건 champion 정확도 {win['acc']*100:.0f}% (레짐 점검)")
    if lb.get("discovered"):
        print(f"  🔬 자동발굴 가설: {', '.join(lb['discovered'])} (진단→전진검증 중)")
    rec = lb["recommend"]
    if rec:
        print(f"\n  🏆 승격 후보: '{rec['variant']}' "
              f"{rec['acc']*100:.0f}% vs champion {(rec['champ_acc'] or 0)*100:.0f}% "
              f"(n={rec['n']}, champion z={rec['z']:.2f} · "
              f"기준선 z={rec.get('z_base', 0):.2f} ≥ {rec.get('z_req', 1.64):.2f})")
        print(f"     → 적용: surge variants --promote {rec['variant']}")
    else:
        from .config import settings
        print(f"\n  승격 후보 없음 (기준: n≥{settings.variant_min_n}, "
              "champion+기준선 동시 초과 · Šidák 다중검정 보정 — 과적합/노이즈 차단)")
    print()
    return 0


def _cmd_duel_archive(args) -> int:
    from .duel import data as duel_data

    res = duel_data.archive(period=args.period)
    print(f"\n  price_history 아카이브: {res['total_rows']:,}행 적재")
    for sym, n in res["symbols"].items():
        print(f"   {sym:<10} {n:>6,}행")
    print()
    return 0


def _cmd_duel_backtest(args) -> int:
    from .duel import backtest as duel_bt
    from .duel.pairs import PAIRS

    pair_ids = list(PAIRS) if args.pair == "all" else [args.pair]
    rc = 0
    for pid in pair_ids:
        rc = max(rc, (_duel_compare_one if args.compare
                      else _duel_backtest_one)(duel_bt, pid, args))
    return rc


def _duel_compare_one(duel_bt, pair_id: str, args) -> int:
    res = duel_bt.compare(period=args.period, pair_id=pair_id,
                          offline=args.offline)
    first = next(iter(res.values()))
    if "error" in first:
        print(f"  [{pair_id}] {first['error']}")
        return 1
    print(f"\n  Duel 2×2 비교 [{pair_id}] ({args.period}) — 동일 구간, "
          "adaptive는 전 구간 아웃오브샘플")
    print(f"  {'구성':<16} {'베팅':>5} {'관망':>5} {'가드':>5} "
          f"{'적중률':>7} {'z':>6} {'수익':>8} {'Sharpe':>7}")
    for name, r in res.items():
        if "error" in r:
            print(f"  {name:<16} {r['error']}")
            continue
        m = r["metrics"]
        print(f"  {name:<16} {r['n_traded']:>5} {r['n_abstain']:>5} "
              f"{r['n_gap_guard']:>5} {r['accuracy']*100:>6.1f}% "
              f"{r['z_vs_coin']:>+6.2f} {m['total_return']*100:>+7.1f}% "
              f"{m['sharpe']:>7.2f}")
        if r["n_gap_guard"]:
            ba = r["guard_blocked_accuracy"]
            print(f"  {'':<16} └ 가드 차단 {r['n_gap_guard']}건: 차단된 트레이드"
                  f" 적중률 {ba*100:.0f}% · would-have 합산 "
                  f"{r['guard_blocked_pnl_sum']*100:+.1f}%")
    print()
    return 0


def _duel_backtest_one(duel_bt, pair_id: str, args) -> int:
    cfg = getattr(args, "config", None)
    res = duel_bt.run(period=args.period, pair_id=pair_id, offline=args.offline,
                      mode="adaptive" if (args.adaptive or cfg) else "static",
                      gap_guard_z=args.gap_guard,
                      adaptive_config=cfg or "adaptive")
    if "error" in res:
        print(f"  [{pair_id}] {res['error']}")
        return 1
    m = res["metrics"]
    mode_tag = " · adaptive(전진 OOS)" if res["mode"] == "adaptive" else ""
    guard_tag = (f" · 갭가드 {res['n_gap_guard']}건 차단"
                 if res.get("n_gap_guard") else "")
    print(f"\n  Duel 백테스트 [{pair_id}] ({args.period}{mode_tag})  "
          f"{res['n_days']}일 중 "
          f"{res['n_traded']}일 베팅 / {res['n_abstain']}일 관망{guard_tag}")
    print(f"  방향 적중률: {res['accuracy']*100:.1f}%  "
          f"(동전던지기 대비 z={res['z_vs_coin']:+.2f})")
    if res.get("accuracy_full_size") is not None:
        half = res.get("accuracy_half_size")
        print(f"  확신도별: 풀사이즈 {res['accuracy_full_size']*100:.0f}%"
              + (f" · 절반사이즈 {half*100:.0f}%" if half is not None else ""))
    print(f"  수익: {m['total_return']*100:+.1f}%  Sharpe={m['sharpe']:.2f}  "
          f"MaxDD={m['max_drawdown']*100:.1f}%  PF={m['profit_factor']:.2f}")
    print(f"  기준선(raw 합산): always-{res['bull']} "
          f"{res['baseline_always_soxl']*100:+.0f}% · always-{res['bear']} "
          f"{res['baseline_always_soxs']*100:+.0f}% · "
          f"오라클 {res['oracle_sum']*100:+.0f}%")
    if res["ic"]:
        print("  신호별 IC(정보계수, 익일 SOXX 방향과의 상관):")
        for k, v in sorted(res["ic"].items(), key=lambda x: -abs(x[1])):
            print(f"   · {k:<16} {v:+.3f}")
    print()
    return 0


def _cmd_adaptive(args) -> int:
    """변인 추정 대시보드: 학습기가 현재 credit하는 변인 가중치, 그 드리프트,
    그리고 '모델이 어떻게 배워야 하는가'(설정 레이스)의 전진/리플레이 성적."""
    from .db import connect
    from .duel import adaptive
    from .duel.pairs import PAIRS

    pair_ids = list(PAIRS) if args.pair == "all" else [args.pair]
    unknown = [p for p in pair_ids if p not in PAIRS]
    if unknown:
        print(f"  알 수 없는 페어: {unknown} — 선택지: {', '.join(PAIRS)} 또는 all")
        return 1

    if args.calibrate:
        from .duel import calibration
        for pid in pair_ids:
            res = calibration.replay_calibration(pid, offline=args.offline,
                                                 period=args.period)
            if "error" in res:
                print(f"\n  [{pid}] {res['error']}")
                continue
            print(f"\n  확신 구간별 적중률 [{pid}] — 리플레이(전량 OOS) + 전진 기록")
            print(f"  {'구간':<8} {'리플레이 n':>10} {'적중률':>7} "
                  f"{'전진 n':>7} {'적중률':>7}")
            for row in calibration.table(pid):
                ra = (f"{row['replay_acc']*100:5.1f}%"
                      if row["replay_acc"] is not None else "    —")
                fa = (f"{row['forward_acc']*100:5.1f}%"
                      if row["forward_acc"] is not None else "    —")
                print(f"  {row['bucket']:<8} {row['replay_n']:>10,} {ra:>7} "
                      f"{row['forward_n']:>7} {fa:>7}")
        print("\n  ※ 밤 카드가 이 표에서 자기 확신 구간의 성적을 인용한다."
              " 전진 열이 최종 심판.\n")
        return 0

    if args.replay:
        from .duel import backtest as duel_bt
        for pid in pair_ids:
            res = duel_bt.race(period=args.period, pair_id=pid,
                               offline=args.offline)
            if "error" in res:
                print(f"\n  [{pid}] {res['error']}")
                continue
            print(f"\n  적응 설정 레이스 [{pid}] — {res['n_sessions']}세션, "
                  f"전량 아웃오브샘플 (항상-한방향 기준선 "
                  f"{res['baseline_always']*100:.1f}%)")
            print(f"  {'설정':<20} {'n':>6} {'적중률':>7} {'z(동전)':>8} "
                  f"{'z(한방향)':>9}")
            ranked = sorted(res["configs"].items(),
                            key=lambda kv: -(kv[1]["accuracy"] or 0))
            for name, s in ranked:
                if s["accuracy"] is None:
                    continue
                print(f"  {name:<20} {s['n']:>6} {s['accuracy']*100:>6.1f}% "
                      f"{s['z_vs_coin']:>+8.2f} {s['z_vs_always']:>+9.2f}")
        print("\n  ※ 리플레이는 후보 선별용 — 최종 심판은 밤마다 쌓이는 전진"
              " 기록(duel_variants)이다.\n")
        return 0

    shown = False
    for pid in pair_ids:
        cur = adaptive.weight_snapshot(pid)
        if cur is None:
            continue
        shown = True
        drift = adaptive.weight_drift(pid, back=args.back) or {}
        d = drift.get("drift", {})
        print(f"\n  변인 추정 [{pid}] — 학습기가 현재 credit하는 가중치"
              f" (드리프트: {args.back}세션 전 대비)")
        for feat in sorted(cur, key=lambda f: -abs(cur[f])):
            dv = d.get(feat)
            tag = f"  Δ{dv:+.3f}" if dv is not None else ""
            flip = "  ⚠부호반전" if feat in (drift.get("sign_flips") or []) else ""
            print(f"   · {feat:<18} {cur[feat]:+.3f}{tag}{flip}")
    if not shown:
        print("\n  변인 가중치 기록이 아직 없습니다 — 밤 콜(surge duel)이 돌 때마다"
              " 박제됩니다 (price_history 아카이브에 min_train 이상의 세션 필요).")

    with connect() as conn:
        rows = conn.execute(
            "SELECT variant, COUNT(*) n, "
            "SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins "
            "FROM duel_variants WHERE variant LIKE 'adaptive%' "
            "AND evaluated_at IS NOT NULL AND correct IS NOT NULL "
            "GROUP BY variant").fetchall()
    if rows:
        print("\n  설정 레이스 — 전진 기록 (라이브 채점 누적):")
        for r in sorted(rows, key=lambda r: -((r["wins"] or 0) / r["n"])):
            acc = (r["wins"] or 0) / r["n"]
            print(f"   · {r['variant']:<20} n={r['n']:>4}  적중률 {acc*100:.1f}%")
    else:
        print("\n  설정 레이스 전진 기록 없음 — `surge duel` 야간 실행 + 익일"
              " `surge duel-eval`이 쌓아갑니다."
              " 과거 근거는 `surge adaptive --replay --offline`으로 확인.")
    print()
    return 0


def _cmd_duel_eval(_args) -> int:
    from . import learn
    from .duel import live as duel_live

    t = duel_live.eval_outcomes()
    # Evolution step: after fresh labels land, let the system mine its own
    # forward diagnostics for new anti-signal hypotheses and race them forward.
    fresh = learn.register_discovered()
    if fresh:
        print(f"  🔬 자동발굴 가설 등록: {', '.join(fresh)} (전진 검증 시작)")
    print(f"\n  Duel 누적 채점: {t['evaluated']}건 평가 "
          f"(베팅 {t['wins'] + t['losses']} · 관망 {t['abstains']})")
    if t["accuracy"] is not None:
        print(f"  적중 {t['wins']} / 오답 {t['losses']} → "
              f"적중률 {t['accuracy']*100:.0f}%  평균 손익 "
              f"{(t['avg_pnl_pct'] or 0)*100:+.2f}%")
    else:
        print("  (아직 채점된 베팅 없음)")
    for pid, p in (t.get("pairs") or {}).items():
        acc = f"{p['accuracy']*100:.0f}%" if p["accuracy"] is not None else "—"
        print(f"   · {pid:<11} 평가 {p['evaluated']:>3} · 적중률 {acc} · "
              f"관망 {p['abstains']}")
    print()
    return 0


def _cmd_dashboard(args) -> int:
    import uvicorn
    print(f"\n  surge HTS dashboard → http://{args.host}:{args.port}\n")
    if args.host in ("0.0.0.0", "::"):                 # bound to the LAN → show phone URL
        import socket

        from .config import settings
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))                 # no packets sent; picks the LAN route
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            ip = None
        if ip:
            print(f"  📱 휴대폰(같은 Wi-Fi) → http://{ip}:{args.port}")
            print("     · PC와 같은 공유기에 연결 · Windows 방화벽에서 해당 포트 허용 필요할 수 있음")
            if settings.dashboard_token:
                print("     · 조회는 자유 · 제어(Run/Kill)는 토큰 필요(설정됨)\n")
            else:
                print("     · 조회는 자유 · 제어(Run/Kill)는 원격 차단(안전기본값);"
                      " 필요 시 .env에 SURGE_DASHBOARD_TOKEN 설정\n")
    uvicorn.run("surge.dashboard.api:app", host=args.host, port=args.port,
                reload=False, log_level="warning")
    return 0


def _cmd_backtest(args) -> int:
    from .backtest import data, validation
    from .backtest.engine import BacktestEngine
    from .backtest.strategy import STRATEGIES

    if args.strategy not in STRATEGIES:
        print(f"unknown strategy '{args.strategy}' (choices: {list(STRATEGIES)})")
        return 1
    syms = ([s.strip().upper() for s in args.symbols.split(",")]
            if args.symbols else data.candidate_symbols(args.top))
    if not syms:
        print("no symbols (pass --symbols or build candidates first)")
        return 1
    print(f"  loading {len(syms)} symbols ({args.period})…")
    price = data.load_price_data(syms, period=args.period)
    if not price:
        print("  no price data loaded")
        return 1
    strat = STRATEGIES[args.strategy]()
    ekw = dict(stop_pct=args.stop, target_pct=args.target, hold_days=args.hold,
               max_positions=args.max_positions)
    res = BacktestEngine(strat, **ekw).run(price)
    m = res.metrics
    print(f"\n  Backtest [{args.strategy}]  {len(price)} symbols  "
          f"{m.get('n_periods')} days  {m.get('n_trades')} trades")
    print(f"  total_return={m.get('total_return',0)*100:+.1f}%  "
          f"CAGR={m.get('cagr',0)*100:+.1f}%  Sharpe={m.get('sharpe',0):.2f}  "
          f"Sortino={m.get('sortino',0):.2f}")
    print(f"  MaxDD={m.get('max_drawdown',0)*100:.1f}%  Calmar={m.get('calmar',0):.2f}  "
          f"WinRate={m.get('win_rate',0)*100:.0f}%  PF={m.get('profit_factor',0):.2f}")

    if args.montecarlo:
        mc = validation.monte_carlo([t.pnl for t in res.trades])
        if mc.get("n_sims"):
            print(f"\n  Monte Carlo ({mc['n_sims']} sims): "
                  f"prob_loss={mc['prob_loss']*100:.0f}%  "
                  f"final p5/p50/p95=${mc['p5_final']:,.0f}/"
                  f"${mc['median_final']:,.0f}/${mc['p95_final']:,.0f}  "
                  f"worst-5% MDD={mc['p5_max_drawdown']*100:.0f}%")
    if args.walkforward:
        wf = validation.walk_forward(price, strat, **ekw)
        print(f"\n  Walk-forward ({wf.get('n_windows',0)} windows): "
              f"mean_return={wf.get('mean_return',0)*100:+.1f}%  "
              f"pct_positive={wf.get('pct_positive',0)*100:.0f}%  "
              f"consistent={wf.get('consistent')}")
    if args.crash:
        ct = validation.crash_test(price, strat, **ekw)
        if ct:
            print(f"\n  Crash test ({ct['shock']*100:.0f}% on {ct['crash_date']}): "
                  f"return {ct['baseline_return']*100:+.1f}% → "
                  f"{ct['shocked_return']*100:+.1f}%  "
                  f"MDD {ct['baseline_mdd']*100:.0f}% → {ct['shocked_mdd']*100:.0f}%")
    print()
    return 0


def _cmd_trade(args) -> int:
    from .trading.models import TradingMode
    from .trading.orchestrator import TradingEngine, ensure_funded

    mode = TradingMode.LIVE if args.live else TradingMode.PAPER
    if mode == TradingMode.LIVE:
        print("\n  ⚠  LIVE mode — orders are STAGED for your approval, never "
              "auto-submitted. Use `surge approvals` to review.\n")
    ensure_funded(mode)
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    summary = TradingEngine(mode).run_cycle(symbols=syms, top=args.top)
    if summary.get("halted"):
        print("  system HALTED — kill switch active. Use `surge killswitch --reset`.")
        return 1
    if "kill_switch" in summary:
        print(f"  KILL SWITCH fired: {summary['kill_switch']}")
        return 0
    print(f"\n  Cycle [{summary['mode']}] status={summary['status']} "
          f"macro={summary['macro']} equity=${summary['equity']:,.0f}")
    for d in summary["decisions"]:
        o = d["outcome"]
        print(f"   {d['action']:<4} {d['symbol']:<7} net={d['net']:<5} "
              f"-> {o.get('result')}" + (f" {o.get('qty','')}@{o.get('price','')}"
                                         if o.get("result") == "filled" else ""))
    if summary["exits"]:
        print(f"   exits: {summary['exits']}")
    print()
    return 0


def _cmd_portfolio(args) -> int:
    from .trading import store
    from .trading.brokers import default_last_price
    from .trading.models import TradingMode
    from .trading.risk import RiskEngine

    mode = TradingMode.LIVE if args.live else TradingMode.PAPER
    positions = store.get_positions(mode)
    last = {p.symbol: (default_last_price(p.symbol) or p.avg_price) for p in positions}
    status, m = RiskEngine(mode).risk_state(last)
    print(f"\n  Portfolio [{mode.value}]  equity=${m['equity']:,.0f}  "
          f"cash=${m['cash']:,.0f}  status={status.value}")
    print(f"  daily={m['daily']*100:+.1f}%  weekly={m['weekly']*100:+.1f}%  "
          f"monthly={m['monthly']*100:+.1f}%  exposure={m['gross_exposure']*100:.0f}%")
    if not positions:
        print("  (no open positions)\n")
        return 0
    print(f"\n  {'sym':<7}{'qty':>8}{'avg':>9}{'last':>9}{'uPnL':>10}")
    for p in positions:
        lp = last.get(p.symbol, p.avg_price)
        print(f"  {p.symbol:<7}{p.qty:>8.0f}{p.avg_price:>9.2f}{lp:>9.2f}"
              f"{p.unrealized_pnl(lp):>+10.2f}")
    print()
    return 0


def _cmd_approvals(args) -> int:
    from .trading import store
    from .trading.audit import audit
    mode_note = "(live order approval queue — your action submits real orders)"
    if args.approve:
        store.set_approval(args.approve, "approved")
        audit("operator", "approved_order", payload={"order_id": args.approve})
        store.update_order_status(args.approve, "submitted")
        print(f"  approved {args.approve} {mode_note}")
        return 0
    if args.reject:
        store.set_approval(args.reject, "rejected")
        store.update_order_status(args.reject, "cancelled")
        audit("operator", "rejected_order", payload={"order_id": args.reject})
        print(f"  rejected {args.reject}")
        return 0
    pend = store.list_pending_approvals()
    print(f"\n  Pending approvals {mode_note}\n")
    if not pend:
        print("  (none)\n")
        return 0
    for a in pend:
        print(f"   {a['order_id']}  {a['side']:<4} {a['qty']:.0f} {a['symbol']} "
              f"{a['order_type']}")
    print("\n  Approve: surge approvals --approve <order_id>\n")
    return 0


def _cmd_killswitch(args) -> int:
    from .trading import killswitch, store
    from .trading.brokers import default_last_price
    from .trading.models import TradingMode

    mode = TradingMode.LIVE if args.live else TradingMode.PAPER
    if args.reset:
        killswitch.reset(mode)
        print("  kill switch reset — trading re-enabled")
        return 0
    positions = store.get_positions(mode)
    last = {p.symbol: (default_last_price(p.symbol) or p.avg_price) for p in positions}
    summary = killswitch.trigger(mode, args.reason or "manual trigger", last)
    print(f"  KILL SWITCH fired: {summary}")
    return 0


def _cmd_stats(_args) -> int:
    tables = [
        "securities", "daily_snapshot", "trap_flags", "candidates",
        "candidate_outcomes", "catalysts", "surge_events", "ingest_runs",
        "account_history", "positions", "orders", "fills", "decisions",
        "agent_opinions", "risk_state", "audit_log",
    ]
    with connect() as conn:
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            print(f"{t:<16} {n:>10}")
    return 0


def _cmd_daily(_args) -> int:
    """The daily self-improvement heartbeat: score → evolve → judge → record."""
    from . import daily

    r = daily.run_daily()
    print("\n  ── 🧠 오늘의 자기개선 리포트 "
          f"({r['run_date']}) ──")
    sc = r["scored"]
    print(f"  채점(증분): duel {sc.get('duel')} · rotation {sc.get('rotation')} · "
          f"surge {sc.get('surge')}")
    if r["discovered_new"]:
        print(f"  🔬 신규 발굴 가설: {', '.join(r['discovered_new'])} (전진 검증 시작)")
    else:
        print("  🔬 신규 발굴 가설: 없음")
    print(f"  판정: {r['headline']}")
    for name, e in r["evidence"].items():
        ep = e.get("evidence_pct")
        print(f"   · {name:<9} {e['mark']} n={e['n']:<5} "
              f"증거 {('%.0f%%' % (ep*100)) if ep is not None else '—'}")
    if r["changes"]:
        print(f"  변화: {' · '.join(r['changes'])}")
    if r["promote_ready"]:
        print(f"  ✅ 승인 대기(사람 검토): {', '.join(r['promote_ready'])} "
              "— 자동 승격 안 함")
    if r.get("stale_inputs"):
        print(f"  ⏰ 스케줄 점검: {', '.join(r['stale_inputs'])} 입력이 오래됨 "
              "— 예약 실행(작업 스케줄러)이 도는지 확인 필요")
    if r["warnings"]:
        print(f"  ⚠ 건너뛴 단계: {', '.join(r['warnings'])}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="surge", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=_cmd_init)
    sub.add_parser("universe", help="refresh securities master").set_defaults(
        func=_cmd_universe
    )

    sp = sub.add_parser("snapshot", help="run daily feature snapshot")
    sp.add_argument("--symbols", help="comma-separated subset (default: full universe)")
    sp.add_argument("--period", default="60d", help="yfinance history window")
    sp.add_argument("--limit", type=int, help="cap universe size (testing)")
    sp.add_argument(
        "--fast",
        action="store_true",
        help="enable Stage-1 price pre-filter (skip names far above max_price)",
    )
    sp.add_argument(
        "--asof",
        help="point-in-time replay: ignore bars after this ISO date (no look-ahead)",
    )
    sp.set_defaults(func=_cmd_snapshot)

    sp = sub.add_parser("watchlist", help="ranked surge candidates for the latest day")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--why", action="store_true", help="show score reasons")
    sp.set_defaults(func=_cmd_watchlist)

    sp = sub.add_parser(
        "reversals", help="fade watchlist: popped names likely to reverse down"
    )
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--min-score", type=float, default=2.0)
    sp.add_argument("--why", action="store_true", help="show score reasons")
    sp.set_defaults(func=_cmd_reversals)

    sub.add_parser(
        "fade", help="label archived surges as held/faded next day"
    ).set_defaults(func=_cmd_fade)

    sub.add_parser(
        "backfill-outcomes", help="record realized next-day move for past candidates"
    ).set_defaults(func=_cmd_backfill_outcomes)

    sp = sub.add_parser("eval", help="predictability metrics (Precision@K, lift)")
    sp.add_argument("--k", type=int, default=10)
    sp.set_defaults(func=_cmd_eval)

    sp = sub.add_parser("surges", help="list archived surge events")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=_cmd_surges)

    sub.add_parser("stats", help="row counts").set_defaults(func=_cmd_stats)

    # ── backtest ─────────────────────────────────────────────────────────────
    sp = sub.add_parser("backtest", help="leak-free strategy backtest + validation")
    sp.add_argument("--strategy", default="momentum", help="momentum | reversion")
    sp.add_argument("--symbols", help="comma-separated (default: candidates)")
    sp.add_argument("--top", type=int, default=30, help="candidate universe size")
    sp.add_argument("--period", default="2y", help="yfinance history window")
    sp.add_argument("--stop", type=float, default=0.10)
    sp.add_argument("--target", type=float, default=0.20)
    sp.add_argument("--hold", type=int, default=5)
    sp.add_argument("--max-positions", type=int, default=20)
    sp.add_argument("--montecarlo", action="store_true")
    sp.add_argument("--walkforward", action="store_true")
    sp.add_argument("--crash", action="store_true")
    sp.set_defaults(func=_cmd_backtest)

    # ── trading platform ─────────────────────────────────────────────────────
    sp = sub.add_parser("trade", help="run one trading decision cycle (paper default)")
    sp.add_argument("--symbols", help="comma-separated universe (default: candidates)")
    sp.add_argument("--top", type=int, default=10, help="top-N candidates to consider")
    sp.add_argument("--live", action="store_true",
                    help="LIVE mode (orders staged for approval, never auto-submitted)")
    sp.set_defaults(func=_cmd_trade)

    sp = sub.add_parser("portfolio", help="positions, equity, risk state")
    sp.add_argument("--live", action="store_true")
    sp.set_defaults(func=_cmd_portfolio)

    sp = sub.add_parser("approvals", help="live-order human-approval queue")
    sp.add_argument("--approve", help="approve order_id (submits the real order)")
    sp.add_argument("--reject", help="reject order_id")
    sp.set_defaults(func=_cmd_approvals)

    sp = sub.add_parser("killswitch", help="trigger/reset the kill switch")
    sp.add_argument("--reason", help="trigger reason")
    sp.add_argument("--reset", action="store_true", help="clear halt, re-enable trading")
    sp.add_argument("--live", action="store_true")
    sp.set_defaults(func=_cmd_killswitch)

    sub.add_parser("verdict", help="THE truth gate — does anything beat its baseline?"
                   ).set_defaults(func=_cmd_verdict)
    sub.add_parser("daily", help="daily self-improvement loop: score→evolve→judge→record"
                   ).set_defaults(func=_cmd_daily)

    sp = sub.add_parser("watch", help="curated targets: short/swing levels + multibagger")
    sp.add_argument("--horizon", default="all",
                    help="short | swing | long | all")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--why", action="store_true")
    sp.add_argument("--persist", action="store_true",
                    help="journal short/swing levels to watch_levels")
    sp.set_defaults(func=_cmd_watch)

    sub.add_parser("krx-check", help="diagnose KRX connectivity + credentials"
                   ).set_defaults(func=_cmd_krx_check)

    sp = sub.add_parser("rotation", help="KR value-chain rotation screen (AMVF)")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--why", action="store_true")
    sp.set_defaults(func=_cmd_rotation)
    sub.add_parser("rotation-eval", help="score rotation calls at T+1/T+3/T+5"
                   ).set_defaults(func=_cmd_rotation_eval)

    sp = sub.add_parser("quotes", help="live quotes via multi-provider failover")
    sp.add_argument("--symbols", default="SOXL,SOXS")
    sp.add_argument("--health", action="store_true",
                    help="probe every provider and show latency")
    sp.set_defaults(func=_cmd_quotes)

    # ── duel (leveraged/inverse pairs) ──────────────────────────────────────
    from .duel.pairs import PAIRS as _DUEL_PAIRS

    _pair_help = " | ".join(_DUEL_PAIRS) + " | all"
    sp = sub.add_parser("duel", help="tonight's bull-vs-bear calls (persisted)")
    sp.add_argument("--pair", default="all", help=_pair_help)
    sp.add_argument("--no-futures", action="store_true",
                    help="skip the live NQ-futures overlay")
    sp.set_defaults(func=_cmd_duel)

    sp = sub.add_parser("duel-backtest", help="replay the duel rule over history")
    sp.add_argument("--period", default="2y")
    sp.add_argument("--pair", default="soxl_soxs", help=_pair_help)
    sp.add_argument("--offline", action="store_true",
                    help="use the price_history archive instead of live fetch")
    sp.add_argument("--adaptive", action="store_true",
                    help="walk-forward learned weights (every call out-of-sample)")
    sp.add_argument("--config", default=None,
                    help="adaptive.CONFIGS entry to replay (implies --adaptive)")
    sp.add_argument("--gap-guard", type=float, default=None, metavar="Z",
                    help="cancel-at-open gap guard σ (0=off; default=production)")
    sp.add_argument("--compare", action="store_true",
                    help="2×2 verdict: static/adaptive × guard off/on")
    sp.set_defaults(func=_cmd_duel_backtest)

    sub.add_parser("duel-eval", help="score past duel calls vs what happened"
                   ).set_defaults(func=_cmd_duel_eval)

    sp = sub.add_parser("adaptive",
                        help="변인 추정: learned weights, drift, config race")
    sp.add_argument("--pair", default="all", help=_pair_help)
    sp.add_argument("--back", type=int, default=20,
                    help="drift comparison horizon in sessions (default 20)")
    sp.add_argument("--replay", action="store_true",
                    help="run the OOS config race over history (evidence, "
                         "not the final judge)")
    sp.add_argument("--calibrate", action="store_true",
                    help="확신 구간별 적중률 원장 갱신 (리플레이 OOS → 박제)")
    sp.add_argument("--offline", action="store_true",
                    help="use the price_history archive instead of live fetch")
    sp.add_argument("--period", default="2y",
                    help="look-back for --replay/--calibrate")
    sp.set_defaults(func=_cmd_adaptive)

    sub.add_parser("report", help="one-screen daily duel report"
                   ).set_defaults(func=_cmd_report)

    sp = sub.add_parser("variants",
                        help="shadow-variant A/B leaderboard + promotion")
    sp.add_argument("--promote", help="make this variant the active champion")
    sp.add_argument("--reset", action="store_true", help="revert to base champion")
    sp.add_argument("--backfill", action="store_true",
                    help="seed shadow rows from stored champion components")
    sp.set_defaults(func=_cmd_variants)

    sp = sub.add_parser("factors",
                        help="shadow-FACTOR leaderboard — which un-used signal to ADD")
    sp.add_argument("--backfill", action="store_true",
                    help="replay candidate factors over the archive (instant n)")
    sp.add_argument("--pair", default="soxl_soxs", help="pair id or 'all'")
    sp.add_argument("--period", default="2y", help="backfill look-back (e.g. 2y)")
    sp.set_defaults(func=_cmd_factors)

    sp = sub.add_parser("kr-factors",
                        help="KR attention shadow-factor leaderboard (search surge)")
    sp.add_argument("--backfill", action="store_true",
                    help="replay search-surge over DataLab history (instant n)")
    sp.add_argument("--days", type=int, default=365, help="backfill look-back days")
    sp.set_defaults(func=_cmd_kr_factors)

    sp = sub.add_parser("duel-gap",
                        help="why predictions diverged from verification")
    sp.add_argument("--pair", default="all", help="pair id or 'all'")
    sp.add_argument("--limit", type=int, default=10, help="recent calls to detail")
    sp.set_defaults(func=_cmd_duel_gap)

    sp = sub.add_parser("duel-archive",
                        help="persist daily bars for all pairs into price_history")
    sp.add_argument("--period", default="3mo", help="e.g. 3mo (incremental) or max")
    sp.set_defaults(func=_cmd_duel_archive)

    sp = sub.add_parser("dashboard", help="launch the HTS web dashboard (FastAPI)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=_cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage (e.g. cp949); force UTF-8 so
    # em-dashes and Korean score reasons print without UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("command failed: {}", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
