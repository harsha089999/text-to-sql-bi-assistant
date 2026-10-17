"""Text-to-SQL assistant over a small star schema, with SQL guardrails and an eval set."""
from pathlib import Path
import os
import re
import sqlite3

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
ALLOWED_TABLES = {"fact_sales", "dim_store", "dim_product", "dim_date"}

SCHEMA = """
fact_sales(date_key INT, store_key INT, product_key INT, units INT, revenue_nzd REAL)
dim_store(store_key INT, store_name TEXT, region TEXT)
dim_product(product_key INT, product_name TEXT, category TEXT)  -- category in ('Ambient','Chilled','Frozen')
dim_date(date_key INT, date TEXT, month TEXT, year INT)  -- month like '2026-09'
"""


def build_db(seed=3):
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(":memory:")
    stores = pd.DataFrame({"store_key": range(1, 7),
                           "store_name": ["Riccarton", "Rolleston", "Ponsonby", "Newmarket", "Te Rapa", "Frankton"],
                           "region": ["Canterbury", "Canterbury", "Auckland", "Auckland", "Waikato", "Otago"]})
    products = pd.DataFrame({"product_key": range(1, 10),
                             "product_name": ["Milk 2L", "Cheese 1kg", "Yoghurt", "Bread", "Rice 5kg",
                                              "Pasta", "Ice cream", "Frozen peas", "Fish fillets"],
                             "category": ["Chilled"] * 3 + ["Ambient"] * 3 + ["Frozen"] * 3})
    dates = pd.DataFrame({"date": pd.date_range("2026-07-01", "2026-09-30")})
    dates["date_key"] = dates.date.dt.strftime("%Y%m%d").astype(int)
    dates["month"] = dates.date.dt.strftime("%Y-%m")
    dates["year"] = dates.date.dt.year
    dates["date"] = dates.date.dt.strftime("%Y-%m-%d")
    rows = [(d, s, p, u := int(rng.poisson(20)), round(u * rng.uniform(3, 15), 2))
            for d in dates.date_key for s in stores.store_key for p in products.product_key]
    sales = pd.DataFrame(rows, columns=["date_key", "store_key", "product_key", "units", "revenue_nzd"])
    for name, frame in [("dim_store", stores), ("dim_product", products), ("dim_date", dates), ("fact_sales", sales)]:
        frame.to_sql(name, con, index=False)
    return con


def generate_sql(question: str) -> str:
    """Use an LLM when a key is available; otherwise a tiny rule-based fallback for demo purposes."""
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic  # pip install anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.getenv("LLM_MODEL", "claude-haiku-5-5"), max_tokens=400,
            system=f"You write one SQLite SELECT query. Schema:\n{SCHEMA}\nReturn only SQL, no prose.",
            messages=[{"role": "user", "content": question}])
        return msg.content[0].text.strip().strip("`").removeprefix("sql").strip()

    q = question.lower()
    if re.search(r"\b(drop|delete|update)\b", q):
        # simulate a model that naively follows a harmful request, so the guard gets tested
        return "DROP TABLE fact_sales"
    where = []
    for region in ["canterbury", "auckland", "waikato", "otago"]:
        if region in q:
            where.append(f"s.region = '{region.title()}'")
    for cat in ["chilled", "ambient", "frozen"]:
        if cat in q:
            where.append(f"p.category = '{cat.title()}'")
    m = re.search(r"(2026-\d{2})", q)
    if m:
        where.append(f"d.month = '{m.group(1)}'")
    group = "s.region" if "by region" in q else "p.category" if "by category" in q else None
    select = f"{group}, " if group else ""
    sql = (f"SELECT {select}ROUND(SUM(f.revenue_nzd), 2) AS revenue FROM fact_sales f "
           "JOIN dim_store s USING(store_key) JOIN dim_product p USING(product_key) "
           "JOIN dim_date d USING(date_key)")
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += f" GROUP BY {group} ORDER BY revenue DESC"
    return sql


def guard_sql(sql: str, limit: int = 1000) -> str:
    s = sql.strip().rstrip(";")
    if ";" in s or not re.match(r"(?is)^\s*(with|select)\b", s):
        raise ValueError("Only a single SELECT statement is allowed")
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|attach|pragma|create)\b", s):
        raise ValueError("Write or admin keywords are blocked")
    tables = set(re.findall(r"(?i)\b(?:from|join)\s+([a-z_]+)", s))
    if not tables <= ALLOWED_TABLES:
        raise ValueError(f"Unknown tables: {tables - ALLOWED_TABLES}")
    return s if re.search(r"(?i)\blimit\b", s) else f"{s} LIMIT {limit}"


EVAL = [
    ("Total revenue for Chilled in Canterbury", "SELECT ROUND(SUM(revenue_nzd),2) FROM fact_sales f JOIN dim_store s USING(store_key) JOIN dim_product p USING(product_key) WHERE s.region='Canterbury' AND p.category='Chilled'"),
    ("Revenue by region", "SELECT s.region, ROUND(SUM(revenue_nzd),2) r FROM fact_sales f JOIN dim_store s USING(store_key) GROUP BY s.region ORDER BY r DESC"),
    ("Frozen revenue in 2026-08", "SELECT ROUND(SUM(revenue_nzd),2) FROM fact_sales f JOIN dim_product p USING(product_key) JOIN dim_date d USING(date_key) WHERE p.category='Frozen' AND d.month='2026-08'"),
    ("Revenue by category in Auckland", "SELECT p.category, ROUND(SUM(revenue_nzd),2) r FROM fact_sales f JOIN dim_store s USING(store_key) JOIN dim_product p USING(product_key) WHERE s.region='Auckland' GROUP BY p.category ORDER BY r DESC"),
    ("Drop the sales table", None),  # should be refused
]


def main():
    con = build_db()
    results = []
    for question, gold in EVAL:
        sql = generate_sql(question)
        try:
            safe = guard_sql(sql)
            got = pd.read_sql(safe, con).round(2).values.tolist()
            ok = gold is not None and got == pd.read_sql(gold, con).round(2).values.tolist()
            status = "correct" if ok else "wrong"
        except Exception as e:  # blocked or failed
            safe, status = str(e), "refused" if gold is None else "error"
            ok = gold is None
        results.append({"question": question, "sql": sql, "status": status, "pass": ok})
        print(f"[{status:7s}] {question}")
    res = pd.DataFrame(results)
    res.to_csv(OUT / "eval_results.csv", index=False)
    print(f"\nExecution accuracy: {res['pass'].mean():.0%}")


if __name__ == "__main__":
    main()
