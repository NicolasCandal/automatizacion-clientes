# Arquitectura y garantías

## Capas

- `core/`: modelos inmutables, contratos estructurales, normalización, brokers,
  construcción de catálogo, coincidencias, conciliación de aliases, planes de
  registro e interpretación de actividad TXT. Solo biblioteca estándar.
- `infra/`: lectura y escritura de Excel/Sheets, autenticación, archivos JSON,
  manifiestos TXT, RapidFuzz, reloj y logging. Traduce fallos externos a los
  errores de la aplicación y recibe configuración explícita.
- `services/`: coordina catálogo, coincidencias, registro e importación mediante
  puertos. No construye clientes de red ni interpreta JSON.
- `utils/`: utilidades compartidas; el diagnóstico de comandos retirados no
  contiene reglas del negocio.
- `main.py`: composición de adaptadores, opciones, validación de rutas y códigos
  de salida. Importarlo no ejecuta operaciones.

Los contratos están tipados con `Protocol`. Los modelos usan fechas de
calendario para períodos e instantes conscientes de zona para eventos. Se
conservan nombres originales, todas las apariciones y varias filas de una
persona en la misma hoja. La normalización no distingue homónimos reales.

## Resolución y persistencia

La prioridad del motor es grupo/exclusión, teléfono, alias por nombre, coincidencia
exacta y aproximada. Los brokers restringen destinos; no resuelven ambigüedades.
Se comparan todos los candidatos antes de limitar las sugerencias mostradas.
Los umbrales iniciales son 92 para resolución y 80 para revisión. Se configuran
con `AC_UMBRAL_COINCIDENCIA_AUTOMATICO`, `AC_UMBRAL_COINCIDENCIA_REVISION` y
`AC_LIMITE_SUGERENCIAS_COINCIDENCIA`.

Los servicios leen una sola extracción y comparten el catálogo. Previsualizan
decisiones antes de aprender aliases; el aprendizaje completo se contrasta antes
de registrar. No se aprenden decisiones parciales ni se reemplazan asociaciones
o teléfonos conocidos ante un conflicto. Las revisiones e IDs del catálogo
pertenecen a un origen concreto. Una caché corrupta no equivale a una vacía;
`--reconstruir-catalogo` permite repararla, sin recuperar IDs que solo existían allí.

Los pendientes bloquean las marcas por defecto. En modo real se guardan catálogo
e informe para revisión, sin aprender aliases ni crear respaldo. Con
`--permitir-pendientes` se autoriza el reemplazo con solo los resueltos, incluidos
los desmarcados que eso implique. Las exclusiones se conservan como lista JSON
normalizada para consulta y con nombres originales para persistencia.

El servicio revalida el plan antes de persistir archivos auxiliares. En modo real
guarda catálogo, informe y aprendizaje antes de modificar el origen. El respaldo
se crea solo cuando hay cambios. Después de cada hoja se verifican clientes,
estructura, encabezados y marcas esperadas. No hay transacción entre archivos y
planilla ni entre hojas; un error puede dejar archivos auxiliares guardados.

## Excel

Se requieren las hojas `Inviu`, `IOL`, `Bull`, `Balanz` y `PPI`. La columna A1
debe indicar Cliente, Clientes o Nombre. Los clientes empiezan en A3, se conservan
huecos y se rechazan nombres derivados de fórmulas. Las semanas empiezan en C,
o en D para Balanz, con encabezados en la fila 2. Son etiquetas opacas, no fechas
interpretadas por el programa.

Las marcas se reconocen por verde RGB `118,146,60` o `x` literal. Al aplicar,
los clientes seleccionados quedan verdes y los demás blancos, sin `x`. No se
escriben filas sin cliente ni otras semanas o columnas fijas. Fórmulas, datos
ajenos a marcas, combinaciones y protecciones en el destino se rechazan.
Los colores de tema o indexados no resueltos impiden consultar esas marcas.

La revisión es SHA-256 de los bytes. El respaldo copia los bytes exactos a un
archivo exclusivo y sincronizado. La escritura serializa a un temporal cerrado,
sincroniza y vuelve a comprobar la revisión antes de reemplazar el original.
Los bloqueos de Windows 32/33 se reintentan de forma acotada. Se rechazan libros
con dibujos, macros, controles, slicers o firmas cuya conservación no está
garantizada por el adaptador. El guardado no normaliza marcas si no hay cambios.

## Google Sheets y autenticación

El lector solicita las cinco hojas completas, sin una cota fija de filas o ZZ,
con valores, formato, dimensiones, combinaciones, protecciones, bandas, tablas,
reglas condicionales y tema. Resuelve colores efectivos y de tema; un color
irresoluble invalida la consulta de marcas, sin impedir leer clientes.
Las consultas reutilizan la instantánea; `revision()` siempre descarga una nueva
y calcula SHA-256 del JSON solicitado. Un fallo invalida el estado anterior.
Los servicios encuadran sus lecturas con comprobaciones de revisión.

El escritor comprueba la revisión antes de preparar y antes de enviar cada
lote. Actualiza valores y campos de fondo mediante un único `batchUpdate` por
hoja. No inserta ni desplaza columnas; amplía la cantidad absoluta de columnas
solo si falta espacio. Agrupa filas contiguas sin escribir huecos. Los lotes
mayores de 1.900.000 bytes se rechazan antes de enviarse.

Se rechazan validaciones, combinaciones, protecciones, bandas y reglas
condicionales que interfieran con las celdas a modificar, y hojas con tablas,
pivotes o fuentes de datos. Una simulación valida el plan, pero no garantiza
que el escritor admita todas las características del destino.

El respaldo es JSON local de los campos consultados de las cinco hojas. No
incluye otras hojas, permisos, historial u otros recursos no solicitados y no
es una copia integral de Drive. No hay restaurador automático. El JSON permite
inspección y recuperación manual de los datos y formatos capturados.

La carga habitual usa OAuth o cuenta de servicio según `Config`. Nunca inicia
consentimiento como recuperación de un error. OAuth inicial usa navegador,
PKCE y callback en `127.0.0.1` con puerto efímero y espera limitada. La carga
puede renovar en memoria; el comando solo persiste esa renovación en modo real.
El token anterior no se borra ante revocación o fallo. Los alcances configurados
deben estar declarados o concedidos; no se amplían de forma implícita ni se
infieren equivalencias entre URI. Google valida los permisos efectivos.

Los JSON de credenciales y token contienen secretos en texto plano; no están
cifrados por la aplicación. En Windows se heredan permisos del directorio.
Los logs propios omiten secretos y se silencian temporalmente los loggers ya
cargados de los SDK de autenticación. Ese cambio global requiere ejecutar la
autenticación secuencialmente. Las causas técnicas se conservan para depuración
y no deben publicarse indiscriminadamente.

## Reintentos y concurrencia

Tenacity limita las operaciones de red con timeout y espera exponencial
aleatoria. Los valores iniciales son cuatro intentos, timeout de 30 segundos y
esperas de 1 a 30 segundos. El timeout no es un plazo total del flujo.
Se reintentan fallos temporales de conexión, HTTP 408/429 y 5xx salvo 501/505.
HTTP 403 se reintenta solo si todos sus motivos estructurados indican cuota.
TLS inválido, permisos, autenticación rechazada y respuestas malformadas fallan
sin reintento. No se multiplican reintentos internos del transporte y del SDK.

Antes de reenviar una escritura se comprueba la revisión. Un timeout puede
haber ocurrido después de aplicar el lote; si la revisión cambió, se detiene.
Si continúa idéntica, solo se repiten asignaciones idempotentes. No se confirma
un éxito incierto por inferencias. El intercambio OAuth de código de un solo
uso solo reintenta `ConnectTimeout`, antes de establecer la conexión.

Se requiere un único escritor por estado y origen. La comprobación local del
hash no bloquea Sheets ni es una precondición atómica del servidor. Tampoco el
reemplazo de Excel o JSON constituye un compare-and-swap del sistema de archivos.
Las consultas completas de Sheets consumen memoria, tiempo y cuota; cambios de
colaboradores o fórmulas volátiles pueden invalidar una revisión.

## Archivos locales e importación TXT

El guardado JSON valida tipos y números finitos antes de crear un temporal;
sincroniza, cierra y reemplaza en la misma carpeta. Un fallo previo conserva el
destino. No se sincroniza el directorio ni se garantiza durabilidad ante un corte
de energía. Aliases y exclusiones comprueban además su estado persistido previo.

La importación exige un manifiesto no vacío, versión 1 y campos conocidos. Las
rutas TXT deben ser relativas, únicas y resolver dentro de su carpeta, sin
ascensos, escapes o flujos alternativos. Se rechazan codificaciones diferentes
de UTF-8, ZIP y entradas malformadas sin generar un lote parcial. Se cierran los
archivos incluso si falla el análisis.

Los patrones admitidos son Android con separador ` - ` e iOS con corchetes, horas
de 12/24 horas, segundos opcionales, LF/CRLF, BOM UTF-8 y LRM/RLM iniciales.
El orden día/mes es explícito; se admite ISO `YYYY-MM-DD`, y los años cortos
requieren siglo 1900 o 2000. No se convierten zonas horarias de las fechas TXT.
Solo cuentan etiquetas de remitentes declaradas y no ambiguas con el separador
`: `. Los avisos y continuaciones no generan actividad independiente. Un cuerpo
que imite un encabezado puede ser indistinguible: el texto no se autentica.

La salida conserva un contacto por archivo con actividad, su identidad y la
última fecha dentro del período inclusivo. Los mensajes posteriores no eliminan
la actividad de la semana. Los grupos se conservan para que el motor los excluya.
No se fusionan chats por nombre ni se infieren identidades o países. Una semana
vacía es válida; revisar antes de utilizarla para reemplazar marcas.

Los límites iniciales son 500 chats, 25 MiB por TXT, 256 KiB por línea, 100 MiB
por lote y 1 MiB para el manifiesto. Se detectan cambios habituales mediante
identidad, tamaño y fechas de archivo; no es una instantánea atómica. Las entradas
deben permanecer sin cambios durante la importación. No se comprueba que el lote
contenga todos los chats o que cada exportación cubra todo el historial.

## Verificación y dependencias

Ruff, mypy estricto y pytest cubren todos los paquetes y tests. Los stubs locales
de `google_auth_oauthlib` describen la superficie usada; no alteran el SDK ni
ocultan código con exclusiones generales. Poetry fija dependencias en el lock.
CI ejecuta la matriz Windows/Linux con Python 3.12, 3.13 y 3.14 y construye las
distribuciones; no despliega ni publica automáticamente.

Las pruebas usan libros, chats y credenciales ficticios y HTTP simulado. La
validación local no acredita una autorización real, una planilla remota real
ni la ejecución de la matriz de CI en GitHub. Antes del uso operativo validar
las reglas con una exportación propia y una copia del libro o documento.
