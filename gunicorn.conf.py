"""Gunicorn configuration for the Kalshi-Frigo dashboard on Railway.

Run with:
    gunicorn --config gunicorn.conf.py web_dashboard:app

gthread is required rather than the default sync worker: /api/stream is a
long-lived Server-Sent Events response, and a sync worker would be occupied
by that one connection until the client disconnects.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
worker_class = "gthread"

# MUST stay at 1. All dashboard state - sse_listeners, strategy_state,
# _child_procs - is held in module-level dicts, so a second worker gets its own
# copy: a /api/stream connection held by worker 1 never sees events broadcast
# by worker 2, and /api/bots disagrees with /api/strategies. Scale with
# threads instead. Remove this constraint before raising `workers`.
workers = 1
threads = int(os.environ.get("WEB_THREADS", "32"))

# SSE connections are long-lived, so the worker timeout must be generous or
# gunicorn will reap workers that are behaving correctly.
timeout = int(os.environ.get("WEB_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")


def post_worker_init(worker):
    """Start the monitor/log-tail threads inside each forked worker.

    These threads must be created after the fork; a thread started before it
    does not exist in the child process.
    """
    from web_dashboard import start_background_workers

    start_background_workers()
