# ig-espejo

Análisis de seguidores de Instagram: quién te sigue, a quién sigues y —lo que
de verdad interesa— **quién no te devuelve el seguimiento**.

Herramienta hermana de `ig-unsender`, pero independiente: vive en su propia
carpeta, tiene su propia configuración y su propio panel.

> **Solo lectura.** No sigue, no deja de seguir, no bloquea y no borra nada.
> No existe ningún camino en el código que modifique tu cuenta. Lo peor que
> puede pasar aquí es que Instagram te diga "espera un rato".

---

## Qué te dice

| | |
|---|---|
| **Mutuos** | os seguís los dos |
| **No te siguen** | tú les sigues y no te devuelven el seguimiento |
| **Fans** | te siguen y tú no les sigues |
| **Nuevos** | han empezado a seguirte desde el escaneo anterior |
| **Te dejaron de seguir** | te seguían en el escaneo anterior y ya no |
| **Recuperados** | se fueron en algún momento… y han vuelto |
| **Con indicios** | cuentas con pinta de falsas, con sus motivos escritos |

Además: reciprocidad en %, verificados, privadas, evolución entre escaneos y
export a CSV de cualquier lista.

### El histórico es la gracia

Un escaneo suelto solo puede decirte quién te sigue **ahora**. Con dos ya sabes
quién se ha ido, y con tres quién se fue y volvió. Por eso cada escaneo guarda
una foto fija en `snapshots/`: es lo que convierte una lista en una historia.

---

## Instalación

```bash
cd ig-espejo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # y rellena tu usuario/contraseña
```

La sesión se comparte con `ig-unsender` (`session.file: "../session.json"`).
No es un detalle menor: re-loguearse una y otra vez es la señal más clara de
automatización que le puedes dar a Instagram. Un login, dos herramientas.

## Uso

```bash
python run.py --scan            # escaneo rápido + informe
python run.py --scan --deep     # además consulta el perfil de los más sospechosos
python run.py --report          # informe del último escaneo, sin tocar la red
python run.py --historico       # evolución de seguidores entre escaneos
python run.py --csv no_te_siguen > lista.csv

python dashboard.py             # panel web en http://127.0.0.1:8788
python dashboard.py --port 9100 --no-browser
```

Listas exportables con `--csv`: `mutuos`, `no_te_siguen`, `fans`, `nuevos`,
`perdidos`, `recuperados`, `sospechosos`.

## Rápido vs profundo

| | Escaneo rápido | Escaneo profundo |
|---|---|---|
| Peticiones | 2 | 2 + una por cuenta analizada |
| Qué obtiene | usuario, nombre, privada, verificada, si tiene foto | además: publicaciones, biografía, ratios |
| Riesgo de rate-limit | bajo | el más alto de la herramienta |

El profundo no recorre a todos tus seguidores: ordena por indicios baratos y
solo consulta a los primeros (`deep_scan_limit`, 60 por defecto), con pausas
aleatorias entre peticiones y pausas largas cada N cuentas.

## Los "indicios" no son un veredicto

La puntuación 0-100 solo sirve para **ordenar la lista**. Lo que importa son
los motivos, que van escritos al lado de cada cuenta:

- Sin foto de perfil · +30
- El usuario lleva N dígitos seguidos · +20
- Sin nombre completo · +10
- Ninguna publicación · +25 *(requiere escaneo profundo)*
- Sin biografía · +10 *(requiere escaneo profundo)*
- Sigue a muchísimas más cuentas de las que le siguen · +25 *(requiere escaneo profundo)*

Todos los pesos y umbrales se tocan desde el panel o el `config.yaml`. Y
cualquiera que esté en **intocables** no se juzga nunca, dé los indicios que dé.

## Tests

```bash
python test_analysis.py     # 40 tests, sin red ni credenciales
```

## Archivos que nunca se commitean

`config.yaml` · `config.yaml.bak` · `session.json` · `snapshots/` · `*.log`

Ya están en el `.gitignore` del repositorio.

## Lo que NO hace (y por qué)

No sigue ni deja de seguir a nadie. Podría: la API lo permite y sería el paso
"natural" después de ver quién no te corresponde. Pero dejar de seguir en
bloque es exactamente el tipo de actividad que dispara los bloqueos de
Instagram, y a diferencia de ig-unsender —donde el borrado es el objetivo
declarado y se pide confirmación escrita— aquí el objetivo es *entender*, no
actuar. Si algún día se añade, tendrá que pasar por la misma ceremonia:
simulación primero, confirmación escrita y cupo diario.
