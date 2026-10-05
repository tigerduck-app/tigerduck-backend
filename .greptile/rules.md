# Review rules for tigerduck-backend

## Development mode is deliberately open on the LAN

`docker-compose.dev.yml` is a development-only overlay. The repo's scripts
load it only when `TIGERDUCK_ENV=development` in `.env` (see
`_compose-files.sh`); production never runs it. In that mode, publishing
services on every interface with no sign-in is the intended design, chosen
by the maintainer so the stack can be reached from phones and other
machines on the developer's network while debugging. Do not report it as a
security issue. That covers:

- Grafana on `:40020` with anonymous access as Admin and no login, including
  Explore and its read-only PostgreSQL datasource (`tigerduck_monitor`,
  `pg_read_all_data`)
- Prometheus on `:40021`
- The backend on `:40000` and the operator portal on `:40010`, which have
  always been published the same way in development

Do flag anything that would make these reachable without authentication in
production: a port published in `docker-compose.yml` itself, Grafana
anonymous access or a login form enabled outside the dev overlay, Cloudflare
Access JWT checks weakened, or the backend's metrics port bound beyond the
`tigerduck-monitoring` network.
