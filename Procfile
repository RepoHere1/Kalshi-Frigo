web: gunicorn --config gunicorn.conf.py web_dashboard:app
worker: python cli.py run --btc-updown --live --loop
keep-alive: python scripts/keep_dry_lanes_alive.py
