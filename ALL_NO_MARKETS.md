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
- **Quarter-Kelly Sizing**: Uses fractional Kelly criterion with a 25% multiplier
- **20% Max Clip**: Maximum single position limited to 20% of the ALL NO book

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
ALL_NO_KELLY_MULTIPLIER=0.25   # Kelly multiplier (default: 0.25)
ALL_NO_MAX_CLIP_PCT=0.20       # Maximum clip percentage (default: 0.20)
```

### Settings File
The ALL NO MARKETS settings are included in `src/config/settings.py`:
```python
all_no_enabled: bool = True             # Enable the ALL NO MARKETS strategy
all_no_starting_balance: float = 250.0  # ALL NO starting balance
all_no_kelly_multiplier: float = 0.25  # Quarter-Kelly for ALL NO book
all_no_max_clip_pct: float = 0.20       # Maximum clip size for ALL NO book
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
- **Risk Model**: Quarter-Kelly with 20% hard cap
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
- **fair 0.90, price 0.88**: Kelly fraction = 0.041667 (4.17% of book)
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
- Page rendering and functionality

## Integration Notes

### Database Schema

The ALL NO MARKETS strategy uses its own database tables:
- `all_no_book`: Book balance tracking
- `all_no_positions`: Open positions
- `all_no_trade_logs`: Closed trades

These are completely separate from the main `positions` and `trade_logs` tables used by other strategies.

### Trading Process

1. **Book Initialization**: On first run, seeds $250 starting balance
2. **Market Scanning**: Evaluates markets for NO-side trading opportunities
3. **Kelly Sizing**: Calculates position size using quarter-Kelly with 20% cap
4. **Order Placement**: Submits limit orders at bid prices (maker entries)
5. **Position Tracking**: Records positions in `all_no_positions` table
6. **P&L Calculation**: Tracks realized P&L in `all_no_trade_logs` table

### Safety Features

1. **Separate Balances**: Prevents accidental fund mixing between books
2. **Read-Only Dashboard**: Prevents accidental order modification from dashboard
3. **Hard Caps**: 20% maximum clip prevents overexposure
4. **Quarter-Kelly**: Conservative sizing reduces risk
5. **Independent Lifecycle**: Can be started/stopped independently

## Migration

### From Initial Version

The ALL NO MARKETS strategy was added in this version. Existing deployments will need to:

1. Add `all_no_markets` to the `LANES` dictionary in `scripts/turn_on_dry_lanes.py`
2. Set the `ALL_NO_ENABLED` environment variable if auto-starting
3. Review the new dashboard page and strategy documentation

### Best Practices

1. **Separate Monitoring**: Monitor ALL NO MARKETS book separately from other books
2. **Independent Testing**: Test ALL NO MARKETS in isolation before live deployment
3. **Balance Management**: The $250 seed is permanent - plan trading accordingly
4. **NO-Side Focus**: This strategy only trades NO contracts, supplement with other strategies for YES-side opportunities

## Technical Limitations

1. **Dashboard Order Management**: Cannot place/cancel orders from the dashboard (read-only)
2. **Strategy Independence**: Requires separate management from regular lanes
3. **Market Access**: Only trades NO contracts - consider complementary strategies for YES-side
4. **No Advanced Features**: Currently does not include stop-loss, profit-taking, or adaptive exits

## Future Enhancements

1. **Dashboard Order Controls**: Add ability to place/cancel orders from dashboard
2. **Dynamic Exits**: Implement stop-loss and profit-taking for ALL NO book
3. **Advanced Sizing**: Consider adaptive Kelly multipliers based on volatility
4. **Risk Integration**: Incorporate ALL NO book risk into overall portfolio risk management
5. **Analytics**: Add specific analytics for ALL NO MARKETS performance
