# Kitchen Hub

An always-on kitchen dashboard: clock, weather (Open-Meteo), 12-hour and 4-day forecast, and news headlines.

- **Change town, clock style, night hours or news feeds:** edit `config.json`. Saving it re-runs the news job.
- **News:** `.github/workflows/news.yml` runs `scripts/fetch_news.py` every 30 minutes and commits `news.json`.
- **Run news now:** Actions tab > Update news > Run workflow.
- Cost: $0 (GitHub Pages + Actions on a public repo, Open-Meteo free API, no keys).
