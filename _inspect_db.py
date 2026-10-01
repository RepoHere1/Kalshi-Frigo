import sqlite3
conn = sqlite3.connect('trading_system.db')
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print('tables:', [t[0] for t in tables])
try:
    rows = conn.execute('SELECT * FROM runtime_config').fetchall()
    print('runtime_config rows:', rows)
except Exception as e:
    print('runtime_config error:', e)
print('--- sample live trade_log ---')
for r in conn.execute("SELECT mode, strategy, market_id, side, pnl, exit_timestamp FROM trade_logs WHERE COALESCE(NULLIF(mode,''),'dry')='live'").fetchall():
    print(r)
print('--- sample live position ---')
for r in conn.execute("SELECT mode, strategy, market_id, side, quantity, status FROM positions WHERE COALESCE(NULLIF(mode,''),'dry')='live'").fetchall():
    print(r)
print('--- sample dry trade_log ---')
for r in conn.execute("SELECT mode, strategy, market_id, side, pnl, exit_timestamp FROM trade_logs WHERE COALESCE(NULLIF(mode,''),'dry')='dry'").fetchall():
    print(r)
print('--- sample dry position ---')
for r in conn.execute("SELECT mode, strategy, market_id, side, quantity, status FROM positions WHERE COALESCE(NULLIF(mode,''),'dry')='dry'").fetchall():
    print(r)
