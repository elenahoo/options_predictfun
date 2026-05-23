# Deribit-Derived Terminal Probability

This scanner compares Predict.fun daily up/down markets with a Deribit-implied
terminal probability at the same event baseline strike.

Predict.fun daily markets resolve on the terminal price at the event deadline:

```text
Up   = S_T >= K
Down = S_T <  K
```

where `K` is the baseline price embedded in the Predict.fun event. Because this is
not a touch/barrier payoff, the live scanner does not use the Monte Carlo touch
probability as the target. It uses the terminal risk-neutral distribution
implied by Deribit options.

## Live Method

1. Fetch the active Predict.fun daily up/down markets for BTC and ETH.
2. Read the event baseline from `strikePrice`; this is the comparison strike.
3. Require Deribit to have an option expiry on the same calendar date as the
   Predict.fun daily event.
4. Fetch Deribit option tickers around the target maturity and fit SVI smiles.
5. Build an implied-volatility surface while preserving Deribit's actual option
   expiry timestamp, normally 08:00 UTC.
6. Price calls from the fitted surface and derive the terminal digital:

```text
P(S_T >= K) = -exp(rT) * dC(K,T) / dK
```

7. Compare:

```text
Predict.fun Up   vs Deribit P(S_T >= K)
Predict.fun Down vs 1 - Deribit P(S_T >= K)
```

The live code stores the target as
`model_method = "deribit_terminal_digital_from_call_slope"`.

## Why This Is Apple To Apple

Deribit listed options are European terminal payoffs. A call surface gives the
risk-neutral terminal distribution through the Breeden-Litzenberger
relationship. Predict.fun daily up/down events are also terminal events at a
baseline strike. Comparing terminal probability to terminal probability avoids
the previous mismatch where a path-dependent touch probability could be much
higher than the event's true settlement probability.

## Notes

- `Up` and `Down` are complements for a binary Predict.fun market, so the scanner
  can detect both sides from the up spread:
  `predictfun_up - deribit_up`.
- If the up side is cheap, the trader buys the Predict.fun YES token and places a
  GTC sell at the Deribit up target.
- If the up side is rich, the down side is cheap; the trader buys the Predict.fun
  NO token and places a GTC sell at `1 - deribit_up`.
- The local-volatility and Monte Carlo code remains in
  `old_scripts/x_price_option_all_expiries.py` for diagnostics and historical
  sweeps, but it is not the live target methodology for daily up/down trading.
