package main

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync/atomic"
	"syscall"
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

	// Buckets are dense around the 300 ms SLO. histogram_quantile() interpolates linearly
	// inside a bucket, so with the default buckets (0.25 then 0.5) a p95 near the SLO would
	// be a guess across 250 ms. With 0.3 as an edge, "is p95 over the SLO?" is exact.
	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latency in seconds.",
		Buckets: []float64{0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1, 2},
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
	startupDelay := time.Duration(envInt("STARTUP_DELAY_SECONDS", 30)) * time.Second
	drainDelay := time.Duration(envInt("SHUTDOWN_DRAIN_SECONDS", 5)) * time.Second

	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", workHandler(iterations))
	mux.HandleFunc("GET /work", workHandler(iterations))

	mux.HandleFunc("GET /healthz", healthHandler)
	mux.HandleFunc("GET /ready", readyHandler)

	mux.Handle("GET /metrics", promhttp.Handler())

	startupTimer := time.AfterFunc(startupDelay, func() {
		ready.Store(true)
		log.Printf("startup delay of %v done, ready", startupDelay)
	})

	srv := &http.Server{
		Addr:              fmt.Sprintf(":%d", port),
		Handler:           logRequests(mux),
		ReadHeaderTimeout: 5 * time.Second,
	}

	// Cancelled on SIGTERM (what Kubernetes sends) or Ctrl-C locally.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()

	go func() {
		log.Printf("Listening on http://localhost:%d (iterations=%d, startup delay=%v)", port, iterations, startupDelay)
		// ErrServerClosed is the normal result of Shutdown, not a failure.
		if err := srv.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server: %v", err)
		}
	}()

	<-ctx.Done()
	stop() // restore default signal handling: a second Ctrl-C kills the process immediately

	// Kubernetes removes the pod from the Service's endpoints at the same time as it sends
	// SIGTERM, not before, so requests keep arriving for a few seconds. Keep serving
	// (and report not-ready) until that has propagated, then stop accepting connections.
	// Stopping the timer keeps a pod terminated mid-startup from flipping back to ready.
	startupTimer.Stop()
	ready.Store(false)
	log.Printf("SIGTERM received, draining for %v", drainDelay)
	time.Sleep(drainDelay)

	// Shutdown waits for in-flight requests. The timeout plus drainDelay must stay below
	// the pod's terminationGracePeriodSeconds (30 s), or the kubelet SIGKILLs us first.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	log.Print("shutdown complete")
}
