"""Curated target watchlist (the query names + the rotation chain leaders).

Deliberately SMALL and hand-maintained — the user asked for periodic tracking of
a few targets, NOT market-wide screening. `market`: 'us' (yfinance) or 'kr'
(krx). `horizons`: which lenses apply. `room`: rough long-term 10x optionality
flag (small/optional vs already-large) — qualitative, not a probability.
"""

from __future__ import annotations

# ticker, name, market, themes, horizons, room
TARGETS: list[dict] = [
    # ── US ──────────────────────────────────────────────────────────────────
    {"t": "NVDA", "name": "NVIDIA", "mkt": "us", "theme": "AI",
     "h": ["short", "swing"], "room": "low"},          # already mega-cap
    {"t": "MRVL", "name": "Marvell", "mkt": "us", "theme": "AI 반도체",
     "h": ["short", "swing"], "room": "mid"},
    {"t": "AMAT", "name": "Applied Materials", "mkt": "us", "theme": "반도체 장비",
     "h": ["swing"], "room": "low"},
    {"t": "INTC", "name": "Intel", "mkt": "us", "theme": "반도체 턴어라운드",
     "h": ["swing"], "room": "mid"},
    {"t": "AVGO", "name": "Broadcom", "mkt": "us", "theme": "AI", "h": ["swing"],
     "room": "low"},
    {"t": "CEG", "name": "Constellation Energy", "mkt": "us", "theme": "AI 전력/원전",
     "h": ["swing"], "room": "mid"},
    {"t": "ETN", "name": "Eaton", "mkt": "us", "theme": "전력 인프라", "h": ["swing"],
     "room": "low"},
    {"t": "OKLO", "name": "Oklo", "mkt": "us", "theme": "SMR 원전",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "SMR", "name": "NuScale Power", "mkt": "us", "theme": "SMR 원전(상용 1호)",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "LEU", "name": "Centrus Energy", "mkt": "us", "theme": "농축우라늄/HALEU 연료",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "ASTS", "name": "AST SpaceMobile", "mkt": "us", "theme": "위성통신",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "RKLB", "name": "Rocket Lab", "mkt": "us", "theme": "우주발사체",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "IREN", "name": "IREN", "mkt": "us", "theme": "AI 데이터센터/전력",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "VERA", "name": "Vera Therapeutics", "mkt": "us", "theme": "바이오",
     "h": ["long"], "room": "high"},
    # ── txt 워치 유니버스 (Tier-S / 고옵셔널리티) ─────────────────────────────
    {"t": "NBIS", "name": "Nebius", "mkt": "us", "theme": "AI 클라우드/인프라",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "CRWV", "name": "CoreWeave", "mkt": "us", "theme": "AI 클라우드",
     "h": ["swing", "long"], "room": "high"},
    {"t": "ALAB", "name": "Astera Labs", "mkt": "us", "theme": "AI 커넥티비티",
     "h": ["short", "swing"], "room": "high"},
    {"t": "CRDO", "name": "Credo", "mkt": "us", "theme": "AI 커넥티비티(AEC)",
     "h": ["short", "swing"], "room": "high"},
    {"t": "APLD", "name": "Applied Digital", "mkt": "us", "theme": "AI 데이터센터",
     "h": ["short", "swing", "long"], "room": "high"},
    {"t": "TEM", "name": "Tempus AI", "mkt": "us", "theme": "AI 헬스케어",
     "h": ["swing", "long"], "room": "high"},
    {"t": "CRCL", "name": "Circle", "mkt": "us", "theme": "스테이블코인 인프라",
     "h": ["swing", "long"], "room": "high"},
    {"t": "ABBV", "name": "AbbVie", "mkt": "us", "theme": "제약", "h": ["swing"],
     "room": "low"},
    {"t": "ZS", "name": "Zscaler", "mkt": "us", "theme": "보안 SW", "h": ["swing"],
     "room": "mid"},
    {"t": "SOXL", "name": "Semi 3x Bull", "mkt": "us", "theme": "반도체 3x",
     "h": ["short"], "room": "na"},
    # ── KR ──────────────────────────────────────────────────────────────────
    {"t": "005930", "name": "삼성전자", "mkt": "kr", "theme": "HBM/파운드리",
     "h": ["swing"], "room": "low"},
    {"t": "000660", "name": "SK하이닉스", "mkt": "kr", "theme": "HBM", "h": ["swing"],
     "room": "low"},
    {"t": "042700", "name": "한미반도체", "mkt": "kr", "theme": "HBM 패키징",
     "h": ["short", "swing"], "room": "mid"},
    {"t": "089030", "name": "테크윙", "mkt": "kr", "theme": "HBM 테스트(번인)",
     "h": ["short", "swing"], "room": "high"},
    {"t": "357780", "name": "솔브레인", "mkt": "kr", "theme": "반도체 소재",
     "h": ["swing"], "room": "mid"},
    {"t": "095340", "name": "ISC", "mkt": "kr", "theme": "반도체 테스트소켓",
     "h": ["short", "swing"], "room": "high"},
    # ── 대미투자특별법 수혜 (조선·전력·방산) — 정책태그, 검증은 policy_tagged 팩터 ──
    {"t": "042660", "name": "한화오션", "mkt": "kr", "theme": "조선/美 MRO(특별법)",
     "h": ["swing", "long"], "room": "mid"},
    {"t": "012450", "name": "한화에어로스페이스", "mkt": "kr", "theme": "방산/우주(특별법)",
     "h": ["swing", "long"], "room": "mid"},
    {"t": "010120", "name": "LS ELECTRIC", "mkt": "kr", "theme": "전력인프라(특별법 공통분모)",
     "h": ["swing", "long"], "room": "mid"},
    {"t": "079550", "name": "LIG넥스원", "mkt": "kr", "theme": "방산(특별법)",
     "h": ["swing"], "room": "mid"},
    {"t": "047810", "name": "KAI", "mkt": "kr", "theme": "우주/방산",
     "h": ["short", "swing"], "room": "mid"},
    {"t": "034020", "name": "두산에너빌리티", "mkt": "kr", "theme": "원전/전력",
     "h": ["swing", "long"], "room": "mid"},
    {"t": "086520", "name": "펩트론", "mkt": "kr", "theme": "비만/바이오",
     "h": ["long"], "room": "high"},
    {"t": "140410", "name": "메지온", "mkt": "kr", "theme": "희귀질환", "h": ["long"],
     "room": "high"},
    {"t": "440110", "name": "파두", "mkt": "kr", "theme": "AI SSD 팹리스",
     "h": ["swing", "long"], "room": "high"},
    {"t": "000270", "name": "기아", "mkt": "kr", "theme": "자동차/밸류업",
     "h": ["swing"], "room": "low"},
]

# private / not tradeable — surfaced as N/A in long-term notes
PRIVATE = {"SpaceX": "비상장(직접 투자 불가; ASTS/RKLB로 우주 익스포저 대체)"}


def by_horizon(horizon: str) -> list[dict]:
    return [t for t in TARGETS if horizon in t["h"]]
