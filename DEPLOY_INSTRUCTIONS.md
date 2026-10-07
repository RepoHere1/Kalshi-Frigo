# Railway Deploy - Action Required

## The Problem
Railway CLI v5.49.2 rejects all tokens. Auth flow is broken.

## The Fix (pick ONE option)

### Option A: Railway Web UI (2 minutes)
1. Go to https://railway.app/project/6e07c63c-c198-4af5-86af-370ab4404f04
2. Click "Deployments" tab
3. Click "Redeploy" (top right)
4. Wait 2-3 minutes for build
5. Check /health endpoint

### Option B: GitHub Auto-Deploy (zero effort)
1. Go to Railway project Settings → GitHub Integration
2. Toggle "Auto Deploy" ON for `main` branch
3. Push any commit to trigger

### Option C: Manual Railway CLI Re-auth (5 minutes)
```bash
# Clear all Railway auth
rm ~/.railway/config.json
unset RAILWAY_TOKEN
unset RAILWAY_PROJECT_TOKEN

# Install latest Railway CLI
npm i -g @railway/cli

# Login (opens browser)
railway login

# Link project
railway link

# Deploy
railway up
```

## Verify Deploy
```bash
# Check status
railway status

# View logs
railway logs

# Test endpoint
curl https://[service].up.railway.app/health
```

## Expected Result
- Dashboard shows 4 UP/DOWN strategy cards:
  - BTC UP/DOWN (15min)
  - DOGE UP/DOWN (15min)
  - Hyperliquid UP/DOWN (15min)
  - BTC UP/DOWN (1 hour)
- Strategies can be started from dashboard
- Each strategy reads its own Kalshi series + Coinbase spot feed
