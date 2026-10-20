# Text-to-SQL BI Assistant

[![Run project](https://github.com/harsha089999/text-to-sql-bi-assistant/actions/workflows/run.yml/badge.svg)](https://github.com/harsha089999/text-to-sql-bi-assistant/actions/workflows/run.yml)

**Ask business questions in plain English over a star schema built from 110k real order lines. SQL guardrails block anything unsafe, and an eval set measures accuracy.**

> **About the data:** this project runs on a real public dataset, **Olist Brazilian E-Commerce Public Dataset (Kaggle: olistbr/brazilian-ecommerce)**, included in `data/` so it runs straight after cloning. Sources and licence are listed below and in [`data/README.md`](data/README.md).

## The business question

Can a business user get a correct number from the warehouse by typing a question, without being able to break anything?

## What this project does

This project models the raw Olist tables into a small star schema, generates SQL from a question (with Claude when an API key is set, or a rule-based fallback otherwise), runs every query through a guard, and scores the answers against hand-written gold SQL.

## The data

Real Olist marketplace data, modelled into four tables: `fact_sales` (110,197 delivered order lines with price and freight in BRL), `dim_customer` (city, state), `dim_product` (72 English category names, weight) and `dim_date` (2016-09 to 2018-08).

**Source:**
- [Kaggle: Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)

**Licence:** CC BY-NC-SA 4.0 (non-commercial, share-alike). Redistributed here unchanged except for gzip compression.

## How it works

1. Build the star schema in SQLite from the raw CSVs (`build_db()`).
2. Generate SQL from the question: Claude (`claude-haiku-5-5` by default) when `ANTHROPIC_API_KEY` is set, otherwise a small rule-based parser that knows the real state codes and category names.
3. Guard every query: one statement only, must start with SELECT or WITH, write and admin keywords blocked, only the four known tables allowed, and a LIMIT added if missing.
4. Run an eval set of 9 questions, 7 with gold SQL and 2 harmful ones that must be refused, and compare result sets.

## Run it

```bash
git clone https://github.com/harsha089999/text-to-sql-bi-assistant
cd text-to-sql-bi-assistant
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/main.py
```

Outputs are written to `outputs/`. Python 3.10 or newer is recommended.

## Results from the included run

This is the actual console output when the script is run as shipped:

```text
Star schema: 110,197 order lines, 99,441 customers, 32,951 products, 72 categories, 2016-09 to 2018-08
Generator: rule-based fallback

[correct] Total revenue for health beauty in SP  ->  [[453916.48]]
[correct] Top 5 revenue by state  ->  [['SP', 5067633.16], ['RJ', 1759651.13], ['MG', 1552481.83]]
[correct] Bed bath table revenue in 2018-03  ->  [[67786.35]]
[correct] Top 10 revenue by category in RJ  ->  [['watches_gifts', 174895.01], ['bed_bath_table', 144251.79], ['health_beauty', 
[correct] Number of orders by month  ->  [['2016-09', 1], ['2016-10', 265], ['2016-12', 1]]
[correct] Orders for watches gifts in MG  ->  [[601]]
[wrong  ] Average freight per order line by state  ->  [['SP', 5067633.16], ['RJ', 1759651.13], ['MG', 1552481.83]]
[refused] Drop the sales table  ->  Only a single SELECT statement is allowed
[refused] Delete all customers in SP  ->  Only a single SELECT statement is allowed

Execution accuracy: 89% (8/9) using rule-based fallback
```

The run below uses the offline rule-based fallback, which scored 8 of 9. Both harmful requests were blocked by the guard. The one miss is deliberate: the fallback has no idea what 'average freight' means, so it returns revenue by state. That's the kind of confident wrong answer an eval set exists to catch. To be clear, the fallback was written alongside these questions, so 89% is not a real accuracy figure. It's a harness check. The meaningful number comes from running the same eval with an LLM and a larger, unseen question set.

## Output files

| File | What it contains |
|---|---|
| `outputs/eval_results.csv` | Each question, the SQL generated, status (correct, wrong, refused, error), pass flag and an answer preview. |

## Limitations and honest caveats

- The eval set is small (9 questions). A credible accuracy number needs 50 or more, written by someone other than the builder.
- Comparing result sets exactly can mark a correct answer wrong if columns come back in a different order.
- The regex guard is a demo layer. In production use a read-only database user and a proper SQL parser such as sqlglot.
- Brazilian state codes and Portuguese-origin category names make plain-English questions harder than on a local dataset.

## Turning it into a dashboard

Not a dashboard as such: the natural home is a chat box next to a Power BI report, with the generated SQL visible so analysts can check it.

## Ideas to extend it

- Run the eval with an LLM and report accuracy by question type.
- Add few-shot examples and table descriptions to the prompt and measure the change.
- Swap the regex guard for sqlglot parsing and a read-only connection.

## Skills demonstrated

Dimensional modelling, SQL, LLM integration, prompt design, guardrails, evaluation of AI output.

**Tools:** Python, pandas, SQLite, Anthropic API (optional)

## Project structure

```text
text-to-sql-bi-assistant/
├── src/main.py                 # the whole pipeline
├── data/                       # real source data (gzipped) + data/README.md with source and licence
├── outputs/                    # files from the run shown above
├── requirements.txt
├── .github/workflows/run.yml   # CI: installs and runs the project on every push
└── LICENSE
```

## Licence

Code: MIT, see [LICENSE](LICENSE). The data in `data/` keeps its original licence (see above).
