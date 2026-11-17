# Roundtree webinar replay: automatic updates

After every Wednesday LIVE Masterclass, a GitHub Action:
1. finds the newest "LIVE Masterclass" cloud recording in Will's Zoom,
2. downloads the MP4, captions (.vtt) and chat (.txt),
3. uploads them to GHL Media,
4. works out the start point (first caption, when Will starts) and when the team posts "Text READY" in the chat,
5. writes `replay.json`.

The replay page (roundtreemasterclass.com/replay-8640) reads `replay.json` on every visit, so it shows the
newest webinar without anyone editing the page.

## Files
- `scripts/update_replay.py`: the runner
- `.github/workflows/update-replay.yml`: runs Wed night, Wed late night and Thu morning (PT), or by hand (Actions → Run workflow)
- `replay.json`: what the page plays now; `replay-history.json`: past replays
- `page/replay-page.html`: the page code to paste into the GHL custom-code element (built by `page/build_page.py` from the old hand-edited code)

## One-time setup
Repository secrets (Settings → Secrets and variables → Actions):
- `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`: Zoom Marketplace → Develop → Build App → **Server-to-Server OAuth**, scopes
  `cloud_recording:read:list_user_recordings:admin` and `cloud_recording:read:recording:admin`, then Activate
- `GHL_TOKEN`: a GHL Private Integration token with **medias.write** (and medias.readonly)
- optional `SLACK_WEBHOOK_URL`

## Manual headline
`replay.json` can carry `"subtitle": "..."`. The page shows it under the title, and the runner keeps it across updates.
