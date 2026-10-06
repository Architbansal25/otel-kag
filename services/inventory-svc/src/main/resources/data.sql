-- Generous quantities: scenario 2 must fail through pool starvation,
-- not by accidentally running the warehouse dry mid-demo.
INSERT INTO stock (sku, quantity) VALUES ('SKU-1001', 1000000);
INSERT INTO stock (sku, quantity) VALUES ('SKU-1002', 1000000);
INSERT INTO stock (sku, quantity) VALUES ('SKU-1003', 1000000);
-- Deliberately empty: used by scenario 1 to produce a genuine business error.
INSERT INTO stock (sku, quantity) VALUES ('SKU-DEAD', 0);
