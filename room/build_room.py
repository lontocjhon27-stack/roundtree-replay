"""Build the evergreen-room-style replay page that reads replay.json.

Input : D:/PerfLogs/roundtree-masterclass/evergreen-room/room-v2.html (the room design, with contact_id support)
Output: room-replay.html  (paste into the GHL custom-code element of roundtreemasterclass.com/replay-8640)
"""
import re
import sys

CONFIG_URL = sys.argv[1] if len(sys.argv) > 1 else \
    'https://raw.githubusercontent.com/lontocjhon27-stack/roundtree-replay/main/replay.json'
src = open('D:/PerfLogs/roundtree-masterclass/evergreen-room/room-v2.html', encoding='utf-8').read()
out = src


def sub1(pattern, repl, flags=0):
    global out
    out, n = re.subn(pattern, repl, out, count=1, flags=flags)
    assert n == 1, pattern


sub1(r"var VIDEO_URL = '[^']*';", "var VIDEO_URL = CFG.video_url;")
sub1(r"var START_AT  = [^;]+;[^\n]*", "var START_AT  = CFG.start_at || 0;              // first caption = Will starts")
sub1(r"var WEBHOOK_URL = '[^']*';", "var WEBHOOK_URL = CFG.webhook_url || '';       // set in replay.json to send watch events to GHL")
sub1(r"var LIVE_ATTENDEES = null;", "var LIVE_ATTENDEES = CFG.live_attendees || null;")
sub1(r"var FEATURED = \[[\s\S]*?\n  \];", "var FEATURED = (CFG.featured || []).map(function(x){ return [fmtT(x[0]), x[1]]; });")
sub1(r"var OFFER_AT = [^;]+;", "var OFFER_AT = CFG.offer_at || CFG.promo_at || 0;")
sub1(r"var CHAT = \[[\s\S]*?\n  \];", "var CHAT = (CFG.room_chat || []).slice();")
sub1(r"var TEAM = \[[\s\S]*?\n  \];",
     "var TEAM = [[START_AT + 10, 'Welcome in! 👋 Drop your city below.']];\n"
     "  if (FEATURED.length) TEAM.push([CFG.featured[0][0] + 90, 'On your phone? Every link is in the 🎁 Resources tab — just tap.']);")
# progress is per recording (a new replay each week), and separate from the evergreen room's storage
sub1(r"key:'fcwr2:' \+ \(who\.email \|\| 'anon'\)", "key:'fcrp:' + (CFG.meeting_id || '') + ':' + (who.email || who.contact_id || 'anon')")

# wrap the room logic so it starts after replay.json is loaded
first = out.index('<script>\n(function(){')
out = out[:first] + '<script>\nwindow.__roomInit = function (CFG) {\n  function fmtT(s){ s = Math.floor(s); var h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60; return h + ":" + (m<10?"0":"") + m + ":" + (x<10?"0":"") + x; }' + out[first + len('<script>\n(function(){'):]
last = out.rindex('})();')
out = out[:last] + '};' + out[last + len('})();'):]

loader = """
<script>
/* ===== Replay loader: replay.json is updated automatically after every webinar ===== */
(function () {
  var CONFIG_URL = '__CONFIG_URL__';
  function getJSON(url) { return fetch(url, { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }); }
  getJSON(CONFIG_URL + '?v=' + Date.now())
    .then(function (cfg) {
      if (!cfg.room_chat_url) return cfg;
      return getJSON(cfg.room_chat_url).then(function (c) { cfg.room_chat = c; return cfg; }).catch(function () { return cfg; });
    })
    .then(function (cfg) { window.__roomInit(cfg); })
    .catch(function (e) {
      console.warn('[replay] config failed, using fallback', e);
      window.__roomInit({ video_url: '__FALLBACK_VIDEO__', start_at: __FALLBACK_START__, offer_at: __FALLBACK_OFFER__ });
    });
})();
</script>
"""
loader = (loader.replace('__CONFIG_URL__', CONFIG_URL)
          .replace('__FALLBACK_VIDEO__', 'https://files.leadconnectorhq.com/file/6afcf7abf560033dc17333d5/masterclass-replay-2026-11-04.mp4')
          .replace('__FALLBACK_START__', '344').replace('__FALLBACK_OFFER__', '5022'))
idx = out.rindex('</body>') if '</body>' in out else len(out)
out = out[:idx] + loader + out[idx:]
open('room-replay.html', 'w', encoding='utf-8', newline='\n').write(out)
print('room-replay.html', len(out), 'bytes')
