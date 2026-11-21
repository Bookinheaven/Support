# Support

**Developer-First Terminal Database Workspace**

[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests Passing](https://img.shields.io/badge/tests-passing-brightgreen.svg)](tests/)

---

## Overview

**Support** is an open-source, terminal-native database workspace being built in Python. Its goal is to provide developers with a reliable, terminal-based environment to interact with relational databases, inspect schemas, explore data relationships, and perform controlled database operations using conversational AI paired with deterministic software engineering.

> [!NOTE]
> The project is currently undergoing an incremental rebuild from a clean foundation. Features are being introduced step-by-step alongside dedicated test coverage.

---

## Current Status (v0.1.0)

The application currently provides:
- **CLI Framework**: Minimal Typer-based entry point with `--help` and `--version` (`-v`, `-V`).
- **Interactive Welcome Banner**: Clean startup output directing users to available CLI options.
- **Automated Tests**: Unit test suite verifying CLI invocation, option flags, and help messaging.

Architecture and vision documents for the roadmap ahead:
- [`VISION.md`](VISION.md) — Product vision, core capabilities, scope, and engineering principles.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Target system architecture, in-memory schema graph, execution engine, and security boundaries.
- [`CURRENT_STATE.md`](CURRENT_STATE.md) — Architectural audit and technical baseline.

---

## Installation & Development Setup

### 1. Prerequisites
- **Python Runtime**: Python 3.11 or higher (`python --version`).
- **Virtual Environment**: Recommended to install in an isolated `.venv`.

### 2. Setup Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Package
```bash
# Install core package with development dependencies (pytest)
pip install -e ".[dev]"
```

---

## Usage

### General CLI Commands
```bash
# Launch welcome banner
support

# Check version
support --version
# Or short flag:
support -v

# View available options
support --help
```

---

## Running Tests

Run the test suite using `pytest`:

```bash
pytest -v
```

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
