# Bounded EPUB text archive v2

Document maintainers provide one ZIP EPUB with mimetype=application/epub+zip, META-INF/container.xml, one OPF rootfile, dc:title/dc:identifier, a manifest and a nonempty unique spine of XHTML items. This is a text-focused EPUB subset, not a complete EPUB conformance validator or rendering engine. The code preserves declared spine order, extracts namespace-aware body text, excludes script/style content, collapses whitespace and indexes chapter title/body using SQLite FTS5 unicode61. It stores book source SHA256 and chapter paths in an independent SQLite archive; queries do not reopen the EPUB. The fixture generator creates original prose and deterministic ZIP metadata.

No extraction to disk or external resource fetching occurs. Paths must be canonical relative POSIX paths with no dot/dot-dot segments, absolute names, backslashes or remote manifest URLs. Duplicate/encrypted ZIP members, missing spine members, non-XHTML spine items, DTD/entity XML and unknown spine refs fail. Limits: 500 members, 20 MB expanded total, 2 MB per referenced member. XML parsing additionally rejects non-UTF-8 input. No DRM, OCR, CSS layout, cover rendering or EPUB navigation model is supported. Text joins XML text nodes with spaces, so inline-element typography may differ from rendered prose.

Search accepts a nonempty <=200-character query, tokenizes Unicode letters/digits and ANDs literal tokens; it does not accept raw FTS operators. Results follow spine position and include snippets, capped at 100 chapters. Requires SQLite built with FTS5; availability is tested. CLI builds a temporary archive and atomically hard-links it into a new target, refusing existing non-archive files and symlinks. Any extraction/import failure leaves an existing target unchanged.

## Versions

A book is identified solely by its EPUB package identifier (`dc:identifier`); a version is identified solely by the source EPUB SHA-256. One archive holds one package identifier and any number of its versions.

- First import still requires a target path that does not exist; the archive is fully built in a temporary file and atomically hard-linked into place.
- Importing a new SHA for the same identifier fully validates the EPUB and stores it as a new version: its own spine order, chapter paths, chapter text and source digest. Old chapters and old FTS rows are never overwritten or deleted.
- Re-importing an identical SHA is idempotent: no version, chapter or search row is duplicated; the response reports `action: "unchanged"`.
- An EPUB whose SHA already exists under a different package identifier is rejected as an identifier conflict; an EPUB with a different identifier is rejected against an occupied archive.
- The newest successfully imported version becomes the current version. A failed import rolls back its single transaction, so no half-imported version is searchable and the previous version's results do not change. Version selection can also be changed explicitly; only a fully saved version can ever be selected.
- `versions` lists every version with id, SHA-256, import time, chapter count and current flag. Search, annotation and pending commands accept a version selector: an integer version id, a full SHA-256, or omitted for the current version.

The FTS5 table keeps the same literal-token query and the same spine-position ordering; each row additionally carries unindexed version and chapter-position columns so queries are scoped to one version without changing tokenization or ranking.

## Reader annotations

An annotation is anchored to one chapter of one selected version and stores:

- the book and source version identity;
- the original chapter identity (spine position and member path at annotation time);
- the exact raw-text excerpt selected by the reader;
- at most 32 Unicode characters immediately before and after the excerpt, as saved context clues;
- the reader's note text, stored verbatim and never rewritten.

The excerpt must be a nonempty contiguous substring of the chapter's extracted body text. Empty excerpts, excerpts that do not occur in that chapter, and excerpts occurring more than once in the same chapter are rejected. Anchoring uses exact substring matching on extracted text only; FTS tokenization results, wildcard syntax and whitespace-normalized phrases are never accepted as anchors.

## Relocation when a new version is imported

Importing a new version drives, inside the same transaction, a per-annotation resolution over every chapter text of that version, using the saved excerpt and its saved context:

1. Every exact occurrence of the excerpt is found by raw substring search.
2. An occurrence is a candidate only when at least one saved, non-empty context clue still matches the text immediately bordering it: the saved prefix equals the text directly before the occurrence, or the saved suffix equals the text directly after it.
3. Exactly one candidate across the whole version marks the annotation `located` on that chapter and excerpt offset. More than one candidate marks it `ambiguous`; none marks it `missing`.

Chapter moves, renames and path changes never alter the excerpt or the reader's note, and are handled purely through text matching. Located annotations are the only annotations attached to search hits for that version. Ambiguous and missing annotations are never attached to a chapter hit, but remain readable through the pending list together with the original anchor (source version, chapter position/path), excerpt, both context clues and verbatim note; ambiguous entries additionally enumerate every candidate. Pending annotations are never auto-resolved, and no reader text is modified or deleted. Each version keeps its own locations, so searching an old version still displays the annotations located on that old version, including its original chapter placement.

Example: an annotation on excerpt `old quay` in chapter two of version 1 moves with its paragraph into chapter three of version 2 while an identical excerpt with different context appears in another chapter. Version 2 search shows the annotation only on the chapter-three hit; version 1 search still shows it on chapter two. If two version-2 occurrences both satisfy a context clue, the annotation is ambiguous and hidden from search; if the excerpt disappears, it is missing and stays in the pending list.

## CLI

All commands emit JSON on stdout and exit 2 with a message on stderr for rejected input.

- `import --source EPUB --db DB` — first import (new target) or a new version for an existing archive; repeated SHA is idempotent.
- `versions --db DB` — list versions with the current flag.
- `select --db DB --version ID_OR_SHA` — set the current version.
- `annotate --db DB --chapter POSITION --excerpt TEXT [--excerpt-file PATH] --note TEXT [--note-file PATH] [--version ID_OR_SHA]` — add an annotation; file inputs contain a single JSON string, and inline/file inputs are mutually exclusive.
- `search --db DB --query TEXT [--version ID_OR_SHA]` — literal-token FTS query scoped to one version; hits include only located annotations.
- `pending --db DB [--version ID_OR_SHA]` — ambiguous/missing annotations with preserved anchors, context and notes.

### Author catalogue

An archive also catalogs the OPF `dc:creator`/`dc:contributor` declarations of every imported version and lets the reader declare which of those names belong to the same canonical author identity.

- `author-add --db DB --name NAME` — create an independent canonical identity. Display names may repeat; creating an identity never matches or merges it with another.
- `authors --db DB` — list identities with their current aliases.
- `alias-add --db DB --author ID --alias NAME` / `alias-remove --db DB --author ID --alias NAME` — add or revoke an exact full-name mapping. Aliases are exact strings: no case folding, trimming of interior spaces, fuzzy matching or automatic merge.
- `contributors --db DB [--db DB ...] [--catalogue DB] [--version ID_OR_SHA]` — every raw source declaration with its current resolution.
- `unresolved --db DB [...] [--catalogue DB] [--version ID_OR_SHA]` — declarations that are currently `unmapped` or `ambiguous`.
- `author-books --db DB [--db DB ...] [--catalogue DB] --author ID [--version ID_OR_SHA]` — books and versions whose declarations uniquely resolve to that identity.

Each declaration records its source `version_id`/`version_sha256`, `category` (`creator`/`contributor`), 1-based `ordinal` within that category, the original `name`, the OPF `role` (`null` when absent) and any remaining OPF attributes. Declarations are stored per version and never rewritten, merged across versions or replaced by a canonical name. A repeated SHA remains idempotent; a new SHA stores its own declarations inside the same transaction, so a failed import leaves none behind.

Resolution is computed from the current catalogue on every query: an identity's display name and its aliases are its exact names. A declaration is `resolved` only when exactly one identity maps its name; with no mapping it is `unmapped`, and with two or more identities it is `ambiguous` and belongs to none of them. Adding or revoking an alias therefore moves matching declarations in or out of an identity's book list without touching stored data.

Multiple archives can be named for a query; the first (or `--catalogue`) owns identities and aliases, and all named archives contribute declarations. Resulting book groups are keyed by archive plus book, and per-version declarations are never deduplicated.

Example: a book declaring `J. Lee` appears under `Jordan Lee` only after `alias-add --alias 'J. Lee'`; revoking it makes the declaration unresolved again while the raw name, category, order, role and version remain readable.

Synthetic fixtures stay original and small: the two-chapter sample, a four-variant version set (unique relocation, ambiguous relocation, missing chapter) exercising every relocation state, and metadata-only EPUBs for the author catalogue.
