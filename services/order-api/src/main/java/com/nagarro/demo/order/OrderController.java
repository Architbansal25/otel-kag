package com.nagarro.demo.order;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.util.Map;
import java.util.UUID;

@RestController
public class OrderController {

    private static final Logger log = LoggerFactory.getLogger(OrderController.class);
    private static final String QUEUE = "order.events";

    private final RestClient inventory;
    private final JmsTemplate jms;

    public OrderController(RestClient inventoryClient, JmsTemplate jms) {
        this.inventory = inventoryClient;
        this.jms = jms;
    }

    @PostMapping("/orders")
    public ResponseEntity<Map<String, Object>> placeOrder(@RequestBody Map<String, Object> body) {
        String sku = String.valueOf(body.getOrDefault("sku", "SKU-1001"));
        String orderId = UUID.randomUUID().toString();

        try {
            inventory.post()
                    .uri("/inventory/{sku}/reserve", sku)
                    .retrieve()
                    .body(Map.class);
        } catch (ResourceAccessException e) {
            // Read timeout against inventory-svc. This is the alarm the room sees
            // during scenario 2 -- and it names the wrong service.
            log.error("Inventory call timed out for order {} sku {}: {}", orderId, sku, e.getMessage());
            return ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
                    .body(Map.of("error", "inventory_timeout", "orderId", orderId, "sku", sku));
        } catch (RestClientResponseException e) {
            log.warn("Inventory rejected order {} sku {}: HTTP {}", orderId, sku, e.getStatusCode().value());
            return ResponseEntity.status(e.getStatusCode())
                    .body(Map.of("error", "inventory_rejected", "orderId", orderId, "sku", sku));
        }

        // The async hop. The OTel Java agent instruments JMS, so trace context
        // propagates across the queue into notification-svc.
        jms.convertAndSend(QUEUE, "{\"orderId\":\"" + orderId + "\",\"sku\":\"" + sku + "\"}");
        log.info("Order {} placed for sku {}", orderId, sku);

        return ResponseEntity.status(HttpStatus.CREATED)
                .body(Map.of("orderId", orderId, "sku", sku, "status", "PLACED"));
    }

    /**
     * Scenario 1: publishes a malformed payload straight onto the queue so
     * notification-svc fails to parse it. A single-hop, obvious failure --
     * the warm-up that the flat-log baseline also solves.
     */
    @PostMapping("/orders/poison")
    public ResponseEntity<Map<String, Object>> poison() {
        jms.convertAndSend(QUEUE, "{\"orderId\":\"NOT-CLOSED\", \"sku\":");
        log.warn("Published a deliberately malformed message to {}", QUEUE);
        return ResponseEntity.accepted().body(Map.of("status", "POISON_SENT"));
    }
}
