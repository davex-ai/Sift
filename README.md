<p align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=2ecc71&height=200&section=header&text=Sift&fontSize=80&fontColor=ffffff&fontAlignY=38&desc=Nigerian%20Price%20Intelligence%20Bot&descAlignY=60&descSize=20" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" />
  <img src="https://img.shields.io/badge/Language-Python-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/LLM-Qwen%202.5-FF6B35?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Scrapers-5%20Stores-27AE60?style=for-the-badge" />
  <img src="https://img.shields.io/badge/DB-SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white" />
</p>

<p align="center">
  <img src="https://readme-typing-svg.herokuapp.com?font=Fira+Code&size=20&pause=1000&color=27AE60&center=true&vCenter=true&width=650&lines=Search+Jumia%2C+Konga%2C+Slot%2C+Jiji+%26+Temu+at+once;AI-powered+shopping+recommendations;Understands+Nigerian+context+%26+Naira+budgets;Price+alerts+%2B+history+tracking" />
</p>

---

## 🛍️ What is Sift?

**Sift** is an intelligent Telegram shopping bot built for Nigerian consumers. Type what you want — naturally, in plain English or Nigerian slang — and Sift simultaneously searches **Jumia, Konga, Slot, Jiji, and Temu**, deduplicates results, and delivers AI-ranked recommendations with the best value pick highlighted.

> "NEPA took light" → Sift finds UPS/generators  
> "tokunbo" → filters for used/refurbished items  
> "under 150k" → budget applied automatically

---

## 📱 Preview

<p align="center">
  
<img width="512" height="967" width="340" alt="Sift bot in action — searching Samsung phones under ₦230,000 with AI analysis" src="https://github.com/user-attachments/assets/89e935b3-ba38-48c2-bd20-bb3da7fa24b6" />

  <br/>
  <em>Sift searching for Samsung phones under ₦230,000, with AI value analysis</em>
</p>

---

## ✨ Features

### 🔍 Multi-Store Search
- Scrapes **Jumia, Konga, Slot, Jiji, and Temu** in parallel
- Smart rate limiting and Playwright fallback for JS-heavy pages
- ScraperAPI integration for anti-bot bypass

### 🧠 Intent Engine
- Understands natural language queries, slang, and vague descriptions
- Detects product category, brand, budget, and comparison mode automatically
- Ambiguity score triggers clarification flow (asks one question before searching when needed)
- Supports Nigerian shopping context: *tokunbo*, *NEPA*, local price references

### 🤖 AI Recommendations
- Powered by **Qwen/Qwen2.5-7B-Instruct** via HuggingFace Inference API
- Generates concise, opinionated recommendations with a clear winner
- Template fallback if LLM is unavailable or too slow
- Flags Jiji used-item cautions automatically

### 🔄 Deduplication & Ranking
- Cross-store CRDT-style product grouping using fuzzy title matching
- Same-source threshold: `0.85` | Cross-source threshold: `0.72`
- LLM-assisted disambiguation for uncertain matches
- Products ranked by value score (price, rating, reviews)

### 🔔 Price Alerts
- Set alerts on any product — get notified when price drops
- Persistent alert storage via SQLite
- `/alerts` command to view and manage active alerts

### 📊 Session & Conversation Flow
- Multi-turn clarification: category → budget → brand → search
- Per-user session state with rate limiting (5 queries/user/min)
- Paginated results with `Next / Prev` inline keyboard navigation
- 👍 / 👎 feedback buttons on every result

---

## 🛠️ Tech Stack

```
Bot Layer:
  - python-telegram-bot >= 21.0.0
  - Async handlers with ConversationHandler

Scraping:
  - requests + BeautifulSoup4 + lxml
  - Playwright (Chromium) for JS-rendered pages
  - ScraperAPI proxy for anti-bot bypass
  - Rate limiter: 8 req/60s, jitter 8–16s delay

LLM:
  - Qwen/Qwen2.5-7B-Instruct (HuggingFace Inference API)
  - Template fallback synthesizer

Database:
  - SQLAlchemy 2.0 + SQLite (sift.db)
  - aiosqlite for async upgrade path
  - Price history: 30-day rolling window
  - In-memory cache: 30-min TTL, 2,000 entry cap
```

---

## 📦 Project Structure

```
Sift/
├── main.py                  # Entry point (bot / test / health)
├── config.py                # All configuration & constants
├── requirements.txt
├── .env.example
├── sift.db                  # SQLite database (auto-created)
│
├── bot/
│   ├── telegram_bot.py      # Bot startup & handler registration
│   ├── handlers.py          # All command & message handlers
│   ├── session.py           # Per-user session state
│   └── formatter.py         # Message formatting helpers
│
├── pipeline/
│   ├── intent.py            # NLP intent parser
│   └── pipeline.py          # Search orchestration
│
├── scrapers/
│   ├── base.py              # BaseScraper abstract class
│   ├── jumia.py             # Jumia scraper
│   ├── konga.py             # Konga scraper
│   ├── slot.py              # Slot scraper
│   ├── jiji.py              # Jiji scraper
│   └── temu.py              # Temu scraper
│
├── normalizer/
│   ├── core.py              # Normalization pipeline
│   ├── dedup.py             # Product grouping & dedup
│   └── scorer.py            # Value scoring logic
│
├── llm/
│   └── synthesizer.py       # Qwen LLM + fallback synthesizer
│
├── db/
│   ├── models.py            # SQLAlchemy models (alerts, history)
│   └── cache.py             # In-memory TTL cache
│
└── utils/
    ├── currency.py          # ₦ parsing & formatting
    ├── headers.py           # Rotating HTTP headers
    └── rate_limiter.py      # Per-scraper rate limiter
```

---

## ⚙️ Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/sift.git
cd sift
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in your tokens:
```

```env
TELEGRAM_BOT_TOKEN=your_token_here
HF_API_TOKEN=hf_your_token_here
SCRAPERAPI_KEY=your_scraperapi_key_here
USE_PLAYWRIGHT=true
ENABLED_SCRAPERS=jumia,konga,slot,jiji,temu
USD_TO_NGN=1620
EUR_TO_NGN=1750
DB_PATH=sift.db
```

### 4. Run

```bash
# Start the bot
python main.py

# Test a search without Telegram
python main.py --test "Samsung Galaxy A15 under 200k"

# Health check all scrapers
python main.py --health
```

---

## 🧭 Architecture

```
User (Telegram)
      │
      ▼
  bot/handlers.py
      │
      ├── session.py  ← multi-turn clarification flow
      │
      ▼
  pipeline/intent.py  ← parse query → category, budget, brand
      │
      ▼
  pipeline/pipeline.py  ← fan out to all enabled scrapers
      │
  ┌───┴────────────────────────────────────┐
  │  Jumia │ Konga │ Slot │ Jiji │ Temu   │
  └───┬────────────────────────────────────┘
      │
      ▼
  normalizer/dedup.py  ← group + deduplicate
      │
      ▼
  normalizer/scorer.py  ← rank by value
      │
      ▼
  llm/synthesizer.py  ← Qwen AI recommendation
      │
      ▼
  bot/formatter.py  ← render Telegram message
      │
      ▼
 User sees ranked results + AI analysis
```

---

## 🏪 Supported Stores

| Store  | Type        | Notes                              |
|--------|-------------|------------------------------------|
| 🟠 Jumia | Marketplace | Nigeria's largest e-commerce store |
| 🔵 Konga | Marketplace | Wide electronics & appliances range |
| 🔴 Slot  | Electronics | Nigeria's top gadget retail chain  |
| 🟢 Jiji  | Classifieds | New & used (tokunbo) listings       |
| 🟣 Temu  | International | USD prices converted to ₦         |

---

## 🧠 Intent System

The intent parser understands 20+ product categories and maps natural language to structured search terms:

```
"I need something to charge my MacBook" → charger, Apple brand
"round thing content creators use" → ring light
"NEPA took light again" → generator / UPS / inverter
"tokunbo iPhone 13" → used iPhone 13, Jiji preferred
"best blender under 50k" → blender, budget ₦50,000, best_value mode
```

Ambiguity score `>= 0.65` triggers a clarification question before searching, balancing speed vs. accuracy.

---

## ⚠️ Known Limitations

- Scrapers may need selector updates as store layouts change — run `--health` periodically
- Playwright adds startup overhead (~3–5s per JS-heavy store)
- Temu listings are in USD; exchange rate is configurable but may drift
- Jiji results include used items that need manual condition inspection

---

## 💡 Adding a New Store

```python
# 1. Create scrapers/mystore.py
class MyStoreScraper(BaseScraper):
    def search(self, query: str, max_results: int = 12) -> list[Product]:
        ...

# 2. Register in scrapers/__init__.py
REGISTRY["mystore"] = MyStoreScraper

# 3. Add to .env
ENABLED_SCRAPERS=jumia,konga,slot,jiji,temu,mystore
```

That's it — the pipeline picks it up automatically.

---

## 🧑‍💻 Author

Built with 🟢 in Nigeria — for Nigerian shoppers.

Author: [Dave](https://davex-ai.vercel.app/)
Inspiration: Was looking to build a startup, sift looked like a good idea, at first glance atleast
---

## 🧪 Status

```diff
+ Intent parser:        DONE
+ Multi-store scraping: DONE
+ Deduplication:        DONE
+ LLM synthesis:        DONE
+ Telegram bot:         DONE
+ Price alerts:         DONE
+ Price history DB:     DONE
~ More store scrapers:  IN PROGRESS
```

---

## ⭐ Support

If Sift saved you money or time:

- ⭐ Star the repo
- 🍴 Fork and add your local stores
- 🐛 Open issues for broken scrapers

---

<p align="center">
  <img src="https://media.giphy.com/media/26ufdipQqU2lhNA4g/giphy.gif" width="100" />
</p>

<p align="center">
  <b>"Stop checking five tabs. Let Sift do it in one."</b>
</p>

<p align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=2ecc71&height=100&section=footer" />
</p>
