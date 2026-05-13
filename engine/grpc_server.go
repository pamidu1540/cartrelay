package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"os"
	"strconv"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

type Server struct {
	pb.UnimplementedLogisticsEngineServer
}

type contextKey string
const reqIDKey contextKey = "request_id"

// ── Auth Interceptor ──────────────────────────────────────────────────────────

// AuthInterceptor validates HMAC-SHA256 signatures and enforces a 5-minute
// replay window to prevent forgery and replay attacks.
func AuthInterceptor(
	ctx context.Context,
	req any,
	info *grpc.UnaryServerInfo,
	handler grpc.UnaryHandler,
) (any, error) {
	expected := os.Getenv("INTERNAL_TOKEN")
	if expected == "" {
		return nil, status.Error(codes.Internal, "server misconfiguration: INTERNAL_TOKEN is not set")
	}

	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "missing gRPC metadata")
	}

	var reqID, tsStr, signature string
	if ids := md.Get("x-request-id"); len(ids) > 0 { reqID = ids[0] }
	if ts := md.Get("x-timestamp"); len(ts) > 0 { tsStr = ts[0] }
	if sig := md.Get("x-hmac-signature"); len(sig) > 0 { signature = sig[0] }

	// Validate timestamp to prevent replay attacks (5-minute window).
	tsInt, err := strconv.ParseInt(tsStr, 10, 64)
	if err != nil || math.Abs(float64(time.Now().Unix()-tsInt)) > 300 {
		return nil, status.Error(codes.Unauthenticated, "request timestamp expired or invalid")
	}

	// Verify HMAC signature.
	payload := fmt.Sprintf("%s:%s", tsStr, reqID)
	mac := hmac.New(sha256.New, []byte(expected))
	mac.Write([]byte(payload))
	expectedMAC := hex.EncodeToString(mac.Sum(nil))

	if !hmac.Equal([]byte(signature), []byte(expectedMAC)) {
		slog.Warn("auth failure: HMAC mismatch", "method", info.FullMethod, "req_id", reqID)
		return nil, status.Error(codes.Unauthenticated, "invalid HMAC signature")
	}

	// Inject request ID into context for downstream idempotency.
	ctx = context.WithValue(ctx, reqIDKey, reqID)

	return handler(ctx, req)
}

// ── Health & System ───────────────────────────────────────────────────────────

func (s *Server) CheckHealth(ctx context.Context, _ *pb.Empty) (*pb.HealthStatus, error) {
	maintStr, err := GetConfigValue("maintenance_mode")
	if err != nil {
		return nil, status.Error(codes.Internal, "db error reading maintenance_mode")
	}
	return &pb.HealthStatus{
		IsHealthy:       true,
		MaintenanceMode: maintStr == "true",
		Version:         Version,
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
	label := "disabled"
	if newVal == "true" {
		label = "enabled"
	}
	return &pb.StatusResponse{Success: true, Message: "Maintenance mode " + label}, nil
}

// ── Catalog Management ────────────────────────────────────────────────────────

func (s *Server) GetCatalog(ctx context.Context, _ *pb.Empty) (*pb.CatalogResponse, error) {
	items, err := GetCachedCatalog()
	if err != nil {
		return nil, status.Error(codes.Internal, "catalog unavailable")
	}
	return &pb.CatalogResponse{Items: items}, nil
}

func (s *Server) AddItem(ctx context.Context, item *pb.Item) (*pb.StatusResponse, error) {
	if strings.TrimSpace(item.Name) == "" {
		return &pb.StatusResponse{Success: false, Message: "name is required"}, nil
	}
	id, err := DBAddItem(item)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	InvalidateCatalog()
	DBAuditLog(0, "add_item", id, fmt.Sprintf("name=%q price=%.2f", item.Name, item.Price))
	return &pb.StatusResponse{Success: true, Message: "Item added: " + id}, nil
}

func (s *Server) EditItemPrice(ctx context.Context, req *pb.EditPriceRequest) (*pb.StatusResponse, error) {
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
	return &pb.StatusResponse{Success: true, Message: "Stock adjusted"}, nil
}

func (s *Server) RemoveItem(ctx context.Context, req *pb.ItemId) (*pb.StatusResponse, error) {
	if err := DBRemoveItem(req.Id); err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	InvalidateCatalog()
	DBAuditLog(0, "remove_item", req.Id, "soft-deleted")
	return &pb.StatusResponse{Success: true, Message: "Item removed"}, nil
}

// ── Warehouse Management (Phase 1) ────────────────────────────────────────────

func (s *Server) AddWarehouse(ctx context.Context, req *pb.AddWarehouseRequest) (*pb.StatusResponse, error) {
	id, err := DBAddWarehouse(req.Name, req.Lat, req.Lon)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	DBAuditLog(0, "add_warehouse", id, fmt.Sprintf("name=%q", req.Name))
	return &pb.StatusResponse{Success: true, Message: "Warehouse added: " + id}, nil
}

func (s *Server) ListWarehouses(ctx context.Context, _ *pb.Empty) (*pb.ListWarehouseResponse, error) {
	whs, err := DBGetWarehouses()
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	return &pb.ListWarehouseResponse{Warehouses: whs}, nil
}

// ── Logistics & Orders ────────────────────────────────────────────────────────

func (s *Server) CalculateQuote(ctx context.Context, req *pb.QuoteRequest) (*pb.QuoteResponse, error) {
	if len(req.CartItems) == 0 {
		return &pb.QuoteResponse{Success: false, ErrorMessage: "cart is empty"}, nil
	}

	// Resolve the nearest branch while considering cart contents.
	distKm, branchName, err := GetDistance(req.UserLat, req.UserLon, req.CartItems)
	if err != nil {
		return &pb.QuoteResponse{Success: false, ErrorMessage: err.Error()}, nil
	}

	// Fetch item details for the receipt.
	ids := make([]string, 0, len(req.CartItems))
	qtyMap := make(map[string]int32)
	for _, ci := range req.CartItems {
		ids = append(ids, ci.ItemId)
		qtyMap[ci.ItemId] = ci.Quantity
	}
	items, err := DBGetItemsByIDs(ids)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error fetching items")
	}

	// Pricing logic.
	var subtotal float64
	var sb strings.Builder
	sb.WriteString("════ RECEIPT ════\n")
	for _, id := range ids {
		it := items[id]
		lineTotal := it.Price * float64(qtyMap[id])
		subtotal += lineTotal
		sb.WriteString(fmt.Sprintf("%s x%d: LKR %.2f\n", it.Name, qtyMap[id], lineTotal))
	}

	rate, _ := configFloat("delivery_rate_per_km")
	if rate == 0 { rate = 75.0 }
	delivery := distKm * rate
	grand := subtotal + delivery

	sb.WriteString(fmt.Sprintf("─────────────────\nSubtotal: LKR %.2f\n", subtotal))
	sb.WriteString(fmt.Sprintf("Delivery: LKR %.2f (%.1f km)\n", delivery, distKm))
	sb.WriteString(fmt.Sprintf("═════════════════\nTOTAL: LKR %.2f", grand))

	return &pb.QuoteResponse{
		Success:          true,
		GrandTotal:       grand,
		FormattedReceipt: sb.String(),
		DistanceKm:       distKm,
		NearestBranch:    branchName,
	}, nil
}

func (s *Server) PlaceOrder(ctx context.Context, req *pb.OrderRequest) (*pb.OrderResponse, error) {
	// Extract the request ID for idempotency tracking.
	idempotencyKey, _ := ctx.Value(reqIDKey).(string)

	// InsertOrder now handles stock decrement and idempotency atomically.
	orderID, err := InsertOrder(req, nil, idempotencyKey)
	if err != nil {
		if errors.Is(err, ErrDuplicateOrder) {
			return &pb.OrderResponse{Success: false, Message: "Duplicate order prevented."}, nil
		}
		if errors.Is(err, ErrOutOfStock) {
			return &pb.OrderResponse{Success: false, Message: err.Error()}, nil
		}
		return nil, status.Error(codes.Internal, "order placement failed")
	}

	return &pb.OrderResponse{Success: true, OrderId: orderID, Message: "Order placed successfully"}, nil
}

func (s *Server) GetActiveOrders(ctx context.Context, req *pb.UserId) (*pb.OrderListResponse, error) {
	orders, err := DBGetActiveOrders(req.UserId)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: int32(len(orders))}, nil
}

func (s *Server) GetAllOrders(ctx context.Context, req *pb.GetAllOrdersRequest) (*pb.OrderListResponse, error) {
	orders, total, err := DBGetAllOrders(req.StatusFilter, req.Page, req.PageSize)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: total}, nil
}

func (s *Server) UpdateOrderStatus(ctx context.Context, req *pb.UpdateStatusRequest) (*pb.StatusResponse, error) {
	userID, err := DBUpdateOrderStatus(req.OrderId, req.NewStatus)
	if err != nil {
		return nil, status.Error(codes.Internal, "db error")
	}
	DBAuditLog(0, "update_status", req.OrderId, "status="+req.NewStatus)
	return &pb.StatusResponse{Success: true, Message: "Status updated", NotifyUserId: userID}, nil
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func configFloat(key string) (float64, error) {
	v, err := GetConfigValue(key)
	if err != nil || v == "" {
		return 0, fmt.Errorf("not set")
	}
	return strconv.ParseFloat(v, 64)
}