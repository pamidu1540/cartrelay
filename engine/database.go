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
	// [FIX BUG-012]: Added specific error for duplicate requests
	ErrDuplicateOrder = errors.New("duplicate order request")
)

// InitDatabase opens the SQLite file, applies performance pragmas,
// enforces a single-writer connection pool, and creates the schema.
func InitDatabase(path string) error {
	var err error
	db, err = sql.Open("sqlite", path)
	if err != nil {
		return fmt.Errorf("open db: %w", err)
	}

	// SQLite serialises all writes behind one connection.
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
	// [FIX BUG-012]: Seamless schema migration to add idempotency tracking.
	// Exec will ignore errors if the column already exists.
	_, _ = db.Exec("ALTER TABLE orders ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT '';")

	stmts := []string{
		`CREATE TABLE IF NOT EXISTS items (
			id            TEXT    PRIMARY KEY,
			name          TEXT    NOT NULL,
			price         REAL    NOT NULL CHECK(price >= 0),
			volume_cubes  REAL    NOT NULL CHECK(volume_cubes >= 0),
			category      TEXT    NOT NULL DEFAULT 'Uncategorized',
			stock         INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
			photo_file_id TEXT    NOT NULL DEFAULT '',
			deleted_at    INTEGER
		)`,

		`CREATE TABLE IF NOT EXISTS orders (
			order_id         TEXT    PRIMARY KEY,
			user_id          INTEGER NOT NULL,
			username         TEXT    NOT NULL,
			status           TEXT    NOT NULL DEFAULT 'pending',
			total            REAL    NOT NULL,
			receipt          TEXT    NOT NULL DEFAULT '',
			timestamp        INTEGER NOT NULL,
			idempotency_key  TEXT    NOT NULL DEFAULT ''
		)`,

		`CREATE INDEX IF NOT EXISTS idx_orders_status_ts
			ON orders(status, timestamp ASC)`,
		`CREATE INDEX IF NOT EXISTS idx_orders_user_ts
			ON orders(user_id, timestamp DESC)`,
		
		// [FIX BUG-012]: Ensure only valid requests trigger the constraint
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency 
			ON orders(idempotency_key) WHERE idempotency_key != ''`,

		// [Phase 1: Cluster A] Warehouse storage
		`CREATE TABLE IF NOT EXISTS warehouses (
			id   TEXT PRIMARY KEY,
			name TEXT NOT NULL,
			lat  REAL NOT NULL,
			lon  REAL NOT NULL
		)`,

		`CREATE TABLE IF NOT EXISTS carts (
			user_id      INTEGER PRIMARY KEY,
			cart_json    TEXT    NOT NULL DEFAULT '{}',
			last_updated INTEGER NOT NULL
		)`,

		`CREATE TABLE IF NOT EXISTS config (
			key   TEXT PRIMARY KEY,
			value TEXT NOT NULL DEFAULT ''
		)`,

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

// [FIX BUG-012]: Updated signature to require idempotencyKey
func InsertOrder(req *pb.OrderRequest, cart []*pb.CartItem, idempotencyKey string) (string, error) {
	if len(req.QuoteData) == 0 {
		return "", ErrQuoteMissing
	}

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
	defer tx.Rollback()

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
		`INSERT INTO orders(order_id,user_id,username,status,total,receipt,timestamp,idempotency_key)
		 VALUES(?,?,?,?,?,?,?,?)`,
		orderID, req.UserId, req.Username, "pending",
		req.ExpectedTotal, req.ReceiptText, time.Now().Unix(), idempotencyKey,
	)
	if err != nil {
		// Detect SQLite Unique Constraint failure for idempotency check
		if strings.Contains(err.Error(), "UNIQUE constraint failed: orders.idempotency_key") {
			return "", ErrDuplicateOrder
		}
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

func DBExpireCarts(olderThanHours int) error {
	cutoff := time.Now().Add(-time.Duration(olderThanHours) * time.Hour).Unix()
	_, err := db.Exec("DELETE FROM carts WHERE last_updated<?", cutoff)
	return err
}

// ── Warehouses (Phase 1: Cluster A) ───────────────────────────────────────────

func DBAddWarehouse(name string, lat, lon float64) (string, error) {
	id := "WH-" + uuid.NewString()
	_, err := db.Exec(
		`INSERT INTO warehouses(id, name, lat, lon) VALUES(?,?,?,?)`,
		id, name, lat, lon,
	)
	return id, err
}

func DBGetWarehouses() ([]*pb.Warehouse, error) {
	rows, err := db.Query(`SELECT id, name, lat, lon FROM warehouses`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var whs []*pb.Warehouse
	for rows.Next() {
		var w pb.Warehouse
		if err := rows.Scan(&w.Id, &w.Name, &w.Lat, &w.Lon); err != nil {
			return nil, err
		}
		whs = append(whs, &w)
	}
	return whs, rows.Err()
}

// ── Audit & Performance ───────────────────────────────────────────────────────

func DBAuditLog(actorID int64, action, targetID, detail string) {
	_, err := db.Exec(
		`INSERT INTO audit_log(ts,actor_id,action,target_id,detail) VALUES(?,?,?,?,?)`,
		time.Now().Unix(), actorID, action, targetID, detail,
	)
	if err != nil {
		slog.Warn("audit log write failed", "err", err)
	}
}

// StartWALCheckpointTimer prevents unbounded WAL growth (PERF-001)
func StartWALCheckpointTimer() {
	ticker := time.NewTicker(5 * time.Minute)
	go func() {
		for range ticker.C {
			if db == nil {
				continue
			}
			// TRUNCATE resets the WAL file to 0 bytes safely
			_, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE);")
			if err != nil {
				slog.Warn("WAL checkpoint failed", "err", err)
			} else {
				slog.Debug("WAL checkpoint completed successfully")
			}
		}
	}()
}