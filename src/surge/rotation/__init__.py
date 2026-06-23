"""Rotation — Korean attention / value-chain rotation screener.

Implements the AMVF thesis the user has been refining ("don't buy the hottest
stock; buy the value-chain node interest is rotating INTO") on the surge
honest-forward scaffolding: reconstructable, mostly-keyless inputs (smart-money
net flows via Naver, RVOL/momentum via OHLCV, value-chain position from a static
graph) → transparent weighted score → cross-sectional percentile Failure Filter
→ predictions stored BEFORE the session and scored at T+1/T+3/T+5 AFTER it.

What this module deliberately does NOT do: trust the framework's headline
"6/7 hit" (in-sample, survivorship-selected) or pay for the fragile/arbitraged
attention layers (news/SNS). Those join only as shadow signals once the forward
record proves they add value — same discipline as duel's variant A/B.
"""
