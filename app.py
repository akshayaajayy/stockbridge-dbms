from flask import Flask, render_template, jsonify, request, redirect, session
import sqlite3
import os
import hashlib
import secrets
from functools import wraps
import time
from datetime import datetime

import ai
import llm
import metrics

app = Flask(__name__)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load settings from the .env file next to this file. override=True makes
# the file win over any stale value left in the terminal session.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
except ImportError:
    pass

app.secret_key = os.environ.get(
    "SECRET_KEY", "stockbridge-secret-key-change-this"
)
DATABASE = os.path.join(BASE_DIR, "database", "smartstock.db")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# COST MODEL  (ESTIMATED costs, not actual M5 costs)
# ============================================================
# Purchase = quantity x selling price x 0.70
# Transfer = Rs 2 fixed handling + quantity x Rs 0.80 x distance factor
# Distance factor: 1.0 same state, 2.0 different state.
# Tie goes to TRANSFER.

HANDLING_FEE = 2.00
TRANSPORT_PER_UNIT = 0.80
PURCHASE_FACTOR = 0.70
SAME_STATE_FACTOR = 1.0
DIFFERENT_STATE_FACTOR = 2.0

# CA_1, CA_2 -> California | TX_1, TX_2 -> Texas | WI_1 -> Wisconsin
# The state is the part of the store id before "_", so CA_3, TX_3 etc.
# work without changes.


def state_of(store_id):
    return str(store_id or "").split("_")[0].upper()


def distance_factor(source_store, destination_store):

    if state_of(source_store) == state_of(destination_store):
        return SAME_STATE_FACTOR

    return DIFFERENT_STATE_FACTOR


def estimate_costs(quantity, unit_price, source_store, destination_store):
    """
    Returns (transfer_cost, purchase_cost, recommendation, potential_saving).
    potential_saving = ABS(purchase_cost - transfer_cost).
    """

    qty = float(quantity or 0)
    price = float(unit_price or 0)

    transfer = round(
        HANDLING_FEE
        + qty * TRANSPORT_PER_UNIT
        * distance_factor(source_store, destination_store),
        2
    )

    purchase = round(qty * price * PURCHASE_FACTOR, 2)

    recommendation = "TRANSFER" if transfer <= purchase else "PURCHASE"

    return transfer, purchase, recommendation, round(abs(purchase - transfer), 2)


# ============================================================
# AUTH TABLES
# ============================================================

def init_auth_tables():

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ORGANISATION (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS USER (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
                CHECK(role IN ('admin', 'manager', 'viewer')),
            FOREIGN KEY(org_id) REFERENCES ORGANISATION(id),
            UNIQUE(org_id, username)
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# PASSWORD
# ============================================================

def hash_password(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


# ============================================================
# CSRF
# ============================================================

def get_csrf():

    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)

    return session["csrf"]


def check_csrf():

    token = request.form.get("csrf")

    if token is None:
        data = request.get_json(silent=True)

        if data:
            token = data.get("csrf")

    return token == session.get("csrf")


# ============================================================
# CURRENT USER
# ============================================================

def current_user():

    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    user = conn.execute("""
        SELECT
            u.id,
            u.username,
            u.role,
            u.org_id,
            o.name AS org

        FROM USER u

        JOIN ORGANISATION o
            ON u.org_id = o.id

        WHERE u.id = ?
    """, (user_id,)).fetchone()

    conn.close()

    return user


# ============================================================
# LOGIN REQUIRED
# ============================================================

def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not current_user():
            return redirect("/login")

        return function(*args, **kwargs)

    return wrapper


def api_login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not current_user():

            return jsonify({
                "success": False,
                "message": "Please log in"
            }), 401

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# ROLE REQUIRED
# ============================================================

def role_required(*roles):

    def decorator(function):

        @wraps(function)
        def wrapper(*args, **kwargs):

            user = current_user()

            if not user:

                return jsonify({
                    "success": False,
                    "message": "Please log in"
                }), 401

            if user["role"] not in roles:

                return jsonify({
                    "success": False,
                    "message": "You do not have permission"
                }), 403

            return function(*args, **kwargs)

        return wrapper

    return decorator


# ============================================================
# GLOBAL TEMPLATE DATA
# ============================================================

@app.context_processor
def inject_globals():

    user = current_user()

    return {
        "user": user,
        "org": user["org"] if user else "",
        "csrf": get_csrf()
    }


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":

        return render_template(
            "auth.html",
            mode="register",
            error=None,
            created=False
        )

    if not check_csrf():

        return render_template(
            "auth.html",
            mode="register",
            error="Invalid request. Please try again.",
            created=False
        )

    org = request.form.get("org", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not org or not username or not password:

        return render_template(
            "auth.html",
            mode="register",
            error="All fields are required.",
            created=False
        )

    if len(password) < 8:

        return render_template(
            "auth.html",
            mode="register",
            error="Password must be at least 8 characters.",
            created=False
        )

    conn = get_db()

    try:

        result = conn.execute("""
            INSERT INTO ORGANISATION (name)
            VALUES (?)
        """, (org,))

        org_id = result.lastrowid

        conn.execute("""
            INSERT INTO USER
                (org_id, username, password_hash, role)

            VALUES (?, ?, ?, 'admin')
        """, (
            org_id,
            username,
            hash_password(password)
        ))

        conn.commit()

    except sqlite3.IntegrityError:

        conn.rollback()
        conn.close()

        return render_template(
            "auth.html",
            mode="register",
            error="Organisation already exists.",
            created=False
        )

    conn.close()

    return render_template(
        "auth.html",
        mode="login",
        error=None,
        created=True
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":

        return render_template(
            "auth.html",
            mode="login",
            error=None,
            created=False
        )

    if not check_csrf():

        return render_template(
            "auth.html",
            mode="login",
            error="Invalid request. Please try again.",
            created=False
        )

    org = request.form.get("org", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    conn = get_db()

    user = conn.execute("""
        SELECT
            u.id,
            u.password_hash

        FROM USER u

        JOIN ORGANISATION o
            ON u.org_id = o.id

        WHERE o.name = ?
          AND u.username = ?
    """, (
        org,
        username
    )).fetchone()

    conn.close()

    if user is None:

        return render_template(
            "auth.html",
            mode="login",
            error="Invalid organisation, username or password.",
            created=False
        )

    if user["password_hash"] != hash_password(password):

        return render_template(
            "auth.html",
            mode="login",
            error="Invalid organisation, username or password.",
            created=False
        )

    session.clear()

    session["user_id"] = user["id"]
    session["csrf"] = secrets.token_hex(16)

    return redirect("/")


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# ============================================================
# PAGES
# ============================================================

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/inventory")
@login_required
def inventory_page():
    return render_template("inventory.html")


@app.route("/aging")
@login_required
def aging_page():
    return render_template("aging.html")


@app.route("/transfers")
@login_required
def transfers_page():
    return render_template("transfers.html")


@app.route("/branches")
@login_required
def branches_page():
    return render_template("branches.html")


@app.route("/reports")
@login_required
def reports_page():
    return render_template("reports.html")


# ============================================================
# TEAM PAGE
# ============================================================

@app.route("/team")
@login_required
def team_page():

    user = current_user()

    if user["role"] != "admin":
        return "Access denied", 403

    return render_template("team.html")


# ============================================================
# DASHBOARD API
# ============================================================

@app.route("/api/dashboard")
@api_login_required
def dashboard_api():

    conn = get_db()

    # total_saving     = ACTUAL savings from approved TRANSFERs only.
    # potential_saving = what pending recommendations could still save.
    result = conn.execute("""
        SELECT
            (SELECT COALESCE(SUM(current_stock), 0)
             FROM INVENTORY) AS total_stock,

            (SELECT COUNT(*)
             FROM INVENTORY
             WHERE current_stock < 5) AS low_stock,

            (SELECT COUNT(*)
             FROM INVENTORY_AGE
             WHERE age_status = 'Aging') AS aging_stock,

            (SELECT COUNT(*)
             FROM TRANSFER
             WHERE status = 'PENDING') AS pending_transfers,

            (SELECT COUNT(*)
             FROM TRANSFER
             WHERE status = 'APPROVED'
               AND recommendation = 'TRANSFER') AS approved_transfers,

            (SELECT COUNT(*)
             FROM TRANSFER
             WHERE status = 'APPROVED'
               AND recommendation = 'PURCHASE') AS approved_purchases,

            (SELECT COALESCE(SUM(saving), 0)
             FROM TRANSFER
             WHERE status = 'APPROVED'
               AND recommendation = 'TRANSFER') AS total_saving,

            (SELECT COALESCE(SUM(potential_saving), 0)
             FROM TRANSFER
             WHERE status = 'PENDING') AS potential_saving
    """).fetchone()

    conn.close()

    return jsonify({
        "total_stock": result["total_stock"],
        "low_stock": result["low_stock"],
        "aging_stock": result["aging_stock"],
        "pending_transfers": result["pending_transfers"],
        "approved_transfers": result["approved_transfers"],
        "approved_purchases": result["approved_purchases"],
        "total_saving": round(result["total_saving"], 2),
        "potential_saving": round(result["potential_saving"], 2)
    })


# ============================================================
# INVENTORY API
# ============================================================

@app.route("/api/inventory")
@api_login_required
def inventory_api():

    conn = get_db()

    rows = conn.execute("""
        SELECT
            i.inventory_id,
            i.item_id,
            i.store_id,
            i.sales_last_14_days,
            i.current_stock,

            d.avg_daily_demand,

            ROUND(
                d.avg_daily_demand * 14, 2
            ) AS expected_14_day_demand,

            ROUND(
                i.current_stock -
                (d.avg_daily_demand * 14), 2
            ) AS stock_balance,

            CASE
                WHEN i.current_stock <
                     (d.avg_daily_demand * 14)
                    THEN 'SHORTAGE'

                WHEN i.current_stock >
                     (d.avg_daily_demand * 14)
                    THEN 'EXCESS'

                ELSE 'BALANCED'
            END AS stock_status,

            ia.inventory_age_days,
            ia.age_status

        FROM INVENTORY i

        LEFT JOIN DEMAND d
            ON i.item_id = d.item_id
            AND i.store_id = d.store_id

        LEFT JOIN INVENTORY_AGE ia
            ON i.inventory_id = ia.inventory_id

        ORDER BY i.item_id, i.store_id
    """).fetchall()

    conn.close()

    return jsonify([dict(row) for row in rows])


# ============================================================
# AGING API
# ============================================================

@app.route("/api/aging")
@api_login_required
def aging_api():

    conn = get_db()

    rows = conn.execute("""
        SELECT
            i.item_id,
            i.store_id,
            i.current_stock,
            ia.received_date,
            ia.inventory_age_days,
            ia.age_status

        FROM INVENTORY i

        JOIN INVENTORY_AGE ia
            ON i.inventory_id = ia.inventory_id

        ORDER BY
            ia.inventory_age_days DESC
    """).fetchall()

    conn.close()

    return jsonify([dict(row) for row in rows])


# ============================================================
# TRANSFERS API
# ============================================================

@app.route("/api/transfers")
@api_login_required
def transfers_api():
    """
    Optional query string:
      method=all|transfer|purchase   filter by recommendation
      status=all|pending|approved|rejected
      q=text                         matches item, branch or transfer id
    """

    method_filter = request.args.get("method", "all").strip().upper()
    status_filter = request.args.get("status", "all").strip().upper()
    search = request.args.get("q", "").strip().lstrip("#").strip()

    where = []
    params = []

    if method_filter in ("TRANSFER", "PURCHASE"):
        where.append("recommendation = ?")
        params.append(method_filter)

    if status_filter in ("PENDING", "APPROVED", "REJECTED"):
        where.append("status = ?")
        params.append(status_filter)

    if search:
        like = f"%{search}%"
        where.append("""(
            CAST(transfer_id AS TEXT) = ?
            OR item_id LIKE ?
            OR source_store_id LIKE ?
            OR destination_store_id LIKE ?
        )""")
        params += [search, like, like, like]

    conn = get_db()

    rows = conn.execute(f"""
        SELECT
            transfer_id,
            item_id,
            source_store_id,
            destination_store_id,
            quantity,
            transfer_cost,
            purchase_cost,
            saving,
            potential_saving,
            status,
            transfer_date,
            recommendation,
            decided_by,
            decided_at

        FROM TRANSFER

        {"WHERE " + " AND ".join(where) if where else ""}

        ORDER BY
            CASE
                WHEN status = 'PENDING' THEN 1
                WHEN status = 'APPROVED' THEN 2
                ELSE 3
            END,
            COALESCE(potential_saving, 0) DESC,
            transfer_id
    """, params).fetchall()

    conn.close()

    result = []

    for row in rows:

        r = dict(row)

        t_cost = float(r["transfer_cost"] or 0)
        p_cost = float(r["purchase_cost"] or 0)

        method = r["recommendation"] or (
            "PURCHASE" if p_cost < t_cost else "TRANSFER"
        )

        r["recommendation"] = method

        if method == "PURCHASE":
            r["recommended_cost"] = p_cost
            r["alternative_cost"] = t_cost
        else:
            r["recommended_cost"] = t_cost
            r["alternative_cost"] = p_cost

        r["potential_saving"] = round(
            float(r["potential_saving"] or abs(p_cost - t_cost)), 2
        )

        # saving = ACTUAL (approved TRANSFER only). For a pending row,
        # show potential_saving instead.
        r["display_saving"] = (
            round(float(r["saving"] or 0), 2)
            if r["status"] == "APPROVED"
            else r["potential_saving"]
        )

        result.append(r)

    return jsonify(result)


# ============================================================
# APPROVE TRANSFER
# ============================================================

@app.route(
    "/api/transfers/<int:transfer_id>/approve",
    methods=["POST"]
)
@role_required("admin", "manager")
def approve_transfer(transfer_id):

    if not check_csrf():

        return jsonify({
            "success": False,
            "message": "Invalid CSRF token"
        }), 403

    conn = get_db()

    try:

        conn.execute("BEGIN IMMEDIATE")

        transfer = conn.execute("""
            SELECT
                transfer_id,
                item_id,
                source_store_id,
                destination_store_id,
                quantity,
                transfer_cost,
                purchase_cost,
                recommendation,
                status

            FROM TRANSFER

            WHERE transfer_id = ?
        """, (transfer_id,)).fetchone()

        if transfer is None:

            conn.rollback()

            return jsonify({
                "success": False,
                "message": "Transfer not found"
            }), 404

        if transfer["status"] != "PENDING":

            conn.rollback()

            return jsonify({
                "success": False,
                "message": "Transfer is not pending"
            }), 400

        # TRANSFER: move stock from source branch to destination.
        # PURCHASE: buy new stock for the destination only.
        method = transfer["recommendation"] or (
            "PURCHASE"
            if (transfer["purchase_cost"] or 0)
               < (transfer["transfer_cost"] or 0)
            else "TRANSFER"
        )

        source = conn.execute("""
            SELECT
                inventory_id,
                current_stock

            FROM INVENTORY

            WHERE item_id = ?
              AND store_id = ?
        """, (
            transfer["item_id"],
            transfer["source_store_id"]
        )).fetchone()

        destination = conn.execute("""
            SELECT
                inventory_id,
                current_stock

            FROM INVENTORY

            WHERE item_id = ?
              AND store_id = ?
        """, (
            transfer["item_id"],
            transfer["destination_store_id"]
        )).fetchone()

        if method == "TRANSFER" and source is None:

            conn.rollback()

            return jsonify({
                "success": False,
                "message": "Source inventory not found"
            }), 400

        if destination is None:

            conn.rollback()

            return jsonify({
                "success": False,
                "message": "Destination inventory not found"
            }), 400

        if method == "TRANSFER":

            if source["current_stock"] < transfer["quantity"]:

                conn.rollback()

                return jsonify({
                    "success": False,
                    "message": "Insufficient source stock"
                }), 400

            result = conn.execute("""
                UPDATE INVENTORY

                SET current_stock =
                    current_stock - ?

                WHERE inventory_id = ?
                  AND current_stock >= ?
            """, (
                transfer["quantity"],
                source["inventory_id"],
                transfer["quantity"]
            ))

            if result.rowcount == 0:

                conn.rollback()

                return jsonify({
                    "success": False,
                    "message": "Source stock changed. Transfer cancelled."
                }), 400

        conn.execute("""
            UPDATE INVENTORY

            SET current_stock =
                current_stock + ?

            WHERE inventory_id = ?
        """, (
            transfer["quantity"],
            destination["inventory_id"]
        ))

        # Actual saving exists only when stock is really transferred.
        # An approved PURCHASE is not a saving.
        if method == "TRANSFER":
            actual_saving = round(
                float(transfer["purchase_cost"] or 0)
                - float(transfer["transfer_cost"] or 0), 2
            )
        else:
            actual_saving = 0.0

        # Stock changes and the status/audit update are one transaction:
        # either everything is saved or nothing is.
        conn.execute("""
            UPDATE TRANSFER

            SET status = 'APPROVED',
                recommendation = ?,
                saving = ?,
                decided_by = ?,
                decided_at = datetime('now', 'localtime')

            WHERE transfer_id = ?
              AND status = 'PENDING'
        """, (
            method,
            actual_saving,
            current_user()["username"],
            transfer_id
        ))

        conn.commit()

        if method == "PURCHASE":
            message = (
                f"Purchase approved. {transfer['quantity']} units "
                f"added to {transfer['destination_store_id']}."
            )
        else:
            message = (
                f"Transfer approved. {transfer['quantity']} units moved "
                f"from {transfer['source_store_id']} to "
                f"{transfer['destination_store_id']}."
            )

        return jsonify({
            "success": True,
            "message": message
        })

    except Exception as e:

        conn.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        conn.close()


# ============================================================
# REJECT TRANSFER
# ============================================================

@app.route(
    "/api/transfers/<int:transfer_id>/reject",
    methods=["POST"]
)
@role_required("admin", "manager")
def reject_transfer(transfer_id):

    if not check_csrf():

        return jsonify({
            "success": False,
            "message": "Invalid CSRF token"
        }), 403

    conn = get_db()

    try:

        result = conn.execute("""
            UPDATE TRANSFER

            SET status = 'REJECTED',
                decided_by = ?,
                decided_at = datetime('now', 'localtime')

            WHERE transfer_id = ?
              AND status = 'PENDING'
        """, (current_user()["username"], transfer_id))

        conn.commit()

        if result.rowcount == 0:

            return jsonify({
                "success": False,
                "message": "Transfer not found or already processed"
            }), 400

        return jsonify({
            "success": True,
            "message": "Transfer rejected"
        })

    except Exception as e:

        conn.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        conn.close()


# ============================================================
# EXPLAINABLE AI
# ============================================================
# Every recommendation is produced by a transparent rule (the cheaper
# option wins), so the explanation below is the real reasoning, not a
# guess: it re-reads the same costs, demand, stock and ageing data and
# reports what supports the decision and what argues against it.

def _stock_position(conn, item_id, store_id):

    row = conn.execute("""
        SELECT
            i.inventory_id,
            i.current_stock,
            d.avg_daily_demand,
            ia.inventory_age_days,
            ia.age_status

        FROM INVENTORY i

        LEFT JOIN DEMAND d
            ON d.item_id = i.item_id
            AND d.store_id = i.store_id

        LEFT JOIN INVENTORY_AGE ia
            ON ia.inventory_id = i.inventory_id

        WHERE i.item_id = ?
          AND i.store_id = ?
    """, (item_id, store_id)).fetchone()

    return dict(row) if row else None


def _money(value):
    return "₹{:,.2f}".format(value)


def build_explanation(conn, t):

    t_cost = float(t["transfer_cost"] or 0)
    p_cost = float(t["purchase_cost"] or 0)
    qty = int(t["quantity"] or 0)

    method = t["recommendation"] or (
        "PURCHASE" if p_cost < t_cost else "TRANSFER"
    )

    chosen, other = (
        (p_cost, t_cost) if method == "PURCHASE" else (t_cost, p_cost)
    )

    gap = other - chosen
    gap_pct = (gap / other * 100) if other > 0 else 0.0

    chosen_name = "Purchase" if method == "PURCHASE" else "Transfer"
    other_name = "transfer" if method == "PURCHASE" else "purchase"

    dest_id = t["destination_store_id"]
    src_id = t["source_store_id"]

    dest = _stock_position(conn, t["item_id"], dest_id)
    src = _stock_position(conn, t["item_id"], src_id)

    # Approved rows have already changed stock; rewind to what the
    # decision-maker saw.
    if t["status"] == "APPROVED":
        if dest:
            dest["current_stock"] -= qty
        if src and method == "TRANSFER":
            src["current_stock"] += qty

    factors = []

    def add(kind, title, detail):
        factors.append({"kind": kind, "title": title, "detail": detail})

    # --- 1. Cost -------------------------------------------------
    if gap > 0:
        add(
            "support",
            "Lower cost",
            f"{chosen_name} is estimated at {_money(chosen)} against "
            f"{_money(other)} for the {other_name} option, which is "
            f"{_money(gap)} ({gap_pct:.1f}%) cheaper."
        )
    else:
        add(
            "info",
            "Costs are equal",
            "Both options cost the same, so the transfer is preferred "
            "because it avoids raising a new purchase order."
        )

    # --- 2. Is the destination really short? -----------------------
    shortfall = None

    if dest and dest["avg_daily_demand"]:

        demand = float(dest["avg_daily_demand"])
        need = demand * 14
        stock = dest["current_stock"]
        shortfall = max(need - stock, 0)
        cover = stock / demand if demand > 0 else 0

        if shortfall > 0:
            fill = qty / shortfall * 100
            add(
                "support",
                "Destination is short",
                f"{dest_id} holds {stock} units but is expected to sell "
                f"about {need:.0f} in 14 days (roughly {cover:.1f} days "
                f"of cover). The {qty} units requested fill "
                f"{min(fill, 999):.0f}% of the {shortfall:.0f}-unit gap."
            )

            if qty > shortfall * 1.25:
                add(
                    "concern",
                    "May overstock the destination",
                    f"The quantity is {qty - shortfall:.0f} units more "
                    f"than the shortfall of {shortfall:.0f}."
                )
        else:
            add(
                "concern",
                "Destination is not short",
                f"{dest_id} holds {stock} units against about {need:.0f} "
                f"expected demand over 14 days, so the need is weak."
            )
    else:
        add(
            "info",
            "No demand data for the destination",
            f"There is no demand history for this item at {dest_id}, "
            f"so the size of the shortage could not be checked."
        )

    # --- 3. Source branch ------------------------------------------
    if method == "TRANSFER":

        if src is None:
            add(
                "concern",
                "Source inventory not found",
                f"No stock record exists for this item at {src_id}."
            )

        else:

            if src["current_stock"] < qty:
                add(
                    "concern",
                    "Source cannot cover this",
                    f"{src_id} holds {src['current_stock']} units but "
                    f"{qty} are needed."
                )

            elif src["avg_daily_demand"]:

                s_need = float(src["avg_daily_demand"]) * 14
                after = src["current_stock"] - qty

                if after >= s_need:
                    add(
                        "support",
                        "Source stays covered",
                        f"After sending {qty}, {src_id} keeps {after} units "
                        f"against about {s_need:.0f} needed for its own "
                        f"14-day demand."
                    )
                else:
                    add(
                        "concern",
                        "Source would run short",
                        f"After sending {qty}, {src_id} keeps only {after} "
                        f"units, below the {s_need:.0f} it needs for its "
                        f"own 14-day demand."
                    )

            else:
                add(
                    "info",
                    "No demand data for the source",
                    f"{src_id} has no demand history, so the effect on it "
                    f"could not be checked."
                )

            if src.get("age_status") == "Aging":
                add(
                    "support",
                    "Moves aging stock",
                    f"Stock at {src_id} has been held "
                    f"{src['inventory_age_days']} days, so moving it first "
                    f"reduces the risk of it going unsold."
                )

    else:

        if src and src["avg_daily_demand"]:

            spare = src["current_stock"] - float(src["avg_daily_demand"]) * 14

            if spare < qty:
                add(
                    "support",
                    "Source has little to spare",
                    f"{src_id} has about {max(spare, 0):.0f} spare units "
                    f"beyond its own 14-day demand, fewer than the {qty} "
                    f"needed, so a transfer would leave it short."
                )
            else:
                add(
                    "info",
                    "A transfer was possible",
                    f"{src_id} could spare the stock, but buying is still "
                    f"cheaper."
                )

    # --- Confidence ------------------------------------------------
    concerns = sum(1 for f in factors if f["kind"] == "concern")

    score = 2 if gap_pct >= 20 else 1 if gap_pct >= 5 else 0
    score -= concerns

    if score >= 2:
        confidence = "High"
    elif score == 1:
        confidence = "Medium"
    else:
        confidence = "Low"

    if concerns:
        confidence_note = (
            f"{concerns} concern{'s' if concerns != 1 else ''} to check "
            f"before approving."
        )
    elif gap_pct < 5:
        confidence_note = "The cost gap is small, so the choice is close."
    else:
        confidence_note = "Clear cost advantage and no concerns found."

    # --- What would change the answer ------------------------------
    if method == "TRANSFER":
        flip_note = (
            f"A higher transport cost (above {_money(p_cost)}, currently "
            f"{_money(t_cost)}) or a lower purchase price (below "
            f"{_money(t_cost)}) could make PURCHASE cheaper."
        )
    else:
        flip_note = (
            f"A lower transport cost (below {_money(p_cost)}, currently "
            f"{_money(t_cost)}) or a higher purchase price (above "
            f"{_money(t_cost)}) could make TRANSFER cheaper."
        )

    # --- Shortage / excess figures (also used by the AI wording) ----
    destination_shortage = round(shortfall, 1) if shortfall else 0

    source_excess = None

    if src and src["avg_daily_demand"]:
        source_excess = round(max(
            src["current_stock"] - float(src["avg_daily_demand"]) * 14, 0
        ), 1)

    # --- Rule-based reasons (short bullet list for the Why? panel) ---
    reasons = []

    if shortfall is not None and shortfall > 0:
        reasons.append("Destination has a stock shortage")

    if method == "TRANSFER":
        if source_excess:
            reasons.append("Source has excess stock")
        if gap > 0:
            reasons.append("Transfer is cheaper than purchasing")
        else:
            reasons.append("Transfer and purchase cost the same")
        reasons.append(
            "Inventory can be redistributed between branches"
        )
    else:
        if gap > 0:
            reasons.append("Purchase is cheaper than transferring")
        if source_excess is not None and source_excess < qty:
            reasons.append("Source branch has no spare stock to transfer")

    # --- How the estimated costs were built ------------------------
    cost_basis = None
    unit_price = t.get("unit_price")

    if unit_price is not None:

        est_t, est_p, _, _ = estimate_costs(qty, unit_price, src_id, dest_id)

        if abs(est_t - t_cost) < 0.01 and abs(est_p - p_cost) < 0.01:

            factor = distance_factor(src_id, dest_id)
            region = (
                "same state" if factor == SAME_STATE_FACTOR
                else "different state"
            )

            cost_basis = {
                "transfer": (
                    f"{_money(HANDLING_FEE)} handling + {qty} units x "
                    f"{_money(TRANSPORT_PER_UNIT)} x {factor:g} "
                    f"({region})"
                ),
                "purchase": (
                    f"{qty} units x {_money(float(unit_price))} selling "
                    f"price x {PURCHASE_FACTOR:g}"
                )
            }

    # --- Plain-English summary -------------------------------------
    action = (
        f"Buy {qty} new units for {dest_id}"
        if method == "PURCHASE"
        else f"Move {qty} units from {src_id} to {dest_id}"
    )

    if gap > 0:
        summary = (
            f"{action}: it is {gap_pct:.1f}% cheaper than the "
            f"{other_name} option"
        )
    else:
        summary = f"{action}: it costs the same as the {other_name} option"

    if shortfall is not None and shortfall > 0:
        summary += f", and {dest_id} is short by about {shortfall:.0f} units."
    else:
        summary += "."

    return {
        "transfer_id": t["transfer_id"],
        "item_id": t["item_id"],
        "status": t["status"],
        "method": method,
        "headline": action,
        "summary": summary,
        "confidence": confidence,
        "confidence_note": confidence_note,
        "estimated_saving": round(gap, 2),
        "destination_shortage": destination_shortage,
        "source_excess": source_excess,
        "reasons": reasons,
        "cost_basis": cost_basis,
        "cost_note": (
            "Costs are estimates from the StockBridge cost model, "
            "not actual M5 costs."
        ),
        "costs": {
            "transfer": round(t_cost, 2),
            "purchase": round(p_cost, 2),
            "gap": round(gap, 2),
            "gap_pct": round(gap_pct, 1)
        },
        "factors": factors,
        "flip_note": flip_note,
        "rule": (
            "Rule: compare the estimated cost of moving stock from another "
            "branch with the estimated cost of buying new stock, and "
            "recommend the cheaper one (a tie goes to TRANSFER). Stock "
            "levels, demand and ageing are then used to check the choice "
            "and flag concerns."
        )
    }


@app.route("/api/transfers/<int:transfer_id>/explain")
@api_login_required
def explain_transfer(transfer_id):

    conn = get_db()

    try:

        row = conn.execute("""
            SELECT * FROM TRANSFER WHERE transfer_id = ?
        """, (transfer_id,)).fetchone()

        if row is None:

            return jsonify({
                "success": False,
                "message": "Transfer not found"
            }), 404

        return jsonify(build_explanation(conn, dict(row)))

    finally:

        conn.close()


# ============================================================
# AI ASSISTANT
# ============================================================

AI_CALLS = {}
AI_LIMIT = 20          # requests per user
AI_WINDOW = 60         # seconds
AI_ROLES = ("admin", "manager")


def ai_allowed(user_id):
    """Simple per-user rate limit so the AI bill can't run away."""

    now = time.time()

    recent = [t for t in AI_CALLS.get(user_id, []) if now - t < AI_WINDOW]

    if len(recent) >= AI_LIMIT:
        AI_CALLS[user_id] = recent
        return False

    recent.append(now)
    AI_CALLS[user_id] = recent

    return True


def ai_guard():
    """Common checks for AI POST routes. Returns (user, error_response)."""

    user = current_user()

    # AI assistant: admins and managers only (viewers are read-only).
    if user["role"] not in AI_ROLES:
        return None, (jsonify({
            "success": False,
            "message": "The AI assistant is available to admins and "
                       "managers."
        }), 403)

    if not check_csrf():
        return None, (jsonify({
            "success": False,
            "message": "Invalid CSRF token"
        }), 403)

    if not llm.is_configured():
        return None, (jsonify({
            "success": False,
            "message": llm.problem() or "AI is not set up yet."
        }), 503)

    if not ai_allowed(user["id"]):
        return None, (jsonify({
            "success": False,
            "message": "Too many AI requests. Please wait a minute."
        }), 429)

    return user, None


def ai_error(error):
    """Turn an LLM failure into a clean JSON response."""

    if isinstance(error, llm.LLMNotConfigured):
        status = 503
    else:
        status = 502

    return jsonify({"success": False, "message": str(error)}), status


@app.route("/assistant")
@login_required
def assistant_page():
    return render_template("assistant.html")


@app.route("/api/ai/status")
@api_login_required
def ai_status():

    cfg = llm.config()

    return jsonify({
        "allowed": current_user()["role"] in AI_ROLES,
        "configured": cfg is not None,
        "problem": llm.problem(),
        "provider": cfg["provider"] if cfg else None,
        "model": cfg["model"] if cfg else None,
        "backup": (llm.fallback_config() or {}).get("model"),
        "sends_rows": os.environ.get("LLM_SEND_ROWS", "1") != "0"
    })

@app.route("/api/ai/chat", methods=["POST"])
@api_login_required
def ai_chat():

    user, problem = ai_guard()

    if problem:
        return problem

    data = request.get_json(silent=True) or {}
    question = str(data.get("question", ""))
    started = time.perf_counter()

    def ms():
        return int((time.perf_counter() - started) * 1000)

    try:

        result = ai.answer_question(
            DATABASE,
            question,
            data.get("history", [])
        )

    except ai.SQLGuardError as error:

        metrics.log_ai(user["id"], question, None, "rejected", ms())

        return jsonify({"success": False, "message": str(error)}), 400

    except llm.LLMError as error:

        metrics.log_ai(user["id"], question, None, "error", ms())

        return ai_error(error)

    if result.get("blocked"):
        outcome = "blocked"
    elif result.get("error"):
        outcome = "failed_query"
    elif not result.get("sql"):
        outcome = "no_sql"
    else:
        outcome = "answered"

    result["success"] = True
    result["log_id"] = metrics.log_ai(
        user["id"], question, result, outcome, ms()
    )

    return jsonify(result)


@app.route("/api/ai/rate", methods=["POST"])
@api_login_required
def ai_rate():

    user = current_user()
    data = request.get_json(silent=True) or {}

    if user["role"] not in AI_ROLES or not check_csrf():
        return jsonify({"success": False, "message": "Not allowed"}), 403

    try:
        ok = metrics.rate(int(data.get("log_id")), user["id"],
                          int(data.get("rating")))
    except (TypeError, ValueError):
        ok = False

    return jsonify({"success": ok})


# ============================================================
# ANALYTICS (performance metrics)
# ============================================================

ANALYTICS_ROLES = ("admin", "manager")


@app.route("/analytics")
@login_required
def analytics_page():

    if current_user()["role"] not in ANALYTICS_ROLES:
        return "Access denied", 403

    return render_template("analytics.html")


@app.route("/api/analytics")
@api_login_required
def analytics_api():

    user = current_user()

    if user["role"] not in ANALYTICS_ROLES:
        return jsonify({"success": False,
                        "message": "You do not have permission"}), 403

    conn = get_db()

    try:
        data = metrics.business(conn)
    finally:
        conn.close()

    data["baseline"] = metrics.get_baseline()
    data["ai"] = metrics.ai_summary() if user["role"] == "admin" else None
    data["scorecard"] = metrics.scorecard(data, data["ai"])
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")

    return jsonify(data)


@app.route("/api/analytics/export.csv")
@api_login_required
@role_required("admin")
def analytics_export():
    """AI usage log as CSV, for audits or a report appendix."""

    import csv
    import io

    conn = sqlite3.connect(metrics.DB)

    try:
        rows = conn.execute(
            "SELECT id, ts, user_id, question, outcome, row_count, "
            "latency_ms, rating, sql_text FROM AI_LOG ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "time", "user_id", "question", "outcome",
                     "rows", "latency_ms", "rating", "sql"])
    writer.writerows(rows)

    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ai_log.csv"})


@app.route("/api/analytics/baseline", methods=["POST"])
@api_login_required
@role_required("admin")
def analytics_baseline():

    if not check_csrf():
        return jsonify({"success": False, "message": "Invalid CSRF token"}), 403

    conn = get_db()

    try:
        metrics.save_baseline(metrics.business(conn)["health"])
    finally:
        conn.close()

    return jsonify({"success": True, "message": "Baseline saved"})


@app.route("/api/transfers/<int:transfer_id>/ai-explain", methods=["POST"])
@api_login_required
def ai_explain_transfer(transfer_id):

    user, problem = ai_guard()

    if problem:
        return problem

    conn = get_db()

    try:

        row = conn.execute("""
            SELECT * FROM TRANSFER WHERE transfer_id = ?
        """, (transfer_id,)).fetchone()

        if row is None:

            return jsonify({
                "success": False,
                "message": "Transfer not found"
            }), 404

        expl = build_explanation(conn, dict(row))

    finally:

        conn.close()

    try:

        text = ai.explain_recommendation(expl)

    except llm.LLMError as error:

        return ai_error(error)

    return jsonify({"success": True, "text": text})


@app.route("/api/ai/briefing", methods=["POST"])
@api_login_required
def ai_briefing():

    user, problem = ai_guard()

    if problem:
        return problem

    conn = get_db()

    try:

        # Every number comes from SQL; the AI only words the briefing.
        stats = dict(conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM INVENTORY
                 WHERE current_stock < 5) AS low_stock_items,

                (SELECT COUNT(DISTINCT store_id) FROM INVENTORY
                 WHERE current_stock < 5) AS branches_with_low_stock,

                (SELECT COUNT(*) FROM INVENTORY_AGE
                 WHERE age_status = 'Aging') AS aging_items,

                (SELECT COUNT(*) FROM TRANSFER
                 WHERE status = 'PENDING') AS pending_recommendations,

                (SELECT COUNT(*) FROM TRANSFER
                 WHERE status = 'PENDING'
                   AND recommendation = 'TRANSFER') AS pending_transfers,

                (SELECT COUNT(*) FROM TRANSFER
                 WHERE status = 'PENDING'
                   AND recommendation = 'PURCHASE') AS pending_purchases,

                (SELECT COALESCE(SUM(potential_saving), 0) FROM TRANSFER
                 WHERE status = 'PENDING') AS total_potential_saving
        """).fetchone())

        stats["total_potential_saving"] = round(
            float(stats["total_potential_saving"]), 2
        )

        stats["items_needing_attention"] = (
            stats["low_stock_items"]
            + stats["aging_items"]
            + stats["pending_recommendations"]
        )

        rows = conn.execute("""
            SELECT *

            FROM TRANSFER

            WHERE status = 'PENDING'

            ORDER BY COALESCE(potential_saving, 0) DESC

            LIMIT 10
        """).fetchall()

        top_pending = []

        for row in rows:

            expl = build_explanation(conn, dict(row))

            top_pending.append({
                "id": row["transfer_id"],
                "item": row["item_id"],
                "method": expl["method"],
                "route": (
                    f"{row['source_store_id']} -> "
                    f"{row['destination_store_id']}"
                    if expl["method"] == "TRANSFER"
                    else f"Supplier -> {row['destination_store_id']}"
                ),
                "quantity": row["quantity"],
                "potential_saving": round(
                    float(row["potential_saving"] or 0), 2
                ),
                "confidence": expl["confidence"],
                "concerns": [
                    f["title"] + ": " + f["detail"]
                    for f in expl["factors"]
                    if f["kind"] == "concern"
                ]
            })

    finally:

        conn.close()

    try:

        text = ai.briefing({"summary": stats, "top_pending": top_pending})

    except llm.LLMError as error:

        return ai_error(error)

    return jsonify({
        "success": True,
        "text": text,
        "count": stats["items_needing_attention"],
        "stats": stats
    })


# ============================================================
# BRANCHES API
# ============================================================

@app.route("/api/branches")
@api_login_required
def branches_api():

    conn = get_db()

    rows = conn.execute("""
        SELECT
            b.store_id,

            COALESCE(
                SUM(i.current_stock), 0
            ) AS total_stock,

            COUNT(i.inventory_id)
                AS inventory_items

        FROM BRANCH b

        LEFT JOIN INVENTORY i
            ON b.store_id = i.store_id

        GROUP BY b.store_id

        ORDER BY b.store_id
    """).fetchall()

    conn.close()

    return jsonify([dict(row) for row in rows])


# ============================================================
# REPORTS API
# ============================================================

@app.route("/api/reports")
@api_login_required
def reports_api():
    """All figures come from SQL aggregation over TRANSFER."""

    conn = get_db()

    s = dict(conn.execute("""
        SELECT
            COUNT(*) AS total_recommendations,

            COALESCE(SUM(status = 'PENDING'), 0)  AS pending,
            COALESCE(SUM(status = 'APPROVED'), 0) AS approved,
            COALESCE(SUM(status = 'REJECTED'), 0) AS rejected,

            COALESCE(SUM(status = 'APPROVED'
                         AND recommendation = 'TRANSFER'), 0)
                AS approved_transfers,
            COALESCE(SUM(status = 'APPROVED'
                         AND recommendation = 'PURCHASE'), 0)
                AS approved_purchases,
            COALESCE(SUM(status = 'PENDING'
                         AND recommendation = 'TRANSFER'), 0)
                AS pending_transfers,
            COALESCE(SUM(status = 'PENDING'
                         AND recommendation = 'PURCHASE'), 0)
                AS pending_purchases,

            COALESCE(SUM(CASE WHEN status = 'APPROVED'
                               AND recommendation = 'TRANSFER'
                              THEN quantity END), 0) AS units_moved,
            COALESCE(SUM(CASE WHEN status = 'APPROVED'
                               AND recommendation = 'PURCHASE'
                              THEN quantity END), 0) AS units_purchased,

            -- money actually committed by approved decisions
            COALESCE(SUM(CASE WHEN status = 'APPROVED'
                              THEN CASE WHEN recommendation = 'PURCHASE'
                                        THEN purchase_cost
                                        ELSE transfer_cost END
                         END), 0) AS actual_spend,

            COALESCE(SUM(CASE WHEN status = 'APPROVED'
                              THEN CASE WHEN recommendation = 'PURCHASE'
                                        THEN transfer_cost
                                        ELSE purchase_cost END
                         END), 0) AS alternative_cost,

            -- actual saving: approved TRANSFERs only
            COALESCE(SUM(CASE WHEN status = 'APPROVED'
                               AND recommendation = 'TRANSFER'
                              THEN saving END), 0) AS saving,

            -- potential saving: pending recommendations
            COALESCE(SUM(CASE WHEN status = 'PENDING'
                              THEN potential_saving END), 0)
                AS potential_saving,

            COALESCE(SUM(transfer_cost), 0) AS total_transfer_cost,
            COALESCE(SUM(purchase_cost), 0) AS total_purchase_cost

        FROM TRANSFER
    """).fetchone())

    decisions = conn.execute("""
        SELECT
            transfer_id, item_id, source_store_id,
            destination_store_id, quantity, transfer_cost,
            purchase_cost, saving, status, recommendation,
            decided_by, decided_at

        FROM TRANSFER

        WHERE status IN ('APPROVED', 'REJECTED')

        ORDER BY COALESCE(decided_at, '') DESC, transfer_id DESC

        LIMIT 15
    """).fetchall()

    conn.close()

    for key in ("actual_spend", "alternative_cost", "saving",
                "potential_saving", "total_transfer_cost",
                "total_purchase_cost"):
        s[key] = round(float(s[key]), 2)

    recent_decisions = []

    for r in decisions:

        method = r["recommendation"] or "TRANSFER"
        is_purchase = method == "PURCHASE"

        recent_decisions.append({
            "transfer_id": r["transfer_id"],
            "item_id": r["item_id"],
            "from": "Supplier" if is_purchase else r["source_store_id"],
            "to": r["destination_store_id"],
            "quantity": r["quantity"],
            "method": method,
            "status": r["status"],
            "cost": round(float(
                (r["purchase_cost"] if is_purchase else r["transfer_cost"])
                or 0), 2),
            "saving": (
                round(float(r["saving"] or 0), 2)
                if r["status"] == "APPROVED" and not is_purchase else 0.0
            ),
            "decided_by": r["decided_by"],
            "decided_at": r["decided_at"]
        })

    s["recent_decisions"] = recent_decisions

    # Same shape as before: approved decisions only.
    s["recent"] = [
        d for d in recent_decisions if d["status"] == "APPROVED"
    ]

    return jsonify(s)


# ============================================================
# TEAM API
# ============================================================

@app.route("/api/team")
@api_login_required
def team_api():

    user = current_user()

    if user["role"] != "admin":

        return jsonify({
            "success": False,
            "message": "Only admins can manage the team"
        }), 403

    conn = get_db()

    users = conn.execute("""
        SELECT
            id,
            username,
            role

        FROM USER

        WHERE org_id = ?

        ORDER BY username
    """, (user["org_id"],)).fetchall()

    conn.close()

    result = []

    for u in users:

        result.append({
            "id": u["id"],
            "username": u["username"],
            "role": u["role"],
            "is_you": u["id"] == user["id"]
        })

    return jsonify(result)


# ============================================================
# ADD TEAM MEMBER
# ============================================================

@app.route("/api/team", methods=["POST"])
@api_login_required
def add_team_member():

    user = current_user()

    if user["role"] != "admin":

        return jsonify({
            "success": False,
            "message": "Only admins can add team members"
        }), 403

    if not check_csrf():

        return jsonify({
            "success": False,
            "message": "Invalid CSRF token"
        }), 403

    data = request.get_json(silent=True) or {}

    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "")

    if not username or not password or not role:

        return jsonify({
            "success": False,
            "message": "All fields are required"
        }), 400

    if len(password) < 8:

        return jsonify({
            "success": False,
            "message": "Password must be at least 8 characters"
        }), 400

    if role not in ("admin", "manager", "viewer"):

        return jsonify({
            "success": False,
            "message": "Invalid role"
        }), 400

    conn = get_db()

    try:

        conn.execute("""
            INSERT INTO USER
                (org_id, username, password_hash, role)

            VALUES (?, ?, ?, ?)
        """, (
            user["org_id"],
            username,
            hash_password(password),
            role
        ))

        conn.commit()

    except sqlite3.IntegrityError:

        conn.rollback()
        conn.close()

        return jsonify({
            "success": False,
            "message": "Username already exists"
        }), 400

    conn.close()

    return jsonify({
        "success": True,
        "message": "Team member added"
    })


# ============================================================
# DELETE TEAM MEMBER
# ============================================================

@app.route(
    "/api/team/<int:user_id>/delete",
    methods=["POST"]
)
@api_login_required
def delete_team_member(user_id):

    user = current_user()

    if user["role"] != "admin":

        return jsonify({
            "success": False,
            "message": "Only admins can remove team members"
        }), 403

    if not check_csrf():

        return jsonify({
            "success": False,
            "message": "Invalid CSRF token"
        }), 403

    if user_id == user["id"]:

        return jsonify({
            "success": False,
            "message": "You cannot remove yourself"
        }), 400

    conn = get_db()

    result = conn.execute("""
        DELETE FROM USER

        WHERE id = ?
          AND org_id = ?
    """, (
        user_id,
        user["org_id"]
    ))

    conn.commit()
    conn.close()

    if result.rowcount == 0:

        return jsonify({
            "success": False,
            "message": "User not found"
        }), 404

    return jsonify({
        "success": True,
        "message": "Team member removed"
    })


# ============================================================
# CURRENT USER API
# ============================================================

@app.route("/api/me")
@api_login_required
def me():

    user = current_user()

    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "org": user["org"]
    })


# ============================================================
# ERROR HANDLING
# ============================================================

from werkzeug.exceptions import HTTPException


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    """Never send an HTML error page to the browser's fetch() calls."""

    if isinstance(error, HTTPException):
        return error

    app.logger.exception("Unexpected error")

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "message": "Something went wrong on the server. "
                       "Check the terminal window for details."
        }), 500

    return "Something went wrong. Check the terminal for details.", 500


# ============================================================
# INITIALIZE
# ============================================================

def backup_database(conn):
    """Copy the whole database file before the cost model is applied."""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(
        os.path.dirname(DATABASE), f"smartstock_backup_{stamp}.db"
    )

    target = sqlite3.connect(path)

    try:
        conn.backup(target)
    finally:
        target.close()

    return path


def migrate_transfer_table():
    """
    Safe to run on every start. Adds the columns the recommendation
    workflow needs and, ONCE (rows whose unit_price is still empty),
    re-applies the estimated cost model to existing transfers.

    Nothing is deleted. Before any change the database file is copied to
    database/smartstock_backup_<time>.db and the old rows are kept in the
    TRANSFER_BACKUP table.

    Already-approved rows are history: their costs and saving are kept,
    they are marked TRANSFER (the stock really moved).
    """

    conn = get_db()

    try:

        exists = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'TRANSFER'
        """).fetchone()

        if not exists:
            return

        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(TRANSFER)")
        }

        needs_migration = (
            "unit_price" not in cols
            or conn.execute(
                "SELECT 1 FROM TRANSFER WHERE unit_price IS NULL LIMIT 1"
            ).fetchone() is not None
        )

        # 1. Back up first, before touching anything.
        if needs_migration:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS TRANSFER_BACKUP AS
                SELECT * FROM TRANSFER
            """)
            conn.commit()

            path = backup_database(conn)
            print(f"Database backed up to {path}")

        # 2. New columns.
        for name, kind in (
            ("recommendation", "TEXT"),
            ("decided_by", "TEXT"),
            ("decided_at", "TEXT"),
            ("potential_saving", "REAL"),
            ("unit_price", "REAL"),
        ):
            if name not in cols:
                conn.execute(
                    f"ALTER TABLE TRANSFER ADD COLUMN {name} {kind}"
                )

        conn.commit()

        if not needs_migration:
            return

        # 3. Recalculate existing rows once.
        rows = conn.execute("""
            SELECT transfer_id, quantity, purchase_cost, status,
                   source_store_id, destination_store_id
            FROM TRANSFER
            WHERE unit_price IS NULL
        """).fetchall()

        for r in rows:

            qty = r["quantity"] or 0

            # The old purchase_cost was quantity x selling price, so the
            # selling price per unit can be recovered from it.
            unit_price = (
                float(r["purchase_cost"] or 0) / qty if qty > 0 else 0.0
            )

            if r["status"] == "APPROVED":

                conn.execute("""
                    UPDATE TRANSFER
                    SET unit_price = ?,
                        recommendation = 'TRANSFER',
                        potential_saving = 0
                    WHERE transfer_id = ?
                """, (unit_price, r["transfer_id"]))

                continue

            t_cost, p_cost, method, potential = estimate_costs(
                qty, unit_price,
                r["source_store_id"], r["destination_store_id"]
            )

            conn.execute("""
                UPDATE TRANSFER
                SET unit_price = ?,
                    transfer_cost = ?,
                    purchase_cost = ?,
                    recommendation = ?,
                    potential_saving = ?,
                    saving = 0
                WHERE transfer_id = ?
            """, (unit_price, t_cost, p_cost, method, potential,
                  r["transfer_id"]))

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:
        conn.close()


init_auth_tables()
migrate_transfer_table()
metrics.init()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    cfg = llm.config()

    if cfg:

        backup = llm.fallback_config()

        print(
            f"AI provider ready: "
            f"{cfg['provider'].capitalize()} "
            f"({cfg['model']})"
            + (f"  backup model: {backup['model']}" if backup else "")
        )

    else:

        print(
            "AI not set up: "
            + llm.problem()
        )

    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
