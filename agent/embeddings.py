"""Local embeddings via BAAI/bge-small-en-v1.5 (sentence-transformers).

Free, fully local, no API key — chosen over all-MiniLM-L6-v2 (the more
commonly-defaulted small model) because it benchmarks meaningfully better
on retrieval (MTEB) while staying small enough to run fine on CPU. 384
dims — matches vector(384) columns in db/init.sql.

bge models need an instruction prefix on QUERIES (not on the documents
being indexed) to perform as intended — this is the model's own documented
convention, not something invented here. Skipping it silently degrades
retrieval quality without erroring, so it'd be an easy thing to miss.
"""

from sentence_transformers import SentenceTransformer

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed content being indexed (no query prefix)."""
    vectors = _get_model().encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]


def embed_query(text: str) -> list[float]:
    """Embed a search query (with the bge-required prefix)."""
    vector = _get_model().encode(_QUERY_PREFIX + text, normalize_embeddings=True)
    return vector.tolist()
