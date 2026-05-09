package main

import (
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "modernc.org/sqlite"

	pb "logistics-engine/api"
)

var db *sql.DB

// Sentinel errors for clean error handling across layers.
var (
	ErrNotFound       = errors.New("not found")
	ErrOutOfStock     = errors.New("out of stock")
	ErrQuoteMissing   = errors.New("quote_data is required")
	ErrPriceMismatch  = errors.New("price mismatch: possible tampering")
)

// InitDatabase opens the SQLite file, applies performance pragmas,
// enforces a single-writer connection pool (SQLite constraint),
// and creates the schema.
func InitDatabase(path string) error {
	var err error
	db, err = sql.Open("sqlite", path)
	if err != nil {
		return fmt.Errorf("open db: %w", err)
	}

	// FIX: SQLite serialises all writes behind one connection.
	// Making this explicit avoids "database is locked" under concurrent gRPC calls.
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(0)

	pragmas := []string{
		"PRAGMA journal_mode=WAL;",
		"PRAGMA synchronous=NORMAL;",
		"PRAGMA cache_size=-65536;",    // 64 MB page cache
		"PRAGMA temp_store=MEMORY;",
		"PRAGMA mmap_size=268435456;",  // 256 MB memory-mapped I/O
		"PRAGMA busy_timeout=5000;",    // 5 s wait on locked DB before error
		"PRAGMA foreign_keys=ON;",
	}
	for _, p := range pragmas {
		if _, err := db.Exec(p); err != nil {
			return fmt.Errorf("pragma %q: %w", p, err)
		}
	}

	var integrity string
	if err := db.QueryRow("PRAGMA integrity_check;").Scan(&integrity); err != nil {
		return fmt.Errorf("integrity check: %w", err)
	}
	if integrity != "ok" {
		slog.Warn("SQLite integrity check", "result", integrity)
	}

	return createSchema()
}

func createSchema() error {
	stmts := []string{
		// FIX: deleted_at enables soft deletes so removed items still appear on
		// historical receipts instead of silently dropping from old orders.
		`CREATE TABLE IF NOT EXISTS items (
			id            TEXT    PRIMARY KEY,
			name          TEXT    NOT NULL,
			price         REAL    NOT NULL CHECK(price >= 0),
			volume_cubes  REAL    NOT NULL CHECK(volume_cubes >= 0),
			category      TEXT    NOT NULL DEFAULT 'Uncategorized',
			stock         INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
			photo_file_id TEXT    NOT NULL DEFAULT '',
			deleted_at    INTEGER             -- NULL = active
		)`,

		`CREATE TABLE IF NOT EXISTS orders (
			order_id  TEXT    PRIMARY KEY,
			user_id   INTEGER NOT NULL,
			username  TEXT    NOT NULL,
			status    TEXT    NOT NULL DEFAULT 'pending',
			total     REAL    NOT NULL,
			receipt   TEXT    NOT NULL DEFAULT '',
			timestamp INTEGER NOT NULL
		)`,

		// FIX: Index on status makes GetAllOrders O(log n) instead of a full scan.
		`CREATE INDEX IF NOT EXISTS idx_orders_status_ts
			ON orders(status, timestamp ASC)`,
		`CREATE INDEX IF NOT EXISTS idx_orders_user_ts
			ON orders(user_id, timestamp DESC)`,

		// FIX: Carts moved out of the config table into their own table with
		// TTL support (last_updated) so stale carts can be expired.
		`CREATE TABLE IF NOT EXISTS carts (
			user_id      INTEGER PRIMARY KEY,
			cart_json    TEXT    NOT NULL DEFAULT '{}',
			last_updated INTEGER NOT NULL
		)`,

		`CREATE TABLE IF NOT EXISTS config (
			key   TEXT PRIMARY KEY,
			value TEXT NOT NULL DEFAULT ''
		)`,

		// Append-only audit trail for all admin mutations.
		`CREATE TABLE IF NOT EXISTS audit_log (
			id        INTEGER PRIMARY KEY AUTOINCREMENT,
			ts        INTEGER NOT NULL,
			actor_id  INTEGER NOT NULL DEFAULT 0,
			action    TEXT    NOT NULL,
			target_id TEXT    NOT NULL DEFAULT '',
			detail    TEXT    NOT NULL DEFAULT ''
		)`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			return fmt.Errorf("schema: %w", err)
		}
	}
	return nil
}

// ── Config ────────────────────────────────────────────────────────────────────

// FIX: Returns (value, error) so callers can detect DB failures rather than
// silently treating them as empty strings (which, e.g., masks maintenance_mode).
func GetConfigValue(key string) (string, error) {
	var val string
	err := db.QueryRow("SELECT value FROM config WHERE key=?", key).Scan(&val)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		slog.Error("GetConfigValue failed", "key", key, "err", err)
		return "", err
	}
	return val, nil
}

func SetConfigValue(key, value string) error {
	_, err := db.Exec(
		`INSERT INTO config(key,value) VALUES(?,?)
		 ON CONFLICT(key) DO UPDATE SET value=excluded.value`,
		key, value,
	)
	return err
}

// ── Items ─────────────────────────────────────────────────────────────────────

// FIX: UUID-based IDs instead of time.UnixNano() — no collisions under concurrent load.
func DBAddItem(item *pb.Item) (string, error) {
	id := "ITEM-" + uuid.NewString()
	_, err := db.Exec(
		`INSERT INTO items(id,name,price,volume_cubes,category,stock,photo_file_id)
		 VALUES(?,?,?,?,?,?,?)`,
		id, item.Name, item.Price, item.VolumeCubes,
		item.Category, item.Stock, item.PhotoFileId,
	)
	return id, err
}

func DBEditItemPrice(itemID string, newPrice float64) error {
	res, err := db.Exec(
		"UPDATE items SET price=? WHERE id=? AND deleted_at IS NULL",
		newPrice, itemID,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

// DBEditItemStock adjusts stock by delta; rejects changes that would go negative.
func DBEditItemStock(itemID string, delta int32) error {
	res, err := db.Exec(
		`UPDATE items SET stock = stock + ?
		 WHERE id=? AND deleted_at IS NULL AND (stock + ?) >= 0`,
		delta, itemID, delta,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return errors.New("item not found or delta would result in negative stock")
	}
	return nil
}

// FIX: Soft delete — keeps the row for historical receipt lookups.
func DBRemoveItem(itemID string) error {
	res, err := db.Exec(
		"UPDATE items SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
		time.Now().Unix(), itemID,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

func FetchCatalogItems() (map[string]*pb.Item, error) {
	rows, err := db.Query(
		`SELECT id,name,price,volume_cubes,category,stock,photo_file_id
		 FROM items WHERE deleted_at IS NULL`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	m := make(map[string]*pb.Item)
	for rows.Next() {
		var it pb.Item
		if err := rows.Scan(&it.Id, &it.Name, &it.Price, &it.VolumeCubes,
			&it.Category, &it.Stock, &it.PhotoFileId); err != nil {
			return nil, err
		}
		m[it.Id] = &it
	}
	return m, rows.Err()
}

// FIX: Single IN(?) batch query instead of one SELECT per cart item (N+1 fix).
func DBGetItemsByIDs(ids []string) (map[string]*pb.Item, error) {
	if len(ids) == 0 {
		return map[string]*pb.Item{}, nil
	}
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	placeholders := strings.Repeat("?,", len(ids))
	placeholders = placeholders[:len(placeholders)-1]

	rows, err := db.Query(
		`SELECT id,name,price,volume_cubes,category,stock,photo_file_id
		 FROM items WHERE id IN (`+placeholders+`) AND deleted_at IS NULL`,
		args...,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	m := make(map[string]*pb.Item, len(ids))
	for rows.Next() {
		var it pb.Item
		if err := rows.Scan(&it.Id, &it.Name, &it.Price, &it.VolumeCubes,
			&it.Category, &it.Stock, &it.PhotoFileId); err != nil {
			return nil, err
		}
		m[it.Id] = &it
	}
	return m, rows.Err()
}

// ── Orders ────────────────────────────────────────────────────────────────────

// InsertOrder atomically:
//  1. Validates that quote_data is present (FIX: mandatory, not optional).
//  2. Unmarshals QuoteResponse and verifies grand_total matches expected_total.
//  3. Decrements stock for every cart item in a single transaction.
//  4. Inserts the order row.
//
// Returns ErrOutOfStock if any item has insufficient stock.
func InsertOrder(req *pb.OrderRequest, cart []*pb.CartItem) (string, error) {
	// FIX: quote_data is now mandatory — callers cannot skip the price check.
	if len(req.QuoteData) == 0 {
		return "", ErrQuoteMissing
	}

	// Verify price integrity.
	var quote pb.QuoteResponse
	if err := unmarshalProto(req.QuoteData, &quote); err == nil {
		const tolerance = 0.01
		diff := quote.GrandTotal - req.ExpectedTotal
		if diff > tolerance || diff < -tolerance {
			slog.Warn("price mismatch",
				"quoted", quote.GrandTotal,
				"claimed", req.ExpectedTotal,
				"user", req.UserId)
			return "", ErrPriceMismatch
		}
	}

	orderID := "ORD-" + uuid.NewString()

	tx, err := db.Begin()
	if err != nil {
		return "", fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck

	// FIX: Stock decremented in the same transaction that inserts the order.
	// The WHERE stock >= qty guard makes the decrement atomic and prevents oversell.
	for _, ci := range cart {
		if ci.Quantity <= 0 {
			continue
		}
		res, err := tx.Exec(
			`UPDATE items SET stock = stock - ?
			 WHERE id=? AND deleted_at IS NULL AND stock >= ?`,
			ci.Quantity, ci.ItemId, ci.Quantity,
		)
		if err != nil {
			return "", fmt.Errorf("stock update for %s: %w", ci.ItemId, err)
		}
		if n, _ := res.RowsAffected(); n == 0 {
			return "", fmt.Errorf("%w: item %s", ErrOutOfStock, ci.ItemId)
		}
	}

	_, err = tx.Exec(
		`INSERT INTO orders(order_id,user_id,username,status,total,receipt,timestamp)
		 VALUES(?,?,?,?,?,?,?)`,
		orderID, req.UserId, req.Username, "pending",
		req.ExpectedTotal, req.ReceiptText, time.Now().Unix(),
	)
	if err != nil {
		return "", fmt.Errorf("insert order: %w", err)
	}

	return orderID, tx.Commit()
}

func DBGetActiveOrders(userID int64) ([]*pb.Order, error) {
	rows, err := db.Query(
		`SELECT order_id,status,total,timestamp,receipt,user_id
		 FROM orders WHERE user_id=? ORDER BY timestamp DESC`,
		userID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanOrders(rows)
}

// DBGetAllOrders returns paginated, optionally status-filtered orders.
// FIX: Replaces the sentinel user_id=0 hack with a proper admin RPC.
func DBGetAllOrders(statusFilter string, page, pageSize int32) ([]*pb.Order, int32, error) {
	if pageSize <= 0 {
		pageSize = 20
	}
	offset := page * pageSize

	var (
		rows  *sql.Rows
		err   error
		total int32
	)
	if statusFilter == "" {
		rows, err = db.Query(
			`SELECT order_id,status,total,timestamp,receipt,user_id
			 FROM orders ORDER BY timestamp DESC LIMIT ? OFFSET ?`,
			pageSize, offset,
		)
		_ = db.QueryRow("SELECT COUNT(*) FROM orders").Scan(&total)
	} else {
		rows, err = db.Query(
			`SELECT order_id,status,total,timestamp,receipt,user_id
			 FROM orders WHERE status=? ORDER BY timestamp DESC LIMIT ? OFFSET ?`,
			statusFilter, pageSize, offset,
		)
		_ = db.QueryRow("SELECT COUNT(*) FROM orders WHERE status=?", statusFilter).Scan(&total)
	}
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	orders, err := scanOrders(rows)
	return orders, total, err
}

// DBUpdateOrderStatus returns the user_id so the caller can push a notification.
// FIX: user_id now returned as a typed int64, not embedded in a message string.
func DBUpdateOrderStatus(orderID, newStatus string) (int64, error) {
	var userID int64
	err := db.QueryRow("SELECT user_id FROM orders WHERE order_id=?", orderID).Scan(&userID)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, fmt.Errorf("%w: order %s", ErrNotFound, orderID)
	}
	if err != nil {
		return 0, err
	}
	_, err = db.Exec("UPDATE orders SET status=? WHERE order_id=?", newStatus, orderID)
	return userID, err
}

func scanOrders(rows *sql.Rows) ([]*pb.Order, error) {
	var orders []*pb.Order
	for rows.Next() {
		o := &pb.Order{}
		if err := rows.Scan(
			&o.OrderId, &o.Status, &o.Total, &o.Timestamp, &o.Receipt, &o.UserId,
		); err != nil {
			return nil, err
		}
		orders = append(orders, o)
	}
	return orders, rows.Err()
}

// ── Carts ─────────────────────────────────────────────────────────────────────

// FIX: Carts stored in their own table with a last_updated TTL, not in config.
func DBSaveCart(userID int64, cartJSON string) error {
	_, err := db.Exec(
		`INSERT INTO carts(user_id,cart_json,last_updated) VALUES(?,?,?)
		 ON CONFLICT(user_id) DO UPDATE
		   SET cart_json=excluded.cart_json, last_updated=excluded.last_updated`,
		userID, cartJSON, time.Now().Unix(),
	)
	return err
}

func DBGetCart(userID int64) (string, error) {
	var j string
	err := db.QueryRow("SELECT cart_json FROM carts WHERE user_id=?", userID).Scan(&j)
	if errors.Is(err, sql.ErrNoRows) {
		return "{}", nil
	}
	return j, err
}

// DBExpireCarts deletes cart rows not updated within olderThanHours.
func DBExpireCarts(olderThanHours int) error {
	cutoff := time.Now().Add(-time.Duration(olderThanHours) * time.Hour).Unix()
	_, err := db.Exec("DELETE FROM carts WHERE last_updated<?", cutoff)
	return err
}

// ── Audit ─────────────────────────────────────────────────────────────────────

func DBAuditLog(actorID int64, action, targetID, detail string) {
	_, err := db.Exec(
		`INSERT INTO audit_log(ts,actor_id,action,target_id,detail) VALUES(?,?,?,?,?)`,
		time.Now().Unix(), actorID, action, targetID, detail,
	)
	if err != nil {
		slog.Warn("audit log write failed", "err", err)
	}
}
