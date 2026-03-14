# Lipservice

An IRC proxy (bouncer) with a RESTful HTTP API. Lipservice maintains persistent
connections to IRC servers and exposes them to clients over REST + Server-Sent
Events.

## Quick Start

```bash
pip install -r requirements.txt
python3 -m lipservice
```

The server starts on `http://127.0.0.1:8080`.

## Configuration

All settings are read from environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LIPSERVICE_HOST` | `127.0.0.1` | Listen address |
| `LIPSERVICE_PORT` | `8080` | Listen port |
| `LIPSERVICE_USER` | `admin` | Proxy account username |
| `LIPSERVICE_PASS` | `changeme` | Proxy account password |
| `LIPSERVICE_TOKEN_LIFETIME` | `86400` | Auth token lifetime in seconds |
| `LIPSERVICE_MAX_BACKLOG` | `1000` | Max messages kept per channel/query |

## Usage

### Authenticate

```bash
curl -X POST http://localhost:8080/api/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"changeme"}'
```

Returns a bearer token. Use it on all subsequent requests:

```bash
-H 'Authorization: Bearer <token>'
```

### Add and connect to a network

```bash
# Create
curl -X POST http://localhost:8080/api/networks \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"libera","host":"irc.libera.chat","port":6697,"tls":true,"nick":"mynick"}'

# Connect
curl -X POST http://localhost:8080/api/networks/libera/connect \
  -H 'Authorization: Bearer <token>'
```

### Join a channel and send a message

```bash
# Join
curl -X POST http://localhost:8080/api/networks/libera/channels \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"#test"}'

# Send
curl -X POST http://localhost:8080/api/networks/libera/channels/%23test/messages \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello from lipservice"}'
```

### Read messages

```bash
curl http://localhost:8080/api/networks/libera/channels/%23test/messages?limit=20 \
  -H 'Authorization: Bearer <token>'
```

### Stream events (SSE)

```bash
curl -N http://localhost:8080/api/events \
  -H 'Authorization: Bearer <token>' \
  -H 'Accept: text/event-stream'
```

## API Overview

All endpoints are under `/api`. See [doc/client-protocol.md](doc/client-protocol.md)
for the full specification.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/token` | Obtain a bearer token |
| `DELETE` | `/auth/token` | Revoke current token |
| `GET` | `/networks` | List networks |
| `POST` | `/networks` | Create a network |
| `GET` | `/networks/:name` | Get a network |
| `PATCH` | `/networks/:name` | Update a network |
| `DELETE` | `/networks/:name` | Delete a network |
| `POST` | `/networks/:name/connect` | Connect to IRC server |
| `POST` | `/networks/:name/disconnect` | Disconnect from IRC server |
| `GET` | `/networks/:net/channels` | List joined channels |
| `POST` | `/networks/:net/channels` | Join a channel |
| `GET` | `/networks/:net/channels/:chan` | Get channel info |
| `DELETE` | `/networks/:net/channels/:chan` | Part a channel |
| `PUT` | `/networks/:net/channels/:chan/topic` | Set topic |
| `GET` | `/networks/:net/channels/:chan/members` | List members |
| `GET` | `/networks/:net/channels/:chan/messages` | List messages |
| `POST` | `/networks/:net/channels/:chan/messages` | Send a message |
| `GET` | `/networks/:net/messages/:nick` | List private messages |
| `POST` | `/networks/:net/messages/:nick` | Send a private message |
| `GET` | `/user` | Get proxy user info |
| `GET` | `/networks/:net/user` | Get IRC identity |
| `PUT` | `/networks/:net/user/nick` | Change nick |
| `POST` | `/networks/:net/raw` | Send raw IRC command |
| `GET` | `/events` | SSE event stream |
| `GET` | `/status` | Proxy status |

## Project Structure

```
lipservice/
├── doc/
│   └── client-protocol.md   Protocol specification
├── lipservice/
│   ├── app.py                FastAPI application
│   ├── auth.py               Token authentication
│   ├── config.py             Environment-based settings
│   ├── irc.py                Async IRC client
│   ├── models.py             Pydantic request/response models
│   ├── routes.py             REST API routes
│   └── state.py              In-memory state and event bus
├── requirements.txt
└── README.md
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
