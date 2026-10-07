package com.nagarro.demo.order;

import io.swagger.v3.oas.annotations.Hidden;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jms.JmsException;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.net.ConnectException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentLinkedDeque;

@Tag(name = "Orders", description = "Place and look up customer orders")
@RestController
public class OrderController {

    private static final Logger log = LoggerFactory.getLogger(OrderController.class);
    private static final String QUEUE = "order.events";
    private static final int RECENT_ORDERS = 50;

    private final RestClient inventory;
    private final JmsTemplate jms;

    /** The last few placed orders, newest first. In-memory: this is a demo, not a ledger. */
    private final Deque<Map<String, Object>> recent = new ConcurrentLinkedDeque<>();

    public OrderController(RestClient inventoryClient, JmsTemplate jms) {
        this.inventory = inventoryClient;
        this.jms = jms;
    }

    @Schema(description = "A new order")
    public record OrderRequest(@Schema(example = "SKU-1001",
            description = "Product to order: SKU-1001, SKU-1002 or SKU-1003") String sku) {
    }

    @Operation(summary = "Place an order",
            description = "Reserves stock in inventory-svc, then publishes an event that "
                    + "notification-svc consumes to notify the customer.")
    @PostMapping("/orders")
    public ResponseEntity<Map<String, Object>> placeOrder(@RequestBody(required = false) OrderRequest body) {
        String sku = body == null || body.sku() == null || body.sku().isBlank() ? "SKU-1001" : body.sku();
        String orderId = UUID.randomUUID().toString();

        try {
            inventory.post()
                    .uri("/inventory/{sku}/reserve", sku)
                    .retrieve()
                    .body(Map.class);
        } catch (ResourceAccessException e) {
            if (e.getCause() instanceof ConnectException) {
                // Nothing is listening: the dependency is down, not slow.
                log.error("Inventory unreachable for order {} sku {}: {}", orderId, sku, e.getMessage());
                return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                        .body(Map.of("error", "inventory_unavailable", "orderId", orderId, "sku", sku));
            }
            // Read timeout against inventory-svc. This is the alarm the room sees
            // when inventory slows down -- and it names the wrong service.
            log.error("Inventory call timed out for order {} sku {}: {}", orderId, sku, e.getMessage());
            return ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
                    .body(Map.of("error", "inventory_timeout", "orderId", orderId, "sku", sku));
        } catch (RestClientResponseException e) {
            log.warn("Inventory rejected order {} sku {}: HTTP {}", orderId, sku, e.getStatusCode().value());
            HttpStatus status = e.getStatusCode().is5xxServerError()
                    ? HttpStatus.BAD_GATEWAY : HttpStatus.valueOf(e.getStatusCode().value());
            return ResponseEntity.status(status)
                    .body(Map.of("error", "inventory_rejected", "orderId", orderId, "sku", sku,
                            "inventoryStatus", e.getStatusCode().value()));
        }

        // The async hop. The OTel Java agent instruments JMS, so trace context
        // propagates across the queue into notification-svc.
        try {
            jms.convertAndSend(QUEUE, "{\"orderId\":\"" + orderId + "\",\"sku\":\"" + sku + "\"}");
        } catch (JmsException e) {
            log.error("Could not publish order {} to {}: {}", orderId, QUEUE, e.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(Map.of("error", "broker_unavailable", "orderId", orderId, "sku", sku));
        }
        log.info("Order {} placed for sku {}", orderId, sku);

        Map<String, Object> order = Map.of("orderId", orderId, "sku", sku, "status", "PLACED",
                "placedAt", Instant.now().toString());
        recent.addFirst(order);
        while (recent.size() > RECENT_ORDERS) {
            recent.pollLast();
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(order);
    }

    @Operation(summary = "List the most recent orders (newest first)")
    @GetMapping("/orders")
    public List<Map<String, Object>> recentOrders() {
        return Collections.unmodifiableList(new ArrayList<>(recent));
    }

    @Operation(summary = "Look up one order by id")
    @GetMapping("/orders/{orderId}")
    public ResponseEntity<Map<String, Object>> order(
            @Parameter(description = "Id returned by POST /orders") @PathVariable String orderId) {
        return recent.stream()
                .filter(o -> orderId.equals(o.get("orderId")))
                .findFirst()
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.status(HttpStatus.NOT_FOUND)
                        .body(Map.of("error", "not_found", "orderId", orderId)));
    }

    @Operation(summary = "Check stock for a product (proxied to inventory-svc)")
    @GetMapping("/products/{sku}/availability")
    public ResponseEntity<Map<String, Object>> availability(
            @Parameter(example = "SKU-1001") @PathVariable String sku) {
        try {
            Map<?, ?> stock = inventory.get()
                    .uri("/inventory/{sku}/stock", sku)
                    .retrieve()
                    .body(Map.class);
            Object qty = stock == null ? null : stock.get("quantity");
            return ResponseEntity.ok(Map.of("sku", sku, "available", qty == null ? 0 : qty));
        } catch (ResourceAccessException e) {
            boolean down = e.getCause() instanceof ConnectException;
            log.error("Availability check for {} failed: {}", sku, e.getMessage());
            return ResponseEntity.status(down ? HttpStatus.SERVICE_UNAVAILABLE : HttpStatus.GATEWAY_TIMEOUT)
                    .body(Map.of("error", down ? "inventory_unavailable" : "inventory_timeout", "sku", sku));
        } catch (RestClientResponseException e) {
            return ResponseEntity.status(e.getStatusCode().is5xxServerError()
                            ? HttpStatus.BAD_GATEWAY : HttpStatus.valueOf(e.getStatusCode().value()))
                    .body(Map.of("error", "inventory_rejected", "sku", sku));
        }
    }

    /**
     * Scenario 1: publishes a malformed payload straight onto the queue so
     * notification-svc fails to parse it. Hidden from Swagger: presenter only.
     */
    @Hidden
    @PostMapping("/orders/poison")
    public ResponseEntity<Map<String, Object>> poison() {
        jms.convertAndSend(QUEUE, "{\"orderId\":\"NOT-CLOSED\", \"sku\":");
        log.warn("Published a deliberately malformed message to {}", QUEUE);
        return ResponseEntity.accepted().body(Map.of("status", "POISON_SENT"));
    }
}
