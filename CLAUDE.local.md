## Project overview

Lipservice is an IRC bouncer with a RESTful HTTP + SSE client protocol.
Python, FastAPI, asyncio. No database yet — all state is in-memory.

## Build & test

```bash
pip install -r requirements.txt
python -m pytest tests/          # unit tests (mocked IRC)
python tests/test_live.py        # integration test against irc.oftc.net
mypy lipservice                  # type checking — keep this clean
```

## Conventions

- Use `python`, never `python3`, in scripts and docs.
- Type-hint everything; run mypy before committing.
- Apache 2.0 license.
- The user prefers the term "networks" as a mnemonic for IRC servers.

## Git

- Always use `commit` (the shell alias/script), never bare `git commit`,
  which is booby-trapped by the Cursor front end.

## Things to remember

- Robustness across server restarts: auto-reconnect with exponential
  backoff (1 s → 5 min cap) is implemented; NickServ IDENTIFY is sent
  (if configured) and channels are rejoined automatically after reconnect.
- Connection liveness is probed with PING every 60 s; dead after 120 s
  of silence.
- "Infinite memory": message backlog is bounded by LIPSERVICE_MAX_BACKLOG
  (default 1000 per channel/query). There is no persistent storage yet.
- Explicit disconnect (POST /disconnect) cancels auto-reconnect;
  connection drops do not.
