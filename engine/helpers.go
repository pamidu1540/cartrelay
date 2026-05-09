package main

import (
	"google.golang.org/protobuf/proto"
)

// unmarshalProto is a convenience wrapper around proto.Unmarshal.
func unmarshalProto(data []byte, msg proto.Message) error {
	return proto.Unmarshal(data, msg)
}
