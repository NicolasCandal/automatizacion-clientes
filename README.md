# Automatización de clientes

Importa actividad semanal desde archivos TXT exportados de WhatsApp, identifica
clientes contra un libro Excel o Google Sheets y prepara o aplica sus marcas
semanales. La versión 0.3 concentra toda la implementación en `src/`.

La importación guarda identidad, teléfono declarado, última fecha de actividad y
condición de grupo. No extrae información comercial del cuerpo de los mensajes.
Importar los chats y registrar las marcas son operaciones separadas.

El proyecto se distribuye sin datos de clientes ni credenciales. Los tests y
ejemplos contienen únicamente datos sintéticos. Los archivos personales de
entrada, estado y respaldo están excluidos de Git y de las distribuciones.

## Estructura

```text
.
├── pyproject.toml
├── poetry.lock
├── .env.example
├── .pre-commit-config.yaml
├── .github/workflows/main.yml
├── README.md
├── docs/
│   ├── arquitectura.md
│   └── migracion.md
├── examples/whatsapp/
│   ├── manifiesto.json
│   ├── contacto_demo.txt
│   └── grupo_demo.txt
├── src/
│   ├── automatizacion_clientes/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── config.py
│   │   ├── main.py
│   │   ├── core/
│   │   ├── infra/
│   │   ├── services/
│   │   └── utils/
│   ├── indice/          # Fachadas de compatibilidad
│   ├── matching/        # Fachadas de compatibilidad
│   └── repositorio/     # Fachadas de compatibilidad
├── typings/
└── tests/
    ├── unit/
    └── integration/
```

Las carpetas de entrada, estado, salida y respaldos se configuran; no forman
parte del paquete. [Arquitectura y garantías](docs/arquitectura.md) describe los
contratos, validaciones, reintentos y límites de cada adaptador.

## Instalación

Requiere Python 3.12, 3.13 o 3.14 y Poetry. La CI fija Poetry 2.3.1.

```console
poetry sync --with dev
poetry run automatizacion-clientes --help
```

Copiar `.env.example` a `.env` y ajustar únicamente los valores propios.
Todas las variables tienen prefijo `AC_`; la configuración se valida mediante
Pydantic Settings. No se carga ningún archivo de entorno al importar módulos.

Las rutas relativas se resuelven desde `AC_DIRECTORIO_BASE`. Cambiar
`AC_DIRECTORIO_DATOS` no modifica las otras rutas explícitas del ejemplo.
Mantener estados y respaldos separados para cada libro o documento.

El modo predeterminado es simulación. `--simular` y `--aplicar` prevalecen sobre
`AC_SIMULACION`. Una simulación no guarda archivos de la aplicación ni cambia
marcas; en Sheets puede leer la API y renovar credenciales en memoria.

## 1. Preparar los chats exportados

Exportar cada conversación como TXT UTF-8 y declarar sus identidades en un
manifiesto. El ejemplo ficticio de [examples/whatsapp](examples/whatsapp)
muestra los campos exactos y los formatos admitidos.

Cada entrada declara su archivo, nombre, teléfono opcional, remitentes válidos
y si corresponde a un grupo. El orden día/mes es explícito. No se infieren
identidades a partir del nombre del archivo. Las rutas TXT deben permanecer
dentro de la carpeta del manifiesto.

El período es inclusivo. Se toma la última actividad dentro de esas fechas,
aunque el chat también contenga mensajes posteriores. Un chat sin actividad en
el período no produce un contacto; una semana vacía es válida.

```console
poetry run automatizacion-clientes --importar-whatsapp --desde 2026-11-02 --hasta 2026-11-06 --manifiesto datos/entrada/whatsapp/manifiesto.json --simular
```

Después de revisar el lote, guardar la entrada semanal:

```console
poetry run automatizacion-clientes --importar-whatsapp --desde 2026-11-02 --hasta 2026-11-06 --manifiesto datos/entrada/whatsapp/manifiesto.json --aplicar
```

Esta operación escribe solamente `AC_CONTACTOS_PATH`. No abre el libro,
no autoriza Google y no registra marcas. Se valida el lote completo antes de
guardar; los mensajes completos no se incluyen en la salida semanal.

## 2. Configurar el destino

### Excel

Usar `AC_REPOSITORIO=excel` y configurar `AC_EXCEL_PATH`.
El libro debe contener `Inviu`, `IOL`, `Bull`, `Balanz` y `PPI`.
A1 identifica la columna de clientes, los nombres empiezan en A3 y los
encabezados semanales están en la fila 2. Las semanas comienzan en C, salvo
Balanz, donde comienzan en D.

Se reconocen marcas verdes RGB `118,146,60` o una `x` literal. Al aplicar,
los seleccionados quedan verdes y los demás blancos en la columna indicada.
Se rechazan estructuras o características cuya conservación no está soportada;
consultar las restricciones del adaptador antes de usar un libro operativo.

### Google Sheets

Usar `AC_REPOSITORIO=sheets`, `AC_SPREADSHEET_ID` y credenciales propias.
El documento utiliza las mismas cinco hojas y convenciones que Excel.

Para OAuth inicial, ejecutar la autorización como operación independiente:

```console
poetry run automatizacion-clientes --autorizar-google
```

La operación abre el navegador y guarda el token. Los demás comandos no inician
consentimiento automáticamente ni borran tokens ante un fallo.
La configuración inicial solicita solo lectura; para aplicar se debe configurar
`AC_GOOGLE_ALCANCES=["https://www.googleapis.com/auth/spreadsheets"]` y disponer
de consentimiento y permisos efectivos de escritura.

También se admite `AC_GOOGLE_TIPO_AUTENTICACION=cuenta_servicio`; en ese caso
se usan las credenciales de esa cuenta y sus permisos sobre el documento,
sin el comando de consentimiento OAuth.

Guardar credenciales y tokens fuera del código, en las rutas configuradas de
`secretos/`. La aplicación no cifra esos JSON.

## 3. Resolver y registrar la semana

El registro lee `AC_CONTACTOS_PATH`, construye o actualiza el catálogo del
destino, resuelve coincidencias y prepara el reemplazo de marcas.

```console
poetry run automatizacion-clientes --encabezado "2026-11-02 al 2026-11-06" --simular
```

El encabezado es una etiqueta opaca: usar el texto exacto de una semana existente
o una etiqueta nueva que incluya el año. No se interpreta como una fecha.

Los contactos pendientes bloquean las marcas por defecto. Con `--aplicar`
se guarda el informe para revisarlos, sin aprender aliases ni crear un respaldo
si el plan está bloqueado. `--permitir-pendientes` autoriza explícitamente usar
solo los resueltos, incluidos los desmarcados que implique ese reemplazo.

```console
poetry run automatizacion-clientes --encabezado "2026-11-02 al 2026-11-06" --aplicar
```

Cuando hay cambios, el flujo crea respaldo, comprueba revisiones y verifica
el resultado. En Excel el respaldo conserva los bytes originales. En Sheets
es un JSON local de los campos consultados de las cinco hojas, sin otras hojas,
permisos ni historial; no es una copia integral de Drive.

Se requiere un único escritor por origen y estado. No hay transacción conjunta
entre archivos auxiliares y planilla ni entre hojas, ni restauración automática.
Una operación interrumpida puede haber aplicado parte de las marcas.
Una simulación no garantiza la compatibilidad de todas las características
que valida el escritor al aplicar.

## Opciones y resultados

`--env-file RUTA` elige un archivo de configuración; `--sin-env` evita cargarlo.
`--directorio-base RUTA` sustituye la base de rutas.
`--reconstruir-catalogo` reconstruye una caché inválida desde el origen; no
recupera IDs presentes únicamente en la caché perdida.

| Código | Resultado |
| --- | --- |
| 0 | Operación completada o ayuda |
| 1 | Fallo inesperado |
| 2 | Argumentos, configuración o dominio inválidos |
| 3 | Plan bloqueado por pendientes |
| 4 | Error de infraestructura |
| 5 | Cambio concurrente del origen o estado |
| 6 | Registro interrumpido |
| 130 | Interrupción del usuario |

Los diagnósticos usan logging con nivel, fecha y contexto; no se utiliza
`print()` en el código de la aplicación.

## Desarrollo y verificación

```console
poetry check --lock
poetry run ruff check src typings tests
poetry run ruff format --check src typings tests
poetry run mypy
poetry run pytest --cov=automatizacion_clientes --cov-report=term-missing
poetry build
```

Ruff y mypy estricto incluyen todos los paquetes y tests. Los hooks de pre-commit
ejecutan las mismas comprobaciones de estilo y tipos. GitHub Actions configura
tests y construcción en Windows/Linux con Python 3.12, 3.13 y 3.14; no publica
ni despliega automáticamente.

Las pruebas usan datos ficticios, libros temporales y HTTP simulado. Antes del
uso operativo falta aceptar el flujo con una exportación propia y una copia del
destino. Las pruebas locales no acreditan OAuth real ni ejecución remota de CI.

## Migración desde los módulos anteriores

La versión 0.3 retira los comandos `python -m indice`, `python -m matching` y
`python -m repositorio`: muestran la guía de migración y devuelven 2, sin
ejecutar operaciones. Sus paquetes conservan fachadas tipadas para parte de la
API anterior; las escrituras requieren una revisión explícita.

Revisar [Migración a 0.3](docs/migracion.md) antes de actualizar tareas externas,
configuración, catálogos o aliases anteriores. La refactorización no convierte
automáticamente el estado real existente.
