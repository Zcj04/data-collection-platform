"""Read-only inspection of stored cumulative revenue snapshots."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
from collections import defaultdict
from core.db import get_connection
from core.daily_operations import PLATFORM_FIELDS

sys.stdout.reconfigure(encoding='utf-8')
conn = get_connection()
data = defaultdict(dict)
for row in conn.execute('SELECT date,venue,platform,metrics_json,period_start FROM daily_summary ORDER BY date'):
    if row['platform'] not in PLATFORM_FIELDS:
        continue
    metrics = json.loads(row['metrics_json'])
    values = {key: float(value or 0) for key,value in metrics.items()
              if key in PLATFORM_FIELDS[row['platform']]}
    data[row['venue']].setdefault(row['date'], {})[row['platform']] = values
print('EXISTING', json.dumps([dict(r) for r in conn.execute('SELECT * FROM venue_lifecycle')],ensure_ascii=False))
for venue, days in sorted(data.items()):
    observations=[]
    previous={}
    for day, platforms in sorted(days.items()):
        changes=[]
        for platform, values in platforms.items():
            old_day, old = previous.get(platform, ('', {}))
            if old_day[:7] == day[:7]:
                delta=sum(values.values())-sum(old.values())
            else:
                delta=sum(values.values())
            if abs(delta) > .01:
                changes.append((platform, round(delta,2)))
            previous[platform]=(day,values)
        observations.append((day,changes))
    last=next((day for day,changes in reversed(observations) if changes),None)
    tail=[day for day,changes in observations if last and day>last]
    if tail or '香港' in venue:
        print(json.dumps({'venue':venue,'last_change':last,'tail_count':len(tail),'first_zero':tail[0] if tail else None,'platforms':{p: (d, v) for p,(d,v) in previous.items()},'boundary':[(d,p) for d,p in days.items() if last and last[:7]==d[:7] and d>=last][:4]},ensure_ascii=False))
