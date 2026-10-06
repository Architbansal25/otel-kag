package com.nagarro.demo.inventory;

import com.zaxxer.hikari.HikariDataSource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.sql.DataSource;
import java.util.Map;

@RestController
public class InventoryController {

    private static final Logger log = LoggerFactory.getLogger(InventoryController.class);

    private final InventoryService inventory;
    private final DataSource dataSource;

    public InventoryController(InventoryService inventory, DataSource dataSource) {
        this.inventory = inventory;
        this.dataSource = dataSource;
    }

    @PostMapping("/inventory/{sku}/reserve")
    public Map<String, Object> reserve(@PathVariable String sku) {
        int remaining = inventory.reserve(sku);
        return Map.of("sku", sku, "reserved", 1, "remaining", remaining);
    }

    @GetMapping("/inventory/{sku}/stock")
    public Map<String, Object> stock(@PathVariable String sku) {
        return Map.of("sku", sku, "quantity", inventory.stock(sku));
    }

    /**
     * Exposes the live pool configuration. The KAG graph builder reads this to
     * record the ConnectionPool node's capacity as a fact -- it is the property
     * that "deploy #47" changes, and the one the causal chain ultimately names.
     */
    @GetMapping("/admin/pool")
    public Map<String, Object> pool() {
        if (dataSource instanceof HikariDataSource hikari) {
            var mx = hikari.getHikariPoolMXBean();
            return Map.of(
                    "maxPoolSize", hikari.getMaximumPoolSize(),
                    "connectionTimeoutMs", hikari.getConnectionTimeout(),
                    "active", mx == null ? -1 : mx.getActiveConnections(),
                    "idle", mx == null ? -1 : mx.getIdleConnections(),
                    "awaitingConnection", mx == null ? -1 : mx.getThreadsAwaitingConnection());
        }
        return Map.of("maxPoolSize", -1, "note", "not a HikariDataSource");
    }

    @ExceptionHandler(InventoryService.OutOfStockException.class)
    public ResponseEntity<Map<String, Object>> outOfStock(InventoryService.OutOfStockException e) {
        log.warn("Reservation rejected: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.CONFLICT)
                .body(Map.of("error", "out_of_stock", "message", e.getMessage()));
    }
}
