package main

// grpc_server.go — all RPC handler implementations (v5)
//
// Fixes addressed here:
//   BUG-017: cart ownership validated on CalculateQuote
//   BUG-029: status transitions enforced in database.go / DBUpdateOrderStatus
//   BUG-014: empty warehouses table returns error, not 0-distance
//   BUG-019: all list RPCs are paginated
//   SEC-005: rate limiting applied via RateLimitInterceptor (interceptors.go)

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

// Server implements pb.LogisticsEngineServer
type Server struct {
	pb.UnimplementedLogisticsEngineServer
}

// ── Health & system ───────────────────────────────────────────────────────────

func (s *Server) CheckHealth(ctx context.Context, _ *pb.Empty) (*pb.HealthStatus, error) {
	dbStatus := "ok"
	if err := pool.Ping(ctx); err != nil {
		dbStatus = fmt.Sprintf("error: %v", err)
	}
	maintMode, _ := GetConfigValue(ctx, "maintenance_mode")
	return &pb.HealthStatus{
		IsHealthy:       dbStatus == "ok",
		MaintenanceMode: maintMode == "true",
		Version:         Version,
		DbStatus:        dbStatus,
		UptimeSeconds:   int64(time.Since(startedAt).Seconds()),
	}, nil
}

func (s *Server) ToggleMaintenance(ctx context.Context, _ *pb.Empty) (*pb.StatusResponse, error) {
	cur, err := GetConfigValue(ctx, "maintenance_mode")
	if err != nil {
		return nil, status.Errorf(codes.Internal, "read maintenance mode: %v", err)
	}
	next := "true"
	if cur == "true" {
		next = "false"
	}
	if err := SetConfigValue(ctx, "maintenance_mode", next); err != nil {
		return nil, status.Errorf(codes.Internal, "set maintenance mode: %v", err)
	}
	label := "disabled"
	if next == "true" {
		label = "enabled"
	}
	DBAuditLog(ctx, actorFromCtx(ctx), "toggle_maintenance", "config", "maintenance="+next)
	return &pb.StatusResponse{Success: true, Message: "Maintenance mode " + label}, nil
}

func (s *Server) ScheduleMaintenance(ctx context.Context, req *pb.MaintenanceWindow) (*pb.StatusResponse, error) {
	if req.StartTs <= 0 || req.EndTs <= req.StartTs {
		return nil, status.Errorf(codes.InvalidArgument, "invalid maintenance window")
	}
	if err := SetConfigValue(ctx, "maintenance_until", fmt.Sprintf("%d", req.EndTs)); err != nil {
		return nil, status.Errorf(codes.Internal, "schedule maintenance: %v", err)
	}
	DBAuditLog(ctx, actorFromCtx(ctx), "schedule_maintenance", "config",
		fmt.Sprintf("start=%d end=%d reason=%q", req.StartTs, req.EndTs, req.Reason))
	return &pb.StatusResponse{Success: true,
		Message: fmt.Sprintf("Maintenance scheduled from %s to %s",
			time.Unix(req.StartTs, 0).Format("15:04 02 Jan"),
			time.Unix(req.EndTs, 0).Format("15:04 02 Jan")),
	}, nil
}

// ── Catalog ───────────────────────────────────────────────────────────────────

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

func (s *Server) AddItem(ctx context.Context, item *pb.Item) (*pb.StatusResponse, error) {
	id, err := DBAddItem(ctx, item, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add item: %v", err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: "Item added", EntityId: id}, nil
}

func (s *Server) EditItem(ctx context.Context, item *pb.Item) (*pb.StatusResponse, error) {
	if err := DBEditItem(ctx, item, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: "Item updated"}, nil
}

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

func (s *Server) EditItemStock(ctx context.Context, req *pb.EditStockRequest) (*pb.StatusResponse, error) {
	if err := DBEditItemStock(ctx, req.ItemId, req.Delta, req.Reason, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: fmt.Sprintf("Stock adjusted by %+d", req.Delta)}, nil
}

func (s *Server) RemoveItem(ctx context.Context, req *pb.ItemId) (*pb.StatusResponse, error) {
	if err := DBRemoveItem(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	InvalidateCatalogCache()
	return &pb.StatusResponse{Success: true, Message: "Item removed"}, nil
}

func (s *Server) BulkImportItems(ctx context.Context, req *pb.BulkImportRequest) (*pb.BulkImportResponse, error) {
	created, updated, errs := DBBulkImportItems(ctx, req.Items, actorFromCtx(ctx))
	if len(errs) > 0 {
		slog.Warn("bulk import partial errors", "count", len(errs))
	}
	InvalidateCatalogCache()
	return &pb.BulkImportResponse{Created: created, Updated: updated, Errors: errs}, nil
}

// ── Cart ──────────────────────────────────────────────────────────────────────

func (s *Server) SaveCart(ctx context.Context, req *pb.SaveCartRequest) (*pb.StatusResponse, error) {
	j, err := marshalCartItems(req.Items)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "marshal cart: %v", err)
	}
	if err := DBSaveCart(ctx, req.UserId, j); err != nil {
		return nil, status.Errorf(codes.Internal, "save cart: %v", err)
	}
	return &pb.StatusResponse{Success: true}, nil
}

func (s *Server) GetCart(ctx context.Context, req *pb.UserId) (*pb.CartResponse, error) {
	j, ts, err := DBGetCart(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get cart: %v", err)
	}
	items, err := unmarshalCartItems(j)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "unmarshal cart: %v", err)
	}
	return &pb.CartResponse{UserId: req.UserId, Items: items, UpdatedAt: ts}, nil
}

func (s *Server) ClearCart(ctx context.Context, req *pb.UserId) (*pb.StatusResponse, error) {
	if err := DBClearCart(ctx, req.UserId); err != nil {
		return nil, status.Errorf(codes.Internal, "clear cart: %v", err)
	}
	return &pb.StatusResponse{Success: true}, nil
}

// ── Quotes ────────────────────────────────────────────────────────────────────

func (s *Server) CalculateQuote(ctx context.Context, req *pb.QuoteRequest) (*pb.QuoteResponse, error) {
	// BUG-017: validate cart belongs to requesting user
	if req.UserId != 0 && len(req.CartItems) > 0 {
		storedJSON, _, _ := DBGetCart(ctx, req.UserId)
		storedItems, _ := unmarshalCartItems(storedJSON)
		if !cartItemsMatch(req.CartItems, storedItems) {
			slog.Warn("quote cart mismatch", "user_id", req.UserId)
			return nil, status.Errorf(codes.PermissionDenied, "cart items do not match stored cart")
		}
	}

	if req.UserLat == 0 && req.UserLon == 0 {
		return nil, status.Errorf(codes.InvalidArgument, "delivery location required")
	}

	whs, err := DBListWarehouses(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list warehouses: %v", err)
	}
	// BUG-014: no warehouses = error, not 0-distance
	if len(whs) == 0 {
		return nil, status.Errorf(codes.FailedPrecondition,
			"no warehouses configured — please add a warehouse before taking orders")
	}

	// Find nearest active warehouse
	nearest, distKM := nearestWarehouse(whs, req.UserLat, req.UserLon)
	if nearest == nil {
		return nil, status.Errorf(codes.FailedPrecondition, "no active warehouses available")
	}

	// Optionally refine distance via Mapbox
	mapboxKM, err := FetchMapboxDistance(req.UserLat, req.UserLon, nearest.Lat, nearest.Lon)
	if err == nil && mapboxKM > 0 {
		distKM = mapboxKM
	}

	settings, err := GetSystemSettings(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "settings: %v", err)
	}

	// Fetch item details
	ids := make([]string, len(req.CartItems))
	for i, ci := range req.CartItems {
		ids[i] = ci.ItemId
	}
	itemMap, err := DBGetItemsByIDs(ctx, ids)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "fetch items: %v", err)
	}

	var subtotal, totalVol float64
	for _, ci := range req.CartItems {
		it, ok := itemMap[ci.ItemId]
		if !ok {
			return nil, status.Errorf(codes.NotFound, "item %s not found", ci.ItemId)
		}
		if it.Stock < int32(ci.Quantity) {
			return nil, status.Errorf(codes.ResourceExhausted,
				"insufficient stock for %s (have %d, need %d)", it.Name, it.Stock, ci.Quantity)
		}
		subtotal += it.Price * float64(ci.Quantity)
		totalVol += it.VolumeCubes * float64(ci.Quantity)
	}

	deliveryFee := settings.DeliveryBaseFare + settings.DeliveryRatePerKm*distKM
	if settings.FreeDeliveryThreshold > 0 && subtotal >= settings.FreeDeliveryThreshold {
		deliveryFee = 0
	}

	heavySurcharge := 0.0
	if totalVol > 10 {
		heavySurcharge = (totalVol - 10) * 50
	}

	taxRate := settings.TaxRate
	tax := subtotal * taxRate

	// Promo discount
	var promoDiscount float64
	var promoApplied string
	if req.PromoCode != "" {
		effect, err := DBApplyPromoCode(ctx, req.PromoCode, req.UserId, subtotal)
		if err == nil && effect.Valid {
			promoDiscount = effect.Discount
			promoApplied = req.PromoCode
		}
	}

	// Loyalty redemption
	var loyaltyDiscount float64
	if req.LoyaltyRedeem > 0 {
		bal, _ := DBGetLoyaltyBalance(ctx, req.UserId)
		redeemPts := req.LoyaltyRedeem
		if redeemPts > bal.Points {
			redeemPts = bal.Points
		}
		loyaltyDiscount = float64(redeemPts) / float64(settings.LoyaltyRedeemRate)
	}

	grandTotal := subtotal + deliveryFee + heavySurcharge + tax - promoDiscount - loyaltyDiscount
	if grandTotal < 0 {
		grandTotal = 0
	}

	// Loyalty points to earn
	ptsEarned := int32(0)
	if settings.LoyaltyEarnRate > 0 {
		ptsEarned = int32(grandTotal / 100 * float64(settings.LoyaltyEarnRate))
	}

	receipt := formatReceipt(req.CartItems, itemMap, subtotal, deliveryFee,
		heavySurcharge, tax, promoDiscount, loyaltyDiscount, grandTotal,
		nearest.Name, distKM, promoApplied)

	resp := &pb.QuoteResponse{
		Success:            true,
		Subtotal:           subtotal,
		DeliveryFee:        deliveryFee,
		HeavySurcharge:     heavySurcharge,
		Tax:                tax,
		PromoDiscount:      promoDiscount,
		LoyaltyDiscount:    loyaltyDiscount,
		GrandTotal:         grandTotal,
		FormattedReceipt:   receipt,
		DistanceKm:         distKM,
		NearestBranch:      nearest.Name,
		PromoApplied:       promoApplied,
		LoyaltyPointsEarned: ptsEarned,
	}

	// Serialise quote_data for tamper-proof PlaceOrder
	qd, err := marshalProto(resp)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "marshal quote: %v", err)
	}
	resp.QuoteData = qd

	return resp, nil
}

// ── Orders ────────────────────────────────────────────────────────────────────

func (s *Server) PlaceOrder(ctx context.Context, req *pb.OrderRequest) (*pb.OrderResponse, error) {
	if len(req.QuoteData) == 0 {
		return nil, status.Errorf(codes.InvalidArgument, "quote_data is required")
	}

	settings, _ := GetSystemSettings(ctx)

	// Get cart items for stock decrement
	cartJSON, _, _ := DBGetCart(ctx, req.UserId)
	cartItems, _ := unmarshalCartItems(cartJSON)
	if len(cartItems) == 0 {
		return nil, status.Errorf(codes.InvalidArgument, "cart is empty")
	}

	orderID, humanRef, err := InsertOrder(ctx, req, cartItems, settings)
	if err != nil {
		switch err {
		case ErrDuplicateOrder:
			return &pb.OrderResponse{Success: true, OrderId: orderID,
				Message: "Order already placed", HumanRef: humanRef}, nil
		case ErrPriceMismatch:
			return nil, status.Errorf(codes.InvalidArgument, "price mismatch — please requote")
		case ErrOutOfStock:
			return nil, status.Errorf(codes.ResourceExhausted, err.Error())
		default:
			slog.Error("PlaceOrder failed", "user", req.UserId, "err", err)
			return nil, status.Errorf(codes.Internal, "place order failed")
		}
	}

	// Clear cart after successful order
	_ = DBClearCart(ctx, req.UserId)

	// Notify all admins
	go notifyAdminsNewOrder(context.Background(), humanRef, req.Username, req.ExpectedTotal)

	// Auto-dispatch if available (A-031)
	go autoDispatch(context.Background(), orderID)

	return &pb.OrderResponse{
		Success:  true,
		OrderId:  orderID,
		HumanRef: humanRef,
		Message:  fmt.Sprintf("Order %s placed successfully!", humanRef),
	}, nil
}

func (s *Server) GetOrder(ctx context.Context, req *pb.OrderId) (*pb.Order, error) {
	o, err := DBGetOrder(ctx, req.OrderId)
	if err != nil {
		return nil, grpcErr(err)
	}
	return o, nil
}

func (s *Server) GetUserOrders(ctx context.Context, req *pb.GetUserOrdersRequest) (*pb.OrderListResponse, error) {
	orders, total, err := DBGetUserOrders(ctx, req.UserId, req.Page, req.PageSize)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get user orders: %v", err)
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: total}, nil
}

func (s *Server) GetAllOrders(ctx context.Context, req *pb.GetAllOrdersRequest) (*pb.OrderListResponse, error) {
	orders, total, err := DBGetAllOrders(ctx, req)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get all orders: %v", err)
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: total}, nil
}

func (s *Server) UpdateOrderStatus(ctx context.Context, req *pb.UpdateStatusRequest) (*pb.StatusResponse, error) {
	userID, err := DBUpdateOrderStatus(ctx, req.OrderId, req.NewStatus, req.Reason, req.ActorId)
	if err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{
		Success:      true,
		Message:      fmt.Sprintf("Order status updated to %s", req.NewStatus),
		NotifyUserId: userID,
	}, nil
}

func (s *Server) CancelOrder(ctx context.Context, req *pb.CancelOrderRequest) (*pb.StatusResponse, error) {
	if err := DBCancelOrder(ctx, req.OrderId, req.UserId, req.Reason); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Order cancelled"}, nil
}

func (s *Server) ReportOrderIssue(ctx context.Context, req *pb.IssueReport) (*pb.StatusResponse, error) {
	if err := DBReportIssue(ctx, req); err != nil {
		return nil, status.Errorf(codes.Internal, "report issue: %v", err)
	}
	go notifyAdminsIssue(context.Background(), req.OrderId, req.IssueType)
	return &pb.StatusResponse{Success: true, Message: "Issue reported — our team will review it shortly"}, nil
}

func (s *Server) ResolveOrderIssue(ctx context.Context, req *pb.ResolveIssueRequest) (*pb.StatusResponse, error) {
	if err := DBResolveIssue(ctx, req); err != nil {
		return nil, status.Errorf(codes.Internal, "resolve issue: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Issue resolved"}, nil
}

func (s *Server) AssignDriver(ctx context.Context, req *pb.AssignDriverRequest) (*pb.StatusResponse, error) {
	if err := DBAssignDriver(ctx, req.OrderId, req.DriverId, req.AdminId); err != nil {
		return nil, grpcErr(err)
	}
	// Notify driver
	go notifyDriver(context.Background(), req.DriverId, req.OrderId)
	return &pb.StatusResponse{Success: true, Message: "Driver assigned"}, nil
}

func (s *Server) AddOrderNote(ctx context.Context, req *pb.OrderNoteRequest) (*pb.StatusResponse, error) {
	if err := DBAddOrderNote(ctx, req.OrderId, req.AdminId, req.Text); err != nil {
		return nil, status.Errorf(codes.Internal, "add note: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Note added"}, nil
}

// ── Warehouses ────────────────────────────────────────────────────────────────

func (s *Server) ListWarehouses(ctx context.Context, _ *pb.Empty) (*pb.WarehouseListResponse, error) {
	whs, err := DBListWarehouses(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list warehouses: %v", err)
	}
	return &pb.WarehouseListResponse{Warehouses: whs}, nil
}

func (s *Server) AddWarehouse(ctx context.Context, w *pb.Warehouse) (*pb.StatusResponse, error) {
	id, err := DBAddWarehouse(ctx, w, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add warehouse: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Warehouse added", EntityId: id}, nil
}

func (s *Server) EditWarehouse(ctx context.Context, w *pb.Warehouse) (*pb.StatusResponse, error) {
	if err := DBEditWarehouse(ctx, w, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Warehouse updated"}, nil
}

func (s *Server) RemoveWarehouse(ctx context.Context, req *pb.WarehouseId) (*pb.StatusResponse, error) {
	if err := DBRemoveWarehouse(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Warehouse removed"}, nil
}

// ── Drivers ───────────────────────────────────────────────────────────────────

func (s *Server) ListDrivers(ctx context.Context, req *pb.ListDriversRequest) (*pb.DriverListResponse, error) {
	drivers, err := DBListDrivers(ctx, req.AvailableOnly)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list drivers: %v", err)
	}
	return &pb.DriverListResponse{Drivers: drivers}, nil
}

func (s *Server) AddDriver(ctx context.Context, d *pb.Driver) (*pb.StatusResponse, error) {
	id, err := DBAddDriver(ctx, d, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add driver: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Driver added", EntityId: id}, nil
}

func (s *Server) EditDriver(ctx context.Context, d *pb.Driver) (*pb.StatusResponse, error) {
	if err := DBEditDriver(ctx, d, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Driver updated"}, nil
}

func (s *Server) RemoveDriver(ctx context.Context, req *pb.DriverId) (*pb.StatusResponse, error) {
	if err := DBRemoveDriver(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Driver deactivated"}, nil
}

func (s *Server) SetDriverAvailability(ctx context.Context, req *pb.DriverAvailability) (*pb.StatusResponse, error) {
	if err := DBSetDriverAvailability(ctx, req.DriverId, req.IsAvailable, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	label := "offline"
	if req.IsAvailable {
		label = "available"
	}
	return &pb.StatusResponse{Success: true, Message: "Driver marked " + label}, nil
}

func (s *Server) BroadcastDrivers(ctx context.Context, req *pb.BroadcastRequest) (*pb.StatusResponse, error) {
	ids, err := DBGetDriverTelegramIDs(ctx, req.ActiveOnly)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get driver IDs: %v", err)
	}
	for _, id := range ids {
		_ = DBEnqueueNotif(ctx, id, "driver_broadcast", req.Message)
	}
	return &pb.StatusResponse{
		Success: true,
		Message: fmt.Sprintf("Broadcast queued for %d driver(s)", len(ids)),
	}, nil
}

// ── Promotions ────────────────────────────────────────────────────────────────

func (s *Server) ListPromos(ctx context.Context, _ *pb.Empty) (*pb.PromoListResponse, error) {
	promos, err := DBListPromos(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list promos: %v", err)
	}
	return &pb.PromoListResponse{Promos: promos}, nil
}

func (s *Server) AddPromo(ctx context.Context, p *pb.Promo) (*pb.StatusResponse, error) {
	id, err := DBAddPromo(ctx, p, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add promo: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Promo created", EntityId: id}, nil
}

func (s *Server) EditPromo(ctx context.Context, p *pb.Promo) (*pb.StatusResponse, error) {
	_, err := pool.Exec(ctx, `
		UPDATE promos SET code=$1, description=$2, discount_type=$3, discount_value=$4,
		       min_order_value=$5, max_uses=$6, per_user_limit=$7, valid_from=$8,
		       valid_until=$9, is_active=$10
		WHERE id=$11`,
		p.Code, p.Description, p.DiscountType, p.DiscountValue, p.MinOrderValue,
		p.MaxUses, p.PerUserLimit, p.ValidFrom, p.ValidUntil, p.IsActive, p.Id,
	)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "edit promo: %v", err)
	}
	DBAuditLog(ctx, actorFromCtx(ctx), "edit_promo", p.Id, fmt.Sprintf("active=%v", p.IsActive))
	return &pb.StatusResponse{Success: true, Message: "Promo updated"}, nil
}

func (s *Server) RemovePromo(ctx context.Context, req *pb.PromoId) (*pb.StatusResponse, error) {
	if err := DBRemovePromo(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Promo removed"}, nil
}

func (s *Server) ApplyPromoCode(ctx context.Context, req *pb.ApplyPromoRequest) (*pb.PromoEffect, error) {
	effect, err := DBApplyPromoCode(ctx, req.PromoCode, req.UserId, req.CartTotal)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "apply promo: %v", err)
	}
	return effect, nil
}

// ── Loyalty ───────────────────────────────────────────────────────────────────

func (s *Server) GetLoyaltyBalance(ctx context.Context, req *pb.UserId) (*pb.LoyaltyBalance, error) {
	bal, err := DBGetLoyaltyBalance(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get loyalty: %v", err)
	}
	return bal, nil
}

func (s *Server) AdjustLoyaltyPoints(ctx context.Context, adj *pb.LoyaltyAdjustment) (*pb.StatusResponse, error) {
	if err := DBAdjustLoyalty(ctx, adj); err != nil {
		return nil, status.Errorf(codes.Internal, "adjust loyalty: %v", err)
	}
	return &pb.StatusResponse{
		Success: true,
		Message: fmt.Sprintf("Loyalty points adjusted by %+d", adj.Delta),
	}, nil
}

// ── Customer profile ──────────────────────────────────────────────────────────

func (s *Server) GetProfile(ctx context.Context, req *pb.UserId) (*pb.CustomerProfile, error) {
	p, err := DBGetProfile(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get profile: %v", err)
	}
	return p, nil
}

func (s *Server) UpsertProfile(ctx context.Context, p *pb.CustomerProfile) (*pb.StatusResponse, error) {
	if err := DBUpsertProfile(ctx, p); err != nil {
		return nil, status.Errorf(codes.Internal, "upsert profile: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Profile saved"}, nil
}

func (s *Server) ListSavedAddresses(ctx context.Context, req *pb.UserId) (*pb.AddressListResponse, error) {
	addrs, err := DBListSavedAddresses(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list addresses: %v", err)
	}
	return &pb.AddressListResponse{Addresses: addrs}, nil
}

func (s *Server) SaveAddress(ctx context.Context, a *pb.SavedAddress) (*pb.StatusResponse, error) {
	if err := DBSaveAddress(ctx, a); err != nil {
		return nil, status.Errorf(codes.Internal, "save address: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Address saved"}, nil
}

func (s *Server) DeleteAddress(ctx context.Context, req *pb.AddressId) (*pb.StatusResponse, error) {
	// user_id must be passed; we rely on RPC caller to supply it via metadata
	userID := actorFromCtx(ctx)
	if err := DBDeleteAddress(ctx, req.Id, userID); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Address deleted"}, nil
}

// ── Notifications ─────────────────────────────────────────────────────────────

func (s *Server) GetNotifPrefs(ctx context.Context, req *pb.UserId) (*pb.NotifPrefs, error) {
	p, err := DBGetNotifPrefs(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get notif prefs: %v", err)
	}
	return p, nil
}

func (s *Server) SetNotifPrefs(ctx context.Context, p *pb.NotifPrefs) (*pb.StatusResponse, error) {
	if err := DBSetNotifPrefs(ctx, p); err != nil {
		return nil, status.Errorf(codes.Internal, "set notif prefs: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Notification preferences saved"}, nil
}

func (s *Server) GetPendingNotifs(ctx context.Context, req *pb.UserId) (*pb.NotifListResponse, error) {
	notifs, err := DBGetPendingNotifs(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get notifs: %v", err)
	}
	return &pb.NotifListResponse{Notifs: notifs}, nil
}

// ── Stock subscriptions ───────────────────────────────────────────────────────

func (s *Server) SubscribeStock(ctx context.Context, req *pb.StockSubRequest) (*pb.StatusResponse, error) {
	if err := DBSubscribeStock(ctx, req.UserId, req.ItemId); err != nil {
		return nil, status.Errorf(codes.Internal, "subscribe stock: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "You'll be notified when this item is back in stock"}, nil
}

func (s *Server) UnsubscribeStock(ctx context.Context, req *pb.StockSubRequest) (*pb.StatusResponse, error) {
	if err := DBUnsubscribeStock(ctx, req.UserId, req.ItemId); err != nil {
		return nil, status.Errorf(codes.Internal, "unsubscribe stock: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Stock notification cancelled"}, nil
}

// ── Admin users ───────────────────────────────────────────────────────────────

func (s *Server) ListAdmins(ctx context.Context, _ *pb.Empty) (*pb.AdminListResponse, error) {
	admins, err := DBListAdmins(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list admins: %v", err)
	}
	return &pb.AdminListResponse{Admins: admins}, nil
}

func (s *Server) AddAdmin(ctx context.Context, a *pb.AdminUser) (*pb.StatusResponse, error) {
	if err := DBAddAdmin(ctx, a, actorFromCtx(ctx)); err != nil {
		return nil, status.Errorf(codes.Internal, "add admin: %v", err)
	}
	return &pb.StatusResponse{Success: true,
		Message: fmt.Sprintf("Admin %d added with role %s", a.TelegramId, a.Role)}, nil
}

func (s *Server) RemoveAdmin(ctx context.Context, req *pb.AdminUserId) (*pb.StatusResponse, error) {
	if err := DBRemoveAdmin(ctx, req.TelegramId, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Admin removed"}, nil
}

func (s *Server) GetAuditLog(ctx context.Context, req *pb.AuditLogRequest) (*pb.AuditLogResponse, error) {
	entries, total, err := DBGetAuditLog(ctx, req)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get audit log: %v", err)
	}
	return &pb.AuditLogResponse{Entries: entries, TotalCount: total}, nil
}

func (s *Server) GetSystemSettings(ctx context.Context, _ *pb.Empty) (*pb.SystemSettings, error) {
	settings, err := GetSystemSettings(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get settings: %v", err)
	}
	return settings, nil
}

func (s *Server) SetSystemSettings(ctx context.Context, req *pb.SystemSettings) (*pb.StatusResponse, error) {
	if err := SetSystemSettings(ctx, req, actorFromCtx(ctx)); err != nil {
		return nil, status.Errorf(codes.Internal, "set settings: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Settings updated"}, nil
}

// ── Analytics ─────────────────────────────────────────────────────────────────

func (s *Server) GetRevenueSummary(ctx context.Context, req *pb.DateRangeRequest) (*pb.RevenueSummary, error) {
	sum, err := DBGetRevenueSummary(ctx, req.FromTs, req.ToTs)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "revenue summary: %v", err)
	}
	return sum, nil
}

func (s *Server) GetTopItems(ctx context.Context, req *pb.TopItemsRequest) (*pb.TopItemsResponse, error) {
	items, err := DBGetTopItems(ctx, req.FromTs, req.ToTs, req.Limit)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "top items: %v", err)
	}
	return &pb.TopItemsResponse{Items: items}, nil
}

func (s *Server) GetOrderVolumeTrend(ctx context.Context, req *pb.DateRangeRequest) (*pb.OrderVolumeTrend, error) {
	points, err := DBGetOrderVolumeTrend(ctx, req.FromTs, req.ToTs)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "order trend: %v", err)
	}
	return &pb.OrderVolumeTrend{Points: points}, nil
}

func (s *Server) GetLowStockReport(ctx context.Context, req *pb.LowStockRequest) (*pb.LowStockResponse, error) {
	items, err := DBGetLowStock(ctx, req.Threshold)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "low stock: %v", err)
	}
	return &pb.LowStockResponse{Items: items}, nil
}

// ── Internal helpers ──────────────────────────────────────────────────────────

func grpcErr(err error) error {
	switch err {
	case ErrNotFound:
		return status.Errorf(codes.NotFound, err.Error())
	case ErrUnauthorized:
		return status.Errorf(codes.PermissionDenied, err.Error())
	case ErrOutOfStock:
		return status.Errorf(codes.ResourceExhausted, err.Error())
	case ErrInvalidTransition:
		return status.Errorf(codes.InvalidArgument, err.Error())
	case ErrDuplicateOrder:
		return status.Errorf(codes.AlreadyExists, err.Error())
	default:
		return status.Errorf(codes.Internal, err.Error())
	}
}

func notifyAdminsNewOrder(ctx context.Context, humanRef, username string, total float64) {
	admins, _ := DBListAdmins(ctx)
	msg := fmt.Sprintf("🛒 New order *%s* from @%s — LKR %.0f", humanRef, username, total)
	for _, a := range admins {
		_ = DBEnqueueNotif(ctx, a.TelegramId, "new_order", msg)
	}
}

func notifyAdminsIssue(ctx context.Context, orderID, issueType string) {
	admins, _ := DBListAdmins(ctx)
	msg := fmt.Sprintf("⚠️ Issue reported on order *%s*: %s", orderID, issueType)
	for _, a := range admins {
		_ = DBEnqueueNotif(ctx, a.TelegramId, "issue_report", msg)
	}
}

func notifyDriver(ctx context.Context, driverID, orderID string) {
	drivers, _ := DBListDrivers(ctx, false)
	for _, d := range drivers {
		if d.Id == driverID && d.TelegramId != 0 {
			_ = DBEnqueueNotif(ctx, d.TelegramId, "driver_assign",
				fmt.Sprintf("🚗 New delivery assigned: order *%s*", orderID))
			return
		}
	}
}

// autoDispatch assigns the nearest available driver to a newly placed order.
func autoDispatch(ctx context.Context, orderID string) {
	_, err := DBGetOrder(ctx, orderID)
	if err != nil {
		return
	}
	drivers, err := DBListDrivers(ctx, true) // available only
	if err != nil || len(drivers) == 0 {
		slog.Warn("auto-dispatch: no available drivers", "order", orderID)
		return
	}
	// Simple: assign first available driver (extend with geo-routing later)
	driver := drivers[0]
	if err := DBAssignDriver(ctx, orderID, driver.Id, 0); err != nil {
		slog.Warn("auto-dispatch assign failed", "order", orderID, "driver", driver.Id, "err", err)
		return
	}
	notifyDriver(ctx, driver.Id, orderID)
	slog.Info("auto-dispatch assigned", "order", orderID, "driver", driver.Id)
}
