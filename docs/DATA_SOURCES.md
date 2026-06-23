# Step 1 — 데이터 소스 확정 (무료 MVP 우선)

목표: **전일 종가 대비 +100% 급등주의 *전일* 상태를 매일 박제**한다. 핵심은 정확도보다
"사후 복원이 불가능한 값(즉시포착)을 오늘부터 누적"하는 것.

## 확보 난이도 × 우선순위 매트릭스

| 피처군 | 대표 신호 | 무료 확보 | 소스(MVP) | 복원 가능? | 비고 |
|---|---|---|---|---|---|
| 종목 마스터 | 티커/거래소/시총 | ✅ | NASDAQ Trader symbol directory | 가능 | 정적 |
| OHLCV·갭 | 시/고/저/종·거래량 | ✅ | yfinance | 가능(과거 조회) | 워크호스 |
| 유동·구조 | float·발행주식·기관보유 | ⚠️ | yfinance `.info` | **현재값만** | 과거 float 복원 난망 → 박제 필요 |
| 공매도 | short % of float·DTC | ⚠️ | yfinance `.info`(지연) | 부분 | 정밀치는 유료(Ortex) |
| **차입비용** | borrow fee·utilization | ❌ | (유료: IBKR/Ortex) | **불가** | 최고 선행성·MVP 제외, 자리만 확보 |
| **옵션 플로우** | 콜/풋·IV·스윕 | ✅(요약) | yfinance `.option_chain` | **불가** | 스냅샷 필수 |
| 소셜 | 멘션 가속·breadth | ⚠️ | (후속: Reddit/StockTwits API) | **불가** | Phase 2 |
| 촉매 캘린더 | 실적·FDA 등 | ⚠️ | yfinance 실적일 / 후속 FDA | 가능(미래일) | 선제 적재 |

## 시세(quote) 이중화 — SOXL/SOXS 추적 (2026-06-11)

`sources/quotes.py`의 3중 failover 체인. 모든 소비자(trading·duel·dashboard)가
`brokers.default_last_price`(60초 캐시)를 통해 이 체인을 탄다. `surge quotes --health`.

| 순위 | 공급자 | 키 | 성격 | 검증 |
|---|---|---|---|---|
| 1 | yfinance | 불필요 | 주 소스 | ✅ 라이브 |
| 2 | Finnhub REST | `SURGE_FINNHUB_API_KEY`(무료 60콜/분) | **진짜 벤더 이중화** | 키 등록 시 활성 |
| 3 | Yahoo chart API 직접(httpx) | 불필요 | 클라이언트 경로 이중화(yfinance 라이브러리 파손 대비; 인프라는 동일 Yahoo) | ✅ 라이브 |
| — | Stooq | 불필요 | **평가 후 탈락** — 이 망에서 전 엔드포인트 404 | ❌ |

SOXL/SOXS는 Prometheus `surge_quote{symbol=…}` 게이지로도 노출(Grafana 차트 가능).
MCP 레지스트리에는 주식/금융 커넥터 없음(검색 0건).

## MVP 결정

- **1차 소스 = yfinance** (API 키 불필요 → *오늘 바로 실행*). float/short/options/실적일을 한 번에 커버.
- **2차 소스(선택) = Finnhub** 무료 60콜/분. `FINNHUB_API_KEY` 설정 시에만 활성.
- **유료 자리만 확보**: 차입비용(borrow), Ortex 정밀 공매도, 소셜 API — 스키마에 컬럼은 두되 NULL 허용.

## 즉시포착(매일 안 모으면 영원히 손실) — 최우선

1. 옵션 IV·콜/풋 거래량 (`.option_chain`)
2. float·기관보유·공매도(지연이라도) (`.info`)
3. 애프터/프리마켓 갭은 일별 OHLCV의 다음날 시가로 근사
4. (후속) 차입비용·소셜 가속

> 차입비용/소셜은 무료로 과거 복원이 사실상 불가하므로, 무료 확보 경로가 생기는 즉시
> 박제 대상에 추가한다. 그전까지는 컬럼을 NULL로 유지(스키마 호환).
