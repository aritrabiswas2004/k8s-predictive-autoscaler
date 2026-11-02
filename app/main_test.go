package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
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
