#!/usr/bin/env bash
# build.sh — validate and pack the Apple Reminders MCP desktop extension
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SCRIPT_DIR}/dist"

FORCE=0
for arg in "$@"; do
    case "${arg}" in
        -f|--force) FORCE=1 ;;
        -h|--help)
            cat <<'USAGE'
usage: build.sh [--force]

Validates the manifest, checks the version agrees across the three files
that carry it, and packs the extension into dist/.

  -f, --force   Rebuild a version that has already been built, overwriting
                the existing artifact. Only safe when that artifact never
                left this machine (see the refusal message for why).
USAGE
            exit 0
            ;;
        *)
            echo "✗ Unknown argument: ${arg}" >&2
            echo "  Try: build.sh --help" >&2
            exit 2
            ;;
    esac
done

echo "=== Apple Reminders MCP — build ==="

# Check for mcpb CLI
if ! command -v mcpb &>/dev/null; then
    echo "Installing mcpb CLI…"
    npm install -g @anthropic-ai/mcpb
fi

# Validate
echo ""
echo "Validating manifest…"
mcpb validate "${SCRIPT_DIR}/manifest.json"
echo "✓ Manifest valid."

# The version lives in three files and the packer only reads one of them, so a
# drifted manifest would ship silently mislabelled. __init__.py matters as much
# as the other two: it is what the server reports to the MCP client over the
# wire, and it sat at 0.1.2 through two releases before anyone noticed.
VERSION=$(grep '^version' "${SCRIPT_DIR}/pyproject.toml" | head -1 | sed 's/version = "\(.*\)"/\1/')
MANIFEST_VERSION=$(python3 -c "import json;print(json.load(open('${SCRIPT_DIR}/manifest.json'))['version'])")
if [[ "${VERSION}" != "${MANIFEST_VERSION}" ]]; then
    echo "✗ Version mismatch: pyproject.toml is ${VERSION}, manifest.json is ${MANIFEST_VERSION}." >&2
    echo "  Set both to the same value and re-run." >&2
    exit 1
fi
INIT_VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${SCRIPT_DIR}/src/apple_reminders_mcp/__init__.py")
if [[ "${VERSION}" != "${INIT_VERSION}" ]]; then
    echo "✗ Version mismatch: pyproject.toml is ${VERSION}, __init__.py is ${INIT_VERSION}." >&2
    echo "  Set both to the same value and re-run." >&2
    exit 1
fi
echo "✓ Version ${VERSION} consistent across pyproject.toml, manifest.json, and __init__.py."

# Every tool the manifest advertises must actually exist on the server, or the
# installed extension promises a surface it does not have.
python3 - "${SCRIPT_DIR}" <<'PYCHECK'
import json, subprocess, sys
root = sys.argv[1]
listed = {t["name"] for t in json.load(open(f"{root}/manifest.json"))["tools"]}
src = open(f"{root}/src/apple_reminders_mcp/server.py").read()
missing = sorted(n for n in listed if f"def {n}(" not in src)
if missing:
    print(f"✗ manifest.json advertises tools server.py does not define: {', '.join(missing)}",
          file=sys.stderr)
    raise SystemExit(1)
print(f"✓ All {len(listed)} manifest tools are defined in server.py.")
PYCHECK

mkdir -p "${OUT}"
STABLE="${OUT}/apple-reminders.mcpb"
VERSIONED="${OUT}/apple-reminders-${VERSION}.mcpb"

# A built version is spent. The bundle carries no build number, so its
# version string is the only identity an artifact has -- and `mcpb pack`
# stamps every zip entry with the wall-clock time of the build, so two
# builds of identical source are never byte-identical. That means a
# rebuild cannot be checked against the original by re-packing and
# comparing: the second file simply overwrites the first and the
# difference becomes unrecoverable. Refuse, and make the bump explicit.
if [[ -e "${VERSIONED}" && "${FORCE}" -ne 1 ]]; then
    EXISTING_SHA=$(shasum -a 1 "${VERSIONED}" | cut -d' ' -f1)
    EXISTING_WHEN=$(date -r "${VERSIONED}" "+%Y-%m-%d %H:%M")
    echo "" >&2
    echo "✗ Version ${VERSION} has already been built." >&2
    echo "    ${VERSIONED##*/}  ${EXISTING_SHA}  (${EXISTING_WHEN})" >&2
    echo "" >&2
    echo "  Rebuilding would overwrite it with a different file carrying the" >&2
    echo "  same version — and builds are not reproducible, so the two could" >&2
    echo "  never be told apart afterwards." >&2
    echo "" >&2
    echo "  Bump the version in all three files, then re-run:" >&2
    echo "    pyproject.toml, manifest.json, src/apple_reminders_mcp/__init__.py" >&2
    echo "" >&2
    echo "  If that build never left this machine, overwrite it with:" >&2
    echo "    ./build.sh --force" >&2
    exit 1
fi
if [[ -e "${VERSIONED}" ]]; then
    echo "!  --force: overwriting the existing ${VERSION} artifact."
fi

# Pack
echo ""
mcpb pack "${SCRIPT_DIR}" "${STABLE}"

# Make the version visible in Finder. We do three things, because for an
# arbitrary `.mcpb` file (not an app bundle) Finder's Get Info pane does
# NOT read kMDItemVersion — it only reads CFBundleShortVersionString from
# Info.plist for bundles. So:
#
#   1. Write a Spotlight comment via Finder, which shows in Get Info →
#      Comments. AppleScript here drives Finder so the comment lands in
#      both Spotlight and the parent folder's .DS_Store.
#   2. Stamp kMDItemVersion as well, for tools / scripts that DO read it
#      (e.g. `mdls`, Spotlight-based file managers).
#   3. Produce a versioned filename copy alongside the stable name. The
#      version is then visible in the filename itself — the bulletproof
#      display that survives copy/upload/quarantine stripping the xattrs.
cp -f "${STABLE}" "${VERSIONED}"
for F in "${STABLE}" "${VERSIONED}"; do
    osascript -e "tell application \"Finder\" to set comment of (POSIX file \"${F}\" as alias) to \"Apple Reminders MCP — v${VERSION}\"" >/dev/null 2>&1 || true
    xattr -w "com.apple.metadata:kMDItemVersion" "${VERSION}" "${F}" 2>/dev/null || true
    mdimport "${F}" 2>/dev/null || true
done

# Record the artifact's checksum. Builds are not reproducible -- `mcpb pack`
# stamps every zip entry with the time of the build -- so a version's hash
# cannot be recovered by rebuilding it later. This ledger is the only place
# it survives. The moving `apple-reminders.mcpb` copy is deliberately left
# out: it is replaced on every build, so listing it would break verification.
LEDGER="${OUT}/SHA1SUMS"
VERSIONED_NAME="$(basename "${VERSIONED}")"

if [[ ! -e "${LEDGER}" ]]; then
    {
        echo "# SHA-1 checksums of every Apple Reminders MCP bundle built from this repo."
        echo "#"
        echo "# Verify the archive:      cd dist && shasum -c SHA1SUMS"
        echo "# Ignoring pruned files:   cd dist && shasum -c --ignore-missing SHA1SUMS"
        echo "#"
        echo "# Appended by build.sh, one line per version, in build order. A --force"
        echo "# rebuild replaces that version's line. Keep comment lines starting with"
        echo "# '#'; a blank line makes shasum report a formatting warning."
    } > "${LEDGER}"
fi

# --force rebuilds a version that is already listed: replace its line rather
# than leaving two entries claiming different hashes for one version.
LEDGER_RE="  ${VERSIONED_NAME//./\\.}$"        # dots are filename literals, not wildcards
if grep -q "${LEDGER_RE}" "${LEDGER}" 2>/dev/null; then
    grep -v "${LEDGER_RE}" "${LEDGER}" > "${LEDGER}.tmp"
    mv "${LEDGER}.tmp" "${LEDGER}"
fi
( cd "${OUT}" && shasum -a 1 "${VERSIONED_NAME}" ) >> "${LEDGER}"

echo ""
echo "✓ Built:"
echo "    ${STABLE}        (stable name — drag-install target)"
echo "    ${VERSIONED}    (versioned name — visible version in filename)"
echo ""
echo "Both files have the version stamped into Finder Comments (Get Info → Comments)."
echo "Checksum recorded in dist/SHA1SUMS — verify with: cd dist && shasum -c SHA1SUMS"
echo ""
echo "To install: double-click the .mcpb file, or drag it into Claude Desktop."
echo ""
echo "ℹ  macOS will prompt for Reminders access on first use. You can also pre-grant"
echo "   under System Settings → Privacy & Security → Reminders."
echo "ℹ  Reading real tags additionally needs Full Disk Access — see README."
