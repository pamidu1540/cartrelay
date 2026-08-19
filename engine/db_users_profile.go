package main

// db_users_profile.go — Customer profile, notifications, admin users, audit log, and analytics

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"

	pb "logistics-engine/api"
)

// ── Customer profile ──────────────────────────────────────────────────────────

// DBGetProfile fetches a user's customer profile.
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

// DBUpsertProfile creates or updates a customer profile.
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

// DBListSavedAddresses returns all saved addresses for a specific user.
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

// DBSaveAddress upserts an address and updates the default status if needed.
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

// DBDeleteAddress removes a saved address for a user.
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

// DBGetNotifPrefs fetches a user's notification preferences.
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

// DBSetNotifPrefs upserts a user's notification preferences.
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

// DBGetPendingNotifs retrieves unsent notifications for a user.
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

// DBEnqueueNotif adds a new pending notification to a user's queue.
func DBEnqueueNotif(ctx context.Context, userID int64, ntype, text string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO pending_notifs (user_id, type, text) VALUES ($1,$2,$3)`,
		userID, ntype, text,
	)
	return err
}

// ── Admin users ───────────────────────────────────────────────────────────────

// DBListAdmins retrieves a list of all admin users from the database.
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

// DBAddAdmin upserts an admin user with their role and username.
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

// DBRemoveAdmin deletes an admin user by their Telegram ID.
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

// DBIsAdmin checks if a Telegram ID belongs to an admin and returns their role.
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

// DBAuditLog asynchronously writes an audit log entry.
func DBAuditLog(ctx context.Context, actorID int64, action, targetID, detail string) {
	// fire-and-forget in background to avoid synchronous DB write in hot paths
	go func() {
		ctxBg, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
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

// DBGetAuditLog retrieves paginated and filtered audit log entries.
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

// DBGetRevenueSummary aggregates the total revenue and order count within a time range.
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

// DBGetTopItems fetches the top items by units sold within a specific time range.
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

// DBGetOrderVolumeTrend returns daily order counts for a given time range.
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

// DBGetLowStock retrieves items with stock levels below a specified threshold.
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
