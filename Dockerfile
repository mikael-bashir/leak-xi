# Leak XI — mathlib_search (retrieval), as a real FastMCP server matching
# Leak-I/II's own wrapper architecture exactly (mcp.server.fastmcp, @mcp.tool,
# mcp.sse_app()) rather than a bespoke REST API, so the app's "Add Server"
# dialog can register and live-handshake against it like every other Leak
# server. SQLite FTS5 index built from the Mathlib source tree at image build
# time — ~1 GB build-time clone, tiny runtime footprint. Pin the Mathlib rev
# to the same release the Leak XII gateway compiles against so names never
# drift.
FROM python:3.12-slim

ARG MATHLIB_REV=v4.32.0

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastmcp nest_asyncio "uvicorn[standard]"

COPY build_index.py /opt/leak-xi/build_index.py

RUN git clone --depth 1 --branch ${MATHLIB_REV} \
      https://github.com/leanprover-community/mathlib4 /opt/mathlib4 \
    && python /opt/leak-xi/build_index.py /opt/mathlib4 /opt/index/mathlib.db \
    && rm -rf /opt/mathlib4

COPY server.py /opt/leak-xi/server.py

ENV DB_PATH=/opt/index/mathlib.db
WORKDIR /opt/leak-xi
EXPOSE 7860
CMD ["python3", "server.py"]
