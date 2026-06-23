"""Institutional-grade trading platform (extends surge).

Paper-trading is the default and is fully automated. Live mode wires a real
broker but routes every order to a human-approval queue — the autonomous
system never submits real-money orders unattended. Risk preservation > profit.
"""
