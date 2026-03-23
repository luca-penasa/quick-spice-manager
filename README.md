# Quick Spice Manager

[![PyPI](https://img.shields.io/pypi/v/quick-spice-manager?style=flat-square)](https://pypi.python.org/pypi/quick-spice-manager/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/quick-spice-manager?style=flat-square)](https://pypi.python.org/pypi/quick-spice-manager/)
[![PyPI - License](https://img.shields.io/pypi/l/quick-spice-manager?style=flat-square)](https://pypi.python.org/pypi/quick-spice-manager/)
[![Coookiecutter - Wolt](https://img.shields.io/badge/cookiecutter-Wolt-00c2e8?style=flat-square&logo=cookiecutter&logoColor=D4AA00&link=https://github.com/woltapp/wolt-python-package-cookiecutter)](https://github.com/woltapp/wolt-python-package-cookiecutter)


---

**Documentation**: [https://luca-penasa.github.io/quick-spice-manager](https://luca-penasa.github.io/quick-spice-manager)

**Source Code**: [https://github.com/luca-penasa/quick-spice-manager](https://github.com/luca-penasa/quick-spice-manager)

**PyPI**: [https://pypi.org/project/quick-spice-manager/](https://pypi.org/project/quick-spice-manager/)

---

`quick-spice-manager` provides a straightforward way to download, cache, and load ESA SPICE kernels for use with [`planetary-coverage`](https://planetary-coverage.readthedocs.io/). The core of the library is an FTP-based download engine that fetches kernels directly from the ESA public FTP server (`spiftp.esac.esa.int`) with parallel transfers and progress reporting. On top of that, `SpiceManager` wraps `planetary_coverage.TourConfig` to handle metakernel resolution, local caching, and environment-based overrides — so you can get a loaded kernel set in a single line.

Supported missions include JUICE, SOLAR-ORBITER, BEPICOLOMBO, MARS-EXPRESS, ROSETTA, and [many more](#supported-missions).

## Installation

```sh
pip install quick-spice-manager
```

## Usage

### Basic usage

```python
from quick_spice_manager import SpiceManager

# Downloads kernels automatically from the ESA FTP server and loads them.
# Kernels are cached in the platform user-cache directory and reused on
# subsequent calls — no re-download unless the cache is cleared.
sm = SpiceManager(spacecraft="JUICE", mk="plan")

# Access the underlying planetary_coverage.TourConfig
tc = sm.tour_config

# Inspect coverage timestamps, compute geometry, etc.
print(tc.coverage)
```

### Listing available metakernels

```python
sm = SpiceManager(spacecraft="JUICE")
print(sm.metakernels)  # e.g. ['plan', 'ops', ...]
```

### Inspecting the current configuration

```python
# Returns a pandas DataFrame — renders as a table in Jupyter
print(sm.config)
```

### Cache management

```python
print(sm.cache_size)   # human-readable size of the kernel cache
sm.clear_cache()       # delete cached kernels (will re-download on next use)
```

### Using a local metakernel

Pass an absolute path to an existing `.tm` file to skip FTP resolution:

```python
sm = SpiceManager(
    spacecraft="JUICE",
    mk="/path/to/my_local.tm",
    kernels_dir="/path/to/kernels",
)
tc = sm.tour_config
```

### Environment variable overrides

Two environment variables (readable from a `.env` file in the working directory) let you override the FTP-based workflow without changing code:

| Variable | Effect |
|---|---|
| `SPICE_METAKERNEL` | Path to a local metakernel file. Disables automatic FTP download. |
| `SPICE_DIRECTORY` | Directory containing the kernel files referenced by the metakernel. |

```sh
# .env
SPICE_METAKERNEL=/data/kernels/juice_ops.tm
SPICE_DIRECTORY=/data/kernels
```

### Supported missions

The FTP downloader supports the following ESA missions (case-insensitive, common aliases accepted):

| Mission | Accepted names |
|---|---|
| BepiColombo | `BEPICOLOMBO`, `MPO`, `MTM`, `MMO` |
| Comet Interceptor | `COMET-INTERCEPTOR` |
| EnVision | `ENVISION` |
| ExoMars 2016 | `EXOMARS2016`, `TGO`, `EDM` |
| ExoMars RSP | `EXOMARSRSP` |
| Gaia | `GAIA` |
| Hera | `HERA` |
| Huygens | `HUYGENS`, `CASP` |
| INTEGRAL | `INTEGRAL` |
| JUICE | `JUICE` |
| JWST | `JWST` |
| Mars Express | `MARS-EXPRESS`, `MEX`, `BEAGLE2` |
| Rosetta | `ROSETTA` |
| SMART-1 | `SMART-1` |
| Solar Orbiter | `SOLAR-ORBITER`, `SOLO` |
| Venus Express | `VENUS-EXPRESS`, `VEX` |

### Logging

Logging is disabled by default. Enable it for debugging:

```python
from quick_spice_manager import log_enable, log_enable_debug

log_enable()        # INFO level
log_enable_debug()  # DEBUG level
```

## Development

* Clone this repository
* Requirements:
  * [uv](https://docs.astral.sh/uv/)
  * Python 3.10+
* Create a virtual environment and install the dependencies

```sh
uv sync
```


### Testing

```sh
uv run pytest
```

### Documentation

The documentation is automatically generated from the content of the [docs directory](https://github.com/luca-penasa/quick-spice-manager/tree/master/docs) and from the docstrings
 of the public signatures of the source code. The documentation is updated and published as a [Github Pages page](https://pages.github.com/) automatically as part each release.



### Releasing

#### Manual release

Releases are done with the command, e.g. incrementing patch:

```bash
uv run just bump patch
# also push, of course:
git push origin main --tags
```

this will update the changelog, commit it, and make a corresponding tag.

as the CI is not yet configured for publish on pypi it can be done by hand:

```bash
uv build
uv publish --build path/to/wheel
```
#### Automatic release - to be fixed


Trigger the [Draft release workflow](https://github.com/luca-penasa/quick-spice-manager/actions/workflows/draft_release.yml)
(press _Run workflow_). This will update the changelog & version and create a GitHub release which is in _Draft_ state.

Find the draft release from the
[GitHub releases](https://github.com/luca-penasa/quick-spice-manager/releases) and publish it. When
 a release is published, it'll trigger [release](https://github.com/luca-penasa/quick-spice-manager/blob/master/.github/workflows/release.yml) workflow which creates PyPI
 release and deploys updated documentation.

### Updating with copier

To update the skeleton of the project using copier:
```sh
uvx copier update --defaults
```

### Pre-commit

Pre-commit hooks run all the auto-formatting (`ruff format`), linters (e.g. `ruff` and `mypy`), and other quality
 checks to make sure the changeset is in good shape before a commit/push happens.

You can install the hooks with (runs for each commit):

```sh
pre-commit install
```

Or if you want them to run only for each push:

```sh
pre-commit install -t pre-push
```

Or if you want e.g. want to run all checks manually for all files:

```sh
pre-commit run --all-files
```

---

This project was generated using [a fork](https://github.com/luca-penasa/wolt-python-package-cookiecutter) of the [wolt-python-package-cookiecutter](https://github.com/woltapp/wolt-python-package-cookiecutter) template.
