# The sync bridge with GitGalaxy

`squid-bot` was forged inside **[GitGalaxy](https://github.com/squid-protocol/gitgalaxy)**, where it regulates multi-agent editing (4,800+ merged pull requests as of Oct 26, 2026).

To allow continuous evolution inside GitGalaxy while maintaining a clean, generalized open-source package, `squid-bot` includes a bi-directional sync bridge (`scripts/sync.py`):

```bash
# Check drift between GitGalaxy and squid-bot
python scripts/sync.py status
python scripts/sync.py diff

# Pull recent improvements from GitGalaxy into squid-bot
python scripts/sync.py pull

# Push generalized fixes from squid-bot back to GitGalaxy (see the warning below)
python scripts/sync.py push
```

The bridge translates paths, abstracts repository slugs, maps package imports, updates lock variables, and adapts test suites. `pull` is safe. **`push` is not a clean reverse:** it writes squid-bot's generalizations into GitGalaxy as well, including the default lock directory, which would split GitGalaxy's heavy-job slots between old and new agents. To move a change into GitGalaxy, `git format-patch` the commit, rewrite its paths (`src/squid_bot/X` → `tests/tools/[box/]X`, `tests/test_X` → `tests/tools/test_X`) and `git apply` it in a fresh GitGalaxy worktree.
