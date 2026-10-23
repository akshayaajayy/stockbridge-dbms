# StockBridge

Smart inventory redistribution. For every branch shortage, StockBridge compares **moving stock from another branch** with **buying new stock**, recommends the cheaper option, and explains why.

## Run it

1. Install Python 3.10 or newer.
2. Put all project files in one folder (keep your `templates/` and `static/` folders and the `database/` folder with `smartstock.db`).
3. Open a terminal in that folder and run:

```bash
pip install -r requirements.txt
python setup_ai.py       # asks for your free Gemini key, writes .env
python app.py
```

4. Open http://127.0.0.1:5000, choose **Create an organisation** (you become the admin), then log in.
5. Click **Ask AI** (bottom corner) or open http://127.0.0.1:5000/assistant and ask a question.

You should see `AI provider ready: Gemini (gemini-flash-latest)` in the terminal. If you see `AI not set up`, the key is missing: run `python setup_ai.py` again.

The app expects `database/smartstock.db` with the tables `INVENTORY`, `DEMAND`, `INVENTORY_AGE`, `BRANCH` and `TRANSFER`. On start it creates the login tables and adds the new `TRANSFER` columns (`recommendation`, `decided_by`, `decided_at`, `potential_saving`, `unit_price`).

**First start after upgrading:** existing transfers are recalculated once with the cost model below. Nothing is deleted. Before any change the app copies the database to `database/smartstock_backup_<time>.db` and keeps the old rows in a `TRANSFER_BACKUP` table. Already-approved rows are treated as history: they are marked `TRANSFER` and their costs and `saving` are not recalculated.

## How recommendations work

All costs are **estimated costs, not actual M5 costs**.

| | Formula |
|---|---|
| Purchase cost | quantity x selling price x 0.70 |
| Transfer cost | Rs 2 fixed handling + quantity x Rs 0.80 x distance factor |
| Distance factor | 1.0 same state, 2.0 different state |

Branches map to states by their prefix: `CA_1`, `CA_2` = California; `TX_1`, `TX_2` = Texas; `WI_1` = Wisconsin.

- Transfer cost <= purchase cost: recommend `TRANSFER` (a tie goes to transfer). Otherwise recommend `PURCHASE`.
- `potential_saving` = `ABS(purchase_cost - transfer_cost)` for every recommendation.
- `saving` is the **actual** saving: purchase cost minus transfer cost, recorded only when a `TRANSFER` is approved. An approved `PURCHASE` is not a saving.
- `unit_price` is the selling price per unit used for the purchase estimate. For existing rows it is recovered once from the old `purchase_cost / quantity`. Use `estimate_costs()` in `app.py` when creating new recommendations.

## What approving does

| Recommendation | Source branch stock | Destination branch stock | Saving recorded |
|---|---|---|---|
| TRANSFER | decreases by quantity | increases by quantity | purchase cost - transfer cost |
| PURCHASE | unchanged | increases by quantity | none |

Stock changes and the status update happen in **one SQLite transaction** (all or nothing), and a transfer is refused if the source no longer has enough stock. The row becomes `APPROVED` (or `REJECTED`) with `decided_by` (logged-in username) and `decided_at` (timestamp), so you can show who decided what and when.

## Transfers page API

`GET /api/transfers` accepts `method=all|transfer|purchase`, `status=all|pending|approved|rejected` and `q=` (searches item, branch or transfer id; `#25` works). Each row includes `transfer_cost`, `purchase_cost`, `recommendation`, `potential_saving`, `saving` (actual) and `display_saving` (actual if approved, otherwise potential).

## Dashboard and reports

- **Total savings** = actual savings from approved transfers. **Potential savings** = money pending recommendations could still save. They are never mixed.
- `GET /api/dashboard`: total stock, low stock, aging stock, pending, approved transfers, approved purchases, total and potential savings.
- `GET /api/reports`: SQL aggregates for recommendations by status and method, actual and potential savings, total transfer and purchase cost, and `recent_decisions` (approved and rejected, with who and when).

## Explainable recommendations (the "Why?" button)

Each row on the Transfers page has a **Why?** button (`GET /api/transfers/<id>/explain`). It is rule-based, not AI. It shows:

- the recommendation, a plain-English summary and the estimated saving
- short reasons (for example "Destination has a stock shortage", "Source has excess stock", "Transfer is cheaper than purchasing")
- how the estimated costs were built (when the stored costs match the cost model)
- the factors behind it: cost gap, destination shortage, whether the source stays covered, aging stock, overstock risk
- a confidence level (High / Medium / Low) with any concerns to check
- what would change it (a higher transport cost or lower purchase price could make PURCHASE cheaper, and vice versa)

For rows already approved, stock levels are rewound to what they were at decision time.

## Roles

- **admin**: everything, plus the Team page (add and remove users), approve/reject, AI assistant
- **manager**: view data, approve/reject, use the AI assistant
- **viewer**: view inventory, aging, transfers (including "Why?") and reports. Cannot approve, reject, manage the team or use the AI assistant.

## Before going live

- Set a long random `SECRET_KEY` in `.env` (`setup_ai.py` does this for you).
- Passwords use plain SHA-256 without a salt. For real use, switch to `werkzeug.security.generate_password_hash` / `check_password_hash`.
- Run behind a production server (for example gunicorn), not `app.run`.

## AI assistant (natural language to SQL)

Three AI features, all optional and free to run, for admins and managers. Without an API key the rest of the app works as before. **The AI never approves, rejects or changes anything.** AI retrieves and explains, a human decides, the application executes the transaction.

1. **Assistant page** (`/assistant`, and the floating chat): ask things like "Which branch has the highest inventory?" or "Show me products with aging stock". The AI writes a SQL query, the app validates and runs it, and you get a plain-English answer, a table, the query used, and a CSV download.
2. **AI wording of "Why?"** (`POST /api/transfers/<id>/ai-explain`): the rule-based explanation is computed first; the AI only rewrites those verified facts in natural language. It explains the existing decision and does not change it.
3. **Daily briefing** (`POST /api/ai/briefing`): items needing attention, low-stock branches, aging items, pending recommendations, and the top potential saving. All numbers come from SQL; the AI only words them.

### Free Gemini setup

1. Go to https://aistudio.google.com/apikey, sign in with a Google account and click **Create API key**. No credit card is needed. **Do not enable billing.**
2. Run `python setup_ai.py`. It lists the models your key can use, tests the one you pick and writes `.env`.
3. Restart `python app.py`.

Recommended models (both free):

| Model | Use |
|---|---|
| `gemini-flash-latest` (main) | Best answers. Google's alias for the current Flash model. |
| `gemini-flash-lite-latest` (backup) | Faster, with a bigger free allowance. Used automatically if the main model is rate-limited or busy. |

The `-latest` names are aliases that follow Google's newest version, so the project keeps working when old versions are retired. You can pick a specific version from the list in `setup_ai.py` instead. Free-tier limits are per model and fairly low (roughly 5 to 15 requests per minute), and one chat question uses two requests, so if you get a "limit reached" message, wait a minute.

Settings live in `.env` (see `.env.example`):

| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY` | your key |
| `AI_MODEL` | main model |
| `AI_FALLBACK_MODEL` | backup model (`none` to disable) |
| `LLM_SEND_ROWS` | `1` (default) sends up to 40 result rows to Gemini so it can write the sentence answer; `0` keeps rows local and just shows the table |
| `SECRET_KEY` | long random text for logins |

### How AI-written SQL is kept safe

The model's SQL is never trusted:

- It runs on a separate **read-only** database connection (`mode=ro`, `query_only`).
- Exactly one `SELECT` is allowed. Anything containing `DELETE`, `UPDATE`, `INSERT`, `DROP`, `ALTER`, `CREATE`, `PRAGMA` (and similar) is refused, not retried.
- A SQLite authorizer allows reads only on: `INVENTORY`, `INVENTORY_AGE`, `PRODUCT`, `CATEGORY`, `BRANCH`, `SALE`, `SALE_ITEM`, `PRODUCT_PRICE`, `DEMAND`, `TRANSFER` (tables that don't exist in your database are skipped), plus a list of harmless functions.
- `USER`, `ORGANISATION` (logins, password hashes) and `TRANSFER_BACKUP` are blocked. Requests like "Delete all users" or "Show me the USER table" are refused with a clear message.
- Queries time out after 5 seconds and results are capped at 200 rows. Each user is limited to 20 AI requests per minute.

### Privacy note

To answer, the app sends your table structure and the question to the AI provider. With `LLM_SEND_ROWS=1` it also sends up to 40 result rows, and the briefing and explanation features send transfer details. Free tiers may use prompts to improve their models, so check each provider's terms. Set `LLM_SEND_ROWS=0` to keep result rows off their servers.

### Suggested demo

1. **Inventory:** ask "Which branch has the highest inventory?"
2. **Aging:** ask "Show me products with aging stock."
3. **Recommendation:** open Transfers, show transfer vs purchase cost and the recommendation, click **Why?**
4. **Approval:** click Approve, then show status, decided by, decided at, and the changed stock.
5. **Security:** ask "Delete all users" and "Show me the USER table"; both are refused.

### Troubleshooting

| Message or symptom | Fix |
|---|---|
| "No Gemini API key found" / "AI not set up" | Run `python setup_ai.py`, then restart the app. |
| "rejected your API key" | Wrong, expired or restricted key. Create a new one in AI Studio and run `python setup_ai.py`. |
| "does not know the model" | Run `python setup_ai.py` and choose a model from the list. |
| "free limit was reached" | Wait a minute. Free limits are low. The backup model is tried automatically. |
| "Gemini is busy" | Temporary overload. Try again. |
| "I couldn't build a working query" | Rephrase the question using words from your data (item, branch, status). |
| "available to admins and managers" | Viewers cannot use the AI assistant. Log in as a manager or admin. |
| "No module named ..." | Run `pip install -r requirements.txt`. |
| Ask AI button missing | Make sure `chatbot.js` is the one your `base.html` loads, then hard-refresh (Ctrl+F5). |
