#!/usr/bin/env python3
"""
Refreshes cached flight status (Supabase settings.flight_status) from AeroDataBox.
Meant to be run periodically (e.g. hourly via cron). Only calls the AeroDataBox API
for flights whose cached status is actually stale, to stay within the free quota
before the trip dates.

Staleness rules (mirrors the in-app JS logic):
  - default: refetch at most every 12h
  - "hot" flights: refetch at most every 1h, starting from a given date
"""
import json
import time
import datetime
import urllib.request

SB_URL = 'https://ukcnotbbfnvlvnmfhgmh.supabase.co'
SB_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVrY25vdGJiZm52bHZubWZoZ21oIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3MDk0MTgsImV4cCI6MjA5ODI4NTQxOH0.57fsqKomnbTUYMWJ7o5loMBYKByMPdvk4eRcD3Q_cpE'
RAPIDAPI_KEY = '5f2d58c9cdmshba90ad8a0c65643p1e5606jsnba11b5bd99af'
RAPIDAPI_HOST = 'aerodatabox.p.rapidapi.com'

TRACKED_FLIGHTS = [
    {'num': 'VF278', 'date': '2026-07-05'},
    {'num': 'VF9', 'date': '2026-07-05'},
    {'num': 'TO7448', 'date': '2026-07-09'},
    {'num': 'TO7451', 'date': '2026-07-11'},
    {'num': 'VF516', 'date': '2026-07-18'},
    {'num': 'VF587', 'date': '2026-07-18'},
]

DEFAULT_STALE_MS = 12 * 60 * 60 * 1000
HOT_STALE_MS = 60 * 60 * 1000
HOT_FROM = {'VF278': '2026-07-04', 'VF587': '2026-07-17'}


def sb_headers():
    return {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}', 'Content-Type': 'application/json'}


def sb_get(path):
    req = urllib.request.Request(SB_URL + path, headers=sb_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def sb_upsert_settings(key, value):
    req = urllib.request.Request(
        SB_URL + '/rest/v1/settings',
        data=json.dumps({'key': key, 'value': value}).encode(),
        headers={**sb_headers(), 'Prefer': 'resolution=merge-duplicates,return=minimal'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def stale_ms(num):
    hot_from = HOT_FROM.get(num)
    if hot_from and datetime.date.today() >= datetime.date.fromisoformat(hot_from):
        return HOT_STALE_MS
    return DEFAULT_STALE_MS


def fetch_status(num, date):
    req = urllib.request.Request(
        f'https://{RAPIDAPI_HOST}/flights/number/{num}/{date}',
        headers={
            'x-rapidapi-key': RAPIDAPI_KEY,
            'x-rapidapi-host': RAPIDAPI_HOST,
            'User-Agent': 'curl/8.4.0',
            'Accept': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data[0] if isinstance(data, list) and data else data
    except Exception as e:
        print(f'  fetch error for {num}: {e}')
        return None


def parse_time(t):
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(t.replace('Z', '+00:00').replace(' ', 'T'))
    except Exception:
        return None


def parse_status(raw):
    if not raw:
        return {'label': 'Нет данных', 'cls': 'fs-muted'}
    status = (raw.get('status') or '').lower()
    dep = raw.get('departure') or {}
    sched = (dep.get('scheduledTime') or {}).get('utc')
    revised = ((dep.get('revisedTime') or {}).get('utc')
               or (dep.get('runwayTime') or {}).get('utc')
               or (dep.get('predictedTime') or {}).get('utc'))
    delay_min = None
    a, b = parse_time(sched), parse_time(revised)
    if a and b:
        delay_min = round((b - a).total_seconds() / 60)

    if 'cancel' in status:
        return {'label': 'Отменён', 'cls': 'fs-bad'}
    if 'divert' in status:
        return {'label': 'Перенаправлен', 'cls': 'fs-bad'}
    if delay_min is not None and delay_min >= 15:
        return {'label': f'Задержан +{delay_min} мин', 'cls': 'fs-warn'}
    if 'land' in status or 'arriv' in status:
        return {'label': 'Прибыл', 'cls': 'fs-ok'}
    if 'enroute' in status or 'depart' in status or 'air' in status:
        return {'label': 'В воздухе', 'cls': 'fs-ok'}
    if any(k in status for k in ('expect', 'schedul', 'checkin', 'boarding', 'gateclosed')):
        return {'label': 'По расписанию', 'cls': 'fs-ok'}
    return {'label': raw.get('status') or 'Нет данных', 'cls': 'fs-muted'}


def main():
    rows = sb_get('/rest/v1/settings?key=eq.flight_status&select=value')
    cache = {}
    if rows and rows[0].get('value'):
        try:
            cache = json.loads(rows[0]['value'])
        except Exception:
            cache = {}

    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    changed = False
    first_fetch = True

    for f in TRACKED_FLIGHTS:
        cached = cache.get(f['num'])
        if cached and (now_ms - cached.get('fetchedAt', 0)) <= stale_ms(f['num']):
            print(f"{f['num']}: cache fresh, skipped")
            continue
        if not first_fetch:
            time.sleep(1.2)  # stay under the per-second rate limit
        first_fetch = False
        raw = fetch_status(f['num'], f['date'])
        if raw is None:
            # API call failed (rate limit, transient error, etc.) — leave the
            # previous cache entry untouched so it stays "stale" and gets
            # retried on the next cron run, instead of being marked fresh
            # with no real data.
            print(f"{f['num']}: fetch failed, keeping previous cache entry")
            continue
        parsed = parse_status(raw)
        parsed['fetchedAt'] = now_ms
        cache[f['num']] = parsed
        changed = True
        print(f"{f['num']}: {parsed['label']}")

    if changed:
        sb_upsert_settings('flight_status', json.dumps(cache, ensure_ascii=False))
        print('Supabase cache updated.')
    else:
        print('Nothing stale — no API calls made.')


if __name__ == '__main__':
    main()
