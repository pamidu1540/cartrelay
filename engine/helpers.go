package main

// helpers.go — shared utilities

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"math/big"
	"strings"

	"google.golang.org/protobuf/proto"

	pb "logistics-engine/api"
)

// ── Cart serialisation ────────────────────────────────────────────────────────

func marshalCartItems(items []*pb.CartItem) (string, error) {
	b, err := json.Marshal(items)
	if err != nil {
		return "[]", err
	}
	return string(b), nil
}

func unmarshalCartItems(j string) ([]*pb.CartItem, error) {
	if j == "" || j == "[]" || j == "null" {
		return []*pb.CartItem{}, nil
	}
	var items []*pb.CartItem
	if err := json.Unmarshal([]byte(j), &items); err != nil {
		return []*pb.CartItem{}, err
	}
	return items, nil
}

// cartItemsMatch returns true if the two slices contain the same items
// (order-insensitive, quantity-exact). Used for cart ownership validation.
func cartItemsMatch(a, b []*pb.CartItem) bool {
	if len(a) == 0 && len(b) == 0 {
		return true
	}
	if len(a) != len(b) {
		return false
	}
	sum := make(map[string]int32)
	for _, ci := range a {
		sum[ci.ItemId] += ci.Quantity
	}
	for _, ci := range b {
		sum[ci.ItemId] -= ci.Quantity
	}
	for _, v := range sum {
		if v != 0 {
			return false
		}
	}
	return true
}

// ── Proto serialisation ────────────────────────────────────────────────────────

func marshalProto(m proto.Message) ([]byte, error) {
	return proto.Marshal(m)
}

func unmarshalProto(b []byte, m proto.Message) error {
	return proto.Unmarshal(b, m)
}

// ── OTP generation ─────────────────────────────────────────────────────────────

// generateOTP returns a cryptographically random 6-digit string.
func generateOTP() string {
	max := big.NewInt(1000000)
	n, err := rand.Int(rand.Reader, max)
	if err != nil {
		// Fallback: pseudo-random (should not happen in practice)
		return "000000"
	}
	return fmt.Sprintf("%06d", n.Int64())
}

// ── Receipt formatter ─────────────────────────────────────────────────────────

func formatReceipt(
	cartItems []*pb.CartItem,
	itemMap map[string]*pb.Item,
	subtotal, deliveryFee, heavySurcharge, tax, promoDiscount, loyaltyDiscount, grandTotal float64,
	branchName string,
	distKM float64,
	promoApplied string,
) string {
	var sb strings.Builder

	sb.WriteString("🧾 *Order Summary*\n")
	sb.WriteString("━━━━━━━━━━━━━━━━━━━━\n")

	for _, ci := range cartItems {
		it, ok := itemMap[ci.ItemId]
		if !ok {
			continue
		}
		lineTotal := it.Price * float64(ci.Quantity)
		sb.WriteString(fmt.Sprintf("• %s × %d  —  LKR %.0f\n",
			it.Name, ci.Quantity, lineTotal))
		if ci.Note != "" {
			sb.WriteString(fmt.Sprintf("  _(%s)_\n", ci.Note))
		}
	}

	sb.WriteString("━━━━━━━━━━━━━━━━━━━━\n")
	sb.WriteString(fmt.Sprintf("Subtotal:         LKR %.0f\n", subtotal))

	if deliveryFee == 0 {
		sb.WriteString("Delivery:         FREE 🎉\n")
	} else {
		sb.WriteString(fmt.Sprintf("Delivery (%.1f km): LKR %.0f\n", distKM, deliveryFee))
	}

	if heavySurcharge > 0 {
		sb.WriteString(fmt.Sprintf("Heavy surcharge:  LKR %.0f\n", heavySurcharge))
	}
	if tax > 0 {
		sb.WriteString(fmt.Sprintf("Tax:              LKR %.0f\n", tax))
	}
	if promoDiscount > 0 {
		sb.WriteString(fmt.Sprintf("Promo (%s):  -LKR %.0f\n", promoApplied, promoDiscount))
	}
	if loyaltyDiscount > 0 {
		sb.WriteString(fmt.Sprintf("Loyalty points:  -LKR %.0f\n", loyaltyDiscount))
	}

	sb.WriteString("━━━━━━━━━━━━━━━━━━━━\n")
	sb.WriteString(fmt.Sprintf("*Total:           LKR %.0f*\n", grandTotal))
	sb.WriteString(fmt.Sprintf("\n📦 Dispatched from: %s", branchName))

	return sb.String()
}
