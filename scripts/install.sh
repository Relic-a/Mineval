#!/bin/sh
# Requires curl, sha256sum, and GitHub CLI (for cryptographic provenance).
set -eu
repo=Relic-a/Mineval
version=${1:?Usage: install.sh vVERSION | uninstall}
bin_dir=${MINEVAL_BIN_DIR:-"$HOME/.local/bin"}
if [ "$version" = uninstall ]; then
  if [ -f "$bin_dir/.mineval-installed" ]; then
    rm -f "$bin_dir/mineval" "$bin_dir/.mineval-installed"
    printf '%s\n' 'CLI removed. Settings and benchmark data preserved.'
  else
    printf '%s\n' 'No installer ownership marker; remove your manual installation explicitly.' >&2
    exit 1
  fi
  exit 0
fi
case "$version" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo 'Use an explicit vMAJOR.MINOR.PATCH release tag' >&2; exit 1;; esac
case "$version" in *[!a-zA-Z0-9._-]*) exit 1;; esac
[ "$(uname -s)" = Linux ] || { echo 'Only Linux is supported' >&2; exit 1; }
case "$(uname -m)" in x86_64) arch=x86_64;; *) echo 'Only Linux x86_64 is currently supported' >&2; exit 1;; esac
for tool in curl sha256sum gh; do command -v "$tool" >/dev/null || { echo "Install $tool first" >&2; exit 1; }; done
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
artifact=mineval-$version-linux-$arch
base=https://github.com/$repo/releases/download/$version
curl --fail --location --proto '=https' --tlsv1.2 "$base/$artifact" -o "$tmp/$artifact"
curl --fail --location --proto '=https' --tlsv1.2 "$base/$artifact.sha256" -o "$tmp/checksum"
# Verify publisher workflow identity as well as checksum. Failure is fatal.
gh attestation verify "$tmp/$artifact" --repo "$repo" --signer-workflow "$repo/.github/workflows/release.yml" --source-ref "refs/tags/$version"
(cd "$tmp" && sha256sum --check checksum)
mkdir -p "$bin_dir"
if [ -e "$bin_dir/mineval" ] && [ ! -f "$bin_dir/.mineval-installed" ]; then
  echo 'Refusing to replace an unmanaged mineval executable' >&2; exit 1
fi
install -m 755 "$tmp/$artifact" "$bin_dir/.mineval-new"
mv -f "$bin_dir/.mineval-new" "$bin_dir/mineval"
printf '%s\n' "$version" > "$bin_dir/.mineval-installed"
printf 'Installed %s/mineval. Add this directory to PATH if needed; shell files were not changed.\n' "$bin_dir"
