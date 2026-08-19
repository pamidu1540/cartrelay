package main

// db_warehouses_drivers.go — Warehouses and driver management database operations

import (
	"context"
	"fmt"

	pb "logistics-engine/api"
)

// ── Warehouses ────────────────────────────────────────────────────────────────

// DBListWarehouses returns a list of all warehouses ordered by name.
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

// DBAddWarehouse inserts a new warehouse and returns its ID.
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

// DBEditWarehouse updates details for an existing warehouse.
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

// DBRemoveWarehouse deletes a warehouse from the database.
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

// DBListDrivers retrieves active drivers, optionally filtering by availability.
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

// DBAddDriver inserts a new driver record and returns its ID.
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

// DBEditDriver updates an existing driver's details.
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

// DBRemoveDriver soft-deletes a driver by setting them as inactive.
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

// DBSetDriverAvailability toggles the available status of a driver.
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

// DBGetDriverTelegramIDs fetches the Telegram IDs of drivers.
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
