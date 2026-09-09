"""Hybrid search over agent.agent_knowledge: keyword (ts_rank) + vector
(cosine distance), merged via reciprocal rank fusion.

This is the ONE tool the live agent (Phase 8's RETRIEVE node) will call —
it runs both signals and merges them internally; the agent never chooses
between keyword-only or vector-only, see the conversation this was
written from for why that's not a useful decision to hand to an LLM.
"""

import os
import sys

import psycopg

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")
RRF_K = 60  # standard reciprocal-rank-fusion constant, not tuned


def _keyword_search(conn, query_text: str, limit: int = 10) -> list[tuple]:
    return conn.execute(
        """
        SELECT id, title, content, related_keys
        FROM agent.agent_knowledge
        WHERE content_tsv @@ plainto_tsquery('english', %s)
        ORDER BY ts_rank(content_tsv, plainto_tsquery('english', %s)) DESC
        LIMIT %s
        """,
        (query_text, query_text, limit),
    ).fetchall()


def _vector_search(conn, query_vector_str: str, limit: int = 10) -> list[tuple]:
    return conn.execute(
        """
        SELECT id, title, content, related_keys
        FROM agent.agent_knowledge
        ORDER BY content_vector <=> %s::vector
        LIMIT %s
        """,
        (query_vector_str, limit),
    ).fetchall()


def search(query_text: str, top_k: int = 3) -> list[dict]:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
    from embeddings import embed_query

    qvec = embed_query(query_text)
    qvec_str = "[" + ",".join(str(x) for x in qvec) + "]"

    with psycopg.connect(DB_DSN) as conn:
        keyword_rows = _keyword_search(conn, query_text)
        vector_rows = _vector_search(conn, qvec_str)

    # Reciprocal rank fusion: score = sum of 1/(RRF_K + rank) across
    # whichever list(s) a row appears in. A row in both lists outranks one
    # in only one list, but a row in only one list still counts.
    scores: dict[int, float] = {}
    rows_by_id: dict[int, tuple] = {}
    for rank, row in enumerate(keyword_rows):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (RRF_K + rank)
        rows_by_id[row[0]] = row
    for rank, row in enumerate(vector_rows):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (RRF_K + rank)
        rows_by_id[row[0]] = row

    ranked_ids = sorted(scores, key=lambda i: scores[i], reverse=True)[:top_k]
    return [
        {
            "title": rows_by_id[i][1],
            "content": rows_by_id[i][2],
            "related_keys": rows_by_id[i][3],
            "score": round(scores[i], 5),
        }
        for i in ranked_ids
    ]


if __name__ == "__main__":
    query_text = " ".join(sys.argv[1:]) or "pod is Ready but running stale config"
    for r in search(query_text):
        print(f"[{r['score']}] {r['title']}")
        print(f"    related_keys: {r['related_keys']}")
