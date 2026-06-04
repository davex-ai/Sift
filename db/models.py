"""
Database models — SQLite via SQLAlchemy.

Tables:
  product         — canonical product registry
  price_snapshot  — daily price per product per store (history)
  alert           — user price alerts
  scraper_health  — selector health tracking

Upgrade path: swap SQLite URI for PostgreSQL with zero code changes.
"""

import logging
from datetime import datetime
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, Boolean, Text, ForeignKey, Index,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from typing import Optional

import config

logger = logging.getLogger(__name__)

Base = declarative_base()
_engine = None
_Session = None


# ══════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════

class ProductRecord(Base):
    """
    Canonical product — deduped across all stores.
    One row per unique physical product.
    """
    __tablename__ = "product"

    id = Column(String(8), primary_key=True)        # ProductGroup.id
    canonical_title = Column(String(500), nullable=False)
    brand = Column(String(100))
    model = Column(String(200))
    category = Column(String(100))
    specs_json = Column(Text, default="{}")          # JSON specs dict
    image_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    price_history = relationship("PriceSnapshot", back_populates="product", lazy="dynamic")
    alerts = relationship("PriceAlert", back_populates="product", lazy="dynamic")

    __table_args__ = (
        Index("ix_product_title", "canonical_title"),
        Index("ix_product_brand", "brand"),
    )


class PriceSnapshot(Base):
    """
    Price at a point in time from a specific store.
    This is how we build price history.
    One row per (product, store, day).
    """
    __tablename__ = "price_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(8), ForeignKey("product.id"), nullable=False)
    store = Column(String(50), nullable=False)
    price_ngn = Column(Float, nullable=False)
    url = Column(String(500))
    availability = Column(String(30), default="unknown")
    scraped_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("ProductRecord", back_populates="price_history")

    __table_args__ = (
        Index("ix_snapshot_product_store", "product_id", "store"),
        Index("ix_snapshot_scraped_at", "scraped_at"),
    )


class PriceAlert(Base):
    """
    User-set price alert.
    'Notify me when PS5 drops below ₦500k'
    """
    __tablename__ = "price_alert"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)           # Telegram user ID
    product_id = Column(String(8), ForeignKey("product.id"), nullable=False)
    product_title = Column(String(500))                 # denormalized for display
    threshold_ngn = Column(Float, nullable=False)
    target_store = Column(String(50))                   # None = any store
    is_active = Column(Boolean, default=True)
    triggered_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("ProductRecord", back_populates="alerts")

    __table_args__ = (
        Index("ix_alert_user", "user_id"),
        Index("ix_alert_active", "is_active"),
    )


class ScraperHealth(Base):
    """
    Tracks scraper success/failure over time.
    Used to detect when a site changes its HTML structure.
    """
    __tablename__ = "scraper_health"

    id = Column(Integer, primary_key=True, autoincrement=True)
    store = Column(String(50), nullable=False)
    checked_at = Column(DateTime, default=datetime.utcnow)
    result_count = Column(Integer, default=0)
    success = Column(Boolean, default=True)
    error_msg = Column(Text)
    fetch_method = Column(String(30))   # scraperapi | playwright | plain

    __table_args__ = (
        Index("ix_health_store", "store"),
        Index("ix_health_checked_at", "checked_at"),
    )


# ══════════════════════════════════════════════════════════════
# Engine + session factory
# ══════════════════════════════════════════════════════════════

def init_db(db_path: str = None) -> None:
    """Create tables if they don't exist. Call once at startup."""
    global _engine, _Session

    path = db_path or config.DB_PATH
    db_url = f"sqlite:///{path}"

    _engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine)
    logger.info(f"[DB] Initialized: {db_url}")


@contextmanager
def session_scope():
    """Provide a transactional session scope."""
    if _Session is None:
        init_db()
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def save_price_snapshots(groups, scraped_at: datetime = None) -> None:
    """Persist current prices from ProductGroups into price_snapshot."""
    from normalizer.dedup import ProductGroup
    import json

    ts = scraped_at or datetime.utcnow()

    with session_scope() as db:
        for g in groups:
            # Upsert product record
            existing = db.query(ProductRecord).filter_by(id=g.id).first()
            if not existing:
                record = ProductRecord(
                    id=g.id,
                    canonical_title=g.canonical_title,
                    brand=g.brand,
                    model=g.model,
                    specs_json=json.dumps(g.specs),
                    image_url=g.image_url,
                )
                db.add(record)
            else:
                existing.updated_at = ts

            # Price snapshots per store
            for source in g.sources:
                if source.price_ngn:
                    snap = PriceSnapshot(
                        product_id=g.id,
                        store=source.store,
                        price_ngn=source.price_ngn,
                        url=source.url,
                        availability=source.availability,
                        scraped_at=ts,
                    )
                    db.add(snap)


def get_price_history(product_id: str, days: int = 30) -> list[dict]:
    """Return price history for a product over the last N days."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=days)
    with session_scope() as db:
        rows = (
            db.query(PriceSnapshot)
            .filter(
                PriceSnapshot.product_id == product_id,
                PriceSnapshot.scraped_at >= cutoff,
            )
            .order_by(PriceSnapshot.scraped_at)
            .all()
        )
        return [
            {
                "store": r.store,
                "price_ngn": r.price_ngn,
                "scraped_at": r.scraped_at.isoformat(),
            }
            for r in rows
        ]


def get_30d_average(product_id: str, store: str = None) -> Optional[float]:
    """Average price over last 30 days (optionally per store)."""
    from datetime import timedelta
    from sqlalchemy import func

    cutoff = datetime.utcnow() - timedelta(days=30)
    with session_scope() as db:
        q = db.query(func.avg(PriceSnapshot.price_ngn)).filter(
            PriceSnapshot.product_id == product_id,
            PriceSnapshot.scraped_at >= cutoff,
        )
        if store:
            q = q.filter(PriceSnapshot.store == store)
        result = q.scalar()
        return float(result) if result else None


def get_active_alerts(user_id: int = None) -> list[PriceAlert]:
    """Return all active alerts, optionally filtered by user."""
    with session_scope() as db:
        q = db.query(PriceAlert).filter_by(is_active=True)
        if user_id:
            q = q.filter_by(user_id=user_id)
        return q.all()


# Typing hint
from typing import Optional
