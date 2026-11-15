"""Washout-recovery scale-in bot for make.fun btc-race-24h-20261115-0828.

Strategy (fresh design, written from the kit's RULES.txt and the frozen dev
window; no code copied from the previous race entry):

  A 24h cbBTC window after a -4% day is dominated by one or two sharp
  washouts followed by a multi-hour recovery. The bot buys the washout in
  three equal tranches and rides the recovery, instead of trying to time
  the trend with fast moving-average crossovers (all kit baselines lose
  on this window).

  Entry (flat only): the 30-minute return is below -80 bps AND the 5-minute
  return has turned positive above +30 bps -- the dip is deep and the
  bounce has just started.
  Scale-in: a later tranche is added only when the same signal fires again
  AND the price is at least 50 bps below the last tranche's entry, so the
  bot averages down into a deepening washout instead of chasing.
  Exits (whole position): trailing stop 150 bps below the peak close since
  the first tranche, hard stop 150 bps below the average entry, or a
  120-bar (2 hour) time stop from the first tranche -- recoveries exhaust
  in ~2 hours, holding longer only gives gains back in the drift down.

  Orders: buy = one third of the $1,000 starting cash per tranche
  ($333.33), sell = full position. Long only, no shorts, no leverage.
  Deterministic: decisions use only the sequence of closes seen so far;
  no randomness, no wall-clock, no network, stdlib only.

  Fill reconciliation: a buy/sell decided on bar k fills at bar k+1's
  open. The bot records the decision when it sends the order and confirms
  it against the account in the next batch (qty moved = filled,
  qty unchanged = rejected, e.g. a missing bar); a rejected buy's tranche
  is removed, a rejected sell is retried. This keeps the bot's internal
  state in sync with the account even if a fill is rejected.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# Strategy parameters (tuned on the frozen dev window; validated on the
# previous race's dev window as out-of-sample -- see backtest.py/search.py).
DROP_BPS = 80      # 30-min return must be below -80 bps to buy
REV_BPS = 30       # 5-min return must be above +30 bps to buy
STEP_BPS = 50      # next tranche only 50+ bps below the last entry
N_TRANCHES = 3     # at most 3 tranches, $333.33 each
TRAIL_BPS = 150    # trailing stop below the peak close since entry
HARD_BPS = 150     # hard stop below the average entry price
TIME_STOP = 120    # bars from the first tranche, then exit
TRANCHE = 1_000_000_000 // N_TRANCHES  # $333.33 in micro-USDC
MAX_SLIP_BPS = 100


class Bot:
    def __init__(self):
        self.closes = []      # closes of bars seen, in order (price units)
        self.tranches = []    # decision closes of sent buys in this position
        self.first_k = 0      # index in self.closes of the first tranche
        self.pending = None   # ("buy",) or ("sell",) sent last batch, unconfirmed
        self.last_qty = 0     # qty seen at the previous on_batch

    def _qty(self, state):
        holding = state.positions.get(SYMBOL)
        return holding.qty if holding is not None else 0

    def _buy(self, state):
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": TRANCHE,
                 "max_slippage_bps": MAX_SLIP_BPS}]

    def _sell(self, state):
        qty = self._qty(state)
        return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                 "max_slippage_bps": MAX_SLIP_BPS}]

    def on_batch(self, state, batch):
        # 1. Reconcile the order sent on the previous batch against the
        #    account in this batch. A fill moves qty; a rejection leaves
        #    it unchanged.
        qty = self._qty(state)
        if self.pending == "buy":
            if qty > self.last_qty:
                pass  # filled: keep the tranche recorded at decision time
            else:
                if self.tranches:  # rejected: drop the unfilled tranche
                    self.tranches.pop()
                if not self.tranches:
                    self.first_k = len(self.closes)
        elif self.pending == "sell":
            if qty == 0:
                self.tranches = []  # filled: position closed
            # else rejected: keep tranches, the exit logic below retries
        self.pending = None

        # 2. Record this batch's close, if the symbol has a bar.
        for bar in batch.get("bars", []):
            if bar.get("symbol") == SYMBOL:
                self.closes.append(int(bar["c"]))
        self.last_qty = qty

        # 3. Decide on the latest close.
        if state.warming_up:
            return []
        k = len(self.closes) - 1
        if k < 30:
            return []
        closes = self.closes
        r30 = closes[k] / closes[k - 30] - 1
        r5 = closes[k] / closes[k - 5] - 1

        if qty == 0 and not self.tranches:
            # Flat: enter only on a deep 30-min dip with the bounce started.
            if r30 < -DROP_BPS / 10000 and r5 > REV_BPS / 10000:
                if state.cash < TRANCHE:
                    return []
                self.tranches = [closes[k]]
                self.first_k = k
                self.pending = "buy"
                return self._buy(state)
            return []

        if not self.tranches:
            return []

        # Holding (or a buy is pending): exits, then scale-in.
        avg = sum(self.tranches) / len(self.tranches)
        peak = max(closes[self.first_k:k + 1])
        if closes[k] <= peak * (1 - TRAIL_BPS / 10000):
            self.pending = "sell"
            return self._sell(state)
        if closes[k] <= avg * (1 - HARD_BPS / 10000):
            self.pending = "sell"
            return self._sell(state)
        if k - self.first_k >= TIME_STOP:
            self.pending = "sell"
            return self._sell(state)
        if len(self.tranches) < N_TRANCHES:
            last = self.tranches[-1]
            if (r30 < -DROP_BPS / 10000 and r5 > REV_BPS / 10000
                    and closes[k] <= last * (1 - STEP_BPS / 10000)):
                if state.cash < TRANCHE:
                    return []
                self.tranches.append(closes[k])
                self.pending = "buy"
                return self._buy(state)
        return []


if __name__ == "__main__":
    makefun_bars.run(Bot().on_batch)
