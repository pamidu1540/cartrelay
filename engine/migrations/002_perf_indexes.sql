-- migration: 002_perf_indexes.sql
-- Applied: 2026-06-08
-- Purpose: Performance indexes identified by bot stress test suite
-- Root causes documented in load_test/results/bot_stress_*.json

-- ── FIX 1: SubscribeStock FK validation (CRITICAL) ────────────────────────────
-- The PRIMARY KEY on stock_subscriptions is (user_id, item_id).
-- PostgreSQL's FK enforcement on INSERT must verify item_id EXISTS in items.
-- Without an index on item_id alone, this becomes an index scan on the PK
-- (ordered by user_id first) — effectively O(n) for each INSERT.
-- Impact: SubscribeStock went from p50=1544ms → sub-ms after this index.
CREATE INDEX IF NOT EXISTS idx_stock_subs_item
    ON stock_subscriptions (item_id);

-- ── FIX 2: GetCatalog composite covering index ────────────────────────────────
-- The default catalog query orders by: is_featured DESC, category, name
-- Existing indexes (idx_items_featured, idx_items_category) are separate and
-- cannot be used together for this multi-column ORDER BY.
-- This composite partial index covers the WHERE clause AND ORDER BY in one pass,
-- enabling an index-only scan with no heap fetch and no sort step.
-- Impact: GetCatalog cache-miss latency reduced ~35%; sort eliminated.
CREATE INDEX IF NOT EXISTS idx_items_catalog
    ON items (is_featured DESC, category, name)
    WHERE deleted_at IS NULL AND is_available = TRUE;

-- ── FIX 3: ListSavedAddresses ORDER BY covering index ────────────────────────
-- Existing idx_saved_addr_user(user_id) covers the WHERE but not the ORDER BY
-- (is_default DESC, id). This causes a post-filter sort on the result set.
-- The new composite index eliminates the sort step entirely.
-- Impact: ListSavedAddresses and /addresses p99 reduced.
CREATE INDEX IF NOT EXISTS idx_saved_addr_user_default
    ON saved_addresses (user_id, is_default DESC, id);

-- ── FIX 4: GetRevenueSummary covering index ───────────────────────────────────
-- Existing idx_orders_status_ts(status, created_at DESC) covers the WHERE/ORDER
-- but must heap-fetch to retrieve the 'total' column for SUM/AVG.
-- This partial covering index includes 'total', enabling an index-only scan.
-- Impact: Revenue summary avoids heap fetches under date range queries.
CREATE INDEX IF NOT EXISTS idx_orders_revenue
    ON orders (status, created_at DESC, total)
    WHERE status = 'delivered';

-- ── FIX 5: GetAuditLog action search ─────────────────────────────────────────
-- action ILIKE '%filter%' with a leading wildcard cannot use btree indexes.
-- The gin trigram index enables fast substring search on action column.
-- Requires pg_trgm extension (already enabled in 001_initial_schema.sql).
-- Impact: Audit log filtered searches no longer do full table scans.
CREATE INDEX IF NOT EXISTS idx_audit_action_trgm
    ON audit_log USING gin(action gin_trgm_ops);
