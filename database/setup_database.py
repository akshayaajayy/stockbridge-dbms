import sqlite3
import pandas as pd
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DB_PATH = os.path.join(BASE_DIR, "database", "smartstock.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "database", "schema.sql")

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)

with open(SCHEMA_PATH, "r") as file:
    conn.executescript(file.read())

category = pd.read_csv(os.path.join(OUTPUT_DIR, "category.csv"))
product = pd.read_csv(os.path.join(OUTPUT_DIR, "product.csv"))
branch = pd.read_csv(os.path.join(OUTPUT_DIR, "branch.csv"))
sales = pd.read_csv(os.path.join(OUTPUT_DIR, "sales.csv"))
prices = pd.read_csv(os.path.join(OUTPUT_DIR, "product_price.csv"))
inventory = pd.read_csv(os.path.join(OUTPUT_DIR, "inventory.csv"))
demand = pd.read_csv(os.path.join(OUTPUT_DIR, "demand.csv"))

category.to_sql("CATEGORY", conn, if_exists="append", index=False)
product.to_sql("PRODUCT", conn, if_exists="append", index=False)
branch.to_sql("BRANCH", conn, if_exists="append", index=False)

sales["date"] = pd.to_datetime(sales["date"]).dt.strftime("%Y-%m-%d")

sale = sales[["store_id", "date"]].drop_duplicates().reset_index(drop=True)
sale["sale_id"] = range(1, len(sale) + 1)
sale = sale.rename(columns={"date": "sale_date"})

sale.to_sql("SALE", conn, if_exists="append", index=False)

sale_item = sales.merge(
    sale,
    left_on=["store_id", "date"],
    right_on=["store_id", "sale_date"],
    how="left"
)

sale_item = sale_item[
    ["sale_id", "item_id", "quantity_sold"]
]

sale_item.to_sql("SALE_ITEM", conn, if_exists="append", index=False)

prices["date"] = pd.to_datetime(prices["date"]).dt.strftime("%Y-%m-%d")

prices = prices.rename(columns={
    "date": "price_date"
})

prices.to_sql(
    "PRODUCT_PRICE",
    conn,
    if_exists="append",
    index=False
)

inventory_db = inventory[
    [
        "item_id",
        "store_id",
        "sales_last_14_days",
        "current_stock"
    ]
]

inventory_db.to_sql(
    "INVENTORY",
    conn,
    if_exists="append",
    index=False
)

inventory_ids = pd.read_sql_query(
    "SELECT inventory_id, item_id, store_id FROM INVENTORY",
    conn
)

age = inventory[
    [
        "item_id",
        "store_id",
        "received_date",
        "inventory_age_days",
        "age_status"
    ]
].copy()

age["received_date"] = pd.to_datetime(
    age["received_date"]
).dt.strftime("%Y-%m-%d")

age = age.merge(
    inventory_ids,
    on=["item_id", "store_id"],
    how="left"
)

age = age[
    [
        "inventory_id",
        "received_date",
        "inventory_age_days",
        "age_status"
    ]
]

age.to_sql(
    "INVENTORY_AGE",
    conn,
    if_exists="append",
    index=False
)

demand.to_sql(
    "DEMAND",
    conn,
    if_exists="append",
    index=False
)

conn.commit()

print("Database created successfully!")
print(f"Location: {DB_PATH}")

tables = pd.read_sql_query(
    """
    SELECT name
    FROM sqlite_master
    WHERE type = 'table'
    ORDER BY name
    """,
    conn
)

print("\nTables:")
print(tables)

conn.close()