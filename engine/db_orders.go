package main

// db_orders.go — Order creation, state machine, issues, and query database operations

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/jackc/pgx/v5"

	pb "logistics-engine/api"
)

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

// DBGetOrder retrieves an order by its unique ID along with its history and notes.
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

// DBGetUserOrders returns paginated orders for a specific user.
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

// DBGetAllOrders returns paginated orders across all users with optional filtering.
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

// DBCancelOrder cancels an order if it is still within the permissible cancellation window.
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

// DBAssignDriver assigns a driver to a given order.
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

// DBAddOrderNote appends a text note to an order's history.
func DBAddOrderNote(ctx context.Context, orderID string, actorID int64, text string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO order_notes (order_id, actor_id, text) VALUES ($1, $2, $3)`,
		orderID, actorID, text,
	)
	return err
}

// DBReportIssue records a new issue report for a specific order.
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

// DBResolveIssue marks an open issue on an order as resolved.
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
