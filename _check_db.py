import asyncio, aiosqlite

async def check():
    conn = await aiosqlite.connect('trading_system.db')
    cur = await conn.execute("SELECT COUNT(*) FROM trade_logs WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'live'")
    live_trade_logs = (await cur.fetchone())[0]
    cur = await conn.execute("SELECT COUNT(*) FROM positions WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'live'")
    live_positions = (await cur.fetchone())[0]
    cur = await conn.execute('SELECT * FROM strategy_runtime')
    rows = await cur.fetchall()
    print(f'LIVE trade_logs rows: {live_trade_logs}')
    print(f'LIVE positions rows: {live_positions}')
    print(f'strategy_runtime rows: {len(rows)}')
    for r in rows:
        print(f'  {r}')
    await conn.close()

asyncio.run(check())