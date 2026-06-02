# Shopping Scraper Pipeline

Modular scraper for Nigerian shopping assistant. Jumia + Amazon in parallel,
normalized to NGN, ranked by relevance + budget fit.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Create a `.env` file:
```
SCRAPERAPI_KEY=your_key_here          # scraperapi.com — 1000 free requests
EXCHANGERATE_API_KEY=your_key_here    # exchangerate-api.com — free tier
```

## Quick Test

```bash
python shopping-scraper/test_pipeline.py
```

## Usage

```python
from shopping_scraper import ShoppingPipeline

pipeline = ShoppingPipeline()  # reads keys from .env
results = pipeline.search("laptop under 500k")

for r in results:
    print(r.rank, r.title, r.price_display, r.source)
```

## Adding a New Scraper (e.g. Konga)

1. Create `scrapers/konga.py`
2. Subclass `BaseScraper`, implement `search()`
3. Register in `__init__.py`:
   ```python
   self._scrapers["konga"] = KongaScraper(...)
   ```
Done. The pipeline handles parallelism, dedup, and ranking automatically.

## Architecture

```
User query
    ↓
extract_budget()          # "500k" → 500_000 NGN
    ↓
ThreadPoolExecutor        # All scrapers run in parallel
  ├── JumiaScraper        # ScraperAPI → Playwright fallback
  └── AmazonScraper       # ScraperAPI autoparse → Playwright fallback
    ↓
Normalizer
  ├── Filter (no title/garbage)
  ├── Deduplicate (85% title similarity)
  ├── Score (relevance + budget + rating + availability)
  └── Rank + return top N
```

## Selector Maintenance

When scrapers break (they will), the selectors are in `SELECTORS` dict
at the top of each scraper file. Open devtools on the site, find the
new class names, update the dict. Nothing else changes.
