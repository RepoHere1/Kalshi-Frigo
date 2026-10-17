# ALL NO MARKETS Strategy Documentation

## Overview

The ALL NO MARKETS strategy is a specialized trading strategy that operates as a completely **separate book** from the main DRY ($300) and LIVE (real money) trading books. It is exclusively designed for **NO-side trading** with maker entries and uses a dedicated $250 starting balance.

## Key Features

### 1. **Separate Book Architecture**
- **Independent Book**: Dedicated $250 starting balance (not connected to DRY's $300 or LIVE accounts)
- **One-Shot Seeding**: The seed is written once and remembered, so redeploys never re-fund a book that has already traded
- **Complete Separation**: No shared tables, tallies, or balances between ALL NO and regular lanes

### 2. **Trading Strategy**
- **NO-Side Only**: Exclusively sells NO contracts (down contracts)
- **Maker Entries Only**: Places limit orders at bid prices (resting on the spread)
- **Full-Kelly Sizing**: Uses full Kelly criterion with a 1.0 multiplier
- **80% Max Clip**: Maximum single position limited to 80% of the ALL NO book

### 3. **Dashboard Integration**
- **Separate Page**: `/all-no-markets` route with dedicated HTML page (`static/all_no_markets.html`)
- **White Background, Black Cards**: Distinct visual style from other lanes
- **Read-Only by Default**: Dashboard shows positions but doesn't execute orders
- **Mode Switch**: Includes DRY/LIVE switch in the same control group as the main dashboard

### 4. **Book Management**
- **Schema**: Dedicated tables (`all_no_book`, `all_no_positions`, `all_no_trade_logs`)
- **Balance Tracking**: Real-time balance calculation including deposits, withdrawals, P&L
- **No Drift**: Balance derived from book entries, preventing counter drift issues

## Configuration

### Environment Variables
```bash
ALL_NO_ENABLED=true           # Enable the ALL NO MARKETS strategy (default: false)
ALL_NO_STARTING_BALANCE=250.0  # Starting balance (default: 250.0, can be overridden)
ALL_NO_KELLY_MULTIPLIER=1.0   # Kelly multiplier (default: 1.0 = full Kelly)
ALL_NO_MAX_CLIP_PCT=0.80       # Maximum clip percentage (default: 0.80)
```

### Settings File
The ALL NO MARKETS settings are included in `src/config/settings.py`:
```python
all_no_enabled: bool = True             # Enable the ALL NO MARKETS strategy
all_no_starting_balance: float = 250.0  # ALL NO starting balance
all_no_kelly_multiplier: float = 1.0    # Full Kelly for ALL NO book
all_no_max_clip_pct: float = 0.80       # Maximum clip size for ALL NO book
```

## How to Use

### Manual Execution
```bash
# Start the ALL NO MARKETS strategy
python cli.py run --all-no-markets

# Start with looping enabled (default: true)
python cli.py run --all-no-markets --loop

# Set custom interval between cycles
python cli.py run --all-no-markets --loop --interval 60
```

### Through the Scripts
```bash
# Turn on all DRY lanes including ALL NO MARKETS
python scripts/turn_on_dry_lanes.py

# Run a dry-run (no changes, just report)
python scripts/turn_on_dry_lanes.py --dry-run

# Arm lanes without spawning (resume on next dashboard boot)
python scripts/turn_on_dry_lanes.py --no-spawn
```

### Dashboard Integration
1. **Start the main dashboard**: `python cli.py dashboard`
2. **Navigate to `/all-no-markets`** to see the ALL NO MARKETS book
3. **Control the strategy** from the dashboard if dashboard-managed lanes are enabled

## Book Details

### Strategy Characteristics
- **Trading Side**: NO (sell down contracts only)
- **Order Type**: Maker (limit orders at bid prices)
- **Risk Model**: Full Kelly with 80% hard cap
- **Position Sizing**: Fractional Kelly based on edge calculation
- **Fees**: Fee-aware with NO-side specific cost structure

### Kelly Sizing Formula
For a NO contract bought at price P with a model fair of F:

```
# Net odds for a NO contract bought at `price`
b = (1.0 - price) / price

# Kelly fraction with ALL NO multiplier
f = kelly_fraction(F, b) * ALL_NO_KELLY_MULTIPLIER

# Clip by maximum clip percentage
f = max(0.0, min(f, ALL_NO_MAX_CLIP_PCT))
```

### Sample Calculations
- **fair 0.90, price 0.88**: Kelly fraction = 0.166667 (16.67% of book)
- **fair == price**: No edge, no bet (Kelly = 0)
- **fair < price**: Losing edge, no bet (Kelly = 0)
- **untradeable prices**: Returns 0.0 instead of raising

## Testing

Run the ALL NO MARKETS tests:
```bash
pytest tests/test_all_no_markets.py -v
```

The tests cover:
- Kelly sizing logic
- Book balance tracking
- Separation from regular lanes