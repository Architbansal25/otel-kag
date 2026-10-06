package com.nagarro.demo.notification;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/** Consumer health, read by the KAG graph builder as the queue-lag fact. */
@RestController
public class StatsController {

    private final OrderEventListener listener;

    public StatsController(OrderEventListener listener) {
        this.listener = listener;
    }

    @GetMapping("/admin/stats")
    public Map<String, Object> stats() {
        return listener.stats();
    }
}
