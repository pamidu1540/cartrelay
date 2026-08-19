package main

// db_cart.go — Shopping cart persistence and expiration database operations

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
)

// DBSaveCart upserts the user's shopping cart items as a JSON string.
func DBSaveCart(ctx context.Context, userID int64, itemsJSON string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO carts (user_id, items_json, last_updated) VALUES ($1, $2, $3)
		ON CONFLICT (user_id) DO UPDATE
		  SET items_json=$2, last_updated=$3`,
		userID, itemsJSON, time.Now().Unix(),
	)
	return err
}

// DBGetCart retrieves the JSON string of a user's cart items.
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

// DBClearCart deletes a user's cart from the database.
func DBClearCart(ctx context.Context, userID int64) error {
	_, err := pool.Exec(ctx, "DELETE FROM carts WHERE user_id=$1", userID)
	return err
}

// DBExpireCarts deletes all carts that haven't been updated within a given time frame.
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
