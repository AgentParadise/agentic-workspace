# PII and infrastructure hygiene

This repository is public. Everything committed here is permanent and, once it
has been part of a pull request, is permanent on GitHub's side too.

## Never commit

- Absolute home paths. No `/Users/<name>/...`, no `/home/<name>/...`.
  Use repo-relative paths, `$(git rev-parse --show-toplevel)`, or a
  placeholder such as `/Users/private-person` or `<repo-root>`.
- Real hostnames for personal or internal infrastructure. No homelab DNS, no
  VPS nicknames, no internal service hostnames.
  Use `example.com`, `example.lan`, or `example-vps`.
- Real usernames, machine names, tailnet addresses, or LAN addresses that
  identify a specific person or host.

Private RFC1918 addresses, `127.0.0.1`, `0.0.0.0` and Docker bridge addresses
are fine. They identify nothing.

## Why this is strict

Rewriting history does not undo a leak here. When a pull request is opened,
GitHub snapshots the head commit into `refs/pull/N/head` on its own side. That
ref is server-controlled and read-only. No force push, ref delete, or garbage
collection removes it. The only remedies are a GitHub Support request or
deleting and recreating the repository, and both are expensive.

This repository has already been rebuilt once for exactly this reason.

## Before you commit

    git diff --cached | grep -nE '/Users/|/home/[a-z]|\.local\b|[a-z0-9-]+\.(xyz|lan|internal)'

Push protection and secret scanning are enabled, but they catch credentials,
not paths and hostnames. That part is on you.
