package com.nagarro.demo.inventory;

import io.opentelemetry.api.trace.Span;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/** Echoes the current trace id so a response can be opened straight in Jaeger. */
@Component
public class TraceIdFilter extends OncePerRequestFilter {

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        var ctx = Span.current().getSpanContext();
        if (ctx.isValid()) {
            response.setHeader("X-Trace-Id", ctx.getTraceId());
        }
        chain.doFilter(request, response);
    }
}
