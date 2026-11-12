# Plan de trabajo — Krunchbox Backend Challenge

## 1. El problema

Un sistema de entradas para un recinto. Comprar reduce la capacidad disponible; reembolsar la restaura.

- **Stack obligatorio:** Python, FastAPI, SQLAlchemy, PostgreSQL.
- **Mínimo exigido:** 2 modelos SQLAlchemy y 1 endpoint funcional de compra o reembolso.
- **Qué evalúan:** el modelado de datos y la estructura del código del endpoint.
- **Opcional que suma:** Docker.

El riesgo central, aunque el enunciado no lo nombra, es la **sobreventa por concurrencia**: dos personas comprando la última entrada a la vez. Todo el diseño parte de ahí.

## 2. Supuestos acordados

| # | Supuesto | Consecuencia en el diseño |
|---|----------|---------------------------|
| 1 | Dos tipos de entrada, General y VIP, con la puerta abierta a más | Los tipos son **filas** de una tabla, no un enum |
| 2 | Reembolsos parciales | El reembolso se pide por ítem y cantidad; se guarda historial |
| 3 | Hay reglas de reembolso | Política aislada en una función pura, configurable y testeable |
| 4 | Al seleccionar entradas quedan **reservadas unos minutos** hasta que se confirma la compra | La compra nace en estado `pending` con fecha de expiración; el cupo distingue `held` de `sold` |
| 5 | Frontend sencillo (React + Vite) con cuentas | Autenticación JWT y rol de administrador |
| 6 | Demo presencial ante el entrevistador | Todo arranca con `docker compose up`, con datos de ejemplo que permiten mostrar cada regla; la disponibilidad se refresca sola cada pocos segundos |
| 7 | Seguridad frente a vulnerabilidades habituales | Ver sección 7 |
| 8 | Docker y, al final, deploy en Oracle Cloud | Configuración por variables de entorno desde el inicio |

Fuera de alcance: pasarela de pago (confirmar la compra equivale al pago aprobado), asientos numerados, cancelación de eventos, envío de correos.

## 3. Modelo de datos

```
User 1──N Purchase N──1 Event 1──N TicketType
              │                        │
              1                        │
              N                        │
        PurchaseItem N─────────────────┘
              │
Purchase 1──N Refund 1──N RefundItem N──1 PurchaseItem
```

### 3.1 `User`
`id, email (único), full_name, hashed_password, is_admin, created_at`

- Necesario por el sistema de cuentas: cada compra tiene dueño y solo el dueño puede confirmarla, cancelarla o reembolsarla.
- `is_admin` separa a quien crea eventos de quien compra. Un booleano basta; una tabla de roles sería sobrediseño aquí.
- La contraseña se guarda con bcrypt, nunca en claro.

### 3.2 `Event`
`id, name, description, venue, starts_at (con zona horaria), created_at`

- **No tiene capacidad propia.** La capacidad vive en cada tipo de entrada; la del evento es la suma. Así no hay dos números que puedan contradecirse.
- `starts_at` con zona horaria: las reglas de reembolso dependen de "horas antes del evento" y una fecha sin zona las volvería ambiguas.

### 3.3 `TicketType` — la pieza clave
`id, event_id, name, price, capacity, held, sold, is_refundable`

- **Fila por tipo y por evento, no enum.** Añadir "Platinum" o "Early Bird" es un `INSERT` (o un `POST /events/{id}/ticket-types`), sin migración ni cambio de código. Además cada evento fija sus propios precios y cupos.
- **Dos contadores en la misma fila que `capacity`:**
  - `held`: entradas reservadas a la espera de confirmación.
  - `sold`: entradas de compras confirmadas.
  - Disponibles = `capacity - held - sold`.
- Separarlos permite saber cuántas están realmente vendidas y cuántas solo apartadas, y liberar las apartadas sin tocar las vendidas. Ese desglose solo lo ve el administrador; el público ve las disponibles.
- **Restricciones en la base de datos:**
  - `CHECK (held >= 0 AND sold >= 0 AND held + sold <= capacity)`: aunque el código tuviera un error, la base de datos rechaza la sobreventa.
  - Índice único sobre `(event_id, lower(name))`: no hay dos "VIP" en el mismo evento, tampoco escritos "vip".
- `is_refundable` permite tipos sin devolución (por ejemplo, una preventa con descuento).

Alternativas descartadas:
- *Una fila por entrada física:* correcto si hubiera asientos numerados, pero con cupo general solo añade filas y bloqueos.
- *Calcular vendidas con `COUNT`/`SUM` sobre las compras:* obliga a bloquear o a usar aislamiento serializable para no sobrevender, y es más lento.

### 3.4 `Purchase`
`id, user_id, event_id, status, total_amount, idempotency_key, expires_at, confirmed_at, created_at`

- Cabecera de la compra. Una compra pertenece a **un** evento y puede mezclar tipos (2 General + 1 VIP).
- **La reserva y la compra son la misma fila en distintos estados**, no dos tablas. Confirmar es una transición de estado, sin copiar datos:

```
pending ──confirmar──▶ completed ──reembolso──▶ partially_refunded ──▶ refunded
   │
   ├──cancelar──▶ cancelled
   └──vence──▶ expired
```

- `expires_at`: hasta cuándo se respeta la reserva (10 minutos por defecto, configurable).
- `status` se guarda como texto con `CHECK`, no como enum nativo de PostgreSQL: añadir un estado no exige `ALTER TYPE`.
- `idempotency_key` con `UNIQUE (user_id, idempotency_key)`: si el cliente reintenta por un fallo de red, no se reserva dos veces.
- **Índice único parcial** `(user_id, event_id) WHERE status = 'pending'`: un usuario solo puede tener una reserva pendiente por evento. Evita acaparar cupo abriendo reservas en serie.

### 3.5 `PurchaseItem`
`id, purchase_id, ticket_type_id, quantity, unit_price, refunded_quantity`

- **`unit_price` es una foto del precio al reservar.** Si el precio cambia después, la confirmación y los reembolsos siguen usando ese precio.
- **`refunded_quantity`** con `CHECK (refunded_quantity BETWEEN 0 AND quantity)`: es imposible reembolsar más de lo comprado, también a nivel de base de datos.
- `UNIQUE (purchase_id, ticket_type_id)`: un tipo aparece una sola vez por compra.

### 3.6 `Refund` y `RefundItem`
`Refund: id, purchase_id, amount, refund_percentage, reason, idempotency_key, created_at`
`RefundItem: id, refund_id, purchase_item_id, quantity, amount`

- Cada reembolso es un **registro inmutable**, no un simple cambio de estado. Da historial y auditoría: cuándo, cuánto, con qué porcentaje.
- `refund_percentage` guarda la regla aplicada en ese momento (100 o 50), porque la política puede cambiar.
- `RefundItem` detalla qué entradas se devolvieron, lo que hace posibles varios reembolsos parciales sobre la misma compra.
- `idempotency_key` con `UNIQUE (purchase_id, idempotency_key)`: reintentar un reembolso no devuelve más entradas.

### 3.7 Decisiones transversales
- **Dinero en `NUMERIC(10,2)`** y `Decimal` en Python; nunca `float`.
- **Fechas en UTC con zona horaria.**
- **Claves enteras autoincrementales:** suficientes para el reto y más legibles en las demostraciones.

## 4. Concurrencia

### Reservar
Una sentencia por tipo de entrada:

```sql
UPDATE ticket_types
SET held = held + :qty
WHERE id = :id AND held + sold + :qty <= capacity
RETURNING price;
```

- Comprobar y reservar ocurren en la misma sentencia. PostgreSQL bloquea la fila; una segunda reserva simultánea espera y vuelve a evaluar el `WHERE` con el valor ya actualizado.
- Si no devuelve fila, no hay cupo: se revierte todo y se responde `409`.
- En reservas con varios tipos, las filas se actualizan **ordenadas por id** para que dos operaciones cruzadas no se bloqueen mutuamente (deadlock).
- Los contadores, la compra y sus ítems se confirman en **una sola transacción**: o todo o nada.

Se prefiere a `SELECT ... FOR UPDATE` porque es una sola ida a la base de datos y mantiene el bloqueo menos tiempo.

### Confirmar, cancelar y reembolsar
- Las tres bloquean primero la fila de la compra con `SELECT ... FOR UPDATE` y vuelven a comprobar su estado. Dos peticiones simultáneas sobre la misma compra se procesan en fila.
- **Confirmar:** mueve las cantidades de `held` a `sold`. Si la reserva ya venció, se libera y se responde `409`. Si el evento ya empezó, se rechaza.
- **Cancelar:** resta de `held`.
- **Reembolsar:** valida todo antes de modificar nada; luego incrementa `refunded_quantity`, resta de `sold` y crea el `Refund`. Con la compra ya bloqueada busca primero un reembolso con la misma clave de idempotencia: un reintento, incluso simultáneo, recibe el original.

### Vencimiento de reservas
- Una tarea en segundo plano revisa cada pocos segundos las compras `pending` vencidas, las marca `expired` y devuelve su cupo.
- Usa `FOR UPDATE SKIP LOCKED` y una transacción por compra: es segura con varias instancias de la API y no compite con una confirmación en curso.
- Además, antes de cada reserva se liberan las vencidas de ese evento, así el cupo no depende de que la tarea haya pasado ya.

## 5. Reglas de negocio

### Reserva y compra
1. Requiere sesión iniciada.
2. El evento debe existir y no haber empezado.
3. Cada tipo pedido debe pertenecer a ese evento y aparecer una sola vez.
4. Máximo 10 entradas por compra y 20 por cliente y evento sumando todas sus compras; las devueltas no cuentan (configurable).
5. Debe haber cupo para **todos** los tipos pedidos; si falta uno, no se reserva nada.
6. Una sola reserva pendiente por usuario y evento.
7. La reserva dura 10 minutos (configurable). Si no se confirma, vence y el cupo vuelve a la venta.
8. Quien cancela o deja vencer 3 reservas del mismo evento en una hora no puede reservar ese evento hasta que pase la hora (configurable).
9. No se confirma una reserva si el evento ya empezó.
10. Solo el dueño puede confirmar o cancelar su reserva.

### Reembolso
1. Solo el dueño de la compra puede reembolsarla.
2. Solo se reembolsan compras confirmadas.
3. No hay reembolsos una vez empezado el evento.
4. Ventanas de tiempo (configurables):
   - 7 días o más antes del evento: **100 %**.
   - Entre 48 horas y 7 días: **50 %**.
   - Menos de 48 horas: **no se permite**.
5. Los tipos marcados como no reembolsables no se devuelven.
6. Se puede reembolsar parcialmente: se indica cantidad por ítem.
7. No se puede devolver más de lo que queda sin reembolsar en cada ítem.
8. El importe se calcula sobre el precio pagado, no el actual. Se redondea al centavo por entrada, así dividir un reembolso en varios paga lo mismo.
9. Las entradas devueltas vuelven a estar a la venta de inmediato.
10. Una compra admite varios reembolsos hasta agotarse; su estado pasa a `partially_refunded` o `refunded`.

## 6. API

| Método | Ruta | Acceso | Qué hace |
|--------|------|--------|----------|
| POST | `/auth/register` | público | Crea cuenta |
| POST | `/auth/login` | público | Devuelve token JWT |
| GET | `/auth/me` | usuario | Datos de la cuenta |
| GET | `/events` | público | Eventos futuros con disponibilidad por tipo; paginado (`limit`, `offset`, `include_past`) |
| GET | `/events/{id}` | público | Detalle de un evento |
| POST | `/events` | admin | Crea evento con sus tipos |
| POST | `/events/{id}/ticket-types` | admin | Añade un tipo nuevo |
| **POST** | **`/events/{id}/purchases`** | usuario | **Reserva** entradas (cabecera `Idempotency-Key` opcional) |
| **POST** | **`/purchases/{id}/confirm`** | usuario | **Confirma la compra** |
| POST | `/purchases/{id}/cancel` | usuario | Cancela una reserva pendiente |
| **POST** | **`/purchases/{id}/refunds`** | usuario | **Reembolso** total o parcial (cabecera `Idempotency-Key` opcional) |
| GET | `/purchases` | usuario | Mis compras, paginado |
| GET | `/purchases/{id}` | usuario | Detalle, con la política de reembolso vigente |

Códigos: `201` creado, `401` sin sesión, `403` sin permiso, `404` no existe, `409` sin cupo, reserva vencida o conflicto, `422` regla de negocio o datos inválidos, `429` demasiadas reservas abandonadas o demasiadas peticiones. Los errores de negocio comparten formato: `{"error": {"code", "message"}}`.

## 7. Seguridad

| Riesgo | Medida |
|--------|--------|
| Inyección SQL | Todas las consultas pasan por SQLAlchemy con parámetros enlazados; no se construye SQL con texto del usuario |
| Datos de entrada maliciosos | Esquemas Pydantic estrictos: tipos, rangos, longitudes máximas y rechazo de campos desconocidos |
| Asignación masiva | Los esquemas de entrada no incluyen campos sensibles (`is_admin`, precios, estados); nadie puede hacerse admin por la API |
| Acceso a datos ajenos (IDOR) | Toda consulta de compras filtra por el usuario del token; una compra ajena responde `404` |
| Contraseñas | Hash bcrypt con sal; longitud mínima; el login responde igual si falla el correo o la contraseña y tarda lo mismo en ambos casos |
| Sesiones | JWT firmado, de vida corta, con el secreto en variable de entorno; la API se niega a arrancar en producción con el secreto por defecto |
| CSRF | El token viaja en la cabecera `Authorization`, no en una cookie, por lo que otro sitio no puede usarlo |
| XSS | React escapa todo lo que muestra; cabecera `Content-Security-Policy` estricta en el frontend |
| Fuerza bruta | Límite de peticiones por IP en login y registro, aplicado en nginx |
| Abuso de escrituras | nginx limita el resto de escrituras y el tráfico total de la API por IP |
| Acaparar entradas sin comprar | Una reserva pendiente por cliente y evento, tope de reservas abandonadas y tope de entradas por cliente y evento |
| Reembolsos duplicados | Reembolso idempotente por clave, serializado por el bloqueo de la compra |
| Cifras de venta | El desglose reservadas/vendidas solo lo recibe el administrador |
| Trazabilidad | Registro de auditoría en JSON por operación, con el id de la petición que también recibe el cliente |
| Clickjacking y similares | Cabeceras `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` y, en producción, `Strict-Transport-Security` |
| CORS | Ningún origen permitido por defecto: el frontend y la API comparten origen a través de nginx |
| Fugas de información | Los errores internos devuelven un mensaje genérico, sin trazas |
| Secretos | En `.env`, fuera del repositorio; se entrega `.env.example` |
| Infraestructura | Contenedores sin usuario root; la base de datos no se expone a internet; HTTPS en el deploy |
| Dependencias | Versiones fijadas |

## 8. Estructura del código

```
backend/
  app/
    main.py            # app, id de petición, cabeceras, manejadores de error, tarea de vencimiento
    audit.py           # registro de auditoría
    config.py          # ajustes por variables de entorno
    database.py        # engine y sesión
    models.py          # modelos SQLAlchemy
    schemas.py         # entrada/salida Pydantic
    security.py        # contraseñas y JWT
    deps.py            # dependencias: sesión, usuario actual, admin
    exceptions.py      # errores de dominio → HTTP
    seed.py            # cuenta admin y datos de ejemplo para la demo
    routers/           # endpoints delgados: validan, delegan, responden
    services/
      purchases.py     # reservar, confirmar, cancelar, vencer
      refunds.py       # lógica de reembolso
      refund_policy.py # ventanas de tiempo (función pura)
  alembic/             # migraciones
  tests/
frontend/              # React + Vite, servido por nginx
docker-compose.yml
README.md
```

Los endpoints no contienen lógica de negocio: la delegan en `services/`. Los servicios lanzan errores de dominio y un único manejador los traduce a HTTP. Así la lógica se prueba sin levantar HTTP y el endpoint se lee de un vistazo.

## 9. Fases

1. **Backend:** modelos, servicios, endpoints, autenticación, medidas de seguridad.
2. **Migraciones y datos de ejemplo:** Alembic; seed con un admin, un usuario de demo y eventos pensados para mostrar cada regla (reembolso al 100 %, al 50 %, fuera de plazo, tipo no reembolsable, evento casi agotado).
3. **Tests** contra PostgreSQL real: reserva, confirmación, vencimiento, sin cupo, reembolsos parciales, cada regla, idempotencia, permisos y pruebas de concurrencia (N reservas simultáneas sobre las últimas entradas).
4. **Docker:** `docker-compose` con base de datos, API y frontend; migraciones y seed al arrancar.
5. **Frontend:** registro e inicio de sesión, eventos con disponibilidad que se refresca sola, selección y reserva con cuenta atrás, confirmación, mis compras y reembolso.
6. **README** en inglés: cómo ejecutar, supuestos, reglas, decisiones de diseño, guion de demo.
7. **Deploy en Oracle Cloud:** ver sección 10.
8. **Revisión de código y endurecimiento:** ver sección 11.

## 10. Deploy en Oracle Cloud

### Opciones evaluadas

| Opción | Cómo sería | Veredicto |
|--------|-----------|-----------|
| **A. Una máquina virtual con Docker Compose** | La misma pila que en local, más un proxy con HTTPS | **Recomendada** |
| B. Contenedores gestionados + PostgreSQL gestionado de Oracle | Cada pieza en un servicio distinto de Oracle | Descartada: de pago, más piezas que configurar y nada que el reto evalúe |
| C. Kubernetes (OKE) | Clúster con manifiestos | Descartada: sobredimensionado para tres contenedores |

Por qué A:
- Es lo que pediste: una instancia creada solo para este problema.
- Lo que se despliega es exactamente lo que se probó en local, con el mismo `docker compose`.
- Cabe en la capa gratuita de Oracle.
- Se explica en dos frases ante el entrevistador.

### Arquitectura

```
Internet ──443──▶ Caddy (HTTPS automático)
                    │
                    ▼
                  nginx (app React, límite de login, CSP)
                    │ /api
                    ▼
                  FastAPI ──▶ PostgreSQL (volumen en disco)
```

Solo Caddy publica puertos (80 y 443). nginx, la API y la base de datos quedan en la red interna de Docker.

### Decisiones

1. **Máquina.** Ampere A1 (ARM) de la capa gratuita, con 1 núcleo y 6 GB: sobra para construir las imágenes en la propia máquina. Si Oracle no tiene cupo de A1 en tu región, la alternativa gratuita es la micro de AMD (1 GB), que obliga a construir las imágenes fuera. Sistema: Ubuntu 24.04.
2. **HTTPS.** Caddy pide y renueva solo el certificado de Let's Encrypt. Necesita un nombre de dominio, no vale la IP sola:
   - Con dominio propio: un registro A apuntando a la máquina.
   - Sin dominio: un subdominio gratuito de DuckDNS.
   Sin HTTPS las contraseñas viajarían en claro, así que no se contempla publicar por HTTP.
3. **Cómo llega el código.** Repositorio en GitHub; en la máquina, `git clone` y `docker compose up --build`. Actualizar es `git pull` y repetir. La integración continua se añadió después (sección 11); el despliegue sigue siendo manual.
4. **Configuración de producción.** Un archivo `docker-compose.prod.yml` que se superpone al actual:
   - Añade Caddy y quita los puertos publicados de la base de datos, la API y nginx.
   - `ENVIRONMENT=production`: activa HSTS y obliga a un secreto JWT real.
   - Secretos y contraseñas generados al azar en un `.env` que solo existe en el servidor.
   - `restart: unless-stopped` para que todo vuelva tras un reinicio.
5. **Red de Oracle.** Abrir 80 y 443 a internet y el 22 (SSH) solo a tu IP, en la lista de seguridad de la red. Las imágenes de Ubuntu de Oracle traen además un cortafuegos propio que hay que comprobar.

### Cambios de código necesarios

1. **IP real del cliente en nginx.** Detrás de Caddy, nginx vería todas las peticiones con la IP de Caddy y el límite de login se aplicaría a todos los usuarios juntos. Hay que configurar nginx para leer la IP original.
2. **Documentación de la API en `/api/docs`.** Hoy solo se alcanza por el puerto 8000 local; en producción debe servirse a través del mismo dominio.
3. **`docker-compose.prod.yml` y `Caddyfile`.**
4. **Guía de deploy** en el README.

### Reparto de pasos

Tú (requieren tu cuenta y tus credenciales):
1. Crear la cuenta de Oracle Cloud y la instancia, con tu clave SSH.
2. Abrir los puertos en la red de Oracle.
3. Crear el subdominio o el registro DNS.
4. Crear el repositorio en GitHub.

Yo:
1. Los cambios de código de arriba, probados en local.
2. Inicializar git y dejar el primer commit listo.
3. Una guía paso a paso con los comandos exactos para la máquina.
4. Verificar el sitio publicado desde fuera: HTTPS, cabeceras, límite de login y flujo completo.

### Riesgos

- **Cupo de A1.** Es habitual que Oracle responda "out of capacity" al crear instancias gratuitas ARM; a veces hay que reintentar o cambiar de zona.
- **Límites de la capa gratuita.** Conviene confirmarlos en la consola al crear la instancia, porque Oracle los ajusta con el tiempo.
- **Datos de demo.** Las fechas de los eventos son relativas al momento de la carga inicial; si el deploy se hace días antes de la entrevista, hay que recargar los datos ese día para que las reglas de reembolso se vean como se espera.

## 11. Revisión de código y endurecimiento

Con el sitio ya publicado se revisó el programa completo. No apareció inyección SQL ni forma de sobrevender, pero sí huecos en reglas de negocio y en operación. Esta sección recoge qué se decidió y por qué.

### Reglas de negocio

| Problema | Decisión | Por qué así |
|----------|----------|-------------|
| Una cuenta podía reservar, dejar vencer y volver a reservar sin límite, dejando entradas fuera de la venta sin pagar | Tras 3 reservas canceladas o vencidas del mismo evento en una hora, se rechazan las nuevas (`429`) hasta que pase la hora | Se cuenta por cliente **y evento**: quien abandona en un evento puede seguir comprando en otro. Las compras confirmadas no cuentan, así que un cliente real no lo nota |
| El máximo de 10 entradas era por compra y se evadía con varias compras | Máximo de 20 por cliente y evento, sumando compras confirmadas | Se mantienen los dos límites: 10 acota cada operación, 20 acota al cliente. Las entradas devueltas liberan cupo |
| Reintentar un reembolso tras un fallo de red devolvía más entradas | `Idempotency-Key` también en el reembolso | La compra ya se bloqueaba durante el reembolso; buscar la clave bajo ese bloqueo resuelve también los reintentos simultáneos, sin manejar errores de unicidad |
| Redondeo por operación: al 50 %, dividir un reembolso pagaba medio centavo más por entrada | Redondear el importe de **una** entrada y multiplicar | El resultado no depende de cómo se agrupen las entradas |
| Se podía confirmar una reserva con el evento empezado | Confirmar comprueba la fecha | Coherente con que tampoco se puede reservar |
| "VIP" y "vip" podían coexistir en un evento | Índice único sobre `lower(name)` | La regla queda en la base de datos, no solo en el código |
| Se podían crear eventos en el pasado | Validación en el esquema de entrada | |

### API y operación

- **Paginación** en `GET /events` y `GET /purchases`, y eventos pasados ocultos por defecto: cada navegador pide estas listas cada 3 segundos.
- **Desglose de ventas solo para el administrador.** El público sigue viendo las disponibles y el aforo, que es lo que necesita la barra de disponibilidad. El total ocupado se puede deducir restando; ocultarlo del todo exigiría quitar la barra.
- **Registro de auditoría** en el log, una línea JSON por operación, con un id de petición generado por el servidor y devuelto en `X-Request-ID`. Se eligió el log y no una tabla: las filas de `purchases` y `refunds` ya son el registro de negocio; el log añade quién lo pidió y en qué petición, sin escribir más en cada transacción.
- **`/health` consulta la base de datos**, para que Docker no dé por sana una API que no puede atender.
- **Límites en nginx** para todas las escrituras y para el tráfico total por IP, además del de login.
- **Datos de demostración opcionales** (`SEED_DEMO_DATA`): activos en local, apagados por defecto en producción. La cuenta de administrador se crea siempre.
- **CORS cerrado por defecto:** el frontend y la API comparten origen.
- **Integración continua** en GitHub Actions: linter y formato (`ruff`), tipos (`mypy`), migraciones contra modelos, tests de backend y frontend, y construcción de las imágenes.

### Lo que se dejó como está, y por qué

- **El registro revela si un correo ya existe.** Ocultarlo de verdad exige verificar el correo; sin envío de correos cualquier cambio sería cosmético.
- **Sin bloqueo por cuenta tras fallos de login.** En un sitio público permitiría a cualquiera dejar fuera al usuario legítimo. Queda el límite por IP.
- **La documentación de la API sigue pública en producción,** porque los entrevistadores la usan para revisar la solución.
- **Los límites por cliente se rodean creando más cuentas.** La solución es verificar el correo o un captcha, fuera del alcance del reto.
