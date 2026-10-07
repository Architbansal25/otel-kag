package com.nagarro.demo.inventory;

import io.swagger.v3.oas.annotations.Hidden;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/** Presenter-only controls. Hidden from Swagger so the audience does not see the cause. */
@Hidden
@RestController
public class ChaosController {

    private final Chaos chaos;

    public ChaosController(Chaos chaos) {
        this.chaos = chaos;
    }

    @GetMapping("/admin/chaos")
    public Map<String, Object> state() {
        return chaos.state();
    }

    @PostMapping("/admin/chaos/latency")
    public Map<String, Object> latency(@RequestParam long ms) {
        chaos.setLatency(ms);
        return chaos.state();
    }

    @PostMapping("/admin/chaos/errors")
    public Map<String, Object> errors(@RequestParam double rate) {
        chaos.setErrorRate(rate);
        return chaos.state();
    }

    @DeleteMapping("/admin/chaos")
    public Map<String, Object> clear() {
        chaos.clear();
        return chaos.state();
    }
}
