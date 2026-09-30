"""Example protected API. Runs as its own Lambda function, fronted by API Gateway HTTP API.

The `@GUARD.protect` decorator is the entire integration: it reads request metadata before your
code runs (and may reject with 429 without calling `api`), and records metadata after your code
returns (and may trigger a block for the NEXT request). It never touches `event["body"]`.
"""
import json
import os

from immune.guard import build_from_env

MODEL_PATH = os.environ.get("IMMUNE_MODEL_PATH", "model.json")
GUARD = build_from_env(MODEL_PATH)          # loaded once per container, reused across warm invocations

ROUTES = {
    "GET /items": lambda event: {"items": ["widget", "gadget", "gizmo"]},
    "GET /items/{id}": lambda event: {"item": (event.get("pathParameters") or {}).get("id")},
    "GET /search": lambda event: {"results": []},
    "GET /me": lambda event: {"user": "demo"},
    "POST /orders": lambda event: {"order_id": "demo-order"},
    "POST /events": lambda event: {"accepted": True},
    "GET /sync": lambda event: {"changes": []},
    "POST /auth/login": lambda event: {"token": "demo-token"},
    "GET /reports": lambda event: {"report": "demo"},
}


def api(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    key = f"{http.get('method', 'GET').upper()} {http.get('path', '/')}"
    route = event.get("routeKey", key).split(" ", 1)
    fn = ROUTES.get(" ".join(route)) or ROUTES.get(key)
    body = fn(event) if fn else {"message": "not found"}
    status = 200 if fn else 404
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


handler = GUARD.protect(api)
