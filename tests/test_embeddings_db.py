"""Chunk embeddings and semantic search. Real Postgres + pgvector; a fake embedder, no network."""

import pytest

from neuroscope.embed import EMBEDDING_DIM, VertexEmbedder, embed_chunks, status, to_pgvector
from neuroscope.pubmed import parse_articles, store
from neuroscope.search import search, semantic_search
from tests.test_documents_db import article, article_set

# Words that mean the same thing share a direction, as a real embedding model arranges.
CONCEPTS = [("swelling", "edema", "oedema"), ("sleep", "insomnia"), ("memory", "cognitive", "cognition")]


class FakeEmbedder:
    def __init__(self, model="fake-model-1"):
        self.model = model
        self.calls = []

    def _vector(self, text):
        v = [0.0] * EMBEDDING_DIM
        v[-1] = 0.01                                   # never a zero vector
        for i, words in enumerate(CONCEPTS):
            v[i] = float(sum(text.lower().count(w) for w in words))
        return v

    def embed_documents(self, texts):
        self.calls.append(len(texts))
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


EDEMA = article("900000011", "Lecanemab safety findings.", [
    (None, "Lecanemab was associated with cerebral edema on MRI in a minority of participants.")])
SLEEP = article("900000012", "Suvorexant and insomnia.", [
    (None, "Suvorexant improved insomnia in people with mild dementia.")])
OTHER = article("900000013", "Donepezil pharmacokinetics.", [
    (None, "Plasma concentrations of donepezil were dose proportional.")])


@pytest.fixture()
def docs(conn):
    store(conn, parse_articles(article_set(EDEMA, SLEEP, OTHER)))
    return conn


def test_to_pgvector_checks_the_dimension():
    assert to_pgvector([0.5] * EMBEDDING_DIM).startswith("[0.5,0.5,")
    with pytest.raises(ValueError):
        to_pgvector([0.1, 0.2])


def test_embed_chunks_is_resumable_and_records_the_model(docs):
    conn, fake = docs, FakeEmbedder()
    assert status(conn, fake.model) == {"chunks": 3, "embedded": 0, "other_model": 0, "to_embed": 3}
    assert embed_chunks(conn, fake, limit=2, batch_size=2) == {"embedded": 2, "remaining": 1}
    assert embed_chunks(conn, fake, batch_size=2) == {"embedded": 1, "remaining": 0}
    assert embed_chunks(conn, fake) == {"embedded": 0, "remaining": 0}          # nothing left to do
    assert fake.calls == [2, 1]
    rows = conn.execute("SELECT embedding_model, embedded_at IS NOT NULL, vector_dims(embedding) FROM chunks").fetchall()
    assert rows == [("fake-model-1", True, EMBEDDING_DIM)] * 3


def test_changing_the_model_reembeds_everything(docs):
    conn = docs
    embed_chunks(conn, FakeEmbedder("fake-model-1"))
    newer = FakeEmbedder("fake-model-2")
    assert status(conn, newer.model) == {"chunks": 3, "embedded": 0, "other_model": 3, "to_embed": 3}
    assert embed_chunks(conn, newer) == {"embedded": 3, "remaining": 0}


def test_a_changed_abstract_loses_its_embedding(docs):
    conn, fake = docs, FakeEmbedder()
    embed_chunks(conn, fake)
    revised = article("900000011", "Lecanemab safety findings.", [(None, "Revised text about lecanemab and edema.")])
    store(conn, parse_articles(article_set(revised)))
    assert status(conn, fake.model)["to_embed"] == 1


def test_semantic_search_finds_meaning_that_keyword_search_misses(docs):
    conn, fake = docs, FakeEmbedder()
    embed_chunks(conn, fake)
    # The question shares no words with the abstract ("swelling" vs "edema").
    assert search(conn, "brain swelling") == []
    hits = semantic_search(conn, fake, "brain swelling", limit=1)
    assert [(h.title, h.drugs) for h in hits] == [("Lecanemab safety findings.", ["lecanemab"])]
    assert hits[0].rank > 0.9 and hits[0].url == "https://pubmed.ncbi.nlm.nih.gov/900000011/"
    # Ordered by similarity, one row per document.
    assert [h.title for h in semantic_search(conn, fake, "trouble with sleep", limit=3)][0] == "Suvorexant and insomnia."
    # The drug filter applies here too.
    assert semantic_search(conn, fake, "brain swelling", drug="Belsomra") != hits
    assert [h.drugs for h in semantic_search(conn, fake, "brain swelling", drug="Belsomra")] == [["suvorexant"]]
    with pytest.raises(ValueError):
        semantic_search(conn, fake, "brain swelling", drug="notadrug")


def test_semantic_search_ignores_chunks_from_another_model(docs):
    conn = docs
    embed_chunks(conn, FakeEmbedder("fake-model-1"))
    assert semantic_search(conn, FakeEmbedder("fake-model-2"), "brain swelling") == []


def test_vertex_embedder_explains_missing_setup(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        VertexEmbedder()
