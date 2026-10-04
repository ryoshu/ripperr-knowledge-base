# Ripperr knowledge base indexer

This service consumes only ripperr's documented HTTP change feed. It keeps an
independent SQLite database on the always-on Mac Pro with FTS5 and sqlite-vec
indexes over speaker-scoped transcript windows. Ripperr can be offline while
this database remains searchable.

## Setup

Install a Python 3.11 runtime and run `uv sync --locked --python 3.11`. Put the
ripperr bearer token in `~/.config/ripperr-kb/api-token` with mode `600`. The
default ripperr URL is `https://nyarlathotep.taile4827e.ts.net`; override it
with `RIPPERR_API_URL` when deploying elsewhere. No token is stored in the repo.

```sh
uv run kb-index sync
uv run kb-index status
uv run kb-index search 'What was the J.K. Dobbins paradox?' -k 5
uv run kb-index eval eval/podcast_questions.jsonl
uv run kb-index serve-indexer
```

The initial sync calls ripperr's bootstrap endpoint once. The cursor and every
document, FTS row, and vector row covered by an event commit together. A
pruned cursor triggers a new bootstrap; the old podcast documents are pruned
only after its full event range has been replayed. A failed request or model
run leaves the last committed cursor intact. The continuous worker backs off
when the laptop is asleep and keeps its existing index available.

The index defaults to `~/.local/share/ripperr-kb/index.db`; the local CPU model
cache defaults to `~/.cache/ripperr-kb/models`. Embeddings use Model2Vec's
retrieval-tuned `minishlab/potion-retrieval-32M` (512 dimensions). Its static
CPU runtime works on the Mac Pro's Intel macOS 12 installation. A model or chunker change requires
a new index database and bootstrap. The `uv.lock` file pins the runtime
dependencies; the model is downloaded into the cache on first use.

For the Mac Pro, run `python3 deploy/install_launchd.py` inside this checkout,
then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryoshu.ripperr-kb.indexer.plist`.
The installer generates a user-specific plist without putting secrets in it.

## Evaluation

`eval/podcast_questions.jsonl` contains 22 questions tied to real transcript
passages, with episode GUIDs and approximate timestamps. `kb-index eval`
reports passage hit rate at ranks 5 and 10 plus mean rank among hits. The
questions were chosen from the initial ripperr corpus, so the baseline measures
regressions on that corpus; add new questions as the collection grows.

Run `uv run pytest` for transactional sync, reset, revision, and retrieval tests.

## Boundaries

This repo currently provides local index sync and search. MCP transport and
the web-page ingestion endpoint are the next work items. It does not read
ripperr's SQLite database or import ripperr internals.
