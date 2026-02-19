# Contributing to Album Slideshow

Thank you for your interest in contributing to Album Slideshow! We welcome bug reports, feature requests, and pull requests.

## Reporting Issues

- Use [GitHub Issues](https://github.com/eyalgal/album_slideshow/issues) to report bugs or request features.
- Include your Home Assistant version, integration version, and steps to reproduce the issue.
- If relevant, include log output from **Settings → System → Logs** (filter by `album_slideshow`).

## Development Setup

1. Fork and clone the repository.
2. Copy the `custom_components/album_slideshow` folder into your Home Assistant development environment's `config/custom_components/` directory.
3. Restart Home Assistant to load the integration.

### Project Structure

```
custom_components/album_slideshow/
├── __init__.py        # Integration setup and service registration
├── camera.py          # Camera entity (image rendering and slideshow logic)
├── config_flow.py     # Configuration UI flow
├── const.py           # Constants and defaults
├── coordinator.py     # Data coordinator (Google Photos API and local folder scanning)
├── number.py          # Number entities (slide interval, refresh hours)
├── select.py          # Select entities (fill mode, orientation, order, aspect ratio)
├── sensor.py          # Sensor entities (photo count, album title)
├── store.py           # In-memory settings store
├── services.yaml      # Service definitions
├── strings.json       # UI strings
├── translations/      # Localisation files
├── manifest.json      # Integration manifest
└── icon.png / icon.svg / logo.png
```

### Code Style

- Follow existing code conventions in the repository.
- Use type hints where possible.
- Keep changes focused and minimal.

## Submitting a Pull Request

1. Create a feature branch from `main`.
2. Make your changes and verify they work in a Home Assistant instance.
3. Ensure all Python files pass syntax validation:
   ```bash
   python3 -c "import ast; ast.parse(open('file.py').read())"
   ```
4. Open a pull request with a clear description of the change.

## Code of Conduct

Be respectful and constructive. We are all here to improve the project.
