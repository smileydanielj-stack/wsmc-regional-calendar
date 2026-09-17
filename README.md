# WSMC Regional Calendar

A regional maritime and response calendar for wsmcoop.org.

## What is here

| File | Purpose |
|---|---|
| `build_calendar.py` | Aggregator. Standard-library Python, no installs. Run `python3 build_calendar.py`. |
| `wsmc_events.json` | Hand-maintained WSMC events (conference, WSMC-hosted training). Edit this by hand. |
| `events.json` | Output: normalized upcoming events. The web page reads this. |
| `calendar.ics` | Output: the same events as a subscribable iCal feed. |
| `wsmc-regional-calendar.html` | The Squarespace code-block page. The build embeds a data snapshot in it. |

## Sources

1. **WA Ecology NACES** (https://apps.ecology.wa.gov/naces/) is the source of truth for drills and exercises.
   The script requests the List View for this year and next with an ASP.NET postback.
   Drill coordinator names and phone numbers are deliberately left out.
2. **Marine Exchange of Puget Sound "WA Maritime" Google Calendar** supplies port, agency, and industry events
   through its public iCal feed. Its copies of Ecology drills are dropped because they lag NACES.
3. **Marine Exchange committee pages** supply meetings that are not in the Google Calendar:
   the Puget Sound Marine Firefighting Commission (a dated list plus one sentence giving the time) and the
   Puget Sound Harbor Safety Committee (one "Upcoming Meeting" block, plus the yearly Meetings Archive PDF,
   which lists dates before they are announced). The PDF step needs `pdftotext` (poppler) and is skipped without it.
   Teams join links are not republished; each event links back to the committee page.
4. **`wsmc_events.json`** supplies WSMC's own events. Anything with "WSMC" in the title is highlighted.

## How the page gets data

The page has two settings near the top of its script: `DATA_URL` and `ICS_URL`.
When `DATA_URL` is empty the page uses the snapshot embedded by the last build.
When it points at a hosted `events.json` the page loads fresh data and falls back to the snapshot on failure.
Setting `ICS_URL` reveals the Subscribe button.

## Hosting

GitHub Actions (`.github/workflows/build.yml`) rebuilds the feed twice a day and publishes it with GitHub Pages:

- https://smileydanielj-stack.github.io/wsmc-regional-calendar/events.json
- https://smileydanielj-stack.github.io/wsmc-regional-calendar/calendar.ics
- https://smileydanielj-stack.github.io/wsmc-regional-calendar/ (the page itself, handy for checking)

Data files are committed back only when events actually change. If a source is down, its last known events are
carried forward, the site still updates, and the run is marked failed so GitHub emails the repository owner.
Run it by hand from the Actions tab with "Run workflow".

## Preview locally

`python3 -m http.server 8765` in this folder, then open http://localhost:8765/wsmc-regional-calendar.html
