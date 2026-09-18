# CLAUDE.md — ig-unsender

> Documento de contexto para Claude Code. Léelo entero antes de tocar nada.
> Aquí está la idea del proyecto, el stack, la arquitectura, las decisiones ya
> tomadas (y por qué), las trampas conocidas, y el trabajo pendiente especificado.

---

## 1. Qué es esto

**ig-unsender** anula (*unsend*) en bloque los mensajes que el usuario ha enviado
en sus DMs de Instagram, dejando las conversaciones intactas. Al anular, el
mensaje desaparece **también para el destinatario**.

Instagram no ofrece borrado masivo: solo mensaje a mensaje, manteniendo pulsado →
"Anular envío". Este proyecto automatiza eso con control fino sobre qué se borra
y qué no.

**El usuario es Jimmy** (Jaime Durán Sanz), habla español y prefiere que el
código, los comentarios y la interfaz estén en español. Es quien va a operar la
herramienta sobre su propia cuenta.

### Contexto crítico: esto borra de verdad y no hay deshacer

Este no es un CRUD cualquiera. Cada decisión de diseño está subordinada a dos hechos:

1. **La anulación es irreversible.** No hay papelera. Si el motor borra algo que no
   debía, se acabó.
2. **Automatizar Instagram viola sus ToS.** El riesgo real es un bloqueo temporal o
   un checkpoint de actividad sospechosa en la cuenta del usuario.

De ahí salen los principios que **no debes relajar** al modificar el proyecto:

- **Fail-safe, nunca fail-open.** Ante cualquier duda, no borrar. La acción por
  defecto es `keep`. Un bloque de criterios vacío no casa con nada.
- **La whitelist es sagrada.** Se evalúa antes que todo y nada la sobrescribe. Si
  añades una ruta nueva de borrado, tiene que pasar por `FilterEngine.decide()`.
- **Simulación primero.** `dry_run: true` de fábrica. El borrado real exige
  confirmación explícita (`BORRAR` por consola o por API).
- **Ir despacio es una feature, no un bug.** Las pausas aleatorias y el cupo diario
  son lo que protege la cuenta. No "optimices" quitándolas ni paralelices los
  borrados.
- **Solo mensajes propios.** El motor filtra por `user_id == me_id` antes de nada.
  Nunca debe existir un camino que toque mensajes ajenos.
- **Todo auditable.** Cada decisión se escribe con su motivo. Si añades un tipo de
  decisión, añade su entrada de auditoría.

---

## 2. Stack

| Pieza | Tecnología | Por qué esta |
|---|---|---|
| Lenguaje | Python 3.10+ | Usa `X \| None`, `tuple[bool, str]`; con `from __future__ import annotations` en todos los módulos |
| API de Instagram | `instagrapi==3.0.4` | Envuelve la **API privada** de la app móvil. No existe API oficial para DMs |
| Config | `PyYAML==6.0.2` | YAML legible y comentable; el usuario lo edita a mano además del dashboard |
| Modelos | `pydantic` (transitivo) | Lo arrastra instagrapi. **No lo pinches en `requirements.txt`** o romperás la instalación. El proyecto usa `dataclasses` propias, no pydantic |
| API web | `fastapi==0.115.6` | Async nativo para el SSE, tipado, cero boilerplate |
| Servidor | `uvicorn[standard]==0.34.0` | ASGI para FastAPI |
| Frontend | HTML + CSS + JS vanilla | **Sin build, sin CDN, sin framework.** Un solo archivo autocontenido |
| Tiempo real | Server-Sent Events | Unidireccional (servidor→cliente), que es todo lo que hace falta. Más simple que WebSockets y reconecta solo |

### Decisiones de stack que NO debes cambiar sin motivo fuerte

**Frontend vanilla y sin CDN.** `src/web/static/index.html` es un único archivo con
markup, estilos y lógica dentro. Es deliberado: la herramienta tiene que arrancar en
la máquina del usuario años después, sin `npm install`, sin red, sin que un CDN
caído la rompa. Si necesitas un gráfico, dibújalo con CSS como ya se hace en
`renderBars()`. No metas React, ni Tailwind, ni Chart.js.

**instagrapi se importa de forma perezosa.** `src/client.py` hace el `from instagrapi
import Client` **dentro** de `build_client()`, no arriba del módulo. Así el dashboard
arranca y deja configurar el stack aunque instagrapi no esté instalado todavía, y
los tests corren sin la librería. Mantén ese patrón.

**No hay base de datos.** El estado vive en JSON y JSONL planos. Es suficiente para
la escala real (decenas de miles de mensajes de un solo usuario) y hace que todo
sea inspeccionable con un editor de texto. No metas SQLite salvo que haya una razón
de peso.

---

## 3. Arquitectura

### Mapa de archivos

```
ig-unsender/
├── run.py                  # CLI: --dry-run, --go, --explain, --yes
├── dashboard.py            # Servidor del panel web
├── test_rules.py           # 25 tests del motor de decisión
├── test_engine.py          # 24 tests de integración con Instagram falso
├── config.example.yaml     # Plantilla documentada → se copia a config.yaml
├── requirements.txt
├── .gitignore
├── README.md               # Documentación para el usuario final
├── CLAUDE.md               # Este archivo
└── src/
    ├── config.py           # Carga, validación y guardado del YAML
    ├── client.py           # Login + sesión persistente (instagrapi)
    ├── filters.py          # Clasificación de tipos + motor de decisión
    ├── engine.py           # Recorrido, borrado, reintentos, Controller
    ├── runner.py           # Ciclo de vida de una ejecución (EngineRunner)
    ├── state.py            # Anulados + cupo diario (reanudación)
    ├── audit.py            # Registro append-only JSONL + export CSV
    ├── stats.py            # Métricas en vivo
    ├── events.py           # Bus de eventos para el streaming
    └── web/
        ├── server.py       # API FastAPI + endpoint SSE
        └── static/index.html
```

### Flujo de datos

```
config.yaml
    │
    ▼
load_config() ──► Config (dataclasses)
    │
    ├──► FilterEngine  (whitelist + reglas ordenadas)
    │
    ▼
EngineRunner.start()  ──► hilo daemon
    │
    ▼
Engine.run()
    │
    ├─► build_client()          login / sesión reutilizada
    ├─► _fetch_threads()        lista de chats del alcance
    │
    └─► para cada chat:
          _fetch_messages()
              └─► para cada mensaje MÍO:
                    MessageCtx.build()        normaliza el mensaje
                    State.is_done()?          ¿ya lo hice? → saltar
                    FilterEngine.decide()     → Decision(action, reason, rule)
                        │
                        ├─ protected  → stats + evento + auditoría
                        ├─ keep       → stats + evento + auditoría
                        └─ delete     → dry_run? simular
                                        : _delete_with_retry()
                                          → State.mark_done() + save()
                                          → _cooldown()  (pausa aleatoria)
                    │
                    ▼
            EventBus.publish() ──► cola por suscriptor
                                        │
                                        ▼
                              /api/events (SSE) ──► dashboard
```

### Responsabilidad de cada módulo

**`config.py`** — Define el esquema completo como dataclasses (`Config`,
`MatchCriteria`, `Rule`, `Whitelist`, `ScopeCfg`, `BehaviorCfg`, `DashboardCfg`,
`AuditCfg`...). `load_config()` mergea el YAML sobre los valores por defecto y
acepta credenciales por variable de entorno (`IG_USERNAME`, `IG_PASSWORD`,
`IG_TOTP_SEED`), que **tienen prioridad** sobre el archivo. `save_config()` escribe
desde el dashboard haciendo antes `config.yaml.bak`, e ignora las credenciales que
llegan como `"********"` para no machacar la contraseña real.

Detalle: `Whitelist.from_dict()` acepta los criterios anidados bajo `match:` o
directamente en la raíz de `whitelist:`. Es azúcar para que el YAML a mano sea más
cómodo. Al guardar siempre se normaliza a la forma anidada.

**`filters.py`** — El cerebro. Tres partes:

- `classify(msg)` mapea el `item_type` crudo de la API a una de 16 categorías
  legibles. Para `item_type == "media"` mira `media.media_type` (1=foto, 2=vídeo,
  8=álbum). Cualquier tipo desconocido cae en `"other"`: **nunca lanza excepción**,
  porque Instagram añade tipos nuevos sin avisar.
- `MessageCtx.build(msg, thread)` normaliza mensaje + hilo en un objeto plano con
  todo lo que hace falta para decidir (texto agregado de cuerpo/caption/link,
  timestamp sin tzinfo, participantes en minúscula, tipo, longitud, preview).
- `FilterEngine.decide(ctx)` devuelve un `Decision(action, reason, rule)`.

También tiene `validate()`, que detecta regex rotas, tipos inventados, reglas
inalcanzables tras un `catch_all` y acciones inválidas. Sus avisos se muestran en el
Panel del dashboard y en `--explain`.

**`engine.py`** — `Engine` recorre y ejecuta; `Controller` es el semáforo
compartido (pausa/reanudar/parar) con `EngineState` como máquina de estados. La
lógica de reintentos vive en `_delete_with_retry()`: backoff exponencial con jitter,
detección de rate-limit por coincidencia de texto en la excepción
(`RATE_LIMIT_HINTS`), y parada de emergencia tras N errores consecutivos.

**`runner.py`** — `EngineRunner` gestiona el ciclo de vida: arranca el motor en un
hilo daemon, recarga la config en cada `start()`, y **reutiliza el cliente de
instagrapi entre ejecuciones** para no re-loguearse (que es lo que más sospechas
levanta). Lo usan tanto la CLI como el dashboard, así que el comportamiento es
idéntico se lance desde donde se lance.

**`state.py`** — Conjunto de claves `"thread_id:message_id"` ya anuladas más un
contador por día natural. Escritura atómica (`.tmp` + `replace`) para no dejar el
archivo a medias si se corta la luz. Es lo que permite reanudar sin repetir trabajo.

**`audit.py`** — JSONL append-only. Nunca reescribe ni borra líneas: es un registro
histórico, no un caché. Ofrece `query()` con filtros, `summary()` agregado por
acción y por run, y `to_csv()` para exportar.

**`events.py`** — Bus pub/sub en memoria, thread-safe, con historial acotado
(`deque(maxlen=400)`) para que un cliente que se conecta tarde vea contexto. Si una
cola de suscriptor pasa de `MAX_QUEUE` se considera cliente colgado y se descarta,
para que un navegador zombi no coma memoria indefinidamente.

**`stats.py`** — Contadores con lock. `snapshot()` devuelve un dict serializable que
va tanto al SSE como a `/api/status`.

**`web/server.py`** — FastAPI. Auth por token comparado con `secrets.compare_digest`
(aceptado por query param, header `Authorization: Bearer` o cookie). El endpoint SSE
es un generador async que **no bloquea el event loop**: drena la cola con
`get_nowait()` y duerme 0.25s entre vueltas, con keepalive cada ~10s.

---

## 4. Las decisiones de diseño que importan

### 4.1 Whitelist en OR, reglas en AND

**Esto es lo más importante del proyecto y lo más fácil de romper por accidente.**

`_match_criteria(c, ctx, mode)` combina los criterios de un bloque de dos formas:

- `mode="and"` → **REGLAS**. Todos los criterios rellenados deben cumplirse. Es lo
  que permite pedir "fotos **Y** anteriores a 2025" sin llevarse por delante las
  fotos recientes.
- `mode="or"` → **WHITELIST**. Basta con que uno se cumpla para proteger. Así
  "proteger a `mi_pareja`" y "proteger notas de voz" son dos escudos independientes,
  no una condición conjunta.

La primera implementación hacía todo en AND y la whitelist no protegía casi nada: si
ponías `usernames: [mi_pareja]` y `types: [voice]`, solo salvaba las notas de voz de
mi_pareja. **Lo cazó `test_engine.py`, no la revisión del código.** Los tests que
cubren esto son "usuario protegido", "palabra protegida", "tipo protegido (gif)" y
"whitelist gana a delete". Si los tocas, entiende primero por qué están.

Corolario que conviene tener claro: proteger a un usuario protege el **chat entero**
con esa persona, no mensajes sueltos. Es intencionado y está afirmado explícitamente
en el test "el usuario protege el chat entero, no solo un mensaje".

### 4.2 Jerarquía de decisión

```
1. WHITELIST   → PROTECTED. Intocable. Gana sobre todo.
2. REGLAS      → en orden; la PRIMERA que casa decide (delete | keep).
3. POR DEFECTO → rules.default_action (recomendado y por defecto: keep).
```

Un bloque de criterios sin nada relleno **no casa nunca**, salvo `catch_all: true`.
Esto es a propósito: una regla mal escrita no debe borrar nada.

### 4.3 Motivos legibles en cada decisión

`Decision` lleva `reason` y `rule`. Ese texto viaja al log, al evento SSE, a la tabla
del dashboard y a la línea de auditoría. Es lo que le permite al usuario entender
*por qué* el motor marcó algo, que en una herramienta irreversible es la diferencia
entre confiar y no confiar. Si añades un criterio nuevo, **devuelve un motivo
legible en español**, no un identificador técnico.

### 4.4 Estado y auditoría separados

`state.json` es operativo (mutable, compacto, responde "¿ya hice esto?").
`audit.jsonl` es histórico (append-only, verboso, responde "¿qué pasó y por qué?").
No los fusiones: tienen ciclos de vida y garantías distintas.

### 4.5 El cliente se reutiliza entre ejecuciones

`EngineRunner._client` guarda el `Client` de instagrapi después de cada run. Re-
loguearse constantemente es la señal más clara de automatización para Instagram.
La sesión además se persiste en disco (`session.json`) vía `dump_settings()` /
`load_settings()`.

---

## 5. Esquema de configuración

```yaml
account:          # username, password, totp_seed (o variables de entorno)
session:          # file: session.json
dashboard:        # host, port, auth_token, open_browser
scope:            # QUÉ SE DESCARGA de Instagram
  threads_amount: 0          # 0 = todos
  messages_per_thread: 0     # 0 = todos
  only_thread_ids: []        # si hay IDs, SOLO esos
  skip_thread_ids: []
  include_requests: false    # bandeja de solicitudes
behavior:         # RITMO Y FRENOS
  dry_run: true
  daily_limit: 200                # 0 = sin límite
  min_delay / max_delay           # pausa aleatoria entre borrados
  pause_every / long_pause_min / long_pause_max
  max_retries / backoff_base
  rate_limit_cooldown             # enfriado al detectar rate-limit
  stop_on_consecutive_errors      # 0 = desactivado
whitelist:        # PRIORIDAD ABSOLUTA (criterios en OR)
  protect_groups: false
  message_ids: []
  match: { usernames, types, keywords_any, regex_any, threads, ... }
rules:            # EN ORDEN, la primera que casa decide (criterios en AND)
  default_action: keep
  items:
    - name / action (delete|keep) / enabled / when: {criterios}
state:            # file: state.json
audit:            # file: audit.jsonl, record_keeps: true
logging:          # file, level
```

### Criterios disponibles en `when:` y en `whitelist.match:`

`catch_all` · `types` · `keywords_any` · `keywords_all` · `regex_any` ·
`before` · `after` (YYYY-MM-DD) · `min_length` · `max_length` ·
`usernames` · `threads` · `is_group` · `case_sensitive`

### Tipos de mensaje reconocidos

`text` · `photo` · `video` · `voice` · `gif` · `link` · `share` · `story` ·
`reel` · `disappearing` · `like` · `location` · `profile` · `call` · `system` · `other`

La lista canónica está en `filters.KNOWN_TYPES`. Si añades uno, añádelo también al
mapa `ITEM_TYPE_MAP` y al comentario de `config.example.yaml`. El dashboard la lee
sola vía `/api/meta`, no hay que tocar el HTML.

---

## 6. API HTTP

Todos los endpoints salvo `/` y `/health` exigen token.

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Sirve el dashboard; con `?token=` fija la cookie |
| GET | `/health` | Healthcheck sin auth |
| GET | `/api/status` | Estado, stats, cupo, avisos de config |
| GET | `/api/meta` | Tipos conocidos, rutas |
| GET/POST | `/api/config` | Lee (con credenciales redactadas) / guarda con backup |
| POST | `/api/control/start` | Arranca. Real exige `{"dry_run": false, "confirm": "BORRAR"}` |
| POST | `/api/control/{pause\|resume\|stop}` | Control de ejecución |
| GET | `/api/threads` | Inventario de chats para construir la whitelist |
| GET | `/api/audit` | Consulta con `limit`, `search`, `action`, `run_id` |
| GET | `/api/audit.csv` | Export CSV |
| GET | `/api/events` | **SSE**: `log`, `decision`, `stats`, `status`, `thread` |

---

## 7. Rarezas de instagrapi que te vas a encontrar

Es un envoltorio de la API **privada** de la app móvil. No hay contrato estable.

Superficie que consume el proyecto, **verificada contra instagrapi 3.0.4**:
`direct_threads(amount, selected_filter, box)` · `direct_messages(thread_id, amount)` ·
`direct_message_delete(thread_id, message_id) -> bool` · `direct_pending_inbox(amount)` ·
`direct_thread(thread_id, amount)` · `private_request(path, params)` ·
`totp_generate_code(seed)` · `dump_settings(path)` / `load_settings(path)` ·
`exceptions.LoginRequired`. Todas existen con firmas compatibles. Si actualizas la
versión de instagrapi, **vuelve a comprobar esta lista antes de nada**: es una
librería de API privada y rompe compatibilidad con frecuencia.

- **`item_type` cambia sin avisar.** Instagram añade tipos (`xma_media_share`,
  `generic_xma`...). `classify()` cae a `"other"` en vez de reventar. Mantén ese
  comportamiento.
- **`media_type`**: 1 = foto, 2 = vídeo, 8 = carrusel. Solo existe dentro de un
  `item_type == "media"`.
- **`timestamp`** puede venir con tzinfo. `MessageCtx.build()` lo normaliza quitando
  la zona. Si comparas fechas en otro sitio, haz lo mismo o compararás naive contra
  aware y petará.
- **`direct_message_delete(thread_id, item_id)`** es la llamada de anulación. No
  devuelve nada útil más allá de no lanzar excepción.
- **Rate limits** llegan como excepciones genéricas con texto variable. Se detectan
  por coincidencia de subcadena en `RATE_LIMIT_HINTS` (`"please wait"`, `"429"`,
  `"spam"`...). Si ves un mensaje nuevo de rate-limit en producción, añádelo a esa
  tupla.
- **2FA**: `cl.totp_generate_code(seed)` genera el código desde la semilla TOTP. Sin
  semilla hay que pedirlo por consola, por eso el dashboard **exige** la semilla (no
  hay nadie mirando la terminal).
- **`LoginRequired`** es la excepción de sesión caducada. `build_client()` la captura
  y hace login limpio.
- **`direct_messages(thread_id, amount=0)`** pagina internamente pero **no siempre
  devuelve el histórico completo** en hilos muy largos. Esto es exactamente lo que
  resuelve la mejora 1 de abajo.

---

## 8. Tests

```bash
python test_rules.py     # 25 tests del motor de decisión
python test_engine.py    # 24 tests de integración
```

Ninguno necesita credenciales ni red. `test_engine.py` usa `FakeClient`, un doble que
imita la superficie de instagrapi que consume el motor (`user_id`, `direct_threads`,
`direct_messages`, `direct_message_delete`).

**Ejecútalos siempre después de tocar `filters.py`, `engine.py` o `config.py`.** No
son decorativos: ya cazaron el fallo de diseño OR/AND de la whitelist.

Qué cubren: clasificación de los 16 tipos, precedencia de la whitelist sobre las
reglas, orden de evaluación, validación de config (regex rotas, tipos inventados,
reglas inalcanzables), dry-run vs borrado real, que nunca se tocan mensajes ajenos,
reanudación e idempotencia, cupo diario y parada ordenada.

**Si añades una feature, añade su test.** Especialmente si toca la ruta de borrado.

---

## 9. Convenciones de código

- **Español** para comentarios, docstrings, mensajes de log, motivos de decisión y
  toda la interfaz. **Inglés** para identificadores de código (`FilterEngine`,
  `decide`, `msg_type`). Es la mezcla que ya está por todo el proyecto: mantenla.
- `from __future__ import annotations` en la cabecera de todos los módulos.
- Dataclasses para configuración y estructuras de datos. Nada de pydantic propio.
- Type hints en todas las firmas públicas.
- Los comentarios explican **por qué**, no qué. Si el código necesita que expliques
  qué hace, reescribe el código.
- Nada de `print()` en `src/`: usa el logger del módulo y `EventBus`. Los `print()`
  solo viven en los entrypoints (`run.py`, `dashboard.py`) y en los tests.
- Manejo de errores: captura ancho en los bordes (llamadas a la API), estrecho en el
  núcleo. Un fallo en un mensaje nunca debe tumbar la ejecución entera.

### Archivos que NUNCA se commitean

`config.yaml` · `config.yaml.bak` · `session.json` · `state.json` · `audit.jsonl` ·
`*.log`. Ya están en `.gitignore`. El primero lleva credenciales en claro y el
segundo la sesión de Instagram.

---

## 10. Comandos

```bash
# Instalación
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml

# Uso
python run.py --explain      # árbol de decisión, sin tocar nada
python run.py --dry-run      # simulación
python run.py --go           # borrado real (pide escribir BORRAR)
python run.py --go --yes     # sin preguntar, para cron
python dashboard.py          # panel web, imprime URL con token
python dashboard.py --port 9000 --no-browser

# Tests
python test_rules.py && python test_engine.py
```

---

---

# TRABAJO PENDIENTE

Dos mejoras acordadas con el usuario, en orden de prioridad. Están especificadas con
el detalle suficiente para implementarlas sin adivinar. Léelas enteras antes de
empezar: la 1 toca el núcleo del motor, la 2 es más autocontenida.

---

## MEJORA 1 — Paginación por cursor dentro de cada hilo

### El problema

`instagrapi.direct_messages(thread_id, amount=0)` pagina internamente, pero en hilos
muy largos (años de conversación, decenas de miles de mensajes) **no devuelve el
histórico completo de forma fiable**. Además carga todo en memoria de golpe y, si la
descarga falla a mitad, se pierde el progreso de ese hilo entero.

Hoy el usuario lo esquiva relanzando el proceso varios días (el `state.json` evita
repetir), pero es un parche: si la primera página nunca llega al fondo del hilo, los
mensajes más antiguos **no se alcanzan nunca**, por muchas pasadas que hagas.

### La solución

Implementar paginación explícita por cursor, con consumo incremental y persistencia
del cursor para poder reanudar exactamente donde se quedó.

### Detalle técnico de la API privada

**Verificado leyendo el código fuente de `instagrapi 3.0.4`**
(`instagrapi.mixins.direct.DirectMixin.direct_thread`), no de memoria:

```
GET direct_v2/threads/{thread_id}/

params = {
    "visual_message_return_type": "unseen",
    "direction": "older",
    "seq_id": "40065",
    "limit": "20",
    "cursor": "<oldest_cursor de la página anterior>",   # omitir en la primera
}

respuesta = {
  "thread": {
      "items": [ {...}, {...} ],   # dicts crudos
      "oldest_cursor": "...",       # cursor para la siguiente página
      ...
  }
}
```

Se llama con `client.private_request(path, params=params)`. Los items crudos se
convierten a objetos `DirectMessage` con
`from instagrapi.extractors import extract_direct_message` (verificado que existe).

**La señal de fin es `oldest_cursor` vacío o ausente**, no un campo `has_older`.
instagrapi no consulta `has_older` en ningún momento; su bucle es literalmente
`cursor = thread.get("oldest_cursor")` y corta con `if not cursor: break`. Si la
respuesta real trae `has_older`, úsalo solo como señal secundaria de confirmación,
nunca como única condición de parada.

**Importante:** la paginación va de más nuevo a más antiguo (`direction: "older"`).
El cursor se basa en los propios items, así que ir borrando por el camino **no**
debería desplazarlo — pero verifícalo en el primer hilo real y, si detectas saltos,
la alternativa es recolectar todos los IDs del hilo antes de empezar a borrar.

**Por qué `amount=0` significa "todos":** el bucle de `direct_thread` corta con
`if not cursor or (amount and len(items) >= amount)`. Con `amount=0`, la segunda
condición es siempre falsa, así que solo para cuando se acaba el cursor. Y el
truncado final `if amount: items = items[:amount]` tampoco se aplica. El proyecto
depende de este comportamiento en `ScopeCfg.messages_per_thread` y
`ScopeCfg.threads_amount`.

### Implementación propuesta

**Nuevo módulo `src/pagination.py`:**

```python
def iter_thread_messages(
    client,
    thread_id: str,
    page_size: int = 20,
    max_pages: int = 0,        # 0 = sin límite
    start_cursor: str = "",
    should_continue: Callable[[], bool] = lambda: True,
) -> Iterator[tuple[list, str, bool]]:
    """Genera (mensajes_de_la_pagina, cursor_siguiente, hay_mas) página a página.

    Importa instagrapi de forma perezosa, como el resto del proyecto.
    Comprueba should_continue() entre páginas para respetar pausa/parada.
    """
```

**Cambios en `state.py`:**

```python
self.thread_cursors: Dict[str, str] = {}   # thread_id -> último cursor consumido
self.thread_complete: Set[str] = set()     # hilos recorridos hasta el fondo

def set_cursor(self, thread_id: str, cursor: str) -> None: ...
def get_cursor(self, thread_id: str) -> str: ...
def mark_thread_complete(self, thread_id: str) -> None: ...
def is_thread_complete(self, thread_id: str) -> bool: ...
```

Persistirlos en el JSON junto a `deleted` y `daily_counts`. **Mantén
retrocompatibilidad**: un `state.json` existente sin esas claves debe cargar sin
error (usa `data.get(..., {})`).

**Cambios en `config.py` → `ScopeCfg`:**

```python
page_size: int = 20              # mensajes por petición
max_pages_per_thread: int = 0    # 0 = hasta el fondo
resume_cursors: bool = True      # reanudar desde el cursor guardado
```

**Cambios en `engine.py`:**

Convertir `_fetch_messages()` en un generador `_iter_messages(thread)` que:
- salte los hilos ya marcados como completos cuando `resume_cursors` esté activo;
- arranque desde `state.get_cursor(tid)` si lo hay;
- pase `should_continue=lambda: not self.ctl.stopping` a `iter_thread_messages`;
- guarde el cursor **después de procesar cada página**, no antes (si petamos a
  mitad de página, esa página se reintenta entera; es idempotente porque
  `state.is_done()` filtra lo ya borrado);
- marque el hilo completo cuando `has_older` sea `False`;
- publique un evento `page` al bus con `{thread_id, page_num, fetched, has_more}`
  para que el dashboard muestre el avance dentro de un hilo largo.

Reestructurar `_process()` para consumir el generador: el bucle interno pasa de
"para cada mensaje de la lista" a "para cada página → para cada mensaje de la
página". El resto de la lógica de decisión no cambia.

**Cambios en el dashboard:**

- KPI o línea de progreso con "página N del hilo X".
- En *Alcance*, un campo para `page_size` y `max_pages_per_thread`.
- Un botón "Reiniciar cursores" que limpie `thread_cursors` y `thread_complete`
  (para forzar un repaso completo). Debe pedir confirmación.

### Cómo verificarlo

- Extender `FakeClient` en `test_engine.py` con un `private_request()` simulado que
  devuelva páginas encadenadas con `oldest_cursor` y `has_older`, y un hilo de ~55
  mensajes con `page_size=20` (3 páginas).
- Tests nuevos: que recorre las 3 páginas; que guarda el cursor tras cada una; que
  al reanudar arranca desde el cursor guardado y no repite; que `max_pages_per_thread`
  corta donde debe; que una parada a mitad deja el cursor en un punto válido; que
  marca el hilo completo al agotar `has_older`.
- Los 49 tests actuales deben seguir pasando.

### Riesgos

Es la parte más acoplada a la API privada de todo el proyecto. **Deja
`direct_messages()` como camino alternativo** (un flag `scope.use_pagination: true`
por defecto, con fallback al método antiguo si la paginación falla o el endpoint
cambia). Si Instagram cambia el endpoint, el usuario tiene que poder volver al
comportamiento anterior sin que tú estés delante.

---

## MEJORA 2 — Modo programado

### El problema

Con muchos miles de mensajes, el trabajo se reparte en tandas diarias por el
`daily_limit`. Hoy el usuario tiene que relanzar el proceso a mano cada día, o
montarse un cron (documentado en el README, pero es fricción y además el cron no
sabe nada del estado del dashboard).

### La solución

Un planificador dentro del propio dashboard que lance las tandas solo, con ventana
horaria, historial y apagado automático cuando ya no queda trabajo.

### Implementación propuesta

**Nuevo bloque en `config.py`:**

```python
@dataclass
class SchedulerCfg:
    enabled: bool = False
    mode: str = "daily"              # daily | interval
    at: str = "03:00"                # hora local, para mode=daily
    interval_minutes: int = 0        # para mode=interval
    window_start: str = ""           # "02:00" — solo ejecutar dentro de la ventana
    window_end: str = ""             # "07:00"
    dry_run: bool = False            # con qué modo lanza
    max_runs_per_day: int = 1
    stop_when_done: bool = True      # autodesactivarse al no quedar trabajo
    skip_if_quota_exhausted: bool = True
```

**Nuevo módulo `src/scheduler.py`:**

```python
class Scheduler:
    """Lanza ejecuciones automáticas. Hilo daemon, tick cada 30s."""

    def __init__(self, runner: EngineRunner): ...
    def start(self) -> None: ...          # arranca el hilo
    def stop(self) -> None: ...
    def next_run_at(self) -> datetime | None: ...
    def _tick(self) -> None: ...
```

Lógica de cada tick:
1. Si `not cfg.scheduler.enabled` → nada.
2. Si `runner.busy` → nada (nunca solapar ejecuciones).
3. Si hay `window_start`/`window_end` y la hora actual cae fuera → nada.
4. Si ya se alcanzó `max_runs_per_day` hoy → nada.
5. Si `skip_if_quota_exhausted` y `state.remaining_today(daily_limit) <= 0` → nada.
6. Si toca según `mode` → `runner.start(dry_run=cfg.scheduler.dry_run)`, registrar
   la fecha/hora del disparo y publicar un evento `scheduler` al bus.

**Detalles que no puedes saltarte:**

- **Compara fechas, no deltas.** Guarda `last_run_date` (un `date`, no un
  timestamp) y comprueba `date.today() != last_run_date`. Si comparas deltas de
  tiempo, un portátil que se suspende o un cambio de hora te dispara ejecuciones
  duplicadas o se salta días.
- **Persiste `last_run_date` y el contador de runs del día en `state.json`**, no en
  memoria. Si el usuario reinicia el dashboard, el planificador no debe volver a
  disparar el mismo día.
- **Horario de verano.** España cambia de hora dos veces al año. Usa hora local del
  sistema y acepta que la madrugada del cambio puede ejecutarse una hora antes o
  después. No merece la pena complicarlo más, pero coméntalo en el código.
- **`stop_when_done`**: si una ejecución termina con `deleted == 0`,
  `would_delete == 0` y sin errores, significa que ya no queda nada que hacer.
  Poner `scheduler.enabled = false`, guardarlo en el config y publicar un evento
  destacado para que el usuario lo vea al volver.

**Cambios en `dashboard.py`:** instanciar el `Scheduler` y arrancarlo antes de
`uvicorn.run()`. Pararlo limpiamente al recibir la señal de apagado.

**Nuevos endpoints en `web/server.py`:**

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/scheduler` | Config + `next_run_at` + historial de disparos |
| POST | `/api/scheduler` | Guardar config del planificador |
| POST | `/api/scheduler/trigger` | Disparo manual inmediato (respeta los frenos) |

**Nueva sección "Programación" en el dashboard:**
Interruptor de activación, selector `daily`/`interval`, hora, ventana horaria,
modo (simulación o real), y una tarjeta grande con la próxima ejecución en cuenta
atrás. Historial de los últimos disparos con su resultado.

⚠️ **Salvaguarda de seguridad:** activar el planificador **en modo real** debe exigir
la misma confirmación escribiendo `BORRAR` que el botón de borrado manual. Programar
borrados irreversibles desatendidos es la acción más peligrosa de toda la
herramienta y no puede quedar a un clic de distancia.

### Cómo verificarlo

- Tests con el reloj inyectado (parámetro `now` en `_tick()`, o `monkeypatch` de
  `datetime.now`): que dispara a la hora configurada; que no dispara dos veces el
  mismo día; que respeta la ventana horaria; que no solapa si `runner.busy`; que se
  autodesactiva con `stop_when_done`; que sobrevive a un reinicio sin re-disparar.
- Prueba manual: `mode: interval`, `interval_minutes: 1`, `dry_run: true`, y
  comprobar que encadena ejecuciones sin solaparse.

### Nota para el usuario

El planificador **requiere que `dashboard.py` siga corriendo**. Para un servidor
headless que va a estar encendido igualmente, cron sigue siendo más robusto (ya está
documentado en el README). Deja eso claro en la UI: el planificador es para cuando el
usuario tiene el dashboard abierto, no un sustituto de cron.

---

## Ideas de backlog (no acordadas, solo apuntadas)

No las implementes sin preguntar al usuario primero.

- **Export del "qué se borraría"** a CSV desde una simulación, para revisarlo en una
  hoja de cálculo antes de dar el visto bueno.
- **Perfiles de configuración**: varios juegos de reglas guardados y conmutables
  (`--profile limpieza-antigua`).
- **Papelera lógica**: volcar el contenido completo del mensaje al audit antes de
  anularlo, para que quede al menos el texto aunque el mensaje desaparezca. Ojo con
  el tamaño del JSONL y con que el audit pasaría a contener datos sensibles.
- **Métricas de riesgo**: estimar la probabilidad de checkpoint según el ritmo y
  avisar antes de pasarse.
- **Modo multi-cuenta**: varios `config.yaml` gestionados desde el mismo dashboard.
- **Notificaciones**: aviso por Telegram o email al terminar una tanda o al detectar
  un bloqueo.

---

## Antes de dar por terminada cualquier tarea

1. `python test_rules.py && python test_engine.py` — los 49 tests en verde.
2. `python run.py --explain` con `config.example.yaml` copiado a `config.yaml` — no
   debe petar ni mostrar avisos inesperados.
3. `python dashboard.py` — arranca, se abre, y las seis (o siete) secciones
   renderizan sin errores en la consola del navegador.
4. Si tocaste el esquema de config, actualiza `config.example.yaml` **con sus
   comentarios en español** y el README.
5. Si tocaste la ruta de borrado, añade tests y **revisa que la whitelist sigue
   ganando**.
6. Ningún archivo con credenciales o estado en el commit.

---

## Herramienta hermana: `ig-espejo/`

En el mismo repositorio, pero **independiente**: carpeta propia, `config.yaml`
propio, panel propio (puerto 8788) y tests propios. No comparte código con
ig-unsender; sí comparte la **sesión** (`session.file: "../session.json"`),
porque dos logins distintos duplicarían justo la señal que más sospechas
levanta.

Analiza seguidores: mutuos, quién no te sigue de vuelta, quién te dejó de
seguir entre escaneos y quién se fue y ha vuelto, más una heurística de cuentas
sospechosas con motivos legibles. Guarda una foto fija por escaneo en
`ig-espejo/snapshots/` (ignorado por git: son datos personales de terceros).

**Es de solo lectura por decisión de diseño.** No sigue, no deja de seguir, no
bloquea. Si algún día se añade dejar de seguir en bloque, tiene que pasar por la
misma ceremonia que el borrado: simulación primero, confirmación escrita y cupo
diario.

Antes de darla por terminada: `cd ig-espejo && python test_analysis.py` (40 tests,
sin red ni credenciales).

### Nota transversal sobre los tests

`test_engine.py` se ejecuta **con y sin `instagrapi` instalado**, y debe pasar en
los dos casos: `src/pagination.py` usa el extractor real de la librería cuando
está disponible y uno propio de respaldo cuando no. Por eso el `FakeClient` imita
la forma real de la API (`item_id`, timestamp en microsegundos) y no una
simplificada: un doble infiel haría pasar los tests mintiendo sobre producción.
