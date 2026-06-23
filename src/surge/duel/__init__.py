"""Duel — the nightly bull-vs-bear direction engine for leveraged/inverse pairs.

One question per pair per US session (registry in pairs.py: SOXL/SOXS,
TQQQ/SQQQ, TECL/TECS, LABU/LABD): is the underlying index more likely to rise
(long the bull leg) or fall (long the bear leg) tonight — or is the honest
answer "don't play" (STAND_ASIDE)?

Edge thesis (structural, not curve-fit): Asian semiconductor leaders (TSMC,
Samsung, SK Hynix, Tokyo Electron) finish their trading day BEFORE the US
session opens. Their session is real, settled information about the industry
that the US open has not yet priced continuously. We combine that lead with
prior-day US momentum/trend, volatility regime, and rates into a transparent
weighted vote.

Leak-safety is structural: every US-market feature is shifted to D−1; only the
Asian same-day close (final hours before the US open) uses date D. The backtest
replays the exact production rule. Every live call is persisted before the
session and scored after it — running accuracy is always inspectable.

Both legs are LONG positions (SOXL = 3x bull, SOXS = 3x bear), bracketed with
ATR stop/target and closed by end of session — no overnight 3x exposure, which
also sidesteps leveraged-ETF compounding decay.

No guarantee exists; a 3x product in a hostile regime is gambling, which is why
crisis-VIX forces abstention and the abstain band is a first-class output.
"""
