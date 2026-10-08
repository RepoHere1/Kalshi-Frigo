# Kalshi mechanics (verified constants)

- Every fill pays Kalshi's fee: `0.07 x price x (1 - price)` per contract
  (taker), maker about a quarter of that (`0.0175`), rounded up. An edge that
  has not cleared this fee is not an edge.
- Contracts settle at $1.00 or $0.00. Prices outside 1-99 cents are invalid
  data, never a trade.
- Collection/roll tickers that quote $1.00/$1.00 are not tradeable; skip them.
- A trade is only a win when (exit - entry) x count beats fees on BOTH fills.
