"""Backtest strategies — price/volume only (point-in-time reconstructable).

A strategy receives, for one decision date, each symbol's history UP TO AND
INCLUDING that date (no look-ahead) and returns ranked entry candidates
(symbol, score). The engine executes entries at the NEXT day's open, so the
signal never trades on a bar it used to compute itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


def _rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 1:
        return 50.0
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


class Strategy(ABC):
    name: str = "strategy"

    @abstractmethod
    def generate(self, history: dict[str, pd.DataFrame]) -> list[tuple[str, float]]:
        """history[symbol] is OHLCV up to the decision date (inclusive)."""


class MomentumBreakout(Strategy):
    """Buy breakouts: today's close is the highest of the last `lookback` days,
    confirmed by above-average volume. Classic trend-following."""
    name = "momentum"

    def __init__(self, lookback: int = 20, vol_mult: float = 1.5):
        self.lookback = lookback
        self.vol_mult = vol_mult

    def generate(self, history: dict[str, pd.DataFrame]) -> list[tuple[str, float]]:
        out = []
        for sym, df in history.items():
            if len(df) < self.lookback + 1:
                continue
            window = df.tail(self.lookback + 1)
            close = float(window["close"].iloc[-1])
            prior_high = float(window["high"].iloc[:-1].max())
            avg_vol = float(window["volume"].iloc[:-1].mean())
            vol = float(window["volume"].iloc[-1])
            if close >= prior_high and avg_vol > 0 and vol >= self.vol_mult * avg_vol:
                strength = (vol / avg_vol) * (close / prior_high)
                out.append((sym, round(strength, 3)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out


class MeanReversion(Strategy):
    """Buy oversold dips inside an uptrend: price below the lower band and RSI
    low while the longer trend is up. Bets on a bounce."""
    name = "reversion"

    def __init__(self, sma: int = 20, rsi_buy: float = 30.0, trend: int = 50):
        self.sma = sma
        self.rsi_buy = rsi_buy
        self.trend = trend

    def generate(self, history: dict[str, pd.DataFrame]) -> list[tuple[str, float]]:
        out = []
        for sym, df in history.items():
            if len(df) < self.trend + 1:
                continue
            close = df["close"]
            last = float(close.iloc[-1])
            sma = float(close.tail(self.sma).mean())
            trend_sma = float(close.tail(self.trend).mean())
            std = float(close.tail(self.sma).std(ddof=0)) or 1e-9
            lower = sma - 2 * std
            rsi = _rsi(close)
            if last <= lower and rsi <= self.rsi_buy and last > trend_sma * 0.9:
                # more oversold (lower RSI, deeper below band) → higher score
                depth = (lower - last) / std
                score = (self.rsi_buy - rsi) + max(0.0, depth) * 10
                out.append((sym, round(float(score), 3)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out


STRATEGIES = {
    MomentumBreakout.name: MomentumBreakout,
    MeanReversion.name: MeanReversion,
}
