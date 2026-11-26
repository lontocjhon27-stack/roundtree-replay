"""Publish the newest Will Roundtree LIVE Masterclass recording to the replay page.

Runs in GitHub Actions after each webinar (or by hand):
  1. Zoom (Server-to-Server OAuth): find the newest "LIVE Masterclass" cloud recording
  2. Download its MP4, closed captions (.vtt) and chat (.txt)
  3. Upload all three to GHL Media (Private Integration token)
  4. Work out the start point (first caption = Will starts talking) and the "Text READY" time
  5. Write replay.json, which the replay page reads on every visit

Environment (GitHub secrets / vars):
  ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET   Zoom Server-to-Server OAuth app
  ZOOM_USER            Zoom user that hosts the webinar (default will@wemanagementservices.com)
  GHL_TOKEN            GHL Private Integration token (medias.write)
  GHL_LOCATION_ID      default pcY7ioQLTs15S0hJiOdG
  TOPIC_MATCH          regex for the meeting topic (default "LIVE Masterclass|Masterclass")
  MEETING_ID           optional: publish this meeting id / uuid instead of the newest
  FORCE=1              re-publish even if replay.json already has this meeting
  DRY_RUN=1            find + download + compute, but no uploads and no replay.json change
  SLACK_WEBHOOK_URL    optional: post "replay is live" to Slack
"""
import base64
import datetime as dt
import json
import os
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

import urllib.parse
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, 'replay.json')
HISTORY_PATH = os.path.join(ROOT, 'replay-history.json')
PT = ZoneInfo('America/Los_Angeles')

ZOOM_USER = os.environ.get('ZOOM_USER', 'will@wemanagementservices.com')
GHL_LOCATION_ID = os.environ.get('GHL_LOCATION_ID', 'pcY7ioQLTs15S0hJiOdG')
TOPIC_RE = re.compile(os.environ.get('TOPIC_MATCH', r'LIVE Masterclass|Masterclass'), re.I)
DRY = os.environ.get('DRY_RUN') == '1'
FORCE = os.environ.get('FORCE') == '1'
MIN_MINUTES = 45                       # ignore short test meetings
TEAM_RE = re.compile(r'Will Roundtree|We Management|Jhon|Fundable CEO Team|Kristin', re.I)


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


def zoom_recordings(token, days=14):
    today = dt.date.today()
    r = requests.get(f'https://api.zoom.us/v2/users/{ZOOM_USER}/recordings',
                     params={'from': str(today - dt.timedelta(days=days)), 'to': str(today), 'page_size': 100},
                     headers={'Authorization': f'Bearer {token}'}, timeout=30)
    r.raise_for_status()
    return r.json().get('meetings', [])


STAFF_RE = re.compile(r'wemanagementservices\.com|wroundtree|fireflies|notetaker|otter\.ai|read\.ai|'
                      r'usharplynn|kristin gaxiola|sylvia lewis|lontocjhon|bryanp1430', re.I)
MIN_REAL_ATTENDEES = 3                 # an empty room (only staff / note-taker bots) is not a webinar


def real_attendees(token, uuid):
    """Unique non-staff participants of a past meeting instance (0 if Zoom has no data)."""
    u = urllib.parse.quote(urllib.parse.quote(uuid, safe=''), safe='') if uuid.startswith('/') or '//' in uuid else urllib.parse.quote(uuid, safe='')
    seen, nxt = set(), ''
    for _ in range(20):
        params = {'page_size': 300}
        if nxt:
            params['next_page_token'] = nxt
        r = requests.get(f'https://api.zoom.us/v2/past_meetings/{u}/participants', params=params,
                         headers={'Authorization': f'Bearer {token}'}, timeout=30)
        if r.status_code >= 400:
            log(f'Participants check failed ({r.status_code}); treating as 0 real attendees.')
            return 0
        j = r.json()
        for p in j.get('participants', []):
            email, name = (p.get('user_email') or '').strip().lower(), (p.get('name') or '').strip()
            if STAFF_RE.search(email) or STAFF_RE.search(name):
                continue
            seen.add(email or 'name:' + name.lower())
        nxt = j.get('next_page_token') or ''
        if not nxt:
            break
    return len(seen)


def pick_meeting(meetings, token=None):
    want = os.environ.get('MEETING_ID', '').replace(' ', '')
    good = []
    for m in meetings:
        if want and want not in (str(m.get('id')), m.get('uuid')):
            continue
        if not want and (not TOPIC_RE.search(m.get('topic', '')) or m.get('duration', 0) < MIN_MINUTES):
            continue
        files = m.get('recording_files', [])
        mp4 = [f for f in files if f.get('file_type') == 'MP4' and f.get('status') == 'completed']
        if not mp4:
            continue
        good.append(m)
    good.sort(key=lambda m: m['start_time'], reverse=True)
    for m in good:
        # Guard (added after Nov 25 2026): the recurring room can open on an off-week (note-taker bot +
        # join-before-host) and record for hours with nobody in it. Never publish such a recording.
        if want or FORCE or token is None:
            return m
        n = real_attendees(token, m['uuid'])
        if n >= MIN_REAL_ATTENDEES:
            return m
        log(f"Skipping {m['start_time']} ({m.get('duration')} min): only {n} real attendee(s), not a webinar.")
    return None


def best_file(files, ftype, prefer=None):
    c = [f for f in files if f.get('file_type') == ftype and f.get('status', 'completed') == 'completed']
    if prefer:
        c.sort(key=lambda f: 0 if f.get('recording_type') == prefer else 1)
    else:
        c.sort(key=lambda f: -(f.get('file_size') or 0))
    return c[0] if c else None


def download(token, f, path):
    with requests.get(f['download_url'], headers={'Authorization': f'Bearer {token}'}, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(path, 'wb') as out:
            for chunk in r.iter_content(1 << 20):
                out.write(chunk)
    log(f'  downloaded {os.path.basename(path)} ({os.path.getsize(path) // 1048576} MB)')
    return path


# ---------------------------------------------------------------- timing from captions / chat
def to_sec(ts):
    h, m, s = ts.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def first_caption_second(vtt_text):
    m = re.search(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->', vtt_text)
    return max(0, int(to_sec(m.group(1))) - 1) if m else 0


def ready_second(chat_text):
    """When the team first drops 'READY' in the chat (the page opens its 20%-off pop-up there)."""
    msgs, cur = [], None
    for line in chat_text.splitlines():
        m = re.match(r'^(\d{2}:\d{2}:\d{2})\s+([^:\n]+?):\s*(.*)$', line)
        if m:
            cur = [to_sec(m.group(1)), m.group(2), m.group(3)]
            msgs.append(cur)
        elif cur:
            cur[2] += '\n' + line               # multi-line message continues
    for t, author, text in msgs:
        if TEAM_RE.search(author) and re.search(r'\bREADY\b', text):
            return int(t)
    return None


def caption_cues(vtt_text):
    out = []
    for m in re.finditer(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->[^\n]*\n([^\n]+(?:\n[^\n]+)*)', vtt_text):
        out.append((int(to_sec(m.group(1))), m.group(2).replace('\n', ' ')))
    return out


def featured_moments(vtt_text, offer_at=None):
    """[[sec, key], ...]: when Will shows a QR code, which link the room should feature (scan / call / join)."""
    cues = caption_cues(vtt_text)
    found = []
    for i, (t, x) in enumerate(cues):
        if not re.search(r'\bQR\b|scan (this|that|it|the)', x, re.I):
            continue
        if found and t - found[-1][0] < 180:
            continue
        ctx = ' '.join(c[1] for c in cues[i:i + 3])
        if re.search(r'\bbook\b|game plan|call with|strategy call|sales team', ctx, re.I):
            key = 'call'
        elif re.search(r'fundab\w*\s+(score|scan)|scan your business|fundability', ctx, re.I) and not re.search(r'tier|29\.99|2,?999', ctx, re.I):
            key = 'scan'
        else:
            key = 'join'
        found.append([t, key])
    if offer_at and not any(k == 'join' and abs(t - offer_at) < 120 for t, k in found):
        found.append([offer_at, 'join'])
    found.sort()
    return found


def short_name(name):
    parts = name.strip().split()
    return parts[0] if len(parts) < 2 else f'{parts[0]} {parts[-1][0]}.'


def room_chat(chat_text, featured):
    """Room-style chat: [sec, 'First L.', 'A', text] from the real Zoom chat (attendees only, no bots, no links),
    plus a team 'tap here' card at each QR moment."""
    msgs, cur = [], None
    for line in chat_text.splitlines():
        m = re.match(r'^(\d{2}:\d{2}:\d{2})\s+([^:\n]+?):\s*(.*)$', line)
        if m:
            cur = [int(to_sec(m.group(1))), m.group(2).strip(), m.group(3)]
            msgs.append(cur)
        elif cur:
            cur[2] += '\n' + line
    out = []
    for t, author, text in msgs:
        text = re.sub(r'^Replying to "[^"]*"\s*', '', text.strip())
        if (not text or TEAM_RE.search(author) or re.search(r'fireflies|notetaker|otter|read\.ai', author, re.I)
                or text.startswith('Reacted to ') or re.search(r'https?://', text)):
            continue
        out.append([t, short_name(author), 'A', text[:300]])
    labels = {'scan': 'Scan the code on screen, or tap here 👇', 'call': 'Book your call with the team 👇',
              'join': 'Webinar pricing is live 👇'}
    repeat = {'scan': 'Free scan link again 👇', 'call': 'Not sure which tier? Book a call 👇', 'join': 'Sign up here 👇'}
    seen = set()
    for t, key in featured:
        out.append([t + 20, repeat[key] if key in seen else labels[key], 'Link', key])
        seen.add(key)
    out.sort(key=lambda r: r[0])
    return out


def offer_second(vtt_text):
    """Fallback when nobody posted READY in the chat: the moment Will reveals the webinar price."""
    for m in re.finditer(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->[^\n]*\n([^\n]+)', vtt_text):
        if re.search(r'\$?29\.99|2,?999|text (me )?ready', m.group(2), re.I):
            return int(to_sec(m.group(1)))
    return None



# ---------------------------------------------------------------- shrink big recordings (GHL Media rejects ~500 MB+ with 413)
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '400'))


def shrink_mp4(path):
    mb = os.path.getsize(path) / 1048576
    if mb <= MAX_UPLOAD_MB:
        return path
    import subprocess
    for crf, height in ((30, 720), (34, 720), (34, 540)):
        out = path.replace('.mp4', f'-{height}p-crf{crf}.mp4')
        log(f'  compressing {mb:.0f} MB -> {height}p crf {crf} ...')
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', path,
                        '-vf', f'scale=-2:{height}', '-r', '15', '-c:v', 'libx264', '-preset', 'veryfast',
                        '-crf', str(crf), '-c:a', 'aac', '-b:a', '64k', '-ac', '1', '-movflags', '+faststart', out],
                       check=True)
        new_mb = os.path.getsize(out) / 1048576
        log(f'  compressed to {new_mb:.0f} MB')
        if new_mb <= MAX_UPLOAD_MB:
            return out
    raise RuntimeError(f'Recording still over {MAX_UPLOAD_MB} MB after compression')


def scheduled_start_offset(start_pt):
    """No captions: start the replay at the scheduled top of the hour (webinar = 5:00 PM PT)."""
    top = start_pt.replace(minute=0, second=0, microsecond=0)
    if start_pt.minute >= 30:
        top += dt.timedelta(hours=1)
    off = int((top - start_pt).total_seconds())
    return off if 0 <= off <= 1800 else 0

# ---------------------------------------------------------------- GHL Media
def ghl_upload(path, name, mime):
    with open(path, 'rb') as fh:
        enc = MultipartEncoder(fields={'file': (name, fh, mime), 'name': name, 'hosted': 'false'})
        r = requests.post('https://services.leadconnectorhq.com/medias/upload-file',
                          params={'altId': GHL_LOCATION_ID, 'altType': 'location'},
                          data=enc,
                          headers={'Authorization': f"Bearer {os.environ['GHL_TOKEN']}", 'Version': '2021-07-28',
                                   'Content-Type': enc.content_type, 'Accept': 'application/json'},
                          timeout=3600)
    if not r.ok:
        raise RuntimeError(f'GHL upload {name} failed {r.status_code}: {r.text[:300]}')
    j = r.json()
    url = j.get('url') or j.get('fileUrl')
    if not url and j.get('fileId'):
        url = f"https://assets.cdn.filesafe.space/{GHL_LOCATION_ID}/media/{j['fileId']}"
    log(f'  uploaded {name} -> {url}')
    return url


# ---------------------------------------------------------------- main
def main():
    current = json.load(open(CONFIG_PATH, encoding='utf-8')) if os.path.exists(CONFIG_PATH) else {}
    token = zoom_token()
    meeting = pick_meeting(zoom_recordings(token), token)
    if not meeting:
        log('No finished LIVE Masterclass recording found in the last 14 days.')
        return 0
    log(f"Newest: {meeting['topic']} | {meeting['start_time']} | {meeting.get('duration')} min | uuid {meeting['uuid']}")
    if meeting['uuid'] == current.get('meeting_uuid') and not FORCE:
        log('Replay page already shows this recording. Nothing to do.')
        return 0

    files = meeting['recording_files']
    mp4 = best_file(files, 'MP4', prefer='shared_screen_with_speaker_view')
    cc = best_file(files, 'CC') or best_file(files, 'TRANSCRIPT')
    chat = best_file(files, 'CHAT')

    start_pt = dt.datetime.fromisoformat(meeting['start_time'].replace('Z', '+00:00')).astimezone(PT)
    stamp = start_pt.strftime('%Y-%m-%d')
    tmp = tempfile.mkdtemp()

    vtt_text = chat_text = ''
    vtt_path = chat_path = None
    if cc:
        vtt_path = download(token, cc, os.path.join(tmp, f'replay-{stamp}-captions.vtt'))
        vtt_text = open(vtt_path, encoding='utf-8', errors='replace').read()
    if chat:
        chat_path = download(token, chat, os.path.join(tmp, f'replay-{stamp}-chat.txt'))
        chat_text = open(chat_path, encoding='utf-8', errors='replace').read()

    start_at = first_caption_second(vtt_text)
    if not vtt_text:
        start_at = scheduled_start_offset(start_pt)
        log(f'  no captions/transcript in Zoom: start_at from schedule = {start_at}s')
    offer_at = offer_second(vtt_text)
    promo_at = ready_second(chat_text) or offer_at
    featured = featured_moments(vtt_text, offer_at)
    rchat = room_chat(chat_text, featured)
    room_chat_path = os.path.join(tmp, f'replay-{stamp}-room-chat.txt')
    json.dump(rchat, open(room_chat_path, 'w', encoding='utf-8'), ensure_ascii=False)
    log(f'  start_at {start_at}s  promo_at {promo_at}s  offer_at {offer_at}s  featured {featured}  room chat {len(rchat)} msgs')

    cfg = {
        'meeting_uuid': meeting['uuid'],
        'meeting_id': meeting.get('id'),
        'meeting_topic': meeting['topic'],
        'recorded_at': start_pt.isoformat(),
        'date_label': start_pt.strftime('%A, %B ') + str(start_pt.day) + start_pt.strftime(', %Y'),
        'start_at': start_at,
        'promo_at': promo_at,
        'offer_at': offer_at or promo_at,
        'featured': featured,
        'duration_min': meeting.get('duration'),
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
    }
    if current.get('subtitle'):
        cfg['subtitle'] = current['subtitle']   # manual headline stays until someone changes it

    if DRY:
        log('DRY_RUN: skipping MP4 download/uploads. Would write:', json.dumps(cfg, indent=2))
        return 0

    mp4_path = shrink_mp4(download(token, mp4, os.path.join(tmp, f'replay-{stamp}.mp4')))
    cfg['video_url'] = ghl_upload(mp4_path, f'Masterclass Replay {stamp}.mp4', 'video/mp4')
    if vtt_path:
        # GHL Media rejects .vtt files, so captions go up as .txt (the page only reads the text)
        cfg['vtt_url'] = ghl_upload(vtt_path, f'Masterclass Replay {stamp} captions.txt', 'text/plain')
    if chat_path:
        cfg['chat_url'] = ghl_upload(chat_path, f'Masterclass Replay {stamp} chat.txt', 'text/plain')
    # room-style chat (first name + last initial) lives in GHL, not in the public repo
    cfg['room_chat_url'] = ghl_upload(room_chat_path, f'Masterclass Replay {stamp} room-chat.txt', 'text/plain')

    json.dump(cfg, open(CONFIG_PATH, 'w', encoding='utf-8'), indent=2)
    hist = json.load(open(HISTORY_PATH, encoding='utf-8')) if os.path.exists(HISTORY_PATH) else []
    hist.insert(0, cfg)
    json.dump(hist[:52], open(HISTORY_PATH, 'w', encoding='utf-8'), indent=2)
    log('replay.json updated.')

    hook = os.environ.get('SLACK_WEBHOOK_URL')
    if hook:
        requests.post(hook, json={'text': f"Replay for {cfg['date_label']} is live: https://roundtreemasterclass.com/replay-8640"}, timeout=20)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        open(summary, 'a', encoding='utf-8').write(f"### Replay updated\n- {cfg['meeting_topic']}\n- {cfg['date_label']}\n- starts at {start_at}s\n- video: {cfg['video_url']}\n")
    return 0


if __name__ == '__main__':
    sys.exit(main())
