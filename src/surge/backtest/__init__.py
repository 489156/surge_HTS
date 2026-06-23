"""Backtesting engine — leak-free, point-in-time strategy replay with realistic
execution (slippage/commission), walk-forward and Monte Carlo validation, and
full risk-adjusted metrics.

Honesty note: only features that can be reconstructed point-in-time from price/
volume are backtestable here. Structural features (float, options, borrow) have
no free historical point-in-time source, so strategies in this package use
price/volume signals only — and a clean-looking backtest still proves nothing
without out-of-sample + the 4 anti-traps (survivorship, look-ahead, liquidity,
manipulation).
"""
