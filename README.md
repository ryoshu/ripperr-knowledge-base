# Ripperr knowledge base

An index of ripperr transcripts, and an MCP server that lets agents search it
with citations. It consumes only ripperr's documented HTTP change feed. It keeps an
independent SQLite database on the always-on Mac Pro with FTS5 and sqlite-vec
indexes over speaker-scoped transcript windows. Ripperr can be offline while
this database remains searchable.

## Setup

Install a Python 3.11 runtime and run `uv sync --locked --python 3.11`. Put the
ripperr bearer token in `~/.config/ripperr-knowledge-base/api-token` with mode `600`. The
default ripperr URL is `http://127.0.0.1:8876` (ripperr runs on this host); override it
with `RIPPERR_API_URL` when deploying elsewhere. No token is stored in the repo.

```sh
uv run kb sync
uv run kb status
uv run kb search 'What was the J.K. Dobbins paradox?' -k 5
uv run kb eval eval/podcast_questions.jsonl
uv run kb serve-indexer
uv run kb serve-mcp             # MCP tools on 127.0.0.1:8877/mcp
uv run kb discover 'dynasty fantasy football' --backfill 5
```

The initial sync calls ripperr's bootstrap endpoint once. The cursor and every
document, FTS row, and vector row covered by an event commit together. A
pruned cursor triggers a new bootstrap; the old podcast documents are pruned
only after its full event range has been replayed. A failed request or model
run leaves the last committed cursor intact. The continuous indexer backs off
when ripperr is unreachable and keeps its existing index available.

The index defaults to `~/.local/share/ripperr-knowledge-base/index.db`; the local CPU model
cache defaults to `~/.cache/ripperr-knowledge-base/models`. Embeddings use Model2Vec's
retrieval-tuned `minishlab/potion-retrieval-32M` (512 dimensions). Its static
CPU runtime works on the Mac Pro's Intel macOS 12 installation. A model or chunker change requires
a new index database and bootstrap. The `uv.lock` file pins the runtime
dependencies; the model is downloaded into the cache on first use.

For the Mac Pro, run `KB_MCP_ALLOWED_HOSTS=<tailscale name> python3 deploy/install_launchd.py`
inside this checkout, then `launchctl bootstrap gui/$(id -u)` each of
`~/Library/LaunchAgents/com.ryoshu.ripperr-knowledge-base.{indexer,mcp,ingest}.plist`.
The installer generates user-specific plists without putting secrets in them.

## MCP server

`kb serve-mcp` serves five tools over streamable HTTP (stateless, JSON
responses) on `127.0.0.1:8877/mcp`:

| Tool | Returns |
| --- | --- |
| `kb_search(query, k, feed, speaker, source_type, published_after, published_before)` | Up to 20 hybrid-ranked passages |
| `kb_get_span(guid, start_s, end_s)` | One episode's transcript between two times (at most 15 minutes), without the words split chunks repeat |
| `kb_list_sources()` | Indexed feeds with document counts and date ranges |
| `kb_discover(query, limit)` | Podcast feeds on a topic, as `kb discover` lists them; read-only |
| `kb_subscribe(feed_url, title, backfill)` | Subscribes ripperr to a feed after checking it is one; marked as a write, so clients ask before calling it |

Every passage comes with a citation (title, feed, published date, speaker,
start and end seconds, `h:mm:ss`, URL with `t=` for YouTube, guid and
revision), and its text sits in a `quote` field next to a notice that it is
untrusted source material. Responses carry at most 24,000 characters of quotes.
Discovery results carry the same kind of notice, and say that subscribing is
the user's call. Keep `kb_subscribe` out of any client's auto-approve list.
The server reads the ripperr token from `api-token` for discovery and
subscribing.

It needs its own bearer token in `~/.config/ripperr-knowledge-base/mcp-token`
(mode `600`; separate from the ripperr token, so a client cannot write to
ripperr). On the Mac Pro it is exposed on the tailnet with
`tailscale serve --bg --https=8443 http://127.0.0.1:8877`; set
`KB_MCP_ALLOWED_HOSTS` to that hostname so DNS-rebinding protection accepts it.
Register it in Claude Code with:

```sh
claude mcp add --scope user --transport http knowledge-base \
  https://rickys-mac-pro.taile4827e.ts.net:8443/mcp \
  --header "Authorization: Bearer $(ssh hastur@rickys-mac-pro cat .config/ripperr-knowledge-base/mcp-token)"
```

## Discovery

`kb discover QUERY` searches SearXNG's `podcasts` category
(`KB_SEARXNG_URL`, default `https://search.ryoshu.com`), resolves each result
to an RSS feed, and lists it with SearXNG's episode count and the feed's
newest episode. A result resolves if it is a feed itself (Podchaser returns
feed URLs), an Apple Podcasts link (via the iTunes lookup API), or a page with a
`<link rel="alternate">` feed. Feeds ripperr already follows are marked.

At a terminal it then asks which to subscribe to and calls ripperr's
`POST /v1/feeds`, with `--backfill N` to take each feed's N newest episodes
(1 to 100) instead of only the latest. Without a terminal it only lists:
nothing subscribes without a person choosing. Requests are sequential and read
at most 512 KB of each feed. Ripperr prepares new episodes on its next hourly
run, a worker transcribes them, and the indexer picks them up.

Podchaser matches titles and descriptions, so topic queries work better than
host names.

## Deploying to the Mac Pro

Develop on the laptop and push to GitHub. The Mac Pro checkout
(`~/ripperr-knowledge-base`) has no GitHub credentials; it accepts pushes
directly (`receive.denyCurrentBranch=updateInstead`), so deploy with:

```sh
git push origin main && git push macpro main      # macpro = hastur@rickys-mac-pro:ripperr-knowledge-base
ssh hastur@rickys-mac-pro 'launchctl kickstart -k gui/$(id -u)/com.ryoshu.ripperr-knowledge-base.mcp'
```

When dependencies change, install the `uv export --frozen --no-hashes` output
there with `.venv/bin/pip install -r`, then `pip install --no-deps -e .`.

## Evaluation

`eval/podcast_questions.jsonl` contains 22 questions tied to real transcript
passages, with episode GUIDs and approximate timestamps. `kb eval`
reports passage hit rate at ranks 5 and 10 plus mean rank among hits. The
questions were chosen from the initial ripperr corpus, so the baseline measures
regressions on that corpus; add new questions as the collection grows.

Run `uv run pytest` for transactional sync, reset, revision, and retrieval tests.

## Web ingest

`kb serve-ingest` listens on `127.0.0.1:8878` with its own bearer token
(`~/.config/ripperr-knowledge-base/ingest-token`, separate from the read-only
MCP token). The crawler on the VPS posts cleaned markdown:

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"url":"https://example.com/post","title":"Post","markdown":"..."}' \
  https://<tailscale name>:8444/v1/pages    # tailscale serve --bg --https=8444 http://127.0.0.1:8878
```

The URL is canonicalised (lowercase host, no fragment, tracking parameters or
trailing slash). The same URL with the same content hash returns `unchanged`
and only refreshes `fetched_at`; changed content bumps the revision and
replaces the page's chunks. Pages are indexed as `source_type = web`, with the
host as the feed, so `kb_search` filters work. The endpoint never fetches
anything. `scripts/crawl_push.py` runs on the VPS in its `~/crawl4ai-env`: it crawls URLs one at a time with Crawl4AI and posts them (`~/crawl4ai-env/bin/python ~/ripperr-crawl/crawl_push.py URL ...`). The VPS cannot resolve `.ts.net` names, so it connects by the Mac Pro's tailnet IP. Pages are chunked by paragraph into windows of up to 350 words.
A rebuild from ripperr starts a new database, so web pages must be re-posted.

## Boundaries

This repo provides index sync, search, the MCP server and podcast discovery.
Web pages arrive through the ingest endpoint below. It does not read
ripperr's SQLite database or import ripperr internals.

`cryptography` is held below 49 because later releases ship no Intel macOS
wheel. The Mac Pro has no uv; install from `uv export --frozen` output with pip.
