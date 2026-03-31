# datasette-agent

[![PyPI](https://img.shields.io/pypi/v/datasette-agent.svg)](https://pypi.org/project/datasette-agent/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-agent?include_prereleases&label=changelog)](https://github.com/datasette/datasette-agent/releases)
[![Tests](https://github.com/datasette/datasette-agent/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-agent/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-agent/blob/main/LICENSE)

An LLM-powered agent assistant for Datasette

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-agent
```
## Usage

Visit `/-/agent` to start a conversation with the agent.

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-agent
# Confirm the plugin is visible
uv run datasette plugins
```
To run the tests:
```bash
uv run pytest
```
