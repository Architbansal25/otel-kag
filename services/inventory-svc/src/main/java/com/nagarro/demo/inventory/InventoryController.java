package com.nagarro.demo.inventory;

import com.zaxxer.hikari.HikariDataSource;
import org.slf4j.Logger;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.swagger.v3.oas.annotations.Hidden;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.sql.DataSource;
import java.util.List;
import java.util.Map;

@Tag(name = "Inventory", description = "Stock levels and reservations")
@RestController
public class InventoryController {

    private static final Logger log = LoggerFactory.getLogger(InventoryController.class);

    private final InventoryService inventory;
    private final DataSource dataSource;

    public InventoryController(InventoryService inventory, DataSource dataSource) {
        this.inventory = inventory;
        this.dataSource = dataSource;
    }

    @Operation(summary = "List all products and their stock levels")
    @GetMapping("/inventory")
    public List<Map<String, Object>> all() {
        return inventory.all();
    }

    @Operation(summary = "Reserve one unit of a product")
    @PostMapping("/inventory/{sku}/reserve")
    public Map<String, Object> reserve(
            @Parameter(example = "SKU-1001") @PathVariable String sku) {
        int remaining = inventory.reserve(sku);
        return Map.of("sku", sku, "reserved", 1, "remaining", remaining);
    }

    @Operation(summary = "Current stock level for one product")
    @GetMapping("/inventory/{sku}/stock")
    public Map<String, Object> stock(
            @Parameter(example = "SKU-1001") @PathVariable String sku) {
        return Map.of("sku", sku, "quantity", inventory.stock(sku));
    }

    /**
     * Exposes the live pool configuration. The KAG graph builder reads this to
     * record the ConnectionPool node's capacity as a fact -- it is the property
     * that "deploy #47" changes, and the one the causal chain ultimately names.
     */
    @Hidden
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

    /**
     * Anything unexpected. Spring's default 500 body says only "Internal Server
     * Error"; this one says why, and puts the reason on the trace so the RCA
     * engine can cite it.
     */
    @ExceptionHandler(RuntimeException.class)
    public ResponseEntity<Map<String, Object>> failure(RuntimeException e) {
        log.error("Request failed: {}", e.getMessage(), e);
        Span.current().recordException(e);
        Span.current().setStatus(StatusCode.ERROR, String.valueOf(e.getMessage()));
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("error", "internal_error", "message", String.valueOf(e.getMessage())));
    }

    @ExceptionHandler(org.springframework.dao.EmptyResultDataAccessException.class)
    public ResponseEntity<Map<String, Object>> unknownSku() {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(Map.of("error", "unknown_sku"));
    }
}
