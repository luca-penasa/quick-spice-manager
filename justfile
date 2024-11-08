# A variable for the version file
VERSION_FILE := "pyproject.toml"
CHANGELOG := "CHANGELOG.md"

# Default task
default:
    @just --list

# Task to bump the version (major, minor, patch)
bump version_kind:
    @echo "Bumping {{version_kind}} version..."
    poetry run kacl-cli verify
    poetry run bump-my-version bump {{version_kind}} 

    @echo "New version will be: {{`poetry version -s`}}"
    just release-changelog {{`poetry version -s`}}

    # Commit the changes
    git add {{VERSION_FILE}} {{CHANGELOG}}
    git commit -m "Bump version to {{`poetry version -s`}} and update changelog"
    git tag v{{`poetry version -s`}}
    @echo "Version bump and changelog update complete."

# Task to release changelog with the new version
release-changelog version:
    @echo "Releasing changelog for version kind {{version}}..."
    poetry run kacl-cli release  {{version}}  -m --allow-no-changes

# Clean up task (optional)
clean:
    @echo "Cleaning..."
    rm -rf __pycache__ .pytest_cache

@audit:
  poetry run pip-audit
  poetry run deptry -- src tests