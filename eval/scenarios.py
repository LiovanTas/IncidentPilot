"""The replay corpus: a synthetic monorepo plus 20 incidents with known ground truth.

Each incident declares one *cause* commit and a set of *decoy* commits. Decoys are not
filler -- most of them are designed to beat the correlation heuristic: they land closer
to the alert onset, touch only the alerting service's own files, and carry risk-flavoured
commit messages. The true cause is often a shared library or an upstream service, which
the heuristic ranks below a local decoy. That gap is the point: it is what the agent has
to close by actually reading diffs.

Commits from neighbouring incidents also fall inside each other's 72h correlation
windows, so no window is artificially clean.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

SERVICES = [
    "checkout-api", "cart-service", "payments-worker", "ledger", "auth-gateway",
    "search-api", "inventory-api", "notifications", "web-frontend",
]

# --------------------------------------------------------------------- baseline repo

_CONFIG_DEFAULTS = [
    ("DB_POOL_SIZE", "50"),
    ("DB_MAX_OVERFLOW", "20"),
    ("DB_POOL_TIMEOUT_S", "5"),
    ("HTTP_TIMEOUT_MS", "2500"),
    ("HTTP_MAX_RETRIES", "2"),
    ("HTTP_BACKOFF_MS", "200"),
    ("CACHE_TTL_S", "300"),
    ("CACHE_KEY_VERSION", '"v3"'),
    ("RATE_LIMIT_PER_MIN", "6000"),
    ("RATE_LIMIT_BURST", "600"),
    ("WORKER_CONCURRENCY", "16"),
    ("WORKER_PREFETCH", "64"),
    ("BATCH_SIZE", "500"),
    ("PAGE_SIZE_DEFAULT", "25"),
    ("PAGE_SIZE_MAX", "100"),
    ("CIRCUIT_FAILURE_THRESHOLD", "20"),
    ("CIRCUIT_RESET_S", "30"),
    ("SESSION_TTL_S", "86400"),
    ("TOKEN_LEEWAY_S", "60"),
    ("MAX_PAYLOAD_BYTES", "2097152"),
]

_CONFIG_OVERRIDES: dict[str, dict[str, str]] = {
    "auth-gateway": {"DB_POOL_SIZE": "80", "RATE_LIMIT_PER_MIN": "24000"},
    "search-api": {"DB_POOL_SIZE": "30", "CACHE_TTL_S": "600", "RATE_LIMIT_PER_MIN": "30000"},
    "payments-worker": {"WORKER_CONCURRENCY": "24", "BATCH_SIZE": "200"},
    "ledger": {"DB_POOL_SIZE": "40", "WORKER_CONCURRENCY": "8"},
    "web-frontend": {"DB_POOL_SIZE": "10", "CACHE_TTL_S": "120"},
    "notifications": {"WORKER_CONCURRENCY": "12", "RATE_LIMIT_PER_MIN": "6000"},
}


def config_py(service: str) -> str:
    overrides = _CONFIG_OVERRIDES.get(service, {})
    lines = [
        f'"""Runtime configuration for {service}.',
        "",
        "Every value here is a production tunable. Changes to this file ship on the next",
        "deploy and take effect without a restart of the dependency graph.",
        '"""',
        "",
        "import os",
        "",
        "",
        "def _env(name, default):",
        "    return type(default)(os.environ.get(name, default))",
        "",
        "",
    ]
    for name, default in _CONFIG_DEFAULTS:
        lines.append(f"{name} = {overrides.get(name, default)}")
    lines.append("")
    return "\n".join(lines)


def handlers_py(service: str) -> str:
    return f'''"""Request handlers for {service}."""

from . import config, store
from libs import cache, http_client


def health():
    return {{"status": "ok", "service": "{service}"}}


def handle_read(request):
    key = cache.key("{service}", request.get("id", ""))
    hit = cache.get(key)
    if hit is not None:
        return hit
    payload = store.fetch_one(request.get("id", ""))
    cache.put(key, payload, ttl=config.CACHE_TTL_S)
    return payload


def handle_list(request):
    limit = min(int(request.get("limit", config.PAGE_SIZE_DEFAULT)), config.PAGE_SIZE_MAX)
    return store.fetch_page(limit=limit, cursor=request.get("cursor"))


def handle_write(request):
    body = request.get("body") or b""
    if len(body) > config.MAX_PAYLOAD_BYTES:
        return {{"error": "payload_too_large", "status": 413}}
    return store.write(request)


def call_upstream(path, payload):
    return http_client.post(
        path,
        payload,
        timeout_ms=config.HTTP_TIMEOUT_MS,
        retries=config.HTTP_MAX_RETRIES,
    )
'''


def store_py(service: str) -> str:
    return _STORE_TEMPLATE.replace("{service_table}", service.replace("-", "_")).replace(
        "{service}", service
    )


_STORE_TEMPLATE = '''"""Persistence layer for {service}."""

from . import config

_POOL = None


def pool():
    global _POOL
    if _POOL is None:
        _POOL = connect(
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            pool_timeout=config.DB_POOL_TIMEOUT_S,
        )
    return _POOL


def connect(**kwargs):
    return {"driver": "postgres", **kwargs}


def fetch_one(row_id):
    with pool() as conn:
        return conn.execute(
            "SELECT * FROM {service_table} WHERE id = %s", (row_id,)
        ).fetchone()


def fetch_page(limit, cursor=None):
    with pool() as conn:
        return conn.execute(
            "SELECT * FROM {service_table} WHERE id > %s ORDER BY id LIMIT %s",
            (cursor or 0, limit),
        ).fetchall()


def write(request):
    with pool() as conn:
        return conn.execute(
            "INSERT INTO {service_table} (payload) VALUES (%s) RETURNING id",
            (request.get("body"),),
        ).fetchone()
'''


def values_yaml(service: str, replicas: int = 6, memory: str = "1Gi") -> str:
    return f"""# Deploy manifest for {service}
replicas: {replicas}

image:
  repository: registry.internal/checkout-platform/{service}
  tag: main

resources:
  limits:
    memory: {memory}
    cpu: "2"
  requests:
    memory: 512Mi
    cpu: "1"

env:
  LOG_LEVEL: info
  ASSET_PREFIX: "https://cdn.internal/static/v4"
  FEATURE_NEW_PRICING: "false"
  FEATURE_ASYNC_RECEIPTS: "false"
  FEATURE_STRICT_VALIDATION: "false"

readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 10
"""


HTTP_CLIENT = '''"""Shared outbound HTTP client.

Used by every service in the platform. Defaults here apply to any caller that does not
pass an explicit override, which in practice is most of them.
"""

DEFAULT_TIMEOUT_MS = 3000
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_MS = 250
DEFAULT_POOL_CONNECTIONS = 100
VERIFY_TLS = True


def post(path, payload, timeout_ms=None, retries=None):
    return _request("POST", path, payload,
                    timeout_ms=timeout_ms or DEFAULT_TIMEOUT_MS,
                    retries=retries if retries is not None else DEFAULT_RETRIES)


def get(path, timeout_ms=None, retries=None):
    return _request("GET", path, None,
                    timeout_ms=timeout_ms or DEFAULT_TIMEOUT_MS,
                    retries=retries if retries is not None else DEFAULT_RETRIES)


def _request(method, path, payload, timeout_ms, retries):
    attempt = 0
    while True:
        try:
            return _send(method, path, payload, timeout_ms)
        except TimeoutError:
            attempt += 1
            if attempt > retries:
                raise
            _sleep_ms(DEFAULT_BACKOFF_MS * (2 ** attempt))


def _send(method, path, payload, timeout_ms):
    raise NotImplementedError("bound at runtime by the transport layer")


def _sleep_ms(ms):
    pass
'''

CACHE_LIB = '''"""Shared cache client.

KEY_NAMESPACE is part of every key. Changing it invalidates the entire warm set across
every service at once.
"""

KEY_NAMESPACE = "v3"
DEFAULT_TTL_S = 300
FAIL_OPEN = True

_STORE = {}


def key(service, ident):
    return f"{KEY_NAMESPACE}:{service}:{ident}"


def get(k):
    return _STORE.get(k)


def put(k, value, ttl=DEFAULT_TTL_S):
    if ttl <= 0:
        return value
    _STORE[k] = value
    return value


def invalidate(prefix):
    for k in list(_STORE):
        if k.startswith(prefix):
            del _STORE[k]
'''

SERIALIZATION = '''"""Shared serialization helpers.

Money is represented as Decimal end to end. Any change to how Decimal is encoded is a
breaking change for the ledger and for every downstream consumer of these payloads.
"""

from decimal import Decimal

DECIMAL_AS_STRING = True
DATETIME_FORMAT = "iso8601"


def encode(value):
    if isinstance(value, Decimal):
        return str(value) if DECIMAL_AS_STRING else float(value)
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode_money(raw):
    return Decimal(str(raw))
'''

IDEMPOTENCY = '''"""Idempotency key derivation for the payment path.

The key must be stable across retries of the same logical payment. Deriving it from
anything that changes between attempts will cause duplicate charges.
"""

import hashlib


def charge_key(order_id, amount_cents, attempt_id=None):
    material = f"{order_id}:{amount_cents}"
    return hashlib.sha256(material.encode()).hexdigest()


def refund_key(order_id, refund_id):
    return hashlib.sha256(f"refund:{order_id}:{refund_id}".encode()).hexdigest()
'''

MIGRATION_BASE = """-- 0001_initial.sql
CREATE TABLE IF NOT EXISTS {table} (
    id BIGSERIAL PRIMARY KEY,
    payload JSONB NOT NULL,
    legacy_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_{table}_created_at ON {table} (created_at);
CREATE INDEX idx_{table}_legacy_ref ON {table} (legacy_ref);
"""


def baseline_files() -> dict[str, str]:
    files: dict[str, str] = {
        "README.md": (
            "# checkout-platform\n\n"
            "Monorepo for the checkout platform. Services live under `services/`, shared\n"
            "libraries under `libs/` and `packages/common/`, deploy manifests under\n"
            "`deploy/`, and database migrations under `db/migrations/`.\n\n"
            "A change under `libs/` or `packages/common/` ships to every service.\n"
        ),
        "libs/http_client.py": HTTP_CLIENT,
        "libs/cache.py": CACHE_LIB,
        "packages/common/serialization.py": SERIALIZATION,
        "packages/common/idempotency.py": IDEMPOTENCY,
    }
    for service in SERVICES:
        files[f"services/{service}/config.py"] = config_py(service)
        files[f"services/{service}/handlers.py"] = handlers_py(service)
        files[f"services/{service}/store.py"] = store_py(service)
        files[f"deploy/{service}/values.yaml"] = values_yaml(service)
    for area, table in (("payments", "payments"), ("ledger", "ledger_entries"),
                        ("inventory", "inventory_items")):
        files[f"db/migrations/{area}/0001_initial.sql"] = MIGRATION_BASE.format(table=table)
    files["db/migrations/inventory/0002_cart_items.sql"] = (
        "-- 0002_cart_items.sql\n"
        "CREATE TABLE IF NOT EXISTS cart_items (\n"
        "    id BIGSERIAL PRIMARY KEY,\n"
        "    cart_id BIGINT NOT NULL,\n"
        "    sku TEXT NOT NULL,\n"
        "    qty INT NOT NULL DEFAULT 1\n"
        ");\n\n"
        "CREATE INDEX idx_cart_items_cart_id ON cart_items (cart_id);\n"
        "CREATE INDEX idx_cart_items_sku ON cart_items (sku);\n"
    )
    return files


# ------------------------------------------------------------------------- incidents
#
# Each incident is a dict:
#   id, service, severity, title, description, metrics, labels
#   cause:  commit spec with the ground-truth root cause
#   decoys: commit specs that land in the same window
#
# Commit spec: at_h (hours before onset), author, message, edits [(path, find, replace)],
# and optionally new_files {path: content}.

AUTHORS = [
    "Priya Raghavan", "Marcus Bell", "Ana Sofia Reyes", "Tom Okafor", "Wen Li",
    "Dmitri Volkov", "Hannah Schmidt", "Kofi Mensah", "Yuki Tanaka", "Elena Duarte",
]


def _onset(offset_hours: float) -> datetime:
    return BASE + timedelta(hours=offset_hours)


INCIDENTS: list[dict] = [
    {
        "id": "INC-01",
        "onset": _onset(0),
        "service": "checkout-api",
        "severity": "sev1",
        "title": "checkout-api 5xx rate above 8%",
        "description": (
            "HTTP 5xx on POST /checkout climbed from 0.2% to 8.4% over four minutes. "
            "Database CPU is flat at 18%. Application logs show repeated "
            "'QueuePool limit of size 5 overflow 20 reached, connection timed out'."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.084, "duration_minutes": 22,
                    "latency_p99_ms": 9400},
        "labels": {"alertname": "CheckoutHighErrorRate", "endpoint": "/checkout", "team": "checkout"},
        "cause": {
            "at_h": 1.5, "author": AUTHORS[0],
            "message": "perf: right-size checkout db pool after connection audit\n\n"
                       "The connection audit showed checkout holding far more idle connections\n"
                       "than it uses at p50. Dropping the pool frees headroom on the primary.",
            "edits": [("services/checkout-api/config.py", "DB_POOL_SIZE = 50", "DB_POOL_SIZE = 5")],
        },
        "decoys": [
            {"at_h": 0.6, "author": AUTHORS[1],
             "message": "fix: retry payment authorization on transient gateway errors\n\n"
                        "Adds a bounded retry around the authorize call. Should reduce the\n"
                        "spurious failures we saw over the weekend.",
             "edits": [("services/checkout-api/handlers.py",
                        "        retries=config.HTTP_MAX_RETRIES,",
                        "        retries=config.HTTP_MAX_RETRIES,\n        idempotent=True,")]},
            {"at_h": 3.2, "author": AUTHORS[2],
             "message": "chore: bump checkout replicas for the sale window",
             "edits": [("deploy/checkout-api/values.yaml", "replicas: 6", "replicas: 9")]},
            {"at_h": 14.0, "author": AUTHORS[3],
             "message": "refactor: extract cart total calculation into a helper",
             "edits": [("services/cart-service/handlers.py",
                        'def health():', 'def totals(items):\n    return sum(i["qty"] for i in items)\n\n\ndef health():')]},
        ],
    },
    {
        "id": "INC-02",
        "onset": _onset(26),
        "service": "cart-service",
        "severity": "sev2",
        "title": "cart-service p99 latency 4.2s (was 180ms)",
        "description": (
            "p99 on GET /cart rose from 180ms to 4.2s while p50 moved only 40ms. "
            "Postgres read IOPS tripled and the slow-query log is dominated by "
            "SELECT * FROM cart_items WHERE cart_id = $1. Request volume is unchanged."
        ),
        "metrics": {"error_rate_before": 0.001, "error_rate_after": 0.021, "duration_minutes": 41,
                    "latency_p99_ms": 4200},
        "labels": {"alertname": "CartLatencyHigh", "endpoint": "/cart", "team": "checkout"},
        "cause": {
            "at_h": 5.0, "author": AUTHORS[4],
            "message": "db: drop unused indexes flagged by the index audit\n\n"
                       "pg_stat_user_indexes reported these as never scanned in the last 7 days.\n"
                       "Reclaiming ~40GB across the inventory cluster.",
            "new_files": {
                "db/migrations/inventory/0007_drop_stale_indexes.sql":
                    "-- 0007_drop_stale_indexes.sql\n"
                    "DROP INDEX IF EXISTS idx_cart_items_cart_id;\n"
                    "DROP INDEX IF EXISTS idx_inventory_items_legacy_ref;\n"
            },
        },
        "decoys": [
            {"at_h": 1.1, "author": AUTHORS[5],
             "message": "perf: raise cart page size to cut round trips\n\n"
                        "The mobile client was paginating in tiny chunks. Larger pages should\n"
                        "reduce total query count per session.",
             "edits": [("services/cart-service/config.py", "PAGE_SIZE_DEFAULT = 25", "PAGE_SIZE_DEFAULT = 60")]},
            {"at_h": 2.4, "author": AUTHORS[6],
             "message": "fix: cart cache key collision between guest and member carts",
             "edits": [("services/cart-service/handlers.py",
                        'key = cache.key("cart-service", request.get("id", ""))',
                        'key = cache.key("cart-service", str(request.get("id", "")) + ":" + str(request.get("scope", "guest")))')]},
        ],
    },
    {
        "id": "INC-03",
        "onset": _onset(51),
        "service": "search-api",
        "severity": "sev2",
        "title": "search-api origin load 11x, cache hit ratio at 2%",
        "description": (
            "Cache hit ratio fell from 94% to 2% at 14:05, exactly at a deploy boundary. "
            "Origin query volume is 11x baseline and inventory-api is now shedding load. "
            "User-facing latency is up but error rate is still low."
        ),
        "metrics": {"error_rate_before": 0.001, "error_rate_after": 0.018, "duration_minutes": 35,
                    "latency_p99_ms": 2600},
        "labels": {"alertname": "SearchCacheHitRatioLow", "team": "discovery"},
        "cause": {
            "at_h": 0.8, "author": AUTHORS[7],
            "message": "fix: disable search result caching while we debug stale facets\n\n"
                       "Temporary. Facet counts were going stale for up to 10 minutes and\n"
                       "support has three open tickets about it.",
            "edits": [("services/search-api/config.py", "CACHE_TTL_S = 600", "CACHE_TTL_S = 0")],
        },
        "decoys": [
            {"at_h": 0.3, "author": AUTHORS[8],
             "message": "perf: widen search connection pool for the new ranker",
             "edits": [("services/search-api/config.py", "DB_POOL_SIZE = 30", "DB_POOL_SIZE = 45")]},
            {"at_h": 6.5, "author": AUTHORS[9],
             "message": "chore: raise inventory batch size for the nightly reindex",
             "edits": [("services/inventory-api/config.py", "BATCH_SIZE = 500", "BATCH_SIZE = 2000")]},
        ],
    },
    {
        "id": "INC-04",
        "onset": _onset(77),
        "service": "payments-worker",
        "severity": "sev1",
        "title": "payments-worker consumer lag 340k and climbing",
        "description": (
            "Consumer lag on payments.settle has grown linearly for 90 minutes and now "
            "exceeds 340k messages. Oldest message age is 51 minutes against a 15-minute SLO. "
            "Error rate is normal; nothing is failing, everything is late."
        ),
        "metrics": {"error_rate_before": 0.001, "error_rate_after": 0.004, "duration_minutes": 92,
                    "queue_depth": 340000},
        "labels": {"alertname": "PaymentsConsumerLag", "topic": "payments.settle", "team": "payments"},
        "cause": {
            "at_h": 2.0, "author": AUTHORS[1],
            "message": "chore: reduce payments worker concurrency to ease ledger contention\n\n"
                       "Ledger was reporting lock waits during the settle window. Backing off\n"
                       "the worker pool should reduce contention on the entries table.",
            "edits": [("services/payments-worker/config.py", "WORKER_CONCURRENCY = 24", "WORKER_CONCURRENCY = 4")],
        },
        "decoys": [
            {"at_h": 0.9, "author": AUTHORS[2],
             "message": "fix: handle partial settlement responses from the gateway\n\n"
                        "Adds a branch for the PARTIAL status the provider started returning.",
             "edits": [("services/payments-worker/handlers.py",
                        "def handle_write(request):",
                        "def handle_partial(request):\n    return {\"status\": \"partial\", \"order\": request.get(\"id\")}\n\n\ndef handle_write(request):")]},
            {"at_h": 4.5, "author": AUTHORS[3],
             "message": "chore: bump payments-worker memory limit",
             "edits": [("deploy/payments-worker/values.yaml", "memory: 1Gi", "memory: 2Gi")]},
        ],
    },
    {
        "id": "INC-05",
        "onset": _onset(99),
        "service": "auth-gateway",
        "severity": "sev1",
        "title": "auth-gateway 401 rate 31%, users logged out mid-session",
        "description": (
            "401 responses rose from 0.4% to 31%. The failures cluster by token age: "
            "everything older than about 15 minutes fails validation. Support is reporting "
            "users being logged out mid-checkout. Token issuance itself is healthy."
        ),
        "metrics": {"error_rate_before": 0.004, "error_rate_after": 0.31, "duration_minutes": 28},
        "labels": {"alertname": "AuthUnauthorizedSpike", "team": "platform"},
        "cause": {
            "at_h": 1.2, "author": AUTHORS[4],
            "message": "security: shorten session lifetime per the Q3 audit finding\n\n"
                       "Audit item SEC-441 asked for shorter sessions on the gateway. This\n"
                       "brings us in line with the policy doc.",
            "edits": [("services/auth-gateway/config.py", "SESSION_TTL_S = 86400", "SESSION_TTL_S = 900")],
        },
        "decoys": [
            {"at_h": 0.4, "author": AUTHORS[5],
             "message": "fix: tighten JWT clock leeway to reject replayed tokens\n\n"
                        "Narrowing the acceptance window closes a small replay gap.",
             "edits": [("services/auth-gateway/config.py", "TOKEN_LEEWAY_S = 60", "TOKEN_LEEWAY_S = 30")]},
            {"at_h": 2.8, "author": AUTHORS[6],
             "message": "chore: raise auth rate limit for the mobile refresh storm",
             "edits": [("services/auth-gateway/config.py", "RATE_LIMIT_PER_MIN = 24000", "RATE_LIMIT_PER_MIN = 36000")]},
        ],
    },
    {
        "id": "INC-06",
        "onset": _onset(124),
        "service": "checkout-api",
        "severity": "sev1",
        "title": "checkout-api 504 gateway timeouts at 12%",
        "description": (
            "12% of POST /checkout requests return 504. Traces show the calls to "
            "payments-worker and inventory-api being cancelled client-side at ~300ms, "
            "well below their normal p99 of 1.8s. Downstream services report no errors "
            "and their own latency is unchanged."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.12, "duration_minutes": 33,
                    "latency_p99_ms": 320},
        "labels": {"alertname": "CheckoutGatewayTimeout", "endpoint": "/checkout", "team": "checkout"},
        "cause": {
            "at_h": 3.5, "author": AUTHORS[7],
            "message": "perf: tighten default HTTP timeout across services\n\n"
                       "3s is far above our p99 for internal calls. Dropping the default keeps\n"
                       "slow callers from holding connections.",
            "edits": [("libs/http_client.py", "DEFAULT_TIMEOUT_MS = 3000", "DEFAULT_TIMEOUT_MS = 300")],
        },
        "decoys": [
            {"at_h": 0.7, "author": AUTHORS[8],
             "message": "fix: add circuit breaker around the inventory reservation call\n\n"
                        "Checkout was hanging when inventory got slow. This sheds load faster.",
             "edits": [("services/checkout-api/config.py",
                        "CIRCUIT_FAILURE_THRESHOLD = 20", "CIRCUIT_FAILURE_THRESHOLD = 12")]},
            {"at_h": 1.9, "author": AUTHORS[9],
             "message": "perf: lower checkout upstream timeout to fail fast\n\n"
                        "Matches what the SRE review recommended for tier-1 paths.",
             "edits": [("services/checkout-api/config.py", "HTTP_TIMEOUT_MS = 2500", "HTTP_TIMEOUT_MS = 2000")]},
        ],
    },
    {
        "id": "INC-07",
        "onset": _onset(150),
        "service": "web-frontend",
        "severity": "sev2",
        "title": "web-frontend 500s on order confirmation render",
        "description": (
            "The order confirmation page throws on ~6% of renders with "
            "'TypeError: cannot read property toFixed of string' in the price formatter. "
            "The deploy that preceded it contained no application code changes."
        ),
        "metrics": {"error_rate_before": 0.003, "error_rate_after": 0.061, "duration_minutes": 47},
        "labels": {"alertname": "FrontendRenderErrors", "team": "web"},
        "cause": {
            "at_h": 2.2, "author": AUTHORS[0],
            "message": "chore(deps): bump @platform/serialization 1.9.4 -> 2.0.1\n\n"
                       "Routine dependency update from the weekly bot batch. CI is green.",
            "edits": [("packages/common/serialization.py",
                       "DECIMAL_AS_STRING = True", "DECIMAL_AS_STRING = False")],
            "new_files": {
                "services/web-frontend/package-lock.json":
                    '{\n  "name": "web-frontend",\n  "lockfileVersion": 3,\n'
                    '  "packages": {\n    "node_modules/@platform/serialization": {\n'
                    '      "version": "2.0.1"\n    }\n  }\n}\n'
            },
        },
        "decoys": [
            {"at_h": 0.5, "author": AUTHORS[1],
             "message": "feat: new price formatting for multi-currency carts\n\n"
                        "Adds currency-aware formatting to the confirmation page.",
             "edits": [("services/web-frontend/handlers.py",
                        "def health():",
                        "def format_price(amount, currency=\"USD\"):\n    return f\"{currency} {amount}\"\n\n\ndef health():")]},
            {"at_h": 5.0, "author": AUTHORS[2],
             "message": "chore: enable strict validation flag on web-frontend",
             "edits": [("deploy/web-frontend/values.yaml",
                        'FEATURE_STRICT_VALIDATION: "false"', 'FEATURE_STRICT_VALIDATION: "true"')]},
        ],
    },
    {
        "id": "INC-08",
        "onset": _onset(173),
        "service": "inventory-api",
        "severity": "sev2",
        "title": "inventory-api pods in OOMKill restart loop",
        "description": (
            "All 6 inventory-api pods are restarting every 90 to 120 seconds with "
            "OOMKilled in lastState. Memory climbs to the limit then drops to zero, a clean "
            "sawtooth that began exactly at the 11:40 deploy. Error rate tracks the restart cycle."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.14, "duration_minutes": 38,
                    "restart_count": 26},
        "labels": {"alertname": "InventoryPodRestartLoop", "team": "supply"},
        "cause": {
            "at_h": 1.0, "author": AUTHORS[3],
            "message": "chore: tighten inventory memory request after profiling\n\n"
                       "Profiling showed steady-state RSS well under the current limit. Freeing\n"
                       "the headroom lets us pack more pods per node.",
            "edits": [("deploy/inventory-api/values.yaml", "memory: 1Gi", "memory: 256Mi")],
        },
        "decoys": [
            {"at_h": 0.4, "author": AUTHORS[4],
             "message": "perf: prefetch full SKU catalogue at startup\n\n"
                        "Avoids a cold-start penalty on the first reservation of each SKU.",
             "edits": [("services/inventory-api/handlers.py",
                        "def health():",
                        "CATALOGUE = []\n\n\ndef warm_catalogue(rows):\n    CATALOGUE.extend(rows)\n\n\ndef health():")]},
            {"at_h": 9.0, "author": AUTHORS[5],
             "message": "chore: bump inventory worker prefetch",
             "edits": [("services/inventory-api/config.py", "WORKER_PREFETCH = 64", "WORKER_PREFETCH = 256")]},
        ],
    },
    {
        "id": "INC-09",
        "onset": _onset(197),
        "service": "notifications",
        "severity": "sev3",
        "title": "notifications 429 rate at 78%, delivery backlog growing",
        "description": (
            "78% of outbound notification sends are rejected with 429 by our own limiter. "
            "Send volume is normal for the hour. The backlog is growing at roughly 4k/min "
            "and order confirmation emails are delayed by 20+ minutes."
        ),
        "metrics": {"error_rate_before": 0.01, "error_rate_after": 0.78, "duration_minutes": 64},
        "labels": {"alertname": "NotificationsRateLimited", "team": "growth"},
        "cause": {
            "at_h": 1.7, "author": AUTHORS[6],
            "message": "fix: correct notification rate limit units\n\n"
                       "The limiter config was documented as per-minute but the provider quota\n"
                       "is stated per-second. Aligning the value with the provider doc.",
            "edits": [("services/notifications/config.py", "RATE_LIMIT_PER_MIN = 6000", "RATE_LIMIT_PER_MIN = 60")],
        },
        "decoys": [
            {"at_h": 0.6, "author": AUTHORS[7],
             "message": "feat: batch digest notifications to cut provider spend",
             "edits": [("services/notifications/config.py", "BATCH_SIZE = 500", "BATCH_SIZE = 50")]},
            {"at_h": 3.0, "author": AUTHORS[8],
             "message": "chore: reduce notification burst allowance",
             "edits": [("services/notifications/config.py", "RATE_LIMIT_BURST = 600", "RATE_LIMIT_BURST = 300")]},
        ],
    },
    {
        "id": "INC-10",
        "onset": _onset(223),
        "service": "ledger",
        "severity": "sev1",
        "title": "ledger reconciliation found 412 double-charged orders",
        "description": (
            "The hourly reconciliation job flagged 412 orders with more than one successful "
            "charge in the last two hours. Affected orders share an order_id but have distinct "
            "payment_ids. The payment provider shows an elevated retry rate starting at 03:10."
        ),
        "metrics": {"error_rate_before": 0.0, "error_rate_after": 0.009, "duration_minutes": 120},
        "labels": {"alertname": "LedgerDuplicateCharges", "team": "payments", "priority": "P1"},
        "cause": {
            "at_h": 2.6, "author": AUTHORS[9],
            "message": "fix: make idempotency keys unique per attempt\n\n"
                       "Retries were colliding with the original request in the provider's\n"
                       "idempotency cache and returning a cached failure. Scoping the key to\n"
                       "the attempt makes each retry distinguishable.",
            "edits": [("packages/common/idempotency.py",
                       '    material = f"{order_id}:{amount_cents}"',
                       '    material = f"{order_id}:{amount_cents}:{attempt_id}"')],
        },
        "decoys": [
            {"at_h": 1.0, "author": AUTHORS[0],
             "message": "fix: retry ledger writes on serialization failures\n\n"
                        "Postgres serialization failures were surfacing as hard errors.",
             "edits": [("services/ledger/config.py", "HTTP_MAX_RETRIES = 2", "HTTP_MAX_RETRIES = 4")]},
            {"at_h": 4.0, "author": AUTHORS[1],
             "message": "db: add partial unique index on ledger entries",
             "new_files": {
                 "db/migrations/ledger/0004_unique_entries.sql":
                     "-- 0004_unique_entries.sql\n"
                     "CREATE UNIQUE INDEX CONCURRENTLY idx_ledger_entries_order_payment\n"
                     "    ON ledger_entries ((payload->>'order_id'), (payload->>'payment_id'));\n"
             }},
        ],
    },
    {
        "id": "INC-11",
        "onset": _onset(246),
        "service": "checkout-api",
        "severity": "sev2",
        "title": "checkout-api price mismatch errors on 22% of carts",
        "description": (
            "22% of checkouts fail validation with 'cart total does not match quoted price'. "
            "The failures are concentrated in carts containing promotional items. No checkout "
            "deploy happened in the window; the last one was 9 hours earlier and contained only tests."
        ),
        "metrics": {"error_rate_before": 0.004, "error_rate_after": 0.22, "duration_minutes": 55},
        "labels": {"alertname": "CheckoutPriceMismatch", "endpoint": "/checkout", "team": "checkout"},
        "cause": {
            "at_h": 1.3, "author": AUTHORS[2],
            "message": "chore: flip new pricing engine default to on\n\n"
                       "The new engine has been at 10% for two weeks with no regressions.\n"
                       "Making it the default ahead of removing the old path.",
            "edits": [("deploy/checkout-api/values.yaml",
                       'FEATURE_NEW_PRICING: "false"', 'FEATURE_NEW_PRICING: "true"')],
        },
        "decoys": [
            {"at_h": 0.8, "author": AUTHORS[3],
             "message": "fix: round promotional discounts half-up to match finance\n\n"
                        "Finance reported a one-cent drift on percentage discounts.",
             "edits": [("services/checkout-api/handlers.py",
                        "def handle_write(request):",
                        "def apply_discount(total, pct):\n    return round(total * (1 - pct / 100), 2)\n\n\ndef handle_write(request):")]},
            {"at_h": 9.5, "author": AUTHORS[4],
             "message": "test: add checkout total regression cases",
             "edits": [("services/checkout-api/store.py",
                        "def write(request):",
                        "def count_rows():\n    with pool() as conn:\n        return conn.execute(\"SELECT count(*) FROM checkout_api\").fetchone()\n\n\ndef write(request):")]},
        ],
    },
    {
        "id": "INC-12",
        "onset": _onset(271),
        "service": "cart-service",
        "severity": "sev1",
        "title": "cart-service outbound request volume 7x inbound",
        "description": (
            "cart-service is sending 7x more requests to inventory-api than it receives from "
            "clients. inventory-api is saturated and now failing. The amplification started "
            "before inventory showed any degradation, so inventory is the victim not the cause."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.19, "duration_minutes": 44},
        "labels": {"alertname": "CartRetryAmplification", "team": "checkout"},
        "cause": {
            "at_h": 2.1, "author": AUTHORS[5],
            "message": "fix: make cart resilient to inventory blips\n\n"
                       "Inventory has been flaky during its deploys. More retries with a shorter\n"
                       "wait should hide that from users.",
            "edits": [("services/cart-service/config.py", "HTTP_MAX_RETRIES = 2", "HTTP_MAX_RETRIES = 6"),
                      ("services/cart-service/config.py", "HTTP_BACKOFF_MS = 200", "HTTP_BACKOFF_MS = 0")],
        },
        "decoys": [
            {"at_h": 0.9, "author": AUTHORS[6],
             "message": "perf: raise inventory pool to absorb cart traffic",
             "edits": [("services/inventory-api/config.py", "DB_POOL_SIZE = 50", "DB_POOL_SIZE = 70")]},
            {"at_h": 3.3, "author": AUTHORS[7],
             "message": "chore: scale inventory-api replicas",
             "edits": [("deploy/inventory-api/values.yaml", "replicas: 6", "replicas: 10")]},
        ],
    },
    {
        "id": "INC-13",
        "onset": _onset(295),
        "service": "search-api",
        "severity": "sev2",
        "title": "search-api p99 6.8s and memory pressure on all pods",
        "description": (
            "Search response sizes grew by roughly 20x. p99 latency is 6.8s, egress bandwidth "
            "is at the NAT gateway limit, and pods are approaching their memory limit without "
            "yet being killed. Query volume is flat."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.033, "duration_minutes": 51,
                    "latency_p99_ms": 6800},
        "labels": {"alertname": "SearchResponseSizeHigh", "team": "discovery"},
        "cause": {
            "at_h": 1.4, "author": AUTHORS[8],
            "message": "feat: return more results per search page\n\n"
                       "Product asked for infinite scroll without the pagination stutter.\n"
                       "Raising the default page size removes most of the round trips.",
            "edits": [("services/search-api/config.py", "PAGE_SIZE_DEFAULT = 25", "PAGE_SIZE_DEFAULT = 500"),
                      ("services/search-api/config.py", "PAGE_SIZE_MAX = 100", "PAGE_SIZE_MAX = 1000")],
        },
        "decoys": [
            {"at_h": 0.5, "author": AUTHORS[9],
             "message": "perf: add facet aggregation to the search response\n\n"
                        "Adds counts per category so the UI can render filters without a\n"
                        "second request.",
             "edits": [("services/search-api/handlers.py",
                        "def handle_list(request):",
                        "def facets(rows):\n    return {\"categories\": len(rows)}\n\n\ndef handle_list(request):")]},
            {"at_h": 7.0, "author": AUTHORS[0],
             "message": "chore: raise search memory limit",
             "edits": [("deploy/search-api/values.yaml", "memory: 1Gi", "memory: 1536Mi")]},
        ],
    },
    {
        "id": "INC-14",
        "onset": _onset(320),
        "service": "payments-worker",
        "severity": "sev1",
        "title": "payments-worker failing 41% of settlements",
        "description": (
            "41% of settlement jobs fail with 'column payments.legacy_ref does not exist'. "
            "The failures began mid-run, not at a deploy: the worker code was unchanged for "
            "two days. Failed jobs are being retried and re-failing."
        ),
        "metrics": {"error_rate_before": 0.003, "error_rate_after": 0.41, "duration_minutes": 36},
        "labels": {"alertname": "PaymentsSettlementFailures", "team": "payments"},
        "cause": {
            "at_h": 4.2, "author": AUTHORS[1],
            "message": "db: drop legacy_ref now that the backfill is complete\n\n"
                       "The migration to payment_reference finished last sprint. Removing the\n"
                       "old column and its index.",
            "new_files": {
                "db/migrations/payments/0012_drop_legacy_columns.sql":
                    "-- 0012_drop_legacy_columns.sql\n"
                    "DROP INDEX IF EXISTS idx_payments_legacy_ref;\n"
                    "ALTER TABLE payments DROP COLUMN legacy_ref;\n"
            },
        },
        "decoys": [
            {"at_h": 1.1, "author": AUTHORS[2],
             "message": "fix: harden settlement error handling\n\n"
                        "Wraps the settle path so a single bad row cannot poison the batch.",
             "edits": [("services/payments-worker/handlers.py",
                        "def handle_read(request):",
                        "def settle_safe(row):\n    try:\n        return settle(row)\n    except Exception:\n        return None\n\n\ndef settle(row):\n    return row\n\n\ndef handle_read(request):")]},
            {"at_h": 2.0, "author": AUTHORS[3],
             "message": "chore: raise payments batch size for the settle window",
             "edits": [("services/payments-worker/config.py", "BATCH_SIZE = 200", "BATCH_SIZE = 800")]},
        ],
    },
    {
        "id": "INC-15",
        "onset": _onset(342),
        "service": "auth-gateway",
        "severity": "sev2",
        "title": "auth-gateway circuit breaker open, 503s on token refresh",
        "description": (
            "The breaker around the identity provider is opening within seconds of closing, "
            "producing 503s on ~9% of refresh calls. The provider itself reports normal "
            "latency and a 0.8% error rate, which is its usual background level."
        ),
        "metrics": {"error_rate_before": 0.008, "error_rate_after": 0.09, "duration_minutes": 29},
        "labels": {"alertname": "AuthCircuitOpen", "team": "platform"},
        "cause": {
            "at_h": 1.8, "author": AUTHORS[4],
            "message": "fix: make the identity breaker more sensitive\n\n"
                       "We rode out a 3-minute provider brownout last week without tripping.\n"
                       "A lower threshold should shed load sooner.",
            "edits": [("services/auth-gateway/config.py",
                       "CIRCUIT_FAILURE_THRESHOLD = 20", "CIRCUIT_FAILURE_THRESHOLD = 2")],
        },
        "decoys": [
            {"at_h": 0.7, "author": AUTHORS[5],
             "message": "perf: shorten identity provider timeout\n\n"
                        "Their p99 is 400ms, so 2.5s is generous.",
             "edits": [("services/auth-gateway/config.py", "HTTP_TIMEOUT_MS = 2500", "HTTP_TIMEOUT_MS = 1200")]},
            {"at_h": 3.6, "author": AUTHORS[6],
             "message": "chore: reduce breaker reset window",
             "edits": [("services/auth-gateway/config.py", "CIRCUIT_RESET_S = 30", "CIRCUIT_RESET_S = 10")]},
        ],
    },
    {
        "id": "INC-16",
        "onset": _onset(367),
        "service": "checkout-api",
        "severity": "sev2",
        "title": "checkout-api rejecting 17% of orders with 413",
        "description": (
            "17% of POST /checkout requests are rejected with 413 payload_too_large. "
            "The rejected requests are carts with more than about 12 line items, which "
            "correlates with our highest-value orders."
        ),
        "metrics": {"error_rate_before": 0.003, "error_rate_after": 0.17, "duration_minutes": 62},
        "labels": {"alertname": "CheckoutPayloadRejected", "endpoint": "/checkout", "team": "checkout"},
        "cause": {
            "at_h": 2.9, "author": AUTHORS[7],
            "message": "security: cap request body size on checkout\n\n"
                       "Pen test finding PT-19 asked for an explicit body cap on the write path.\n"
                       "64KB is well above the p99 request we measured.",
            "edits": [("services/checkout-api/config.py",
                       "MAX_PAYLOAD_BYTES = 2097152", "MAX_PAYLOAD_BYTES = 65536")],
        },
        "decoys": [
            {"at_h": 0.6, "author": AUTHORS[8],
             "message": "feat: support bulk line-item add on the cart endpoint\n\n"
                        "Lets the mobile client submit a whole cart in one call.",
             "edits": [("services/checkout-api/handlers.py",
                        "def handle_list(request):",
                        "def handle_bulk(request):\n    return [handle_write(r) for r in request.get(\"items\", [])]\n\n\ndef handle_list(request):")]},
            {"at_h": 1.5, "author": AUTHORS[9],
             "message": "chore: enable strict validation on checkout",
             "edits": [("deploy/checkout-api/values.yaml",
                        'FEATURE_STRICT_VALIDATION: "false"', 'FEATURE_STRICT_VALIDATION: "true"')]},
        ],
    },
    {
        "id": "INC-17",
        "onset": _onset(391),
        "service": "web-frontend",
        "severity": "sev2",
        "title": "web-frontend 404s on all static assets, pages render unstyled",
        "description": (
            "Every CSS and JS asset returns 404 from the CDN. Pages render as unstyled HTML. "
            "The application itself is healthy: API calls succeed and the server returns 200 "
            "for document requests."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.44, "duration_minutes": 19},
        "labels": {"alertname": "FrontendAssetsMissing", "team": "web"},
        "cause": {
            "at_h": 0.5, "author": AUTHORS[0],
            "message": "chore: point assets at the v5 CDN bucket\n\n"
                       "v5 has the new compression settings. Cutting over ahead of the\n"
                       "bucket lifecycle change next week.",
            "edits": [("deploy/web-frontend/values.yaml",
                       'ASSET_PREFIX: "https://cdn.internal/static/v4"',
                       'ASSET_PREFIX: "https://cdn.internal/static/v5"')],
        },
        "decoys": [
            {"at_h": 0.2, "author": AUTHORS[1],
             "message": "fix: correct the frontend readiness probe path",
             "edits": [("deploy/web-frontend/values.yaml", "path: /healthz", "path: /health")]},
            {"at_h": 6.0, "author": AUTHORS[2],
             "message": "perf: lower frontend cache TTL for faster rollouts",
             "edits": [("services/web-frontend/config.py", "CACHE_TTL_S = 120", "CACHE_TTL_S = 30")]},
        ],
    },
    {
        "id": "INC-18",
        "onset": _onset(414),
        "service": "inventory-api",
        "severity": "sev2",
        "title": "inventory-api query rate 40x, database at 96% CPU",
        "description": (
            "Query count per API request jumped from roughly 2 to roughly 80. The database is "
            "at 96% CPU serving a very high volume of tiny, fast, identical-shape SELECTs. "
            "No individual query is slow."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.07, "duration_minutes": 48,
                    "latency_p99_ms": 3100},
        "labels": {"alertname": "InventoryQueryAmplification", "team": "supply"},
        "cause": {
            "at_h": 2.3, "author": AUTHORS[3],
            "message": "refactor: simplify inventory availability lookup\n\n"
                       "Replaces the hand-written join with a clearer per-item lookup. Easier\n"
                       "to reason about and the tests are much simpler.",
            "edits": [("services/inventory-api/store.py",
                       'def fetch_page(limit, cursor=None):\n'
                       '    with pool() as conn:\n'
                       '        return conn.execute(\n'
                       '            "SELECT * FROM inventory_api WHERE id > %s ORDER BY id LIMIT %s",\n'
                       '            (cursor or 0, limit),\n'
                       '        ).fetchall()',
                       'def fetch_page(limit, cursor=None):\n'
                       '    with pool() as conn:\n'
                       '        ids = conn.execute(\n'
                       '            "SELECT id FROM inventory_api WHERE id > %s ORDER BY id LIMIT %s",\n'
                       '            (cursor or 0, limit),\n'
                       '        ).fetchall()\n'
                       '    rows = []\n'
                       '    for row_id in ids:\n'
                       '        rows.append(fetch_one(row_id))\n'
                       '    return rows')],
        },
        "decoys": [
            {"at_h": 0.8, "author": AUTHORS[4],
             "message": "perf: widen inventory pool to handle the new query volume\n\n"
                        "Connections were queuing during peak.",
             "edits": [("services/inventory-api/config.py", "DB_POOL_SIZE = 70", "DB_POOL_SIZE = 120")]},
            {"at_h": 4.4, "author": AUTHORS[5],
             "message": "chore: shorten inventory cache TTL for fresher stock counts",
             "edits": [("services/inventory-api/config.py", "CACHE_TTL_S = 300", "CACHE_TTL_S = 15")]},
        ],
    },
    {
        "id": "INC-19",
        "onset": _onset(439),
        "service": "cart-service",
        "severity": "sev2",
        "title": "cart-service cache hit ratio 0%, origin load 9x",
        "description": (
            "Cache hit ratio for cart-service went to zero at 08:15. search-api and "
            "inventory-api show the same pattern at the same minute, which rules out any "
            "single service's own code. Origin databases are carrying the full load."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.028, "duration_minutes": 40,
                    "latency_p99_ms": 2900},
        "labels": {"alertname": "CartCacheHitRatioLow", "team": "checkout"},
        "cause": {
            "at_h": 1.6, "author": AUTHORS[6],
            "message": "chore: bump cache key namespace for the new payload shape\n\n"
                       "The v3 payloads are missing the fields the new renderer needs, so the\n"
                       "namespace has to move with the shape change.",
            "edits": [("libs/cache.py", 'KEY_NAMESPACE = "v3"', 'KEY_NAMESPACE = "v4"')],
        },
        "decoys": [
            {"at_h": 0.7, "author": AUTHORS[7],
             "message": "fix: stop caching carts that contain gift cards\n\n"
                        "Gift card balances were going stale.",
             "edits": [("services/cart-service/handlers.py",
                        "    cache.put(key, payload, ttl=config.CACHE_TTL_S)",
                        "    if not request.get(\"has_gift_card\"):\n        cache.put(key, payload, ttl=config.CACHE_TTL_S)")]},
            {"at_h": 2.5, "author": AUTHORS[8],
             "message": "chore: lower cart cache TTL",
             "edits": [("services/cart-service/config.py", "CACHE_TTL_S = 300", "CACHE_TTL_S = 90")]},
        ],
    },
    {
        "id": "INC-20",
        "onset": _onset(463),
        "service": "payments-worker",
        "severity": "sev1",
        "title": "payments-worker rejecting amounts, 28% of settlements failing",
        "description": (
            "28% of settlements fail with 'invalid amount: expected string, got float' from "
            "the provider SDK. Failures affect only amounts with fractional cents after "
            "currency conversion. The worker's own code has not changed in three days."
        ),
        "metrics": {"error_rate_before": 0.003, "error_rate_after": 0.28, "duration_minutes": 43},
        "labels": {"alertname": "PaymentsAmountRejected", "team": "payments"},
        "cause": {
            "at_h": 3.1, "author": AUTHORS[9],
            "message": "perf: emit datetimes as epoch millis in shared serialization\n\n"
                       "Saves about 8 bytes per field across every internal payload and the\n"
                       "parsers on both sides are faster.",
            "edits": [("packages/common/serialization.py",
                       'DATETIME_FORMAT = "iso8601"', 'DATETIME_FORMAT = "epoch_millis"'),
                      ("packages/common/serialization.py",
                       "def decode_money(raw):\n    return Decimal(str(raw))",
                       "def decode_money(raw):\n    return Decimal(raw) if isinstance(raw, str) else Decimal(repr(float(raw)))")],
        },
        "decoys": [
            {"at_h": 1.2, "author": AUTHORS[0],
             "message": "fix: normalize currency conversion rounding in payments\n\n"
                        "Conversion was producing sub-cent amounts on a few corridors.",
             "edits": [("services/payments-worker/handlers.py",
                        "def call_upstream(path, payload):",
                        "def convert(amount, rate):\n    return round(amount * rate, 2)\n\n\ndef call_upstream(path, payload):")]},
            {"at_h": 2.2, "author": AUTHORS[1],
             "message": "chore: raise payments worker timeout for slow corridors",
             "edits": [("services/payments-worker/config.py", "HTTP_TIMEOUT_MS = 2500", "HTTP_TIMEOUT_MS = 6000")]},
        ],
    },
]


def all_commits() -> list[dict]:
    """Every commit across every incident, in chronological order, tagged with its role."""
    commits: list[dict] = []
    for incident in INCIDENTS:
        onset: datetime = incident["onset"]
        cause = dict(incident["cause"])
        cause.update(incident_id=incident["id"], role="cause",
                     at=onset - timedelta(hours=cause["at_h"]))
        commits.append(cause)
        for decoy in incident["decoys"]:
            entry = dict(decoy)
            entry.update(incident_id=incident["id"], role="decoy",
                         at=onset - timedelta(hours=entry["at_h"]))
            commits.append(entry)
    commits.sort(key=lambda c: c["at"])
    return commits
