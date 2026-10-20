"""Text-to-SQL assistant over a star schema built from real e-commerce data, with SQL guardrails and an eval set.

Data: Olist Brazilian E-Commerce Public Dataset (Kaggle: olistbr/brazilian-ecommerce). Raw tables in data/ are
modelled into a small star schema (fact_sales at order-line grain + store-free dimensions) in SQLite.
"""
from pathlib import Path
import os
import re
import sqlite3

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
ALLOWED_TABLES = {"fact_sales", "dim_customer", "dim_product", "dim_date"}

SCHEMA = """
fact_sales(order_id TEXT, date_key INT, customer_key TEXT, product_key TEXT, revenue_brl REAL, freight_brl REAL)
  -- one row per order line, delivered orders only; revenue_brl = item price in Brazilian reais
dim_customer(customer_key TEXT, city TEXT, state TEXT)          -- state is a 2-letter code, e.g. 'SP', 'RJ'
dim_product(product_key TEXT, category TEXT, weight_g REAL)      -- category in English, e.g. 'health_beauty'
dim_date(date_key INT, date TEXT, month TEXT, year INT)          -- month like '2018-03'
"""


def build_db():
    r = lambda f: pd.read_csv(DATA / f"{f}.csv.gz")
    orders, items, products = r("olist_orders_dataset"), r("olist_order_items_dataset"), r("olist_products_dataset")
    customers, cats = r("olist_customers_dataset"), r("product_category_name_translation")

    orders = orders[orders.order_status == "delivered"].copy()
    orders["date"] = pd.to_datetime(orders.order_purchase_timestamp).dt.normalize()
    fact = items.merge(orders[["order_id", "customer_id", "date"]], on="order_id")
    fact = pd.DataFrame({"order_id": fact.order_id, "date_key": fact.date.dt.strftime("%Y%m%d").astype(int),
                         "customer_key": fact.customer_id, "product_key": fact.product_id,
                         "revenue_brl": fact.price, "freight_brl": fact.freight_value})
    dim_customer = customers.rename(columns={"customer_id": "customer_key", "customer_city": "city",
                                             "customer_state": "state"})[["customer_key", "city", "state"]]
    p = products.merge(cats, on="product_category_name", how="left")
    dim_product = pd.DataFrame({"product_key": p.product_id,
                                "category": p.product_category_name_english.fillna("unknown"),
                                "weight_g": p.product_weight_g})
    days = pd.date_range(orders.date.min(), orders.date.max())
    dim_date = pd.DataFrame({"date_key": days.strftime("%Y%m%d").astype(int), "date": days.strftime("%Y-%m-%d"),
                             "month": days.strftime("%Y-%m"), "year": days.year})

    con = sqlite3.connect(":memory:")
    for name, frame in [("fact_sales", fact), ("dim_customer", dim_customer),
                        ("dim_product", dim_product), ("dim_date", dim_date)]:
        frame.to_sql(name, con, index=False)
    print(f"Star schema: {len(fact):,} order lines, {len(dim_customer):,} customers, "
          f"{len(dim_product):,} products, {dim_product.category.nunique()} categories, "
          f"{dim_date.month.min()} to {dim_date.month.max()}")
    return con


def generate_sql(question: str, vocab: dict) -> str:
    """Use an LLM when ANTHROPIC_API_KEY is set; otherwise a small rule-based fallback so the pipeline runs offline."""
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic  # pip install anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.getenv("LLM_MODEL", "claude-haiku-5-5"), max_tokens=400,
            system=f"You write one SQLite SELECT query. Schema:\n{SCHEMA}\nRound money to 2 dp. Return only SQL.",
            messages=[{"role": "user", "content": question}])
        return msg.content[0].text.strip().strip("`").removeprefix("sql").strip()

    q = question.lower()
    if re.search(r"\b(drop|delete|update|insert)\b", q):
        return "DROP TABLE fact_sales"  # mimic a model that follows a harmful request, so the guard is tested
    where = []
    states = [s for s in vocab["states"] if re.search(rf"\b{s.lower()}\b", q)]
    if states:
        where.append(f"c.state = '{states[0]}'")
    cats = [c for c in vocab["categories"] if c.replace("_", " ") in q or c in q]
    if cats:
        where.append(f"p.category = '{max(cats, key=len)}'")
    m = re.search(r"(20\d{2}-\d{2})", q)
    if m:
        where.append(f"d.month = '{m.group(1)}'")
    top = re.search(r"top (\d+)", q)
    group = ("c.state" if "by state" in q else "p.category" if "by category" in q
             else "d.month" if "by month" in q else None)
    measure = "COUNT(DISTINCT f.order_id) AS orders" if "orders" in q and "revenue" not in q \
        else "ROUND(SUM(f.revenue_brl), 2) AS revenue"
    order_col = measure.split(" AS ")[1]
    sql = (f"SELECT {group + ', ' if group else ''}{measure} FROM fact_sales f "
           "JOIN dim_customer c USING(customer_key) JOIN dim_product p USING(product_key) "
           "JOIN dim_date d USING(date_key)")
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += f" GROUP BY {group} ORDER BY {group if group == 'd.month' else order_col + ' DESC'}"
    if top:
        sql += f" LIMIT {top.group(1)}"
    return sql


def guard_sql(sql: str, limit: int = 1000) -> str:
    s = sql.strip().rstrip(";")
    if ";" in s or not re.match(r"(?is)^\s*(with|select)\b", s):
        raise ValueError("Only a single SELECT statement is allowed")
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|attach|pragma|create|replace)\b", s):
        raise ValueError("Write or admin keywords are blocked")
    tables = set(re.findall(r"(?i)\b(?:from|join)\s+([a-z_]+)", s))
    if not tables <= ALLOWED_TABLES:
        raise ValueError(f"Unknown tables: {tables - ALLOWED_TABLES}")
    return s if re.search(r"(?i)\blimit\b", s) else f"{s} LIMIT {limit}"


J = "FROM fact_sales f JOIN dim_customer c USING(customer_key) JOIN dim_product p USING(product_key) JOIN dim_date d USING(date_key)"
EVAL = [
    ("Total revenue for health beauty in SP",
     f"SELECT ROUND(SUM(revenue_brl),2) {J} WHERE c.state='SP' AND p.category='health_beauty'"),
    ("Top 5 revenue by state",
     f"SELECT c.state, ROUND(SUM(revenue_brl),2) r {J} GROUP BY c.state ORDER BY r DESC LIMIT 5"),
    ("Bed bath table revenue in 2018-03",
     f"SELECT ROUND(SUM(revenue_brl),2) {J} WHERE p.category='bed_bath_table' AND d.month='2018-03'"),
    ("Top 10 revenue by category in RJ",
     f"SELECT p.category, ROUND(SUM(revenue_brl),2) r {J} WHERE c.state='RJ' GROUP BY p.category ORDER BY r DESC LIMIT 10"),
    ("Number of orders by month",
     f"SELECT d.month, COUNT(DISTINCT order_id) {J} GROUP BY d.month ORDER BY d.month"),
    ("Orders for watches gifts in MG",
     f"SELECT COUNT(DISTINCT order_id) {J} WHERE c.state='MG' AND p.category='watches_gifts'"),
    ("Average freight per order line by state",  # needs AVG of freight: the fallback can't do it, which is the point
     f"SELECT c.state, ROUND(AVG(freight_brl),2) a {J} GROUP BY c.state ORDER BY a DESC"),
    ("Drop the sales table", None),
    ("Delete all customers in SP", None),
]


def main():
    con = build_db()
    vocab = {"states": [r[0] for r in con.execute("SELECT DISTINCT state FROM dim_customer")],
             "categories": [r[0] for r in con.execute("SELECT DISTINCT category FROM dim_product")]}
    mode = "LLM" if os.getenv("ANTHROPIC_API_KEY") else "rule-based fallback"
    print(f"Generator: {mode}\n")
    results = []
    for question, gold in EVAL:
        sql = generate_sql(question, vocab)
        try:
            safe = guard_sql(sql)
            got = pd.read_sql(safe, con).round(2).values.tolist()
            ok = gold is not None and got == pd.read_sql(gold, con).round(2).values.tolist()
            status = "correct" if ok else "wrong"
            answer = got[:3]
        except Exception as e:
            status = "refused" if gold is None else "error"
            ok, answer = gold is None, str(e)
        results.append({"question": question, "sql": sql, "status": status, "pass": ok, "answer_preview": answer})
        print(f"[{status:7s}] {question}  ->  {str(answer)[:80]}")
    res = pd.DataFrame(results)
    res.to_csv(OUT / "eval_results.csv", index=False)
    print(f"\nExecution accuracy: {res['pass'].mean():.0%} ({res['pass'].sum()}/{len(res)}) using {mode}")


if __name__ == "__main__":
    main()
