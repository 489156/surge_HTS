"""Phase 2 — static site export for GitHub Pages (모바일 열람용).

The dashboard's read layer, frozen to a single self-contained HTML page + a
data.json, so the nightly pipeline can publish "tonight's calls + how much to
trust them" to a fixed URL viewable from any phone — no server, no tokens.

Every collector is degrade-safe (a missing table yields an empty section, never
a crash) because this runs inside the same set +e pipeline as everything else.
The page embeds the data inline (window.DATA) so the single file works even
when opened from disk; data.json is written alongside for programmatic use.
"""

from __future__ import annotations

import json

from ..db import connect, utc_now


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — export must never break the pipeline
        return default


_COMP_KR = {
    "asia_lead": "아시아 선행", "trend": "추세", "momentum_5d": "모멘텀",
    "vix_regime": "변동성", "rates": "금리", "mean_reversion": "평균회귀",
    "futures": "선물",
}


def _attribution(components: list[dict], oc_ret: float | None,
                 side: str, bull: str) -> dict | None:
    """Post-hoc hit/miss REASON: compare each stored signal's directional vote
    against what the underlying actually did (open→close). value>0 = the signal
    pointed UP (toward the bull leg); realized_up = oc_ret>0. A signal was RIGHT
    when its direction matched the realized move. Leak-free — uses only the
    frozen components and the realized label."""
    if oc_ret is None or not components:
        return None
    realized_up = oc_ret > 0
    call_up = side == bull
    correct = call_up == realized_up
    ranked = sorted(components, key=lambda c: -abs(
        float(c.get("value") or 0) * float(c.get("weight") or 0)))
    right, wrong = [], []
    for c in ranked:
        v = float(c.get("value") or 0)
        if abs(v) < 0.05:                       # silent signal — no opinion
            continue
        label = _COMP_KR.get(c["name"], c["name"])
        (right if (v > 0) == realized_up else wrong).append(label)
    dom = next((c for c in ranked if abs(float(c.get("value") or 0)) >= 0.05),
               None)
    dom_label = _COMP_KR.get(dom["name"], dom["name"]) if dom else None
    dom_right = ((float(dom["value"]) > 0) == realized_up) if dom else None
    rdir = "상승" if realized_up else "하락"
    cdir = "상승" if call_up else "하락"
    pct_s = f"{oc_ret*100:+.2f}%"
    if correct:
        verdict = (f"적중 — {dom_label} 신호가 {rdir} 방향을 맞힘"
                   if dom_right else f"적중 — 다수 신호가 {rdir} 일치")
        verdict += f" (기초 {pct_s})"
    else:
        lead = f"{dom_label} 등 " if dom_label else ""
        verdict = (f"빗나감 — {lead}신호는 {cdir} 예측했으나 "
                   f"기초가 {rdir} 반전 ({pct_s})")
    return {"verdict": verdict, "realized_pct": oc_ret,
            "right": right[:4], "wrong": wrong[:4]}


def _session_cards(date: str, with_results: bool) -> list[dict]:
    """Per-pair call cards for one session date. `with_results` also attaches
    the realized outcome (correct / pnl / underlying open→close) so a completed
    session can show colls-vs-what-happened."""
    from ..duel import calibration
    from ..duel.pairs import PAIRS

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM duel_decisions WHERE decision_date=? ORDER BY pair",
            (date,)).fetchall()
        shadows = {r["pair"]: r["score"] for r in conn.execute(
            "SELECT pair, score FROM duel_variants "
            "WHERE variant='adaptive' AND decision_date=?", (date,))}
    cards = []
    for r in rows:
        pair = PAIRS.get(r["pair"], {})
        p = ev = None
        if r["pair"] in shadows and shadows[r["pair"]] is not None:
            p = (shadows[r["pair"]] + 1.0) / 2.0
            ev = _safe(lambda: calibration.lookup(r["pair"], p), None)
        try:
            reasons = json.loads(r["reasons"]) if r["reasons"] else []
        except (ValueError, TypeError):
            reasons = []
        card = {
            "pair": r["pair"], "name": pair.get("name", r["pair"]),
            "bull": pair.get("bull"), "bear": pair.get("bear"),
            "side": r["side"], "score": r["score"],
            "conviction": r["conviction"], "size_pct": r["size_factor"],
            "entry": r["entry_ref"], "stop": r["stop_price"],
            "target": r["target_price"], "model": r["model"],
            "forced": bool(r["forced"]) if "forced" in r.keys() else False,
            "adaptive_p": p, "bucket_evidence": ev, "reasons": reasons,
        }
        if with_results:
            card["correct"] = r["correct"]
            card["pnl_pct"] = r["pnl_pct"]
            card["oc_ret"] = r["soxx_oc_ret"]
            card["exit_reason"] = r["exit_reason"]
            try:
                comps = json.loads(r["components"]) if r["components"] else []
            except (ValueError, TypeError):
                comps = []
            card["analysis"] = _safe(
                lambda: _attribution(comps, r["soxx_oc_ret"], r["side"],
                                     pair.get("bull")), None) \
                if r["side"] != "STAND_ASIDE" else None
        cards.append(card)
    return cards


def _calls() -> dict:
    """The UPCOMING session's calls (금일/오늘 밤 예측) — MAX(decision_date),
    the newest call, typically generated pre-open and not yet scored."""
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(decision_date) d FROM duel_decisions").fetchone()
    latest = row["d"] if row else None
    if not latest:
        return {"date": None, "cards": []}
    return {"date": latest, "cards": _session_cards(latest, with_results=False)}


def _previous_results() -> dict:
    """The most recent SCORED session before the upcoming one (전날 콜+결과) —
    each pair's call shown against what the market actually did."""
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(decision_date) d FROM duel_decisions").fetchone()["d"]
        if not latest:
            return {"date": None, "cards": []}
        prev = conn.execute(
            "SELECT MAX(decision_date) d FROM duel_decisions "
            "WHERE decision_date < ? AND evaluated_at IS NOT NULL", (latest,)
        ).fetchone()["d"]
    if not prev:
        return {"date": None, "cards": []}
    cards = _session_cards(prev, with_results=True)
    scored = [c for c in cards if c.get("correct") is not None]
    wins = sum(1 for c in scored if c["correct"])
    return {"date": prev, "cards": cards,
            "n_scored": len(scored), "wins": wins,
            "accuracy": (wins / len(scored)) if scored else None}


def _tally() -> dict:
    from ..duel import live

    return live._tally()


def _verify() -> dict:
    from ..duel.verify import status

    return status()


def _calibration() -> dict:
    from ..duel import calibration
    from ..duel.pairs import PAIRS

    return {pid: calibration.table(pid) for pid in PAIRS}


def _weights() -> dict:
    from ..duel.adaptive import weight_drift, weight_snapshot
    from ..duel.pairs import PAIRS

    out = {}
    for pid in PAIRS:
        cur = weight_snapshot(pid)
        if not cur:
            continue
        drift = weight_drift(pid) or {}
        out[pid] = {"current": cur, "drift": drift.get("drift", {}),
                    "sign_flips": drift.get("sign_flips", [])}
    return out


def _race() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT variant, COUNT(*) n, "
            "SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins "
            "FROM duel_variants WHERE evaluated_at IS NOT NULL "
            "AND correct IS NOT NULL GROUP BY variant").fetchall()
    out = [{"variant": r["variant"], "n": r["n"],
            "acc": (r["wins"] or 0) / r["n"] if r["n"] else None}
           for r in rows]
    return sorted(out, key=lambda x: -(x["acc"] or 0))[:10]


def _learning() -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM learning_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row or not row["payload"]:
        return {}
    payload = json.loads(row["payload"])
    return {k: payload.get(k) for k in
            ("run_date", "headline", "changes", "promote_ready",
             "discovered_new", "verify")}


def _discipline() -> dict:
    """Per-user risk-discipline read + trajectory (성장추적). Empty when the
    user hasn't opted in — the card simply doesn't render."""
    from ..duel.discipline import summary, trajectory

    s = summary()
    if not s:
        return {}
    s["trajectory"] = [{"at": t["assessed_at"][:10], "factor": t["factor"]}
                       for t in trajectory(limit=12)]
    return s


def collect() -> dict:
    """Everything the public page shows. Read-only; per-section degrade-safe."""
    return {
        "generated_at": utc_now(),
        "calls": _safe(_calls, {"date": None, "cards": []}),
        "previous": _safe(_previous_results, {"date": None, "cards": []}),
        "tally": _safe(_tally, {}),
        "verify": _safe(_verify, {}),
        "calibration": _safe(_calibration, {}),
        "weights": _safe(_weights, {}),
        "race": _safe(_race, []),
        "discipline": _safe(_discipline, {}),
        "learning": _safe(_learning, {}),
    }


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    # </script> inside JSON strings would close the tag early — escape slashes.
    payload = payload.replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", payload)


def export_site(outdir: str = "site") -> dict:
    """Write index.html (self-contained) + data.json into `outdir`."""
    import pathlib

    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    data = collect()
    (out / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "index.html").write_text(render_html(data), encoding="utf-8")
    n_cards = len(data["calls"]["cards"])
    return {"outdir": str(out), "date": data["calls"]["date"],
            "cards": n_cards}


_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>surge — 야간 방향 콜</title>
<style>
:root{
  --bg:#F4F6F3; --card:#FDFEFD; --ink:#1C2422; --sub:#5C6A66; --line:#DCE3DF;
  --accent:#2E7D6B; --accent-ink:#FFFFFF;
  --bull:#C0392B; --bull-bg:#F9E9E7; --bear:#2E5FC0; --bear-bg:#E8EEFA;
  --hold:#6B7672; --hold-bg:#ECEFED; --warn:#B07818;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#10151A; --card:#171E24; --ink:#E6ECE9; --sub:#8FA09A; --line:#26303A;
  --accent:#4FA48F; --accent-ink:#0C1210;
  --bull:#E06052; --bull-bg:#33201E; --bear:#5B85E0; --bear-bg:#1B2436;
  --hold:#8A948F; --hold-bg:#20272C; --warn:#D19A3F;
}}
:root[data-theme="light"]{
  --bg:#F4F6F3; --card:#FDFEFD; --ink:#1C2422; --sub:#5C6A66; --line:#DCE3DF;
  --accent:#2E7D6B; --accent-ink:#FFFFFF;
  --bull:#C0392B; --bull-bg:#F9E9E7; --bear:#2E5FC0; --bear-bg:#E8EEFA;
  --hold:#6B7672; --hold-bg:#ECEFED; --warn:#B07818;
}
:root[data-theme="dark"]{
  --bg:#10151A; --card:#171E24; --ink:#E6ECE9; --sub:#8FA09A; --line:#26303A;
  --accent:#4FA48F; --accent-ink:#0C1210;
  --bull:#E06052; --bull-bg:#33201E; --bear:#5B85E0; --bear-bg:#1B2436;
  --hold:#8A948F; --hold-bg:#20272C; --warn:#D19A3F;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.55 "Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,
  "Malgun Gothic","Apple SD Gothic Neo",sans-serif;
  -webkit-font-smoothing:antialiased}
main{max-width:680px;margin:0 auto;padding:20px 14px 60px;
  display:flex;flex-direction:column;gap:22px}
h1{font-size:1.25rem;margin:0;letter-spacing:-.01em}
h1 small{color:var(--sub);font-weight:400;font-size:.8rem;margin-left:8px}
h2{font-size:.82rem;margin:0 0 10px;color:var(--sub);font-weight:600;
  text-transform:uppercase;letter-spacing:.08em}
section{background:var(--card);border:1px solid var(--line);padding:16px}
.summary{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.badge{display:inline-block;padding:3px 10px;font-size:.78rem;font-weight:600;
  border:1px solid var(--line);background:var(--bg)}
.badge.ok{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.badge.warn{color:var(--warn);border-color:var(--warn)}
.card{border:1px solid var(--line);padding:12px 14px;margin-bottom:10px}
.card:last-child{margin-bottom:0}
.pairline{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.pairname{font-weight:700;font-size:1.02rem}
.pill{display:inline-block;padding:2px 11px;font-weight:700;font-size:.88rem;border-radius:2px}
.pill.bull{background:var(--bull-bg);color:var(--bull)}
.pill.bear{background:var(--bear-bg);color:var(--bear)}
.pill.hold{background:var(--hold-bg);color:var(--hold)}
.nums{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums;font-size:.85rem;color:var(--sub);margin-top:6px}
.evidence{margin-top:6px;font-size:.85rem;color:var(--ink)}
.evidence b{color:var(--accent)}
details{margin-top:8px}
summary{cursor:pointer;font-size:.82rem;color:var(--sub)}
details ul{margin:6px 0 0;padding-left:18px;font-size:.83rem;color:var(--sub)}
table{width:100%;border-collapse:collapse;font-size:.84rem;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 6px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--sub);font-weight:600;font-size:.76rem}
.scroll{overflow-x:auto}
.bar{height:8px;background:var(--hold-bg);position:relative;margin:3px 0 8px}
.bar i{position:absolute;top:0;bottom:0;background:var(--accent)}
.bar i.neg{background:var(--bear)}
.wlabel{display:flex;justify-content:space-between;font-size:.8rem;color:var(--sub)}
.foot{font-size:.75rem;color:var(--sub);line-height:1.6}
.klabel{font-size:.78rem;color:var(--sub)}
</style>
</head>
<body>
<main id="app"></main>
<script>
window.DATA = __DATA__;
(function(){
const D=window.DATA, app=document.getElementById('app');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=(x,d=1)=>x==null?'—':(100*x).toFixed(d)+'%';
const num=(x,d=2)=>x==null?'—':Number(x).toFixed(d);
let html='';

// ── header + summary ────────────────────────────────────────────────────────
const calls=D.calls||{}, cards=calls.cards||[], fam=(D.verify||{}).family||{};
const nDir=cards.filter(c=>c.side&&c.side!=='STAND_ASIDE').length;
const famBadge=fam.verified?'<span class="badge ok">엔진 검증 ✅</span>'
  :`<span class="badge">검증 축적중 e=${num(fam.e_pooled,2)}/${num(fam.threshold,0)}</span>`;
html+=`<h1>surge 야간 방향 콜<small>${esc(calls.date||'—')} 미국 세션</small></h1>
<div class="summary">
  <span class="badge">${cards.length}페어 판정</span>
  <span class="badge">${nDir}건 방향 콜 · ${cards.length-nDir}건 관망</span>
  ${famBadge}
</div>`;

// ── 금일 예측 (다가오는 세션 — 장 시작 전 콜) ────────────────────────────────
html+=`<section><h2>금일 예측 · ${esc(calls.date||'—')} 세션 (장 시작 전 콜)</h2>`;
if(!cards.length) html+='<div class="klabel">아직 저장된 콜이 없습니다 — 야간 파이프라인 첫 실행 후 채워집니다.</div>';
for(const c of cards){
  const dir=c.side==='STAND_ASIDE'?'hold':(c.side===c.bull?'bull':'bear');
  const label=c.side==='STAND_ASIDE'?'관망':(dir==='bull'?`${esc(c.side)} 상승`:`${esc(c.side)} 하락`);
  const forced=c.forced?' <span class="pill hold" style="font-size:.72rem">필수매수 강제</span>':'';
  let ev='';
  if(c.adaptive_p!=null){
    ev=`<div class="evidence">적응 엔진 P(상승) <b>${pct(c.adaptive_p)}</b>`;
    const e=c.bucket_evidence;
    if(e&&e.replay_acc!=null) ev+=` — 이 확신 구간(${esc(e.bucket)}) 과거 적중률 <b>${pct(e.replay_acc)}</b> (리플레이 n=${e.replay_n.toLocaleString()}${e.forward_n?` · 전진 ${pct(e.forward_acc,0)} n=${e.forward_n}`:''})`;
    ev+='</div>';
  }
  const brackets=(c.entry&&c.stop&&c.target)
    ?`진입≈$${num(c.entry)} · 손절 $${num(c.stop)} · 익절 $${num(c.target)} · 비중 ${pct(c.size_pct!=null?c.size_pct*0.1:null,0)}`
    :'브래킷 없음(관망)';
  let reasons='';
  if((c.reasons||[]).length)
    reasons=`<details><summary>근거 ${c.reasons.length}개</summary><ul>${c.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul></details>`;
  html+=`<div class="card">
    <div class="pairline"><span class="pairname">${esc(c.name)}</span>
      <span class="pill ${dir}">${label}</span>${forced}</div>
    <div class="nums">score ${num(c.score,3)} · 확신 ${num(c.conviction,3)} · 모델 ${esc(c.model||'champion')}</div>
    <div class="nums">${brackets}</div>${ev}${reasons}</div>`;
}
html+='</section>';

// ── 직전 세션 콜 + 실제 결과 ─────────────────────────────────────────────────
const prev=D.previous||{};
if(prev.date){
  const pacc=prev.accuracy!=null?` · 적중 ${pct(prev.accuracy)} (${prev.wins}/${prev.n_scored})`:'';
  html+=`<section><h2>직전 세션 결과 · ${esc(prev.date)}${pacc}</h2>`;
  for(const c of (prev.cards||[])){
    const dir=c.side==='STAND_ASIDE'?'hold':(c.side===c.bull?'bull':'bear');
    const label=c.side==='STAND_ASIDE'?'관망':(dir==='bull'?`${esc(c.side)} 상승`:`${esc(c.side)} 하락`);
    let res='', analysis='';
    if(c.side==='STAND_ASIDE'){
      res=c.oc_ret!=null?`<span class="klabel">관망 (기초 ${pct(c.oc_ret,2)})</span>`:'<span class="klabel">관망</span>';
    }else if(c.correct!=null){
      const ok=c.correct===1;
      const pnl=c.pnl_pct!=null?` · pnl ${pct(c.pnl_pct,2)}`:'';
      res=`<span class="pill ${ok?'bull':'bear'}">${ok?'✅ 적중':'❌ 빗나감'}</span><span class="klabel">${pnl}${c.exit_reason?' · '+esc(c.exit_reason):''}</span>`;
      const a=c.analysis;
      if(a){
        const rt=a.right&&a.right.length?`<span style="color:var(--bull)">맞은 신호: ${a.right.map(esc).join(', ')}</span>`:'';
        const wr=a.wrong&&a.wrong.length?`<span style="color:var(--bear)">틀린 신호: ${a.wrong.map(esc).join(', ')}</span>`:'';
        analysis=`<div class="evidence"><b>왜 ${ok?'맞았나':'틀렸나'}</b> — ${esc(a.verdict)}</div>`+
                 (rt||wr?`<div class="nums">${[rt,wr].filter(Boolean).join(' · ')}</div>`:'');
      }
    }else{
      res='<span class="klabel">채점 대기</span>';
    }
    html+=`<div class="card">
      <div class="pairline"><span class="pairname">${esc(c.name)}</span>
        <span class="pill ${dir}">${label}</span></div>
      <div class="nums">${res}</div>${analysis}</div>`;
  }
  html+='</section>';
}

// ── cumulative record ───────────────────────────────────────────────────────
const t=D.tally||{}, tp=t.pairs||{};
html+=`<section><h2>누적 채점 (전진 기록)</h2><div class="scroll"><table>
<tr><th>페어</th><th>평가</th><th>승</th><th>패</th><th>관망</th><th>적중률</th></tr>`;
for(const [pid,s] of Object.entries(tp))
  html+=`<tr><td>${esc(pid)}</td><td>${s.evaluated}</td><td>${s.wins}</td><td>${s.losses}</td><td>${s.abstains}</td><td>${pct(s.accuracy)}</td></tr>`;
html+=`<tr><td><b>전체</b></td><td><b>${t.evaluated??0}</b></td><td><b>${t.wins??0}</b></td><td><b>${t.losses??0}</b></td><td><b>${t.abstains??0}</b></td><td><b>${pct(t.accuracy)}</b></td></tr>
</table></div></section>`;

// ── verification progress ───────────────────────────────────────────────────
const vp=(D.verify||{}).pairs||[];
html+=`<section><h2>확신 검증 진행 — anytime-valid e-value (임계 ${num(fam.threshold,0)})</h2>
<div class="klabel" style="margin-bottom:8px">패밀리(교차풀링): e=${num(fam.e_pooled,2)} · 불일치 세션 ${fam.n_discordant??0}건 — 페어별 수년 걸릴 검증을 전 페어 합산으로 단축</div>
<div class="scroll"><table><tr><th>페어</th><th>전진n</th><th>프라이어</th><th>e(워밍)</th><th>상태</th></tr>`;
for(const p of vp){
  const st=p.verified?'✅검증':(p.provisional?'🟢잠정':'⏳축적');
  html+=`<tr><td>${esc(p.pair)}</td><td>${p.n_forward}</td><td>${pct(p.prior_rate)}</td><td>${num(p.e_warm,2)}</td><td>${st}</td></tr>`;
}
html+='</table></div></section>';

// ── conviction ledger ───────────────────────────────────────────────────────
const cal=D.calibration||{};
const calPairs=Object.entries(cal).filter(([,rows])=>rows.some(r=>r.replay_n>0));
if(calPairs.length){
  html+='<section><h2>확신 구간 원장 — 주장한 확신 vs 실측 적중률</h2>';
  for(const [pid,rows] of calPairs){
    html+=`<div class="klabel" style="margin:8px 0 4px"><b>${esc(pid)}</b></div>
    <div class="scroll"><table><tr><th>구간</th><th>리플레이 n</th><th>적중률</th><th>전진 n</th><th>적중률</th></tr>`;
    for(const r of rows)
      html+=`<tr><td>${esc(r.bucket)}</td><td>${r.replay_n.toLocaleString()}</td><td>${pct(r.replay_acc)}</td><td>${r.forward_n}</td><td>${pct(r.forward_acc)}</td></tr>`;
    html+='</table></div>';
  }
  html+='</section>';
}

// ── variable weights ────────────────────────────────────────────────────────
const w=D.weights||{};
if(Object.keys(w).length){
  html+='<section><h2>변인 추정 — 학습기가 현재 credit하는 가중치</h2>';
  for(const [pid,info] of Object.entries(w)){
    const entries=Object.entries(info.current).sort((a,b)=>Math.abs(b[1])-Math.abs(a[1])).slice(0,6);
    const mx=Math.max(...entries.map(([,v])=>Math.abs(v)),1e-9);
    html+=`<div class="klabel" style="margin:8px 0 4px"><b>${esc(pid)}</b>${info.sign_flips.length?` · ⚠부호반전: ${info.sign_flips.map(esc).join(', ')}`:''}</div>`;
    for(const [f,v] of entries){
      html+=`<div class="wlabel"><span>${esc(f)}</span><span>${v>=0?'+':''}${num(v,3)}</span></div>
      <div class="bar"><i class="${v<0?'neg':''}" style="left:${v<0?50-Math.abs(v)/mx*50:50}%;width:${Math.abs(v)/mx*50}%"></i></div>`;
    }
  }
  html+='</section>';
}

// ── config race ─────────────────────────────────────────────────────────────
if((D.race||[]).length){
  html+=`<section><h2>설정 레이스 — 전진 채점 누적</h2><div class="scroll"><table>
  <tr><th>변형/설정</th><th>n</th><th>적중률</th></tr>`;
  for(const r of D.race)
    html+=`<tr><td>${esc(r.variant)}</td><td>${r.n}</td><td>${pct(r.acc)}</td></tr>`;
  html+='</table></div></section>';
}

// ── risk-discipline (personalized sizing) ───────────────────────────────────
const dsc=D.discipline||{};
if(dsc.factor!=null){
  const ceil=dsc.equity_ceiling!=null?pct(dsc.equity_ceiling):'—';
  html+=`<section><h2>투자행동 리스크 규율 — 개인화 사이징 감쇠</h2>
  <div class="klabel">현재 사이즈 ×${num(dsc.factor,2)} · 삶-비중 상한 ${ceil} · 출처 ${esc(dsc.source||'self')} · ${esc((dsc.assessed_at||'').slice(0,10))}</div>`;
  if((dsc.trajectory||[]).length>1)
    html+=`<div class="klabel" style="color:var(--sub);margin-top:6px">규율 궤적(성장추적): ${dsc.trajectory.map(t=>num(t.factor,2)).join(' → ')}</div>`;
  html+=`<div class="klabel" style="color:var(--sub);margin-top:6px">※ 사이즈만 줄인다 — 확신·방향 불변</div></section>`;
}

// ── learning log ────────────────────────────────────────────────────────────
const L=D.learning||{};
if(L.run_date){
  html+=`<section><h2>자기개선 일지 (${esc(L.run_date)})</h2>
  <div style="font-size:.9rem">${esc(L.headline||'—')}</div>`;
  if((L.changes||[]).length)
    html+=`<ul style="font-size:.85rem;color:var(--sub);margin:8px 0 0;padding-left:18px">${L.changes.map(c=>`<li>${esc(c)}</li>`).join('')}</ul>`;
  if((L.promote_ready||[]).length)
    html+=`<div class="badge warn" style="margin-top:8px">승격 검토 대기: ${L.promote_ready.map(esc).join(', ')}</div>`;
  html+='</section>';
}

// ── footer ──────────────────────────────────────────────────────────────────
html+=`<div class="foot">생성 ${esc(D.generated_at||'')} (UTC) · 매일 밤 파이프라인이 자동 갱신 ·
모든 콜은 세션 시작 전 박제되고 사후 채점됩니다.<br>
투자자문 아님 · 수익 보장 없음 · 3배 레버리지 상품은 원금 전액 손실이 가능합니다.</div>`;
app.innerHTML=html;
})();
</script>
</body>
</html>
"""
