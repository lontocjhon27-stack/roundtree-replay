"""Daily bad-number check (WE Management GHL).

For contacts added in the last LOOKBACK_DAYS that have a phone and are not already tagged `bad-number`:
if 2+ outbound SMS are undelivered/failed and none were ever delivered, then
  - add tag `bad-number`
  - turn on DND for SMS only (every workflow skips SMS steps; email + calls continue)
  - create a task for the contact owner: "Bad number: get a correct phone".

Public repo: logs counts only, never names, emails or phone numbers.

Env: GHL_TOKEN (Private Integration: contacts r/w, conversations + messages read), GHL_LOCATION_ID,
     DRY_RUN=1 (count only, change nothing), LOOKBACK_DAYS (default 45).
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests

API = 'https://services.leadconnectorhq.com'
LOC = os.environ.get('GHL_LOCATION_ID', 'pcY7ioQLTs15S0hJiOdG')
DRY = os.environ.get('DRY_RUN') == '1'
LOOKBACK = int(os.environ.get('LOOKBACK_DAYS') or 45)
TAG = 'bad-number'
MIN_FAILED = 2


def log(*a):
    print(*a, flush=True)


def ghl(method, path, body=None, params=None, version='2021-07-28'):
    h = {'Authorization': 'Bearer ' + os.environ['GHL_TOKEN'], 'Version': version,
         'Content-Type': 'application/json', 'Accept': 'application/json'}
    for attempt in range(5):
        r = requests.request(method, API + path, headers=h, params=params,
                             data=json.dumps(body) if body is not None else None, timeout=60)
        if r.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        if r.status_code >= 400:
            msg = ''
            try:
                msg = str(r.json().get('message', ''))[:120]
            except Exception:
                pass
            raise RuntimeError(f'GHL {method} {re.sub(r"/[A-Za-z0-9]{20,}", "/<id>", path.split("?")[0])} -> {r.status_code} {msg}')
        return r.json() if r.text else {}
    raise RuntimeError('GHL rate limited')


def recent_contacts():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK)
    out = []
    for page in range(1, 60):
        j = ghl('POST', '/contacts/search', {'locationId': LOC, 'page': page, 'pageLimit': 100,
                                              'sort': [{'field': 'dateAdded', 'direction': 'desc'}]})
        cs = j.get('contacts') or []
        stop = False
        for c in cs:
            added = dt.datetime.fromisoformat(str(c.get('dateAdded')).replace('Z', '+00:00'))
            if added < cutoff:
                stop = True
                break
            if c.get('phone') and TAG not in (c.get('tags') or []):
                out.append((c['id'], c.get('assignedTo') or ''))
        if stop or len(cs) < 100:
            break
    return out


def sms_counts(cid):
    delivered = failed = 0
    convs = ghl('GET', '/conversations/search', params={'locationId': LOC, 'contactId': cid, 'limit': 5},
                version='2021-04-15').get('conversations') or []
    for cv in convs:
        m = ghl('GET', f'/conversations/{cv["id"]}/messages', params={'limit': 100}, version='2021-04-15')
        for x in (m.get('messages') or {}).get('messages') or []:
            if (x.get('messageType') or x.get('type')) != 2 or x.get('direction') != 'outbound':
                continue
            st = x.get('status')
            if st in ('delivered', 'read'):
                delivered += 1
            elif st in ('undelivered', 'failed'):
                failed += 1
    return delivered, failed


def mark_bad(cid, owner):
    ghl('POST', f'/contacts/{cid}/tags', {'tags': [TAG]})
    ghl('PUT', f'/contacts/{cid}', {'dndSettings': {'SMS': {
        'status': 'active', 'message': 'Bad number: 2+ texts undelivered, none delivered (auto check)', 'code': ''}}})
    if owner:
        due = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        ghl('POST', f'/contacts/{cid}/tasks', {
            'title': 'Bad number: get a correct phone',
            'body': 'Texts to this number keep failing (2+ undelivered, none delivered), so SMS is now off for this '
                    'contact. Reach them by email, call or DM, update their phone number, then turn SMS DND back off.',
            'dueDate': due, 'completed': False, 'assignedTo': owner})


def main():
    contacts = recent_contacts()
    log(f'Checking {len(contacts)} contacts with a phone added in the last {LOOKBACK} days (not yet tagged).')
    bad = errors = 0
    for i, (cid, owner) in enumerate(contacts, 1):
        try:
            d, f = sms_counts(cid)
            if f >= MIN_FAILED and d == 0:
                bad += 1
                if not DRY:
                    mark_bad(cid, owner)
        except Exception as e:
            errors += 1
            if errors <= 3:
                log('Error:', str(e)[:160])
            if 'not authorized for this scope' in str(e):
                log('STOP: the GHL Private Integration token needs the conversations.readonly and '
                    'conversations/message.readonly scopes. Nothing was changed.')
                sys.exit(1)
        if i % 200 == 0:
            log(f'...{i} checked, {bad} bad so far')
    log(f'Done. Bad numbers found: {bad}' + (' (DRY RUN, nothing changed)' if DRY else ' (tagged + SMS DND + owner task)')
        + f'. Errors: {errors}.')
    if errors and errors == len(contacts):
        sys.exit(1)


if __name__ == '__main__':
    main()
