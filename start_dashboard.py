#!/usr/bin/env python3
"""Kalshi-Frigo local dashboard launcher - pure Python. Replaces start_dashboard.cmd."""
import os
import sys
import subprocess

def main():
    port = "5000"
    mode = "live"

    # Parse arguments
    args = sys.argv[1:]
    for arg in args:
        if arg.lower() == "demo":
            mode = "demo"
        elif arg.isdigit():
            port = arg
        else:
            print(f'Unknown argument: "{arg}". Usage: start_dashboard.py [demo] [port]')
            sys.exit(1)

    master = r"D:\master.env"
    if not os.path.exists(master):
        print()
        print(f"ERROR: {master} not found.")
        print("The Kalshi / OpenRouter / dashboard credentials live in that file.")
        print()
        sys.exit(1)

    # Parse master.env
    creds = {}
    with open(master, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                name, value = line.split('=', 1)
                creds[name.strip()] = value.strip()

    # Demo credentials
    if mode == "demo":
        creds["KALSHI_API_KEY"] = creds.get("KALSHI_DEMO_API_KEY_ID", "")
        creds["KALSHI_PRIVATE_KEY_PATH"] = creds.get("KALSHI_DEMO_PRIVATE_KEY_PATH", r"C:\Users\Device\kalshi_demo_key.pem")

    # KALSHI_API_KEY; master.env names it KALSHI_API_KEY_ID
    if "KALSHI_API_KEY" not in creds or not creds["KALSHI_API_KEY"]:
        creds["KALSHI_API_KEY"] = creds.get("KALSHI_API_KEY_ID", "")

    if not creds.get("KALSHI_API_KEY"):
        print()
        print(f"ERROR: no Kalshi API key id found in {master}")
        sys.exit(1)

    key_path = creds.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_path or not os.path.exists(key_path):
        print()
        print(f"ERROR: private key not found at {key_path}")
        sys.exit(1)

    # Set environment variables
    os.environ["KALSHI_API_KEY"] = creds["KALSHI_API_KEY"]
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = key_path
    os.environ["OPENROUTER_API_KEY"] = creds.get("OPENROUTER_API_KEY", "")
    os.environ["DASHBOARD_TOKEN"] = creds.get("DASHBOARD_TOKEN", "")
    os.environ["PORT"] = port
    os.environ["PYTHONUNBUFFERED"] = "1"

    print()
    print(" Kalshi-Frigo dashboard")
    print(" ----------------------")
    print(f"  mode    : {mode}")
    print(f"  key id  : {creds['KALSHI_API_KEY']}")
    print(f"  key file: {key_path}")
    print(f"  url     : http://localhost:{port}/")
    print()
    print(" (close this window to stop the dashboard)")
    print()

    # Open browser
    import webbrowser
    webbrowser.open(f"http://localhost:{port}/")

    # Run the dashboard
    os.execvp("python", ["python", "web_dashboard.py"])

if __name__ == "__main__":
    main()