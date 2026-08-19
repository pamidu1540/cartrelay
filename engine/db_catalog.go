package main

// db_catalog.go — Product, category, stock, and catalog database operations

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	pb "logistics-engine/api"
)

// DBAddItem inserts a new item into the database and returns its ID.
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

// DBEditItem updates an existing item's details if it is not deleted.
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

// DBEditItemPrice updates the price of a specific item.
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

// DBEditItemStock adjusts the stock count of an item by a given delta.
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

// DBRemoveItem soft-deletes an item by setting its deleted_at timestamp.
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

// FetchCatalogItems retrieves a paginated list of available catalog items.
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

// DBGetItemsByIDs fetches a map of items by their unique IDs.
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

// DBBulkImportItems imports or updates a batch of items and returns counts and errors.
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

// DBSubscribeStock subscribes a user to restock alerts for an item.
func DBSubscribeStock(ctx context.Context, userID int64, itemID string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO stock_subscriptions (user_id, item_id) VALUES ($1,$2)
		ON CONFLICT DO NOTHING`,
		userID, itemID,
	)
	return err
}

// DBUnsubscribeStock removes a user's restock subscription for an item.
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

// CheckLowStockAndAlert checks for items below their restock threshold and alerts admins.
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
		defer func() { _ = br.Close() }()
		for i := 0; i < batch.Len(); i++ {
			if _, err := br.Exec(); err != nil {
				return err
			}
		}
	}
	return nil
}
