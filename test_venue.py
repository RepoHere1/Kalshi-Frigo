#!/usr/bin/env python3
from src.jobs.venue_feeds import *

s = snapshot()
print(f"Binance: ${s['spot']:.0f}, imbalance {s['imb']:+.1f}%, basis {s.get('basis_pct') or '?'}, funding {s.get('funding') or '?'}")
agree, reason = venue_agreement(85800, 85820, 85733, 100)
print(f"venue_agreement test: {agree} ({reason})")
agree2, reason2 = venue_agreement(85800, 85900, 85733, 100)
print(f"diverge test: {agree2} ({reason2})")
