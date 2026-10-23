-- Q1: Current Inventory
-- Displays the current stock of every product at every branch.

SELECT
    p.item_id,
    p.dept_id,
    p.cat_id,
    b.store_id,
    i.current_stock
FROM PRODUCT p
JOIN INVENTORY i
    ON p.item_id = i.item_id
JOIN BRANCH b
    ON i.store_id = b.store_id;


-- Q2: Average Daily Demand
-- Calculates the historical average daily demand for each product and branch.

SELECT
    p.item_id,
    b.store_id,
    d.total_units_sold,
    ROUND(d.avg_daily_demand, 2) AS avg_daily_demand
FROM DEMAND d
JOIN PRODUCT p
    ON d.item_id = p.item_id
JOIN BRANCH b
    ON d.store_id = b.store_id;


-- Q3: Inventory Aging Analysis
-- Shows the age and status of inventory at each branch.

SELECT
    p.item_id,
    b.store_id,
    i.current_stock,
    ia.received_date,
    ia.inventory_age_days,
    ia.age_status
FROM INVENTORY i
JOIN PRODUCT p
    ON i.item_id = p.item_id
JOIN BRANCH b
    ON i.store_id = b.store_id
JOIN INVENTORY_AGE ia
    ON i.inventory_id = ia.inventory_id
ORDER BY ia.inventory_age_days DESC;


-- Q4: Branch-wise Sales Summary
-- Summarizes total sales and average daily sales for each branch.

SELECT
    b.store_id,
    SUM(si.quantity_sold) AS total_units_sold,
    COUNT(DISTINCT s.sale_date) AS sales_days,
    ROUND(
        CAST(SUM(si.quantity_sold) AS REAL)
        / COUNT(DISTINCT s.sale_date),
        2
    ) AS avg_daily_sales
FROM SALE s
JOIN BRANCH b
    ON s.store_id = b.store_id
JOIN SALE_ITEM si
    ON s.sale_id = si.sale_id
GROUP BY b.store_id
ORDER BY total_units_sold DESC;


-- Q5: Category-wise Sales Summary
-- Summarizes sales volume for each product category.

SELECT
    c.cat_id,
    SUM(si.quantity_sold) AS total_units_sold,
    COUNT(DISTINCT p.item_id) AS product_count
FROM CATEGORY c
JOIN PRODUCT p
    ON c.cat_id = p.cat_id
JOIN SALE_ITEM si
    ON p.item_id = si.item_id
GROUP BY c.cat_id
HAVING SUM(si.quantity_sold) > 0
ORDER BY total_units_sold DESC;


-- Q6: Shortage Detection
-- Identifies branches where current stock is below expected
-- 7-day demand.

SELECT
    i.item_id,
    i.store_id,
    i.current_stock,
    ROUND(d.avg_daily_demand, 2) AS avg_daily_demand,
    ROUND(d.avg_daily_demand * 7, 2) AS expected_demand_7_days,
    ROUND(
        d.avg_daily_demand * 7 - i.current_stock,
        2
    ) AS shortage
FROM INVENTORY i
JOIN DEMAND d
    ON i.item_id = d.item_id
    AND i.store_id = d.store_id
WHERE d.avg_daily_demand * 7 > i.current_stock
ORDER BY shortage DESC;


-- Q7: Excess Stock Detection
-- Identifies branches where current stock is greater than
-- expected 7-day demand.

SELECT
    i.item_id,
    i.store_id,
    i.current_stock,
    ROUND(d.avg_daily_demand, 2) AS avg_daily_demand,
    ROUND(d.avg_daily_demand * 7, 2) AS expected_demand_7_days,
    ROUND(
        i.current_stock - d.avg_daily_demand * 7,
        2
    ) AS excess
FROM INVENTORY i
JOIN DEMAND d
    ON i.item_id = d.item_id
    AND i.store_id = d.store_id
WHERE i.current_stock > d.avg_daily_demand * 7
ORDER BY excess DESC;


-- Q8: Aging Inventory with Excess Stock
-- Identifies aging or critical inventory that also has excess stock,
-- making it a potential redistribution source.

SELECT
    i.item_id,
    i.store_id,
    i.current_stock,
    ia.inventory_age_days,
    ia.age_status,
    ROUND(d.avg_daily_demand * 7, 2) AS expected_demand_7_days,
    ROUND(
        i.current_stock - d.avg_daily_demand * 7,
        2
    ) AS excess
FROM INVENTORY i
JOIN INVENTORY_AGE ia
    ON i.inventory_id = ia.inventory_id
JOIN DEMAND d
    ON i.item_id = d.item_id
    AND i.store_id = d.store_id
WHERE ia.age_status IN ('Aging', 'Critical')
  AND i.current_stock > d.avg_daily_demand * 7
ORDER BY ia.inventory_age_days DESC;


-- Q9: Redistribution Candidates
-- Matches a branch with excess stock to another branch with
-- shortage of the same product and calculates the suggested
-- transfer quantity.

SELECT
    s.item_id,
    s.store_id AS source_branch,
    d.store_id AS destination_branch,
    ROUND(
        s.current_stock - s.avg_daily_demand * 7,
        2
    ) AS source_excess,
    ROUND(
        d.avg_daily_demand * 7 - d.current_stock,
        2
    ) AS destination_shortage,
    ROUND(
        MIN(
            s.current_stock - s.avg_daily_demand * 7,
            d.avg_daily_demand * 7 - d.current_stock
        ),
        2
    ) AS suggested_transfer_qty
FROM
(
    SELECT
        i.item_id,
        i.store_id,
        i.current_stock,
        d.avg_daily_demand
    FROM INVENTORY i
    JOIN DEMAND d
        ON i.item_id = d.item_id
        AND i.store_id = d.store_id
    WHERE i.current_stock > d.avg_daily_demand * 7
) s
JOIN
(
    SELECT
        i.item_id,
        i.store_id,
        i.current_stock,
        d.avg_daily_demand
    FROM INVENTORY i
    JOIN DEMAND d
        ON i.item_id = d.item_id
        AND i.store_id = d.store_id
    WHERE i.current_stock < d.avg_daily_demand * 7
) d
    ON s.item_id = d.item_id
    AND s.store_id <> d.store_id
ORDER BY suggested_transfer_qty DESC;

-- Q10: Branch Stock Position View
-- Creates a reusable view for inventory analysis.

CREATE VIEW IF NOT EXISTS v_branch_stock_position AS
SELECT
    i.item_id,
    i.store_id,
    i.current_stock,
    ROUND(d.avg_daily_demand, 2) AS avg_daily_demand,
    ROUND(d.avg_daily_demand * 7, 2) AS expected_demand_7_days,
    ROUND(
        MAX(0, d.avg_daily_demand * 7 - i.current_stock),
        2
    ) AS shortage,
    ROUND(
        MAX(0, i.current_stock - d.avg_daily_demand * 7),
        2
    ) AS excess
FROM INVENTORY i
JOIN DEMAND d
    ON i.item_id = d.item_id
    AND i.store_id = d.store_id;

-- Q11: Inventory Aging View
-- Creates a reusable view combining inventory and age information.

CREATE VIEW IF NOT EXISTS v_inventory_aging AS
SELECT
    i.item_id,
    i.store_id,
    i.current_stock,
    ia.received_date,
    ia.inventory_age_days,
    ia.age_status
FROM INVENTORY i
JOIN INVENTORY_AGE ia
    ON i.inventory_id = ia.inventory_id;

-- Q12: Redistribution Candidate View
-- Creates a reusable view of possible transfers between branches.

CREATE VIEW IF NOT EXISTS v_redistribution_candidates AS
SELECT
    s.item_id,
    s.store_id AS source_branch,
    d.store_id AS destination_branch,
    ROUND(
        s.current_stock - s.avg_daily_demand * 7,
        2
    ) AS source_excess,
    ROUND(
        d.avg_daily_demand * 7 - d.current_stock,
        2
    ) AS destination_shortage,
    ROUND(
        MIN(
            s.current_stock - s.avg_daily_demand * 7,
            d.avg_daily_demand * 7 - d.current_stock
        ),
        2
    ) AS suggested_transfer_qty
FROM
(
    SELECT
        i.item_id,
        i.store_id,
        i.current_stock,
        d.avg_daily_demand
    FROM INVENTORY i
    JOIN DEMAND d
        ON i.item_id = d.item_id
        AND i.store_id = d.store_id
    WHERE i.current_stock > d.avg_daily_demand * 7
) s
JOIN
(
    SELECT
        i.item_id,
        i.store_id,
        i.current_stock,
        d.avg_daily_demand
    FROM INVENTORY i
    JOIN DEMAND d
        ON i.item_id = d.item_id
        AND i.store_id = d.store_id
    WHERE i.current_stock < d.avg_daily_demand * 7
) d
    ON s.item_id = d.item_id
    AND s.store_id <> d.store_id;