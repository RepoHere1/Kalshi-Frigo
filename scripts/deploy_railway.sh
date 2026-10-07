#!/usr/bin/env bash
# deploy_railway.sh - Deploy Kalshi-Frigo to Railway
# Usage: ./scripts/deploy_railway.sh
# Prerequisites: Railway CLI installed (`npm i -g @railway/cli`)

set -e

echo "=== Railway Deploy for Kalshi-Frigo ==="
echo ""

# Check Railway CLI
if ! command -v railway &> /dev/null; then
    echo "Railway CLI not found. Installing..."
    npm i -g @railway/cli
fi

# Check if logged in
echo "Checking Railway auth..."
if ! railway whoami &> /dev/null; then
    echo "Not logged in. Starting login flow..."
    railway login --browserless
    echo "Login complete. Run this script again to deploy."
    exit 0
fi

echo "Logged in successfully."
echo ""

# Link project if needed
echo "Checking project linkage..."
if ! railway status &> /dev/null; then
    echo "Linking to project..."
    railway link --project 6e07c63c-c198-4af5-86af-370ab4404f04 --service 8df25994-8477-4fae-86fe-5aa7c9fd91cf
fi

echo "Deploying to Railway..."
railway up

echo ""
echo "=== Deploy complete ==="
echo "Check status: railway status"
echo "View logs: railway logs"
echo "Dashboard: https://railway.app/project/6e07c63c-c198-4af5-86af-370ab4404f04"