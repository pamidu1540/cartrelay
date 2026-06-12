package main

// database.go — PostgreSQL data-access layer

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"embed"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	pgxtrace "gopkg.in/DataDog/dd-trace-go.v1/contrib/jackc/pgx.v5"

	pb "logistics-engine/api"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

var pool *pgxpool.Pool

// ── Sentinel errors ───────────────────────────────────────────────────────────

var (
	ErrNotFound         = errors.New("not found")
	ErrOutOfStock       = errors.New("out of stock")
	ErrQuoteMissing     = errors.New("quote_data is required")
	ErrPriceMismatch    = errors.New("price mismatch: possible tampering")
	ErrDuplicateOrder   = errors.New("duplicate order: idempotency key already used")
	ErrUnauthorized     = errors.New("unauthorized")
	ErrInvalidTransition = errors.New("invalid status transition")
)

// ── Init ─────────────────────────────────────────────────────────────────────

func InitDatabase(dsn string) error {
	if dsn == "" {
		return fmt.Errorf("DATABASE_URL is not set")
	}

	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return fmt.Errorf("parse DATABASE_URL: %w", err)
	}

	// Pool sizing — sensible defaults, overridable via env
	cfg.MaxConns = envInt32("DB_MAX_CONNS", 20)
	cfg.MinConns = envInt32("DB_MIN_CONNS", 2)
	cfg.MaxConnLifetime = time.Duration(envInt("DB_CONN_LIFETIME_S", 3600)) * time.Second
	cfg.MaxConnIdleTime = time.Duration(envInt("DB_CONN_IDLE_S", 300)) * time.Second
	cfg.HealthCheckPeriod = 30 * time.Second

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	var p *pgxpool.Pool
	if os.Getenv("DD_AGENT_HOST") != "" || os.Getenv("DD_API_KEY") != "" {
		slog.Info("initializing database pool with Datadog tracing...")
		p, err = pgxtrace.NewPoolWithConfig(ctx, cfg, pgxtrace.WithServiceName("logistics-postgres"))
	} else {
		p, err = pgxpool.NewWithConfig(ctx, cfg)
	}
	if err != nil {
		return fmt.Errorf("pgxpool.New: %w", err)
	}

	if err := p.Ping(ctx); err != nil {
		return fmt.Errorf("db ping: %w", err)
	}

	pool = p
	slog.Info("database connected", "max_conns", cfg.MaxConns)
	return nil
}

func CloseDatabase() {
	if pool != nil {
		pool.Close()
	}
}

// RunMigrations executes all SQL files in migrations/ in filename order.
func RunMigrations() error {
	entries, err := migrationFS.ReadDir("migrations")
	if err != nil {
		return fmt.Errorf("read migrations dir: %w", err)
	}

	ctx := context.Background()
	_, err = pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			filename TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`)
	if err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".sql") {
			continue
		}
		var exists bool
		_ = pool.QueryRow(ctx,
			"SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE filename=$1)", e.Name(),
		).Scan(&exists)
		if exists {
			continue
		}

		sql, err := migrationFS.ReadFile("migrations/" + e.Name())
		if err != nil {
			return fmt.Errorf("read migration %s: %w", e.Name(), err)
		}

		tx, err := pool.Begin(ctx)
		if err != nil {
			return fmt.Errorf("begin migration tx: %w", err)
		}
		if _, err := tx.Exec(ctx, string(sql)); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("apply migration %s: %w", e.Name(), err)
		}
		if _, err := tx.Exec(ctx,
			"INSERT INTO schema_migrations (filename) VALUES ($1)", e.Name()); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("record migration %s: %w", e.Name(), err)
		}
		if err := tx.Commit(ctx); err != nil {
			return fmt.Errorf("commit migration %s: %w", e.Name(), err)
		}
		slog.Info("migration applied", "file", e.Name())
	}
	return nil
}

// SeedAdmins upserts admin users from the ADMIN_USER_IDS env var.
func SeedAdmins(raw string) error {
	ctx := context.Background()
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		id, err := strconv.ParseInt(part, 10, 64)
		if err != nil {
			slog.Warn("invalid admin user ID", "raw", part)
			continue
		}
		_, err = pool.Exec(ctx, `
			INSERT INTO admin_users (telegram_id, role)
			VALUES ($1, 'super_admin')
			ON CONFLICT (telegram_id) DO NOTHING`,
			id,
		)
		if err != nil {
			return fmt.Errorf("seed admin %d: %w", id, err)
		}
	}
	return nil
}

// ── Config ────────────────────────────────────────────────────────────────────

func GetConfigValue(ctx context.Context, key string) (string, error) {
	var val string
	err := pool.QueryRow(ctx, "SELECT value FROM config WHERE key=$1", key).Scan(&val)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	return val, err
}

func SetConfigValue(ctx context.Context, key, value string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO config (key, value) VALUES ($1, $2)
		ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
		key, value,
	)
	return err
}

func GetSystemSettings(ctx context.Context) (*pb.SystemSettings, error) {
	rows, err := pool.Query(ctx, "SELECT key, value FROM config")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	m := make(map[string]string)
	for rows.Next() {
		var k, v string
		if err := rows.Scan(&k, &v); err != nil {
			return nil, err
		}
		m[k] = v
	}

	return &pb.SystemSettings{
		DeliveryBaseFare:     parseFloat(m["delivery_base_fare"], 500),
		DeliveryRatePerKm:    parseFloat(m["delivery_rate_per_km"], 75),
		TaxRate:              parseFloat(m["tax_rate"], 0.08),
		FreeDeliveryThreshold: parseFloat(m["free_delivery_threshold"], 5000),
		CancelWindowMinutes:  parseInt32(m["cancel_window_minutes"], 5),
		LoyaltyEarnRate:      parseInt32(m["loyalty_earn_rate"], 10),
		LoyaltyRedeemRate:    parseInt32(m["loyalty_redeem_rate"], 1),
		MaxOrderValue:        parseInt32(m["max_order_value"], 0),
		MinOrderValue:        parseInt32(m["min_order_value"], 0),
		OrderCutoffTime:      m["order_cutoff_time"],
	}, nil
}

func SetSystemSettings(ctx context.Context, s *pb.SystemSettings, actorID int64) error {
	settings := map[string]string{
		"delivery_base_fare":      fmt.Sprintf("%.2f", s.DeliveryBaseFare),
		"delivery_rate_per_km":    fmt.Sprintf("%.2f", s.DeliveryRatePerKm),
		"tax_rate":                fmt.Sprintf("%.4f", s.TaxRate),
		"free_delivery_threshold": fmt.Sprintf("%.2f", s.FreeDeliveryThreshold),
		"cancel_window_minutes":   fmt.Sprintf("%d", s.CancelWindowMinutes),
		"loyalty_earn_rate":       fmt.Sprintf("%d", s.LoyaltyEarnRate),
		"loyalty_redeem_rate":     fmt.Sprintf("%d", s.LoyaltyRedeemRate),
		"max_order_value":         fmt.Sprintf("%d", s.MaxOrderValue),
		"min_order_value":         fmt.Sprintf("%d", s.MinOrderValue),
		"order_cutoff_time":       s.OrderCutoffTime,
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	batch := &pgx.Batch{}
	for k, v := range settings {
		batch.Queue(`INSERT INTO config (key, value) VALUES ($1, $2)
			 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`, k, v)
	}
	br := tx.SendBatch(ctx, batch)
	for range settings {
		if _, err := br.Exec(); err != nil {
			br.Close()
			return fmt.Errorf("set config batch exec: %w", err)
		}
	}
	if err := br.Close(); err != nil {
		return fmt.Errorf("set config batch close: %w", err)
	}
	DBAuditLog(ctx, actorID, "set_system_settings", "config", fmt.Sprintf("%+v", settings))
	return tx.Commit(ctx)
}

// ── Items ─────────────────────────────────────────────────────────────────────

func DBAddItem(ctx context.Context, item *pb.Item, actorID int64) (string, error) {
	var id string
	err := pool.QueryRow(ctx, `
		INSERT INTO items (name, price, volume_cubes, category, stock, photo_file_id,
		                   description, is_featured, is_available, available_from,
		                   available_until, tax_rate, reorder_level)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
		RETURNING id`,
		item.Name, item.Price, item.VolumeCubes, item.Category, item.Stock,
		item.PhotoFileId, item.Description, item.IsFeatured, item.IsAvailable,
		item.AvailableFrom, item.AvailableUntil, item.TaxRate, item.ReorderLevel,
	).Scan(&id)
	if err != nil {
		return "", err
	}
	DBAuditLog(ctx, actorID, "add_item", id,
		fmt.Sprintf("name=%q price=%.2f stock=%d", item.Name, item.Price, item.Stock))
	return id, nil
}

func DBEditItem(ctx context.Context, item *pb.Item, actorID int64) error {
	res, err := pool.Exec(ctx, `
		UPDATE items SET
			name=$1, price=$2, volume_cubes=$3, category=$4, stock=$5,
			photo_file_id=$6, description=$7, is_featured=$8, is_available=$9,
			available_from=$10, available_until=$11, tax_rate=$12, reorder_level=$13
		WHERE id=$14 AND deleted_at IS NULL`,
		item.Name, item.Price, item.VolumeCubes, item.Category, item.Stock,
		item.PhotoFileId, item.Description, item.IsFeatured, item.IsAvailable,
		item.AvailableFrom, item.AvailableUntil, item.TaxRate, item.ReorderLevel,
		item.Id,
	)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "edit_item", item.Id, fmt.Sprintf("price=%.2f", item.Price))
	return nil
}

func DBEditItemPrice(ctx context.Context, itemID string, newPrice float64, actorID int64) error {
	res, err := pool.Exec(ctx,
		"UPDATE items SET price=$1 WHERE id=$2 AND deleted_at IS NULL",
		newPrice, itemID,
	)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "edit_price", itemID, fmt.Sprintf("new_price=%.2f", newPrice))
	return nil
}

func DBEditItemStock(ctx context.Context, itemID string, delta int32, reason string, actorID int64) error {
	res, err := pool.Exec(ctx, `
		UPDATE items SET stock = stock + $1
		WHERE id=$2 AND deleted_at IS NULL AND (stock + $1) >= 0`,
		delta, itemID,
	)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return errors.New("item not found or delta would result in negative stock")
	}
	// Notify stock subscribers if restocked (delta > 0)
	if delta > 0 {
		// #nosec G118 -- background stock notification must outlive request context
		go notifyStockSubscribers(context.Background(), itemID)
	}
	DBAuditLog(ctx, actorID, "edit_stock", itemID, fmt.Sprintf("delta=%+d reason=%q", delta, reason))
	return nil
}

func DBRemoveItem(ctx context.Context, itemID string, actorID int64) error {
	res, err := pool.Exec(ctx,
		"UPDATE items SET deleted_at=$1 WHERE id=$2 AND deleted_at IS NULL",
		time.Now().Unix(), itemID,
	)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "remove_item", itemID, "soft-deleted")
	return nil
}

func FetchCatalogItems(ctx context.Context, catFilter, search string, page, pageSize int32) ([]*pb.Item, int32, error) {
	if pageSize <= 0 {
		pageSize = 50
	}
	offset := page * pageSize

	where := "deleted_at IS NULL AND is_available = TRUE"
	args := []any{}
	n := 1

	if catFilter != "" {
		where += fmt.Sprintf(" AND category = $%d", n)
		args = append(args, catFilter)
		n++
	}
	if search != "" {
		where += fmt.Sprintf(" AND name ILIKE $%d", n)
		args = append(args, "%"+search+"%")
		n++
	}

	// Fetch with window function
	args = append(args, pageSize, offset)
	rows, err := pool.Query(ctx, fmt.Sprintf(`
		SELECT id, name, price, volume_cubes, category, stock, photo_file_id,
		       description, is_featured, is_available, available_from, available_until,
		       tax_rate, reorder_level, deleted_at, created_at, count(*) over()
		FROM items
		WHERE %s
		ORDER BY is_featured DESC, category, name
		LIMIT $%d OFFSET $%d`, where, n, n+1),
		args...,
	)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	return scanItems(rows, true)
}

func DBGetItemsByIDs(ctx context.Context, ids []string) (map[string]*pb.Item, error) {
	if len(ids) == 0 {
		return map[string]*pb.Item{}, nil
	}
	// Build $1,$2,... placeholders
	placeholders := make([]string, len(ids))
	args := make([]any, len(ids))
	for i, id := range ids {
		placeholders[i] = fmt.Sprintf("$%d", i+1)
		args[i] = id
	}

	rows, err := pool.Query(ctx, fmt.Sprintf(`
		SELECT id, name, price, volume_cubes, category, stock, photo_file_id,
		       description, is_featured, is_available, available_from, available_until,
		       tax_rate, reorder_level, deleted_at, created_at
		FROM items
		WHERE id IN (%s) AND deleted_at IS NULL`,
		strings.Join(placeholders, ",")),
		args...,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	items, _, err := scanItems(rows, false)
	if err != nil {
		return nil, err
	}
	m := make(map[string]*pb.Item, len(items))
	for _, it := range items {
		m[it.Id] = it
	}
	return m, nil
}

func DBBulkImportItems(ctx context.Context, items []*pb.Item, actorID int64) (int32, int32, []string) {
	var created, updated int32
	var errs []string

	for _, item := range items {
		if item.Id != "" {
			// Try update
			err := DBEditItem(ctx, item, actorID)
			if err == nil {
				updated++
				continue
			}
			if !errors.Is(err, ErrNotFound) {
				errs = append(errs, fmt.Sprintf("edit %s: %v", item.Id, err))
				continue
			}
		}
		_, err := DBAddItem(ctx, item, actorID)
		if err != nil {
			errs = append(errs, fmt.Sprintf("add %q: %v", item.Name, err))
			continue
		}
		created++
	}
	return created, updated, errs
}

func scanItems(rows pgx.Rows, withTotal bool) ([]*pb.Item, int32, error) {
	items := make([]*pb.Item, 0, 32)
	var total int32
	for rows.Next() {
		var it pb.Item
		var deletedAt *int64
		dest := []any{
			&it.Id, &it.Name, &it.Price, &it.VolumeCubes, &it.Category,
			&it.Stock, &it.PhotoFileId, &it.Description, &it.IsFeatured,
			&it.IsAvailable, &it.AvailableFrom, &it.AvailableUntil,
			&it.TaxRate, &it.ReorderLevel, &deletedAt, &it.CreatedAt,
		}
		if withTotal {
			dest = append(dest, &total)
		}
		if err := rows.Scan(dest...); err != nil {
			return nil, 0, err
		}
		if deletedAt != nil {
			it.DeletedAt = *deletedAt
		}
		items = append(items, &it)
	}
	return items, total, rows.Err()
}

// ── Warehouses ────────────────────────────────────────────────────────────────

func DBListWarehouses(ctx context.Context) ([]*pb.Warehouse, error) {
	rows, err := pool.Query(ctx, `
		SELECT id, name, address, lat, lon, open_time, close_time, is_active, capacity_units
		FROM warehouses ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var whs []*pb.Warehouse
	for rows.Next() {
		w := &pb.Warehouse{}
		if err := rows.Scan(&w.Id, &w.Name, &w.Address, &w.Lat, &w.Lon,
			&w.OpenTime, &w.CloseTime, &w.IsActive, &w.CapacityUnits); err != nil {
			return nil, err
		}
		whs = append(whs, w)
	}
	return whs, rows.Err()
}

func DBAddWarehouse(ctx context.Context, w *pb.Warehouse, actorID int64) (string, error) {
	var id string
	err := pool.QueryRow(ctx, `
		INSERT INTO warehouses (name, address, lat, lon, open_time, close_time, is_active, capacity_units)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id`,
		w.Name, w.Address, w.Lat, w.Lon, w.OpenTime, w.CloseTime, w.IsActive, w.CapacityUnits,
	).Scan(&id)
	if err != nil {
		return "", err
	}
	DBAuditLog(ctx, actorID, "add_warehouse", id, fmt.Sprintf("name=%q lat=%.4f lon=%.4f", w.Name, w.Lat, w.Lon))
	return id, nil
}

func DBEditWarehouse(ctx context.Context, w *pb.Warehouse, actorID int64) error {
	res, err := pool.Exec(ctx, `
		UPDATE warehouses SET name=$1, address=$2, lat=$3, lon=$4,
		       open_time=$5, close_time=$6, is_active=$7, capacity_units=$8
		WHERE id=$9`,
		w.Name, w.Address, w.Lat, w.Lon, w.OpenTime, w.CloseTime, w.IsActive, w.CapacityUnits, w.Id,
	)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "edit_warehouse", w.Id, "updated")
	return nil
}

func DBRemoveWarehouse(ctx context.Context, id string, actorID int64) error {
	res, err := pool.Exec(ctx, "DELETE FROM warehouses WHERE id=$1", id)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "remove_warehouse", id, "deleted")
	return nil
}

// ── Drivers ───────────────────────────────────────────────────────────────────

func DBListDrivers(ctx context.Context, availableOnly bool) ([]*pb.Driver, error) {
	q := `SELECT id, name, phone, telegram_id, vehicle_type, zone, is_available, is_active
	      FROM drivers WHERE is_active=TRUE`
	if availableOnly {
		q += " AND is_available=TRUE"
	}
	q += " ORDER BY name"

	rows, err := pool.Query(ctx, q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var drivers []*pb.Driver
	for rows.Next() {
		d := &pb.Driver{}
		var tgID *int64
		if err := rows.Scan(&d.Id, &d.Name, &d.Phone, &tgID, &d.VehicleType,
			&d.Zone, &d.IsAvailable, &d.IsActive); err != nil {
			return nil, err
		}
		if tgID != nil {
			d.TelegramId = *tgID
		}
		drivers = append(drivers, d)
	}
	return drivers, rows.Err()
}

func DBAddDriver(ctx context.Context, d *pb.Driver, actorID int64) (string, error) {
	var id string
	var tgID *int64
	if d.TelegramId != 0 {
		tgID = &d.TelegramId
	}
	err := pool.QueryRow(ctx, `
		INSERT INTO drivers (name, phone, telegram_id, vehicle_type, zone, is_available, is_active)
		VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`,
		d.Name, d.Phone, tgID, d.VehicleType, d.Zone, d.IsAvailable, d.IsActive,
	).Scan(&id)
	if err != nil {
		return "", err
	}
	DBAuditLog(ctx, actorID, "add_driver", id, fmt.Sprintf("name=%q phone=%s", d.Name, d.Phone))
	return id, nil
}

func DBEditDriver(ctx context.Context, d *pb.Driver, actorID int64) error {
	var tgID *int64
	if d.TelegramId != 0 {
		tgID = &d.TelegramId
	}
	res, err := pool.Exec(ctx, `
		UPDATE drivers SET name=$1, phone=$2, telegram_id=$3, vehicle_type=$4,
		       zone=$5, is_available=$6, is_active=$7
		WHERE id=$8`,
		d.Name, d.Phone, tgID, d.VehicleType, d.Zone, d.IsAvailable, d.IsActive, d.Id,
	)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "edit_driver", d.Id, "updated")
	return nil
}

func DBRemoveDriver(ctx context.Context, id string, actorID int64) error {
	res, err := pool.Exec(ctx, "UPDATE drivers SET is_active=FALSE WHERE id=$1", id)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "remove_driver", id, "deactivated")
	return nil
}

func DBSetDriverAvailability(ctx context.Context, driverID string, avail bool, actorID int64) error {
	res, err := pool.Exec(ctx,
		"UPDATE drivers SET is_available=$1 WHERE id=$2 AND is_active=TRUE", avail, driverID)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	label := "offline"
	if avail {
		label = "on-duty"
	}
	DBAuditLog(ctx, actorID, "driver_availability", driverID, label)
	return nil
}

func DBGetDriverTelegramIDs(ctx context.Context, activeOnly bool) ([]int64, error) {
	q := "SELECT telegram_id FROM drivers WHERE is_active=TRUE AND telegram_id IS NOT NULL"
	if activeOnly {
		q += " AND is_available=TRUE"
	}
	rows, err := pool.Query(ctx, q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// ── Orders ────────────────────────────────────────────────────────────────────

// InsertOrder atomically validates quote integrity, decrements stock,
// inserts the order with items and initial status event, and awards loyalty points.
func InsertOrder(ctx context.Context, req *pb.OrderRequest, cartItems []*pb.CartItem, settings *pb.SystemSettings) (string, string, error) {
	if len(req.QuoteData) == 0 {
		return "", "", ErrQuoteMissing
	}

	// Verify HMAC signature of QuoteData
	if len(req.QuoteData) < 32 {
		return "", "", ErrQuoteMissing
	}
	qdLen := len(req.QuoteData) - 32
	qd := req.QuoteData[:qdLen]
	sig := req.QuoteData[qdLen:]
	
	mac := hmac.New(sha256.New, []byte(os.Getenv("INTERNAL_TOKEN")))
	mac.Write(qd)
	expectedSig := mac.Sum(nil)
	
	if !hmac.Equal(sig, expectedSig) {
		slog.Warn("quote signature invalid", "user", req.UserId)
		return "", "", ErrPriceMismatch
	}

	var quote pb.QuoteResponse
	if err := unmarshalProto(qd, &quote); err == nil {
		const tolerance = 0.02
		diff := quote.GrandTotal - req.ExpectedTotal
		if diff > tolerance || diff < -tolerance {
			slog.Warn("price mismatch",
				"quoted", quote.GrandTotal,
				"claimed", req.ExpectedTotal,
				"user", req.UserId)
			return "", "", ErrPriceMismatch
		}
	}

	idempKey := req.IdempotencyKey
	if idempKey == "" {
		idempKey = fmt.Sprintf("auto-%d-%d", req.UserId, time.Now().UnixNano())
	}

	// Human-readable ref: ORD-YYYYMMDD-NNNN
	var seqVal int64
	if err := pool.QueryRow(ctx, "SELECT nextval('order_ref_seq')").Scan(&seqVal); err != nil {
		return "", "", fmt.Errorf("seq: %w", err)
	}
	humanRef := fmt.Sprintf("ORD-%s-%04d",
		time.Now().Format("20060102"), seqVal)

	// Generate delivery OTP (U-026)
	deliveryOTP := generateOTP()

	tx, err := pool.Begin(ctx)
	if err != nil {
		return "", "", fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	var existing string
	err = tx.QueryRow(ctx,
		"SELECT order_id FROM orders WHERE idempotency_key=$1", idempKey,
	).Scan(&existing)
	if err == nil {
		return existing, "", ErrDuplicateOrder
	}

	// Decrement stock for each cart item via pgx.Batch
	stockBatch := &pgx.Batch{}
	for _, ci := range cartItems {
		if ci.Quantity <= 0 {
			continue
		}
		stockBatch.Queue(`
			UPDATE items SET stock = stock - $1
			WHERE id=$2 AND deleted_at IS NULL AND stock >= $1`,
			ci.Quantity, ci.ItemId,
		)
	}
	br := tx.SendBatch(ctx, stockBatch)
	for _, ci := range cartItems {
		if ci.Quantity <= 0 {
			continue
		}
		ct, err := br.Exec()
		if err != nil {
			_ = br.Close()
			return "", "", fmt.Errorf("stock update for %s: %w", ci.ItemId, err)
		}
		if ct.RowsAffected() == 0 {
			_ = br.Close()
			return "", "", fmt.Errorf("%w: item %s", ErrOutOfStock, ci.ItemId)
		}
	}
	_ = br.Close()

	// Insert order
	var orderID string
	err = tx.QueryRow(ctx, `
		INSERT INTO orders (human_ref, user_id, username, status, total, receipt,
		                    order_notes, delivery_address, scheduled_for, contactless,
		                    recipient_name, delivery_otp, idempotency_key)
		VALUES ($1,$2,$3,'pending',$4,$5,$6,$7,$8,$9,$10,$11,$12)
		RETURNING order_id`,
		humanRef, req.UserId, req.Username, req.ExpectedTotal, req.ReceiptText,
		req.OrderNotes, req.DeliveryAddress, req.ScheduledFor,
		req.Contactless, req.RecipientName, deliveryOTP, idempKey,
	).Scan(&orderID)
	if err != nil {
		if isPGUniqueViolation(err) {
			return "", "", ErrDuplicateOrder
		}
		return "", "", fmt.Errorf("insert order: %w", err)
	}

	// Insert order items (for history/reorder)
	itemsBatch := &pgx.Batch{}
	for _, ci := range cartItems {
		if ci.Quantity <= 0 {
			continue
		}
		itemsBatch.Queue(`
			INSERT INTO order_items (order_id, item_id, item_name, quantity, unit_price, item_note)
			SELECT $1, id, name, $2, price, $3 FROM items WHERE id=$4`,
			orderID, ci.Quantity, ci.Note, ci.ItemId,
		)
	}
	brItems := tx.SendBatch(ctx, itemsBatch)
	for _, ci := range cartItems {
		if ci.Quantity <= 0 {
			continue
		}
		_, err = brItems.Exec()
		if err != nil {
			_ = brItems.Close()
			return "", "", fmt.Errorf("insert order_item: %w", err)
		}
	}
	_ = brItems.Close()
	
	// Track promo usage if one was applied
	if quote.PromoApplied != "" {
		_, err = tx.Exec(ctx, `
			INSERT INTO promo_usage (promo_id, user_id, order_id)
			SELECT id, $1, $2 FROM promos WHERE code=$3`,
			req.UserId, orderID, quote.PromoApplied,
		)
		if err != nil {
			slog.Warn("failed to track promo usage", "order", orderID, "promo", quote.PromoApplied, "err", err)
		}
	}

	// Insert initial status event
	_, err = tx.Exec(ctx, `
		INSERT INTO order_status_history (order_id, status, actor_id)
		VALUES ($1, 'pending', $2)`,
		orderID, req.UserId,
	)
	if err != nil {
		return "", "", fmt.Errorf("status event: %w", err)
	}

	// Award loyalty points
	if settings != nil && settings.LoyaltyEarnRate > 0 {
		pts := int32(req.ExpectedTotal / 100 * float64(settings.LoyaltyEarnRate))
		if pts > 0 {
			_, _ = tx.Exec(ctx, `
				INSERT INTO loyalty (user_id, points) VALUES ($1, $2)
				ON CONFLICT (user_id) DO UPDATE SET points = loyalty.points + $2`,
				req.UserId, pts,
			)
		}
	}

	return orderID, humanRef, tx.Commit(ctx)
}

func DBGetOrder(ctx context.Context, orderID string) (*pb.Order, error) {
	o := &pb.Order{}
	err := pool.QueryRow(ctx, `
		SELECT o.order_id, o.human_ref, o.status, o.total, o.created_at,
		       o.receipt, o.user_id, o.username,
		       COALESCE(o.driver_id,''), COALESCE(d.name,''),
		       o.order_notes, o.delivery_address, o.scheduled_for,
		       o.contactless, o.recipient_name, o.delivery_otp, o.issue_status
		FROM orders o
		LEFT JOIN drivers d ON d.id = o.driver_id
		WHERE o.order_id=$1`,
		orderID,
	).Scan(
		&o.OrderId, &o.HumanRef, &o.Status, &o.Total, &o.Timestamp,
		&o.Receipt, &o.UserId, &o.Username,
		&o.DriverId, &o.DriverName,
		&o.OrderNotes, &o.DeliveryAddress, &o.ScheduledFor,
		&o.Contactless, &o.RecipientName, &o.DeliveryOtp, &o.IssueStatus,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}

	// Load status history
	o.History, _ = dbGetOrderHistory(ctx, orderID)
	o.Notes, _ = dbGetOrderNotes(ctx, orderID)
	return o, nil
}

func dbGetOrderHistory(ctx context.Context, orderID string) ([]*pb.OrderStatusEvent, error) {
	rows, err := pool.Query(ctx, `
		SELECT status, ts, actor_id, note
		FROM order_status_history WHERE order_id=$1 ORDER BY ts`,
		orderID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var history []*pb.OrderStatusEvent
	for rows.Next() {
		e := &pb.OrderStatusEvent{}
		if err := rows.Scan(&e.Status, &e.Ts, &e.ActorId, &e.Note); err != nil {
			return nil, err
		}
		history = append(history, e)
	}
	return history, rows.Err()
}

func dbGetOrderNotes(ctx context.Context, orderID string) ([]*pb.OrderNote, error) {
	rows, err := pool.Query(ctx, `
		SELECT ts, actor_id, text FROM order_notes WHERE order_id=$1 ORDER BY ts`,
		orderID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var notes []*pb.OrderNote
	for rows.Next() {
		n := &pb.OrderNote{}
		if err := rows.Scan(&n.Ts, &n.ActorId, &n.Text); err != nil {
			return nil, err
		}
		notes = append(notes, n)
	}
	return notes, rows.Err()
}

func DBGetUserOrders(ctx context.Context, userID int64, page, pageSize int32) ([]*pb.Order, int32, error) {
	if pageSize <= 0 {
		pageSize = 20
	}
	offset := page * pageSize

	rows, err := pool.Query(ctx, `
		SELECT order_id, human_ref, status, total, created_at, receipt, user_id, username,
		       COALESCE(driver_id,''), '', order_notes, delivery_address,
		       scheduled_for, contactless, recipient_name, delivery_otp, issue_status, count(*) over()
		FROM orders
		WHERE user_id=$1
		ORDER BY created_at DESC
		LIMIT $2 OFFSET $3`,
		userID, pageSize, offset,
	)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	return scanOrdersFlat(rows, true)
}

func DBGetAllOrders(ctx context.Context, req *pb.GetAllOrdersRequest) ([]*pb.Order, int32, error) {
	if req.PageSize <= 0 {
		req.PageSize = 20
	}
	offset := req.Page * req.PageSize

	where := "1=1"
	args := []any{}
	n := 1

	if req.StatusFilter != "" {
		where += fmt.Sprintf(" AND o.status=$%d", n)
		args = append(args, req.StatusFilter)
		n++
	}
	if req.DriverFilter != "" {
		where += fmt.Sprintf(" AND o.driver_id=$%d", n)
		args = append(args, req.DriverFilter)
		n++
	}
	if req.FromTs > 0 {
		where += fmt.Sprintf(" AND o.created_at>=$%d", n)
		args = append(args, req.FromTs)
		n++
	}
	if req.ToTs > 0 {
		where += fmt.Sprintf(" AND o.created_at<=$%d", n)
		args = append(args, req.ToTs)
		n++
	}

	args = append(args, req.PageSize, offset)
	rows, err := pool.Query(ctx, fmt.Sprintf(`
		SELECT o.order_id, o.human_ref, o.status, o.total, o.created_at, o.receipt,
		       o.user_id, o.username, COALESCE(o.driver_id,''), COALESCE(d.name,''),
		       o.order_notes, o.delivery_address, o.scheduled_for,
		       o.contactless, o.recipient_name, o.delivery_otp, o.issue_status, count(*) over()
		FROM orders o
		LEFT JOIN drivers d ON d.id = o.driver_id
		WHERE %s
		ORDER BY o.created_at DESC
		LIMIT $%d OFFSET $%d`, where, n, n+1),
		args...,
	)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	return scanOrdersFlat(rows, true)
}

// DBUpdateOrderStatus enforces status machine transitions and records history.
func DBUpdateOrderStatus(ctx context.Context, orderID, newStatus, reason string, actorID int64) (int64, error) {
	// State machine transitions
	allowed := map[string]map[string]bool{
		"pending":    {"confirmed": true, "cancelled": true},
		"confirmed":  {"dispatched": true, "cancelled": true},
		"dispatched": {"delivered": true, "failed": true},
		"failed":     {"dispatched": true, "cancelled": true},
		// terminal states
		"delivered": {},
		"cancelled": {},
	}

	var current string
	var userID int64
	err := pool.QueryRow(ctx,
		"SELECT status, user_id FROM orders WHERE order_id=$1", orderID,
	).Scan(&current, &userID)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, fmt.Errorf("%w: order %s", ErrNotFound, orderID)
	}
	if err != nil {
		return 0, err
	}

	validNext, ok := allowed[current]
	if !ok {
		return 0, fmt.Errorf("%w: unknown current status %q", ErrInvalidTransition, current)
	}
	if !validNext[newStatus] {
		return 0, fmt.Errorf("%w: %q → %q not permitted", ErrInvalidTransition, current, newStatus)
	}

	tx, err := pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	if _, err := tx.Exec(ctx,
		"UPDATE orders SET status=$1 WHERE order_id=$2", newStatus, orderID); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO order_status_history (order_id, status, actor_id, note)
		VALUES ($1, $2, $3, $4)`,
		orderID, newStatus, actorID, reason,
	); err != nil {
		return 0, err
	}

	DBAuditLog(ctx, actorID, "update_status", orderID,
		fmt.Sprintf("%s→%s reason=%q", current, newStatus, reason))

	return userID, tx.Commit(ctx)
}

func DBCancelOrder(ctx context.Context, orderID string, userID int64, reason string) error {
	// Only allow cancel within window (settings-driven)
	settings, err := GetSystemSettings(ctx)
	if err != nil {
		return err
	}
	windowMins := settings.CancelWindowMinutes
	if windowMins <= 0 {
		windowMins = 5
	}

	var status string
	var createdAt int64
	err = pool.QueryRow(ctx,
		"SELECT status, created_at FROM orders WHERE order_id=$1 AND user_id=$2",
		orderID, userID,
	).Scan(&status, &createdAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return err
	}

	if status != "pending" {
		return fmt.Errorf("order cannot be cancelled in status %q", status)
	}
	age := time.Since(time.Unix(createdAt, 0))
	if age > time.Duration(windowMins)*time.Minute {
		return fmt.Errorf("cancellation window of %d minutes has passed", windowMins)
	}

	_, err = DBUpdateOrderStatus(ctx, orderID, "cancelled", reason, userID)
	return err
}

func DBAssignDriver(ctx context.Context, orderID, driverID string, actorID int64) error {
	res, err := pool.Exec(ctx,
		"UPDATE orders SET driver_id=$1 WHERE order_id=$2", driverID, orderID)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "assign_driver", orderID, fmt.Sprintf("driver=%s", driverID))
	return nil
}

func DBAddOrderNote(ctx context.Context, orderID string, actorID int64, text string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO order_notes (order_id, actor_id, text) VALUES ($1, $2, $3)`,
		orderID, actorID, text,
	)
	return err
}

func DBReportIssue(ctx context.Context, r *pb.IssueReport) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	_, err = tx.Exec(ctx, `
		INSERT INTO issue_reports (order_id, user_id, issue_type, description, photo_id)
		VALUES ($1,$2,$3,$4,$5)`,
		r.OrderId, r.UserId, r.IssueType, r.Description, r.PhotoId,
	)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx,
		"UPDATE orders SET issue_status='open' WHERE order_id=$1", r.OrderId)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func DBResolveIssue(ctx context.Context, req *pb.ResolveIssueRequest) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	_, err = tx.Exec(ctx, `
		UPDATE issue_reports
		SET status='resolved', resolution=$1, resolved_by=$2, resolved_at=$3
		WHERE order_id=$4 AND status='open'`,
		req.Resolution, req.AdminId, time.Now().Unix(), req.OrderId,
	)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx,
		"UPDATE orders SET issue_status='resolved' WHERE order_id=$1", req.OrderId)
	if err != nil {
		return err
	}
	DBAuditLog(ctx, req.AdminId, "resolve_issue", req.OrderId,
		fmt.Sprintf("resolution=%s note=%q", req.Resolution, req.Note))
	return tx.Commit(ctx)
}

func scanOrdersFlat(rows pgx.Rows, withTotal bool) ([]*pb.Order, int32, error) {
	orders := make([]*pb.Order, 0, 32)
	var total int32
	for rows.Next() {
		o := &pb.Order{}
		dest := []any{
			&o.OrderId, &o.HumanRef, &o.Status, &o.Total, &o.Timestamp,
			&o.Receipt, &o.UserId, &o.Username, &o.DriverId, &o.DriverName,
			&o.OrderNotes, &o.DeliveryAddress, &o.ScheduledFor,
			&o.Contactless, &o.RecipientName, &o.DeliveryOtp, &o.IssueStatus,
		}
		if withTotal {
			dest = append(dest, &total)
		}
		if err := rows.Scan(dest...); err != nil {
			return nil, 0, err
		}
		orders = append(orders, o)
	}
	return orders, total, rows.Err()
}

// ── Carts ─────────────────────────────────────────────────────────────────────

func DBSaveCart(ctx context.Context, userID int64, itemsJSON string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO carts (user_id, items_json, last_updated) VALUES ($1, $2, $3)
		ON CONFLICT (user_id) DO UPDATE
		  SET items_json=$2, last_updated=$3`,
		userID, itemsJSON, time.Now().Unix(),
	)
	return err
}

func DBGetCart(ctx context.Context, userID int64) (string, int64, error) {
	var j string
	var ts int64
	err := pool.QueryRow(ctx,
		"SELECT items_json, last_updated FROM carts WHERE user_id=$1", userID,
	).Scan(&j, &ts)
	if errors.Is(err, pgx.ErrNoRows) {
		return "[]", 0, nil
	}
	return j, ts, err
}

func DBClearCart(ctx context.Context, userID int64) error {
	_, err := pool.Exec(ctx, "DELETE FROM carts WHERE user_id=$1", userID)
	return err
}

func DBExpireCarts(ctx context.Context, olderThanHours int) error {
	cutoff := time.Now().Add(-time.Duration(olderThanHours) * time.Hour).Unix()
	res, err := pool.Exec(ctx, "DELETE FROM carts WHERE last_updated<$1", cutoff)
	if err != nil {
		return err
	}
	if res.RowsAffected() > 0 {
		slog.Info("expired carts deleted", "count", res.RowsAffected())
	}
	return nil
}

// ── Promotions ────────────────────────────────────────────────────────────────

func DBListPromos(ctx context.Context) ([]*pb.Promo, error) {
	rows, err := pool.Query(ctx, `
		SELECT id, code, description, discount_type, discount_value, min_order_value,
		       max_uses, per_user_limit, valid_from, valid_until, is_active, uses_count
		FROM promos ORDER BY code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var promos []*pb.Promo
	for rows.Next() {
		p := &pb.Promo{}
		if err := rows.Scan(&p.Id, &p.Code, &p.Description, &p.DiscountType,
			&p.DiscountValue, &p.MinOrderValue, &p.MaxUses, &p.PerUserLimit,
			&p.ValidFrom, &p.ValidUntil, &p.IsActive, &p.UsesCount); err != nil {
			return nil, err
		}
		promos = append(promos, p)
	}
	return promos, rows.Err()
}

func DBAddPromo(ctx context.Context, p *pb.Promo, actorID int64) (string, error) {
	var id string
	err := pool.QueryRow(ctx, `
		INSERT INTO promos (code, description, discount_type, discount_value, min_order_value,
		                    max_uses, per_user_limit, valid_from, valid_until, is_active)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id`,
		p.Code, p.Description, p.DiscountType, p.DiscountValue, p.MinOrderValue,
		p.MaxUses, p.PerUserLimit, p.ValidFrom, p.ValidUntil, p.IsActive,
	).Scan(&id)
	if err != nil {
		return "", err
	}
	DBAuditLog(ctx, actorID, "add_promo", id, fmt.Sprintf("code=%q type=%s value=%.2f", p.Code, p.DiscountType, p.DiscountValue))
	return id, nil
}

func DBRemovePromo(ctx context.Context, id string, actorID int64) error {
	res, err := pool.Exec(ctx, "DELETE FROM promos WHERE id=$1", id)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "remove_promo", id, "deleted")
	return nil
}

func DBApplyPromoCode(ctx context.Context, code string, userID int64, cartTotal float64) (*pb.PromoEffect, error) {
	now := time.Now().Unix()
	p := &pb.Promo{}
	err := pool.QueryRow(ctx, `
		SELECT id, discount_type, discount_value, min_order_value, max_uses,
		       per_user_limit, valid_from, valid_until, is_active, uses_count
		FROM promos WHERE code=$1`,
		code,
	).Scan(&p.Id, &p.DiscountType, &p.DiscountValue, &p.MinOrderValue,
		&p.MaxUses, &p.PerUserLimit, &p.ValidFrom, &p.ValidUntil, &p.IsActive, &p.UsesCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return &pb.PromoEffect{Valid: false, ErrorMessage: "promo code not found"}, nil
	}
	if err != nil {
		return nil, err
	}

	if !p.IsActive {
		return &pb.PromoEffect{Valid: false, ErrorMessage: "promo is not active"}, nil
	}
	if p.ValidFrom > 0 && now < p.ValidFrom {
		return &pb.PromoEffect{Valid: false, ErrorMessage: "promo not yet valid"}, nil
	}
	if p.ValidUntil > 0 && now > p.ValidUntil {
		return &pb.PromoEffect{Valid: false, ErrorMessage: "promo has expired"}, nil
	}
	if p.MinOrderValue > 0 && cartTotal < p.MinOrderValue {
		return &pb.PromoEffect{Valid: false, ErrorMessage: fmt.Sprintf("minimum order LKR %.0f required", p.MinOrderValue)}, nil
	}
	if p.MaxUses > 0 && p.UsesCount >= p.MaxUses {
		return &pb.PromoEffect{Valid: false, ErrorMessage: "promo has reached maximum uses"}, nil
	}
	if p.PerUserLimit > 0 {
		var userUses int32
		_ = pool.QueryRow(ctx,
			"SELECT COUNT(*) FROM promo_usage WHERE promo_id=$1 AND user_id=$2",
			p.Id, userID,
		).Scan(&userUses)
		if userUses >= p.PerUserLimit {
			return &pb.PromoEffect{Valid: false, ErrorMessage: "you have already used this promo"}, nil
		}
	}

	var discount float64
	switch p.DiscountType {
	case "percent":
		discount = cartTotal * (p.DiscountValue / 100)
	case "fixed":
		discount = p.DiscountValue
		if discount > cartTotal {
			discount = cartTotal
		}
	}

	return &pb.PromoEffect{Valid: true, Discount: discount, PromoId: p.Id}, nil
}

// ── Loyalty ───────────────────────────────────────────────────────────────────

func DBGetLoyaltyBalance(ctx context.Context, userID int64) (*pb.LoyaltyBalance, error) {
	b := &pb.LoyaltyBalance{UserId: userID}
	err := pool.QueryRow(ctx,
		"SELECT points, tier FROM loyalty WHERE user_id=$1", userID,
	).Scan(&b.Points, &b.Tier)
	if errors.Is(err, pgx.ErrNoRows) {
		b.Points = 0
		b.Tier = "bronze"
		return b, nil
	}
	return b, err
}

func DBAdjustLoyalty(ctx context.Context, adj *pb.LoyaltyAdjustment) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	_, err = tx.Exec(ctx, `
		INSERT INTO loyalty (user_id, points) VALUES ($1, $2)
		ON CONFLICT (user_id) DO UPDATE SET points = GREATEST(0, loyalty.points + $2)`,
		adj.UserId, adj.Delta,
	)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO loyalty_log (user_id, delta, reason, actor_id)
		VALUES ($1,$2,$3,$4)`,
		adj.UserId, adj.Delta, adj.Reason, adj.ActorId,
	)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ── Customer profile ──────────────────────────────────────────────────────────

func DBGetProfile(ctx context.Context, userID int64) (*pb.CustomerProfile, error) {
	p := &pb.CustomerProfile{UserId: userID}
	err := pool.QueryRow(ctx, `
		SELECT display_name, phone, language, birthday, created_at
		FROM customer_profiles WHERE user_id=$1`, userID,
	).Scan(&p.DisplayName, &p.Phone, &p.Language, &p.Birthday, &p.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return p, nil
	}
	return p, err
}

func DBUpsertProfile(ctx context.Context, p *pb.CustomerProfile) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO customer_profiles (user_id, display_name, phone, language, birthday)
		VALUES ($1,$2,$3,$4,$5)
		ON CONFLICT (user_id) DO UPDATE
		  SET display_name=$2, phone=$3, language=$4, birthday=$5`,
		p.UserId, p.DisplayName, p.Phone, p.Language, p.Birthday,
	)
	return err
}

func DBListSavedAddresses(ctx context.Context, userID int64) ([]*pb.SavedAddress, error) {
	rows, err := pool.Query(ctx, `
		SELECT id, user_id, label, address, lat, lon, is_default
		FROM saved_addresses WHERE user_id=$1 ORDER BY is_default DESC, id`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var addrs []*pb.SavedAddress
	for rows.Next() {
		a := &pb.SavedAddress{}
		if err := rows.Scan(&a.Id, &a.UserId, &a.Label, &a.Address, &a.Lat, &a.Lon, &a.IsDefault); err != nil {
			return nil, err
		}
		addrs = append(addrs, a)
	}
	return addrs, rows.Err()
}

func DBSaveAddress(ctx context.Context, a *pb.SavedAddress) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	if a.IsDefault {
		_, err = tx.Exec(ctx,
			"UPDATE saved_addresses SET is_default=FALSE WHERE user_id=$1", a.UserId)
		if err != nil {
			return err
		}
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO saved_addresses (user_id, label, address, lat, lon, is_default)
		VALUES ($1,$2,$3,$4,$5,$6)
		ON CONFLICT (id) DO UPDATE
		  SET label=$2, address=$3, lat=$4, lon=$5, is_default=$6`,
		a.UserId, a.Label, a.Address, a.Lat, a.Lon, a.IsDefault,
	)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func DBDeleteAddress(ctx context.Context, id int64, userID int64) error {
	res, err := pool.Exec(ctx,
		"DELETE FROM saved_addresses WHERE id=$1 AND user_id=$2", id, userID)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// ── Notification preferences ──────────────────────────────────────────────────

func DBGetNotifPrefs(ctx context.Context, userID int64) (*pb.NotifPrefs, error) {
	p := &pb.NotifPrefs{
		UserId: userID, OrderUpdates: true, SystemAlerts: true, Promotions: true,
	}
	err := pool.QueryRow(ctx, `
		SELECT order_updates, promotions, new_products, system_alerts, flash_deals
		FROM notif_prefs WHERE user_id=$1`, userID,
	).Scan(&p.OrderUpdates, &p.Promotions, &p.NewProducts, &p.SystemAlerts, &p.FlashDeals)
	if errors.Is(err, pgx.ErrNoRows) {
		return p, nil
	}
	return p, err
}

func DBSetNotifPrefs(ctx context.Context, p *pb.NotifPrefs) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO notif_prefs (user_id, order_updates, promotions, new_products, system_alerts, flash_deals)
		VALUES ($1,$2,$3,$4,$5,$6)
		ON CONFLICT (user_id) DO UPDATE
		  SET order_updates=$2, promotions=$3, new_products=$4, system_alerts=$5, flash_deals=$6`,
		p.UserId, p.OrderUpdates, p.Promotions, p.NewProducts, p.SystemAlerts, p.FlashDeals,
	)
	return err
}

func DBGetPendingNotifs(ctx context.Context, userID int64) ([]*pb.Notif, error) {
	rows, err := pool.Query(ctx, `
		SELECT id, user_id, type, text, sent, created_at
		FROM pending_notifs WHERE user_id=$1 AND NOT sent ORDER BY created_at`,
		userID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var notifs []*pb.Notif
	for rows.Next() {
		n := &pb.Notif{}
		if err := rows.Scan(&n.Id, &n.UserId, &n.Type, &n.Text, &n.Sent, &n.CreatedAt); err != nil {
			return nil, err
		}
		notifs = append(notifs, n)
	}
	return notifs, rows.Err()
}

func DBEnqueueNotif(ctx context.Context, userID int64, ntype, text string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO pending_notifs (user_id, type, text) VALUES ($1,$2,$3)`,
		userID, ntype, text,
	)
	return err
}

// ── Stock subscriptions ───────────────────────────────────────────────────────

func DBSubscribeStock(ctx context.Context, userID int64, itemID string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO stock_subscriptions (user_id, item_id) VALUES ($1,$2)
		ON CONFLICT DO NOTHING`,
		userID, itemID,
	)
	return err
}

func DBUnsubscribeStock(ctx context.Context, userID int64, itemID string) error {
	_, err := pool.Exec(ctx,
		"DELETE FROM stock_subscriptions WHERE user_id=$1 AND item_id=$2", userID, itemID)
	return err
}

func notifyStockSubscribers(ctx context.Context, itemID string) {
	// Fetch item name for the notification text.
	var name string
	_ = pool.QueryRow(ctx, "SELECT name FROM items WHERE id=$1", itemID).Scan(&name)
	msg := fmt.Sprintf("✅ Good news! *%s* is back in stock. Tap /start to order.", name)

	// Bulk-insert one notification row per subscriber in a single query,
	// eliminating the previous N+1 pattern (one INSERT per subscriber).
	_, _ = pool.Exec(ctx, `
		INSERT INTO pending_notifs (user_id, type, text)
		SELECT user_id, 'stock_restored', $2
		FROM stock_subscriptions
		WHERE item_id = $1
		ON CONFLICT DO NOTHING`,
		itemID, msg,
	)

	// Remove subscriptions after notifying.
	_, _ = pool.Exec(ctx, "DELETE FROM stock_subscriptions WHERE item_id=$1", itemID)
}

// ── Admin users ───────────────────────────────────────────────────────────────

func DBListAdmins(ctx context.Context) ([]*pb.AdminUser, error) {
	rows, err := pool.Query(ctx, `
		SELECT telegram_id, username, role, added_by, added_at
		FROM admin_users ORDER BY added_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var admins []*pb.AdminUser
	for rows.Next() {
		a := &pb.AdminUser{}
		if err := rows.Scan(&a.TelegramId, &a.Username, &a.Role, &a.AddedBy, &a.AddedAt); err != nil {
			return nil, err
		}
		admins = append(admins, a)
	}
	return admins, rows.Err()
}

func DBAddAdmin(ctx context.Context, a *pb.AdminUser, actorID int64) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO admin_users (telegram_id, username, role, added_by)
		VALUES ($1,$2,$3,$4)
		ON CONFLICT (telegram_id) DO UPDATE SET role=$3, username=$2`,
		a.TelegramId, a.Username, a.Role, actorID,
	)
	if err != nil {
		return err
	}
	DBAuditLog(ctx, actorID, "add_admin", fmt.Sprintf("%d", a.TelegramId),
		fmt.Sprintf("role=%s username=%s", a.Role, a.Username))
	return nil
}

func DBRemoveAdmin(ctx context.Context, telegramID int64, actorID int64) error {
	res, err := pool.Exec(ctx,
		"DELETE FROM admin_users WHERE telegram_id=$1", telegramID)
	if err != nil {
		return err
	}
	if res.RowsAffected() == 0 {
		return ErrNotFound
	}
	DBAuditLog(ctx, actorID, "remove_admin", fmt.Sprintf("%d", telegramID), "removed")
	return nil
}

func DBIsAdmin(ctx context.Context, telegramID int64) (bool, string, error) {
	var role string
	err := pool.QueryRow(ctx,
		"SELECT role FROM admin_users WHERE telegram_id=$1", telegramID,
	).Scan(&role)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, "", nil
	}
	return err == nil, role, err
}

// ── Audit log ─────────────────────────────────────────────────────────────────

func DBAuditLog(ctx context.Context, actorID int64, action, targetID, detail string) {
	// fire-and-forget in background to avoid synchronous DB write in hot paths
	go func() {
		ctxBg := context.Background()
		_, err := pool.Exec(ctxBg, `
			INSERT INTO audit_log (actor_id, action, target_id, detail)
			VALUES ($1,$2,$3,$4)`,
			actorID, action, targetID, detail,
		)
		if err != nil {
			slog.Warn("audit log write failed", "err", err, "action", action)
		}
	}()
}

func DBGetAuditLog(ctx context.Context, req *pb.AuditLogRequest) ([]*pb.AuditEntry, int32, error) {
	if req.PageSize <= 0 {
		req.PageSize = 50
	}
	offset := req.Page * req.PageSize

	where := "1=1"
	args := []any{}
	n := 1

	if req.ActorFilter != 0 {
		where += fmt.Sprintf(" AND actor_id=$%d", n)
		args = append(args, req.ActorFilter)
		n++
	}
	if req.ActionFilter != "" {
		where += fmt.Sprintf(" AND action ILIKE $%d", n)
		args = append(args, "%"+req.ActionFilter+"%")
		n++
	}
	if req.FromTs > 0 {
		where += fmt.Sprintf(" AND ts>=$%d", n)
		args = append(args, req.FromTs)
		n++
	}
	if req.ToTs > 0 {
		where += fmt.Sprintf(" AND ts<=$%d", n)
		args = append(args, req.ToTs)
		n++
	}

	args = append(args, req.PageSize, offset)
	rows, err := pool.Query(ctx, fmt.Sprintf(`
		SELECT id, ts, actor_id, action, target_id, detail, count(*) over()
		FROM audit_log WHERE %s
		ORDER BY ts DESC LIMIT $%d OFFSET $%d`, where, n, n+1),
		args...,
	)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	var entries []*pb.AuditEntry
	var total int32
	for rows.Next() {
		e := &pb.AuditEntry{}
		if err := rows.Scan(&e.Id, &e.Ts, &e.ActorId, &e.Action, &e.TargetId, &e.Detail, &total); err != nil {
			return nil, 0, err
		}
		entries = append(entries, e)
	}
	return entries, total, rows.Err()
}

// ── Analytics ─────────────────────────────────────────────────────────────────

func DBGetRevenueSummary(ctx context.Context, fromTS, toTS int64) (*pb.RevenueSummary, error) {
	s := &pb.RevenueSummary{}
	err := pool.QueryRow(ctx, `
		SELECT COALESCE(SUM(total),0), COUNT(*), COALESCE(AVG(total),0)
		FROM orders
		WHERE status='delivered' AND created_at>=$1 AND created_at<=$2`,
		fromTS, toTS,
	).Scan(&s.TotalRevenue, &s.OrderCount, &s.AvgOrderValue)
	return s, err
}

func DBGetTopItems(ctx context.Context, fromTS, toTS int64, limit int32) ([]*pb.TopItemEntry, error) {
	if limit <= 0 {
		limit = 10
	}
	rows, err := pool.Query(ctx, `
		SELECT oi.item_id, oi.item_name,
		       SUM(oi.quantity) AS units_sold,
		       SUM(oi.quantity * oi.unit_price) AS revenue
		FROM order_items oi
		JOIN orders o ON o.order_id = oi.order_id
		WHERE o.status='delivered' AND o.created_at>=$1 AND o.created_at<=$2
		GROUP BY oi.item_id, oi.item_name
		ORDER BY units_sold DESC
		LIMIT $3`,
		fromTS, toTS, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var items []*pb.TopItemEntry
	for rows.Next() {
		e := &pb.TopItemEntry{}
		if err := rows.Scan(&e.ItemId, &e.Name, &e.UnitsSold, &e.Revenue); err != nil {
			return nil, err
		}
		items = append(items, e)
	}
	return items, rows.Err()
}

func DBGetOrderVolumeTrend(ctx context.Context, fromTS, toTS int64) ([]*pb.OrderVolumePoint, error) {
	rows, err := pool.Query(ctx, `
		SELECT DATE_TRUNC('day', to_timestamp(created_at)) AS day, COUNT(*)
		FROM orders
		WHERE created_at>=$1 AND created_at<=$2
		GROUP BY day ORDER BY day`,
		fromTS, toTS,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var points []*pb.OrderVolumePoint
	for rows.Next() {
		p := &pb.OrderVolumePoint{}
		var t time.Time
		if err := rows.Scan(&t, &p.Count); err != nil {
			return nil, err
		}
		p.DateTs = t.Unix()
		points = append(points, p)
	}
	return points, rows.Err()
}

func DBGetLowStock(ctx context.Context, threshold int32) ([]*pb.LowStockEntry, error) {
	if threshold <= 0 {
		threshold = 10
	}
	rows, err := pool.Query(ctx, `
		SELECT id, name, stock, reorder_level
		FROM items
		WHERE deleted_at IS NULL AND stock <= $1
		ORDER BY stock ASC`,
		threshold,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var items []*pb.LowStockEntry
	for rows.Next() {
		e := &pb.LowStockEntry{}
		if err := rows.Scan(&e.ItemId, &e.Name, &e.Stock, &e.Threshold); err != nil {
			return nil, err
		}
		items = append(items, e)
	}
	return items, rows.Err()
}

// ── Background tasks ──────────────────────────────────────────────────────────

func IssueBirthdayRewards(ctx context.Context) error {
	today := time.Now()
	month := today.Month()
	day := today.Day()

	// Find users whose birthday is today (compare month/day of epoch)
	rows, err := pool.Query(ctx, `
		SELECT user_id FROM customer_profiles
		WHERE birthday > 0
		AND EXTRACT(MONTH FROM to_timestamp(birthday)) = $1
		AND EXTRACT(DAY   FROM to_timestamp(birthday)) = $2`,
		month, day,
	)
	if err != nil {
		return err
	}
	defer rows.Close()

	for rows.Next() {
		var uid int64
		if err := rows.Scan(&uid); err != nil {
			continue
		}
		_ = DBEnqueueNotif(ctx, uid, "birthday",
			"🎂 Happy Birthday! Here's a special gift from us — use code BDAY10 for 10% off your next order!")
		slog.Info("birthday reward enqueued", "user_id", uid)
	}
	return rows.Err()
}

func CheckLowStockAndAlert(ctx context.Context) error {
	items, err := DBGetLowStock(ctx, 0) // 0 = use per-item reorder_level
	if err != nil || len(items) == 0 {
		return err
	}

	batch := &pgx.Batch{}
	for _, item := range items {
		if item.Stock <= item.Threshold {
			msg := fmt.Sprintf("⚠️ Low stock alert: *%s* has only %d unit(s) left (threshold: %d)",
				item.Name, item.Stock, item.Threshold)
			batch.Queue(`
				INSERT INTO pending_notifs (user_id, type, text)
				SELECT telegram_id, 'low_stock', $1 
				FROM admin_users WHERE role='super_admin'`, msg)
		}
	}
	
	if batch.Len() > 0 {
		br := pool.SendBatch(ctx, batch)
		defer br.Close()
		for i := 0; i < batch.Len(); i++ {
			if _, err := br.Exec(); err != nil {
				return err
			}
		}
	}
	return nil
}

func EnforceScheduledMaintenance(ctx context.Context) error {
	maintUntilStr, err := GetConfigValue(ctx, "maintenance_until")
	if err != nil || maintUntilStr == "" || maintUntilStr == "0" {
		return nil
	}
	maintUntil, _ := strconv.ParseInt(maintUntilStr, 10, 64)
	if maintUntil == 0 {
		return nil
	}

	now := time.Now().Unix()
	modeStr, _ := GetConfigValue(ctx, "maintenance_mode")

	if now < maintUntil && modeStr != "true" {
		_ = SetConfigValue(ctx, "maintenance_mode", "true")
		slog.Info("scheduled maintenance window started")
	} else if now >= maintUntil && modeStr == "true" {
		_ = SetConfigValue(ctx, "maintenance_mode", "false")
		_ = SetConfigValue(ctx, "maintenance_until", "0")
		slog.Info("scheduled maintenance window ended")
	}
	return nil
}

// ── Utility helpers ───────────────────────────────────────────────────────────

func isPGUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func parseFloat(s string, def float64) float64 {
	if s == "" {
		return def
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return def
	}
	return v
}



func parseInt32(s string, def int32) int32 {
	if s == "" {
		return def
	}
	v, err := strconv.ParseInt(s, 10, 32)
	if err != nil {
		return def
	}
	return int32(v) // #nosec G115 -- safe cast since parsed size is capped to 32 bits
}

func envInt32(key string, def int32) int32 {
	s := os.Getenv(key)
	if s == "" {
		return def
	}
	v, err := strconv.ParseInt(s, 10, 32)
	if err != nil {
		return def
	}
	return int32(v) // #nosec G115 -- safe cast since parsed size is capped to 32 bits
}
