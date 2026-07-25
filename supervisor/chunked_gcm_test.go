package main

import (
	"bytes"
	"crypto/rand"
	"io"
	"testing"
)

func TestChunkedGCM_Roundtrip(t *testing.T) {
	key := make([]byte, 32)
	_, _ = rand.Read(key)

	plaintext := []byte("Hello, Logistics Engine encrypted update payload!")

	var encBuf bytes.Buffer
	writer, err := newChunkedGCMWriter(&encBuf, key)
	if err != nil {
		t.Fatalf("newChunkedGCMWriter failed: %v", err)
	}

	if _, err := writer.Write(plaintext); err != nil {
		t.Fatalf("writer.Write failed: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("writer.Close failed: %v", err)
	}

	reader, err := newChunkedGCMReader(&encBuf, key)
	if err != nil {
		t.Fatalf("newChunkedGCMReader failed: %v", err)
	}

	decrypted, err := io.ReadAll(reader)
	if err != nil {
		t.Fatalf("reader ReadAll failed: %v", err)
	}

	if !bytes.Equal(plaintext, decrypted) {
		t.Errorf("decrypted payload does not match original plaintext")
	}
}

func TestChunkedGCM_MultiChunkRoundtrip(t *testing.T) {
	key := make([]byte, 32)
	_, _ = rand.Read(key)

	// Create >128KB payload to cross multiple 64KB chunk boundaries
	plaintext := make([]byte, 180*1024)
	_, _ = rand.Read(plaintext)

	var encBuf bytes.Buffer
	writer, err := newChunkedGCMWriter(&encBuf, key)
	if err != nil {
		t.Fatalf("newChunkedGCMWriter failed: %v", err)
	}

	if _, err := writer.Write(plaintext); err != nil {
		t.Fatalf("writer.Write failed: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("writer.Close failed: %v", err)
	}

	reader, err := newChunkedGCMReader(&encBuf, key)
	if err != nil {
		t.Fatalf("newChunkedGCMReader failed: %v", err)
	}

	decrypted, err := io.ReadAll(reader)
	if err != nil {
		t.Fatalf("reader ReadAll failed: %v", err)
	}

	if !bytes.Equal(plaintext, decrypted) {
		t.Errorf("multi-chunk decrypted payload does not match original plaintext")
	}
}

func TestChunkedGCM_TamperedKey(t *testing.T) {
	key := make([]byte, 32)
	wrongKey := make([]byte, 32)
	_, _ = rand.Read(key)
	_, _ = rand.Read(wrongKey)

	plaintext := []byte("Sensitive OTA data")

	var encBuf bytes.Buffer
	writer, err := newChunkedGCMWriter(&encBuf, key)
	if err != nil {
		t.Fatalf("newChunkedGCMWriter failed: %v", err)
	}
	_, _ = writer.Write(plaintext)
	_ = writer.Close()

	reader, err := newChunkedGCMReader(&encBuf, wrongKey)
	if err != nil {
		t.Fatalf("newChunkedGCMReader failed: %v", err)
	}

	_, err = io.ReadAll(reader)
	if err == nil {
		t.Error("expected decryption error with wrong key, got nil")
	}
}
