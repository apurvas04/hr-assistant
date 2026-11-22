"""Run once from the project root:  python setup_repo_files.py
Creates .gitignore, requirements.txt, .env.example and a README.md stub.
It never overwrites a file that already exists."""
from pathlib import Path

FILES = {
".gitignore": """# secrets: NEVER commit these
.env
.env.*
!.env.example

# python
.venv/
__pycache__/
*.pyc
.pytest_cache/

# runtime data (traces contain user questions)
traces/

# editor / OS / temp
.vscode/
.idea/
.DS_Store
Thumbs.db
*.tmp
""",

"requirements.txt": """redis>=5
sentence-transformers>=3
PyYAML>=6
numpy
groq
python-dotenv
mcp<2
pytest
""",

".env.example": """# Copy this file to .env and fill in your own key. Never commit .env.
GROQ_API_KEY=your-groq-key-here
ANSWER_MODELS=openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b

# optional
# PROMPT_GUARD_MODEL=meta-llama/llama-prompt-guard-2-86m
# CACHE_THRESHOLD=0.15
# ROUTER_MAX_DISTANCE=0.45
# TRACE_CONTENT=0
""",

"README.md": """# HR Assistant

An internal HR assistant for a fictional company ("Nimbus"): retrieval-augmented answers with
citations, role-based access control, tool calling through an MCP server, guardrails, a
semantic cache, a model router, versioned prompts and tracing.

## Quick start

```
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server
python -m venv .venv
.venv\\Scripts\\activate
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
""",
}

for name, content in FILES.items():
    p = Path(name)
    if p.exists():
        print(f"skipped  {name} (already exists)")
    else:
        p.write_text(content, encoding="utf-8")
        print(f"created  {name}")
