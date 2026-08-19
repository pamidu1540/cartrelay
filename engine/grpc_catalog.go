package main

// grpc_catalog.go — Catalog, items, stock, and restock subscription gRPC handlers

import (
	"context"
	"fmt"
	"log/slog"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

// ── Catalog RPCs ─────────────────────────────────────────────────────────────

// GetCatalog handles the GetCatalog gRPC request.
func (s *Server) GetCatalog(ctx context.Context, req *pb.CatalogRequest) (*pb.CatalogResponse, error) {
	// Try cache first (catalog_cache.go)
	if req.SearchQuery == "" && req.CategoryFilter == "" {
		if cached, err := GetCachedCatalog(ctx, "", "", req.Page, req.PageSize); err == nil {
			return cached, nil
		}
	}
	items, total, err := FetchCatalogItems(ctx, req.CategoryFilter, req.SearchQuery, req.Page, req.PageSize)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get catalog: %v", err)
	}
	return &pb.CatalogResponse{Items: items, TotalCount: total}, nil
}

// GetItem handles the GetItem gRPC request.
func (s *Server) GetItem(ctx context.Context, req *pb.ItemId) (*pb.Item, error) {
	items, err := DBGetItemsByIDs(ctx, []string{req.Id})
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get item: %v", err)
	}
	it, ok := items[req.Id]
	if !ok {
		return nil, status.Errorf(codes.NotFound, "item %s not found", req.Id)
	}
	return it, nil
}

// AddItem handles the AddItem gRPC request.
func (s *Server) AddItem(ctx context.Context, item *pb.Item) (*pb.StatusResponse, error) {
	id, err := DBAddItem(ctx, item, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add item: %v", err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: "Item added", EntityId: id}, nil
}

// EditItem handles the EditItem gRPC request.
func (s *Server) EditItem(ctx context.Context, item *pb.Item) (*pb.StatusResponse, error) {
	if err := DBEditItem(ctx, item, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: "Item updated"}, nil
}

// EditItemPrice handles the EditItemPrice gRPC request.
func (s *Server) EditItemPrice(ctx context.Context, req *pb.EditPriceRequest) (*pb.StatusResponse, error) {
	if req.NewPrice < 0 {
		return nil, status.Errorf(codes.InvalidArgument, "price cannot be negative")
	}
	if err := DBEditItemPrice(ctx, req.ItemId, req.NewPrice, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: fmt.Sprintf("Price updated to %.2f", req.NewPrice)}, nil
}

// EditItemStock handles the EditItemStock gRPC request.
func (s *Server) EditItemStock(ctx context.Context, req *pb.EditStockRequest) (*pb.StatusResponse, error) {
	if err := DBEditItemStock(ctx, req.ItemId, req.Delta, req.Reason, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: fmt.Sprintf("Stock adjusted by %+d", req.Delta)}, nil
}

// RemoveItem handles the RemoveItem gRPC request.
func (s *Server) RemoveItem(ctx context.Context, req *pb.ItemId) (*pb.StatusResponse, error) {
	if err := DBRemoveItem(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: "Item removed"}, nil
}

// BulkImportItems handles the BulkImportItems gRPC request.
func (s *Server) BulkImportItems(ctx context.Context, req *pb.BulkImportRequest) (*pb.BulkImportResponse, error) {
	created, updated, errs := DBBulkImportItems(ctx, req.Items, actorFromCtx(ctx))
	if len(errs) > 0 {
		slog.Warn("bulk import partial errors", "count", len(errs))
	}
	InvalidateCatalogCache()
	return &pb.BulkImportResponse{Created: created, Updated: updated, Errors: errs}, nil
}

// ── Stock Subscriptions ──────────────────────────────────────────────────────

// SubscribeStock handles the SubscribeStock gRPC request.
func (s *Server) SubscribeStock(ctx context.Context, req *pb.StockSubRequest) (*pb.StatusResponse, error) {
	if err := DBSubscribeStock(ctx, req.UserId, req.ItemId); err != nil {
		return nil, status.Errorf(codes.Internal, "subscribe stock: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "You'll be notified when this item is back in stock"}, nil
}

// UnsubscribeStock handles the UnsubscribeStock gRPC request.
func (s *Server) UnsubscribeStock(ctx context.Context, req *pb.StockSubRequest) (*pb.StatusResponse, error) {
	if err := DBUnsubscribeStock(ctx, req.UserId, req.ItemId); err != nil {
		return nil, status.Errorf(codes.Internal, "unsubscribe stock: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Stock notification cancelled"}, nil
}
