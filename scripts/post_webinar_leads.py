"""After each Will Roundtree LIVE Masterclass: tag attendees HOT/WARM/COLD in GHL and send the setter reports.

Runs in GitHub Actions ~20-60 min after the webinar ends (or by hand):
  1. Zoom (Server-to-Server OAuth): find the newest ended instance of the masterclass meeting
  2. Pull its participants (join/leave per session) and work out real minutes attended per person
     (sessions merged, so rejoiners are not double counted). HOT >= 75 min, WARM 30-74, COLD < 30.
  3. GHL: for each attendee contact -> remove old temp tags, add "<temp> leads" + "webinar attended",
     move this webinar's Live Webinars opportunity to "Attended - Hot/Warm/Cold".
     The GHL tag workflows then assign a setter (round-robin, existing setters kept) and create the
     labeled HOT/WARM/COLD call task in Manual Actions.
  4. Wait for owners, then write the per-setter report tables + Slack summary into GHL Custom Values
     and add the tag "webinar-report-send" to the report trigger contact. Two GHL workflows
     (EMAIL + SLACK) fire on that tag and send the reports using those Custom Values.

The repo is PUBLIC: logs print counts only, never names, emails or phone numbers.
State (processed meeting uuids) is kept in post-webinar-state.json.

Environment:
  ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET   Zoom S2S OAuth (meeting:read:list_past_instances:admin,
                                                       meeting:read:list_past_participants:admin)
  GHL_TOKEN             GHL Private Integration token (contacts, opportunities, locations/customValues read+write)
  GHL_LOCATION_ID       default pcY7ioQLTs15S0hJiOdG
  MEETING_ID            Zoom meeting id of the recurring masterclass (default 85794795023)
  MEETING_UUID          optional: process this exact past instance (e.g. Nov 18 test)
  DRY_RUN=1             compute + print counts only, no GHL writes, no send
  NO_SEND=1             tag in GHL but do not trigger the email/Slack send
  FORCE=1               process even if this meeting uuid is already in the state file
"""
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, 'post-webinar-state.json')
PT = ZoneInfo('America/Los_Angeles')

LOC = os.environ.get('GHL_LOCATION_ID', 'pcY7ioQLTs15S0hJiOdG')
MEETING_ID = os.environ.get('MEETING_ID', '').strip() or '85794795023'
MEETING_UUID = os.environ.get('MEETING_UUID', '').strip()
DRY = os.environ.get('DRY_RUN') == '1'
NO_SEND = os.environ.get('NO_SEND') == '1'
FORCE = os.environ.get('FORCE') == '1'

HOT, WARM = 75, 30
MIN_MEETING_MIN = 45                      # ignore short/test instances
MAX_AGE_H = 20                            # only process instances that ended in the last N hours (scheduled runs)
STAFF = re.compile(r'wemanagementservices\.com|wroundtree|fireflies|notetaker|otter\.ai|read\.ai|'
                   r'usharplynn|kristin gaxiola|sylvia lewis|lontocjhon|bryanp1430', re.I)
SETTERS = {'dFuNrLRBXKs3nUdZv9r3': 'Bryan', 'Af2WeEEtiy7Z5U7yKyMc': 'Jeremiah', 'zsqykvPCJS05iKCe94rC': 'Phillip'}
PIPE = 'sBmrS1YUlbkpxLjNTX8Q'                 # Live Webinars (All Dates)
WEBINAR_DATE_FIELD = 'Xoa0EHn9Ap89HX3x47Fj'   # opportunity.webinar_date, "November 18 2026 17:00:00"
TEMP_TAGS = {'hot': 'hot leads', 'warm': 'warm leads', 'cold': 'cold leads'}
SEND_CONTACT = os.environ.get('REPORT_CONTACT_ID', 'NASxSWrCNx9ukPe5QPAk')   # "jhon lontoc (test - ignore)"
SEND_TAG = 'webinar-report-send'
TZ_TAGS = {'tz-eastern': ('Eastern', 'America/New_York'), 'tz-central': ('Central', 'America/Chicago'),
           'tz-mountain': ('Mountain', 'America/Denver'), 'tz-pacific': ('Pacific', 'America/Los_Angeles'),
           'tz-hawaii': ('Hawaii', 'Pacific/Honolulu')}
CV_KEYS = {  # custom value name -> what it holds
    'Webinar Report Date': 'date',
    'Webinar Report Bryan': 'Bryan', 'Webinar Report Jeremiah': 'Jeremiah', 'Webinar Report Phillip': 'Phillip',
    'Webinar Report Slack': 'slack',
}
API = 'https://services.leadconnectorhq.com'


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Zoom
def zoom_token():
    cred = base64.b64encode(f"{os.environ['ZOOM_CLIENT_ID']}:{os.environ['ZOOM_CLIENT_SECRET']}".encode()).decode()
    r = requests.post('https://zoom.us/oauth/token',
                      params={'grant_type': 'account_credentials', 'account_id': os.environ['ZOOM_ACCOUNT_ID']},
                      headers={'Authorization': f'Basic {cred}'}, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']


def enc_uuid(u):
    # Zoom: uuids starting with "/" or containing "//" must be double URL-encoded.
    q = urllib.parse.quote(u, safe='')
    return urllib.parse.quote(q, safe='') if (u.startswith('/') or '//' in u) else q


def zget(token, path, params=None):
    r = requests.get('https://api.zoom.us/v2' + path, params=params or {},
                     headers={'Authorization': f'Bearer {token}'}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f'Zoom {path.split("?")[0]} -> {r.status_code} {r.text[:200]}')
    return r.json()


def pick_instance(token):
    if MEETING_UUID:
        return {'uuid': MEETING_UUID, 'start_time': None}
    inst = zget(token, f'/past_meetings/{MEETING_ID}/instances').get('meetings', [])
    inst.sort(key=lambda m: m.get('start_time', ''), reverse=True)
    for m in inst:
        st = dt.datetime.fromisoformat(m['start_time'].replace('Z', '+00:00'))
        if dt.datetime.now(dt.timezone.utc) - st > dt.timedelta(hours=MAX_AGE_H + 3):
            break
        return m
    return None


def participants(token, uuid):
    out, nxt = [], ''
    for _ in range(50):
        p = {'page_size': 300}
        if nxt:
            p['next_page_token'] = nxt
        j = zget(token, f'/past_meetings/{enc_uuid(uuid)}/participants', p)
        out += j.get('participants', [])
        nxt = j.get('next_page_token') or ''
        if not nxt:
            break
    return out


def minutes_by_person(parts):
    people = {}
    for p in parts:
        email = (p.get('user_email') or '').strip().lower()
        name = (p.get('name') or '').strip()
        if STAFF.search(email) or STAFF.search(name):
            continue
        key = email or ('name:' + name.lower())
        try:
            j = dt.datetime.fromisoformat(p['join_time'].replace('Z', '+00:00'))
            l = dt.datetime.fromisoformat(p['leave_time'].replace('Z', '+00:00'))
        except Exception:
            continue
        d = people.setdefault(key, {'email': email, 'name': name, 'iv': []})
        d['iv'].append((j, l))
        if name and (not d['name'] or len(name) < len(d['name'])):
            d['name'] = name
    for d in people.values():
        iv = sorted(d['iv'])
        tot, (cs, ce) = 0.0, iv[0]
        for s, e in iv[1:]:
            if s <= ce:
                ce = max(ce, e)
            else:
                tot += (ce - cs).total_seconds()
                cs, ce = s, e
        tot += (ce - cs).total_seconds()
        d['minutes'] = round(tot / 60)
        d['temp'] = 'hot' if d['minutes'] >= HOT else 'warm' if d['minutes'] >= WARM else 'cold'
        # Old registration Zap appended the setter's surname to Zoom names; strip it for matching/display.
        d['name'] = re.sub(r'\s+(Freeman|Senados|Perez|Bowers)$', '', re.sub(r'\s+', ' ', re.sub(r'\(.*?\)', '', d['name'])).strip())
    return list(people.values())


# ---------------------------------------------------------------- GHL
def H():
    return {'Authorization': 'Bearer ' + os.environ['GHL_TOKEN'], 'Version': '2021-07-28',
            'Content-Type': 'application/json', 'Accept': 'application/json'}


def ghl(method, path, body=None, params=None, ok404=False):
    for attempt in range(4):
        r = requests.request(method, API + path, headers=H(), params=params,
                             data=json.dumps(body) if body is not None else None, timeout=60)
        if r.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            # public repo logs: status + GHL message only, never the response body (may hold contact data)
            msg = ''
            try:
                msg = str(r.json().get('message', ''))[:120]
            except Exception:
                pass
            raise RuntimeError(f'GHL {method} {re.sub(r"/[A-Za-z0-9]{20,}", "/<id>", path.split("?")[0])} -> {r.status_code} {msg}')
        return r.json() if r.text else {}
    raise RuntimeError(f'GHL {method} {path} rate limited')


def find_contact(person, webinar_date):
    if person['email']:
        j = ghl('GET', '/contacts/search/duplicate', params={'locationId': LOC, 'email': person['email']})
        c = (j or {}).get('contact')
        if c:
            return c['id']
    # Zoom email differs from the registration email: match by full name among this webinar's registrants.
    if person['name'] and ' ' in person['name']:
        j = ghl('POST', '/contacts/search', {'locationId': LOC, 'page': 1, 'pageLimit': 20, 'query': person['name']})
        cands = [c for c in (j.get('contacts') or [])
                 if f"{c.get('firstName','')} {c.get('lastName','')}".strip().lower() == person['name'].lower()]
        if len(cands) == 1:
            return cands[0]['id']
    return None


def attended_stages():
    pl = ghl('GET', '/opportunities/pipelines', params={'locationId': LOC})
    pipe = next(p for p in pl['pipelines'] if p['id'] == PIPE)
    st = lambda rx: next((s['id'] for s in pipe['stages'] if re.search(rx, s['name'], re.I)), None)
    return {'hot': st(r'attended.*hot'), 'warm': st(r'attended.*warm'), 'cold': st(r'attended.*cold')}


def write_contact(cid, person, stages, webinar_date):
    ghl('DELETE', f'/contacts/{cid}/tags', {'tags': list(TEMP_TAGS.values())})
    ghl('POST', f'/contacts/{cid}/tags', {'tags': [TEMP_TAGS[person['temp']], 'webinar attended']})
    os_ = ghl('GET', '/opportunities/search', params={'location_id': LOC, 'pipeline_id': PIPE, 'contact_id': cid, 'limit': 20})
    val = lambda f: str(f.get('fieldValueString') or f.get('fieldValue') or f.get('field_value') or f.get('value') or '')
    opp = next((o for o in os_.get('opportunities', [])
                if any(f.get('id') == WEBINAR_DATE_FIELD and val(f) == webinar_date for f in o.get('customFields', []))), None)
    if opp:
        ghl('PUT', f'/opportunities/{opp["id"]}', {'pipelineStageId': stages[person['temp']]})


def contact(cid):
    return ghl('GET', f'/contacts/{cid}')['contact']


# ---------------------------------------------------------------- report
def esc(s):
    return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def fmt_phone(p):
    d = re.sub(r'\D', '', p or '')
    return f'({d[1:4]}) {d[4:7]}-{d[7:]}' if len(d) == 11 and d[0] == '1' else (p or '—')


def call_tonight(tz):
    h = dt.datetime.now(ZoneInfo(tz)).hour
    return 'Yes' if 9 <= h < 21 else 'Tomorrow 9 AM their time'


def table(rows):
    td = 'style="border:1px solid #ddd;padding:6px 8px;font-family:Arial,Helvetica,sans-serif;font-size:13px;vertical-align:top;"'
    th = 'style="border:1px solid #ddd;padding:6px 8px;font-family:Arial,Helvetica,sans-serif;font-size:13px;background:#f3f3f3;text-align:left;"'
    lab = {'hot': '🔥 HOT', 'warm': '🟠 WARM', 'cold': '🔵 COLD'}
    order = {'hot': 0, 'warm': 1, 'cold': 2}
    rows = sorted(rows, key=lambda r: (order[r['temp']], -r['minutes']))
    head = ''.join(f'<th {th}>{h}</th>' for h in ['#', 'Lead', 'Phone', 'Temp', 'Watched', 'Lead time zone', 'Call tonight?'])
    body = ''.join(
        f'<tr><td {td}>{i}</td><td {td}>{esc(r["display"])}</td><td {td}>{esc(fmt_phone(r["phone"]))}</td>'
        f'<td {td}>{lab[r["temp"]]}</td><td {td}>{r["minutes"]} min</td><td {td}>{esc(r["tz"])}</td>'
        f'<td {td}>{esc(r["tonight"])}</td></tr>' for i, r in enumerate(rows, 1))
    return f'<table style="border-collapse:collapse;width:100%;"><tr>{head}</tr>{body}</table>'


def setter_block(name, rows):
    c = lambda t: sum(1 for r in rows if r['temp'] == t)
    p = 'style="margin:0px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;"'
    return (f'<p {p}><b>Summary:</b> {len(rows)} leads &nbsp;·&nbsp; 🔥 {c("hot")} Hot &nbsp;·&nbsp; '
            f'🟠 {c("warm")} Warm &nbsp;·&nbsp; 🔵 {c("cold")} Cold</p><p style="margin:0px;">&nbsp;</p>' + table(rows))


def set_custom_values(values):
    cur = ghl('GET', f'/locations/{LOC}/customValues').get('customValues', [])
    by = {c['name']: c for c in cur}
    for name, value in values.items():
        if name in by:
            ghl('PUT', f'/locations/{LOC}/customValues/{by[name]["id"]}', {'name': name, 'value': value})
        else:
            ghl('POST', f'/locations/{LOC}/customValues', {'name': name, 'value': value})


# ---------------------------------------------------------------- main
def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {'processed': []}


def main():
    state = load_state()
    token = zoom_token()
    inst = pick_instance(token)
    if not inst:
        log('No recent masterclass instance. Nothing to do.')
        return
    uuid = inst['uuid']
    if uuid in state['processed'] and not FORCE:
        log('Already processed this webinar. Nothing to do.')
        return

    parts = participants(token, uuid)
    if not parts:
        log('No participants yet (meeting still running or data not ready). Will retry next run.')
        return
    starts = [p['join_time'] for p in parts if p.get('join_time')]
    start = dt.datetime.fromisoformat(min(starts).replace('Z', '+00:00')).astimezone(PT)
    ends = [p['leave_time'] for p in parts if p.get('leave_time')]
    end = dt.datetime.fromisoformat(max(ends).replace('Z', '+00:00'))
    length = (end - dt.datetime.fromisoformat(min(starts).replace('Z', '+00:00'))).total_seconds() / 60
    if length < MIN_MEETING_MIN:
        log(f'Instance too short ({length:.0f} min), treating as a test. Nothing to do.')
        return
    # Webinar slot = 5:00 PM PT on the day the meeting started.
    slot = start.replace(hour=17, minute=0, second=0, microsecond=0)
    webinar_date = f'{slot:%B} {slot.day} {slot:%Y} 17:00:00'
    label = f'{slot:%b} {slot.day}'

    people = minutes_by_person(parts)
    counts = {t: sum(1 for p in people if p['temp'] == t) for t in ('hot', 'warm', 'cold')}
    log(f'Webinar {label}: {len(people)} attendees (excl. staff) -> HOT {counts["hot"]} / WARM {counts["warm"]} / COLD {counts["cold"]}')
    if DRY:
        # Read-only GHL checks: token scopes, stages, custom values, and how many attendees match a contact.
        stages = attended_stages()
        cvs = ghl('GET', f'/locations/{LOC}/customValues').get('customValues', [])
        found = sum(1 for p in people if find_contact(p, webinar_date))
        have = [n for n in CV_KEYS if any(c['name'] == n for c in cvs)]
        log(f'DRY RUN (read-only GHL): stages found {sum(1 for v in stages.values() if v)}/3, '
            f'matched contacts {found}/{len(people)}, report custom values present {len(have)}/{len(CV_KEYS)}. No changes made.')
        return

    stages = attended_stages()
    matched, unmatched_people = [], []
    for p in people:
        cid = find_contact(p, webinar_date)
        if not cid:
            unmatched_people.append(p)
            continue
        write_contact(cid, p, stages, webinar_date)
        p['cid'] = cid
        matched.append(p)
    log(f'Tagged {len(matched)} contacts in GHL; {len(unmatched_people)} Zoom attendees had no matching GHL contact.')

    # Let the HOT/WARM/COLD workflows assign setters (they wait 2 min), then read owners.
    time.sleep(240)
    groups = {s: [] for s in SETTERS.values()}
    no_owner = 0
    for p in matched:
        c = contact(p['cid'])
        setter = SETTERS.get(c.get('assignedTo'))
        tz_tag = next((t for t in c.get('tags', []) if t in TZ_TAGS), None)
        tzl, tzname = TZ_TAGS.get(tz_tag, ('Unknown', 'America/New_York'))
        row = {'display': f"{c.get('firstName','')} {c.get('lastName','')}".strip().title() or p['name'],
               'phone': c.get('phone'), 'temp': p['temp'], 'minutes': p['minutes'], 'tz': tzl, 'tonight': call_tonight(tzname)}
        if setter:
            groups[setter].append(row)
        else:
            no_owner += 1
    log('Per setter: ' + ', '.join(f'{k} {len(v)}' for k, v in groups.items()) + f'; no setter owner: {no_owner}')

    hc = lambda rows: sum(1 for r in rows if r['temp'] == 'hot')
    total = sum(len(v) for v in groups.values())
    slack = (f"📣 *{label} Webinar Leads Are Out!*\n\n*{total} attended:*  🔥 {sum(hc(v) for v in groups.values())} Hot  ·  "
             f"🟠 {sum(1 for v in groups.values() for r in v if r['temp']=='warm')} Warm  ·  "
             f"🔵 {sum(1 for v in groups.values() for r in v if r['temp']=='cold')} Cold\n\n"
             f"<@U0C3YQQTP98>  {len(groups['Bryan'])} leads ({hc(groups['Bryan'])} hot)\n"
             f"<@U0C2J041FT2>  {len(groups['Jeremiah'])} leads ({hc(groups['Jeremiah'])} hot)\n"
             f"<@U0C6JNLEKAL>  {len(groups['Phillip'])} leads ({hc(groups['Phillip'])} hot)\n\n"
             "Your full lead report was sent to your *email*. Your leads are also in *Manual Actions*, each tagged 🔥 HOT / 🟠 WARM / 🔵 COLD.\n\n"
             "*Please call your leads now. HOT first.*\n_Reminder: no calls after 9 PM in the lead's local time. Call those first thing tomorrow._")
    if unmatched_people:
        lab = {'hot': 'HOT', 'warm': 'WARM', 'cold': 'COLD'}
        slack += ("\n\n⚠️ *Not matched to a GHL contact* (Zoom name/email differs from registration), please find + tag manually:\n"
                  + '\n'.join(f"• {p['name'] or '(no name)'} ({p['email'] or 'no email'}) – {lab[p['temp']]}, {p['minutes']} min"
                              for p in unmatched_people))
    values = {'Webinar Report Date': label, 'Webinar Report Slack': slack}
    for s, rows in groups.items():
        values[f'Webinar Report {s}'] = setter_block(s, rows) if rows else '<p>No new webinar leads for you this time.</p>'
    set_custom_values(values)
    log('Report custom values updated.')

    if NO_SEND:
        log('NO_SEND: reports not triggered.')
    else:
        ghl('DELETE', f'/contacts/{SEND_CONTACT}/tags', {'tags': [SEND_TAG]})
        time.sleep(3)
        ghl('POST', f'/contacts/{SEND_CONTACT}/tags', {'tags': [SEND_TAG]})
        log('Report send triggered (email + Slack workflows).')

    state['processed'].append(uuid)
    state['last_run'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    json.dump(state, open(STATE_PATH, 'w'), indent=2)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('ERROR:', str(e)[:300])
        sys.exit(1)
