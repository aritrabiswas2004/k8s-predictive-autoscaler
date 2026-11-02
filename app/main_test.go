package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestReadyHandler(t *testing.T) {
	tests := []struct {
		name  string
		ready bool
		want  int
	}{
		{"during startup delay", false, http.StatusServiceUnavailable},
		{"after startup delay", true, http.StatusOK},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ready.Store(tt.ready)
			t.Cleanup(func() { ready.Store(false) })

			rec := httptest.NewRecorder()
			readyHandler(rec, httptest.NewRequest(http.MethodGet, "/ready", nil))

			if rec.Code != tt.want {
				t.Errorf("status = %d, want %d", rec.Code, tt.want)
			}
		})
	}
}

// The middleware must see the status the mux writes for unknown routes and wrong methods,
// otherwise http_requests_total would label 404s and 405s as 200.
func TestLogRequestsRecordsStatus(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthHandler)
	handler := logRequests(mux)

	tests := []struct {
		method, path string
		want         int
	}{
		{http.MethodGet, "/healthz", http.StatusOK},
		{http.MethodGet, "/nope", http.StatusNotFound},
		{http.MethodPost, "/healthz", http.StatusMethodNotAllowed},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, httptest.NewRequest(tt.method, tt.path, nil))

			if rec.Code != tt.want {
				t.Errorf("status = %d, want %d", rec.Code, tt.want)
			}
		})
	}
}

// A request queued behind a busy slot must be counted on arrival, and must give up without
// burning CPU when its client goes away.
func TestWorkHandlerQueue(t *testing.T) {
	slots := make(chan struct{}, 1)
	slots <- struct{}{} // the only slot is busy
	handler := workHandler(1, slots)

	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest(http.MethodGet, "/work", nil).WithContext(ctx)
	rec := httptest.NewRecorder()

	before := testutil.ToFloat64(requestsReceived)
	done := make(chan struct{})
	go func() { handler(rec, req); close(done) }()

	// Wait until the request is counted and queued, then hang up.
	deadline := time.Now().Add(time.Second)
	for testutil.ToFloat64(requestsQueued) != 1 {
		if time.Now().After(deadline) {
			t.Fatal("request never queued")
		}
		time.Sleep(time.Millisecond)
	}
	if got := testutil.ToFloat64(requestsReceived) - before; got != 1 {
		t.Errorf("received = %v while queued, want 1", got)
	}
	cancel()
	<-done

	if rec.Code != statusClientClosed {
		t.Errorf("status = %d, want %d", rec.Code, statusClientClosed)
	}
	if q := testutil.ToFloat64(requestsQueued); q != 0 {
		t.Errorf("queued = %v after hang-up, want 0", q)
	}
	if rec.Body.Len() != 0 {
		t.Errorf("abandoned request did work: body %q", rec.Body.String())
	}
}
