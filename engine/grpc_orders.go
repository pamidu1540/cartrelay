package main

// grpc_orders.go — Cart, Quotes, Order Placement, Order Management, and Notifications

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"os"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"golang.org/x/sync/errgroup"

	pb "logistics-engine/api"
)

// ── Cart RPCs ────────────────────────────────────────────────────────────────

// SaveCart handles the SaveCart gRPC request.
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

// GetCart handles the GetCart gRPC request.
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

// ClearCart handles the ClearCart gRPC request.
func (s *Server) ClearCart(ctx context.Context, req *pb.UserId) (*pb.StatusResponse, error) {
	if err := DBClearCart(ctx, req.UserId); err != nil {
		return nil, status.Errorf(codes.Internal, "clear cart: %v", err)
	}
	return &pb.StatusResponse{Success: true}, nil
}

// ── Quotes RPCs ──────────────────────────────────────────────────────────────

// CalculateQuote handles the CalculateQuote gRPC request.
func (s *Server) CalculateQuote(ctx context.Context, req *pb.QuoteRequest) (*pb.QuoteResponse, error) {
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
	if len(whs) == 0 {
		return nil, status.Errorf(codes.FailedPrecondition,
			"no warehouses configured — please add a warehouse before taking orders")
	}

	// Find nearest active warehouse
	nearest, distKM := nearestWarehouse(whs, req.UserLat, req.UserLon)
	if nearest == nil {
		return nil, status.Errorf(codes.FailedPrecondition, "no active warehouses available")
	}

	var (
		settings *pb.SystemSettings
		itemMap  map[string]*pb.Item
		mapboxKM float64
	)
	g, gCtx := errgroup.WithContext(ctx)

	// Fetch Settings
	g.Go(func() error {
		var err error
		settings, err = GetSystemSettings(gCtx)
		return err
	})

	// Fetch Item Details
	g.Go(func() error {
		ids := make([]string, len(req.CartItems))
		for i, ci := range req.CartItems {
			ids[i] = ci.ItemId
		}
		var err error
		itemMap, err = DBGetItemsByIDs(gCtx, ids)
		return err
	})

	// Fetch Mapbox
	g.Go(func() error {
		mapboxKM, _ = FetchMapboxDistance(nearest.Lat, nearest.Lon, req.UserLat, req.UserLon)
		// Mapbox errors are soft-fails (it already logged, and we'll just use Haversine distKM)
		return nil
	})

	if err := g.Wait(); err != nil {
		return nil, status.Errorf(codes.Internal, "quote dependencies: %v", err)
	}

	if mapboxKM > 0 {
		distKM = mapboxKM
	}

	now := time.Now().Unix()
	var subtotal, totalVol float64
	for _, ci := range req.CartItems {
		it, ok := itemMap[ci.ItemId]
		if !ok {
			return nil, status.Errorf(codes.NotFound, "item %s not found", ci.ItemId)
		}
		if !it.IsAvailable || (it.AvailableFrom > 0 && now < it.AvailableFrom) || (it.AvailableUntil > 0 && now > it.AvailableUntil) {
			return nil, status.Errorf(codes.FailedPrecondition, "item %s is currently not available", it.Name)
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
	if req.LoyaltyRedeem > 0 && settings != nil && settings.LoyaltyRedeemRate > 0 {
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
		Success:             true,
		Subtotal:            subtotal,
		DeliveryFee:         deliveryFee,
		HeavySurcharge:      heavySurcharge,
		Tax:                 tax,
		PromoDiscount:       promoDiscount,
		LoyaltyDiscount:     loyaltyDiscount,
		GrandTotal:          grandTotal,
		FormattedReceipt:    receipt,
		DistanceKm:          distKM,
		NearestBranch:       nearest.Name,
		PromoApplied:        promoApplied,
		LoyaltyPointsEarned: ptsEarned,
	}

	// Serialise quote_data for tamper-proof PlaceOrder
	qd, err := marshalProto(resp)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "marshal quote: %v", err)
	}

	// Secure the quote with HMAC-SHA256
	mac := hmac.New(sha256.New, []byte(os.Getenv("INTERNAL_TOKEN")))
	mac.Write(qd)
	resp.QuoteData = append(qd, mac.Sum(nil)...)

	return resp, nil
}

// ── Orders RPCs ──────────────────────────────────────────────────────────────

// PlaceOrder handles the PlaceOrder gRPC request.
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
			return nil, status.Error(codes.ResourceExhausted, err.Error())
		default:
			slog.Error("PlaceOrder failed", "user", req.UserId, "err", err)
			return nil, status.Errorf(codes.Internal, "place order failed")
		}
	}

	// Clear cart after successful order
	_ = DBClearCart(ctx, req.UserId)

	// Notify all admins
	// #nosec G118 -- background admin notification must outlive request context
	fireAndForget(func(ctx context.Context) {
		notifyAdminsNewOrder(ctx, humanRef, req.Username, req.ExpectedTotal)
	})

	// #nosec G118 -- background auto dispatch must outlive request context
	fireAndForget(func(ctx context.Context) {
		autoDispatch(ctx, orderID)
	})

	return &pb.OrderResponse{
		Success:  true,
		OrderId:  orderID,
		HumanRef: humanRef,
		Message:  fmt.Sprintf("Order %s placed successfully!", humanRef),
	}, nil
}

// GetOrder handles the GetOrder gRPC request.
// It verifies that the caller owns the order or is an admin (actor_id == 0 for internal calls).
func (s *Server) GetOrder(ctx context.Context, req *pb.OrderId) (*pb.Order, error) {
	o, err := DBGetOrder(ctx, req.OrderId)
	if err != nil {
		return nil, grpcErr(err)
	}
	actor := actorFromCtx(ctx)
	if actor != 0 && actor != o.UserId {
		// Check if actor is an admin
		isAdmin, _, _ := DBIsAdmin(ctx, actor)
		if !isAdmin {
			return nil, status.Errorf(codes.PermissionDenied, "order belongs to another user")
		}
	}
	return o, nil
}

// GetUserOrders handles the GetUserOrders gRPC request.
func (s *Server) GetUserOrders(ctx context.Context, req *pb.GetUserOrdersRequest) (*pb.OrderListResponse, error) {
	orders, total, err := DBGetUserOrders(ctx, req.UserId, req.Page, req.PageSize)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get user orders: %v", err)
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: total}, nil
}

// GetAllOrders handles the GetAllOrders gRPC request.
func (s *Server) GetAllOrders(ctx context.Context, req *pb.GetAllOrdersRequest) (*pb.OrderListResponse, error) {
	orders, total, err := DBGetAllOrders(ctx, req)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get all orders: %v", err)
	}
	return &pb.OrderListResponse{Orders: orders, TotalCount: total}, nil
}

// UpdateOrderStatus handles the UpdateOrderStatus gRPC request.
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

// CancelOrder handles the CancelOrder gRPC request.
func (s *Server) CancelOrder(ctx context.Context, req *pb.CancelOrderRequest) (*pb.StatusResponse, error) {
	if err := DBCancelOrder(ctx, req.OrderId, req.UserId, req.Reason); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Order cancelled"}, nil
}

// ReportOrderIssue handles the ReportOrderIssue gRPC request.
func (s *Server) ReportOrderIssue(ctx context.Context, req *pb.IssueReport) (*pb.StatusResponse, error) {
	if err := DBReportIssue(ctx, req); err != nil {
		return nil, status.Errorf(codes.Internal, "report issue: %v", err)
	}
	// #nosec G118 -- background issue notification must outlive request context
	fireAndForget(func(ctx context.Context) {
		notifyAdminsIssue(ctx, req.OrderId, req.IssueType)
	})
	return &pb.StatusResponse{Success: true, Message: "Issue reported — our team will review it shortly"}, nil
}

// ResolveOrderIssue handles the ResolveOrderIssue gRPC request.
func (s *Server) ResolveOrderIssue(ctx context.Context, req *pb.ResolveIssueRequest) (*pb.StatusResponse, error) {
	if err := DBResolveIssue(ctx, req); err != nil {
		return nil, status.Errorf(codes.Internal, "resolve issue: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Issue resolved"}, nil
}

// AssignDriver handles the AssignDriver gRPC request.
func (s *Server) AssignDriver(ctx context.Context, req *pb.AssignDriverRequest) (*pb.StatusResponse, error) {
	if err := DBAssignDriver(ctx, req.OrderId, req.DriverId, req.AdminId); err != nil {
		return nil, grpcErr(err)
	}
	// Notify driver
	// #nosec G118 -- background driver notification must outlive request context
	fireAndForget(func(ctx context.Context) {
		notifyDriver(ctx, req.DriverId, req.OrderId)
	})
	return &pb.StatusResponse{Success: true, Message: "Driver assigned"}, nil
}

// AddOrderNote handles the AddOrderNote gRPC request.
func (s *Server) AddOrderNote(ctx context.Context, req *pb.OrderNoteRequest) (*pb.StatusResponse, error) {
	if err := DBAddOrderNote(ctx, req.OrderId, req.AdminId, req.Text); err != nil {
		return nil, status.Errorf(codes.Internal, "add note: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Note added"}, nil
}

// ── Notification Helpers ─────────────────────────────────────────────────────

func notifyAdminsNewOrder(ctx context.Context, humanRef, username string, total float64) {
	msg := fmt.Sprintf("🛒 New order *%s* from @%s — LKR %.0f", humanRef, username, total)
	_, _ = pool.Exec(ctx, `
		INSERT INTO pending_notifs (user_id, type, text)
		SELECT telegram_id, 'new_order', $1 FROM admin_users
	`, msg)
}

func notifyAdminsIssue(ctx context.Context, orderID, issueType string) {
	msg := fmt.Sprintf("⚠️ Issue reported on order *%s*: %s", orderID, issueType)
	_, _ = pool.Exec(ctx, `
		INSERT INTO pending_notifs (user_id, type, text)
		SELECT telegram_id, 'issue_report', $1 FROM admin_users
	`, msg)
}

func notifyDriver(ctx context.Context, driverID, orderID string) {
	_, _ = pool.Exec(ctx, `
		INSERT INTO pending_notifs (user_id, type, text)
		SELECT telegram_id, 'driver_assign', $2 FROM drivers WHERE id=$1 AND telegram_id != 0
	`, driverID, fmt.Sprintf("🚗 New delivery assigned: order *%s*", orderID))
}

// autoDispatch assigns the nearest available driver to a newly placed order.
func autoDispatch(ctx context.Context, orderID string) {
	var exists bool
	_ = pool.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM orders WHERE order_id=$1)", orderID).Scan(&exists)
	if !exists {
		return
	}
	drivers, err := DBListDrivers(ctx, true)
	if err != nil || len(drivers) == 0 {
		slog.Warn("auto-dispatch: no available drivers", "order", orderID)
		return
	}
	driver := drivers[0]
	if err := DBAssignDriver(ctx, orderID, driver.Id, 0); err != nil {
		slog.Warn("auto-dispatch assign failed", "order", orderID, "driver", driver.Id, "err", err)
		return
	}
	if driver.TelegramId != 0 {
		_ = DBEnqueueNotif(ctx, driver.TelegramId, "driver_assign", fmt.Sprintf("🚗 New delivery assigned: order *%s*", orderID))
	}
	slog.Info("auto-dispatch assigned", "order", orderID, "driver", driver.Id)
}
