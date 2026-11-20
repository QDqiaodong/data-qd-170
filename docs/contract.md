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
- `import-batch --manifest MANIFEST --target DIR` — atomically publish one independent archive per listed EPUB plus a receipt; all-or-nothing.
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

## Batch intake

`import-batch` reads an ordered JSON manifest: an array of objects, each with exactly one `source` field naming a local EPUB path (relative paths resolve against the manifest file's directory; absolute paths are accepted). Every source is validated with the same bounded EPUB checks as a single import, and each book's package identifier and source SHA-256 are read during preflight. Book identity never comes from file names.

The batch is rejected as a whole — naming the manifest items and reasons — when any source is missing or invalid, when two items share a package identifier, or when two items name the same source file (same resolved path or same SHA-256). One identifier therefore maps to exactly one archive per published batch.

All validation and archive building happen inside a temporary sibling directory of the target. Only when every item succeeds is the directory atomically renamed to the target. The target must not already exist as anything (file, directory or symlink) and is never overwritten. On any item or publish failure the target does not exist and no visible partial archives remain; source EPUBs are only read, never moved, rewritten or deleted. If temporary-directory cleanup itself fails, the error reports the actual residue location and never claims the target was published.

Archive file names are derived from the package identifier but sanitized to a bounded safe alphabet (`NNN-slug-sha12.sqlite`), so identifier path characters can never steer the output location. The generated `receipt.json` inside the target (also the command's stdout) records, in manifest order, each input entry, identifier, SHA-256, title and generated archive file name, plus the manifest's own SHA-256 — every input traces to its actual database. Published archives use the same schema as single-import ones and work with every existing command, including later version imports of the same book.

Synthetic fixtures stay original and small: the two-chapter sample, a four-variant version set (unique relocation, ambiguous relocation, missing chapter) exercising every relocation state, and metadata-only EPUBs for the author catalogue.

## Journal anthology workspace

A separate offline workflow shares one local workspace directory and designates one existing archive as the common author catalogue. The workspace keeps its own `workspace.sqlite` and an `anthologies/` directory; every book archive and the catalogue are opened strictly read-only, so workspace operations never mutate an archived library. No login, network or third-party dependency is involved.

### Chapter selection and author identity

A plan is an ordered JSON array; each entry has exactly `archive`, `identifier`, `source_sha256` (64 lowercase hex characters) and `position` (a positive 1-based spine index). The archive path only locates the file: chapter identity is the triple (`identifier`, `source_sha256`, `position`). Saving a plan verifies the triple inside the named archive and rejects an identifier mismatch, a SHA absent from that archive, a missing position, a malformed digest and a repeated chapter within one issue. The current version, another version's same-position chapter and search snippets can never substitute for the explicitly selected chapter. Relative archive paths resolve against the plan file's directory and are stored resolved.

Licensing needs consider only `dc:creator` declarations stored on the selected version (`contributor` declarations never count). A selectable version must declare at least one creator, and every creator name must resolve through the catalogue's current exact-name rules to exactly one canonical identity; zero owners (`unmapped`) and multiple owners (`ambiguous`) both block reservation and publication with a named reason. Two declarations resolving to one canonical identity create a single demand for that chapter. Author ids always come from the catalogue's own `authors` table; an id that exists in another archive but not the catalogue is rejected rather than matched by coincidence.

### Licenses

Each license has a generated, never-reused code and an immutable scope fixed at registration: one canonical author, one book identifier, one exact version SHA, a nonempty set of allowed spine positions and a positive integer quota. Registration rejects unknown catalogue authors, unknown book versions or positions, repeated positions and non-positive quotas. Several active licenses may cover the same chapter and author. One quota unit covers exactly one (issue, chapter, author) triple: another issue reprinting the chapter or a second author of the same chapter each consumes another unit. Revocation marks the license unusable for future demand but always preserves its formal usage history.

### Reservations, feasibility and queue order

Saving a plan consumes no quota. A successful reservation commits the whole issue's demand set, records a monotonically increasing `reservation_seq` and snapshots the resolved author id and display name per chapter. Feasibility is a max-flow matching over demand units and active licenses: a license is eligible only when its author, identifier, version SHA and position scope match, and its capacity is the initial quota minus formal usage rows (revoked licenses are excluded). A new reservation is accepted only when the new issue together with every earlier reserved issue admits a flow covering all demands; a failed application never displaces an earlier reservation and leaves no partial author snapshot.

An editor must withdraw a reserved plan before changing it. Withdrawing releases only the reservation; the plan stays a draft and, when reserved again, receives a fresh sequence number at the tail of the queue. Each issue carries an event log (`reserved`, `reserve_failed`, `withdrawn`, `returned`, `publish_rejected`, `published`) with timestamps and reasons, so failed applications and revocations stay explainable.

### Revocation cascade

Revoking a license and the resulting reservation changes commit in one transaction. Reserved issues are visited in `reservation_seq` order. An issue keeps its reservation while its demands, together with every earlier issue already kept in this pass, remain fully satisfiable from the surviving pool. An issue that can no longer be paid for returns to draft, releases its whole reservation and records the revocation reason; later issues are evaluated without the returned issue occupying any capacity. An issue that still has enough other licenses is never returned merely because it might previously have used the revoked license. Published usage rows are untouched.

### Publication and frozen anthologies

Only a reserved issue can publish. Publication first re-opens the planned archives and re-runs version, chapter and creator resolution against the catalogue as it reads now: a missing archive, a creator that no longer uniquely resolves, or an author-id set different from the reserved set rejects the whole issue before any quota is touched, keeps the reservation and records a `publish_rejected` event. The license assignment is a feasible flow that also covers every other still-reserved issue, so publishing an issue cannot strand an earlier or later reservation; the assignment determines the concrete license code used by each author.

The publication record, every `license_usages` row and the final anthology commit in one transaction (with `BEGIN IMMEDIATE` serialization, so two concurrent applications can never spend the same capacity). An anthology entry stores, in plan order, the exact source identity, member `href`, chapter title and archived body text, the author ids and names as resolved at publication, and each author's actually used license code. Reader annotations are never included and published content is never recomputed against a later catalogue.

Repeating publication for an already published issue, or retrieving its anthology, recognizes the stored record and returns the original data without re-opening an archive or deducting quota again; the call therefore stays safe to retry after an interruption and succeeds even if a license was later revoked or the originating archive was moved away.

### CLI surface

The new commands follow the existing convention: JSON on stdout, a stderr message and exit 2 on rejected input.

- `workspace-init --workspace DIR --catalogue DB`
- `plan-save --workspace DIR --issue N --plan FILE`
- `plan-reserve --workspace DIR --issue N [--catalogue DB]`
- `plan-withdraw --workspace DIR --issue N`
- `plan-list --workspace DIR [--issue N]`
- `license-register --workspace DIR --catalogue DB --author ID --archive DB --identifier ID --version-sha SHA --positions P[,P...] --quota N`
- `license-revoke --workspace DIR --license CODE [--reason TEXT]`
- `license-list --workspace DIR [--license CODE]`
- `publish --workspace DIR --issue N [--catalogue DB]`
- `anthology --workspace DIR --issue N [--output FILE]`

An explicit `--catalogue` on reservation or publication must resolve to the same archive bound at `workspace-init`; a mismatch is rejected. Existing import, search, version, annotation, batch and author-catalogue commands are unchanged.

