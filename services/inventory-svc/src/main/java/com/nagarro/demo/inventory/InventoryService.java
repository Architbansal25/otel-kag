package com.nagarro.demo.inventory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class InventoryService {

    private static final Logger log = LoggerFactory.getLogger(InventoryService.class);

    private final JdbcTemplate jdbc;

    /**
     * How long a reservation keeps its JDBC connection checked out.
     *
     * This is the demo's stand-in for a genuinely slow query. It matters because
     * pool exhaustion is a function of (hold time x concurrency) / pool size --
     * the hold time is what turns a small pool into an outage.
     */
    @Value("${demo.query-hold-ms:150}")
    private long queryHoldMs;

    public InventoryService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * Reserves one unit of `sku`. Runs in a transaction, so the connection stays
     * checked out of the Hikari pool for the whole method -- including the hold.
     */
    @Transactional
    public int reserve(String sku) {
        Integer available = jdbc.queryForObject(
                "SELECT quantity FROM stock WHERE sku = ?", Integer.class, sku);

        holdConnection();

        if (available == null || available <= 0) {
            throw new OutOfStockException(sku);
        }
        jdbc.update("UPDATE stock SET quantity = quantity - 1 WHERE sku = ?", sku);
        return available - 1;
    }

    @Transactional(readOnly = true)
    public int stock(String sku) {
        Integer quantity = jdbc.queryForObject(
                "SELECT quantity FROM stock WHERE sku = ?", Integer.class, sku);
        holdConnection();
        return quantity == null ? 0 : quantity;
    }

    private void holdConnection() {
        try {
            Thread.sleep(queryHoldMs);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.warn("Interrupted while holding the connection");
        }
    }

    public static class OutOfStockException extends RuntimeException {
        public OutOfStockException(String sku) {
            super("No stock remaining for sku " + sku);
        }
    }
}
