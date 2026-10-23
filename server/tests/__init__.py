import os

# Imported before conftest and every test module. An unset TIGERDUCK_ENV means
# production, which refuses to start without a shared secret, so a Settings()
# built here or in `server.main` at import time would fail.
os.environ.setdefault("TIGERDUCK_ENV", "development")
