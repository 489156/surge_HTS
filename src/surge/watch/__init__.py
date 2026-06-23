"""Watch — curated multi-horizon tracking layer.

ADDITIVE and READ-ONLY w.r.t. the prediction/learning loops: it reads the
existing data adapters (US market + KR krx) and computes, for a *small curated
target list*, horizon-appropriate levels and a long-term optionality dossier. It
does NOT touch, feed, or alter duel/rotation engines, their forward eval, or the
shadow-variant A/B — so prediction/learning quality is unchanged.

Three lenses:
- short  (≤1 week)   : ATR bracket + accumulation zone (mechanical levels).
- swing  (1wk–3mo)   : MA-regime + multi-week channel + wider ATR target.
- long   (multibagger): structural optionality dossier + issue tracking. NO
  probability is assigned — that would be uncalibrated fiction; this is a
  research/monitoring aid, refreshed periodically.

Everything is information/tooling, not advice; levels are reproducible rule
outputs, not recommendations.
"""
