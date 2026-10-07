package com.nagarro.demo.logging;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.core.AppenderBase;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributesBuilder;
import io.opentelemetry.api.trace.Span;

/**
 * Attaches every log line written while a request is being handled to that
 * request's span, as a span event.
 *
 * Jaeger stores traces, not logs, so without this the trace view has timings
 * but no words. With it, opening a span in Jaeger shows exactly the log lines
 * that request produced -- the "logs" an engineer would otherwise grep for
 * across four files, already sorted into the request they belong to.
 */
public class SpanEventAppender extends AppenderBase<ILoggingEvent> {

    private static final AttributeKey<String> LEVEL = AttributeKey.stringKey("log.severity");
    private static final AttributeKey<String> LOGGER = AttributeKey.stringKey("log.logger");
    private static final AttributeKey<String> MESSAGE = AttributeKey.stringKey("log.message");
    private static final AttributeKey<String> EX_TYPE = AttributeKey.stringKey("exception.type");
    private static final AttributeKey<String> EX_MESSAGE = AttributeKey.stringKey("exception.message");

    @Override
    protected void append(ILoggingEvent event) {
        Span span = Span.current();
        if (!span.getSpanContext().isValid() || !span.isRecording()) {
            return; // startup and background work: not part of any request
        }
        String message = event.getFormattedMessage();
        AttributesBuilder attrs = Attributes.builder()
                .put(LEVEL, event.getLevel().toString())
                .put(LOGGER, event.getLoggerName())
                .put(MESSAGE, message);
        IThrowableProxy thrown = event.getThrowableProxy();
        if (thrown != null) {
            attrs.put(EX_TYPE, thrown.getClassName());
            attrs.put(EX_MESSAGE, String.valueOf(thrown.getMessage()));
        }
        // The event name is what Jaeger shows first, so make it the readable line.
        String name = event.getLevel() + " " + (message.length() > 160 ? message.substring(0, 160) + "..." : message);
        span.addEvent(name, attrs.build());
        if (event.getLevel().isGreaterOrEqual(Level.ERROR)) {
            span.setAttribute("log.has_error", true);
        }
    }
}
