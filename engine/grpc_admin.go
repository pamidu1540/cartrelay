package main

// grpc_admin.go — Administrative, Driver, Warehouse, Promo, Loyalty, Profile, and Analytics gRPC handlers

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

// ── Health & Maintenance ──────────────────────────────────────────────────────

// CheckHealth handles the CheckHealth gRPC request.
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

// ToggleMaintenance handles the ToggleMaintenance gRPC request.
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

// ScheduleMaintenance handles the ScheduleMaintenance gRPC request.
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

// ── Warehouses ────────────────────────────────────────────────────────────────

// ListWarehouses handles the ListWarehouses gRPC request.
func (s *Server) ListWarehouses(ctx context.Context, _ *pb.Empty) (*pb.WarehouseListResponse, error) {
	whs, err := DBListWarehouses(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list warehouses: %v", err)
	}
	return &pb.WarehouseListResponse{Warehouses: whs}, nil
}

// AddWarehouse handles the AddWarehouse gRPC request.
func (s *Server) AddWarehouse(ctx context.Context, w *pb.Warehouse) (*pb.StatusResponse, error) {
	id, err := DBAddWarehouse(ctx, w, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add warehouse: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Warehouse added", EntityId: id}, nil
}

// EditWarehouse handles the EditWarehouse gRPC request.
func (s *Server) EditWarehouse(ctx context.Context, w *pb.Warehouse) (*pb.StatusResponse, error) {
	if err := DBEditWarehouse(ctx, w, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Warehouse updated"}, nil
}

// RemoveWarehouse handles the RemoveWarehouse gRPC request.
func (s *Server) RemoveWarehouse(ctx context.Context, req *pb.WarehouseId) (*pb.StatusResponse, error) {
	if err := DBRemoveWarehouse(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Warehouse removed"}, nil
}

// ── Drivers ───────────────────────────────────────────────────────────────────

// ListDrivers handles the ListDrivers gRPC request.
func (s *Server) ListDrivers(ctx context.Context, req *pb.ListDriversRequest) (*pb.DriverListResponse, error) {
	drivers, err := DBListDrivers(ctx, req.AvailableOnly)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list drivers: %v", err)
	}
	return &pb.DriverListResponse{Drivers: drivers}, nil
}

// AddDriver handles the AddDriver gRPC request.
func (s *Server) AddDriver(ctx context.Context, d *pb.Driver) (*pb.StatusResponse, error) {
	id, err := DBAddDriver(ctx, d, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add driver: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Driver added", EntityId: id}, nil
}

// EditDriver handles the EditDriver gRPC request.
func (s *Server) EditDriver(ctx context.Context, d *pb.Driver) (*pb.StatusResponse, error) {
	if err := DBEditDriver(ctx, d, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Driver updated"}, nil
}

// RemoveDriver handles the RemoveDriver gRPC request.
func (s *Server) RemoveDriver(ctx context.Context, req *pb.DriverId) (*pb.StatusResponse, error) {
	if err := DBRemoveDriver(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Driver deactivated"}, nil
}

// SetDriverAvailability handles the SetDriverAvailability gRPC request.
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

// BroadcastDrivers handles the BroadcastDrivers gRPC request.
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

// ListPromos handles the ListPromos gRPC request.
func (s *Server) ListPromos(ctx context.Context, _ *pb.Empty) (*pb.PromoListResponse, error) {
	promos, err := DBListPromos(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list promos: %v", err)
	}
	return &pb.PromoListResponse{Promos: promos}, nil
}

// AddPromo handles the AddPromo gRPC request.
func (s *Server) AddPromo(ctx context.Context, p *pb.Promo) (*pb.StatusResponse, error) {
	id, err := DBAddPromo(ctx, p, actorFromCtx(ctx))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "add promo: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Promo created", EntityId: id}, nil
}

// EditPromo handles the EditPromo gRPC request.
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

// RemovePromo handles the RemovePromo gRPC request.
func (s *Server) RemovePromo(ctx context.Context, req *pb.PromoId) (*pb.StatusResponse, error) {
	if err := DBRemovePromo(ctx, req.Id, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Promo removed"}, nil
}

// ApplyPromoCode handles the ApplyPromoCode gRPC request.
func (s *Server) ApplyPromoCode(ctx context.Context, req *pb.ApplyPromoRequest) (*pb.PromoEffect, error) {
	effect, err := DBApplyPromoCode(ctx, req.PromoCode, req.UserId, req.CartTotal)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "apply promo: %v", err)
	}
	return effect, nil
}

// ── Loyalty ───────────────────────────────────────────────────────────────────

// GetLoyaltyBalance handles the GetLoyaltyBalance gRPC request.
func (s *Server) GetLoyaltyBalance(ctx context.Context, req *pb.UserId) (*pb.LoyaltyBalance, error) {
	bal, err := DBGetLoyaltyBalance(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get loyalty: %v", err)
	}
	return bal, nil
}

// AdjustLoyaltyPoints handles the AdjustLoyaltyPoints gRPC request.
func (s *Server) AdjustLoyaltyPoints(ctx context.Context, adj *pb.LoyaltyAdjustment) (*pb.StatusResponse, error) {
	if err := DBAdjustLoyalty(ctx, adj); err != nil {
		return nil, status.Errorf(codes.Internal, "adjust loyalty: %v", err)
	}
	return &pb.StatusResponse{
		Success: true,
		Message: fmt.Sprintf("Loyalty points adjusted by %+d", adj.Delta),
	}, nil
}

// ── Customer Profile & Addresses ─────────────────────────────────────────────

// GetProfile handles the GetProfile gRPC request.
func (s *Server) GetProfile(ctx context.Context, req *pb.UserId) (*pb.CustomerProfile, error) {
	p, err := DBGetProfile(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get profile: %v", err)
	}
	return p, nil
}

// UpsertProfile handles the UpsertProfile gRPC request.
func (s *Server) UpsertProfile(ctx context.Context, p *pb.CustomerProfile) (*pb.StatusResponse, error) {
	if err := DBUpsertProfile(ctx, p); err != nil {
		return nil, status.Errorf(codes.Internal, "upsert profile: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Profile saved"}, nil
}

// ListSavedAddresses handles the ListSavedAddresses gRPC request.
func (s *Server) ListSavedAddresses(ctx context.Context, req *pb.UserId) (*pb.AddressListResponse, error) {
	addrs, err := DBListSavedAddresses(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list addresses: %v", err)
	}
	return &pb.AddressListResponse{Addresses: addrs}, nil
}

// SaveAddress handles the SaveAddress gRPC request.
func (s *Server) SaveAddress(ctx context.Context, a *pb.SavedAddress) (*pb.StatusResponse, error) {
	if err := DBSaveAddress(ctx, a); err != nil {
		return nil, status.Errorf(codes.Internal, "save address: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Address saved"}, nil
}

// DeleteAddress handles the DeleteAddress gRPC request.
func (s *Server) DeleteAddress(ctx context.Context, req *pb.AddressId) (*pb.StatusResponse, error) {
	userID := actorFromCtx(ctx)
	if err := DBDeleteAddress(ctx, req.Id, userID); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Address deleted"}, nil
}

// ── Notifications ─────────────────────────────────────────────────────────────

// GetNotifPrefs handles the GetNotifPrefs gRPC request.
func (s *Server) GetNotifPrefs(ctx context.Context, req *pb.UserId) (*pb.NotifPrefs, error) {
	p, err := DBGetNotifPrefs(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get notif prefs: %v", err)
	}
	return p, nil
}

// SetNotifPrefs handles the SetNotifPrefs gRPC request.
func (s *Server) SetNotifPrefs(ctx context.Context, p *pb.NotifPrefs) (*pb.StatusResponse, error) {
	if err := DBSetNotifPrefs(ctx, p); err != nil {
		return nil, status.Errorf(codes.Internal, "set notif prefs: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Notification preferences saved"}, nil
}

// GetPendingNotifs handles the GetPendingNotifs gRPC request.
func (s *Server) GetPendingNotifs(ctx context.Context, req *pb.UserId) (*pb.NotifListResponse, error) {
	notifs, err := DBGetPendingNotifs(ctx, req.UserId)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get notifs: %v", err)
	}
	return &pb.NotifListResponse{Notifs: notifs}, nil
}

// ── Admin Users & Settings ────────────────────────────────────────────────────

// ListAdmins handles the ListAdmins gRPC request.
func (s *Server) ListAdmins(ctx context.Context, _ *pb.Empty) (*pb.AdminListResponse, error) {
	admins, err := DBListAdmins(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "list admins: %v", err)
	}
	return &pb.AdminListResponse{Admins: admins}, nil
}

// AddAdmin handles the AddAdmin gRPC request.
func (s *Server) AddAdmin(ctx context.Context, a *pb.AdminUser) (*pb.StatusResponse, error) {
	if err := DBAddAdmin(ctx, a, actorFromCtx(ctx)); err != nil {
		return nil, status.Errorf(codes.Internal, "add admin: %v", err)
	}
	return &pb.StatusResponse{Success: true,
		Message: fmt.Sprintf("Admin %d added with role %s", a.TelegramId, a.Role)}, nil
}

// RemoveAdmin handles the RemoveAdmin gRPC request.
func (s *Server) RemoveAdmin(ctx context.Context, req *pb.AdminUserId) (*pb.StatusResponse, error) {
	if err := DBRemoveAdmin(ctx, req.TelegramId, actorFromCtx(ctx)); err != nil {
		return nil, grpcErr(err)
	}
	return &pb.StatusResponse{Success: true, Message: "Admin removed"}, nil
}

// GetAuditLog handles the GetAuditLog gRPC request.
func (s *Server) GetAuditLog(ctx context.Context, req *pb.AuditLogRequest) (*pb.AuditLogResponse, error) {
	entries, total, err := DBGetAuditLog(ctx, req)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get audit log: %v", err)
	}
	return &pb.AuditLogResponse{Entries: entries, TotalCount: total}, nil
}

// GetSystemSettings handles the GetSystemSettings gRPC request.
func (s *Server) GetSystemSettings(ctx context.Context, _ *pb.Empty) (*pb.SystemSettings, error) {
	settings, err := GetSystemSettings(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get settings: %v", err)
	}
	return settings, nil
}

// SetSystemSettings handles the SetSystemSettings gRPC request.
func (s *Server) SetSystemSettings(ctx context.Context, req *pb.SystemSettings) (*pb.StatusResponse, error) {
	if err := SetSystemSettings(ctx, req, actorFromCtx(ctx)); err != nil {
		return nil, status.Errorf(codes.Internal, "set settings: %v", err)
	}
	return &pb.StatusResponse{Success: true, Message: "Settings updated"}, nil
}

// ── Analytics ─────────────────────────────────────────────────────────────────

// GetRevenueSummary handles the GetRevenueSummary gRPC request.
func (s *Server) GetRevenueSummary(ctx context.Context, req *pb.DateRangeRequest) (*pb.RevenueSummary, error) {
	sum, err := DBGetRevenueSummary(ctx, req.FromTs, req.ToTs)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "revenue summary: %v", err)
	}
	return sum, nil
}

// GetTopItems handles the GetTopItems gRPC request.
func (s *Server) GetTopItems(ctx context.Context, req *pb.TopItemsRequest) (*pb.TopItemsResponse, error) {
	items, err := DBGetTopItems(ctx, req.FromTs, req.ToTs, req.Limit)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "top items: %v", err)
	}
	return &pb.TopItemsResponse{Items: items}, nil
}

// GetOrderVolumeTrend handles the GetOrderVolumeTrend gRPC request.
func (s *Server) GetOrderVolumeTrend(ctx context.Context, req *pb.DateRangeRequest) (*pb.OrderVolumeTrend, error) {
	points, err := DBGetOrderVolumeTrend(ctx, req.FromTs, req.ToTs)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "order trend: %v", err)
	}
	return &pb.OrderVolumeTrend{Points: points}, nil
}

// GetLowStockReport handles the GetLowStockReport gRPC request.
func (s *Server) GetLowStockReport(ctx context.Context, req *pb.LowStockRequest) (*pb.LowStockResponse, error) {
	items, err := DBGetLowStock(ctx, req.Threshold)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "low stock: %v", err)
	}
	return &pb.LowStockResponse{Items: items}, nil
}
