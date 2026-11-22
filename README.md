# Support

**Developer-First Terminal Database Workspace**

Support is an AI-assisted database intelligence and operations workspace designed for backend and full-stack developers. It helps engineers understand database structures, investigate data, generate SQL, and perform controlled database operations.

---

## Intended Users

- **Backend Engineers**: Inspecting schemas, reviewing relationships, and composing queries without leaving the terminal.
- **Full-Stack Developers**: Exploring unfamiliar databases and safely verifying data changes during development.

---

## Current Status

Support is currently at its clean foundational stage (**v0.1.0**).

The current implementation provides:
- Minimal Typer-based CLI entry point.
- Version inspection (`support --version`, `-V`).
- Help options (`support --help`).
- Foundational test suite with pytest.

Database connectivity, profile management, and schema operations will be introduced incrementally in subsequent phases.

---

## Development Setup

### 1. Prerequisites
- **Python**: Version 3.11 or higher (`python3 --version`).

### 2. Create and Activate Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install in Editable Mode
Install the package in editable mode with development dependencies:
```bash
pip install -e ".[dev]"
```

---

## Usage

### Run Support
```bash
# Display welcome message
support

# View version information
support --version
support -V

# View available options
support --help
```

---

## Running Tests

Run the test suite using pytest:
```bash
pytest -v
```
