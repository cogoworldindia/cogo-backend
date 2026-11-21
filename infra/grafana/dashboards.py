"""Generates the Grafana dashboards in infra/grafana/dashboards/ (spec §11: per-service RED
metrics; queue depth and consumer lag; outbox backlog; projection lag; sockets; FCM; Places
spend). Run: python3 infra/grafana/dashboards.py"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"


def panel(title: str, exprs: list[tuple[str, str]], unit: str = "short", *, x: int, y: int, w: int = 12, h: int = 8) -> dict:
    return {
        "type": "timeseries",
        "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom"}},
        "targets": [{"expr": e, "legendFormat": legend, "refId": chr(65 + i)} for i, (e, legend) in enumerate(exprs)],
    }


def stat(title: str, expr: str, *, x: int, y: int, w: int = 6, thresholds: tuple[float, float] = (1, 1)) -> dict:
    return {
        "type": "stat",
        "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
            {"color": "green", "value": None}, {"color": "orange", "value": thresholds[0]}, {"color": "red", "value": thresholds[1]}]}}},
        "targets": [{"expr": expr, "refId": "A"}],
    }  # fmt: skip


def dashboard(uid: str, title: str, panels: list[dict]) -> dict:
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {
        "uid": uid,
        "title": title,
        "tags": ["cogo"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-3h", "to": "now"},
        "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus"}]},
        "panels": panels,
    }


SERVICES = dashboard("cogo-services", "CoGo · Services (RED)", [
    panel("Requests/s by service", [('sum by (service) (rate(http_server_request_duration_seconds_count[5m]))', "{{service}}")], "reqps", x=0, y=0),
    panel("5xx/s by service", [('sum by (service) (rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))', "{{service}}")], "reqps", x=12, y=0),
    panel("p95 latency, spec §11 read paths (target 300 ms)", [
        ('histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_seconds_bucket{http_route=~"/v1/feed|/v1/journeys/\\\\{journey_id\\\\}/matches|/v1/conversations|/v1/conversations/\\\\{conversation_id\\\\}/messages"}[5m])))', "{{http_route}}")], "s", x=0, y=8),
    panel("p95 latency by route (top 10)", [('topk(10, histogram_quantile(0.95, sum by (le, service, http_route) (rate(http_server_request_duration_seconds_bucket[5m]))))', "{{service}} {{http_route}}")], "s", x=12, y=8),
    panel("Gateway: requests/s by route", [('sum by (route) (rate(gateway_request_duration_seconds_count[5m]))', "{{route}}")], "reqps", x=0, y=16),
    panel("Gateway: status classes/s", [('sum by (status) (rate(gateway_request_duration_seconds_count[5m]))', "{{status}}")], "reqps", x=12, y=16),
    panel("Gateway: p95 by upstream", [('histogram_quantile(0.95, sum by (le, upstream) (rate(gateway_request_duration_seconds_bucket[5m])))', "{{upstream}}")], "s", x=0, y=24),
    panel("Process memory (RSS)", [("process_resident_memory_bytes", "{{service}}")], "bytes", x=12, y=24),
])  # fmt: skip

EVENTS = dashboard("cogo-events", "CoGo · Events and queues", [
    stat("Dead-lettered messages (alert > 0)", 'sum(rabbitmq_detailed_queue_messages{queue=~".*\\\\.dlq"})', x=0, y=0),
    stat("Queues without consumers", 'count(rabbitmq_detailed_queue_consumers{queue!~".*\\\\.dlq"} == 0) or vector(0)', x=6, y=0),
    stat("Oldest unpublished outbox row (s)", "max(outbox_oldest_pending)", x=12, y=0, thresholds=(10, 60)),
    stat("Deletions overdue (> 24 h)", "max(account_deletions_overdue) or vector(0)", x=18, y=0),
    panel("Queue depth", [('rabbitmq_detailed_queue_messages{queue!~".*\\\\.dlq"}', "{{queue}}")], x=0, y=4),
    panel("Consumers per queue", [('rabbitmq_detailed_queue_consumers{queue!~".*\\\\.dlq"}', "{{queue}}")], x=12, y=4),
    panel("Projection lag p95 (target < 2 s)", [("histogram_quantile(0.95, sum by (le, service) (rate(event_consume_lag_seconds_bucket[5m])))", "{{service}}")], "s", x=0, y=12),
    panel("Events handled/s by outcome", [("sum by (service, outcome) (rate(events_handled_total[5m]))", "{{service}} {{outcome}}")], "ops", x=12, y=12),
    panel("Outbox backlog", [("outbox_pending", "{{service}}")], x=0, y=20),
    panel("Maintenance: rows purged / counters fixed", [("sum by (service, task) (increase(maintenance_rows_total[1h]))", "{{service}} {{task}}")], x=12, y=20),
])  # fmt: skip

PRODUCT = dashboard("cogo-product", "CoGo · Realtime, push, providers", [
    panel("Open WebSockets", [("sum(ws_connections)", "total"), ("ws_connections", "{{instance}}")], x=0, y=0),
    panel("Pushes/min by outcome", [("sum by (status) (rate(push_deliveries_total[5m])) * 60", "{{status}}")], x=12, y=0),
    panel("Emails/min by template", [("sum by (template) (rate(emails_total[5m])) * 60", "{{template}}")], x=0, y=8),
    panel("Google Places calls/hour (spend)", [("sum by (kind) (increase(places_api_requests_total[1h]))", "{{kind}}")], x=12, y=8),
    panel("App Check results/min (spec §9 rollout)", [("sum by (result) (rate(gateway_app_check_total[5m])) * 60", "{{result}}")], x=0, y=16),
    panel("Rate-limited responses/min", [('sum by (route) (rate(gateway_request_duration_seconds_count{status="429"}[5m])) * 60', "{{route}}")], x=12, y=16),
])  # fmt: skip

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for board in (SERVICES, EVENTS, PRODUCT):
        (OUT / f"{board['uid']}.json").write_text(json.dumps(board, indent=2) + "\n")
        print("wrote", board["uid"])
