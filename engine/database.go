package main

// database.go — PostgreSQL data-access layer declarations and sentinel errors

import (
	"errors"

	"github.com/jackc/pgx/v5/pgxpool"
)

var pool *pgxpool.Pool

// ── Sentinel errors ───────────────────────────────────────────────────────────

// Sentinel errors returned by database operations across all domain handlers.
var (
	ErrNotFound          = errors.New("not found")
	ErrOutOfStock        = errors.New("out of stock")
	ErrQuoteMissing      = errors.New("quote_data is required")
	ErrPriceMismatch     = errors.New("price mismatch: possible tampering")
	ErrDuplicateOrder    = errors.New("duplicate order: idempotency key already used")
	ErrUnauthorized      = errors.New("unauthorized")
	ErrInvalidTransition = errors.New("invalid status transition")
)
