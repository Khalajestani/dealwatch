"""SQLAlchemy models. All datetimes are naive UTC (SQLite has no tz support); convert for display only."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, and_
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --- enumerations kept as plain strings so the DB stays readable -------------------------

CATEGORIES: list[tuple[str, str]] = [
    ("household", "Household & cleaning (paper, detergents, cleaners)"),
    ("personal_care", "Personal care (shampoo, toothpaste, razors)"),
    ("groceries", "Groceries & drinks (long-life food, coffee, drinks)"),
    ("health", "Health & pharmacy (vitamins, OTC, first aid)"),
    ("tools_diy", "Tools & DIY (power tools, hardware, garden)"),
    ("electronics", "Electronics & appliances"),
    ("home_garden", "Home & garden (furniture, textiles, garden)"),
    ("pets_baby", "Pets & baby"),
    ("other", "Other"),
]
CATEGORY_KEYS = [key for key, _ in CATEGORIES]
CATEGORY_SHORT = {
    "household": "Household",
    "personal_care": "Personal care",
    "groceries": "Groceries",
    "health": "Health",
    "tools_diy": "Tools & DIY",
    "electronics": "Electronics",
    "home_garden": "Home & garden",
    "pets_baby": "Pets & baby",
    "other": "Other",
}
# icon symbol id (see web/templates/_icons.html) and colour class per category - keep the UI consistent
CATEGORY_ICON = {
    "household": "droplet",
    "personal_care": "heart",
    "groceries": "bag",
    "health": "activity",
    "tools_diy": "tool",
    "electronics": "cpu",
    "home_garden": "home",
    "pets_baby": "smile",
    "other": "tag",
}

# Bought again and again: on these the one-click button is Bought it (skips the item for a while). Anything else is
# bought once, so the button is Already bought (moves it to Purchased for good). The item page offers both.
REFILL_CATEGORIES = {"household", "personal_care", "groceries", "health", "pets_baby"}

PRIORITIES: list[tuple[str, str]] = [
    ("need", "Need - running low, buy soon at the best available price"),
    ("stock_up", "Stock up - buy when there is a real deal"),
    ("wishlist", "Wishlist - only tell me about great deals"),
]
PRIORITY_KEYS = [key for key, _ in PRIORITIES]

UNITS: list[str] = ["piece", "roll", "wash", "tablet", "capsule", "bag", "ml", "l", "g", "kg", "m", "pair"]

RETAILER_STATUSES = ["enabled", "disabled", "blocked"]
RUN_STATUSES = ["running", "success", "partial", "failed"]
PROMO_TYPES = ["none", "sale", "multibuy", "bundle", "bulk", "clearance", "member"]


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(50), default="household")
    search_hints: Mapped[str | None] = mapped_column(Text)  # e.g. "8 rollen, 3-laags" / model numbers
    unit: Mapped[str | None] = mapped_column(String(20))  # unit used for per-unit price comparison
    bulk_ok: Mapped[bool] = mapped_column(Boolean, default=True)  # consider household multi-packs
    priority: Mapped[str] = mapped_column(String(20), default="stock_up")
    target_price: Mapped[float | None] = mapped_column(Float)  # "tell me when <= this" (per pack/product)
    target_unit_price: Mapped[float | None] = mapped_column(Float)  # "tell me when <= this per unit"
    check_interval_days: Mapped[int | None] = mapped_column(Integer)  # None -> derived from priority
    notes: Mapped[str | None] = mapped_column(Text)
    reference_url: Mapped[str | None] = mapped_column(Text)  # product page the item was added from (extra search hint)
    image_path: Mapped[str | None] = mapped_column(String(300))  # photo file name under data/uploads
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # False: paused
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | bought (for good, kept as history)
    bought_at: Mapped[datetime | None] = mapped_column(DateTime)  # when it became bought
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime)  # skip checks until this date
    last_bought_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    observations: Mapped[list[Observation]] = relationship(back_populates="item", cascade="all, delete-orphan")
    price_points: Mapped[list[PricePoint]] = relationship(back_populates="item", cascade="all, delete-orphan")

    @property
    def display_name(self) -> str:
        return f"{self.brand} {self.name}".strip() if self.brand else self.name

    @property
    def is_refill(self) -> bool:
        return self.category in REFILL_CATEGORIES

    @hybrid_property
    def watched(self) -> bool:
        """Checked and reported: not paused and not bought for good."""
        return bool(self.active) and self.status != "bought"

    @watched.inplace.expression
    @classmethod
    def _watched_expression(cls):
        return and_(cls.active.is_(True), cls.status != "bought")


class Retailer(Base):
    __tablename__ = "retailers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(200), unique=True)  # bare domain, e.g. "bol.com"
    status: Mapped[str] = mapped_column(String(20), default="enabled")  # enabled | disabled | blocked
    categories: Mapped[str | None] = mapped_column(Text)  # comma-separated category keys; empty = all
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def category_list(self) -> list[str]:
        return [c.strip() for c in (self.categories or "").split(",") if c.strip()]

    def serves(self, category: str) -> bool:
        cats = self.category_list
        return not cats or category in cats


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | schedule
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    model: Mapped[str | None] = mapped_column(String(100))
    effort: Mapped[str | None] = mapped_column(String(20))
    search_mode: Mapped[str | None] = mapped_column(String(20))
    groups_planned: Mapped[int] = mapped_column(Integer, default=0)
    groups_completed: Mapped[int] = mapped_column(Integer, default=0)
    groups_failed: Mapped[int] = mapped_column(Integer, default=0)
    items_planned: Mapped[int] = mapped_column(Integer, default=0)
    items_checked: Mapped[int] = mapped_column(Integer, default=0)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    deal_count: Mapped[int] = mapped_column(Integer, default=0)
    web_lookups: Mapped[int] = mapped_column(Integer, default=0)  # searches and page reads by Claude
    error: Mapped[str | None] = mapped_column(Text)
    plan_json: Mapped[str | None] = mapped_column(Text)  # what was planned (groups, domains, budgets)
    log_json: Mapped[str | None] = mapped_column(Text)  # queries, per-group errors, dropped offers

    observations: Mapped[list[Observation]] = relationship(back_populates="run", cascade="all, delete-orphan")
    price_points: Mapped[list[PricePoint]] = relationship(back_populates="run", cascade="all, delete-orphan")
    run_items: Mapped[list[RunItem]] = relationship(back_populates="run", cascade="all, delete-orphan")
    report: Mapped[Report | None] = relationship(back_populates="run", uselist=False, cascade="all, delete-orphan")

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class RunItem(Base):
    """One row per (run, item) that was actually checked - keeps 'not found' history too."""

    __tablename__ = "run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    group_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="found")  # found | not_found | error
    note: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="run_items")
    item: Mapped[Item] = relationship()


class Observation(Base):
    """A price seen for an item at a retailer during a run. History of these is the price memory."""

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    retailer_id: Mapped[int | None] = mapped_column(ForeignKey("retailers.id", ondelete="SET NULL"))
    retailer_domain: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="EUR")
    list_price: Mapped[float | None] = mapped_column(Float)  # regular / "van" price if shown
    pack_quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(20))
    unit_price: Mapped[float | None] = mapped_column(Float)
    promo_type: Mapped[str] = mapped_column(String(20), default="none")
    promo_text: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[date | None] = mapped_column(Date)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # analysis (filled after the run's searches complete)
    baseline_price: Mapped[float | None] = mapped_column(Float)
    baseline_basis: Mapped[str | None] = mapped_column(String(10))  # "unit" | "pack"
    discount_pct: Mapped[float | None] = mapped_column(Float)  # best of (vs baseline, vs list price)
    under_target: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="observations")
    item: Mapped[Item] = relationship(back_populates="observations")
    retailer: Mapped[Retailer | None] = relationship()

    @property
    def comparable_price(self) -> float:
        """Price used for comparisons: per unit when known, else the pack price."""
        return self.unit_price if self.unit_price is not None else self.price


class PricePoint(Base):
    """One row per (item, run) with offers: the tracked time series (min / avg / median / max, pack and per unit)."""

    __tablename__ = "price_points"
    __table_args__ = (UniqueConstraint("item_id", "run_id", name="uq_price_point_item_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    n_offers: Mapped[int] = mapped_column(Integer, default=0)
    n_retailers: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str | None] = mapped_column(String(20))
    # pack-level prices across all offers of the run
    min_price: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    median_price: Mapped[float] = mapped_column(Float)
    max_price: Mapped[float] = mapped_column(Float)
    # per-unit prices (only offers with a known pack size)
    min_unit_price: Mapped[float | None] = mapped_column(Float)
    avg_unit_price: Mapped[float | None] = mapped_column(Float)
    median_unit_price: Mapped[float | None] = mapped_column(Float)
    max_unit_price: Mapped[float | None] = mapped_column(Float)
    # the single best offer (lowest per-unit price when known, else lowest pack price)
    best_price: Mapped[float] = mapped_column(Float)
    best_unit_price: Mapped[float | None] = mapped_column(Float)
    best_pack_quantity: Mapped[float | None] = mapped_column(Float)
    best_domain: Mapped[str] = mapped_column(String(200))
    best_url: Mapped[str] = mapped_column(Text)
    best_promo_type: Mapped[str] = mapped_column(String(20), default="none")
    promo_count: Mapped[int] = mapped_column(Integer, default=0)
    deal_count: Mapped[int] = mapped_column(Integer, default=0)

    item: Mapped[Item] = relationship(back_populates="price_points")
    run: Mapped[Run] = relationship(back_populates="price_points")

    @property
    def comparable_min(self) -> float:
        return self.min_unit_price if self.min_unit_price is not None else self.min_price


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True)
    markdown: Mapped[str] = mapped_column(Text)
    html: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[Run] = relationship(back_populates="report")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ChatMessage(Base):
    """One turn on the Ask page. Claude Code keeps nothing between messages, so the conversation lives here."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
