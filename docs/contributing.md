---
icon: lucide/git-pull-request
---

# Contributing

Contributions are welcome! Here's how to get started.

## Development Setup

1. Clone the repository:

    ```bash
    git clone https://github.com/basnijholt/trueloc.git
    cd trueloc
    ```

2. Install development dependencies:

    ```bash
    uv sync --group dev
    ```

3. Install pre-commit hooks:

    ```bash
    pre-commit install
    ```

## Running Tests

```bash
pytest
```

Tests use `respx` to mock HTTP calls, so no GitHub API access is needed.

## Code Quality

The project uses several tools to maintain code quality:

### Linting

```bash
ruff check .
ruff format .
```

### Type Checking

```bash
mypy .
```

### All Checks

Pre-commit runs all checks automatically:

```bash
pre-commit run --all-files
```

## Project Structure

```
src/trueloc/
├── cli.py       # Typer commands (count, count-local, clear-cache)
├── display.py   # Rich table formatting and JSON output
├── github.py    # GitHubClient - API calls with caching
├── local.py     # Local git repo analysis (git log/show)
├── models.py    # Pydantic-style dataclasses for stats
└── utils.py     # Shared utilities (cache, token, date parsing)
```

## Pull Request Guidelines

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests and linting
5. Commit with a clear message
6. Push and open a PR

## Tech Stack

- **Python 3.12+**
- [Typer](https://typer.tiangolo.com/) - CLI framework
- [httpx](https://www.python-httpx.org/) - HTTP client
- [Rich](https://rich.readthedocs.io/) - Terminal output
- [diskcache](https://grantjenks.com/docs/diskcache/) - Persistent caching
- [dateparser](https://dateparser.readthedocs.io/) - Flexible date parsing

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
