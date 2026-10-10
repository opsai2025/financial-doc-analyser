# Bank audit report analyser

A Persian (RTL) Streamlit app that analyses Iranian bank audit reports (PDF) with an LLM and shows the results as dashboards and Excel workbooks. Users, providers, models and API keys are managed in the app; data lives in Supabase (Postgres + Storage).

- Setup, local run and deployment on Posit Connect Cloud: [docs/SETUP.md](docs/SETUP.md)
- Design decisions: [docs/DECISIONS.md](docs/DECISIONS.md) · status: [docs/PLAN.md](docs/PLAN.md) · analysis engine: [docs/ENGINE.md](docs/ENGINE.md)

Run locally: `pip install -r requirements-dev.txt`, copy `.env.example` to `.env`, then `streamlit run app.py`.
