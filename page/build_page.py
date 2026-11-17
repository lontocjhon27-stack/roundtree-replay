"""Turn the hand-edited replay page code into a data-driven version.

Input : original-custom-code.html  (the live custom-code block of roundtreemasterclass.com/replay-8640)
Output: replay-page.html           (paste into the GHL custom-code element)
        sample-data/oct7.vtt, sample-data/oct7-chat.txt (the Oct 7 data pulled out of the old code)

The new page loads replay.json (CONFIG_URL), then the captions and chat files it points to,
and only then starts the original page logic with those values. Nothing else about the page changes.
"""
import json
import os
import re
import sys

CONFIG_URL = sys.argv[1] if len(sys.argv) > 1 else \
    'https://raw.githubusercontent.com/lontocjhon27-stack/roundtree-replay/main/replay.json'

src = open('original-custom-code.html', encoding='utf-8').read()


def cut_template_literal(text, var):
    """Return (new_text, literal_body) with `var NAME = `...`;` replaced by a CFG lookup."""
    m = re.search(r'var ' + var + r' = `', text)
    start = m.start()
    body_start = m.end()
    end = text.index('`;', body_start)
    body = text[body_start:end]
    return text, body, start, end + 2


out = src

# 1. pull the Oct 7 captions + chat out of the code (kept as sample data / first replay.json)
_, vtt, s, e = cut_template_literal(out, 'VTT_RAW')
out = out[:s] + "var VTT_RAW = CFG.vtt || '';" + out[e:]
_, chat, s, e = cut_template_literal(out, 'REAL_CHAT_RAW')
out = out[:s] + "var REAL_CHAT_RAW = CFG.chat || '';" + out[e:]

# 2. start point + promo time come from the config
out, n1 = re.subn(r'var START_AT = 12 \* 60 \+ 30;[^\n]*',
                  'var START_AT = CFG.start_at || 0; // from replay.json (first caption = Will starts)', out)
out, n2 = re.subn(r'var PROMO_VIDEO_TIME = [^;]+;[^\n]*',
                  'var PROMO_VIDEO_TIME = CFG.promo_at || (1 * 3600 + 38 * 60 + 48); // from replay.json', out)
assert n1 == 1 and n2 == 1, (n1, n2)

# 3. the video source is set by the loader
out = re.sub(r'<source src="[^"]*" type="video/mp4">', '<source id="replaySource" src="" type="video/mp4">', out, count=1)

# 4. session-specific wording -> neutral / config-driven
out = out.replace('<div class="eyebrow">A Fundable CEO Original Broadcast</div>',
                  '<div class="eyebrow">A Fundable CEO Original Broadcast<span id="replayDate"></span></div>', 1)
out = out.replace('<p class="hero-sub">', '<p class="hero-sub" id="replaySub">', 1)
out = out.replace('Clients who followed the framework, featured on this session.',
                  'Clients who followed the framework.', 1)
out = out.replace(' Marlon shared it live on this session.', '', 1)

# 5. wrap the original logic so it runs after the config is loaded
first = out.index('<script>')
iife = out.index('(function(){', first)
out = out[:iife] + 'window.__replayInit = function (CFG) {' + out[iife + len('(function(){'):]
last = out.rindex('})();')
out = out[:last] + '};' + out[last + len('})();'):]

loader = r"""
<script>
/* ===== Replay loader: reads replay.json (updated automatically after each webinar) ===== */
(function () {
  var CONFIG_URL = '__CONFIG_URL__';
  function txt(url) { return url ? fetch(url).then(function (r) { return r.ok ? r.text() : ''; }).catch(function () { return ''; }) : Promise.resolve(''); }
  function start(cfg) {
    var video = document.getElementById('replayVideo');
    var source = document.getElementById('replaySource');
    if (cfg.video_url) { source.src = cfg.video_url + '#t=' + (cfg.start_at || 0); video.load(); }
    if (cfg.date_label) { var d = document.getElementById('replayDate'); if (d) d.textContent = ' · Recorded ' + cfg.date_label; }
    if (cfg.subtitle) { var s = document.getElementById('replaySub'); if (s) s.textContent = cfg.subtitle; }
    window.__replayInit(cfg);
  }
  fetch(CONFIG_URL + (CONFIG_URL.indexOf('?') < 0 ? '?' : '&') + 'v=' + Date.now(), { cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error('config ' + r.status); return r.json(); })
    .then(function (cfg) {
      return Promise.all([txt(cfg.vtt_url), txt(cfg.chat_url)]).then(function (res) {
        cfg.vtt = res[0]; cfg.chat = res[1]; start(cfg);
      });
    })
    .catch(function (e) {
      console.warn('[replay] config failed, using fallback', e);
      start({ video_url: '__FALLBACK_VIDEO__', start_at: __FALLBACK_START__ });
    });
})();
</script>
"""

fallback = json.load(open('sample-data/replay.sample.json', encoding='utf-8')) if os.path.exists('sample-data/replay.sample.json') else {
    'video_url': 'https://assets.cdn.filesafe.space/pcY7ioQLTs15S0hJiOdG/media/6ac7de0089da6e6f9bbabbfb.mp4', 'start_at': 750}
loader = (loader.replace('__CONFIG_URL__', CONFIG_URL)
          .replace('__FALLBACK_VIDEO__', fallback['video_url'])
          .replace('__FALLBACK_START__', str(fallback['start_at'])))
out = out + loader

os.makedirs('sample-data', exist_ok=True)
open('sample-data/oct7.vtt', 'w', encoding='utf-8', newline='\n').write(vtt)
open('sample-data/oct7-chat.txt', 'w', encoding='utf-8', newline='\n').write(chat)
open('replay-page.html', 'w', encoding='utf-8', newline='\n').write(out)
print('replay-page.html', len(out), 'bytes (was', len(src), ')  vtt', len(vtt), ' chat', len(chat))
