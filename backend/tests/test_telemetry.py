from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from backend import telemetry


@pytest.fixture(autouse=True)
def clean_telemetry_environment(monkeypatch):
    for name in (
        "OTEL_SDK_DISABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "ENVIRONMENT_NAME",
        "IMAGE_TAG",
        "OTEL_PYTHON_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_resource_identifies_the_service_environment_and_version(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT_NAME", "prod")
    monkeypatch.setenv("IMAGE_TAG", "20260830-014229-4175fa9")

    attributes = telemetry.build_resource().attributes

    assert attributes[telemetry.SERVICE_NAME_ATTRIBUTE] == "interview-share-canvas"
    assert attributes[telemetry.ENVIRONMENT_ATTRIBUTE] == "prod"
    assert attributes[telemetry.SERVICE_VERSION_ATTRIBUTE] == "20260830-014229-4175fa9"


def test_resource_falls_back_when_nothing_deployed_the_process():
    attributes = telemetry.build_resource().attributes

    assert attributes[telemetry.ENVIRONMENT_ATTRIBUTE] == telemetry.LOCAL_ENVIRONMENT_NAME
    assert attributes[telemetry.SERVICE_VERSION_ATTRIBUTE] == telemetry.UNKNOWN_VERSION


def test_service_name_is_overridable(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "canvas-api")

    assert telemetry.build_resource().attributes[telemetry.SERVICE_NAME_ATTRIBUTE] == "canvas-api"


def test_telemetry_is_off_without_an_endpoint():
    assert telemetry.telemetry_enabled() is False


def test_telemetry_is_on_with_an_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    assert telemetry.telemetry_enabled() is True


def test_the_sdk_disabled_flag_wins_over_the_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    assert telemetry.telemetry_enabled() is False


def test_log_level_defaults_to_info():
    assert telemetry.log_level() == logging.INFO


def test_log_level_is_configurable(monkeypatch):
    monkeypatch.setenv("OTEL_PYTHON_LOG_LEVEL", "warning")

    assert telemetry.log_level() == logging.WARNING


def test_an_unreadable_log_level_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("OTEL_PYTHON_LOG_LEVEL", "chatty")

    assert telemetry.log_level() == logging.INFO


def test_configure_telemetry_does_nothing_when_disabled():
    application = FastAPI()

    assert telemetry.configure_telemetry(application) is False


def test_requests_produce_spans_carrying_the_deployment_resource(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")
    monkeypatch.setenv("IMAGE_TAG", "20260830-014229-4175fa9")
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=telemetry.build_resource())
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    application = FastAPI()

    @application.get("/v1/example")
    def example() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    FastAPIInstrumentor.instrument_app(
        application,
        tracer_provider=provider,
        excluded_urls=telemetry.EXCLUDED_URLS,
        exclude_spans=telemetry.EXCLUDED_ASGI_SPANS,
    )
    try:
        with TestClient(application) as client:
            assert client.get("/v1/example").status_code == 200
            assert client.get("/health").status_code == 200
    finally:
        FastAPIInstrumentor.uninstrument_app(application)

    spans = exporter.get_finished_spans()
    routes = {span.attributes.get("http.route") for span in spans}
    assert "/v1/example" in routes
    assert "/health" not in routes, "the health endpoint should stay untraced"
    assert [span.name for span in spans] == ["GET /v1/example"], "no ASGI receive or send spans"

    resource = spans[0].resource.attributes
    assert resource[telemetry.SERVICE_NAME_ATTRIBUTE] == "interview-share-canvas"
    assert resource[telemetry.ENVIRONMENT_ATTRIBUTE] == "dev"
    assert resource[telemetry.SERVICE_VERSION_ATTRIBUTE] == "20260830-014229-4175fa9"
