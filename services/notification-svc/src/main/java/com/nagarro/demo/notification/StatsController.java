package com.nagarro.demo.notification;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/** Consumer health, read by the KAG graph builder as the queue-lag fact. */
@Tag(name = "Notifications", description = "Consumer throughput and lag")
@RestController
public class StatsController {

    private final OrderEventListener listener;

    public StatsController(OrderEventListener listener) {
        this.listener = listener;
    }

    @Operation(summary = "Messages processed, failed, and consumer lag")
    @GetMapping("/admin/stats")
    public Map<String, Object> stats() {
        return listener.stats();
    }
}
