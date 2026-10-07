package com.nagarro.demo.inventory;

import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Runtime fault injection, flipped live from the demo console.
 *
 * Deliberately invisible to the rest of the system: nothing is logged and no
 * change record is written, so the only way to find out what happened is to
 * reason from the telemetry -- exactly the position an on-call engineer is in.
 */
@Component
public class Chaos {

    private volatile long extraLatencyMs = 0;
    private volatile double errorRate = 0.0;

    public void setLatency(long ms) {
        this.extraLatencyMs = Math.max(0, ms);
    }

    public void setErrorRate(double rate) {
        this.errorRate = Math.min(1.0, Math.max(0.0, rate));
    }

    public void clear() {
        this.extraLatencyMs = 0;
        this.errorRate = 0.0;
    }

    /** Called on every data fetch. Sleeps and/or fails according to the current settings. */
    public void apply() {
        long delay = extraLatencyMs;
        if (delay > 0) {
            try {
                Thread.sleep(delay);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        if (errorRate > 0 && ThreadLocalRandom.current().nextDouble() < errorRate) {
            throw new IllegalStateException("Stock lookup failed: storage read error");
        }
    }

    public Map<String, Object> state() {
        return Map.of("latencyMs", extraLatencyMs, "errorRate", errorRate);
    }
}
