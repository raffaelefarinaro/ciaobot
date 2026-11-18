#!/bin/sh
# Ciaobot engine launcher, installed as bin/ciao by mac/scripts/engine_assemble.py.
# Runs the bundled interpreter isolated (-I -B) with a sanitised environment.
# Uses shell builtins only: it must work under env -i with no PATH.
case "$0" in
  */*) ;;
  *) echo "ciao: run the engine launcher by its path" >&2; exit 70 ;;
esac
if [ -L "$0" ]; then
  echo "ciao: run the engine launcher by its real path, not a symlink" >&2
  exit 70
fi
root=$(CDPATH= cd -P -- "${0%/*}/.." && pwd -P) || exit 70
python="$root/bin/python3.12"
cafile="$root/lib/python3.12/site-packages/certifi/cacert.pem"
if [ ! -f "$python" ] || [ ! -f "$cafile" ]; then
  echo "ciao: the engine is incomplete" >&2
  exit 70
fi
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONEXECUTABLE \
  PYTHONPLATLIBDIR PYTHONBREAKPOINT PYTHONWARNINGS PYTHONPYCACHEPREFIX \
  DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH \
  DYLD_FALLBACK_FRAMEWORK_PATH DYLD_VERSIONED_LIBRARY_PATH DYLD_VERSIONED_FRAMEWORK_PATH \
  DYLD_IMAGE_SUFFIX DYLD_ROOT_PATH DYLD_SHARED_REGION DYLD_SHARED_CACHE_DIR DYLD_FORCE_FLAT_NAMESPACE \
  SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE OPENSSL_CONF OPENSSL_MODULES OPENSSL_ENGINES SSLKEYLOGFILE \
  NODE_EXTRA_CA_CERTS NODE_OPTIONS NODE_TLS_REJECT_UNAUTHORIZED
SSL_CERT_FILE=$cafile
PYTHONDONTWRITEBYTECODE=1
export SSL_CERT_FILE PYTHONDONTWRITEBYTECODE
exec "$python" -I -B -m ciao "$@"
