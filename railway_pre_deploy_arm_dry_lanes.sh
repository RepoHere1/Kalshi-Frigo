#!/bin/bash

# RAILWAY PRE-DEPLOYMENT DRY LANE ARMING - Run BEFORE Railway deploy

cd /data/data/com.termux/files/home/Kalshi-Frigo

python3 scripts/railway_pre_deploy_arm_dry_lanes.py "$@"


# Add script content here
