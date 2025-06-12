# A variable for the version file
VERSION_FILE := "pyproject.toml"
CHANGELOG := "CHANGELOG.md"

# Default task
default:
    @just --list


# Task to bump the version (major, minor, patch)
bump version_kind:
    @echo "Bumping {{version_kind}} version..."
    uv run kacl-cli verify
    uv run uv version --bump {{version_kind}}

    @echo "New version will be: {{`uv version --short`}}"
    just release-changelog {{`uv version --short`}}

    # Commit the changes
    git add {{VERSION_FILE}} {{CHANGELOG}}
    git commit -m "Bumped version to {{`uv version --short`}} and update changelog"
    git tag v{{`uv version --short`}}
    @echo "Version bump and changelog update complete."

# Task to release changelog with the new version
release-changelog version:
    @echo "Releasing changelog for version kind {{version}}..."
    uv run kacl-cli release  {{version}}  -m --allow-no-changes

# Clean up task (optional)
clean:
    @echo "Cleaning..."
    rm -rf __pycache__ .pytest_cache

audit:
  uv run pip-audit
  uv run deptry -- src tests
