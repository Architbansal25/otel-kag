package com.nagarro.demo.notification;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.jms.TextMessage;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jms.annotation.JmsListener;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.util.Map;
import java.util.Deque;
import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.atomic.AtomicLong;

@Component
public class OrderEventListener {

    private static final Logger log = LoggerFactory.getLogger(OrderEventListener.class);

    private final ObjectMapper mapper = new ObjectMapper();
    private final RestClient inventory;

    private final AtomicLong processed = new AtomicLong();
    private final AtomicLong failed = new AtomicLong();
    private final AtomicLong lastLagMs = new AtomicLong();

    /**
     * Peak lag over a sliding window.
     *
     * `lastLagMs` alone is useless for diagnosis: the instant traffic stops the
     * consumer drains the backlog and the spot value drops to ~1ms, so the
     * symptom disappears seconds after the incident and the RCA sees nothing.
     * A real incident does not stop being an incident because you stopped
     * looking, so the symptom is peak-within-window, which decays honestly
     * rather than vanishing.
     */
    private static final long LAG_WINDOW_MS = 5 * 60 * 1000L;
    private final Deque<long[]> lagSamples = new ConcurrentLinkedDeque<>();

    public OrderEventListener(RestClient inventoryClient) {
        this.inventory = inventoryClient;
    }

    /**
     * Concurrency must be high enough to keep up comfortably at baseline load --
     * otherwise the queue lags even on a healthy system and scenario 2 loses its
     * second symptom. Each message costs one stock lookup, so consumer throughput
     * is capped by inventory-svc: when the pool starves, these threads block on
     * connection acquisition, throughput collapses and the backlog grows. That
     * backlog looks like a messaging problem. It is not.
     */
    @JmsListener(destination = "order.events", concurrency = "${demo.consumer-concurrency:8}")
    public void onOrderEvent(TextMessage message) throws Exception {
        String payload = message.getText();

        // Queue lag: how long this message sat before anyone picked it up.
        long lag = System.currentTimeMillis() - message.getJMSTimestamp();
        lastLagMs.set(lag);
        lagSamples.add(new long[]{System.currentTimeMillis(), lag});
        if (lag > 1000) {
            log.warn("Consumer lag {} ms on order.events -- falling behind", lag);
        }

        Map<?, ?> event;
        try {
            event = mapper.readValue(payload, Map.class);
        } catch (Exception e) {
            // Scenario 1 lands here: loud, obvious, and genuinely self-contained.
            failed.incrementAndGet();
            log.error("Malformed order event, discarding. payload={} error={}", payload, e.getMessage());
            return;
        }

        String sku = String.valueOf(event.get("sku"));
        var stock = inventory.get()
                .uri("/inventory/{sku}/stock", sku)
                .retrieve()
                .body(Map.class);

        processed.incrementAndGet();
        log.info("Notified customer about order {} (sku {}, {} left)",
                event.get("orderId"), sku, stock == null ? "?" : stock.get("quantity"));
    }

    public Map<String, Object> stats() {
        // peakLagMs() prunes expired samples, so call it first. If nothing is left
        // in the window there has been no traffic, and reporting the last sticky
        // lag value alongside a zero peak reads as a contradiction.
        long peak = peakLagMs();
        long last = lagSamples.isEmpty() ? 0 : lastLagMs.get();
        return Map.of(
                "processed", processed.get(),
                "failed", failed.get(),
                "lastLagMs", last,
                "maxLagMs", peak,
                "idle", lagSamples.isEmpty(),
                "lagWindowMs", LAG_WINDOW_MS);
    }

    /** Highest lag seen in the last {@link #LAG_WINDOW_MS}, dropping older samples. */
    private long peakLagMs() {
        long cutoff = System.currentTimeMillis() - LAG_WINDOW_MS;
        long[] head;
        while ((head = lagSamples.peekFirst()) != null && head[0] < cutoff) {
            lagSamples.pollFirst();
        }
        long peak = 0;
        for (long[] sample : lagSamples) {
            peak = Math.max(peak, sample[1]);
        }
        return peak;
    }
}
