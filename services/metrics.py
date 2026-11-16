"""Prometheus metrics shared by the bot, API and scheduler."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

PAYMENTS_CREATED = Counter(
    "payments_created_total",
    "Payments created",
    ["method"],
    registry=REGISTRY,
)
PAYMENTS_PAID = Counter(
    "payments_paid_total",
    "Payments confirmed as paid",
    ["method"],
    registry=REGISTRY,
)
WEBHOOK_REJECTED = Counter(
    "webhook_rejected_total",
    "Webhooks rejected (bad signature or payload)",
    ["source"],
    registry=REGISTRY,
)
TRIALS_PROVISIONED = Counter(
    "trials_provisioned_total",
    "Trials provisioned",
    registry=REGISTRY,
)
AUTOBANS = Counter("autobans_total", "Automatic bans", ["reason"], registry=REGISTRY)
SOFT_SUSPENSIONS = Counter(
    "soft_suspensions_total",
    "Soft suspensions on traffic anomaly",
    registry=REGISTRY,
)
HIDDIFY_ERRORS = Counter(
    "hiddify_errors_total",
    "Hiddify Manager API failures after retries",
    ["operation"],
    registry=REGISTRY,
)
API_REQUESTS = Counter(
    "api_requests_total",
    "REST API requests",
    ["route", "status"],
    registry=REGISTRY,
)
API_LATENCY = Histogram(
    "api_request_seconds",
    "REST API latency",
    ["route"],
    registry=REGISTRY,
)
ACTIVE_USERS = Gauge("active_users", "Users with an active node", registry=REGISTRY)
JOB_FAILURES = Counter(
    "scheduler_job_failures_total",
    "Scheduler job failures",
    ["job"],
    registry=REGISTRY,
)
LOCATION_UP = Gauge(
    "location_up",
    "1 if the location (Hiddify Manager core or another server) is serving, else 0",
    ["location"],
    registry=REGISTRY,
)
