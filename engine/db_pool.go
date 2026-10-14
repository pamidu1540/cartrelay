package main

// db_pool.go — PostgreSQL connection pooling, migrations, and shared DB utilities

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// InitDatabase initializes the PostgreSQL connection pool using the provided DSN.
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

	p, err := pgxpool.NewWithConfig(ctx, cfg)
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

// CloseDatabase closes the underlying PostgreSQL connection pool.
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
		if err := pool.QueryRow(ctx,
			"SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE filename=$1)", e.Name(),
		).Scan(&exists); err != nil {
			return fmt.Errorf("check migration %s: %w", e.Name(), err)
		}
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
