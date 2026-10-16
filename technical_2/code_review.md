# Code review y mejora de `first_version.py`

Entrega del ejercicio: revisión del pipeline RAG de ingesta y consulta de un
compañero, con todos los problemas encontrados categorizados (bugs, seguridad,
diseño, mantenibilidad, rendimiento y robustez) y la versión mejorada
correspondiente (`improved_version.py`). El razonamiento de cada elección
técnica está en [`decisions.md`](decisions.md); instalación y ejemplos, en el
[`README.md`](README.md).

---

## 1. Resumen ejecutivo

| # | Problema | Categoría | Severidad | Corrección |
|---|----------|-----------|-----------|------------|
| 1 | API key hardcodeada en el código | Seguridad | 🔴 Crítica | Variable de entorno `OPENAI_API_KEY` |
| 2 | SDK de OpenAI obsoleto (`openai.Embedding.create`): **el código no ejecuta** con `openai>=1.0` | Bug | 🔴 Crítica | Migración al cliente `OpenAI()` v1.x |
| 3 | Una llamada API de embedding **por documento** (N+1) | Rendimiento | 🟠 Alta | Batching (hasta 64 textos por llamada) |
| 4 | IDs por índice (`str(i)`): colisionan entre ingestas | Bug | 🟠 Alta | ID determinista SHA-256 + `upsert` idempotente |
| 5 | `create_collection` en el import: falla al segundo uso | Bug | 🟠 Alta | `get_or_create_collection` perezoso |
| 6 | Historial se escribe pero **nunca se lee** | Bug | 🟠 Alta | `load_history` al arrancar |
| 7 | Chroma efímero en memoria vs. historial en disco (persistencia incoherente) | Diseño | 🟠 Alta | `PersistentClient` para ambos |
| 8 | Prompt sin límite: 5 docs gigantes + historial infinito desbordan el contexto | Bug / Coste | 🟠 Alta | Presupuesto de caracteres + ventana de historial |
| 9 | Documents concatenados sin delimitar en el system prompt (prompt injection) | Seguridad | 🟡 Media | Bloque `<contexto>` delimitado + reglas anti-inyección |
| 10 | Sin filtro de relevancia: "Responde usando: \<ruido\>" fuerza alucinaciones | Diseño | 🟡 Media | Umbral de distancia coseno configurable |
| 11 | `open("history.json", "w").write(...)`: fuga de descriptor, escritura no atómica, sin encoding | Bug / Robustez | 🟡 Media | `with`, `utf-8`, escritura atómica `.tmp` + rename |
| 12 | `ask` con efectos ocultos (muta lista, escribe disco, llama a la API) | Diseño | 🟡 Media | `answer` pura + `ask` documentada |
| 13 | Sin tipos, docstrings, logging, manejo de errores ni CLI | Mantenibilidad | 🟡 Media | Contratos claros + logging + CLI |
| 14 | Sin reintentos ante 429/5xx | Robustez | 🟡 Media | `max_retries=3` con backoff del SDK |
| 15 | Sin metadata (fuente, fecha): imposible trazar o citar | Diseño | 🟢 Baja | Metadatas `source`, `chars`, `ingested_at` |

---

## 2. Problemas detectados, en detalle

### 2.1 Bugs y corrección

**B1 — El código ni siquiera ejecuta con el SDK actual.**
`openai.Embedding.create(...)` y `openai.ChatCompletion.create(...)`
(`first_version.py:11`, `first_version.py:38`) son la API del paquete `openai`
v0.x, eliminada en la v1.0 (noviembre de 2023). Cualquier `pip install openai`
hoy instala una versión donde esas funciones no existen.
**Corrección:** cliente `OpenAI()` con `client.embeddings.create(...)` y
`client.chat.completions.create(...)` (`improved_version.py`, `RagPipeline._embed`
y `RagPipeline.answer`).

**B2 — IDs por índice de iteración.**
`ids=[str(i)]` (`first_version.py:19`) numera cada lote desde 0. Dos llamadas a
`ingest_documents` —o dos documentos en ingestas distintas con la misma
posición— producen IDs duplicados: en Chroma eso significa sobrescribir
documentos existentes (pérdida silenciosa de datos) o errores.
**Corrección:** ID determinista = `sha256(contenido)` (`_doc_id`), así el mismo
texto siempre mapea al mismo registro, y `upsert` en lugar de `add` hace la
ingesta **idempotente** (reintentar o reingestar es seguro y deduplica).

**B3 — Efectos secundarios en el import.**
`client.create_collection("docs")` en la línea 7 se ejecuta **al importar el
módulo**: un segundo import (o una colección ya existente) lanza excepción, y
no hay forma de importar el módulo para testearlo sin levantar Chroma.
**Corrección:** la colección se crea dentro de `RagPipeline.__init__` con
`get_or_create_collection`, que no falla si ya existe.

**B4 — Persistencia incoherente: el historial se escribe pero nunca se lee.**
`first_version.py:45` guarda `history.json`, pero ninguna función lo carga: tras
reiniciar el proceso el historial "persistido" se pierde. Además Chroma se crea
con `chromadb.Client()` (efímero, en memoria), así que el índice desaparece
pero el historial sobrevive: estado a medio persistir, lo peor de ambos mundos.
**Corrección:** `PersistentClient` para el índice y `load_history`/`save_history`
simétricos para el historial.

**B5 — Fuga de descriptor y escritura no atómica.**
`open("history.json", "w").write(...)` (`first_version.py:45`) no cierra el
fichero de forma determinista (depende del GC), no fuerza el flush, no usa
`encoding` y una caída a mitad de escritura deja un JSON corrupto.
**Corrección:** `with ... open(..., encoding="utf-8")` + patrón de escritura
atómica: escribir en `history.json.tmp` y `os.replace` (rename atómico). Si el
proceso muere, el fichero original queda intacto.

**B6 — Desborde de contexto sin control.**
El prompt concatena 5 documentos completos (" ".join, línea 30) más **todo** el
historial acumulado (líneas 33-36) sin ningún límite. Con documentos largos o
conversaciones largas: error de la API por exceder el límite de tokens, o coste
creciente en cada turno.
**Corrección:** `build_context` con presupuesto de caracteres (≈ 4 chars/token)
y ventana deslizante de `history_window` turnos.

**B7 — Sin manejo del caso "no hay nada relevante".**
El prompt original ordena "Responde usando: \<contexto\>" aunque la recuperación
devuelva fragmentos irrelevantes (Chroma siempre devuelve los *más cercanos*,
aunque no parezcan relacionados). Eso fuerza al modelo a responder con ruido →
alucinaciones presentadas como hechos.
**Corrección:** umbral de distancia coseno (`max_distance`) que descarta
fragmentos no relevantes; si no queda nada, el contexto va vacío y las reglas
del sistema le permiten al modelo decir "no tengo información suficiente".

### 2.2 Seguridad

**S1 — API key hardcodeada (crítico).**
`API_KEY = "sk-proj-..."` (`first_version.py:5`) versionada en git. Aunque sea
un placeholder, el patrón acaba filtrando claves reales: el historial del repo
las retiene para siempre y cualquier persona con acceso al repositorio la tiene.
**Corrección:** la clave se lee de `OPENAI_API_KEY` al construir el pipeline, y
si falta se lanza un error con instrucciones claras.
**Acción pendiente sobre el código original:** si esa clave llegó a ser real,
**hay que rotarla ya** en el panel de OpenAI; borrarla del código no basta.

**S2 — Prompt injection vía documentos.**
Los documentos recuperados se pegan directamente en el system prompt
(`"Responde usando: " + context`). Un documento malicioso ("ignora las reglas
anteriores y...") se ejecuta como instrucción con plena confianza.
**Corrección (mitigación, no solución absoluta):** el contexto va en un bloque
`<contexto>...</contexto>` delimitado y el system prompt declara explícitamente
que ese bloque son *datos de referencia, no instrucciones*. Defensa en
profundidad estándar para RAG.

**S3 — Rutas relativas dependientes del CWD.**
`"history.json"` apunta a donde sea que se haya lanzado el proceso.
**Corrección:** rutas centralizadas y configurables en `Config`.

### 2.3 Diseño y arquitectura

- **Efectos ocultos:** `ask` muta la lista `history` que le pasan, escribe en
  disco y llama a la API, todo desde una función cuyo contrato aparente es
  "devuelve una respuesta". Ahora `answer()` es pura respecto al disco y `ask()`
  concentra y documenta los efectos.
- **Configuración mágica y dispersa:** nombres de modelo, `n_results=5` y demás
  constantes incrustadas en el código. Ahora viven en un `Config` inmutable,
  inyectable y con valores por defecto documentados.
- **Sin modelo de dominio:** el historial eran tuplas `(user, assistant)` cuyo
  formato solo se sostenía porque `json` serializa tuplas como listas y
  `turn[0]`/`turn[1]` funciona igual con ambas. Ahora `Turn` y `RetrievedChunk`
  son dataclasses con nombre.
- **Acoplamiento total:** todo compartía globals de módulo. Ahora hay una clase
  `RagPipeline` que recibe sus dependencias (`openai_client`, `chroma_client`
  inyectables) → testeable con dobles sin red.
- **Sin trazabilidad:** los documentos no llevan metadata (fuente, fecha, tamaño),
  imposible citar o auditar. Ahora cada documento incluye `source`, `chars` e
  `ingested_at`.

### 2.4 Mantenibilidad

- Tipos reales en firmas (`Sequence[Turn] -> str`), docstrings en cada función
  pública y en `Config`.
- `logging` en lugar de silencio: ingesta, filtros y warnings son observables.
- Mensajes de error accionables (p. ej. "exporta OPENAI_API_KEY antes de...").
- CLI con argparse (`ingest` / `ask`), ejecutable como script y como librería.
- Longitud y complejidad por función acotadas; lógica pura (`build_messages`,
  `build_context`) separada de la de efectos (red/disco).

### 2.5 Rendimiento

- **N+1 en embeddings:** el original hacía una llamada HTTP por documento. El API
  de embeddings acepta lotes: con `embedding_batch_size=64`, indexar 1000
  documentos pasa de 1000 llamadas a 16 (menos latencia total y menos riesgo de
  rate-limit).
- **Reintentos con backoff exponencial** integrados en el SDK (`max_retries=3`):
  los 429/5xx transitorios ya no tumban la ingesta.
- **Ventana de historial:** el coste por turno deja de crecer con la longitud de
  la conversación.
- **Espacio vectorial explícito:** la colección usa distancia **coseno**
  (idiomática para embeddings de texto; el default de Chroma es L2).

### 2.6 Actualización de modelos

`text-embedding-ada-002` y el alias `gpt-4` son legados. Los defaults pasan a
`text-embedding-3-small` (más barato y con mejor calidad que ada-002) y
`gpt-4o-mini`; ambos configurables en `Config` sin tocar código.

---

## 3. Cómo usar la versión mejorada

```bash
pip install "openai>=1.0" "chromadb>=0.5"
export OPENAI_API_KEY="sk-..."          # nunca en el código ni en git

# Indexar un corpus (un párrafo por documento)
python improved_version.py ingest corpus.txt --source corpus.txt

# Preguntar (usa y actualiza history.json)
python improved_version.py ask "¿De qué trata el corpus?"

# Preguntar sin tocar el historial
python improved_version.py ask "¿...?" --no-history
```

Como librería:

```python
from improved_version import Config, RagPipeline

pipeline = RagPipeline(Config(top_k=5))
pipeline.ingest(["doc 1", "doc 2"], source="mi-fuente")
print(pipeline.ask("¿qué hay indexado?"))
```

---

## 4. Cómo se aborda una mejora de este tipo (método replicable)

El orden importa: primero lo que rompe o expone datos, luego lo estructural.

1. **Entender el flujo completo** antes de tocar nada: aquí, ingesta →
   embedding → índice vectorial → recuperación → construcción de prompt → LLM →
   persistencia de historial. Cada problema se localiza en un eslabón.
2. **Seguridad primero:** secretos en el código, inyección, datos en claro.
   Una API key filtrada no se arregla borrándola: se rota.
3. **¿Ejecuta siquiera?** Verificar contra las versiones actuales de las
   dependencias (aquí, el SDK de OpenAI había eliminado la API usada).
4. **Contratos explícitos:** tipos de entrada/salida, efectos secundarios
   declarados, errores con mensaje accionable. Si una función escribe en disco,
   que se note y se separe de la lógica pura.
5. **Idempotencia y atomicidad:** operaciones que se puedan reintentar sin
   corromper estado (upsert con IDs deterministas, escritura `.tmp` + rename).
6. **Rendimiento medido sobre el cuello real:** el N+1 de embeddings dominaba
   la latencia de ingesta; el historial ilimitado, el coste por consulta.
7. **Observabilidad:** logging de las decisiones (cuánto se filtró, cuándo se
   truncó) para poder diagnosticar en producción.
8. **Verificación:** compilar y ejecutar un smoke test con dobles del API antes
   de dar por buena la versión (ver sección 5).
9. **Dejar la lista de siguiente pasos honesta:** lo que esta versión *no*
   resuelve (sección 6).

---

## 5. Verificación realizada

- `python -m py_compile improved_version.py` — sin errores de sintaxis.
- Smoke test end-to-end con **dobles** de `openai` y `chromadb` (sin red, sin
  clave), incluido en el repositorio como `tests/test_smoke.py`:
  ```bash
  python3 tests/test_smoke.py   # → SMOKE OK: 10/10 verificaciones pasadas
  ```
  Cubre: ingesta (idempotencia y descarte de vacíos), IDs deterministas,
  recuperación con filtro de distancia, construcción de mensajes (ventana de
  historial, bloque de contexto delimitado), presupuesto de contexto, `ask`
  completo con persistencia atómica del historial, pureza de `answer` respecto
  al disco y el error claro cuando falta `OPENAI_API_KEY`.
- **No** se probó contra las APIs reales (no hay clave disponible en este
  entorno); la integración real queda pendiente de quien tenga credenciales.

## 6. Limitaciones y siguientes pasos

- **Chunking real:** se indexa por párrafo; un corpus serio requiere troceo por
  tamaño con solapamiento y limpieza.
- **Citas:** con las metadatas ya se puede pedir al modelo que cite `source`;
  queda implementarlo en el prompt.
- **Tests automatizados** (pytest) para `build_messages`, `_doc_id`,
  `save/load_history` y los umbrales de `retrieve`.
- **Evaluación de calidad RAG** (precisión/recuperación con preguntas de
  control) antes de tocar umbrales como `max_distance=0.5`, que es un valor
  razonable pero empírico.
- **Streaming** de la respuesta del chat y **límite de gasto** por llamada.
- **Gestión de secretos** de producción (vault/SSM) en lugar de variable de
  entorno plana cuando esto salga del portátil.
