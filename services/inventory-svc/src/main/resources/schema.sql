DROP TABLE IF EXISTS stock;
CREATE TABLE stock (
    sku      VARCHAR(32) PRIMARY KEY,
    quantity INT NOT NULL
);
