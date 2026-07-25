package main

import (
	"bytes"
	"crypto/rand"
	"io"
	"testing"
)

// BenchmarkChunkedGCM_Encrypt benchmarks 64KB AES-256-GCM chunked stream encryption throughput.
func BenchmarkChunkedGCM_Encrypt(b *testing.B) {
	key := make([]byte, 32)
	_, _ = rand.Read(key)

	payload := make([]byte, 64*1024) // 64KB chunk
	_, _ = rand.Read(payload)

	b.SetBytes(int64(len(payload)))
	b.ResetTimer()
	b.ReportAllocs()

	for i := 0; i < b.N; i++ {
		var buf bytes.Buffer
		writer, _ := newChunkedGCMWriter(&buf, key)
		_, _ = writer.Write(payload)
		_ = writer.Close()
	}
}

// BenchmarkChunkedGCM_Decrypt benchmarks 64KB AES-256-GCM chunked stream decryption throughput.
func BenchmarkChunkedGCM_Decrypt(b *testing.B) {
	key := make([]byte, 32)
	_, _ = rand.Read(key)

	payload := make([]byte, 64*1024)
	_, _ = rand.Read(payload)

	var buf bytes.Buffer
	writer, _ := newChunkedGCMWriter(&buf, key)
	_, _ = writer.Write(payload)
	_ = writer.Close()

	encBytes := buf.Bytes()

	b.SetBytes(int64(len(payload)))
	b.ResetTimer()
	b.ReportAllocs()

	for i := 0; i < b.N; i++ {
		reader, _ := newChunkedGCMReader(bytes.NewReader(encBytes), key)
		_, _ = io.ReadAll(reader)
	}
}
