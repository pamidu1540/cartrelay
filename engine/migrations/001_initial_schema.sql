-- migrations/001_initial_schema.sql
-- Full logistics engine schema for PostgreSQL
-- Run in order; each migration is idempotent.

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- trigram index for item search

-- ── Items ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS items (
    id              TEXT        PRIMARY KEY DEFAULT ('ITEM-' || gen_random_uuid()::text),
    name            TEXT        NOT NULL,
    price           NUMERIC(12,2) NOT NULL CHECK (price >= 0),
    volume_cubes    NUMERIC(8,4) NOT NULL DEFAULT 0 CHECK (volume_cubes >= 0),
    category        TEXT        NOT NULL DEFAULT 'Uncategorized',
    stock           INTEGER     NOT NULL DEFAULT 0 CHECK (stock >= 0),
    photo_file_id   TEXT        NOT NULL DEFAULT '',
    description     TEXT        NOT NULL DEFAULT '',
    is_featured     BOOLEAN     NOT NULL DEFAULT FALSE,
    is_available    BOOLEAN     NOT NULL DEFAULT TRUE,
    available_from  BIGINT      NOT NULL DEFAULT 0,
    available_until BIGINT      NOT NULL DEFAULT 0,
    tax_rate        NUMERIC(5,4) NOT NULL DEFAULT -1,
    reorder_level   INTEGER     NOT NULL DEFAULT 0,
    deleted_at      BIGINT,
    created_at      BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_items_category  ON items (category) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_items_search    ON items USING gin(name gin_trgm_ops) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_items_featured  ON items (is_featured) WHERE deleted_at IS NULL;

-- ── Warehouses ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS warehouses (
    id              TEXT        PRIMARY KEY DEFAULT ('WH-' || gen_random_uuid()::text),
    name            TEXT        NOT NULL,
    address         TEXT        NOT NULL DEFAULT '',
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    open_time       TEXT        NOT NULL DEFAULT '08:00',
    close_time      TEXT        NOT NULL DEFAULT '20:00',
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    capacity_units  INTEGER     NOT NULL DEFAULT 0
);

-- ── Drivers ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS drivers (
    id            TEXT        PRIMARY KEY DEFAULT ('DRV-' || gen_random_uuid()::text),
    name          TEXT        NOT NULL,
    phone         TEXT        NOT NULL DEFAULT '',
    telegram_id   BIGINT      UNIQUE,
    vehicle_type  TEXT        NOT NULL DEFAULT '',
    zone          TEXT        NOT NULL DEFAULT '',
    is_available  BOOLEAN     NOT NULL DEFAULT FALSE,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE
);

-- ── Orders ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT        PRIMARY KEY DEFAULT ('ORD-' || gen_random_uuid()::text),
    human_ref         TEXT        NOT NULL UNIQUE,
    user_id           BIGINT      NOT NULL,
    username          TEXT        NOT NULL DEFAULT '',
    status            TEXT        NOT NULL DEFAULT 'pending',
    total             NUMERIC(12,2) NOT NULL,
    receipt           TEXT        NOT NULL DEFAULT '',
    order_notes       TEXT        NOT NULL DEFAULT '',
    delivery_address  TEXT        NOT NULL DEFAULT '',
    driver_id         TEXT        REFERENCES drivers(id) ON DELETE SET NULL,
    scheduled_for     BIGINT      NOT NULL DEFAULT 0,
    contactless       BOOLEAN     NOT NULL DEFAULT FALSE,
    recipient_name    TEXT        NOT NULL DEFAULT '',
    delivery_otp      TEXT        NOT NULL DEFAULT '',
    idempotency_key   TEXT        UNIQUE,    -- BUG-012: duplicate order guard
    issue_status      TEXT        NOT NULL DEFAULT 'none',
    promo_id          TEXT,
    loyalty_redeemed  INTEGER     NOT NULL DEFAULT 0,
    created_at        BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_orders_user_id    ON orders (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status_ts  ON orders (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_driver_id  ON orders (driver_id) WHERE driver_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_idem_key   ON orders (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- order items (for reorder, stock decrement audit)
CREATE TABLE IF NOT EXISTS order_items (
    id          BIGSERIAL   PRIMARY KEY,
    order_id    TEXT        NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    item_id     TEXT        NOT NULL,
    item_name   TEXT        NOT NULL,
    quantity    INTEGER     NOT NULL CHECK (quantity > 0),
    unit_price  NUMERIC(12,2) NOT NULL,
    item_note   TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);

-- order status history
CREATE TABLE IF NOT EXISTS order_status_history (
    id        BIGSERIAL   PRIMARY KEY,
    order_id  TEXT        NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    status    TEXT        NOT NULL,
    ts        BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT,
    actor_id  BIGINT      NOT NULL DEFAULT 0,
    note      TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_osh_order_id ON order_status_history (order_id, ts);

-- order internal notes (admin-only)
CREATE TABLE IF NOT EXISTS order_notes (
    id        BIGSERIAL   PRIMARY KEY,
    order_id  TEXT        NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    actor_id  BIGINT      NOT NULL,
    text      TEXT        NOT NULL,
    ts        BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

-- ── Carts ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS carts (
    user_id      BIGINT      PRIMARY KEY,
    items_json   TEXT        NOT NULL DEFAULT '[]',
    last_updated BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

-- ── Promotions ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promos (
    id              TEXT        PRIMARY KEY DEFAULT ('PROMO-' || gen_random_uuid()::text),
    code            TEXT        NOT NULL UNIQUE,
    description     TEXT        NOT NULL DEFAULT '',
    discount_type   TEXT        NOT NULL CHECK (discount_type IN ('percent','fixed')),
    discount_value  NUMERIC(10,2) NOT NULL CHECK (discount_value > 0),
    min_order_value NUMERIC(12,2) NOT NULL DEFAULT 0,
    max_uses        INTEGER     NOT NULL DEFAULT 0,
    per_user_limit  INTEGER     NOT NULL DEFAULT 0,
    valid_from      BIGINT      NOT NULL DEFAULT 0,
    valid_until     BIGINT      NOT NULL DEFAULT 0,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    uses_count      INTEGER     NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS promo_usage (
    id        BIGSERIAL   PRIMARY KEY,
    promo_id  TEXT        NOT NULL REFERENCES promos(id) ON DELETE CASCADE,
    user_id   BIGINT      NOT NULL,
    order_id  TEXT,
    used_at   BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT,
    UNIQUE (promo_id, user_id, order_id)
);

-- ── Loyalty ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loyalty (
    user_id  BIGINT  PRIMARY KEY,
    points   INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    tier     TEXT    NOT NULL DEFAULT 'bronze'
);

CREATE TABLE IF NOT EXISTS loyalty_log (
    id        BIGSERIAL   PRIMARY KEY,
    user_id   BIGINT      NOT NULL,
    delta     INTEGER     NOT NULL,
    reason    TEXT        NOT NULL,
    actor_id  BIGINT      NOT NULL DEFAULT 0,
    ts        BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

-- ── Customer profiles ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customer_profiles (
    user_id       BIGINT  PRIMARY KEY,
    display_name  TEXT    NOT NULL DEFAULT '',
    phone         TEXT    NOT NULL DEFAULT '',
    language      TEXT    NOT NULL DEFAULT 'en',
    birthday      BIGINT  NOT NULL DEFAULT 0,
    created_at    BIGINT  NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

CREATE TABLE IF NOT EXISTS saved_addresses (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    label       TEXT        NOT NULL DEFAULT 'Home',
    address     TEXT        NOT NULL,
    lat         DOUBLE PRECISION NOT NULL DEFAULT 0,
    lon         DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_default  BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_saved_addr_user ON saved_addresses (user_id);

-- ── Notification preferences ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notif_prefs (
    user_id       BIGINT  PRIMARY KEY,
    order_updates BOOLEAN NOT NULL DEFAULT TRUE,
    promotions    BOOLEAN NOT NULL DEFAULT TRUE,
    new_products  BOOLEAN NOT NULL DEFAULT FALSE,
    system_alerts BOOLEAN NOT NULL DEFAULT TRUE,
    flash_deals   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS pending_notifs (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    type       TEXT        NOT NULL,
    text       TEXT        NOT NULL,
    sent       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_notifs_unsent ON pending_notifs (user_id, sent) WHERE NOT sent;

-- ── Stock subscriptions ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_subscriptions (
    user_id  BIGINT  NOT NULL,
    item_id  TEXT    NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, item_id)
);

-- ── Admin users ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_users (
    telegram_id  BIGINT  PRIMARY KEY,
    username     TEXT    NOT NULL DEFAULT '',
    role         TEXT    NOT NULL DEFAULT 'dispatcher',
    added_by     BIGINT  NOT NULL DEFAULT 0,
    added_at     BIGINT  NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

-- Seed initial admins from ADMIN_USER_IDS env — handled in Go code at startup.

-- ── Config ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT ''
);

-- Default system settings (idempotent)
INSERT INTO config (key, value) VALUES
    ('delivery_base_fare',      '500'),
    ('delivery_rate_per_km',    '75'),
    ('tax_rate',                '0.08'),
    ('free_delivery_threshold', '5000'),
    ('cancel_window_minutes',   '5'),
    ('loyalty_earn_rate',       '10'),
    ('loyalty_redeem_rate',     '1'),
    ('max_order_value',         '0'),
    ('min_order_value',         '0'),
    ('order_cutoff_time',       ''),
    ('maintenance_mode',        'false'),
    ('maintenance_until',       '0')
ON CONFLICT (key) DO NOTHING;

-- ── Issue reports ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS issue_reports (
    id           BIGSERIAL   PRIMARY KEY,
    order_id     TEXT        NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    user_id      BIGINT      NOT NULL,
    issue_type   TEXT        NOT NULL,
    description  TEXT        NOT NULL DEFAULT '',
    photo_id     TEXT        NOT NULL DEFAULT '',
    status       TEXT        NOT NULL DEFAULT 'open',
    resolution   TEXT        NOT NULL DEFAULT '',
    resolved_by  BIGINT,
    resolved_at  BIGINT,
    created_at   BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT
);

-- ── Delivery slots ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS delivery_slots (
    id           BIGSERIAL   PRIMARY KEY,
    slot_date    DATE        NOT NULL,
    start_time   TEXT        NOT NULL,
    end_time     TEXT        NOT NULL,
    capacity     INTEGER     NOT NULL DEFAULT 20,
    booked       INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_slots_date ON delivery_slots (slot_date);

-- ── Human-readable order ref counter ─────────────────────────────────────────
CREATE SEQUENCE IF NOT EXISTS order_ref_seq START 1;

-- ── Audit log ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id        BIGSERIAL   PRIMARY KEY,
    ts        BIGINT      NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT,
    actor_id  BIGINT      NOT NULL DEFAULT 0,
    action    TEXT        NOT NULL,
    target_id TEXT        NOT NULL DEFAULT '',
    detail    TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log (actor_id, ts DESC);
