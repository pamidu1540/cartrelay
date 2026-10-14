# Architecture and compatibility

CartRelay is a single-store ecommerce application: one Telegram bot, one commerce engine, one database. It does not implement tenant isolation for independent stores.

## Purchase flow

1. The bot loads customer profiles, addresses, and catalog data through gRPC.
2. Cart changes are persisted through the engine.
3. Checkout requests a quote covering stock, products, taxes, delivery, promotions, and loyalty.
4. The engine validates the request and stores an order with an idempotency key.
5. Administrators manage fulfillment and optional driver assignments. The bot polls pending notifications.

Payment collection takes place outside CartRelay. A recorded order is not proof of payment.

## Boundaries

- PostgreSQL stores business data. Embedded migrations run when the engine starts.
- Identity comes from Telegram; the bot supplies actor metadata, and the engine checks HMAC authentication and admin permissions.
- The engine and bot share a Unix socket volume. Compose publishes no database or engine port.
- HMAC authenticates requests but does not encrypt them. Keep the socket private.
- Catalog and distance caches accelerate reads; PostgreSQL remains the source of truth.
- The supervisor is optional: source stacks can be operated with Docker Compose directly.

## Naming compatibility

The product and repository are CartRelay. These identifiers remain compatible with existing clients and deployments:

| Identifier | Reason retained |
| --- | --- |
| `logistics.LogisticsEngine`, protobuf filenames | Existing gRPC clients and generated bindings |
| Go modules `logistics-engine`, `logistics-supervisor` | Internal imports |
| `/app/sockets/logistics.sock` | Shared transport |
| `/opt/logistics-system`, `logistics.service`, container names | Host deployment and supervisor operations |
| Legacy release binary names | Existing release tooling |

The installed command is `cartrelay`; `logistics` remains an alias. Do not rename Docker projects or persistent volumes on an existing store without a verified backup and migration plan.
