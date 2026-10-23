PRAGMA foreign_keys = ON;

CREATE TABLE CATEGORY (
    cat_id TEXT PRIMARY KEY
);

CREATE TABLE PRODUCT (
    item_id TEXT PRIMARY KEY,
    dept_id TEXT NOT NULL,
    cat_id TEXT NOT NULL,
    FOREIGN KEY (cat_id) REFERENCES CATEGORY(cat_id)
);

CREATE TABLE BRANCH (
    store_id TEXT PRIMARY KEY
);

CREATE TABLE SALE (
    sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    sale_date DATE NOT NULL,
    UNIQUE(store_id, sale_date),
    FOREIGN KEY (store_id) REFERENCES BRANCH(store_id)
);

CREATE TABLE SALE_ITEM (
    sale_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    quantity_sold INTEGER NOT NULL CHECK (quantity_sold >= 0),
    PRIMARY KEY (sale_id, item_id),
    FOREIGN KEY (sale_id) REFERENCES SALE(sale_id),
    FOREIGN KEY (item_id) REFERENCES PRODUCT(item_id)
);

CREATE TABLE PRODUCT_PRICE (
    store_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    price_date DATE NOT NULL,
    sell_price REAL NOT NULL CHECK (sell_price >= 0),
    PRIMARY KEY (store_id, item_id, price_date),
    FOREIGN KEY (store_id) REFERENCES BRANCH(store_id),
    FOREIGN KEY (item_id) REFERENCES PRODUCT(item_id)
);

CREATE TABLE INVENTORY (
    inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    sales_last_14_days INTEGER NOT NULL CHECK (sales_last_14_days >= 0),
    current_stock INTEGER NOT NULL CHECK (current_stock >= 0),
    UNIQUE(item_id, store_id),
    FOREIGN KEY (item_id) REFERENCES PRODUCT(item_id),
    FOREIGN KEY (store_id) REFERENCES BRANCH(store_id)
);

CREATE TABLE INVENTORY_AGE (
    inventory_id INTEGER PRIMARY KEY,
    received_date DATE NOT NULL,
    inventory_age_days INTEGER NOT NULL CHECK (inventory_age_days >= 0),
    age_status TEXT NOT NULL CHECK (
        age_status IN ('Fresh', 'Aging', 'Critical')
    ),
    FOREIGN KEY (inventory_id) REFERENCES INVENTORY(inventory_id)
);

CREATE TABLE DEMAND (
    item_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    total_units_sold INTEGER NOT NULL CHECK (total_units_sold >= 0),
    avg_daily_demand REAL NOT NULL CHECK (avg_daily_demand >= 0),
    PRIMARY KEY (item_id, store_id),
    FOREIGN KEY (item_id) REFERENCES PRODUCT(item_id),
    FOREIGN KEY (store_id) REFERENCES BRANCH(store_id)
);