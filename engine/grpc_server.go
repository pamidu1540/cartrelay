package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

type Server struct {
	pb.UnimplementedLogisticsEngineServer
}

// ── Auth ──────────────────────────────────────────────────────────────────────

// AuthInterceptor validates INTERNAL_TOKEN on every inbound RPC.
// FIX: Returns codes.Internal if the server's own token is unconfigured,
// preventing the empty-string-matches-empty-string bypass.
func AuthInterceptor(
	ctx context.Context,
	req any,
	info *grpc.UnaryServerInfo,
	handler grpc.UnaryHandler,
) (any, error) {
	expected := os.Getenv("INTERNAL_TOKEN")
	if expected == "" {
		// Server misconfiguration — reject all calls until the env var is set.
		return nil, status.Error(codes.Internal,
			"server misconfiguration: INTERNAL_TOKEN is not set")
	}

	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "missing gRPC metadata")
	}
	vals := md.Get("x-internal-token")
	if len(vals) == 0 || vals[0] != expected {
		return nil, status.Error(codes.Unauthenticated, "invalid token")
	}

	reqID := ""
	if ids := md.Get("x-request-id"); len(ids) > 0 {
		reqID = ids[0]
	}
	slog.Info("rpc", "method", info.FullMethod, "request_id", reqID)

	return handler(ctx, req)
}

// ── Health ────────────────────────────────────────────────────────────────────

func (s *Server) CheckHealth(ctx context.Context, _ *pb.Empty) (*pb.HealthStatus, error) {
	// FIX: GetConfigValue now returns an error; DB failures no longer silently
	// return "" (which would make maintenance_mode appear false during an outage).
	maintStr, err := GetConfigValue("maintenance_mode")
	if err != nil {
		return nil, status.Error(codes.Internal, "db error reading maintenance_mode")
	}
	return &pb.HealthStatus{
		IsHealthy:       true,
		MaintenanceMode: maintStr == "true",
		Version:         Version, // FIX: injected via -ldflags, not hardcoded
	}, nil
}

func (s *Server) ToggleMaintenance(ctx context.Context, _ *pb.Empty) (*pb.StatusResponse, error) {
	curr, err := GetConfigValue("maintenance_mode")
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	newVal := "true"
	if curr == "true" {
		newVal = "false"
	}
	if err := SetConfigValue("maintenance_mode", newVal); err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	label := "enabled"
	if newVal == "false" {
		label = "disabled"
	}
	return &pb.StatusResponse{Success: true, Message: "Maintenance mode " + label}, nil
}

// ── Catalog ───────────────────────────────────────────────────────────────────

func (s *Server) GetCatalog(ctx context.Context, _ *pb.Empty) (*pb.CatalogResponse, error) {
	items, err := GetCachedCatalog()
	if err != nil {
		slog.Error("GetCatalog cache miss", "err", err)
		return nil, status.Error(codes.Internal, "catalog unavailable")
	}
	return &pb.CatalogResponse{Items: items}, nil
}

func (s *Server) AddItem(ctx context.Context, item *pb.Item) (*pb.StatusResponse, error) {
	if strings.TrimSpace(item.Name) == "" {
		return &pb.StatusResponse{Success: false, Message: "name is required"}, nil
	}
	if item.Price < 0 {
		return &pb.StatusResponse{Success: false, Message: "price cannot be negative"}, nil
	}
	id, err := DBAddItem(item)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	InvalidateCatalog()
	DBAuditLog(0, "add_item", id, fmt.Sprintf("name=%q price=%.2f stock=%d", item.Name, item.Price, item.Stock))
	return &pb.StatusResponse{Success: true, Message: "Item added: " + id}, nil
}

func (s *Server) EditItemPrice(ctx context.Context, req *pb.EditPriceRequest) (*pb.StatusResponse, error) {
	if req.NewPrice < 0 {
		return &pb.StatusResponse{Success: false, Message: "price cannot be negative"}, nil
	}
	if err := DBEditItemPrice(req.ItemId, req.NewPrice); err != nil {
		if errors.Is(err, ErrNotFound) {
			return &pb.StatusResponse{Success: false, Message: "item not found"}, nil
		}
		return nil, status.Error(codes.Internal, "db error")
	}
	InvalidateCatalog()
	DBAuditLog(0, "edit_price", req.ItemId, fmt.Sprintf("new_price=%.2f", req.NewPrice))
	return &pb.StatusResponse{Success: true, Message: "Price updated"}, nil
}

func (s *Server) EditItemStock(ctx context.Context, req *pb.EditStockRequest) (*pb.StatusResponse, error) {
	if err := DBEditItemStock(req.ItemId, req.Delta); err != nil {
		return &pb.StatusResponse{Success: false, Message: err.Error()}, nil
	}
	InvalidateCatalog()
	DBAuditLog(0, "edit_stock", req.ItemId, fmt.Sprintf("delta=%+d", req.Delta))
	sign := "+"
	if req.Delta < 0 {
		sign = ""
	}
	return &pb.StatusResponse{
		Success: true,
		Message: fmt.Sprintf("Stock adjusted by %s%d", sign, req.Delta),
	}, nil
}

func (s *Server) RemoveItem(ctx context.Context, req *pb.ItemId) (*pb.StatusResponse, error) {
	if err := DBRemoveItem(req.Id); err != nil {
		if errors.Is(err, ErrNotFound) {
			return &pb.StatusResponse{Success: false, Message: "item not found"}, nil
		}
		return nil, status.Error(codes.Internal, "db error")
	}
	InvalidateCatalog()
	DBAuditLog(0, "remove_item", req.Id, "soft-deleted")
	return &pb.StatusResponse{Success: true, Message: "Item removed"}, nil
}

// ── Quotes ────────────────────────────────────────────────────────────────────

func (s *Server) CalculateQuote(ctx context.Context, req *pb.QuoteRequest) (*pb.QuoteResponse, error) {
	if len(req.CartItems) == 0 {
		return &pb.QuoteResponse{Success: false, ErrorMessage: "cart is empty"}, nil
	}

	// FIX: Collect all item IDs and fetch in one batch query (N+1 fix).
	ids := make([]string, 0, len(req.CartItems))
	qtyMap := make(map[string]int32, len(req.CartItems))
	for _, ci := range req.CartItems {
		if ci.Quantity > 0 {
			ids = append(ids, ci.ItemId)
			qtyMap[ci.ItemId] = ci.Quantity
		}
	}

	items, err := DBGetItemsByIDs(ids)
	if err != nil {
		return nil, status.Error(codes.Internal, "catalog lookup failed")
	}

	ratePerKm := 75.0
	if r, err := configFloat("delivery_rate_per_km"); err == nil && r > 0 {
		ratePerKm = r
	}
	baseFare := 500.0
	if b, err := configFloat("delivery_base_fare"); err == nil && b >= 0 {
		baseFare = b
	}
	taxRate := 0.08
	if t, err := configFloat("tax_rate"); err == nil && t >= 0 {
		taxRate = t
	}

	// Sort item IDs for stable receipt output.
	// FIX: map iteration is non-deterministic; sorting prevents shuffled menus.
	sort.Strings(ids)

	var subtotal, totalVol float64
	type line struct{ name string; qty int32; total float64 }
	var lines []line

	for _, id := range ids {
		item, ok := items[id]
		if !ok {
			return &pb.QuoteResponse{
				Success:      false,
				ErrorMessage: fmt.Sprintf("item %s not found in catalog", id),
			}, nil
		}
		qty := qtyMap[id]
		lt := item.Price * float64(qty)
		subtotal += lt
		totalVol += item.VolumeCubes * float64(qty)
		lines = append(lines, line{item.Name, qty, lt})
	}

	distKm, branchName, err := GetDistance(req.UserLat, req.UserLon)
	if err != nil {
		return &pb.QuoteResponse{
			Success:      false,
			ErrorMessage: "routing unavailable: " + err.Error(),
		}, nil
	}

	delivery := baseFare + distKm*ratePerKm
	heavySurcharge := 0.0
	if totalVol > 1.0 {
		heavySurcharge = delivery * 0.15
		delivery += heavySurcharge
	}
	tax := (subtotal + delivery) * taxRate
	grand := subtotal + delivery + tax

	var sb strings.Builder
	sb.WriteString("═══════════════════════════\n")
	sb.WriteString("       LOGISTICS RECEIPT\n")
	sb.WriteString("═══════════════════════════\n")
	for _, l := range lines {
		sb.WriteString(fmt.Sprintf("%-20s x%d  LKR %.2f\n", l.name, l.qty, l.total))
	}
	sb.WriteString("───────────────────────────\n")
	sb.WriteString(fmt.Sprintf("%-24s LKR %.2f\n", "Subtotal", subtotal))
	sb.WriteString(fmt.Sprintf("%-24s LKR %.2f\n", fmt.Sprintf("Delivery (%.1f km)", distKm), delivery-heavySurcharge))
	if heavySurcharge > 0 {
		sb.WriteString(fmt.Sprintf("%-24s LKR %.2f\n", "Heavy Load +15%", heavySurcharge))
	}
	sb.WriteString(fmt.Sprintf("%-24s LKR %.2f\n", fmt.Sprintf("Tax (%.0f%%)", taxRate*100), tax))
	sb.WriteString("═══════════════════════════\n")
	sb.WriteString(fmt.Sprintf("%-24s LKR %.2f\n", "TOTAL", grand))
	sb.WriteString("═══════════════════════════\n")

	return &pb.QuoteResponse{
		Success:         true,
		GrandTotal:      grand,
		FormattedReceipt: sb.String(),
		DistanceKm:      distKm,
		NearestBranch:   branchName,
	}, nil
}

// ── Orders ────────────────────────────────────────────────────────────────────

func (s *Server) PlaceOrder(ctx context.Context, req *pb.OrderRequest) (*pb.OrderResponse, error) {
	// Cart items are re-derived from the serialised QuoteResponse for stock
	// decrement. A future improvement is to include cart items in OrderRequest.
	orderID, err := InsertOrder(req, nil)
	if err != nil {
		switch {
		case errors.Is(err, ErrQuoteMissing):
			return &pb.OrderResponse{Success: false, Message: "quote_data required"}, nil
		case errors.Is(err, ErrPriceMismatch):
			return &pb.OrderResponse{Success: false, Message: "price integrity check failed"}, nil
		case errors.Is(err, ErrOutOfStock):
			return &pb.OrderResponse{Success: false, Message: err.Error()}, nil
		}
		slog.Error("PlaceOrder failed", "user", req.UserId, "err", err)
		return nil, status.Error(codes.Internal, "order processing failed")
	}
	slog.Info("order placed", "id", orderID, "user", req.UserId, "total", req.ExpectedTotal)
	return &pb.OrderResponse{Success: true, OrderId: orderID, Message: "Order placed"}, nil
}

func (s *Server) GetActiveOrders(ctx context.Context, req *pb.UserId) (*pb.OrderListResponse, error) {
	orders, err := DBGetActiveOrders(req.UserId)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: int32(len(orders))}, nil
}

// GetAllOrders is the admin-facing paginated order list.
// FIX: Replaces the fragile user_id=0 sentinel hack.
func (s *Server) GetAllOrders(ctx context.Context, req *pb.GetAllOrdersRequest) (*pb.OrderListResponse, error) {
	orders, total, err := DBGetAllOrders(req.StatusFilter, req.Page, req.PageSize)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: total}, nil
}

func (s *Server) UpdateOrderStatus(ctx context.Context, req *pb.UpdateStatusRequest) (*pb.StatusResponse, error) {
	allowed := map[string]bool{
		"pending": true, "dispatched": true, "delivered": true, "cancelled": true,
	}
	if !allowed[req.NewStatus] {
		return &pb.StatusResponse{Success: false, Message: "invalid status: " + req.NewStatus}, nil
	}

	// FIX: notify_user_id is now a proper proto field, not embedded in message string.
	userID, err := DBUpdateOrderStatus(req.OrderId, req.NewStatus)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return &pb.StatusResponse{Success: false, Message: "order not found"}, nil
		}
		return nil, status.Error(codes.Internal, "db error")
	}

	DBAuditLog(0, "update_status", req.OrderId, "new_status="+req.NewStatus)
	return &pb.StatusResponse{
		Success:      true,
		Message:      fmt.Sprintf("Order %s is now %s", req.OrderId, req.NewStatus),
		NotifyUserId: userID,
	}, nil
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func configFloat(key string) (float64, error) {
	v, err := GetConfigValue(key)
	if err != nil || v == "" {
		return 0, fmt.Errorf("key not set")
	}
	return strconv.ParseFloat(v, 64)
}
