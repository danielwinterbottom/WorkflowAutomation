# WorkflowAutomation

Portable workflow orchestration for local development and cluster production.

## Bootstrap HiggsDNA

The bootstrap command has no third-party Python dependencies. It clones HiggsDNA into the local
`workspaces/` directory when missing. When the checkout already exists, it validates the `origin`
and reports the current commit and whether local changes are present. It deliberately does not pull,
reset, or overwrite an existing checkout.

```bash
./workflow bootstrap HiggsDNA
```

Choose a different checkout root on the cluster without changing the workflow definition:

```bash
./workflow bootstrap HiggsDNA --workspace /path/to/group/workspaces
```

Repository URLs, revisions, and directory names are declared in
`config/repositories.json`.

For operating instructions, expected behavior, troubleshooting, and the documentation conventions
for future workflow steps, see [`docs/operator-guide.md`](docs/operator-guide.md).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
