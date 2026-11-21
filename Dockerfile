# Paysight: runs the Streamlit UI in replay mode, with no API key needed.
#   docker build -t paysight .
#   docker run --rm -p 8501:8501 paysight            # then open http://localhost:8501
# Pass --env-file .env to use a live LLM instead of the committed cassettes.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY app ./app
COPY cassettes ./cassettes
COPY eval ./eval
COPY data/ground_truth.yaml ./data/ground_truth.yaml
COPY docs/DATA_DICTIONARY.md ./docs/DATA_DICTIONARY.md
RUN uv sync --frozen --no-dev

# The dataset is deterministic, so it is built into the image (and checked against the
# committed ground truth by the generator's scenario checks).
RUN paysight data generate

RUN useradd --create-home paysight && chown -R paysight /app
USER paysight

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.address=0.0.0.0", \
     "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
