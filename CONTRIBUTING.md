# Contributing to CartRelay

Open an issue describing the expected behavior and reproducible bug or proposed feature. Include redacted logs; never include credentials or customer records.

## Checks

Use Go 1.25+, Python 3.13+, and uv. Install bot dependencies with `uv sync --directory bot --all-extras`.

```bash
cd engine
go test ./...
go vet ./...
cd ../supervisor
go test ./...
go vet ./...
cd ..
uv run --directory bot ruff check src tests
uv run --directory bot pytest tests
```

Most tests need no Telegram credentials. Engine database tests use `TEST_DATABASE_URL`; use only a disposable database. Unit checks do not replace a real Telegram checkout test.

Gitleaks scans should keep the narrowly scoped historical template exception in `.gitleaksignore`. Do not add broad rules that hide future credentials.

## API changes

Edit `engine/api/logistics.proto` and commit the generated Go/Python bindings together. Preserve field numbers and existing package, service, and RPC names. Generate Go bindings with protoc and its Go plugins; Python bindings use `python -m grpc_tools.protoc`. Preserve the relative import in `bot/src/api/logistics_pb2_grpc.py`.

CI validates committed code and never pushes generated changes back to your branch. Keep changes focused and document behavior changes. Use dummy secrets and anonymized fixtures in regression coverage.
