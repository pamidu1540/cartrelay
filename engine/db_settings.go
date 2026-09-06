package main

// db_settings.go — System settings and runtime configuration database operations

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	pb "logistics-engine/api"
)

// GetConfigValue retrieves a configuration value by key.
func GetConfigValue(ctx context.Context, key string) (string, error) {
	var val string
	err := pool.QueryRow(ctx, "SELECT value FROM config WHERE key=$1", key).Scan(&val)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	return val, err
}

// SetConfigValue upserts a configuration key-value pair.
func SetConfigValue(ctx context.Context, key, value string) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO config (key, value) VALUES ($1, $2)
		ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
		key, value,
	)
	return err
}

// GetSystemSettings retrieves all configuration keys and maps them into a SystemSettings object.
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
		DeliveryBaseFare:      parseFloat(m["delivery_base_fare"], 500),
		DeliveryRatePerKm:     parseFloat(m["delivery_rate_per_km"], 75),
		TaxRate:               parseFloat(m["tax_rate"], 0.08),
		FreeDeliveryThreshold: parseFloat(m["free_delivery_threshold"], 5000),
		CancelWindowMinutes:   parseInt32(m["cancel_window_minutes"], 5),
		LoyaltyEarnRate:       parseInt32(m["loyalty_earn_rate"], 10),
		LoyaltyRedeemRate:     parseInt32(m["loyalty_redeem_rate"], 1),
		MaxOrderValue:         parseInt32(m["max_order_value"], 0),
		MinOrderValue:         parseInt32(m["min_order_value"], 0),
		OrderCutoffTime:       m["order_cutoff_time"],
	}, nil
}

// SetSystemSettings updates system configuration values and records an audit log.
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
			_ = br.Close() // #nosec G104 — already returning the primary error
			return fmt.Errorf("set config batch exec: %w", err)
		}
	}
	if err := br.Close(); err != nil {
		return fmt.Errorf("set config batch close: %w", err)
	}
	DBAuditLog(ctx, actorID, "set_system_settings", "config", fmt.Sprintf("%+v", settings))
	return tx.Commit(ctx)
}

// EnforceScheduledMaintenance toggles maintenance mode based on scheduled times.
func EnforceScheduledMaintenance(ctx context.Context) error {
	maintUntilStr, err := GetConfigValue(ctx, "maintenance_until")
	if err != nil || maintUntilStr == "" || maintUntilStr == "0" {
		return nil
	}
	maintUntil, _ := strconv.ParseInt(maintUntilStr, 10, 64)
	if maintUntil == 0 {
		return nil
	}

	maintFromStr, _ := GetConfigValue(ctx, "maintenance_from")
	maintFrom, _ := strconv.ParseInt(maintFromStr, 10, 64)

	now := time.Now().Unix()
	modeStr, _ := GetConfigValue(ctx, "maintenance_mode")

	if now >= maintFrom && now < maintUntil {
		if modeStr != "true" {
			_ = SetConfigValue(ctx, "maintenance_mode", "true")
			slog.Info("scheduled maintenance window started", "from", maintFrom, "until", maintUntil)
		}
	} else if now >= maintUntil {
		if modeStr == "true" {
			_ = SetConfigValue(ctx, "maintenance_mode", "false")
			slog.Info("scheduled maintenance window ended", "until", maintUntil)
		}
		_ = SetConfigValue(ctx, "maintenance_until", "0")
		_ = SetConfigValue(ctx, "maintenance_from", "0")
	}
	return nil
}
