"""Parses knowledge/golden-architecture.md, embeds each ### chunk, and
upserts into agent.agent_knowledge.

Chunking: one atomic fact per ### heading (not per whole document) — see
the doc's own header comment for why. Each chunk's first line after the
heading is `related_keys: [...]` (Python-list-literal syntax), the rest is
body content. content_hash gates re-embedding: unchanged chunks are
skipped on a re-run.
"""

import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from embeddings import embed_documents

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")
DOC_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "golden-architecture.md"

_CHUNK_RE = re.compile(r"^### (.+)$", re.MULTILINE)


def parse_related_keys(line: str) -> list[str]:
    """`related_keys: [a, b]` or `related_keys: []` -> ['a', 'b'] or []."""
    match = re.match(r"related_keys:\s*\[(.*)\]", line.strip())
    if not match:
        return []
    inner = match.group(1).strip()
    if not inner:
        return []
    return [k.strip() for k in inner.split(",")]


def parse_document(text: str) -> tuple[dict, list[dict]]:
    # Frontmatter: between the first two '---' lines.
    parts = text.split("---", 2)
    frontmatter = yaml.safe_load(parts[1])
    body = parts[2]

    chunks = []
    matches = list(_CHUNK_RE.finditer(body))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk_body = body[start:end].strip()

        lines = chunk_body.split("\n", 1)
        related_keys = parse_related_keys(lines[0])
        content = lines[1].strip() if len(lines) > 1 else ""

        chunks.append({"title": title, "related_keys": related_keys, "content": content})

    return frontmatter, chunks


def main() -> None:
    text = DOC_PATH.read_text()
    frontmatter, chunks = parse_document(text)
    doc_id = frontmatter["doc_id"]

    print(f"Parsed {len(chunks)} chunks from {DOC_PATH.name} (doc_id={doc_id})")

    full_texts = [f"{c['title']}\n\n{c['content']}" for c in chunks]
    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in full_texts]

    with psycopg.connect(DB_DSN, autocommit=False) as conn:
        existing = dict(
            conn.execute(
                "SELECT title, content_hash FROM agent.agent_knowledge WHERE doc_id = %s",
                (doc_id,),
            ).fetchall()
        )

        to_embed_idx = [
            i for i, c in enumerate(chunks) if existing.get(c["title"]) != hashes[i]
        ]

        if not to_embed_idx:
            print("Nothing changed, nothing to re-embed.")
            return

        print(f"Embedding {len(to_embed_idx)} new/changed chunk(s) locally (bge-small)...")
        vectors = embed_documents([full_texts[i] for i in to_embed_idx])

        for idx, vector in zip(to_embed_idx, vectors):
            c = chunks[idx]
            conn.execute(
                """
                INSERT INTO agent.agent_knowledge
                    (doc_id, title, content, content_vector, related_keys, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id, title) DO UPDATE SET
                    content = EXCLUDED.content,
                    content_vector = EXCLUDED.content_vector,
                    related_keys = EXCLUDED.related_keys,
                    content_hash = EXCLUDED.content_hash
                """,
                (doc_id, c["title"], c["content"], vector, c["related_keys"], hashes[idx]),
            )
        conn.commit()
        print(f"Upserted {len(to_embed_idx)} chunk(s) into agent.agent_knowledge.")


if __name__ == "__main__":
    main()
