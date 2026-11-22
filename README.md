# HR Assistant

An internal HR assistant for a fictional company ("Nimbus"): retrieval-augmented answers with
citations, role-based access control, tool calling through an MCP server, guardrails, a
semantic cache, a model router, versioned prompts and tracing.

## Quick start

```
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        (then put your Groq key in .env)
python -m app.ingest
python -m app.answer --user E001 "How many casual leave days do I get?"
```

Users are defined in `data/policies/employees.json` (E001 employee, E002 manager, E003 HR).

## Tests and evals

```
python -m pytest tests -q
python -m eval.run_retrieval --label mine --answers
python -m eval.compare
```

(A full write-up and architecture notes are coming.)
