package main

import (
	"net/http"
	"strconv"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Metrics bundles all Prometheus collectors exported by the proxy.
// A nil *Metrics means "metrics disabled"; all record methods are
// no-ops in that case so hot paths don't branch on the flag.
type Metrics struct {
	registry *prometheus.Registry

	requests     *prometheus.CounterVec
	duration     *prometheus.HistogramVec
	retries      *prometheus.CounterVec
	inflight     *prometheus.GaugeVec
	backendUp    *prometheus.GaugeVec
	routeErrors  *prometheus.CounterVec
}

// NewMetrics builds a registry with the proxy's collectors plus the
// standard Go/process collectors. Returning *Metrics (not a handler)
// lets callers register gauges whose current value is computed at
// scrape time (e.g. in-flight counts).
func NewMetrics() *Metrics {
	reg := prometheus.NewRegistry()
	reg.MustRegister(
		prometheus.NewGoCollector(),
		prometheus.NewProcessCollector(prometheus.ProcessCollectorOpts{}),
	)

	m := &Metrics{
		registry: reg,
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "local_ai_proxy_requests_total",
			Help: "Total upstream attempts by model, backend, and HTTP status (or 'transport_error').",
		}, []string{"model", "backend", "status"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "local_ai_proxy_request_duration_seconds",
			Help:    "Duration of successfully-committed requests (from handler entry to response write completion).",
			Buckets: []float64{.05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 120, 300},
		}, []string{"model", "backend"}),
		retries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "local_ai_proxy_retries_total",
			Help: "Failed attempts that triggered a fail-over to another backend.",
		}, []string{"backend", "reason"}),
		inflight: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "local_ai_proxy_inflight",
			Help: "Requests currently in flight to each backend.",
		}, []string{"backend"}),
		backendUp: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "local_ai_proxy_backend_healthy",
			Help: "1 if the backend's last health check succeeded, 0 otherwise. Disabled backends report 0.",
		}, []string{"backend"}),
		routeErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "local_ai_proxy_route_errors_total",
			Help: "Requests rejected by the router before any upstream dispatch.",
		}, []string{"reason"}),
	}
	reg.MustRegister(m.requests, m.duration, m.retries, m.inflight, m.backendUp, m.routeErrors)
	return m
}

// Handler returns the /metrics HTTP handler.
func (m *Metrics) Handler() http.Handler {
	if m == nil {
		return http.NotFoundHandler()
	}
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{Registry: m.registry})
}

// ObserveAttempt records one upstream attempt: the HTTP status returned
// (or "transport_error"), plus the duration (used only when the
// response was successfully committed to the client).
func (m *Metrics) ObserveAttempt(model, backend string, status int, committed bool, seconds float64) {
	if m == nil {
		return
	}
	statusLabel := "transport_error"
	if status > 0 {
		statusLabel = strconv.Itoa(status)
	}
	m.requests.WithLabelValues(model, backend, statusLabel).Inc()
	if committed {
		m.duration.WithLabelValues(model, backend).Observe(seconds)
	}
}

// ObserveRetry increments the retry counter when a request fails over
// from backend → another backend. reason is one of:
// "transport_error", "http_4xx", "http_5xx".
func (m *Metrics) ObserveRetry(backend, reason string) {
	if m == nil {
		return
	}
	m.retries.WithLabelValues(backend, reason).Inc()
}

// ObserveRouteError increments the router-reject counter. reason is
// one of the stable strings in routeErrorReason* below.
func (m *Metrics) ObserveRouteError(reason string) {
	if m == nil {
		return
	}
	m.routeErrors.WithLabelValues(reason).Inc()
}

// SetBackendHealthy updates the 0/1 health gauge for a backend.
func (m *Metrics) SetBackendHealthy(backend string, up bool) {
	if m == nil {
		return
	}
	v := 0.0
	if up {
		v = 1
	}
	m.backendUp.WithLabelValues(backend).Set(v)
}

// SetInflight updates the in-flight gauge for a backend. Called by
// the handler each time the LoadCounter changes.
func (m *Metrics) SetInflight(backend string, count int64) {
	if m == nil {
		return
	}
	m.inflight.WithLabelValues(backend).Set(float64(count))
}

// Stable reason strings for ObserveRouteError — avoids typos drifting
// across callsites and keeps label cardinality bounded.
const (
	RouteErrUnknownModel    = "unknown_model"
	RouteErrNoHealthy       = "no_healthy_backend"
	RouteErrInvalidRequest  = "invalid_request"
	RouteErrBodyTooLarge    = "body_too_large"
)
