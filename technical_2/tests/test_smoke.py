"""Smoke test end-to-end de improved_version.py con dobles (sin red ni API key).

Sustituye los módulos ``openai`` y ``chromadb`` por fakes deterministas antes
de importar el pipeline, y verifica la orquestación completa: ingesta
idempotente, IDs deterministas, recuperación filtrada, construcción de
mensajes, presupuesto de contexto, persistencia atómica del historial y errores
accionables.

Ejecución::

    python3 tests/test_smoke.py
"""

import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- dobles de openai y chromadb -------------------------------------------------

def _fake_vec(text: str) -> list[float]:
    # determinista: mismo texto -> mismo vector
    return [float(sum(map(ord, text)) % 997), 1.0, 0.5]


class FakeEmbeddings:
    def create(self, *, input, model):
        data = [types.SimpleNamespace(embedding=_fake_vec(t)) for t in input]
        return types.SimpleNamespace(data=data)


class FakeCompletions:
    def create(self, *, model, messages):
        ctx = messages[0]["content"].split("<contexto>")[1].split("</contexto>")[0]
        n_docs = ctx.count("---") + 1 if ctx.strip() else 0
        content = f"RESPUESTA(turnos_previos={len(messages) - 2}, contexto_docs={n_docs})"
        message = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.embeddings = FakeEmbeddings()
        self.chat = types.SimpleNamespace(completions=FakeCompletions())


fake_openai = types.ModuleType("openai")
fake_openai.OpenAI = FakeOpenAI
sys.modules["openai"] = fake_openai


class FakeCollection:
    def __init__(self):
        self.store: dict[str, tuple[str, list[float]]] = {}

    def upsert(self, ids, documents, embeddings, metadatas=None):
        for i, key in enumerate(ids):
            self.store[key] = (documents[i], embeddings[i])

    def count(self):
        return len(self.store)

    def query(self, query_embeddings, n_results, include=None):
        items = list(self.store.items())[:n_results]
        return {
            "ids": [[key for key, _ in items]],
            "documents": [[value[0] for _, value in items]],
            "distances": [[0.1] * len(items)],
        }


class FakeChromaClient:
    def __init__(self, path=None):
        self._collection = FakeCollection()

    def get_or_create_collection(self, name=None, metadata=None):
        return self._collection


fake_chromadb = types.ModuleType("chromadb")
fake_chromadb.PersistentClient = FakeChromaClient
fake_chromadb.api = types.SimpleNamespace(ClientAPI=object)
sys.modules["chromadb"] = fake_chromadb


# --- pruebas ----------------------------------------------------------------------

sys.path.insert(0, str(REPO_ROOT))
import improved_version as iv  # (requiere los dobles ya instalados)

TMP = Path(tempfile.mkdtemp(prefix="rag_smoke_"))

cfg = iv.Config(
    api_key="test-key",
    persist_dir=TMP / "chroma",
    history_path=TMP / "history.json",
    max_distance=None,
)
pipeline = iv.RagPipeline(cfg)

# 1. ingesta: descarta vacíos
count = pipeline.ingest(["hola mundo", "adios mundo", "   ", ""], source="test")
assert count == 2, count
assert pipeline._collection.count() == 2

# 2. idempotencia: reingestar no duplica
pipeline.ingest(["hola mundo"], source="test")
assert pipeline._collection.count() == 2, pipeline._collection.count()

# 3. IDs deterministas
assert iv._doc_id("x") == iv._doc_id("x") and iv._doc_id("x") != iv._doc_id("y")

# 4. retrieve + build_messages
chunks = pipeline.retrieve("pregunta")
assert len(chunks) == 2
messages = iv.build_messages(
    "q", [iv.Turn("u1", "a1")], chunks, char_budget=10_000, history_window=6
)
assert messages[0]["role"] == "system" and "<contexto>" in messages[0]["content"]
assert messages[1] == {"role": "user", "content": "u1"}
assert messages[2] == {"role": "assistant", "content": "a1"}
assert messages[-1] == {"role": "user", "content": "q"}

# 5. ventana de historial
turns = [iv.Turn(f"u{i}", f"a{i}") for i in range(10)]
messages = iv.build_messages("q", turns, [], char_budget=100, history_window=6)
assert len(messages) == 1 + 6 * 2 + 1, len(messages)  # system + 6 turnos (2 c/u) + pregunta
assert messages[1]["content"] == "u4"  # solo los últimos 6 turnos

# 6. presupuesto de contexto (trunca y avisa por log)
big = [iv.RetrievedChunk("a", "x" * 600, 0.1), iv.RetrievedChunk("b", "y" * 600, 0.2)]
context = iv.build_context(big, char_budget=700)
assert len(context) <= 700 and "yyy" not in context

# 7. filtro de distancia
pipeline.cfg = iv.Config(
    api_key="k", max_distance=0.05, persist_dir=cfg.persist_dir,
    history_path=cfg.history_path,
)
assert pipeline.retrieve("q") == []  # 0.1 > 0.05 -> todo descartado

# 8. ask completo: respuesta + historial persistido y recargable
pipeline.cfg = cfg
answer = pipeline.ask("pregunta de prueba")
assert answer.startswith("RESPUESTA")
history = iv.load_history(cfg.history_path)
assert len(history) == 1 and history[0].user == "pregunta de prueba"
assert history[0].assistant == answer
pipeline.ask("segunda pregunta")
history = iv.load_history(cfg.history_path)
assert len(history) == 2
assert not cfg.history_path.with_suffix(".json.tmp").exists()  # sin .tmp residual

# 9. answer() no toca disco
before = cfg.history_path.read_text()
pipeline.answer("sin persistir")
assert cfg.history_path.read_text() == before

# 10. falta de API key -> error claro
try:
    iv.RagPipeline(iv.Config(api_key=""))
    raise AssertionError("debía lanzar RuntimeError")
except RuntimeError as error:
    assert "OPENAI_API_KEY" in str(error)

shutil.rmtree(TMP)
print("SMOKE OK: 10/10 verificaciones pasadas")
