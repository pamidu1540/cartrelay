package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"strings"
)

const chunkSize = 64 * 1024 // 64KB

type chunkedGCMWriter struct {
	w     io.Writer
	aead  cipher.AEAD
	nonce []byte
	buf   []byte
	pos   int
	cnt   uint32
}

func newChunkedGCMWriter(w io.Writer, key []byte) (*chunkedGCMWriter, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	if _, err := w.Write(nonce); err != nil {
		return nil, err
	}
	return &chunkedGCMWriter{
		w:     w,
		aead:  aead,
		nonce: nonce,
		buf:   make([]byte, chunkSize),
		pos:   0,
		cnt:   0,
	}, nil
}

func (c *chunkedGCMWriter) Write(p []byte) (n int, err error) {
	var written int
	for len(p) > 0 {
		space := chunkSize - c.pos
		if len(p) < space {
			copy(c.buf[c.pos:], p)
			c.pos += len(p)
			written += len(p)
			break
		}
		copy(c.buf[c.pos:], p[:space])
		c.pos += space
		if err := c.flushChunk(); err != nil {
			return written, err
		}
		p = p[space:]
		written += space
	}
	return written, nil
}

func (c *chunkedGCMWriter) flushChunk() error {
	if c.pos == 0 {
		return nil
	}
	chunkNonce := make([]byte, len(c.nonce))
	copy(chunkNonce, c.nonce)
	binary.BigEndian.PutUint32(chunkNonce[len(chunkNonce)-4:], c.cnt)
	c.cnt++

	ciphertext := c.aead.Seal(nil, chunkNonce, c.buf[:c.pos], nil)
	lenBuf := make([]byte, 4)
	// #nosec G115 -- length is bounded by 64KB chunk size + GCM overhead, safe to cast to uint32
	binary.BigEndian.PutUint32(lenBuf, uint32(len(ciphertext)))
	if _, err := c.w.Write(lenBuf); err != nil {
		return err
	}
	if _, err := c.w.Write(ciphertext); err != nil {
		return err
	}
	c.pos = 0
	return nil
}

func (c *chunkedGCMWriter) Close() error {
	if err := c.flushChunk(); err != nil {
		return err
	}
	lenBuf := make([]byte, 4)
	binary.BigEndian.PutUint32(lenBuf, 0)
	_, err := c.w.Write(lenBuf)
	return err
}

type chunkedGCMReader struct {
	r     io.Reader
	aead  cipher.AEAD
	nonce []byte
	buf   []byte
	cnt   uint32
	eof   bool
}

func newChunkedGCMReader(r io.Reader, key []byte) (*chunkedGCMReader, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := io.ReadFull(r, nonce); err != nil {
		return nil, err
	}
	return &chunkedGCMReader{
		r:     r,
		aead:  aead,
		nonce: nonce,
	}, nil
}

func (c *chunkedGCMReader) Read(p []byte) (n int, err error) {
	if len(c.buf) > 0 {
		n = copy(p, c.buf)
		if n == len(c.buf) {
			c.buf = nil
		} else {
			c.buf = c.buf[n:]
		}
		return n, nil
	}
	if c.eof {
		return 0, io.EOF
	}

	lenBuf := make([]byte, 4)
	if _, err := io.ReadFull(c.r, lenBuf); err != nil {
		return 0, err
	}
	chunkLen := binary.BigEndian.Uint32(lenBuf)
	if chunkLen == 0 {
		c.eof = true
		return 0, io.EOF
	}

	cipherBuf := make([]byte, chunkLen)
	if _, err := io.ReadFull(c.r, cipherBuf); err != nil {
		return 0, err
	}

	chunkNonce := make([]byte, len(c.nonce))
	copy(chunkNonce, c.nonce)
	binary.BigEndian.PutUint32(chunkNonce[len(chunkNonce)-4:], c.cnt)
	c.cnt++

	plainBuf, err := c.aead.Open(nil, chunkNonce, cipherBuf, nil)
	if err != nil {
		return 0, err
	}

	n = copy(p, plainBuf)
	if n == len(plainBuf) {
		c.buf = nil
	} else {
		c.buf = make([]byte, len(plainBuf)-n)
		copy(c.buf, plainBuf[n:])
	}
	return n, nil
}

func getAES128Key() []byte {
	token := os.Getenv("INTERNAL_TOKEN")
	if token == "" {
		panic("INTERNAL_TOKEN environment variable must be set")
	}
	h := sha256.Sum256([]byte(token))
	return h[:16]
}

func decryptEnvFile(path string) ([]byte, error) {
	machineIDBytes, err := os.ReadFile("/etc/machine-id")
	if err != nil {
		return nil, fmt.Errorf("read machine-id: %w", err)
	}
	machineID := strings.TrimSpace(string(machineIDBytes))
	h := sha256.Sum256([]byte(machineID))
	key := h[:16]

	// #nosec G304 -- path is validated/derived configuration path
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(data) < 12 {
		return nil, fmt.Errorf("encrypted env file too short")
	}

	nonce := data[:12]
	ciphertext := data[12:]

	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	plaintext, err := aead.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, fmt.Errorf("decrypt env: %w", err)
	}
	return plaintext, nil
}
