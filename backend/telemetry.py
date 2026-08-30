"""OpenTelemetry configuration for the API.

Telemetry stays off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` names a collector, so
local runs and the test suite neither export nor pay for instrumentation. The
deploy pipeline supplies the collector endpoint along with the two values that
identify a running instance: ``ENVIRONMENT_NAME`` (dev or prod) and
``IMAGE_TAG``, the ECR tag the host was deployed with.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.engine import Engine

DEFAULT_SERVICE_NAME = "interview-share-canvas"

# Used when nothing deployed this process: a developer's machine or a test run.
LOCAL_ENVIRONMENT_NAME = "local"
UNKNOWN_VERSION = "unknown"

SERVICE_NAME_ATTRIBUTE = "service.name"
SERVICE_VERSION_ATTRIBUTE = "service.version"
# The stable semantic convention, superseding the older deployment.environment.
ENVIRONMENT_ATTRIBUTE = "deployment.environment.name"

# The health endpoint is polled by the Compose healthcheck and by the deploy
# script; tracing it would drown the real traffic.
EXCLUDED_URLS = "health"

# The ASGI receive and send spans triple the span count per request and say
# nothing the request span does not already carry.
EXCLUDED_ASGI_SPANS = ["receive", "send"]

DEFAULT_LOG_LEVEL = logging.INFO

# Uvicorn configures these three itself and turns propagation off, so a handler
# on the root logger never sees the server's own logs, the access log included.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")

logger = logging.getLogger(__name__)
# Uvicorn owns the only console handler in a deployed process, so the startup
# confirmation is logged where an operator reading `docker logs` will see it.
startup_logger = logging.getLogger("uvicorn.error")

_configured = False


def _environment_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def telemetry_enabled() -> bool:
    """Report whether an endpoint is configured and the SDK is not disabled."""
    if _environment_flag("OTEL_SDK_DISABLED"):
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def log_level() -> int:
    """Resolve the level below which log records are not exported."""
    configured = os.getenv("OTEL_PYTHON_LOG_LEVEL", "").strip().upper()
    if not configured:
        return DEFAULT_LOG_LEVEL
    level = logging.getLevelNamesMapping().get(configured)
    if level is None:
        logger.warning("Ignoring unknown OTEL_PYTHON_LOG_LEVEL %r", configured)
        return DEFAULT_LOG_LEVEL
    return level


def build_resource() -> Resource:
    """Describe this process: which service, which environment, which version."""
    return Resource.create(
        {
            SERVICE_NAME_ATTRIBUTE: os.getenv("OTEL_SERVICE_NAME", "").strip()
            or DEFAULT_SERVICE_NAME,
            ENVIRONMENT_ATTRIBUTE: os.getenv("ENVIRONMENT_NAME", "").strip()
            or LOCAL_ENVIRONMENT_NAME,
            SERVICE_VERSION_ATTRIBUTE: os.getenv("IMAGE_TAG", "").strip()
            or UNKNOWN_VERSION,
        }
    )


def _configure_providers(resource: Resource) -> None:
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    _export_logs(logger_provider)


def _export_logs(logger_provider: LoggerProvider) -> None:
    """Ship application and uvicorn logs with the same resource as the spans."""
    level = log_level()
    handler = LoggingHandler(level=level, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # A record is discarded before any handler runs when its logger sits above
    # this level, and the root logger defaults to WARNING.
    if root_logger.level > level:
        root_logger.setLevel(level)
    for name in UVICORN_LOGGERS:
        logging.getLogger(name).addHandler(handler)


def configure_telemetry(application: FastAPI, engine: Engine | None = None) -> bool:
    """Instrument the application, returning whether telemetry is exporting.

    Providers and SQLAlchemy are process-wide, so they are configured once. A
    second application in the same process, which only the tests create, still
    gets its own request instrumentation.
    """
    global _configured

    if not telemetry_enabled():
        return False

    if not _configured:
        resource = build_resource()
        _configure_providers(resource)
        if engine is not None:
            SQLAlchemyInstrumentor().instrument(engine=engine)
        _configured = True
        startup_logger.info(
            "OpenTelemetry enabled for %s %s in %s",
            resource.attributes[SERVICE_NAME_ATTRIBUTE],
            resource.attributes[SERVICE_VERSION_ATTRIBUTE],
            resource.attributes[ENVIRONMENT_ATTRIBUTE],
        )

    FastAPIInstrumentor.instrument_app(
        application,
        excluded_urls=EXCLUDED_URLS,
        exclude_spans=EXCLUDED_ASGI_SPANS,
    )
    return True
