# rssfeedz -- Feed Court RSS Aggregator

Static HTML RSS aggregator that generates a wall-of-text view of RSS feeds. This is the only home-directory project with git version control.

## Commands

```bash
python3 parsefeeds.py    # Parse feeds and generate HTML
./feed.refresh.sh        # Refresh feeds (used by cron)
```

## Key Files

- `parsefeeds.py` -- Main parser, reads feedlist.txt, generates index.html and jumble.html
- `feedlist.txt` -- RSS feed URLs (one per line)
- `index.html` -- Generated sorted feed view
- `jumble.html` -- Generated randomized feed view
- `feedcourt.css` -- Styling
- `feedcourt.js` -- Client-side interactions
- `example.cron` -- Example cron configuration

## Output

- Static HTML with auto-refresh meta tag (600s)
- "More" expansion for each feed section
- Sorted and jumbled views

## Workflow

1. Edit feeds: modify `feedlist.txt`
2. Generate: `python3 parsefeeds.py`
3. View: open `index.html` or `jumble.html` in browser
4. Automate: add `feed.refresh.sh` to crontab


## Git Recon (run before reading code)

```bash
# Churn hotspots
git log --format=format: --name-only --since="1 year ago" | sort | uniq -c | sort -nr | head -20
# Bus factor
git shortlog -sn --no-merges
# Bug clusters
git log -i -E --grep="fix|bug|broken" --name-only --format= | sort | uniq -c | sort -nr | head -20
# Activity timeline
git log --format='%ad' --date=format:'%Y-%m' | sort | uniq -c
# Crisis patterns
git log --oneline --since="1 year ago" | grep -iE 'revert|hotfix|emergency|rollback'
```
