package main

import (
	"crypto/sha256"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const port = 8080

var ready atomic.Bool

var (
	requestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total HTTP requests by method, route and status code.",
	}, []string{"method", "route", "code"})

	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latency in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "route"})

	// Counted when a /work request arrives, before any CPU is spent. http_requests_total
	// only counts finished requests, so under overload (client timeouts) it under-reports
	// load exactly when load is highest. Scalers and the forecaster read this one instead.
	requestsReceived = promauto.NewCounter(prometheus.CounterOpts{
		Name: "http_requests_received_total",
		Help: "Work requests received, counted on arrival.",
	})
)

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (rec *statusRecorder) WriteHeader(code int) {
	rec.status = code
	rec.ResponseWriter.WriteHeader(code)
}

func (rec *statusRecorder) Unwrap() http.ResponseWriter {
	return rec.ResponseWriter
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		elapsed := time.Since(start)

		route := r.Pattern // e.g. "GET /work"; empty when nothing matched
		if route == "" {
			route = "unmatched"
		}
		requestsTotal.WithLabelValues(r.Method, route, strconv.Itoa(rec.status)).Inc()
		requestDuration.WithLabelValues(r.Method, route).Observe(elapsed.Seconds())

		log.Printf("%s %s %s %d %v", r.RemoteAddr, r.Method, r.URL.Path, rec.status, elapsed)
	})
}

func envInt(name string, fallback int) int {
	v, err := strconv.Atoi(os.Getenv(name))
	if err != nil {
		return fallback
	}
	return v
}

func burnCPU(iterations int) [32]byte {
	sum := sha256.Sum256([]byte("seed"))

	for range iterations {
		sum = sha256.Sum256(sum[:])
	}

	return sum
}

func workHandler(iterations int) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		requestsReceived.Inc()
		if _, err := fmt.Fprintf(w, "Result: %x\n", burnCPU(iterations)); err != nil {
			log.Printf("write response for %s: %v", r.URL.Path, err)
		}
	}
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

func readyHandler(w http.ResponseWriter, r *http.Request) {
	if !ready.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func main() {
	iterations := envInt("WORK_ITERATIONS", 300000)
	warmup := time.Duration(envInt("WARMUP_SECONDS", 25)) * time.Second

	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", workHandler(iterations))
	mux.HandleFunc("GET /work", workHandler(iterations))

	mux.HandleFunc("GET /healthz", healthHandler)
	mux.HandleFunc("GET /readyz", readyHandler)

	mux.Handle("GET /metrics", promhttp.Handler())

	time.AfterFunc(warmup, func() {
		ready.Store(true)
		log.Printf("warm-up done after %v, ready", warmup)
	})

	log.Printf("Listening on http://localhost:%d (iterations=%d, warmup=%v)", port, iterations, warmup)
	log.Fatal(http.ListenAndServe(fmt.Sprintf(":%d", port), logRequests(mux)))
}
