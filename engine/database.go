package main

import (
	"database/sql"
	_ "modernc.org/sqlite"

	pb "logistics-engine/api"
)

var db *sql.DB

func InitDatabase() error {
	var err error
	db, err = sql.Open("sqlite", "/app/data/logistics.db")
	if err != nil {
		return err
	}

	query := `
	CREATE TABLE IF NOT EXISTS items (
		id TEXT PRIMARY KEY,
		name TEXT,
		price REAL,
		volume_cubes REAL,
		category TEXT,
		stock INTEGER,
		photo_file_id TEXT
	);
	CREATE TABLE IF NOT EXISTS orders (
		order_id TEXT PRIMARY KEY,
		user_id INTEGER,
		username TEXT,
		status TEXT,
		total REAL,
		timestamp INTEGER,
		receipt TEXT
	);
	CREATE TABLE IF NOT EXISTS config (
		key TEXT PRIMARY KEY,
		value TEXT
	);`

	_, err = db.Exec(query)
	return err
}

func FetchCatalogItems() (map[string]*pb.Item, error) {
	rows, err := db.Query("SELECT id, name, price, volume_cubes, category, stock, photo_file_id FROM items")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	items := make(map[string]*pb.Item)
	for rows.Next() {
		var i pb.Item
		if err := rows.Scan(&i.Id, &i.Name, &i.Price, &i.VolumeCubes, &i.Category, &i.Stock, &i.PhotoFileId); err != nil {
			continue
		}
		items[i.Id] = &i
	}
	return items, nil
}