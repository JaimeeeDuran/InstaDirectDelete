"""El cerebro: compara listas, clasifica relaciones y puntúa sospechas.

Todo lo de aquí son funciones puras sobre diccionarios planos. No sabe nada de
Instagram ni de red, y por eso se puede testear entero sin credenciales.

Vocabulario del proyecto (el mismo que ve el usuario en la interfaz):
  mutuos            os seguís los dos
  no_te_siguen      tú les sigues, ellos no te devuelven el seguimiento
  fans              te siguen y tú no les sigues
  perdidos          te seguían en el escaneo anterior y ya no
  nuevos            no te seguían en el escaneo anterior y ahora sí
  recuperados       te dejaron de seguir en algún momento y han vuelto
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Un "registro de usuario" normalizado tiene esta forma (todo opcional salvo pk):
#   {pk, username, full_name, is_private, is_verified, sin_foto}
# El escaneo profundo añade, cuando está disponible:
#   {media_count, follower_count, following_count, biography, is_business}

DIGITOS = re.compile(r"\d")


# ---------------------------------------------------------------------------
# Heurística de cuentas sospechosas
# ---------------------------------------------------------------------------

def score_sospecha(rec: Dict[str, Any], cfg) -> Tuple[int, List[str]]:
    """Puntúa 0-100 y devuelve los motivos legibles que han sumado.

    Los motivos importan más que la cifra: son lo que permite al usuario
    darnos la razón o quitárnosla de un vistazo. Si añades una señal nueva,
    añade también su motivo en español.
    """
    score = 0
    motivos: List[str] = []

    # --- Señales baratas: salen de la propia lista de seguidores ---
    if rec.get("sin_foto"):
        score += cfg.peso_sin_foto
        motivos.append("Sin foto de perfil")

    digitos_seguidos = _racha_digitos(rec.get("username") or "")
    if digitos_seguidos >= cfg.minimo_digitos:
        score += cfg.peso_username_numerico
        motivos.append(f"El usuario lleva {digitos_seguidos} dígitos seguidos")

    if not (rec.get("full_name") or "").strip():
        score += cfg.peso_sin_nombre
        motivos.append("Sin nombre completo")

    # --- Señales caras: solo existen si se hizo escaneo profundo ---
    if "media_count" in rec:
        if rec.get("media_count", 0) == 0:
            score += cfg.peso_sin_publicaciones
            motivos.append("Ninguna publicación")

        if not (rec.get("biography") or "").strip():
            score += cfg.peso_sin_bio
            motivos.append("Sin biografía")

        seguidores = rec.get("follower_count", 0) or 0
        siguiendo = rec.get("following_count", 0) or 0
        if siguiendo > cfg.ratio_extremo * max(seguidores, 1):
            score += cfg.peso_ratio_extremo
            motivos.append(
                f"Sigue a {siguiendo} cuentas y solo le siguen {seguidores}"
            )

    return min(score, 100), motivos


def banda(score: int, cfg) -> str:
    if score >= cfg.umbral_alto:
        return "alto"
    if score >= cfg.umbral_medio:
        return "medio"
    return "bajo"


def _racha_digitos(texto: str) -> int:
    """Longitud de la racha más larga de dígitos seguidos."""
    mejor = actual = 0
    for ch in texto:
        if ch.isdigit():
            actual += 1
            mejor = max(mejor, actual)
        else:
            actual = 0
    return mejor


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------

@dataclass
class Report:
    generado_en: str = ""
    cuenta: str = ""
    total_seguidores: int = 0
    total_siguiendo: int = 0

    mutuos: List[Dict[str, Any]] = field(default_factory=list)
    no_te_siguen: List[Dict[str, Any]] = field(default_factory=list)
    fans: List[Dict[str, Any]] = field(default_factory=list)

    nuevos: List[Dict[str, Any]] = field(default_factory=list)
    perdidos: List[Dict[str, Any]] = field(default_factory=list)
    recuperados: List[Dict[str, Any]] = field(default_factory=list)
    nuevos_seguidos: List[Dict[str, Any]] = field(default_factory=list)
    dejaste_de_seguir: List[Dict[str, Any]] = field(default_factory=list)

    sospechosos: List[Dict[str, Any]] = field(default_factory=list)

    verificados: int = 0
    privados: int = 0
    protegidos: int = 0
    reciprocidad: float = 0.0      # % de los que sigues que te devuelven
    correspondencia: float = 0.0   # % de los que te siguen a los que sigues
    comparado_con: str = ""        # ts del snapshot anterior, si lo hubo
    escaneo_profundo: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generado_en": self.generado_en,
            "cuenta": self.cuenta,
            "totales": {
                "seguidores": self.total_seguidores,
                "siguiendo": self.total_siguiendo,
                "mutuos": len(self.mutuos),
                "no_te_siguen": len(self.no_te_siguen),
                "fans": len(self.fans),
                "verificados": self.verificados,
                "privados": self.privados,
                "protegidos": self.protegidos,
                "sospechosos": len(self.sospechosos),
            },
            "metricas": {
                "reciprocidad": round(self.reciprocidad, 1),
                "correspondencia": round(self.correspondencia, 1),
                "ratio": round(
                    self.total_seguidores / self.total_siguiendo, 2
                ) if self.total_siguiendo else 0.0,
            },
            "movimientos": {
                "nuevos": self.nuevos,
                "perdidos": self.perdidos,
                "recuperados": self.recuperados,
                "nuevos_seguidos": self.nuevos_seguidos,
                "dejaste_de_seguir": self.dejaste_de_seguir,
                "comparado_con": self.comparado_con,
            },
            "listas": {
                "mutuos": self.mutuos,
                "no_te_siguen": self.no_te_siguen,
                "fans": self.fans,
            },
            "sospechosos": self.sospechosos,
            "escaneo_profundo": self.escaneo_profundo,
        }


def analyze(
    actual: Dict[str, Any],
    anterior: Optional[Dict[str, Any]] = None,
    historico: Optional[List[Dict[str, Any]]] = None,
    cfg: Any = None,
) -> Report:
    """Compara un snapshot con el anterior y devuelve el informe completo.

    Args:
        actual: snapshot recién tomado.
        anterior: snapshot previo, si existe (para los movimientos).
        historico: snapshots anteriores al `anterior`, en orden cronológico.
                   Solo se usan para detectar "recuperados".
        cfg: objeto Config (usa .suspects y .is_whitelisted()).
    """
    seguidores: Dict[str, Dict] = dict(actual.get("followers") or {})
    siguiendo: Dict[str, Dict] = dict(actual.get("following") or {})

    rep = Report(
        generado_en=actual.get("ts", ""),
        cuenta=(actual.get("me") or {}).get("username", ""),
        total_seguidores=len(seguidores),
        total_siguiendo=len(siguiendo),
        escaneo_profundo=bool(actual.get("deep")),
    )

    pks_seguidores = set(seguidores)
    pks_siguiendo = set(siguiendo)

    def _marcar(rec: Dict[str, Any]) -> Dict[str, Any]:
        """Añade la marca de protegido sin tocar el registro original."""
        salida = dict(rec)
        if cfg is not None and cfg.is_whitelisted(rec.get("username", "")):
            salida["protegido"] = True
        return salida

    rep.mutuos = _ordenar(_marcar(seguidores[p]) for p in pks_seguidores & pks_siguiendo)
    rep.no_te_siguen = _ordenar(_marcar(siguiendo[p]) for p in pks_siguiendo - pks_seguidores)
    rep.fans = _ordenar(_marcar(seguidores[p]) for p in pks_seguidores - pks_siguiendo)

    rep.verificados = sum(1 for r in seguidores.values() if r.get("is_verified"))
    rep.privados = sum(1 for r in seguidores.values() if r.get("is_private"))
    rep.protegidos = sum(
        1 for r in seguidores.values()
        if cfg is not None and cfg.is_whitelisted(r.get("username", ""))
    )

    if rep.total_siguiendo:
        rep.reciprocidad = len(rep.mutuos) / rep.total_siguiendo * 100
    if rep.total_seguidores:
        rep.correspondencia = len(rep.mutuos) / rep.total_seguidores * 100

    # ----- Movimientos respecto al escaneo anterior -----
    if anterior:
        prev_seguidores = dict(anterior.get("followers") or {})
        prev_siguiendo = dict(anterior.get("following") or {})
        prev_pks_seg = set(prev_seguidores)
        prev_pks_sig = set(prev_siguiendo)

        rep.comparado_con = anterior.get("ts", "")
        rep.nuevos = _ordenar(_marcar(seguidores[p]) for p in pks_seguidores - prev_pks_seg)
        # Los perdidos ya no están en la lista actual: su ficha sale del snapshot viejo.
        rep.perdidos = _ordenar(_marcar(prev_seguidores[p]) for p in prev_pks_seg - pks_seguidores)
        rep.nuevos_seguidos = _ordenar(_marcar(siguiendo[p]) for p in pks_siguiendo - prev_pks_sig)
        rep.dejaste_de_seguir = _ordenar(
            _marcar(prev_siguiendo[p]) for p in prev_pks_sig - pks_siguiendo
        )

        # Recuperados: vuelven a estar ahora, no estaban en el anterior, pero
        # sí aparecieron en algún escaneo más viejo. Es el subconjunto de
        # "nuevos" que en realidad es un regreso.
        if historico:
            vistos_antes = set()
            for snap in historico:
                vistos_antes |= set(snap.get("followers") or {})
            regresos = (pks_seguidores - prev_pks_seg) & vistos_antes
            rep.recuperados = _ordenar(_marcar(seguidores[p]) for p in regresos)

    # ----- Sospechosos -----
    if cfg is not None and cfg.suspects.enabled:
        sospechosos = []
        for pk, rec in seguidores.items():
            if cfg.is_whitelisted(rec.get("username", "")):
                continue  # los intocables no se juzgan
            score, motivos = score_sospecha(rec, cfg.suspects)
            if score >= cfg.suspects.umbral_medio:
                sospechosos.append({
                    **rec,
                    "score": score,
                    "banda": banda(score, cfg.suspects),
                    "motivos": motivos,
                })
        rep.sospechosos = sorted(sospechosos, key=lambda r: -r["score"])

    return rep


def _ordenar(registros) -> List[Dict[str, Any]]:
    """Orden alfabético estable por usuario: la lista no debe bailar entre
    escaneos, porque el usuario la lee comparando con la vez anterior."""
    return sorted(registros, key=lambda r: (r.get("username") or "").lower())


def serie_historica(snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Serie temporal para la gráfica: un punto por escaneo."""
    serie = []
    for snap in snapshots:
        seguidores = set(snap.get("followers") or {})
        siguiendo = set(snap.get("following") or {})
        serie.append({
            "ts": snap.get("ts", ""),
            "seguidores": len(seguidores),
            "siguiendo": len(siguiendo),
            "mutuos": len(seguidores & siguiendo),
        })
    return serie


def to_csv(registros: List[Dict[str, Any]]) -> str:
    """Export plano de una lista de cuentas."""
    import csv
    import io

    campos = ["username", "full_name", "is_private", "is_verified", "sin_foto",
              "media_count", "follower_count", "following_count", "score", "motivos"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=campos, extrasaction="ignore")
    w.writeheader()
    for r in registros:
        fila = dict(r)
        if isinstance(fila.get("motivos"), list):
            fila["motivos"] = " · ".join(fila["motivos"])
        w.writerow(fila)
    return buf.getvalue()
