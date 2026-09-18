# ig-unsender

Anula (*unsend*) en bloque los mensajes que **tú** has enviado en tus DMs de Instagram,
dejando las conversaciones intactas. Al anular, el mensaje desaparece también para la otra persona.

No es un script suelto: es un stack con motor de reglas, whitelist de prioridad absoluta,
auditoría append-only y un dashboard web para conducirlo todo en tiempo real.

---

## ⚠️ Antes de nada

- Automatizar acciones en Instagram va **contra sus Términos de Servicio**. El riesgo real es
  un bloqueo temporal o un checkpoint de actividad sospechosa. El stack va despacio y con pausas
  aleatorias justo para minimizar eso, pero el riesgo nunca es cero.
- Anular **no se puede deshacer**. Si la otra persona ya lo leyó o hizo captura, eso no se recupera.
- **Haz copia antes**: Instagram → Centro de cuentas → *Tu información* → *Descargar tu información* → *Mensajes*.
- Borrar o desactivar tu cuenta **no** borra tus mensajes al otro lado: siguen ahí como
  "Usuario de Instagram". Anular el envío es el único camino.

---

## Instalación

```bash
cd ig-unsender
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## Arranque rápido (dashboard)

```bash
python dashboard.py
```

Imprime en consola una URL con un token de acceso. Ábrela y tendrás todo: cuenta, reglas,
whitelist, alcance, controles y registro en vivo. Sin ese token no se entra.

## Arranque rápido (CLI)

```bash
python run.py --explain    # te enseña cómo quedaría el árbol de decisión, sin tocar nada
python run.py --dry-run    # simulación: dice qué borraría y por qué
python run.py --go         # borrado real (pide escribir BORRAR)
```

---

## Cómo se decide cada mensaje

Tres niveles, de mayor a menor prioridad:

```
1. WHITELIST   →  intocable. Gana sobre todo lo demás.       (criterios en OR)
2. REGLAS      →  en orden; la primera que casa decide.      (criterios en AND)
3. POR DEFECTO →  rules.default_action (recomendado: keep)
```

Esa diferencia OR/AND es deliberada y es la clave para entenderlo:

- En la **whitelist**, cada protección actúa por su cuenta. Si proteges al usuario `mi_pareja`
  **y** el tipo `voice`, salvas el chat entero con esa persona **y además** todas tus notas de voz
  en cualquier chat. Es una red de seguridad: cuantas más cosas metas, más se salva.
- En una **regla**, todos los criterios que rellenes deben cumplirse a la vez. Así puedes pedir
  exactamente "fotos **y** anteriores a 2025" sin llevarte por delante las fotos recientes.

### Tipos de mensaje reconocidos

`text` · `photo` · `video` · `voice` · `gif` · `link` · `share` · `story` · `reel` ·
`disappearing` · `like` · `location` · `profile` · `call` · `system` · `other`

### Criterios disponibles

| Criterio | Qué hace |
|---|---|
| `types` | Tipos de mensaje (lista de arriba) |
| `keywords_any` | Contiene **alguna** de estas palabras |
| `keywords_all` | Contiene **todas** estas palabras |
| `regex_any` | Casa con alguna expresión regular |
| `before` / `after` | Rango de fechas (`YYYY-MM-DD`) |
| `min_length` / `max_length` | Longitud del texto en caracteres |
| `usernames` | Solo en chats con estas personas |
| `threads` | Solo en estos IDs de chat |
| `is_group` | Solo grupos / solo chats individuales |
| `case_sensitive` | Distinguir mayúsculas (por defecto no) |
| `catch_all` | Casa con todo — ponla siempre la última |

### Ejemplo

```yaml
whitelist:
  protect_groups: true          # ningún grupo se toca
  match:
    usernames: [mi_pareja]      # su chat entero, intacto
    types: [voice]              # las notas de voz no se recuperan jamás
    keywords_any: ["te quiero", "dirección"]

rules:
  default_action: keep          # si nada casa, no se toca

  items:
    - name: "Borrar datos sensibles en cualquier sitio"
      action: delete
      when:
        keywords_any: ["contraseña", "iban", "dni"]

    - name: "Borrar fotos y vídeos anteriores a 2025"
      action: delete
      when:
        types: [photo, video]
        before: "2025-01-01"

    - name: "Borrar texto en chats individuales"
      action: delete
      when:
        types: [text]
        is_group: false
```

---

## El dashboard

| Sección | Para qué |
|---|---|
| **Panel** | KPIs en vivo, controles (simular / borrar / pausar / parar), log en streaming, desglose por tipo y tabla de decisiones recientes con su motivo. |
| **Reglas** | Editor visual: añadir, reordenar, activar/desactivar reglas sin tocar el YAML. |
| **Whitelist** | Personas, tipos y palabras protegidas, con chips. Tu red de seguridad. |
| **Alcance** | Cuánto se descarga, y un explorador de chats para limitar la prueba a uno solo o proteger a alguien de un clic. |
| **Auditoría** | Consulta del registro histórico con búsqueda y filtros, más exportación a CSV. |
| **Ajustes** | Cuenta, ritmo, reintentos y frenos de seguridad. |

Todo lo que cambies se guarda en tu `config.yaml` (con backup automático en `config.yaml.bak`).

**Seguridad del dashboard:** escucha solo en `127.0.0.1` y exige un token generado en cada arranque.
Maneja tu sesión de Instagram, así que si lo expones a la red (`--host 0.0.0.0`), ponlo detrás de
HTTPS o de un túnel SSH y fija un `auth_token` largo en el config.

---

## Frenos de seguridad

El stack está construido asumiendo que algo va a salir mal:

- **Simulación por defecto.** `dry_run: true` de fábrica. El borrado real exige escribir `BORRAR`.
- **Whitelist con prioridad absoluta**, evaluada antes que cualquier regla.
- **Cupo diario** con contador persistente: aunque lances el proceso cinco veces, respeta el tope.
- **Pausas aleatorias** entre borrados y pausa larga cada N, para no ir en ráfaga.
- **Backoff exponencial** ante errores y enfriado largo al detectar rate-limit.
- **Parada de emergencia** tras N errores seguidos (señal típica de bloqueo temporal).
- **Reanudación**: `state.json` recuerda cada mensaje anulado; la siguiente pasada no repite.
- **Parada ordenada** con Ctrl+C o desde el dashboard: nunca corta a mitad de una llamada.
- **Auditoría append-only**: cada decisión queda escrita con su motivo, y nunca se reescribe.
- **Solo toca tus mensajes.** Los de la otra persona ni se miran.

---

## Ruta recomendada

1. `python run.py --explain` — comprueba que el árbol de decisión dice lo que crees.
2. Rellena la whitelist **antes** de nada. Es más fácil proteger de más y luego aflojar.
3. `python dashboard.py` → **Simular**. Mira la tabla de decisiones: cada fila lleva su motivo.
4. En *Alcance*, pon un solo chat en "Solo estos IDs" y haz ahí tu primer borrado real.
5. Si todo cuadra, quita el filtro y déjalo correr con un `daily_limit` moderado (150–300).
6. Revisa la *Auditoría* al terminar y expórtala a CSV si quieres guardarla.

### Muchos miles de mensajes

Reparte en varios días. `state.json` evita repetir, así que basta con relanzarlo:

```bash
0 3 * * *  cd /ruta/a/ig-unsender && .venv/bin/python run.py --go --yes >> cron.log 2>&1
```

---

## Estructura

```
ig-unsender/
├── run.py                  # CLI (--dry-run, --go, --explain)
├── dashboard.py            # servidor del panel web
├── test_rules.py           # tests del motor de decisión
├── test_engine.py          # test de integración con un Instagram falso
├── config.example.yaml     # plantilla documentada → copia a config.yaml
├── requirements.txt
├── .gitignore
├── README.md
└── src/
    ├── config.py           # carga, validación y guardado del YAML
    ├── client.py           # login + sesión persistente (instagrapi)
    ├── filters.py          # clasificación de tipos + motor de decisión
    ├── engine.py           # recorrido, borrado, reintentos, controlador
    ├── runner.py           # ciclo de vida de una ejecución
    ├── state.py            # anulados + cupo diario (reanudación)
    ├── audit.py            # registro append-only + export CSV
    ├── stats.py            # métricas en vivo
    ├── events.py           # bus de eventos para el streaming
    └── web/
        ├── server.py       # API FastAPI + SSE
        └── static/index.html
```

## Tests

```bash
python test_rules.py     # 25 tests del motor de decisión
python test_engine.py    # 24 tests de integración, sin tocar la red
```

Ninguno necesita credenciales ni conexión: usan un Instagram simulado.

## Archivos que genera

| Archivo | Qué es | ¿Subir a git? |
|---|---|---|
| `config.yaml` | Tu configuración, con credenciales | ❌ nunca |
| `session.json` | Sesión de Instagram | ❌ nunca |
| `state.json` | Qué se ha anulado ya | ❌ |
| `audit.jsonl` | Registro de decisiones | ❌ |
| `unsender.log` | Log de ejecución | ❌ |

Todos están ya en `.gitignore`.

---

## Descargo

Herramienta para gestionar **tus propios** mensajes. Úsala bajo tu responsabilidad. Instagram no la
respalda y su API privada puede cambiar en cualquier momento y dejarla inservible.
