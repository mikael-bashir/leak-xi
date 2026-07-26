# Leak XI — mathlib_search (retrieval). Lightweight: SQLite FTS5 index built
# from the Mathlib source tree at image build time. ~1 GB build-time clone,
# tiny runtime footprint. Pin the Mathlib rev to the same release the Leak XII
# gateway compiles against so names never drift.
FROM python:3.12-slim

ARG MATHLIB_REV=v4.32.0

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]"==0.30.*

COPY build_index.py /opt/leak-xi/build_index.py

RUN git clone --depth 1 --branch ${MATHLIB_REV} \
      https://github.com/leanprover-community/mathlib4 /opt/mathlib4 \
    && python /opt/leak-xi/build_index.py /opt/mathlib4 /opt/index/mathlib.db \
    && rm -rf /opt/mathlib4

COPY server.py /opt/leak-xi/server.py

ENV DB_PATH=/opt/index/mathlib.db PORT=8011
EXPOSE 8011
CMD ["sh", "-c", "uvicorn server:app --app-dir /opt/leak-xi --host 0.0.0.0 --port ${PORT}"]
