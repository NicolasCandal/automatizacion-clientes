# Migración a 0.3

Todo el código distribuido está en `src/`. Los paquetes `indice`, `matching` y
`repositorio` son fachadas de compatibilidad: no implementan autenticación,
scraping, decisiones de coincidencia ni escritura independientes.

## Comandos

`python -m indice`, `python -m matching` y `python -m repositorio` están retirados.
Devuelven `2`, muestran cómo encontrar el comando actual y no leen configuración
ni ejecutan operaciones. Con `--help` o `-h` devuelven `0` y el mismo diagnóstico.
No se traducen automáticamente argumentos antiguos que podrían guardar archivos.

Usar `automatizacion-clientes --help` o `python -m automatizacion_clientes --help`.
La importación TXT, la autorización OAuth y el registro son operaciones separadas.
Actualizar scripts o tareas externas que invocaban los módulos anteriores.

## Configuración

No se carga `.env` al importar ningún paquete. `repositorio.config` reexporta
`Config` y `cargar_config` actuales. Todas las variables son `AC_`; las opciones
anteriores sin prefijo no configuran el flujo nuevo.

| Opción anterior | Opción actual |
| --- | --- |
| `SPREADSHEET_ID` | `AC_SPREADSHEET_ID`, con `AC_REPOSITORIO=sheets` |
| `CREDENTIALS_PATH` | `AC_CREDENTIALS_PATH` |
| `TOKEN_PATH` | `AC_TOKEN_PATH` |
| `CACHE_CATALOGO_PATH` / atributo `cache_catalogo_path` | `AC_CATALOGO_PATH` / atributo `catalogo_path` |

Las rutas son `Path` y se resuelven desde `AC_DIRECTORIO_BASE`. Los valores
iniciales de credenciales y token están en `secretos/`, no en la raíz. La
migración de código no mueve, lee ni convierte archivos reales o secretos.

## Autenticación

`repositorio.auth.obtener_credenciales` delega en `ProveedorCredencialesGoogle`:
no abre el navegador, no borra tokens revocados y no guarda renovaciones. Sin
configuración explícita usa variables `AC_`, pero no carga `.env` por su cuenta.
Para OAuth inicial usar `--autorizar-google` como operación separada. El alcance
inicial es `spreadsheets.readonly`; la escritura requiere una configuración y un
consentimiento que la permitan. No se agrega el alcance amplio de Drive.

## APIs de repositorio

`RepoXlsx` y `RepoSheets` delegan en los lectores y escritores actuales. Conservan
las consultas de listas y conjuntos, y admiten contexto o `cerrar()` explícito.
Las escrituras con la firma anterior ahora exigen `revision_esperada`:

```python
revision = repositorio.revision()
# Leer, resolver y planificar contra esa revisión antes de aplicar.
repositorio.aplicar_marcas(
    "IOL", col=4, encabezado="Semana", filas={3}, revision_esperada=revision
)
```

Omitir la revisión falla antes de escribir. No se obtiene una revisión nueva
silenciosamente para eludir una planificación obsoleta. Para el uso normal,
preferir el comando y `ServicioRegistro`, que administran respaldo y verificaciones.
Los modelos reexportados son los actuales y se construyen mediante argumentos
por nombre. `crear_respaldo()` puede devolver `Path`; no convertir rutas locales
a cadenas para manipularlas.

El respaldo de Sheets es ahora un JSON local de las cinco hojas consultadas,
no una copia completa creada con Drive. Las restricciones del escritor actual
tienen efecto también al utilizar estas fachadas. `RepoSheets` necesita una
`Config` con alcances de escritura explícitos para crear su escritor; el lector
se puede utilizar sin construirlo. No usar una fachada después de cerrarla.

## Catálogos, coincidencias y aliases

`indice.normalizacion` y `matching.brokers` adaptan únicamente listas y
diccionarios de las funciones puras. `indice.lector.construir_catalogo` utiliza
el constructor actual y conserva la última fila real aunque haya huecos.

Los catálogos de producción usan `version_esquema: 1`. Reconstruirlos desde el
origen; no renombrar un JSON antiguo y tratarlo como una caché nueva. Mantener
estado independiente para cada libro o documento. Los IDs anteriores `p0001`
no se equiparan automáticamente con IDs nuevos: revisar o rehacer los aliases
que dependan de ellos, conservando una copia del estado previo.

La fachada de coincidencias admite una vista histórica de personas solo para
construir un índice en memoria. La referencia de origen y revisión de esa vista
es deliberadamente no autoritativa; no usarla para guardar una caché o planificar
escrituras. `construir_indice` devuelve un objeto opaco, no un diccionario editable.

`resolver_contacto` y `resolver_semana` delegan todas las decisiones en
`MotorCoincidencias`. Se aplican sus correcciones de ambigüedad, restricciones de
broker, teléfonos compartidos y cotitulares. Los resultados históricos incorporan
`ubicaciones`, con todas las filas; `fila_por_hoja` conserva solo la primera como
vista anterior y no debe utilizarse para escribir marcas.

`AliasStore` delega en `AlmacenAliasesJson`, sin una segunda copia mutable de las
asociaciones. Utilizar `registrar`; el atributo privado `_datos` se retiró.
Las consultas devuelven copias. La consulta telefónica singular rechaza números
con varias asociaciones; el motor recibe todas y no elige arbitrariamente.
Las exclusiones delegan en `ListaExclusionJson`; `agregar` guarda inmediatamente.

Estas fachadas no sustituyen los servicios actuales: para simulación, informes y
registro completo usar el paquete `automatizacion_clientes`. Las pruebas
históricas se conservaron en `tests/unit/test_compat_*.py`, con tipado estricto,
uso de APIs públicas y revisiones explícitas al escribir.

## Dependencias y verificaciones

Se retiraron las dependencias directas antiguas de `google-api-python-client`,
`google-auth-httplib2`, `gspread`, `rich` y `python-dotenv`, junto a stubs sin uso.
Pydantic Settings mantiene sus propias dependencias para leer dotenv.
Ruff y mypy incluyen todo `src/`, `typings/` y todos los tests, sin exclusiones
de implementaciones antiguas. No se utiliza `Any` o `ignore_missing_imports`
para ocultar el código anterior.

La matriz de GitHub Actions está configurada, pero no se ha ejecutado remotamente
desde esta sesión. Las verificaciones locales y las pruebas simuladas no
sustituyen la aceptación sobre copias reales del Excel o un documento de prueba.
