"""OpenTelemetry tracing: one trace per answer, with a span for each model call and each lookup.

Off unless NEUROSCOPE_TRACING is set:
    NEUROSCOPE_TRACING=gcp       export to Google Cloud Trace (the live site; needs roles/cloudtrace.agent)
    NEUROSCOPE_TRACING=console   print spans to stderr (local debugging)
Needs the optional packages: pip install -e ".[tracing]". Without them, or when off, every span is a
no-op, so tracing can never break an answer.
"""

from __future__ import annotations

import contextlib
import logging
import os

log = logging.getLogger("neuroscope.tracing")
_tracer = None


def setup() -> None:
    """Configure the exporter once per process, from NEUROSCOPE_TRACING."""
    global _tracer
    mode = os.environ.get("NEUROSCOPE_TRACING", "").strip().lower()
    if not mode or _tracer is not None:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        provider = TracerProvider(resource=Resource.create({"service.name": "neuroscope"}))
        if mode == "gcp":
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            exporter = CloudTraceSpanExporter(project_id=os.environ.get("GOOGLE_CLOUD_PROJECT"))
        else:
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("neuroscope")
        log.info("tracing on (%s)", mode)
    except Exception as exc:                      # missing packages or credentials: stay off
        log.warning("tracing not enabled: %s", exc)


@contextlib.contextmanager
def span(name: str, **attributes):
    """A span with attributes; yields a setter for attributes known only at the end."""
    if _tracer is None:
        yield lambda **_: None
        return
    with _tracer.start_as_current_span(name) as s:
        def set_attrs(**more):
            for k, v in more.items():
                if v is not None:
                    s.set_attribute(f"neuroscope.{k}", v if isinstance(v, (int, float, bool, str)) else str(v))
        set_attrs(**attributes)
        yield set_attrs
