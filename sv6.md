# albaranes-valuation-persistence-api (Servicio 6 / sv6)

> **Orquestador y persistencia de la valoración** — la pieza que cierra el ciclo.
> Recibe un `document_id`, llama al sv5 (motor IA), aplica un conjunto de
> **reglas deterministas** (revalidación de categoría de unidad, reconciliación
> de precios 1a/1b, matching de partida con creación de líneas derivadas,
> conversión de cantidades, cálculo de importe con descuento) y **persiste**
> el resultado en 3 tablas para que el frontend humano lo consulte.

---

## 1. ¿Qué hace exactamente?

`albaranes-valuation-persistence-api` es el **director de orquesta** del subsistema
de valoración. Su pipeline:

1. Si ya existe valoración para ese `document_id` y no se fuerza → la devuelve
   tal cual con `duplicate=true`.
2. Llama al **sv5** vía HTTP para obtener el envelope IA (matching albarán↔contrato
   + líneas sintéticas).
3. Si el envelope llega con `status="no_contract"` → persiste cabecera vacía
   con `review_required=true` y razón `no_contract_selected`.
4. Si llega `status="ok"` → ejecuta el **`ValuationBuilder`** que aplica reglas
   deterministas en **3 pasadas** (base → complementarias → sintéticas) garantizando
   que cuando una línea hereda algo de su padre, el padre ya está resuelto.
5. **Persiste atómicamente** en 3 tablas (DELETE + INSERT) y devuelve un
   resumen al llamante.

Adicionalmente expone un `GET /v1/valuation/{id}` para que el front pinte la
valoración completa con todo el detalle de scoring, partidas, conversión de
unidades y razones de revisión.

> Es un servicio **stateful por BBDD** (igual que sv3): PostgreSQL es la fuente
> de verdad de las valoraciones.

---

## 2. Lugar dentro del ecosistema (cierre del ciclo)

```
   ┌──────────────────────────┐
   │ sv3 · persister          │   ─ POST /v1/valuation/run-async (fire-and-forget)
   │ sv4 · frontend (humano)  │   ─ POST /v1/valuation/{id}/re-run (síncrono)
   │                          │   ─ GET  /v1/valuation/{id} (lectura)
   └────────────┬─────────────┘
                │
                ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  sv6 · albaranes-valuation-persistence-api                  │  ← ESTE SERVICIO
   │  (FastAPI + BackgroundTasks + SQLAlchemy)                   │
   │                                                             │
   │  ┌───────────────────────────────────────────────────────┐  │
   │  │ Pipeline run_valuation:                               │  │
   │  │   exists?force? → IA(sv5) →                           │  │
   │  │   no_contract? → builder(envelope, line_already_v)    │  │
   │  │     ├─ Pasada 1: líneas base                          │  │
   │  │     ├─ Pasada 2: complementarias declaradas           │  │
   │  │     └─ Pasada 3: sintéticas (M1-M7)                   │  │
   │  │   replace_valuation(header, lines)                    │  │
   │  └───────────────────────────────────────────────────────┘  │
   └────────────┬────────────────────────────────────────┬───────┘
                │ POST /v1/albaranes/value                │ INSERT/REPLACE
                ▼                                          ▼
   ┌─────────────────────────────┐             ┌──────────────────────────────┐
   │ sv5 · valuation-api (IA)    │             │ Postgres (BBDD compartida)   │
   │ (matching + sintéticas)     │             │  ├─ albaran_valuations       │
   └─────────────────────────────┘             │  ├─ albaran_line_valuations  │
                                                │  └─ contrato_lines_derived   │
                                                └──────────────────────────────┘
```

**El sv6 cierra el ciclo iniciado por sv1.** Lo que sale aquí (filas en
`albaran_valuations`) es lo que el frontend humano ve y aprueba/rechaza.

---

## 3. Arquitectura interna (Hexagonal / Clean)

```
albaranes-valuation-persistence-api/
├─ main.py                                                     # uvicorn.run(build_app(settings))
├─ config/
│  ├─ settings.py                                              # Pydantic-settings (sin validators cruzados)
│  ├─ logging_config.py                                        # RotatingFileHandler + consola
│  └─ unit_registry.yaml                                       # ⭐ 7 categorías de unidad + factores
├─ domain/
│  ├─ models/
│  │  ├─ contexto_linea.py                                     # Idéntico al de sv2/sv3/sv5
│  │  ├─ unit_models.py                                        # ConversionResult, UnitInfo
│  │  ├─ valuation_envelope.py                                 # Réplica del schema de salida del sv5
│  │  └─ valuation_records.py                                  # ⭐ LineValuationRecord, ValuationHeaderRecord, DerivedContratoLineRecord
│  └─ ports/
│     ├─ valuation_ia_client.py                                # Puerto cliente sv5
│     ├─ valuation_repository.py                               # initialize/get/replace/read_full
│     └─ unit_registry_port.py                                 # classify/convert
├─ application/
│  ├─ pipelines/
│  │  └─ run_valuation_pipeline.py                             # ⭐ Pipeline POST /run
│  └─ services/
│     ├─ valuation_builder.py                                  # ⭐ ORQUESTA en 3 pasadas (741 LOC)
│     ├─ unit_category_guard.py                                # Revalida categoría declarada por IA
│     ├─ price_reconciler.py                                   # Reconcilia precio 1a vs 1b vs albarán
│     ├─ partida_matcher.py                                    # Match (producto, partida) + ALM + derivadas
│     ├─ unit_converter.py                                     # Conversión via UnitRegistry
│     └─ importe_calculator.py                                 # cantidad × precio × (1 - desc/100)
├─ infrastructure/
│  ├─ database/
│  │  ├─ session_factory.py                                    # Engine simple (la BBDD ya existe — sv3 la creó)
│  │  ├─ orm_valuation_models.py                               # ⭐ 3 tablas + STUBS de tablas sv3 (truco FK sin acoplar)
│  │  └─ sqlalchemy_valuation_repository.py                    # Repo principal con DDL idempotente lazy (660 LOC)
│  ├─ clients/
│  │  └─ http_valuation_ia_client.py                           # Cliente HTTP del sv5 (timeout 300s)
│  └─ units/
│     └─ yaml_unit_registry.py                                 # Carga unit_registry.yaml + classify/convert
└─ interface_adapters/
   └─ api/
      └─ app.py                                                # FastAPI: build_app() + 5 endpoints
```

### Patrones aplicados

| Patrón                                                | Dónde                                                            | Por qué                                                                                                |
|-------------------------------------------------------|------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| **Hexagonal / Ports & Adapters**                      | `domain/ports` ↔ `infrastructure/*`                              | 3 puertos: cliente IA, repo, unit registry.                                                            |
| **Pipeline con pasadas ordenadas**                    | `ValuationBuilder.build` (3 pasadas explícitas)                  | Garantiza que cuando una complementaria/sintética hereda partida o descuento, el padre ya está resuelto. |
| **Strategy + Composition**                            | `ValuationBuilder` compone 5 servicios independientes            | Cada servicio es testeable en aislamiento (guard, reconciler, matcher, converter, calculator).         |
| **Repository**                                        | `SqlAlchemyValuationRepository`                                  | DDL idempotente, replace transaccional.                                                                |
| **Composition root**                                  | `build_app(settings)`                                            | Único sitio donde se cablean dependencias.                                                             |
| **Lazy initialization (resuelve race con sv3)**       | `repository.initialize()` perezoso                                | sv6 puede arrancar antes que sv3; las tablas se crean en la **primera** petición real, no al boot.    |
| **Stub-tables FK trick**                              | `orm_valuation_models.py` (3 stubs)                              | Permite FKs reales a tablas del sv3 sin importar su ORM completo (cero acoplamiento de paquetes).      |
| **Replace atómico**                                   | `replace_valuation(header, lines)`                               | DELETE + INSERT en transacción: la valoración es 1:1 con `document_id` y siempre coherente.            |
| **BackgroundTasks (FastAPI native)**                  | `/v1/valuation/run-async`                                         | Sin necesidad de Celery/Redis: el 202 es inmediato y la tarea corre en el mismo proceso.               |
| **DDL idempotente embebido**                          | `_DDL_STATEMENTS` en repo                                         | `CREATE TABLE IF NOT EXISTS` + índices + UQ — el repo es autoarrancable.                               |

---

## 4. Endpoints HTTP

### 4.1 `GET /health`

Devuelve estado completo del wiring. Particularmente útil el campo
`schema_ready` que indica si la inicialización perezosa ya se ha ejecutado.

```json
{
  "ok": true,
  "service": "albaranes-valuation-persistence",
  "version": "1.0.0",
  "valuation_api_base_url": "http://127.0.0.1:8002",
  "unit_registry_yaml_path": "config/unit_registry.yaml",
  "price_tolerance_pct": 2.0,
  "importe_tolerance_pct": 5.0,
  "alm_codigo_partida": "ALM",
  "db_database_url_present": true,
  "schema_ready": true
}
```

### 4.2 `POST /v1/valuation/run` *(síncrono, bloquea)*

Pipeline completo: llama a sv5, aplica reglas, persiste, devuelve resultado.

**Request body:**

```json
{
  "document_id": "8d9a2f66-...-a3e1",
  "codigo_contrato": null,
  "force": false,
  "line_already_valued": false
}
```

| Campo                  | Tipo               | Default | Significado                                                                            |
|------------------------|--------------------|---------|----------------------------------------------------------------------------------------|
| `document_id`          | `string`           | —       | UUID del merge document.                                                                |
| `codigo_contrato`      | `string \| null`   | `null`  | Override del contrato seleccionado (igual que sv5).                                     |
| `force`                | `bool`             | `false` | Si `true`, ignora valoración previa y re-ejecuta. Si `false` y existe → devuelve cache. |
| `line_already_valued`  | `bool`             | `false` | Si `true`, el `PriceReconciler` usa el precio del albarán directamente (caso facturas ya cobradas). |

**Errores:**
- `404` — documento no encontrado (KeyError).
- `400` — error de validación (ValueError).
- `500` — error inesperado (sv5 caído, BBDD, etc.).

**Respuesta `200 OK`:**

```json
{
  "valuation_id": "9f1e3a8c-...",
  "document_id": "8d9a2f66-...-a3e1",
  "status": "ok",
  "total_valorado": 1247.85,
  "total_lines": 7,
  "review_required": true,
  "review_reasons": ["at_least_one_line_requires_review", "lines_without_match:1"],
  "duplicate": false
}
```

### 4.3 `POST /v1/valuation/run-async` *(202, fire-and-forget interno)*

Idéntico al anterior pero **devuelve 202 inmediato** y procesa en
`BackgroundTasks` de FastAPI. Es lo que llama el sv3 al terminar el persist.

**Respuesta `202 Accepted`:**

```json
{
  "accepted": true,
  "document_id": "8d9a2f66-...",
  "codigo_contrato": null,
  "force": false,
  "message": "valoración encolada en background"
}
```

> ⚠ **Limitación importante**: `BackgroundTasks` corre en el **mismo proceso**.
> Si reinicias el container mientras una tarea está en vuelo, **se pierde**.
> Sin durabilidad. Lo dejo en mejoras.

### 4.4 `GET /v1/valuation/{document_id}` *(lectura)*

Devuelve la valoración completa del documento como dict JSON-serializable
con cabecera + líneas + derivadas. El front la consume para pintar la UI
de revisión.

**Respuesta `200 OK`:** estructura aproximada (depende de `read_full_valuation_json`):

```json
{
  "valuation": {
    "id": "9f1e3a8c-...",
    "document_id": "8d9a2f66-...",
    "status": "ok",
    "contrato_codigo": "C-2026-014",
    "provider_ia": "claude",
    "model_name": "claude-sonnet-4-5",
    "total_valorado": 1247.85,
    "total_lines": 7,
    "review_required": true,
    "review_reasons": ["..."],
    "created_at_utc": "2026-05-03T12:34:56Z",
    "updated_at_utc": "2026-05-03T12:34:56Z"
  },
  "lines": [
    {
      "merge_line_id": 4521,
      "line_kind": "from_albaran",
      "rol_linea": "base",
      "matched_contrato_line_id": 1187,
      "precio_unitario_final": 87.5,
      "precio_unitario_source": "both_agreed",
      "precio_unitario_agreement": "match",
      "cantidad_albaran": 7.5,
      "cantidad_convertida": 7.5,
      "factor_conversion": 1.0,
      "importe_calculado": 656.25,
      "importe_source": "calculated",
      "codigo_partida_final": "OBRA-2026-014.04",
      "partida_action": "existing_matched",
      "match_confidence_pct": 96.0,
      "match_method": "exact_concept",
      "review_required": false,
      "review_reasons": [],
      "descuento_albaran_aplicado": null,
      "ia_reasoning": "HM-25 base coincide exactamente..."
    },
    {
      "merge_line_id": null,
      "line_kind": "synthetic_modifier",
      "parent_merge_line_id": 4521,
      "modifier_source": "tiempo_exceso",
      "modifier_reason": "vehículo retrasado 33 min...",
      "descripcion_linea": "INCREMENTO POR EXCESO DE TIEMPO DE DESCARGA",
      "cantidad_albaran": 33,
      "precio_unitario_final": 0.5,
      "importe_calculado": 16.5,
      "codigo_partida_final": "OBRA-2026-014.04",
      "partida_action": "inherited_from_base_line",
      "review_required": false
    }
  ],
  "derived_lines": [/* contrato_lines_derived */]
}
```

### 4.5 `POST /v1/valuation/{document_id}/re-run` *(atajo síncrono)*

Idéntico a `/run` pero con `force=true` y `line_already_valued=false`. Es lo que
llama el front (vía sv3 PATCH con `wait_for_valuation=true`) cuando el revisor
cambia el contrato seleccionado y quiere ver el resultado al instante.

---

## 5. El `ValuationBuilder` (corazón del servicio)

Pieza más compleja del sv6 (~741 LOC). Toma el `ValuationEnvelope` del sv5 y produce
un `(ValuationHeaderRecord, list[LineValuationRecord])` listo para persistir.

### 5.1 Tres pasadas explícitas (orden importa)

```
Pasada 1 — líneas BASE
  ├─ line_kind == 'from_albaran'
  ├─ rol_linea ∈ {None, 'base'}
  └─ Matching normal: partida_matcher.match()

Pasada 2 — líneas COMPLEMENTARIAS DECLARADAS
  ├─ line_kind == 'from_albaran'
  ├─ rol_linea != 'base'  (extra_tiempo, transporte, recargo_horario,
  │                         desplazamiento, operario, otro — DECLARADAS por OCR)
  └─ Heredan partida vía contexto_linea.ref_linea_base (line_index)
       → traducido a merge_line_id → record base ya resuelto (Pasada 1)
       → resolve_partida_for_complementaria()

Pasada 3 — líneas SINTÉTICAS
  ├─ line_kind == 'synthetic_modifier'
  ├─ parent_merge_line_id apunta al merge_line_id de la línea base padre
  └─ Heredan: partida + descuento + cantidad + unidad del PARENT_RECORD
       (no del albarán directamente: el albarán de hormigón no suele
        tener unidad_medida y el record ya la resolvió)
       → resolve_partida_for_synthetic()
```

> **El orden no es decorativo**: si procesas una sintética antes de su base,
> heredas un `parent_record=None` y la línea queda sin partida y marcada
> `review_required` con `synthetic_parent_not_in_context`. La pasada-por-tipo
> garantiza la consistencia.

### 5.2 Por línea (5 pasos)

```
1. UnitCategoryGuard.resolve(line, unidad_albaran, unidad_contrato)
     → (categoria, category_match: bool, reasons)
     · Re-clasifica unidades con UnitRegistry para validar lo que dijo IA.
     · Si IA dijo category_match=true pero las categorías son distintas → fuerza false.

2. PriceReconciler.reconcile(precio_1a, precio_1b, albaran_declared, cantidad,
                             importe_albaran, line_already_valued)
     → (final_price, source ∈ {contract_line_match, pdf_inference, both_agreed,
                                albaran_declared, albaran_calculated, none},
        agreement ∈ {match, mismatch, only_1a, only_1b, neither}, reasons)

3. PartidaMatcher (depende de la pasada):
     · Pasada 1 (base):  match(line, codigo_partida_albaran, ..., contrato_lines)
        → existing_matched | new_line_created | alm_new_line_created | no_action
     · Pasada 2/3:        resolve_partida_for_{complementaria|synthetic}(partida_base)
        → inherited_from_base_line

4. UnitConverter.convert(cantidad, unidad_albaran, unidad_contrato_para_conversion)
     → (cantidad_convertida, factor, ambiguous, reasons)

5. ImporteCalculator.compute(cantidad_convertida, cantidad_albaran,
                             precio_unitario_final, importe_albaran_declarado,
                             descuento_pct)
     → (importe_calculado, source ∈ {calculated, declared_albaran, none},
        descuento_aplicado, reasons)

→ LineValuationRecord(...)
```

### 5.3 `review_required` (heurística por línea)

Una línea `from_albaran` se marca para revisión humana si:

- `not category_match` (las unidades no son comparables).
- `agreement == 'mismatch'` (precio 1a y 1b discrepan más de `PRICE_TOLERANCE_PCT`).
- `source == 'none'` (no hay precio de ningún sitio).
- `converted.ambiguous` (conversión de unidad ambigua).
- `importe_source == 'none'` (no se pudo calcular importe).
- `match_method == 'no_match'` (IA no encontró match).
- `match_confidence_pct < 60.0`.
- Línea con `contexto_linea` y `tarifa_pdf_encontrada=False` y precio no viene
  de contrato (modificador sin tarifa identificada).

Una línea `synthetic_modifier` se marca para revisión si:

- `precio_final is None` y `cantidad > 0` → razón `modifier_identified_no_tariff`.
- `parent_merge_line_id is None` → razón `synthetic_without_parent`.
- `parent_albaran is None` (parent no aparece en el contexto) → razón `synthetic_parent_not_in_context`.
- `match_confidence_pct < 60.0`.

A nivel de cabecera, `review_required = any(r.review_required for r in records)`.

---

## 6. Las reglas deterministas (servicios)

### 6.1 `PriceReconciler` — orden de prioridad de precio

**Decisión de negocio del cliente**:

| Caso                                        | source                  | agreement   | final_price                  |
|---------------------------------------------|-------------------------|-------------|------------------------------|
| `line_already_valued=True` + albaran_decl   | `albaran_declared`      | `neither`   | `precio_albaran_declarado`   |
| `line_already_valued=True` + importe/cant   | `albaran_calculated`    | `neither`   | `importe / cantidad`         |
| 1a y 1b dentro de tolerancia                | `both_agreed`           | `match`     | `mean(1a, 1b)` (atenúa redondeos) |
| 1a y 1b discrepan                           | `contract_line_match`   | `mismatch`  | **prevalece 1a** + reason     |
| Solo 1a                                     | `contract_line_match`   | `only_1a`   | 1a                           |
| Solo 1b                                     | `pdf_inference`         | `only_1b`   | 1b                           |
| Ni 1a ni 1b, hay declarado                  | `albaran_declared`      | `neither`   | declarado                    |
| Ni 1a ni 1b, calculable de imp/cant         | `albaran_calculated`    | `neither`   | importe / cantidad           |
| Nada                                        | `none`                  | `neither`   | `null`                       |

> **Regla de oro**: si la IA encontró línea exacta en la **tabla del contrato** (1a),
> esa manda incluso por encima del PDF (1b). El cliente prefiere fiarse de los datos
> estructurados del ERP.

### 6.2 `PartidaMatcher` — qué partida queda

Maneja 5 situaciones:

| `partida_action`                | Cuándo                                                                                                  | Crea derivada                  |
|---------------------------------|---------------------------------------------------------------------------------------------------------|--------------------------------|
| `alm_new_line_created`          | `codigo_partida_albaran == "ALM"` (configurable).                                                       | Sí, `partida=None`, origen `alm_acopio` |
| `existing_matched`              | Existe línea contrato con `(codigo_producto, codigo_partida_albaran)`.                                  | No                             |
| `new_line_created`              | IA casó pero la línea de contrato tiene partida distinta → derivada con la partida del albarán.         | Sí, origen `missing_partida`   |
| `inherited_from_base_line`      | Línea complementaria o sintética: hereda la partida del padre (Pasadas 2/3).                            | No                             |
| `no_action`                     | Sin matching IA y sin partida albarán.                                                                  | No                             |

> Las **líneas derivadas** se persisten en `contrato_lines_derived` y se referencian
> desde `albaran_line_valuations.derived_contrato_line_id`.

### 6.3 `UnitConverter` — cantidades a la unidad del contrato

- Si no hay cantidad → `(null, null, ambiguous=False, ['no_quantity_in_albaran'])`.
- Si no hay unidad de contrato → factor 1, asume misma unidad, reason `no_contract_unit_assumed_same`.
- Si no hay unidad de albarán → factor 1, reason `no_albaran_unit_assumed_same`.
- Si las unidades son convertibles (misma categoría) → conversión por `UnitRegistry`.
- Si las categorías no casan → `cantidad_convertida=None`, reason `unit_category_mismatch_in_conversion`.

### 6.4 `ImporteCalculator` — fórmula con descuento

```
calc_bruto    = cantidad_efectiva × precio_unitario_final
descuento_apl = 0 si None o fuera de [0,100], si no el valor
calc          = round(calc_bruto × (1 - descuento_apl/100), 2)
```

Tolerancia con `importe_albaran_declarado` (configurable, default 5%):
- Coincide → se usa el **declarado** (más fiel al documento fuente).
- No coincide → se usa el calculado y reason `declared_vs_calculated_mismatch`.

Casos especiales:
- `cantidad = 0` → importe = 0.
- `cantidad > 0` y `precio = null` → importe `null` (Forma C: modificador no tarifado).
- `cantidad = null` → importe `null`.

### 6.5 `UnitCategoryGuard` — re-validación de la IA

```
si IA dijo category_match=false → respetar
si albaran y contrato son misma categoría conocida → ok
si una es 'unknown' y la otra no → partial_unknown, category_match=false
si ambas son 'unknown' → ok pero reason both_units_unknown
si ambas conocidas y distintas → fuerza category_match=false con reason hard_mismatch
```

Esto es **crítico**: si la IA equivoca la categoría (p. ej. "ml = mililitros"
cuando el contrato dice "ml = metro lineal"), el guard lo detecta y la línea
va a revisión.

### 6.6 `UnitRegistry` (YAML) — 7 categorías

```yaml
categories:
  mass:    base: kg     # kg, t (×1000), g (×0.001), tn, tonelada, ...
  volume:  base: m3     # m3, l (×0.001), dm3, cm3, cl
  length:  base: m      # m, ml (=metro lineal), cm, mm, km
  area:    base: m2     # m2, cm2, ha
  count:   base: ud     # ud, pza, caja, palet, ...
  time:    base: h      # h, min, dia, jornada
  lump_sum: base: pa    # solo se convierte consigo misma
```

> **Conflicto explícito documentado** en el YAML: `ml` por defecto es
> *metro lineal* (length) porque en construcción es lo abrumadoramente
> mayoritario. Si en algún proyecto futuro recibes `ml` como mililitros,
> hay que moverlo manualmente a `volume`.

---

## 7. Modelo de datos (PostgreSQL)

### 7.1 Tablas creadas por sv6

#### `albaran_valuations` (cabecera, 1:1 con merge document)

```
id                       VARCHAR(36) PK              -- UUID
document_id              VARCHAR(36) UQ NOT NULL     -- FK → albaran_documents_merge.id (CASCADE)
contrato_codigo          VARCHAR(64)
status                   VARCHAR(32) NOT NULL        -- pending|running|ok|failed|no_contract|partial
provider_ia              VARCHAR(32)                 -- claude|gemini|openai
model_name               VARCHAR(100)
prompt_key               VARCHAR(100)
total_valorado           DOUBLE PRECISION DEFAULT 0
total_lines              INTEGER DEFAULT 0
lines_matched_exact      INTEGER DEFAULT 0
lines_matched_semantic   INTEGER DEFAULT 0
lines_matched_price_only INTEGER DEFAULT 0
lines_unmatched          INTEGER DEFAULT 0
review_required          BOOLEAN DEFAULT FALSE
review_reasons_json      TEXT
raw_ia_envelope_json     TEXT          -- envelope completo del sv5 (puede ser MUY grande)
created_at_utc           VARCHAR(64) NOT NULL
updated_at_utc           VARCHAR(64)
```

#### `albaran_line_valuations` (líneas valoradas)

| Campo                              | Notas                                                                                       |
|------------------------------------|---------------------------------------------------------------------------------------------|
| `id`                               | INT autoincrement                                                                           |
| `valuation_id`                     | FK → albaran_valuations.id (CASCADE)                                                        |
| `merge_line_id`                    | FK → albaran_lines_merge.id (NULL para sintéticas — sub-tanda 2D)                            |
| `matched_contrato_line_id`         | FK → albaran_contrato_lines_merge.id (puede ser NULL)                                       |
| `derived_contrato_line_id`         | FK → contrato_lines_derived.id si la línea generó derivada                                  |
| `precio_unitario_*`                | `_contrato_db`, `_pdf_inferido`, `_final` + `_source` + `_agreement`                        |
| `unidad_*`                         | `_albaran`, `_contrato`, `_categoria` + `_category_match`                                   |
| `cantidad_*`                       | `_albaran`, `_convertida`, `_factor_conversion`                                             |
| `importe_*`                        | `_calculado`, `_albaran_declarado`, `_source`                                               |
| `codigo_partida_*`                 | `_albaran`, `_final`, `partida_action`                                                       |
| `match_confidence_pct/method`      | de la IA                                                                                     |
| `review_required` / `_reasons_json`| auditoría                                                                                    |
| `ia_reasoning`                     | `razon_corta` del LLM                                                                        |
| `rol_linea`                        | base, extra_tiempo, …  (sub-tanda 2C)                                                       |
| `ref_linea_base_merge_id`          | merge_id de la base (complementarias)                                                       |
| `tarifa_pdf_encontrada`            | bool                                                                                         |
| `line_kind`                        | from_albaran \| synthetic_modifier  (sub-tanda 2D)                                          |
| `parent_merge_line_id`             | merge_id del padre (sintéticas)                                                              |
| `modifier_source/_reason/descripcion_linea` | sintéticas                                                                          |
| `descuento_albaran_aplicado`       | el % que se aplicó (auditoría — tanda descuento abr 2026)                                   |

#### `contrato_lines_derived` (líneas de contrato creadas por valoración)

Para cuando el albarán requiere una partida que no existe en el contrato:
guardamos la línea derivada con `origen ∈ {missing_partida, alm_acopio}` y
asignamos su id a la valoración correspondiente.

### 7.2 STUBS de tablas del sv3 (truco brillante)

En `orm_valuation_models.py`:

```python
albaran_documents_merge_stub = Table(
    "albaran_documents_merge",
    Base.metadata,
    Column("id", String(36), primary_key=True),
)
albaran_lines_merge_stub = Table(...)
albaran_contrato_lines_merge_stub = Table(...)
```

Estos stubs **NO** se crean en `create_all()` (se pasa `tables=[...]` con solo
las nuevas), pero permiten que SQLAlchemy resuelva los `ForeignKey(...)` de
las tablas nuevas **sin importar el ORM completo del sv3**. Es **cero
acoplamiento de paquetes Python** con FKs reales en BBDD.

### 7.3 Inicialización perezosa

`build_app(settings)` **NO** llama a `repository.initialize()`. La razón está
documentada en el código:

> *"sv6 puede arrancar antes que sv3 cree sus tablas merge. Forzar la
> creación en el arranque provocaría error inmediato. Inicialización perezosa:
> cada método público del repo llama a `self.initialize()`, que es idempotente
> y solo ejecuta la DDL una vez por proceso. Cuando llegue la primera petición
> de valoración (`run-async` desde sv3, o `re-run` desde el front), el sv3 ya
> habrá creado sus tablas y nosotros crearemos las nuestras sobre ellas."*

Esto se ve en `/health` con el flag `schema_ready`:
- `false` → todavía no llegó ninguna petición real, las tablas pueden no existir.
- `true` → DDL ya ejecutado al menos una vez (idempotente).

---

## 8. Configuración (variables de entorno)

| Variable                         | Default                | Descripción                                                                  |
|----------------------------------|------------------------|------------------------------------------------------------------------------|
| `PG_HOST`                        | `localhost`            |                                                                              |
| `PG_PORT`                        | `5432`                 |                                                                              |
| `PG_DB`                          | `albaranes`            | Compartida con sv3/sv5. **sv6 escribe** (a diferencia de sv5 que solo lee).  |
| `PG_USER`                        | *obl.*                 |                                                                              |
| `PG_PASSWORD`                    | *obl.*                 |                                                                              |
| `VALUATION_API_BASE_URL`         | `http://127.0.0.1:8002`| URL del sv5.                                                                 |
| `VALUATION_API_TIMEOUT_S`        | `300.0`                | Timeout HTTP al sv5 (Claude + PDF puede tardar 60-120 s).                    |
| `UNIT_REGISTRY_YAML_PATH`        | `config/unit_registry.yaml` | Ruta al YAML de unidades.                                              |
| `PRICE_TOLERANCE_PCT`            | `2.0`                  | Tolerancia % entre precio 1a y 1b para considerarlos `match`.                |
| `IMPORTE_TOLERANCE_PCT`          | `5.0`                  | Tolerancia % entre importe declarado y calculado.                            |
| `ALM_CODIGO_PARTIDA`             | `ALM`                  | Código literal del albarán para "almacén/acopio".                            |
| `API_HOST`                       | `127.0.0.1`            |                                                                              |
| `API_PORT`                       | `8003`                 | (sv2: 8000, sv3: 8001, sv5: 8002, sv6: 8003)                                 |
| `LOG_LEVEL`                      | `INFO`                 |                                                                              |
| `LOG_DIR`                        | `logs`                 |                                                                              |
| `SERVICE_VERSION`                | `1.0.0`                |                                                                              |

> **No tiene `GRAPH_KEY`** ni configuración de SharePoint: sv6 no toca SharePoint.

> **No tiene flags de proveedor LLM**: sv6 no llama a LLMs directamente, lo
> hace sv5. Por eso el envelope que recibe ya viene digerido.

### Ejemplo `.env`

```dotenv
PG_HOST=localhost
PG_PORT=5432
PG_DB=albaranes
PG_USER=albaranes_app
PG_PASSWORD=********

VALUATION_API_BASE_URL=http://127.0.0.1:8002
VALUATION_API_TIMEOUT_S=300

UNIT_REGISTRY_YAML_PATH=config/unit_registry.yaml
PRICE_TOLERANCE_PCT=2.0
IMPORTE_TOLERANCE_PCT=5.0
ALM_CODIGO_PARTIDA=ALM

API_HOST=0.0.0.0
API_PORT=8003
LOG_LEVEL=INFO
LOG_DIR=logs
SERVICE_VERSION=1.0.0
```

---

## 9. Flujo detallado del pipeline `/run`

```
1. POST /v1/valuation/run {document_id, codigo_contrato?, force, line_already_valued}

2. RunValuationPipeline.run(request):

   a) existing = repository.get_by_document_id(document_id)
      if existing and not force:
          return RunValuationResult(duplicate=True, ...existing)

   b) envelope = ia_client.value(document_id, codigo_contrato)
        └─ POST sv5 /v1/albaranes/value (timeout 300 s)
        └─ ValuationEnvelope.model_validate(response.json())

   c) if envelope.status == "no_contract":
          empty_header = ValuationHeaderRecord(status='no_contract', review_required=True,
                                                review_reasons=['no_contract_selected'],
                                                raw_ia_envelope_json=envelope.model_dump_json())
          persisted = repository.replace_valuation(empty_header, lines=[])
          return RunValuationResult(...)

   d) header, lines = builder.build(envelope, line_already_valued)
        └─ Pasada 1: bases (rol None|base) → match() → record
        └─ Pasada 2: complementarias (rol != base) → resolve_partida_for_complementaria → record
        └─ Pasada 3: sintéticas → resolve_partida_for_synthetic → record (heredando de parent_record)
        └─ _build_header(envelope, records) con totales y match_counts

   e) header_with_envelope = header con raw_ia_envelope_json adjunto
   f) persisted = repository.replace_valuation(header_with_envelope, lines)
        ├─ initialize() perezoso
        ├─ DELETE FROM albaran_valuations WHERE document_id = :doc_id (CASCADE limpia las lines)
        ├─ INSERT INTO albaran_valuations
        ├─ INSERT INTO albaran_line_valuations × N
        ├─ INSERT INTO contrato_lines_derived × M (las que generó partida_matcher)
        └─ commit

   g) return RunValuationResult(valuation_id, total_valorado, ..., duplicate=False)
```

---

## 10. Cómo se invoca este servicio

### 10.1 Quién lo llama hoy

| Llamante                     | Endpoint                                       | Uso                                                                |
|------------------------------|------------------------------------------------|--------------------------------------------------------------------|
| **sv3** (persister)          | `POST /v1/valuation/run-async`                  | Tras cada persist con contrato seleccionado: fire-and-forget.       |
| **sv4** (frontend humano)    | `POST /v1/valuation/{id}/re-run`                | Revisor pulsa "Valorar" / cambia contrato → bloqueante con resultado. |
| **sv4** (frontend humano)    | `GET /v1/valuation/{id}`                        | Pintar la pantalla de revisión.                                     |
| Operador/script              | `POST /v1/valuation/run` con `force=true`       | Re-valoraciones masivas tras cambio de prompt o YAML de unidades.   |

### 10.2 Curl de prueba

```bash
# Disparar valoración síncrona
curl -X POST http://127.0.0.1:8003/v1/valuation/run \
  -H "Content-Type: application/json" \
  -d '{"document_id": "8d9a2f66-...", "force": false, "line_already_valued": false}'

# Leer valoración persistida
curl http://127.0.0.1:8003/v1/valuation/8d9a2f66-...

# Re-valorar tras cambio de contrato
curl -X POST "http://127.0.0.1:8003/v1/valuation/8d9a2f66-.../re-run?codigo_contrato=C-2026-014"
```

### 10.3 Arranque local

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env       # rellenar PG_*, VALUATION_API_BASE_URL, ...
python main.py               # uvicorn en API_HOST:API_PORT (8003)
```

> **Importante**: el servicio asume que la BBDD ya existe (sv3 la crea). Si
> arrancas sv6 con BBDD vacía, las primeras peticiones fallarán con FK errors
> hasta que sv3 cree sus tablas merge. Es el escenario soportado por la
> inicialización perezosa, **pero no debes esperar respuestas correctas hasta
> que sv3 esté operativo**.

### 10.4 Decisión de despliegue Azure

Igual que sv3 — **Azure Container App** con:
- `minReplicas=1` para evitar cold start.
- `maxReplicas` 2-3 (el cuello de botella real es sv5 + Postgres).
- Ingress **interno** (lo llaman sv3 y sv4 dentro del spoke).
- Identidad gestionada para Key Vault (`PG_PASSWORD`).
- Health check apuntando a `/health` con `schema_ready=true` como condición de
  *readiness* (opcional pero recomendable).

> **No** Azure Functions: las tareas en `BackgroundTasks` requieren proceso
> persistente. Si pasaras a Functions habría que cambiar a Durable Functions o
> Azure Service Bus + Functions Trigger.

---

## 11. Inputs / Outputs del servicio

### Inputs

| Origen                | Naturaleza                              | Detalle                                                                  |
|-----------------------|------------------------------------------|--------------------------------------------------------------------------|
| HTTP                  | `POST /run` / `/run-async` / `/re-run`   | JSON con `document_id`, `codigo_contrato?`, `force`, `line_already_valued` |
| HTTP                  | `GET /valuation/{id}`                    | Lectura simple                                                            |
| `.env`                | Configuración estática                   | BBDD + URL sv5 + tolerancias + ALM literal                                |
| sv5 (HTTP)            | `ValuationEnvelope`                      | Envelope IA con `data.lineas` + `context.lineas_albaran/contrato`         |
| `unit_registry.yaml`  | Configuración estática                   | Categorías + factores cargados al arrancar                                |

### Outputs

| Destino     | Naturaleza                                   | Detalle                                                                   |
|-------------|----------------------------------------------|---------------------------------------------------------------------------|
| HTTP        | `application/json`                           | `RunValuationResult` o JSON completo de valoración                        |
| PostgreSQL  | INSERT en 3 tablas + DELETE previo            | `albaran_valuations`, `albaran_line_valuations`, `contrato_lines_derived` |
| Filesystem  | Logs rotados                                  | `logs/<fichero>.log`                                                      |

> **No toca SharePoint**, **no toca Microsoft Graph**, **no llama a LLMs** directamente.

---

## 12. Decisiones técnicas relevantes

1. **Tres pasadas explícitas en el builder**, no en una sola. Garantiza que las
   complementarias y sintéticas tengan acceso al record YA RESUELTO de su padre
   (con su partida, su descuento, su unidad, su cantidad). En una sola pasada
   habría dependencia de orden de aparición en el array, que es frágil.
2. **Replace transaccional** (`replace_valuation` = DELETE + INSERT). No upsert
   incremental. La valoración es 1:1 con `document_id` y siempre coherente.
   Coste: si una valoración tiene 200 líneas y solo cambia una, se reinsertan
   las 200. Aceptable para volúmenes razonables.
3. **STUBS de tablas del sv3** para FKs sin acoplar paquetes (§7.2). Es la
   solución limpia al problema "cómo tener FK reales sin importar `from sv3.orm import ...`".
4. **Inicialización perezosa de DDL** para resolver el race con sv3 al arrancar
   en orden indeterminado. `repository.is_initialized` lo expone en `/health`.
5. **Sub-tandas 2C / 2D / "tanda descuento"** todas documentadas en código
   con comentarios extensos. Se ve un proyecto que ha evolucionado por etapas
   bien controladas: cada tanda añade una capacidad sin romper las anteriores
   (las líneas con descuento `None` se comportan como antes).
6. **`raw_ia_envelope_json` se persiste en `albaran_valuations`** para auditoría:
   permite reconstruir la valoración completa sin volver a llamar a sv5. Coste:
   puede ser MUY grande (varios MB con `debug` del LLM). Lo dejo como mejora.
7. **`PriceReconciler` con regla del cliente**: si IA encontró línea exacta en
   tabla del contrato (1a), esa manda incluso por encima del PDF (1b). El PDF
   es desambiguador, no fuente primaria.
8. **`UnitCategoryGuard` re-clasifica con el registro determinista** y puede
   forzar `category_match=false` aunque la IA haya dicho `true`. Defensa en
   profundidad contra alucinaciones.
9. **`ImporteCalculator` aplica descuento solo en (0, 100]**. Saneamiento
   defensivo: 0, None, <0 o >100 → ignorado con reason `descuento_fuera_de_rango_ignorado`.
10. **`BackgroundTasks` de FastAPI** en lugar de Celery/Redis: deliberado por
    simplicidad. Asume que las pérdidas por reinicio son raras y aceptables;
    el front siempre puede pulsar "Valorar" (re-run) para recuperar.

---

## 13. Limitaciones conocidas y mejoras propuestas

| #  | Limitación                                                                                         | Mejora propuesta                                                                                  |
|----|----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| 1  | `BackgroundTasks` se pierde con el reinicio del proceso                                            | Pasar a outbox + worker (Celery con Redis, o Service Bus + Functions trigger).                    |
| 2  | Sin auth en endpoints                                                                              | API key header o Easy Auth (Entra ID) en Container App.                                           |
| 3  | Sin retry interno: si sv5 falla, propaga al cliente                                                | Retry exponencial con backoff dentro de `HttpValuationIaClient` (mismo patrón que `RetryPolicy` del sv2/sv5). |
| 4  | `replace_valuation` borra y reinserta (no diff incremental)                                        | Diff-based: solo INSERT/UPDATE/DELETE filas que cambien. Solo merece la pena si hay >100 líneas/doc en producción. |
| 5  | STUBS de tablas son frágiles: si sv3 cambia el tipo de PK, las FK divergen sin warning             | Añadir test de integración que verifique que los tipos de PK coinciden entre microservicios.      |
| 6  | El `ValuationBuilder` es muy grande (741 LOC) y concentra mucha lógica                             | Extraer las 3 pasadas a clases separadas (`BasePassBuilder`, `ComplementariaPassBuilder`, `SyntheticPassBuilder`). |
| 7  | `unit_registry.yaml` es estático (cambio requiere redeploy)                                        | Hot-reload con `watchdog`, o cargarlo desde Azure App Configuration.                              |
| 8  | `raw_ia_envelope_json` en TEXT puede pesar varios MB                                               | Migrar a JSONB con compresión, o externalizarlo a SharePoint/Blob (igual que sv3 hace con los IA artifacts). |
| 9  | Sin caché del envelope IA: re-ejecutar con `force=true` re-llama a Claude (caro en tokens)         | Cache opcional con TTL: si el envelope aún es válido (mismo contrato seleccionado), reusar.       |
| 10 | El conflicto `ml = mililitros vs metro_lineal` se resuelve por defecto a length sin mecanismo de override por proyecto | Variable `ML_DEFAULT_CATEGORY=length` configurable, o tabla por-cliente.            |
| 11 | El `document_id` en URLs no se valida como UUID antes de SQL                                       | Validador Pydantic en el path parameter (`Annotated[str, Path(regex=...)]`) para 400 temprano.    |
| 12 | El `read_full_valuation_json` no tiene paginación                                                  | Si una valoración llega a 500+ líneas (raro pero posible), responder en chunks.                   |

---

## 14. Frontera del microservicio

A diferencia del sv3 (donde sí veía sobrecarga de responsabilidades), **sv6 está
muy bien delimitado** dentro de su rol. Su responsabilidad es muy clara:
"dado un envelope IA, aplica las reglas del cliente y persiste". No hace OCR, no
llama a Sigrid, no toca SharePoint, no descarga PDFs. Cinco servicios pequeños y
desacoplados (`UnitCategoryGuard`, `PriceReconciler`, `PartidaMatcher`,
`UnitConverter`, `ImporteCalculator`) compuestos por el `ValuationBuilder`.

La única pieza que crece demasiado en LOC es el propio `ValuationBuilder`. Es la
"parte difícil" del servicio (§13.6 propone descomponerla). En el resto, la
separación es ejemplar.

> **Único acoplamiento real con sv3**: la BBDD compartida y los STUBS de tablas
> en `orm_valuation_models.py`. El acoplamiento de schema está mitigado por la
> técnica de stubs (sin imports cruzados) pero no eliminado.

---

## 15. Resumen de un vistazo

| Característica         | Valor                                                                                |
|------------------------|--------------------------------------------------------------------------------------|
| Tipo                   | API HTTP (FastAPI + uvicorn + BackgroundTasks)                                       |
| Lenguaje               | Python 3.12                                                                          |
| Entradas               | 4 endpoints: `/run` (sync), `/run-async` (202), `/re-run/{id}` (atajo), `/valuation/{id}` (lectura) |
| Salida                 | `RunValuationResult` o JSON completo de valoración                                   |
| Persistencia propia    | PostgreSQL — 3 tablas nuevas + STUBS de 3 del sv3                                    |
| Storage                | Ninguno (no SharePoint, no blob)                                                     |
| LLMs                   | Ninguno (solo via sv5)                                                               |
| Concurrencia           | uvicorn workers + `BackgroundTasks` (mismo proceso)                                  |
| Despliegue objetivo    | Azure Container App (minReplicas=1, ingress interno)                                 |
| Punto de entrada       | `python main.py`                                                                     |
| Dependencias clave     | `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg`, `httpx`, `pydantic-settings`, `PyYAML`|
| Servicios upstream     | sv3 (run-async) · sv4 (re-run, GET) · operadores (run con force=true)               |
| Servicios downstream   | sv5 (IA) · PostgreSQL                                                                |
| Cierra el ciclo del ecosistema | **Sí** — la fila ganadora en `albaran_valuations` es el output final de los 6 servicios |

---

*Documento generado a partir del análisis del código del paquete `sv6.zip` aportado.*
