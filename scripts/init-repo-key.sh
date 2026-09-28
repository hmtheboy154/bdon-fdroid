#!/usr/bin/env bash
#
# Generate the F-Droid repository signing key and print everything needed to
# configure GitHub Actions.
#
# F-Droid repositories are signed so clients can tell that the index they
# downloaded really came from the repository they added. That signature needs a
# key pair, and the private half must never be committed. This script creates one
# and prints the values to paste into the repository's Actions secrets.
#
# Usage:
#   ./scripts/init-repo-key.sh                       # defaults shown below
#   ALIAS=my-repo PASS='a long pass' ./scripts/init-repo-key.sh
#   KEYSTORE=/secure/place/keystore.p12 ./scripts/init-repo-key.sh
#
# After running, copy keystore.p12 somewhere safe (it cannot be regenerated:
# a new key means every user has to remove and re-add the repository).

set -euo pipefail

ALIAS="${ALIAS:-bdon-fdroid}"
DNAME="${DNAME:-CN=bdon-fdroid repository key, OU=Self-hosted F-Droid repo, O=bdon-fdroid, C=US}"
PASS="${PASS:-}"
KEYSTORE="${KEYSTORE:-keystore.p12}"
VALIDITY="${VALIDITY:-36500}" # ~100 years; the key outlives any device
KEYSIZE="${KEYSIZE:-4096}"

say() { printf '\033[1m==>\033[0m %s\n' "$1"; }
die() { printf 'error: %s\n' "$1" >&2; exit 1; }

command -v keytool >/dev/null 2>&1 || die "keytool not found; install a JDK (e.g. 'apt install default-jdk')"

if [ -z "$PASS" ]; then
  read -r -s -p "Password for the new keystore (at least 6 characters): " PASS
  echo
  read -r -s -p "Confirm: " PASS2
  echo
  [ "$PASS" = "$PASS2" ] || die "the two passwords did not match"
fi
[ "${#PASS}" -ge 6 ] || die "the password must be at least 6 characters"

if [ -e "$KEYSTORE" ]; then
  die "$KEYSTORE already exists. Move it aside first: a published repository's
     key can never be replaced without breaking every existing installation."
fi

say "Generating a $KEYSIZE-bit RSA key"
keytool -genkeypair -noprompt \
  -alias "$ALIAS" \
  -keyalg RSA -keysize "$KEYSIZE" \
  -validity "$VALIDITY" \
  -dname "$DNAME" \
  -keystore "$KEYSTORE" \
  -storetype PKCS12 \
  -storepass "$PASS" \
  -keypass "$PASS" >/dev/null 2>&1

FINGERPRINT=$(keytool -list -v -keystore "$KEYSTORE" -alias "$ALIAS" -storepass "$PASS" 2>/dev/null \
  | grep -i 'SHA256:' | head -1 | sed 's/.*SHA256: *//; s/://g' | tr '[:upper:]' '[:lower:]')

[ -n "$FINGERPRINT" ] || die "could not read the fingerprint back from $KEYSTORE"

cat <<EOF

$(say "Done.")

  Keystore : $KEYSTORE
  Alias    : $ALIAS
  DN       : $DNAME

  Signing key SHA-256 fingerprint (this is what users verify the repository
  against, and it is shown on the GitHub Pages site):

      $FINGERPRINT

$(say "Add these three secrets")
  Settings -> Secrets and variables -> Actions -> New repository secret

    KEYSTORE_BASE64   = the base64 of the keystore, from the block below
    KEYSTORE_PASS     = the password you just chose
    REPO_KEYALIAS     = $ALIAS

$(say "The keystore, base64-encoded")
$(base64 < "$KEYSTORE" | tr -d '\n')

$(say "Next")
  1. Store $KEYSTORE somewhere safe and delete the copy in this directory.
     Losing it means users must remove and re-add the repository.
  2. Paste the three secrets above into the repository's Actions settings.
  3. See docs/setup-github-actions-and-pages.md for enabling GitHub Pages
     and running the first build.
EOF
