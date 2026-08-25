package main

// db_promos_loyalty.go — Promotions, discounts, loyalty points, and rewards

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"

	pb "logistics-engine/api"
)

// ── Promotions ────────────────────────────────────────────────────────────────

// DBListPromos retrieves all promotion codes from the database.
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

// DBAddPromo inserts a new promotion code and returns its ID.
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

// DBRemovePromo deletes a promotion code from the database.
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

// DBApplyPromoCode validates a promotion code and calculates its discount effect.
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

// DBGetLoyaltyBalance returns the loyalty points and tier for a user.
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

// DBAdjustLoyalty applies a delta to a user's loyalty points and logs the transaction.
func DBAdjustLoyalty(ctx context.Context, adj *pb.LoyaltyAdjustment) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	_, err = tx.Exec(ctx, `
		INSERT INTO loyalty (user_id, points) VALUES ($1, GREATEST(0, $2))
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

// IssueBirthdayRewards enqueues birthday reward notifications for eligible users.
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
