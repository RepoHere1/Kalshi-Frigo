# DEPLOYMENT INSTRUCTIONS - Kalshi-Frigo Multi-Asset Trading Bot

## 🎯 WHAT WAS DONE

### Code Changes (ALL COMPLETE ✅)
1. **Added multi-asset support** - 4 trading strategies can now trade different assets:
   - `btc_updown` → KXBTC15M markets vs BTC-USD spot (15-minute)
   - `doge_updown` → KXDOGE15M markets vs DOGE-USD spot (15-minute)
   - `hyperliquid_updown` → KXHYPE15M markets vs HYPE-USD spot (15-minute)
   - `btc_1h_updown` → KXBTC1H markets vs BTC-USD spot (1-hour)

2. **Fixed noise threshold scaling** - Changed from fixed $15 USD noise (only worked for BTC) to 0.02% of target price (works for all assets)

3. **All tests passing** - 654/654 tests pass

4. **Code pushed to GitHub** - Latest commit: `d941d40` on `main` branch

---

## 🚨 WHAT NEEDS TO HAPPEN NOW

**Railway deployment is blocked because the authentication token expired.**

You have THREE options:

---

## OPTION 1: Manual Railway Web UI Deploy (EASIEST - 2 MINUTES)

### Steps:
1. Go to https://railway.app/project/6e07c63c-c198-4af5-86af-370ab4404f04
2. Log in with your Railway account
3. Click on the **service** (should be named "web" or "Kalshi-Frigo")
4. Click **"Settings"** tab
5. Scroll to **"Deployment"** section
6. Click **"Redeploy"** or **"Deploy Latest"**
7. Wait 2-3 minutes for build to complete

### Expected Result:
- Build succeeds (uses `railway.json` startCommand: `gunicorn --config gunicorn.conf.py web_dashboard:app`)
- Health check passes at `/health` endpoint
- Dashboard loads at Railway's public URL
- 4 new strategy cards visible: btc_updown, doge_updown, hyperliquid_updown, btc_1h_updown

---

## OPTION 2: CLI Deploy After Re-Authentication (5 MINUTES)

### Prerequisites:
- Railway CLI installed (`npm i -g @railway/cli` or `brew install railway`)

### Steps:

1. **Clear bad token:**
```bash
unset RAILWAY_TOKEN
rm ~/.railway/config.json
```

2. **Login (browser flow - EASIEST):**
```bash
railway login
# Browser opens → click "Authorize"
```

OR **Login (browserless flow):**
```bash
railway login --browserless
# Copy the URL shown (https://railway.com/activate?user_code=XXXX-XXXX)
# Open in browser → click "Authorize"
# Return to terminal - it auto-completes
```

3. **Link to project:**
```bash
cd /path/to/Kalshi-Frigo
railway link
# Select project: "Kalshi-Frigo" (ID: 6e07c63c-c198-4af5-86af-370ab4404f04)
```

4. **Deploy:**
```bash
railway up
# OR
railway deploy
```

5. **Verify:**
```bash
railway status
railway logs
```

### Expected Result:
- Deployment completes in 2-3 minutes
- Logs show: `Starting gunicorn 23.0.0`
- Health check passes
- Dashboard accessible at Railway public URL

---

## OPTION 3: GitHub Auto-Deploy (IF ENABLED - 0 EFFORT)

### Check if it's enabled:
1. Go to Railway project → Settings → **GitHub Integration**
2. If "Auto Deploy" is **ON** for `main` branch:
   - **It already deployed** when you pushed commit `d941d40`
   - Check Deployments tab to see if a new build started ~5 minutes ago

### If NOT enabled:
1. In Railway project Settings → GitHub Integration
2. Toggle **"Auto Deploy"** to ON
3. Select branch: `main`
4. Click **"Deploy Now"** to trigger first build

### Expected Result:
- Every git push to `main` auto-deploys
- Latest commit `d941d40` is live

---

## 🔍 HOW TO VERIFY IT WORKED

### 1. Check Dashboard URL:
- Find Railway public URL: `https://[your-service].up.railway.app`
- Open in browser
- Should see 9 strategy cards (4 are new UP/DOWN strategies)

### 2. Check Strategy Cards Exist:
Look for these card titles:
- **BTC UP/DOWN (15min)** - `btc_updown`
- **DOGE UP/DOWN (15min)** - `doge_updown`  
- **Hyperliquid UP/DOWN (15min)** - `hyperliquid_updown`
- **BTC UP/DOWN (1 hour)** - `btc_1h_updown`

### 3. Start a Strategy (Test):
- Click **Start** on `btc_updown` card
- Wait 30-60 seconds
- Card should show:
  - Status: "Running"
  - Trades: 0+ (if markets available)
  - Last seen: <10 seconds ago

### 4. Check Logs:
- Click **"Logs"** button (top right)
- Should see: `SpotFeed connected: BTC-USD at 0.20 Hz`
- Should see: `Btc15mFeed fetched N markets`

### 5. Verify Each Asset Trades Independently:
```bash
# SSH into Railway container OR check logs
grep "KXDOGE15M" logs  # DOGE strategy should only trade DOGE markets
grep "KXBTC1H" logs    # BTC 1H should only trade 1-hour markets
grep "KXHYPE15M" logs  # Hyperliquid should only trade HYPE markets
```

---

## 🐛 TROUBLESHOOTING

### Problem: "401 Unauthorized" when deploying
**Solution:** Token expired. Use Option 1 (Web UI) or Option 2 Step 1-2 (re-login)

### Problem: Strategies show 0 trades
**Possible causes:**
1. No markets available at that moment (Kalshi may not have active contracts)
2. Prices outside tradeable range (check logs for "skipping" messages)
3. Paper mode enabled (check `--paper` flag in `STRATEGY_COMMANDS`)

**Debug:**
- Check logs for: `SpotFeed connected`
- Check logs for: `Btc15mFeed fetched N markets`
- If N=0, Kalshi has no active contracts for that series

### Problem: Build fails
**Check:**
1. `requirements.txt` is present
2. `gunicorn.conf.py` exists
3. `railway.json` `startCommand` is correct:
   ```json
   "startCommand": "gunicorn --config gunicorn.conf.py web_dashboard:app"
   ```

### Problem: Health check fails
**Check:**
1. `/health` endpoint returns 200 (defined in `web_dashboard.py`)
2. Port is correct (Railway auto-sets `PORT` env var)
3. `gunicorn` is listening on `0.0.0.0:$PORT`

---

## 📋 ENVIRONMENT VARIABLES NEEDED (Railway Project Settings)

**Required for live trading:**
```bash
KALSHI_EMAIL=your-email@example.com
KALSHI_PASSWORD=your-password
OPENROUTER_API_KEY=sk-or-v1-xxx
LIVE_TRADING_ENABLED=false  # Keep false for paper trading
```

**Optional:**
```bash
TELEGRAM_WEBHOOK=https://...  # For alerts
DISCORD_WEBHOOK=https://...   # For alerts
```

**Note:** `kalshi_private_key.pem` must be in project root (Railway persistent volume OR in repo as git-ignored file)

---

## 🎯 SUMMARY FOR ANY AI

**Task:** Deploy latest code to Railway

**Status:** Code is ready. Authentication is blocking CLI deploy.

**Solutions (pick ONE):**
1. Use Railway web UI → click "Redeploy" (fastest, no CLI needed)
2. Re-authenticate Railway CLI → run `railway up` (requires terminal)
3. Enable GitHub auto-deploy → it already deployed (zero effort)

**Success criteria:**
- Dashboard loads at Railway URL
- 4 new strategy cards visible (btc_updown, doge_updown, hyperliquid_updown, btc_1h_updown)
- Strategies can be started and show "Running" status
- Logs show: `SpotFeed connected: {ASSET}-USD`

**Do NOT:**
- Try to fix authentication issues if web UI works
- Run `railway login` without clearing old token first
- Commit new Railway config files (already in `.gitignore`)

---

## 📁 KEY FILES MODIFIED (Reference)

| File | What Changed |
|------|-------------|
| `cli.py` | Added `--series` and `--spot-product` args |
| `src/jobs/ladder_trader.py` | Replaced `noise_usd=15.0` with `noise_pct=0.0002` |
| `src/jobs/market_data.py` | Made feeds accept `series` and `spot_product` parameters |
| `web_dashboard.py` | Added 4 new strategies to `STRATEGY_COMMANDS`, `STRATEGY_ALIASES`, `STRATEGY_DOCS` |
| `tests/test_ladder_trader.py` | Updated fixtures to use `noise_pct` |

**GitHub commits:**
- `d941d40` - Fix ticker parsing 
- `183197d` - Generalize noise thresholds
- `db8a0be` - Add multi-asset support

**Railway Project:**
- ID: `6e07c63c-c198-4af5-86af-370ab4404f04`
- Service: `8df25994-8477-4fae-86fe-5aa7c9fd91cf`
- URL: https://railway.app/project/6e07c63c-c198-4af5-86af-370ab4404f04
