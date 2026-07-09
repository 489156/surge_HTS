# surge — US next-day surge (+100%) prediction archive

> 목표: 전일 종가 대비 **+100% 급등주**를 *전날에* 후보로 좁힌다.
> 핵심은 모델이 아니라 **사후 복원 불가능한 피처(즉시포착)를 오늘부터 매일 박제**하는 것.

정확도에 대한 현실적 재정의: "내일 터질 종목을 맞힌다"(거의 불가능)가 아니라
**"기저율 0.1%인 후보를 2-stage 깔때기로 2~5%까지 끌어올린다"**.

## 설계 요약

- **2단계 깔때기**: Stage-1(저가·소형·유동성 정적 필터)로 universe를 수백 개로 줄이고,
  비싼 구조/옵션 API 호출은 Stage-2 shortlist(이미 움직였거나 예열 셋업)에만 사용.
- **4개 피처군**: (A) 정적 필터 / (B) 동적 모멘텀·미시구조 / (C) 음의 트랩 필터 /
  (D) 즉시포착 박제(float·옵션·IV; 차입비용·소셜은 자리만 확보).
- **생존자 편향 방지**: securities 행을 절대 삭제하지 않음(delisted 플래그).
- **point-in-time**: 모든 값은 snapshot_date와 함께 저장, 덮어쓰지 않음.

자세한 내용: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md), [src/surge/schema.sql](src/surge/schema.sql)

## 설치 & 실행

```bash
uv sync
uv run surge init                 # DB 생성
uv run surge universe             # 미국 종목 마스터 적재 (NASDAQ Trader, 무료)
uv run surge snapshot --fast      # 일별 박제 (Stage-1 가격 사전필터로 경량화)
uv run surge watchlist --why      # 오늘의 급등 후보 랭킹 + 점수 근거(점화 예측)
uv run surge reversals --why      # 페이드 워치리스트: 급등 후 익일 되돌림 후보(랭킹 미검증)
uv run surge fade                 # 전일 급등의 익일 지속/소멸 라벨링(페이드 모델)
uv run surge backfill-outcomes    # 과거 후보의 실제 익일 결과 기록
uv run surge eval --k 10          # 예측 가능성: Precision@K·적중률·기저율 대비 lift
uv run surge surges               # 적재된 급등 이벤트 조회
uv run surge stats                # 테이블별 행 수
# 빠른 테스트 / 과거 재현:
uv run surge snapshot --symbols AAPL,GME,KOSS
uv run surge snapshot --limit 200 --fast            # universe를 N개로 제한
uv run surge snapshot --asof 2026-06-04 --limit 1200 --fast  # 룩어헤드 없는 시점 재현
```

매일 한 번 `surge snapshot`을 돌리는 것이 데이터 해자의 전부다 — 빠질수록 손해.

**자동화 — GitHub Actions 단일 계층 (2026-07 확정)**:
- `.github/workflows/daily-pipeline.yml`이 유일한 자동화이자 **`data/surge.db`의 유일한
  기록자**다. 평일 UTC 13:30(미국 개장 전 콜 생성)·00:00(마감 후 채점+자기개선) 2회,
  PC 전원과 무관하게 실행되고 결과를 리포에 자동 커밋한다. 토큰 소비 0.
- **구(舊) 로컬 이중 계층은 폐기됨**: Windows 작업 스케줄러 잡과 앱 내 Claude 작업은
  Actions와 동일 DB에 이중 기록해 바이너리 충돌을 일으키므로 반드시 해제한다 —
  `Unregister-ScheduledTask -TaskName surge-daily-morning,surge-daily-evening`
  + 앱 내 `daily-surge-snapshot`/`nightly-duel-call` 삭제. `scripts/*.ps1`은
  장기 Actions 장애 시의 수동 폴백 문서로만 남긴다(상시 등록 금지).
- 로컬은 **읽기 전용 조회**만: `git pull` 후 `surge report`/`surge verify`/`surge adaptive`.

**토큰 정책(자기개선 엔진)**: 야간 루프는 영구 결정론·제로 토큰(재현성이 곧 감사
가능성). LLM 토큰은 **원장이 신호를 줄 때의 연구 세션에만** 쓴다 — learning_log의
`promote_ready`/`verify` ✅/자동발굴 가설(🔬) 누적이 계기이며, 그 외 정기 소비는 없다.

**안정성**: 스냅샷은 **배치별 증분 커밋**이라 중간에 throttle/네트워크 오류가 나도 그때까지
모은 데이터는 보존된다. 한 배치 실패가 전체 런을 죽이지 않는다(`batches_failed` 카운트).
`--fast`는 직전 스냅샷 종가가 `max_price × 3`(기본 $60) 초과인 종목을 건너뛴다.

## 후보 점수 (투명 룰기반)

`surge watchlist`는 Stage-2 shortlist를 구조적 사전조건으로 점수화해 랭킹한다.
모든 점수에 **자연어 근거**가 붙는다(블랙박스 아님):

- **+가점**: 저유동 float, 고공매도, float 완전회전, RVOL 급증, 콜 편중,
  모멘텀/갭/연속상승/강한마감, 최근 리버스 스플릿(저유동 셋업)
- **−감점(트랩)**: 발행 임박(SEC S-1/S-3/424B), 과열 소진, 유동성 부족

데이터 소스: float·옵션·공매도(yfinance), 리버스 스플릿·실적일(yfinance),
발행 파일링·촉매(SEC EDGAR, 무료).

## 페이드(되돌림) 예측 — 관찰된 경향, 스코어러 랭킹은 미검증

2026-06-05~06 검증에서 두 가지가 갈렸다:
- **관찰됨(broad fade)**: 점화 예측은 실패(0/24 +100%)했고, 후보(이미 상승한 저유동주)는
  **익일 평균 −5.7%로 광범위하게 되돌림**했다. "상승한 저유동주는 익일에 평균적으로 빠진다"는
  방향성은 일관됐다.
- **미검증(scorer ranking)**: `reversion.reversion_score`의 *랭킹*은 아직 우위를 못 보였다 —
  이 표본에서 고점수(블로우오프) 종목이 오히려 *덜* 빠졌다(고점수 −1.4% vs 저점수 −4.1%,
  n=2 vs 14, 통계적 무의미). 즉 페이드는 **가장 큰 블로우오프에 집중되기보다 광범위**했다.

`surge reversals`는 이 가설을 **계속 측정하기 위한** 도구다(블랙박스 아님, 근거 공개). 표본이
수십 일로 쌓이기 전까지 "검증됨" 라벨은 붙이지 않는다 — 이는 프로젝트의 과최적화 회피 원칙과
일관된다. `scoring.setup_score`(점화)의 거울로 `reversion_score`(되돌림)가 짝을 이룬다.

## AI 자동매매 플랫폼 (paper 기본 · live는 사람 승인 게이트)

surge 시그널을 첫 전략으로 꽂는 **"계좌가 살아남는 컨테이너"**. 멀티에이전트
(news·technical·fundamental·macro·risk) → 토론(bull/bear/judge, risk 거부권) →
포트폴리오 매니저 → **리스크 엔진**(사이징·손실한도·거부) → 실행(paper 체결 / live 승인대기)
→ 전 단계 감사로그. 자세한 설계: [docs/TRADING.md](docs/TRADING.md).

```bash
uv run surge trade --top 8       # 의사결정 1사이클(paper)
uv run surge portfolio           # 포지션·자본·드로다운·상태
uv run surge approvals           # live 주문 승인 큐(사람이 직접 제출)
uv run surge killswitch --reason "manual"   # 전량청산+중단
uv run surge backtest --strategy momentum --montecarlo --walkforward --crash
uv run surge dashboard           # HTS 웹 대시보드 → http://127.0.0.1:8000
```

## Duel — 매일 밤 SOXL vs SOXS (`src/surge/duel/`)

"오늘 밤 어느 쪽에 베팅하나"를 아시아 반도체 선행(TSMC·삼성·하이닉스·TEL) + 추세·
모멘텀·VIX·금리(+라이브 NQ선물)의 투명 가중 투표로 판정. **관망(STAND_ASIDE)이
정식 출력**이며, ATR 브래킷·종가 청산(오버나이트 금지)·콜 저장→사후 채점 루프 포함.

```bash
uv run surge duel              # 오늘 밤 판정 (저장됨)
uv run surge duel-backtest --period 2y
uv run surge duel-eval         # 누적 적중률 채점
uv run surge quotes --health   # SOXL/SOXS 시세 + 3중 failover 공급자 상태
```

**시세 이중화**: yfinance → Finnhub(키 설정 시, 진짜 벤더 이중화) → Yahoo 직접
호출(라이브러리 파손 대비). 단일 장애점 제거. Prometheus `surge_quote` 게이지 노출.

2년 백테스트 정직 보고: 적중 54.3%(통계적 미유의), 실행가능 PnL −3.4% — 아시아
신호는 시초가 갭에 선반영됨. 상세: [docs/DUEL.md](docs/DUEL.md).

**백테스트**(`src/surge/backtest/`): 룩어헤드 없는 이벤트 리플레이(시그널 D일 종가 →
D+1 시가 진입), 슬리피지·수수료, Sharpe/Sortino/Calmar/MDD/승률/PF, 몬테카를로·
워크포워드·크래시 스트레스. **HTS 대시보드**(`src/surge/dashboard/`): FastAPI +
단일 페이지 터미널(워치리스트·포지션·PnL·에이전트 의견·리스크·킬스위치·감사로그).
Docker 배포: `docker compose up`([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

**안전 게이트(코드로 강제)**: live 주문은 자동 제출 불가 — `AlpacaLiveBroker.place_order`는
`LiveBrokerGateError`를 던지고, 오케스트레이터는 *승인 대기*로만 적재한다. 실제 제출은
`approvals --approve`(사람 행동)로만 가능. **AI가 틀려도, 무인 자동화가 돌아도 계좌가 산다.**

## 다음 단계 (미구현, 자리 확보됨)
- 차입비용·utilization 무료 소스 확보 시 `daily_snapshot.borrow_fee` 활성화 (최고 선행성)
- 소셜 멘션 가속도·breadth(Reddit/StockTwits)
- FDA/PDUFA 촉매 캘린더(현재 촉매: 실적·리버스스플릿·발행)
- 학습/백테스트: 생존자편향·룩어헤드·유동성·조작 4대 함정 제거 후 DSR·PBO
- 페이드 모델: `surge_events.sustained` 라벨이 충분히 쌓이면 익일 소멸 예측 학습
