"""Prometheus metrics. Scrape /metrics with Amazon Managed Service for Prometheus,
Azure Monitor managed Prometheus, or any Prometheus server."""

from prometheus_client import Counter, Gauge, Histogram

CLOUD_CALLS = Counter(
    "kapconnect_cloud_calls_total",
    "Cloud calls by provider, operation and outcome",
    ["provider", "operation", "outcome"],
)
CLOUD_LATENCY = Histogram(
    "kapconnect_cloud_call_seconds",
    "Cloud call latency",
    ["provider", "operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
FAILOVERS = Counter("kapconnect_failovers_total", "Requests served by a non primary provider", ["operation"])
BREAKER_STATE = Gauge("kapconnect_circuit_open", "1 when the provider circuit breaker is open", ["provider"])
OUTBOX_DEPTH = Gauge("kapconnect_edge_buffer_depth", "Operations waiting in the on premises edge buffer")
RESULTS = Counter("kapconnect_results_total", "Diagnostic results processed", ["status"])
SCREEN_FLAGS = Counter("kapconnect_preanalytical_flags_total", "Samples flagged by AI screening", ["label"])
CRITICAL_ALERTS = Counter("kapconnect_critical_alerts_total", "Critical value alerts raised", ["analyte"])
TURNAROUND = Histogram(
    "kapconnect_turnaround_seconds",
    "Collection to delivery turnaround",
    buckets=(30, 60, 120, 300, 600, 1800, 3600, 7200),
)
