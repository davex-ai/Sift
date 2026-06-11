import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
SCRAPERAPI_KEY: str     = os.getenv("SCRAPERAPI_KEY", "")
HF_API_TOKEN: str       = os.getenv("HF_API_TOKEN", "")

LLM_MODEL: str       = "Qwen/Qwen2.5-7B-Instruct"
LLM_MAX_TOKENS: int  = 600
LLM_TEMPERATURE: float = 0.65
LLM_TIMEOUT: int     = 30
LLM_FALLBACK_ENABLED: bool = True

SCRAPERAPI_BASE: str   = "http://api.scraperapi.com"
USE_PLAYWRIGHT: bool   = os.getenv("USE_PLAYWRIGHT", "true").lower() == "true"

RATE_LIMIT_REQUESTS: int        = 8
RATE_LIMIT_WINDOW: int          = 60
MAX_CONCURRENT_PER_SCRAPER: int = 1
MAX_CONCURRENT_TOTAL: int       = 5
MIN_DELAY_SECONDS: float        = 8.0
MAX_DELAY_SECONDS: float        = 16.0
DELAY_JITTER: float             = 1.5
MAX_RETRIES: int                = 3
RETRY_BACKOFF: float            = 2.0

MAX_RESULTS_PER_STORE: int = 12
TOP_N_RESULTS: int         = 10

DEDUP_SAME_SOURCE_THRESHOLD: float  = 0.85
DEDUP_CROSS_SOURCE_THRESHOLD: float = 0.72
DEDUP_UNCERTAIN_LOW: float          = 0.60
DEDUP_LLM_CHECK: bool               = True

USD_TO_NGN: float = float(os.getenv("USD_TO_NGN", "1620"))
EUR_TO_NGN: float = float(os.getenv("EUR_TO_NGN", "1750"))

DB_PATH: str             = os.getenv("DB_PATH", "sift.db")
PRICE_HISTORY_DAYS: int  = 30

CACHE_TTL_SECONDS: int  = 1800
CACHE_MAX_ENTRIES: int  = 2000

BOT_RATE_LIMIT_PER_USER: int = 5
BOT_PAGE_SIZE: int           = 5
BOT_MAX_QUERY_LENGTH: int    = 200

ENABLED_SCRAPERS: list[str] = [
    s.strip()
    for s in os.getenv("ENABLED_SCRAPERS", "jumia,konga,slot,jiji,temu").split(",")
]

ALERT_ON_ZERO_RESULTS: bool  = True
MIN_EXPECTED_RESULTS: int    = 3
HEALTH_CHECK_INTERVAL: int   = 3600